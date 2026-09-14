"""The quality-check store: subject index, reviewers, assignments and dataset writes.

Everything the server owns lives in ``<dataset_root>/<state_dir>/``::

    .bonehub_qc/
    |-- config.json           policy, see config.QCServerConfig
    |-- server_private_key    generated on first start
    |-- admin_key             generated on first start
    |-- users.json            reviewers and their API key digests
    |-- assignments.json      which subject is with whom, and how it ended
    |-- submissions.jsonl     append-only audit trail
    |-- server.log
    |-- backups/              previous segmentations, kept before overwriting
    `-- tmp/                  uploads being validated

A single re-entrant lock serialises state and dataset writes. The work is human-paced,
so the simplicity is worth more than the concurrency; run the server with one worker.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import nibabel as nib
import numpy as np

from bonehub_data_schema import BoneLabelMap, DatasetInfo, SubjectInfo
from bonehub_data_schema.bonehub_dataset_io import DATASET_ZFILL, SUBJECT_ZFILL

from . import auth
from .audit import AuditLog, utc_now_iso
from .config import CONFIG_FILE_NAME, QCServerConfig
from .models import TERMINAL_STATES, Assignment, QueueStats, User

USERS_FILE_NAME = "users.json"
ASSIGNMENTS_FILE_NAME = "assignments.json"
BACKUP_DIR_NAME = "backups"
TMP_DIR_NAME = "tmp"

#: Marker for an argument that was not supplied, where ``None`` is itself a valid value.
UNSET = object()

LABEL_NAME_TO_VALUE: dict[str, int] = {label.name: label.value for label in BoneLabelMap}
LABEL_VALUE_TO_NAME: dict[int, str] = {label.value: label.name for label in BoneLabelMap}


class QCError(Exception):
    """Something the caller did wrong. Carries the HTTP status the API should return."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class SubjectRef:
    """One indexed subject that is a candidate for quality check."""

    dataset_id: int
    subject_id: int
    subject_key: str
    has_image: bool
    has_segmentation: bool
    segmentation_labels: dict[str, int] = field(default_factory=dict)

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.dataset_id, self.subject_id)


def subject_key_of(dataset_id: int, subject_id: int) -> str:
    return f"{str(dataset_id).zfill(DATASET_ZFILL)}_{str(subject_id).zfill(SUBJECT_ZFILL)}"


class QCStore:
    """All server state and every write that touches the dataset folder."""

    def __init__(self, dataset_root: Path, state_dir: Path, config: QCServerConfig | None = None):
        if not dataset_root.is_dir():
            raise RuntimeError(f"Dataset root '{dataset_root}' does not exist or is not a directory.")

        self.dataset_root = dataset_root
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / BACKUP_DIR_NAME).mkdir(exist_ok=True)
        (self.state_dir / TMP_DIR_NAME).mkdir(exist_ok=True)

        self.config_path = self.state_dir / CONFIG_FILE_NAME
        self.config = config or QCServerConfig.load(self.config_path)
        self.config.save(self.config_path)

        self.private_key = auth.load_or_create_private_key(self.state_dir)
        self.admin_key, self.admin_key_generated = auth.load_or_create_admin_key(self.state_dir)

        self.audit = AuditLog(self.dataset_root, self.state_dir)
        self._lock = threading.RLock()

        self._users: dict[str, User] = self._load_users()
        self._assignments: dict[str, Assignment] = self._load_assignments()

        self._index: list[SubjectRef] = []
        self._index_by_key: dict[str, SubjectRef] = {}
        self._dataset_info: dict[int, dict] = {}
        self._index_built_at: datetime | None = None
        self._total_subjects = 0
        self.refresh_index()

    # ------------------------------------------------------------------ paths
    def dataset_path(self, dataset_id: int) -> Path:
        return self.dataset_root / f"Dataset_{str(dataset_id).zfill(DATASET_ZFILL)}"

    def subject_info_path(self, dataset_id: int) -> Path:
        padded = str(dataset_id).zfill(DATASET_ZFILL)
        return self.dataset_path(dataset_id) / f"Subject_info_{padded}.json"

    def dataset_info_path(self, dataset_id: int) -> Path:
        padded = str(dataset_id).zfill(DATASET_ZFILL)
        return self.dataset_path(dataset_id) / f"Dataset_info_{padded}.json"

    def image_path(self, dataset_id: int, subject_id: int) -> Path:
        return self.dataset_path(dataset_id) / "Image" / f"{subject_key_of(dataset_id, subject_id)}.nii.gz"

    def segmentation_path(self, dataset_id: int, subject_id: int) -> Path:
        """Where the segmentation belongs, whether or not it exists yet."""
        return self.dataset_path(dataset_id) / "Segmentation" / f"{subject_key_of(dataset_id, subject_id)}.nii.gz"

    # ------------------------------------------------------------------ index
    def refresh_index(self) -> None:
        """Rebuild the list of subjects eligible for quality check from disk."""
        index: list[SubjectRef] = []
        dataset_info: dict[int, dict] = {}
        total = 0

        for dataset_dir in sorted(self.dataset_root.glob("Dataset_*")):
            if not dataset_dir.is_dir():
                continue
            try:
                dataset_id = int(dataset_dir.name.split("_")[1])
            except (IndexError, ValueError):
                self.audit.event(f"Skipping '{dataset_dir.name}': the folder name carries no dataset id.")
                continue
            if self.config.allowed_dataset_ids is not None and dataset_id not in self.config.allowed_dataset_ids:
                continue

            subjects = self._read_subject_info(dataset_id, missing_ok=True)
            if subjects is None:
                continue
            total += len(subjects)
            dataset_info[dataset_id] = self._read_dataset_info(dataset_id)

            for subject in subjects:
                if subject.subject_id is None:
                    continue
                if not self._is_eligible(subject):
                    continue
                if not self.image_path(dataset_id, subject.subject_id).exists():
                    self.audit.event(
                        f"Subject {subject_key_of(dataset_id, subject.subject_id)} is eligible but its image file "
                        f"is missing; it will not be handed out."
                    )
                    continue
                index.append(
                    SubjectRef(
                        dataset_id=dataset_id,
                        subject_id=subject.subject_id,
                        subject_key=subject_key_of(dataset_id, subject.subject_id),
                        has_image=True,
                        has_segmentation=self.segmentation_path(dataset_id, subject.subject_id).exists(),
                        segmentation_labels=dict(subject.segmentation or {}),
                    )
                )

        index.sort(key=lambda ref: ref.sort_key)
        with self._lock:
            self._index = index
            self._index_by_key = {ref.subject_key: ref for ref in index}
            self._dataset_info = dataset_info
            self._total_subjects = total
            self._index_built_at = datetime.now(timezone.utc)
        self.audit.event(f"Subject index rebuilt: {len(index)} eligible of {total} subjects.")

    def _ensure_fresh_index(self) -> None:
        """Rebuild when the cached index has aged past ``index_refresh_seconds``."""
        ttl = self.config.index_refresh_seconds
        with self._lock:
            built_at = self._index_built_at
        if ttl == 0 or built_at is None:
            self.refresh_index()
            return
        if datetime.now(timezone.utc) - built_at > timedelta(seconds=ttl):
            self.refresh_index()

    def _is_eligible(self, subject: SubjectInfo) -> bool:
        if not subject.image:
            return False
        segmentation = subject.segmentation or {}
        if not segmentation:
            return self.config.include_subjects_without_segmentation
        return any(value in self.config.eligible_label_values for value in segmentation.values())

    # ------------------------------------------------------------------ users
    @property
    def users_path(self) -> Path:
        return self.state_dir / USERS_FILE_NAME

    def _load_users(self) -> dict[str, User]:
        if not self.users_path.exists():
            return {}
        with open(self.users_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {entry["name"]: User(**entry) for entry in raw}

    def _save_users(self) -> None:
        _atomic_write_json(self.users_path, [user.model_dump() for user in self._users.values()])

    def list_users(self) -> list[dict]:
        with self._lock:
            users = [user.public_dict() for user in self._users.values()]
        counts = self._assignment_counts_per_user()
        for user in users:
            user.update(counts.get(user["name"], {"open": 0, "confirmed": 0, "rejected": 0}))
        return sorted(users, key=lambda u: u["name"].lower())

    def create_user(self, name: str, allowed_dataset_ids: list[int] | None = None, note: str = "") -> tuple[User, str]:
        """Create a reviewer and return the user together with its plaintext API key.

        The plaintext key is returned exactly once; only its digest is stored.
        """
        name = name.strip()
        if not name:
            raise QCError("A user name is required.")
        with self._lock:
            if name in self._users:
                raise QCError(f"User '{name}' already exists.", status_code=409)
            api_key = auth.generate_api_key()
            user = User(
                name=name,
                key_prefix=auth.key_prefix(api_key),
                key_hash=auth.hash_api_key(api_key, self.private_key),
                created_at=utc_now_iso(),
                active=True,
                allowed_dataset_ids=allowed_dataset_ids,
                note=note,
            )
            self._users[name] = user
            self._save_users()
        self.audit.record(
            "user_created", {"user": name, "key_prefix": user.key_prefix}, summary=f"Created user '{name}'."
        )
        return user, api_key

    def rotate_user_key(self, name: str) -> str:
        """Issue a new API key for a reviewer and invalidate the old one."""
        with self._lock:
            user = self._require_user(name)
            api_key = auth.generate_api_key()
            user.key_prefix = auth.key_prefix(api_key)
            user.key_hash = auth.hash_api_key(api_key, self.private_key)
            self._save_users()
        self.audit.record("user_key_rotated", {"user": name}, summary=f"Rotated API key for user '{name}'.")
        return api_key

    def set_user_active(self, name: str, active: bool) -> User:
        with self._lock:
            user = self._require_user(name)
            user.active = active
            self._save_users()
        self.audit.record(
            "user_enabled" if active else "user_disabled",
            {"user": name},
            summary=f"{'Enabled' if active else 'Disabled'} user '{name}'.",
        )
        return user

    def update_user(self, name: str, allowed_dataset_ids=UNSET, note: str | None = None) -> User:
        """Change a reviewer. An argument left out is not touched.

        ``allowed_dataset_ids`` takes ``None`` to mean "every dataset the server serves",
        so it needs :data:`UNSET` to tell that apart from "leave the restriction alone" --
        otherwise editing only the note would quietly widen a reviewer's access.
        """
        with self._lock:
            user = self._require_user(name)
            if allowed_dataset_ids is not UNSET:
                user.allowed_dataset_ids = allowed_dataset_ids
            if note is not None:
                user.note = note
            self._save_users()
        self.audit.record("user_updated", {"user": name}, summary=f"Updated user '{name}'.")
        return user

    def delete_user(self, name: str) -> None:
        """Remove a reviewer and release whatever they were still holding."""
        with self._lock:
            self._require_user(name)
            del self._users[name]
            self._save_users()
            for assignment in self._assignments.values():
                if assignment.user == name and assignment.state == "assigned":
                    assignment.state = "released"
            self._save_assignments()
        self.audit.record("user_deleted", {"user": name}, summary=f"Deleted user '{name}'.")

    def authenticate(self, api_key: str | None) -> User:
        if not api_key:
            raise QCError("Missing API key. Send it in the 'X-API-Key' header.", status_code=401)
        candidate = auth.hash_api_key(api_key, self.private_key)
        with self._lock:
            for user in self._users.values():
                if auth.keys_match(candidate, user.key_hash):
                    if not user.active:
                        raise QCError(f"User '{user.name}' is disabled.", status_code=403)
                    return user
        raise QCError("Invalid API key.", status_code=401)

    def is_admin_key(self, admin_key: str | None) -> bool:
        if not admin_key:
            return False
        # Compare digests rather than raw strings, so the comparison is constant-time over
        # a fixed length no matter how long the supplied key is.
        return auth.keys_match(
            auth.hash_api_key(admin_key, self.private_key),
            auth.hash_api_key(self.admin_key, self.private_key),
        )

    def _require_user(self, name: str) -> User:
        user = self._users.get(name)
        if user is None:
            raise QCError(f"User '{name}' does not exist.", status_code=404)
        return user

    # ------------------------------------------------------------ assignments
    @property
    def assignments_path(self) -> Path:
        return self.state_dir / ASSIGNMENTS_FILE_NAME

    def _load_assignments(self) -> dict[str, Assignment]:
        if not self.assignments_path.exists():
            return {}
        with open(self.assignments_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {entry["assignment_id"]: Assignment(**entry) for entry in raw}

    def _save_assignments(self) -> None:
        _atomic_write_json(self.assignments_path, [a.model_dump() for a in self._assignments.values()])

    def _expire_stale_assignments(self) -> None:
        """Return leases whose TTL has passed to the queue. Caller holds the lock."""
        now = datetime.now(timezone.utc)
        changed = False
        for assignment in self._assignments.values():
            if assignment.state != "assigned":
                continue
            if _parse_iso(assignment.expires_at) < now:
                assignment.state = "expired"
                changed = True
                self.audit.event(
                    f"Assignment {assignment.assignment_id} ({assignment.subject_key}, {assignment.user}) expired."
                )
        if changed:
            self._save_assignments()

    def _assignments_for_subject(self, subject_key: str) -> list[Assignment]:
        return [a for a in self._assignments.values() if a.subject_key == subject_key]

    def _assignment_counts_per_user(self) -> dict[str, dict]:
        counts: dict[str, dict] = {}
        with self._lock:
            for assignment in self._assignments.values():
                bucket = counts.setdefault(assignment.user, {"open": 0, "confirmed": 0, "rejected": 0})
                if assignment.state == "assigned":
                    bucket["open"] += 1
                elif assignment.state == "confirmed":
                    bucket["confirmed"] += 1
                elif assignment.state == "rejected":
                    bucket["rejected"] += 1
        return counts

    def _subject_is_available(self, subject_key: str, user: User) -> bool:
        """Caller holds the lock and has already expired stale leases."""
        for assignment in self._assignments_for_subject(subject_key):
            if assignment.state in TERMINAL_STATES:
                return False
            if assignment.state == "assigned":
                return False
            if assignment.state == "rejected":
                # A rejected subject stays out of the queue unless the policy says otherwise,
                # and never goes back to the reviewer who rejected it.
                if not self.config.requeue_rejected or assignment.user == user.name:
                    return False
        return True

    def open_assignments_of(self, user_name: str) -> list[Assignment]:
        with self._lock:
            self._expire_stale_assignments()
            return [a for a in self._assignments.values() if a.user == user_name and a.state == "assigned"]

    def next_subject(self, user: User) -> Assignment:
        """Pick a free subject for this reviewer and lease it to them."""
        self._ensure_fresh_index()
        with self._lock:
            self._expire_stale_assignments()

            open_assignments = [a for a in self._assignments.values() if a.user == user.name and a.state == "assigned"]
            if len(open_assignments) >= self.config.max_concurrent_assignments_per_user:
                # Hand back what they already hold rather than refusing outright, so a client
                # that lost its local copy can pick the same work back up.
                return sorted(open_assignments, key=lambda a: a.assigned_at)[0]

            candidates = [ref for ref in self._index if self._user_may_access(user, ref.dataset_id)]
            if self.config.assignment_strategy == "random":
                random.shuffle(candidates)

            for ref in candidates:
                if not self._subject_is_available(ref.subject_key, user):
                    continue
                now = datetime.now(timezone.utc)
                assignment = Assignment(
                    assignment_id=uuid.uuid4().hex,
                    subject_key=ref.subject_key,
                    dataset_id=ref.dataset_id,
                    subject_id=ref.subject_id,
                    user=user.name,
                    state="assigned",
                    assigned_at=_to_iso(now),
                    expires_at=_to_iso(now + timedelta(seconds=self.config.lease_ttl_seconds)),
                )
                self._assignments[assignment.assignment_id] = assignment
                self._save_assignments()
                self.audit.record(
                    "assigned",
                    {
                        "assignment_id": assignment.assignment_id,
                        "user": user.name,
                        "dataset_id": ref.dataset_id,
                        "subject_id": ref.subject_id,
                        "subject_key": ref.subject_key,
                        "expires_at": assignment.expires_at,
                    },
                    dataset_id=ref.dataset_id,
                    summary=(
                        f"Assigned subject {ref.subject_key} to '{user.name}' "
                        f"(assignment {assignment.assignment_id})."
                    ),
                )
                return assignment

        raise QCError("No subject is available for quality check right now.", status_code=404)

    def get_assignment(self, assignment_id: str, user: User | None = None) -> Assignment:
        with self._lock:
            assignment = self._assignments.get(assignment_id)
        if assignment is None:
            raise QCError(f"Assignment '{assignment_id}' does not exist.", status_code=404)
        if user is not None and assignment.user != user.name:
            raise QCError("This assignment belongs to another reviewer.", status_code=403)
        return assignment

    def release_assignment(self, assignment_id: str, user: User | None = None) -> Assignment:
        """Give a subject back without judging it, so somebody else can review it."""
        with self._lock:
            assignment = self.get_assignment(assignment_id, user)
            if assignment.state != "assigned":
                raise QCError(f"Assignment '{assignment_id}' is already {assignment.state}.", status_code=409)
            assignment.state = "released"
            self._save_assignments()
        self.audit.record(
            "released",
            {
                "assignment_id": assignment_id,
                "user": assignment.user,
                "subject_key": assignment.subject_key,
                "dataset_id": assignment.dataset_id,
                "subject_id": assignment.subject_id,
            },
            dataset_id=assignment.dataset_id,
            summary=f"Released subject {assignment.subject_key} back to the queue.",
        )
        return assignment

    def extend_assignment(self, assignment_id: str, user: User) -> Assignment:
        """Push the lease out by another TTL while a reviewer is still working."""
        with self._lock:
            assignment = self.get_assignment(assignment_id, user)
            if assignment.state != "assigned":
                raise QCError(f"Assignment '{assignment_id}' is already {assignment.state}.", status_code=409)
            assignment.expires_at = _to_iso(
                datetime.now(timezone.utc) + timedelta(seconds=self.config.lease_ttl_seconds)
            )
            self._save_assignments()
        return assignment

    def _user_may_access(self, user: User, dataset_id: int) -> bool:
        if self.config.allowed_dataset_ids is not None and dataset_id not in self.config.allowed_dataset_ids:
            return False
        if user.allowed_dataset_ids is None:
            return True
        return dataset_id in user.allowed_dataset_ids

    # ------------------------------------------------------------ dataset i/o
    def _read_subject_info(self, dataset_id: int, missing_ok: bool = False) -> list[SubjectInfo] | None:
        path = self.subject_info_path(dataset_id)
        if not path.exists():
            if missing_ok:
                self.audit.event(f"No Subject_info file at '{path}'; dataset skipped.")
                return None
            raise QCError(f"Subject info file '{path.name}' is missing.", status_code=500)
        try:
            # Reading and validating are guarded together: a dataset whose file is
            # truncated, half-written or hand-edited must not take the whole server down.
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return [SubjectInfo(**entry) for entry in raw]
        except Exception as exc:
            if missing_ok:
                self.audit.event(f"Could not parse '{path}': {exc}")
                return None
            raise QCError(f"Could not parse '{path.name}': {exc}", status_code=500) from exc

    def _read_dataset_info(self, dataset_id: int) -> dict:
        path = self.dataset_info_path(dataset_id)
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return DatasetInfo(**json.load(f)).sorted_dict()

    def dataset_info(self, dataset_id: int) -> dict:
        with self._lock:
            cached = self._dataset_info.get(dataset_id)
        return cached if cached is not None else self._read_dataset_info(dataset_id)

    def subject_info(self, dataset_id: int, subject_id: int) -> SubjectInfo:
        subjects = self._read_subject_info(dataset_id)
        for subject in subjects or []:
            if subject.subject_id == subject_id:
                return subject
        raise QCError(
            f"Subject {subject_key_of(dataset_id, subject_id)} is not in the subject info file.", status_code=404
        )

    def _mutate_subject_info(
        self, dataset_id: int, subject_id: int, mutate: Callable[[SubjectInfo], None]
    ) -> SubjectInfo:
        """Re-read, modify one subject and write the file back atomically.

        The file is always re-read from disk so a long-running server never writes back a
        stale copy of a dataset that another tool edited in the meantime.
        """
        path = self.subject_info_path(dataset_id)
        subjects = self._read_subject_info(dataset_id) or []
        target = next((s for s in subjects if s.subject_id == subject_id), None)
        if target is None:
            raise QCError(
                f"Subject {subject_key_of(dataset_id, subject_id)} is not in the subject info file.", status_code=404
            )
        mutate(target)
        _atomic_write_json(path, [subject.sorted_dict() for subject in subjects])
        return target

    # ------------------------------------------------------------- submission
    def inspect_segmentation(self, seg_path: Path, dataset_id: int, subject_id: int) -> dict[str, int]:
        """Validate an uploaded segmentation and return the labels it contains.

        Returns ``{label_name: voxel_value}`` for every non-background value present.
        """
        try:
            seg_img = nib.load(str(seg_path))
            seg_data = np.asanyarray(seg_img.dataobj)
        except Exception as exc:
            raise QCError(f"The uploaded segmentation could not be read as NIfTI: {exc}") from exc

        if self.config.require_geometry_match:
            image_file = self.image_path(dataset_id, subject_id)
            if image_file.exists():
                image_img = nib.load(str(image_file))
                if tuple(seg_img.shape[:3]) != tuple(image_img.shape[:3]):
                    raise QCError(
                        f"Segmentation shape {tuple(seg_img.shape[:3])} does not match the image "
                        f"{tuple(image_img.shape[:3])}."
                    )
                if not np.allclose(seg_img.affine, image_img.affine, atol=1e-3):
                    raise QCError("The segmentation affine does not match the image affine.")

        labels: dict[str, int] = {}
        unknown: list[int] = []
        for raw_value in np.unique(seg_data):
            value = int(round(float(raw_value)))
            if value == 0:
                continue
            name = LABEL_VALUE_TO_NAME.get(value)
            if name is None:
                unknown.append(value)
            else:
                labels[name] = value
        if unknown:
            raise QCError(
                f"The segmentation contains voxel values that are not BoneHub labels: {sorted(unknown)}. "
                f"See 'bonehub_data_schema/labelmap.py'."
            )
        return labels

    def submit(
        self,
        assignment_id: str,
        user: User,
        quality_check_confirmed: bool,
        segmentation_tmp_path: Path | None,
        confirmed_labels: list[str] | None = None,
        comment: str | None = None,
    ) -> "SubmissionOutcome":
        """Apply a reviewer's verdict.

        ``quality_check_confirmed=False`` changes nothing in the dataset: the segmentation
        file and Subject_info are left exactly as they are, and only the audit trail and
        the assignment state record that the subject was looked at and rejected.
        """
        assignment = self.get_assignment(assignment_id, user)
        if assignment.state not in {"assigned", "expired"}:
            raise QCError(f"Assignment '{assignment_id}' was already submitted ({assignment.state}).", status_code=409)

        dataset_id, subject_id = assignment.dataset_id, assignment.subject_id

        if not quality_check_confirmed:
            return self._finish_rejected(assignment, comment)

        if segmentation_tmp_path is None:
            raise QCError("A confirmed submission must include the reviewed segmentation file.")

        # Validation is the expensive part, so it happens before the lock is taken.
        present_labels = self.inspect_segmentation(segmentation_tmp_path, dataset_id, subject_id)
        if not present_labels:
            raise QCError("The uploaded segmentation is empty; there is nothing to confirm.")

        if confirmed_labels is None:
            labels_to_confirm = sorted(present_labels)
        else:
            unknown = [label for label in confirmed_labels if label not in LABEL_NAME_TO_VALUE]
            if unknown:
                raise QCError(f"Unknown label names: {unknown}. See 'bonehub_data_schema/labelmap.py'.")
            missing = [label for label in confirmed_labels if label not in present_labels]
            if missing:
                raise QCError(f"These confirmed labels are not present in the uploaded segmentation: {missing}.")
            labels_to_confirm = sorted(set(confirmed_labels))

        return self._finish_confirmed(assignment, segmentation_tmp_path, present_labels, labels_to_confirm, comment)

    def _finish_rejected(self, assignment: Assignment, comment: str | None) -> "SubmissionOutcome":
        with self._lock:
            assignment.state = "rejected"
            assignment.quality_check_confirmed = False
            assignment.submitted_at = utc_now_iso()
            assignment.comment = comment
            assignment.segmentation_written = False
            self._save_assignments()
        self.audit.record(
            "submission",
            {
                "assignment_id": assignment.assignment_id,
                "user": assignment.user,
                "dataset_id": assignment.dataset_id,
                "subject_id": assignment.subject_id,
                "subject_key": assignment.subject_key,
                "quality_check_confirmed": False,
                "segmentation_written": False,
                "updated_labels": {},
                "comment": comment,
            },
            dataset_id=assignment.dataset_id,
            summary=(
                f"Subject {assignment.subject_key} submitted by '{assignment.user}' with "
                f"quality_check_confirmed=False; dataset left unchanged."
                + (f" Comment: {comment}" if comment else "")
            ),
        )
        return SubmissionOutcome(
            assignment=assignment,
            updated_labels={},
            removed_labels=[],
            backup_path=None,
            message="Rejected. Nothing in the dataset was changed.",
        )

    def _finish_confirmed(
        self,
        assignment: Assignment,
        segmentation_tmp_path: Path,
        present_labels: dict[str, int],
        labels_to_confirm: list[str],
        comment: str | None,
    ) -> "SubmissionOutcome":
        dataset_id, subject_id = assignment.dataset_id, assignment.subject_id
        target_path = self.segmentation_path(dataset_id, subject_id)
        confirmed_value = self.config.confirmed_label_value

        with self._lock:
            previous = self.subject_info(dataset_id, subject_id).segmentation or {}
            backup_path = self._backup_segmentation(target_path) if target_path.exists() else None

            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(segmentation_tmp_path), str(target_path))

            # A label the reviewer deleted from a confirmed segmentation is a confirmed
            # absence, which is what value 0 means.
            removed_labels = [
                label for label, value in previous.items() if label not in present_labels and value not in {0, -1}
            ]
            updated_labels = {label: confirmed_value for label in labels_to_confirm}

            def apply(subject: SubjectInfo) -> None:
                for label in labels_to_confirm:
                    subject.set_segmentation_value(label, confirmed_value)
                # Labels present but not vouched for keep the value they already had, and
                # are recorded as value 2 if the reviewer introduced them.
                for label in present_labels:
                    if label in labels_to_confirm:
                        continue
                    if not subject.segmentation or label not in subject.segmentation:
                        subject.set_segmentation_value(label, 2)
                if self.config.mark_removed_labels_absent:
                    for label in removed_labels:
                        subject.set_segmentation_value(label, 0)

            self._mutate_subject_info(dataset_id, subject_id, apply)

            assignment.state = "confirmed"
            assignment.quality_check_confirmed = True
            assignment.submitted_at = utc_now_iso()
            assignment.confirmed_labels = labels_to_confirm
            assignment.removed_labels = removed_labels if self.config.mark_removed_labels_absent else []
            assignment.comment = comment
            assignment.segmentation_written = True
            assignment.backup_path = str(backup_path) if backup_path else None
            self._save_assignments()

            # Keep the cached index honest until the next full rebuild.
            self._index = [r for r in self._index if r.subject_key != assignment.subject_key]
            self._index_by_key.pop(assignment.subject_key, None)

        self.audit.record(
            "submission",
            {
                "assignment_id": assignment.assignment_id,
                "user": assignment.user,
                "dataset_id": dataset_id,
                "subject_id": subject_id,
                "subject_key": assignment.subject_key,
                "quality_check_confirmed": True,
                "segmentation_written": True,
                "segmentation_path": str(target_path),
                "backup_path": assignment.backup_path,
                "labels_in_segmentation": sorted(present_labels),
                "updated_labels": updated_labels,
                "removed_labels": assignment.removed_labels,
                "comment": comment,
            },
            dataset_id=dataset_id,
            summary=(
                f"Subject {assignment.subject_key} confirmed by '{assignment.user}': "
                f"{len(labels_to_confirm)} label(s) set to {confirmed_value} "
                f"({', '.join(labels_to_confirm)}); segmentation written to '{target_path}'."
                + (f" Removed: {', '.join(assignment.removed_labels)}." if assignment.removed_labels else "")
                + (f" Comment: {comment}" if comment else "")
            ),
        )
        return SubmissionOutcome(
            assignment=assignment,
            updated_labels=updated_labels,
            removed_labels=assignment.removed_labels or [],
            backup_path=assignment.backup_path,
            message=f"Confirmed. {len(labels_to_confirm)} label(s) set to {confirmed_value} in Subject_info.",
        )

    def _backup_segmentation(self, target_path: Path) -> Path | None:
        if not self.config.keep_segmentation_backups:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dir = self.state_dir / BACKUP_DIR_NAME / target_path.parent.parent.name
        backup_dir.mkdir(parents=True, exist_ok=True)
        stem = target_path.name[: -len(".nii.gz")] if target_path.name.endswith(".nii.gz") else target_path.stem
        backup_path = backup_dir / f"{stem}_{stamp}.nii.gz"
        shutil.copy2(target_path, backup_path)
        return backup_path

    def new_upload_path(self) -> Path:
        return self.state_dir / TMP_DIR_NAME / f"upload_{uuid.uuid4().hex}.nii.gz"

    # ------------------------------------------------------------------ stats
    def stats(self) -> QueueStats:
        self._ensure_fresh_index()
        with self._lock:
            self._expire_stale_assignments()
            eligible = list(self._index)
            assigned_keys = {a.subject_key for a in self._assignments.values() if a.state == "assigned"}
            confirmed = sum(1 for a in self._assignments.values() if a.state == "confirmed")
            rejected = sum(1 for a in self._assignments.values() if a.state == "rejected")
            per_dataset: dict[int, int] = {}
            for ref in eligible:
                per_dataset[ref.dataset_id] = per_dataset.get(ref.dataset_id, 0) + 1
            available = sum(1 for ref in eligible if ref.subject_key not in assigned_keys)
            built_at = self._index_built_at
            total = self._total_subjects
        return QueueStats(
            total_subjects=total,
            eligible_subjects=len(eligible),
            available=available,
            assigned=len(assigned_keys),
            confirmed=confirmed,
            rejected=rejected,
            datasets=dict(sorted(per_dataset.items())),
            index_built_at=_to_iso(built_at) if built_at else None,
        )

    def all_assignments(self, limit: int = 200, states: Iterable[str] | None = None) -> list[dict]:
        with self._lock:
            self._expire_stale_assignments()
            assignments = list(self._assignments.values())
        if states:
            wanted = set(states)
            assignments = [a for a in assignments if a.state in wanted]
        assignments.sort(key=lambda a: a.assigned_at, reverse=True)
        return [a.model_dump() for a in assignments[:limit]]


@dataclass
class SubmissionOutcome:
    """What ``QCStore.submit`` did, on its way to the API response."""

    assignment: Assignment
    updated_labels: dict[str, int]
    removed_labels: list[str]
    backup_path: str | None
    message: str


# --------------------------------------------------------------------- helpers
def _atomic_write_json(path: Path, payload) -> None:
    """Write JSON through a temporary file so a crash cannot truncate the original."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4)
    os.replace(tmp_path, path)


def _to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment

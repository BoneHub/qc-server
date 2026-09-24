"""The quality-check store: subject index, users, assignments and dataset writes.

The server keeps its credentials apart from everything else. The credentials folder is
inside the container, never on the dataset share::

    /var/lib/bonehub-qc/      (BONEHUB_QC_CREDENTIALS_DIR, a Docker volume)
    |-- server_id             this server's name; a new credentials folder is a new server
    |-- server_private_key    generated on first start, unless BONEHUB_QC_PRIVATE_KEY is set
    |-- admin_key             generated on first start, unless BONEHUB_QC_ADMIN_KEY is set
    `-- users.json            users, their roles and their API key digests

Everything else is on the share, in a folder of this server's own, so that several
servers -- each with its own admin -- can work on one dataset without overwriting each
other's state::

    <dataset_root>/.bonehub_qc/<server_id>/
    |-- session.json          which server this is, when it was created and last started
    |-- config.json           policy, see config.QCServerConfig
    |-- assignments.json      which subject is with whom, and how it ended
    |-- submissions.jsonl     append-only audit trail
    |-- server.log
    |-- backups/              previous segmentations, kept before overwriting
    `-- tmp/                  uploads being validated

A subject that another server has leased is not handed out here, so two servers never
review the same subject at the same time.

Only datasets written under this server's ``bonehub_data_schema`` version are served: in
another version the label values, label statuses and segmentation format may differ.

A re-entrant lock serialises state and dataset writes. The work is human-paced, so the
simplicity is worth more than the concurrency; run the server with one worker. User
accounts have a lock of their own, taken inside the first when both are needed, so that
checking a key -- which every request does -- never waits for a write to the share.
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from bonehub_data_schema import (
    SEGMENTATION_SUFFIX,
    BoneLabelMap,
    DatasetInfo,
    SubjectInfo,
    __version__ as SCHEMA_VERSION,
    is_compatible_schema_version,
)
from bonehub_data_schema.bonehub_dataset_io import DATASET_ZFILL, SUBJECT_ZFILL

from . import __version__ as SERVER_VERSION
from . import auth
from .audit import AuditLog, utc_now_iso
from .config import (
    CONFIG_FILE_NAME,
    DEFAULT_STATE_DIR_NAME,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_REVIEWED,
    STATUS_REVIEWED,
    QCServerConfig,
)
from .models import (
    DATA_ACCESS_DESCRIPTIONS,
    DEFAULT_DATA_ACCESS,
    DEFAULT_ROLES,
    REVIEWER,
    ROLES,
    TERMINAL_STATES,
    Assignment,
    QueueStats,
    User,
)
from .segmentation import (
    SegmentationError,
    SegmentDescription,
    UploadedSegmentation,
    check_stored_geometry,
    read_segment_table,
    read_segmentation_upload,
    write_segmentation,
)

USERS_FILE_NAME = "users.json"
ASSIGNMENTS_FILE_NAME = "assignments.json"
SESSION_FILE_NAME = "session.json"
BACKUP_DIR_NAME = "backups"
TMP_DIR_NAME = "tmp"

#: Marker for an argument that was not supplied, where ``None`` is itself a valid value.
UNSET = object()

#: Every label a segment can be, which leaves out BACKGROUND.
LABEL_NAME_TO_VALUE: dict[str, int] = {label.name: label.value for label in BoneLabelMap if label.value != 0}


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

    def __init__(
        self,
        dataset_root: Path,
        credentials_dir: Path,
        config: QCServerConfig | None = None,
        state_root: Path | None = None,
    ):
        """``credentials_dir`` is inside the container; ``state_root`` is the folder on the
        share that holds one folder per server, ``<dataset_root>/.bonehub_qc`` by default."""
        if not dataset_root.is_dir():
            raise RuntimeError(f"Dataset root '{dataset_root}' does not exist or is not a directory.")

        self.dataset_root = dataset_root
        self.credentials_dir = Path(credentials_dir)
        self.state_root = Path(state_root) if state_root else dataset_root / DEFAULT_STATE_DIR_NAME
        if _is_within(self.credentials_dir, self.dataset_root):
            raise RuntimeError(
                f"The credentials folder '{self.credentials_dir}' is inside the dataset folder. Credentials must "
                "stay inside the container, off the shared dataset; set BONEHUB_QC_CREDENTIALS_DIR elsewhere."
            )

        auth.ensure_credentials_dir(self.credentials_dir)
        self.server_id, self.server_created = auth.load_or_create_server_id(self.credentials_dir)
        self.state_dir = self.state_root / self.server_id
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / BACKUP_DIR_NAME).mkdir(exist_ok=True)
        (self.state_dir / TMP_DIR_NAME).mkdir(exist_ok=True)

        self.audit = AuditLog(self.dataset_root, self.state_dir)

        self.config_path = self.state_dir / CONFIG_FILE_NAME
        self.config = config or QCServerConfig.load(self.config_path, notify=self.audit.event)
        self.config.save(self.config_path)

        self.private_key = auth.load_or_create_private_key(self.credentials_dir)
        self.admin_key, self.admin_key_generated = auth.load_or_create_admin_key(self.credentials_dir)

        self._lock = threading.RLock()
        self._users_lock = threading.RLock()

        self._users: dict[str, User] = self._load_users()
        self._users_stamp = self._users_file_stamp()
        self._assignments: dict[str, Assignment] = self._load_assignments()

        self._record_session()
        self.credentials_on_share = self._find_credentials_on_share()
        if self.credentials_on_share:
            self.audit.event(
                "Credentials of an older server are on the dataset share: "
                + ", ".join(str(path) for path in self.credentials_on_share)
                + ". This server does not use them; delete them from the share.",
                level=logging.WARNING,
            )

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
        key = subject_key_of(dataset_id, subject_id)
        return self.dataset_path(dataset_id) / "Segmentation" / f"{key}{SEGMENTATION_SUFFIX}"

    # --------------------------------------------------------------- sessions
    def _record_session(self, started: bool = False) -> None:
        """Write this server's ``session.json``, so the servers of a dataset can be told apart.

        ``started`` marks a start of the server itself; the CLI, which opens the same state
        next to it, leaves the start time alone.
        """
        path = self.state_dir / SESSION_FILE_NAME
        previous = _read_json(path)
        previous = previous if isinstance(previous, dict) else {}
        now = utc_now_iso()
        _atomic_write_json(
            path,
            {
                "server_id": self.server_id,
                "created_at": previous.get("created_at") or now,
                "last_started_at": now if started else previous.get("last_started_at"),
                "host": socket.gethostname() if started else previous.get("host", socket.gethostname()),
                "server_version": SERVER_VERSION,
                "schema_version": SCHEMA_VERSION,
            },
        )

    def mark_started(self) -> None:
        """Record that the server itself has started on this state."""
        self._record_session(started=True)

    def sessions(self) -> list[dict]:
        """Every server that has kept state in this dataset, this one included, oldest first."""
        found = []
        for path in sorted(self.state_root.glob(f"*/{SESSION_FILE_NAME}")):
            session = _read_json(path)
            if isinstance(session, dict):
                found.append({**session, "this_server": path.parent == self.state_dir})
        return sorted(found, key=lambda session: str(session.get("created_at", "")))

    def _find_credentials_on_share(self) -> list[Path]:
        """Credential files an older server left in the shared state folder."""
        return [self.state_root / name for name in auth.CREDENTIAL_FILE_NAMES if (self.state_root / name).exists()]

    def _leased_by_other_sessions(self) -> set[str]:
        """Subjects that another server on this dataset has out for review right now."""
        now = datetime.now(timezone.utc)
        leased: set[str] = set()
        for path in self.state_root.glob(f"*/{ASSIGNMENTS_FILE_NAME}"):
            if path.parent == self.state_dir:
                continue
            entries = _read_json(path)
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or entry.get("state") != "assigned":
                    continue
                try:
                    if _parse_iso(str(entry.get("expires_at"))) > now:
                        leased.add(str(entry.get("subject_key")))
                except ValueError:
                    continue
        return leased

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

            try:
                info = self._read_dataset_info(dataset_id)
            except Exception as exc:
                self.audit.event(f"Skipping '{dataset_dir.name}': its Dataset_info file could not be read: {exc}")
                continue
            if not is_compatible_schema_version(info.get("schema_version")):
                self.audit.event(
                    f"Skipping '{dataset_dir.name}': it was written with schema version "
                    f"{info.get('schema_version') or '(not recorded)'}, but this server reads {SCHEMA_VERSION}. "
                    "Regenerate the dataset with the current converters."
                )
                continue

            subjects = self._read_subject_info(dataset_id, missing_ok=True)
            if subjects is None:
                continue
            total += len(subjects)
            dataset_info[dataset_id] = info

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
        # Labels recorded with status 0 are not available, so they do not make a segmentation.
        if not subject.available_labels("segmentation"):
            return self.config.include_subjects_without_segmentation
        return any(status in self.config.eligible_label_values for status in subject.segmentation.values())

    # ------------------------------------------------------------------ users
    @property
    def users_path(self) -> Path:
        """User accounts hold key digests, so they stay with the credentials, off the share."""
        return self.credentials_dir / USERS_FILE_NAME

    def _load_users(self) -> dict[str, User]:
        if not self.users_path.exists():
            return {}
        with open(self.users_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {entry["name"]: User(**entry) for entry in raw}

    def _users_file_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.users_path.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _refresh_users(self) -> None:
        """Pick up user accounts another process changed. Caller holds the users lock.

        The CLI runs inside the container next to the live server (``docker compose exec``),
        so an account it adds must reach the server, and must not be lost on its next save.
        """
        stamp = self._users_file_stamp()
        if stamp != self._users_stamp:
            self._users = self._load_users()
            self._users_stamp = stamp

    def _save_users(self) -> None:
        _atomic_write_json(self.users_path, [user.model_dump() for user in self._users.values()])
        self._users_stamp = self._users_file_stamp()

    def list_users(self) -> list[dict]:
        with self._users_lock:
            self._refresh_users()
            users = [user.public_dict() for user in self._users.values()]
        counts = self._assignment_counts_per_user()
        for user in users:
            user.update(counts.get(user["name"], {"open": 0, "confirmed": 0, "rejected": 0}))
        return sorted(users, key=lambda u: u["name"].lower())

    def create_user(
        self,
        name: str,
        allowed_dataset_ids: list[int] | None = None,
        note: str = "",
        data_access: str = DEFAULT_DATA_ACCESS,
        roles: Iterable[str] = DEFAULT_ROLES,
    ) -> tuple[User, str]:
        """Create a user and return it together with its plaintext API key.

        ``roles`` is what the user may do: ``reviewer``, ``editor``, or both, the default.
        The plaintext key is returned exactly once; only its digest is stored.
        """
        name = name.strip()
        if not name:
            raise QCError("A user name is required.")
        _check_data_access(data_access)
        roles = _check_roles(roles)
        with self._users_lock:
            self._refresh_users()
            if name in self._users:
                raise QCError(f"User '{name}' already exists.", status_code=409)
            api_key = auth.generate_api_key()
            user = User(
                name=name,
                key_prefix=auth.key_prefix(api_key),
                key_hash=auth.hash_api_key(api_key, self.private_key),
                created_at=utc_now_iso(),
                active=True,
                roles=roles,
                allowed_dataset_ids=allowed_dataset_ids,
                data_access=data_access,
                note=note,
            )
            self._users[name] = user
            self._save_users()
        self.audit.record(
            "user_created",
            {"user": name, "key_prefix": user.key_prefix, "roles": user.roles, "data_access": user.data_access},
            summary=f"Created user '{name}' ({_account(user)}).",
        )
        return user, api_key

    def rotate_user_key(self, name: str) -> str:
        """Issue a new API key for a user and invalidate the old one."""
        with self._users_lock:
            user = self._require_user(name)
            api_key = auth.generate_api_key()
            user.key_prefix = auth.key_prefix(api_key)
            user.key_hash = auth.hash_api_key(api_key, self.private_key)
            self._save_users()
        self.audit.record("user_key_rotated", {"user": name}, summary=f"Rotated API key for user '{name}'.")
        return api_key

    def set_user_active(self, name: str, active: bool) -> User:
        with self._users_lock:
            user = self._require_user(name)
            user.active = active
            self._save_users()
        self.audit.record(
            "user_enabled" if active else "user_disabled",
            {"user": name},
            summary=f"{'Enabled' if active else 'Disabled'} user '{name}'.",
        )
        return user

    def update_user(
        self,
        name: str,
        allowed_dataset_ids=UNSET,
        note: str | None = None,
        data_access: str | None = None,
        roles: Iterable[str] | None = None,
    ) -> User:
        """Change a user. An argument left out is not touched.

        ``allowed_dataset_ids`` takes ``None`` to mean "every dataset the server serves",
        so it needs :data:`UNSET` to tell that apart from "leave the restriction alone" --
        otherwise editing only the note would quietly widen a user's access. A change of
        ``roles`` applies from the user's next request, in whichever client.
        """
        if data_access is not None:
            _check_data_access(data_access)
        if roles is not None:
            roles = _check_roles(roles)
        with self._users_lock:
            user = self._require_user(name)
            if allowed_dataset_ids is not UNSET:
                user.allowed_dataset_ids = allowed_dataset_ids
            if note is not None:
                user.note = note
            if data_access is not None:
                user.data_access = data_access
            if roles is not None:
                user.roles = roles
            self._save_users()
        self.audit.record(
            "user_updated",
            {"user": name, "roles": user.roles, "data_access": user.data_access},
            summary=f"Updated user '{name}' ({_account(user)}).",
        )
        return user

    def delete_user(self, name: str) -> None:
        """Remove a user and release whatever they were still holding."""
        with self._lock, self._users_lock:
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
        with self._users_lock:
            self._refresh_users()
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
        """Caller holds the users lock."""
        self._refresh_users()
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
                # and never goes back to the user who rejected it.
                if not self.config.requeue_rejected or assignment.user == user.name:
                    return False
        return True

    def open_assignments_of(self, user_name: str) -> list[Assignment]:
        with self._lock:
            self._expire_stale_assignments()
            return [a for a in self._assignments.values() if a.user == user_name and a.state == "assigned"]

    def next_subject(self, user: User, role: str | None = None) -> Assignment:
        """Pick a free subject for this user and lease it to them.

        ``role`` is the role of the client asking. A reviewer can only confirm or reject the
        segmentation a subject has, so a subject without one is left to the editors.
        """
        self._ensure_fresh_index()
        # Read from the share before the lock is taken: the other servers' leases are another
        # process's state, which no lock here holds still anyway.
        leased_elsewhere = self._leased_by_other_sessions()
        with self._lock:
            self._expire_stale_assignments()

            open_assignments = [a for a in self._assignments.values() if a.user == user.name and a.state == "assigned"]
            if len(open_assignments) >= self.config.max_concurrent_assignments_per_user:
                # Hand back what they already hold rather than refusing outright, so a client
                # that lost its local copy can pick the same work back up.
                return sorted(open_assignments, key=lambda a: a.assigned_at)[0]

            # Nobody is handed a subject they could do nothing with: a user sent the segmentation
            # only has nothing to look at in a subject without one, and a reviewer nothing to confirm.
            needs_segmentation = not user.receives_image or role == REVIEWER
            candidates = [
                ref
                for ref in self._index
                if self._user_may_access(user, ref.dataset_id) and (ref.has_segmentation or not needs_segmentation)
            ]
            if self.config.assignment_strategy == "random":
                random.shuffle(candidates)

            ref = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.subject_key not in leased_elsewhere
                    and self._subject_is_available(candidate.subject_key, user)
                ),
                None,
            )
            if ref is None:
                raise QCError("No subject is available for quality check right now.", status_code=404)
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

        as_role = f" as {role}" if role else ""
        self.audit.record(
            "assigned",
            {
                "assignment_id": assignment.assignment_id,
                "user": user.name,
                "role": role,
                "dataset_id": ref.dataset_id,
                "subject_id": ref.subject_id,
                "subject_key": ref.subject_key,
                "expires_at": assignment.expires_at,
            },
            dataset_id=ref.dataset_id,
            summary=(
                f"Assigned subject {ref.subject_key} to '{user.name}'{as_role} "
                f"(assignment {assignment.assignment_id})."
            ),
        )
        return assignment

    def get_assignment(self, assignment_id: str, user: User | None = None) -> Assignment:
        with self._lock:
            assignment = self._assignments.get(assignment_id)
        if assignment is None:
            raise QCError(f"Assignment '{assignment_id}' does not exist.", status_code=404)
        if user is not None and assignment.user != user.name:
            raise QCError("This assignment belongs to another user.", status_code=403)
        return assignment

    def release_assignment(self, assignment_id: str, user: User | None = None) -> Assignment:
        """Give a subject back without judging it, so somebody else can take it."""
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
        """Push the lease out by another TTL while a user is still working."""
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

    def _require_compatible_dataset(self, dataset_id: int) -> None:
        """Refuse to write into a dataset that was regenerated under another schema meanwhile."""
        version = self._read_dataset_info(dataset_id).get("schema_version")
        if not is_compatible_schema_version(version):
            raise QCError(
                f"Dataset {dataset_id} is now at schema version {version or '(not recorded)'}, but this server "
                f"writes {SCHEMA_VERSION}; the submission was not applied.",
                status_code=409,
            )

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

    def segment_table(self, dataset_id: int, subject_id: int) -> list[SegmentDescription]:
        """The segments of a subject's stored segmentation, from its header alone.

        Empty when there is no stored segmentation, and when its header cannot be read: a
        client that downloads the file still gets its handout, and the problem is logged.
        """
        path = self.segmentation_path(dataset_id, subject_id)
        if not path.exists():
            return []
        try:
            return read_segment_table(path)
        except SegmentationError as exc:
            self.audit.event(
                f"The segment table of '{path.name}' could not be read: {exc}", level=logging.WARNING
            )
            return []

    def stored_segmentation_issue(self, dataset_id: int, subject_id: int) -> str | None:
        """Why the stored segmentation cannot be confirmed as it is, or None when it can.

        Checked from the file headers, so a client can be told before anyone spends time on a
        subject whose confirmation the server would refuse.
        """
        path = self.segmentation_path(dataset_id, subject_id)
        image_file = self.image_path(dataset_id, subject_id)
        if not path.exists():
            return "There is no stored segmentation."
        if not self.config.require_geometry_match or not image_file.exists():
            return None
        try:
            check_stored_geometry(path, image_file)
        except SegmentationError as exc:
            return str(exc)
        return None

    # ------------------------------------------------------------- submission
    def inspect_segmentation(self, seg_path: Path, dataset_id: int, subject_id: int) -> UploadedSegmentation:
        """Validate an uploaded ``.seg.nrrd``; its ``labels`` are the labels it contains."""
        image_file = self.image_path(dataset_id, subject_id)
        check_against = image_file if self.config.require_geometry_match and image_file.exists() else None
        try:
            return read_segmentation_upload(seg_path, check_against)
        except SegmentationError as exc:
            raise QCError(str(exc)) from exc

    def submit(
        self,
        assignment_id: str,
        user: User,
        quality_check_confirmed: bool,
        segmentation_tmp_path: Path | None,
        confirmed_labels: list[str] | None = None,
        comment: str | None = None,
        use_stored_segmentation: bool = False,
    ) -> "SubmissionOutcome":
        """Apply a user's verdict.

        ``quality_check_confirmed=False`` changes nothing in the dataset: the segmentation
        file and Subject_info are left exactly as they are, and only the audit trail and
        the assignment state record that the subject was looked at and rejected.

        A confirmation either brings the corrected segmentation (``segmentation_tmp_path``),
        which takes an editor, or, with ``use_stored_segmentation``, vouches for the stored one
        as it is, which takes a reviewer and leaves the file untouched. Either way it takes a
        user who was sent the segmentation, when the subject has one: nobody confirms or
        replaces a segmentation they have not seen.
        """
        assignment = self.get_assignment(assignment_id, user)
        if assignment.state not in {"assigned", "expired"}:
            raise QCError(f"Assignment '{assignment_id}' was already submitted ({assignment.state}).", status_code=409)

        dataset_id, subject_id = assignment.dataset_id, assignment.subject_id

        if not quality_check_confirmed:
            return self._finish_rejected(assignment, comment, user)

        stored_path = self.segmentation_path(dataset_id, subject_id)
        if use_stored_segmentation and segmentation_tmp_path is not None:
            raise QCError("Send a segmentation file or set use_stored_segmentation, not both.")
        # The account's roles decide how it may confirm, whichever client the request comes from.
        if segmentation_tmp_path is not None and not user.is_editor:
            raise QCError(
                f"'{user.name}' is not an editor, so cannot upload a segmentation: segmentations are corrected "
                "in 3D Slicer, by an editor. Confirm the stored segmentation as it is, or reject the subject "
                "with a comment.",
                status_code=403,
            )
        if use_stored_segmentation and not user.is_reviewer:
            raise QCError(
                f"'{user.name}' is not a reviewer, so cannot confirm the stored segmentation as it is. Confirm "
                "the subject from 3D Slicer, which uploads its segmentation.",
                status_code=403,
            )
        if use_stored_segmentation:
            if not stored_path.exists():
                raise QCError(
                    f"Subject {assignment.subject_key} has no segmentation to confirm as it is.", status_code=409
                )
        elif segmentation_tmp_path is None:
            raise QCError(
                "A confirmed submission must include the reviewed segmentation file, or set "
                "use_stored_segmentation to confirm the stored one as it is."
            )
        if stored_path.exists() and not user.receives_segmentation:
            raise QCError(
                f"'{user.name}' is sent {DATA_ACCESS_DESCRIPTIONS[user.data_access]}, so cannot confirm or replace "
                f"the segmentation of {assignment.subject_key}, which they have not seen. Reject the subject with "
                "a comment instead.",
                status_code=403,
            )

        # Validation is the expensive part, so it happens before the lock is taken. The stored
        # file is held to the same rules as an upload: vouching for it certifies it just the same.
        if use_stored_segmentation:
            try:
                upload = self.inspect_segmentation(stored_path, dataset_id, subject_id)
            except QCError as exc:
                raise QCError(
                    f"The stored segmentation of {assignment.subject_key} cannot be confirmed as it is. "
                    f"{exc.message} An editor can correct it in 3D Slicer, which writes it back on the image's "
                    "voxel grid; otherwise reject the subject with a comment.",
                    status_code=409,
                ) from exc
        else:
            upload = self.inspect_segmentation(segmentation_tmp_path, dataset_id, subject_id)
        present_labels = upload.labels
        if not present_labels:
            raise QCError(
                f"The {'stored' if use_stored_segmentation else 'uploaded'} segmentation is empty; "
                "there is nothing to confirm."
            )

        if confirmed_labels is None:
            labels_to_confirm = sorted(present_labels)
        else:
            unknown = [label for label in confirmed_labels if label not in LABEL_NAME_TO_VALUE]
            if unknown:
                raise QCError(f"Unknown label names: {unknown}. See 'bonehub_data_schema/labelmap.py'.")
            missing = [label for label in confirmed_labels if label not in present_labels]
            if missing:
                raise QCError(
                    f"These confirmed labels are not present in the "
                    f"{'stored' if use_stored_segmentation else 'uploaded'} segmentation: {missing}."
                )
            labels_to_confirm = sorted(set(confirmed_labels))

        self._require_compatible_dataset(dataset_id)
        if use_stored_segmentation:
            return self._finish_confirmed(assignment, None, present_labels, labels_to_confirm, comment, user)

        # The dataset gets the canonical form of the upload. Writing it is slow too, so it is
        # staged here and only moved into place under the lock.
        staged_path = self.new_upload_path(prefix="staged")
        try:
            write_segmentation(upload, staged_path)
            return self._finish_confirmed(assignment, staged_path, present_labels, labels_to_confirm, comment, user)
        finally:
            staged_path.unlink(missing_ok=True)

    def _finish_rejected(self, assignment: Assignment, comment: str | None, user: User) -> "SubmissionOutcome":
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
                "data_access": user.data_access,
                "comment": comment,
            },
            dataset_id=assignment.dataset_id,
            summary=(
                f"Subject {assignment.subject_key} submitted by '{assignment.user}' with "
                f"quality_check_confirmed=False; dataset left unchanged."
                + _seen(user)
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
        staged_path: Path | None,
        present_labels: dict[str, int],
        labels_to_confirm: list[str],
        comment: str | None,
        user: User,
    ) -> "SubmissionOutcome":
        """Record a confirmation; ``staged_path`` None confirms the stored segmentation as it is."""
        dataset_id, subject_id = assignment.dataset_id, assignment.subject_id
        target_path = self.segmentation_path(dataset_id, subject_id)
        written = staged_path is not None

        with self._lock:
            previous = self.subject_info(dataset_id, subject_id).segmentation or {}
            backup_path = None
            if written:
                backup_path = self._backup_segmentation(target_path) if target_path.exists() else None
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged_path), str(target_path))

            # A label the editor deleted from a confirmed segmentation is no longer available.
            removed_labels = [
                label
                for label, status in previous.items()
                if label not in present_labels and status != STATUS_NOT_AVAILABLE
            ]
            updated_labels = {label: STATUS_REVIEWED for label in labels_to_confirm}

            def apply(subject: SubjectInfo) -> None:
                for label in labels_to_confirm:
                    subject.set_segmentation_value(label, STATUS_REVIEWED)
                # Labels present but not vouched for keep the status they already had; one the
                # dataset did not have yet is recorded as available but not reviewed.
                for label in present_labels:
                    if label in labels_to_confirm:
                        continue
                    if (subject.segmentation or {}).get(label, STATUS_NOT_AVAILABLE) == STATUS_NOT_AVAILABLE:
                        subject.set_segmentation_value(label, STATUS_NOT_REVIEWED)
                if self.config.mark_removed_labels_absent:
                    for label in removed_labels:
                        subject.set_segmentation_value(label, STATUS_NOT_AVAILABLE)

            self._mutate_subject_info(dataset_id, subject_id, apply)

            assignment.state = "confirmed"
            assignment.quality_check_confirmed = True
            assignment.submitted_at = utc_now_iso()
            assignment.confirmed_labels = labels_to_confirm
            assignment.removed_labels = removed_labels if self.config.mark_removed_labels_absent else []
            assignment.comment = comment
            assignment.segmentation_written = written
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
                "segmentation_written": written,
                "use_stored_segmentation": not written,
                "segmentation_path": str(target_path),
                "backup_path": assignment.backup_path,
                "labels_in_segmentation": sorted(present_labels),
                "updated_labels": updated_labels,
                "removed_labels": assignment.removed_labels,
                "data_access": user.data_access,
                "comment": comment,
            },
            dataset_id=dataset_id,
            summary=(
                f"Subject {assignment.subject_key} confirmed by '{assignment.user}': "
                f"{len(labels_to_confirm)} label(s) set to {STATUS_REVIEWED} (reviewed) "
                f"({', '.join(labels_to_confirm)}); "
                + (
                    f"segmentation written to '{target_path}'."
                    if written
                    else f"the stored segmentation '{target_path}' was confirmed as it is."
                )
                + (f" Removed: {', '.join(assignment.removed_labels)}." if assignment.removed_labels else "")
                + _seen(user)
                + (f" Comment: {comment}" if comment else "")
            ),
        )
        return SubmissionOutcome(
            assignment=assignment,
            updated_labels=updated_labels,
            removed_labels=assignment.removed_labels or [],
            backup_path=assignment.backup_path,
            message=f"Confirmed. {len(labels_to_confirm)} label(s) set to {STATUS_REVIEWED} (reviewed) in Subject_info.",
        )

    def _backup_segmentation(self, target_path: Path) -> Path | None:
        if not self.config.keep_segmentation_backups:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dir = self.state_dir / BACKUP_DIR_NAME / target_path.parent.parent.name
        backup_dir.mkdir(parents=True, exist_ok=True)
        name = target_path.name
        stem = name[: -len(SEGMENTATION_SUFFIX)] if name.endswith(SEGMENTATION_SUFFIX) else target_path.stem
        backup_path = backup_dir / f"{stem}_{stamp}{SEGMENTATION_SUFFIX}"
        shutil.copy2(target_path, backup_path)
        return backup_path

    def new_upload_path(self, prefix: str = "upload") -> Path:
        return self.state_dir / TMP_DIR_NAME / f"{prefix}_{uuid.uuid4().hex}{SEGMENTATION_SUFFIX}"

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
            eligible_keys = {ref.subject_key for ref in eligible}
            elsewhere = (self._leased_by_other_sessions() & eligible_keys) - assigned_keys
            available = len(eligible_keys - assigned_keys - elsewhere)
            built_at = self._index_built_at
            total = self._total_subjects
        return QueueStats(
            total_subjects=total,
            eligible_subjects=len(eligible),
            available=available,
            assigned=len(assigned_keys),
            assigned_by_other_servers=len(elsewhere),
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
def _check_data_access(data_access: str) -> None:
    if data_access not in DATA_ACCESS_DESCRIPTIONS:
        raise QCError(
            f"Unknown data access '{data_access}'. Choose one of: {', '.join(DATA_ACCESS_DESCRIPTIONS)}."
        )


def _check_roles(roles: Iterable[str]) -> list[str]:
    """The roles each once, in the order of ``ROLES``, or a QCError that says what is wrong."""
    roles = [str(role) for role in roles]
    unknown = [role for role in roles if role not in ROLES]
    if unknown:
        raise QCError(f"Unknown role(s): {', '.join(unknown)}. A user is a reviewer, an editor, or both.")
    if not roles:
        raise QCError("A user needs a role: reviewer (the review page), editor (3D Slicer), or both.")
    return [role for role in ROLES if role in roles]


def _account(user: User) -> str:
    """What an account is, for the audit trail: 'reviewer and editor; receives ...'."""
    return f"{' and '.join(user.roles)}; receives {DATA_ACCESS_DESCRIPTIONS[user.data_access]}"


def _seen(user: User) -> str:
    """Audit note on what a verdict was based on, when that was less than the whole subject."""
    if user.data_access == DEFAULT_DATA_ACCESS:
        return ""
    return f" The user is sent {DATA_ACCESS_DESCRIPTIONS[user.data_access]}."


def _atomic_write_json(path: Path, payload) -> None:
    """Write JSON through a temporary file so a crash cannot truncate the original."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4)
    _replace(tmp_path, path)


#: Pauses, in seconds, between attempts at a replace that is refused.
_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def _replace(source: Path, target: Path) -> None:
    """``os.replace``, tried again for a moment when it is refused.

    On Windows a file that another process has open at that instant cannot be replaced -- a
    virus scanner or indexer looking at a file just written, or another client on an SMB
    share -- and the refusal clears within milliseconds. Failing at once would lose a
    user's submission for nothing.
    """
    for delay in _REPLACE_RETRY_DELAYS:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(source, target)


def _read_json(path: Path):
    """A JSON file's content, or None when it is missing or unreadable -- another server may be
    halfway through replacing it."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _is_within(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment

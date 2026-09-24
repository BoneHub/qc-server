"""The quality-check store: subject index, users, assignments, cases and dataset writes.

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
    |-- cases.json            the subjects in progress, with every verdict so far (see workflow)
    |-- cases_done.jsonl      the subjects approved or closed, a line each
    |-- staged/               editors' corrected segmentations, waiting for approval
    |-- submissions.jsonl     append-only audit trail
    |-- server.log
    |-- backups/              the dataset's segmentations, kept before an approval overwrote them
    `-- tmp/                  uploads being validated

The verdicts of reviewers and editors go into this folder only. Nothing in the dataset
changes until the administrator approves a subject (:meth:`QCStore.approve`): only then are
its accepted labels set to reviewed in ``Subject_info``, and a corrected segmentation moved in.

A subject that another server has leased, or has in progress, is not handed out here, so two
servers never work on the same subject at the same time.

Only datasets written under this server's ``bonehub_data_schema`` version are served: in
another version the label values, label statuses and segmentation format may differ.

A re-entrant lock serialises the server's state; run the server with one worker. The writes
into the dataset have a lock of their own, taken before the first when both are needed, so
that copying a segmentation onto the share holds up nobody's lease. User accounts have a
lock of their own too, taken inside the first when both are needed, so that checking a key
-- which every request does -- never waits for a write to the share.
"""

from __future__ import annotations

import hashlib
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
from . import auth, workflow
from .audit import AuditLog, utc_now_iso
from .config import CONFIG_FILE_NAME, DEFAULT_STATE_DIR_NAME, QCServerConfig
from .models import (
    DATA_ACCESS_DESCRIPTIONS,
    DEFAULT_DATA_ACCESS,
    DEFAULT_ROLES,
    EDITOR,
    REJECT_REASONS,
    REVIEWER,
    ROLES,
    Assignment,
    Case,
    FileFingerprint,
    QueueStats,
    User,
)
from .segmentation import (
    SegmentationError,
    SegmentDescription,
    UploadedSegmentation,
    changed_labels,
    check_stored_geometry,
    read_segment_table,
    read_segmentation_upload,
    write_segmentation,
)

USERS_FILE_NAME = "users.json"
ASSIGNMENTS_FILE_NAME = "assignments.json"
CASES_FILE_NAME = "cases.json"
DONE_CASES_FILE_NAME = "cases_done.jsonl"
SESSION_FILE_NAME = "session.json"
BACKUP_DIR_NAME = "backups"
STAGED_DIR_NAME = "staged"
TMP_DIR_NAME = "tmp"

#: Marker for an argument that was not supplied, where ``None`` is itself a valid value.
UNSET = object()

#: Every label a segment can be, which leaves out BACKGROUND.
LABEL_NAME_TO_VALUE: dict[str, int] = {label.name: label.value for label in BoneLabelMap if label.value != 0}

#: How much of a file is read at a time to fingerprint it.
_HASH_CHUNK_BYTES = 2 * 1024 * 1024

#: What each stage waits for, in messages.
_STAGE_WAITS_FOR = {
    workflow.REVIEW: "a reviewer",
    workflow.EDIT: "an editor",
    workflow.APPROVAL: "the administrator's approval",
    workflow.ESCALATED: "the administrator",
    workflow.APPLIED: "nothing: it is approved and in the dataset",
    workflow.CLOSED: "nothing: the administrator closed it",
}


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
        for name in (BACKUP_DIR_NAME, STAGED_DIR_NAME, TMP_DIR_NAME):
            (self.state_dir / name).mkdir(exist_ok=True)

        self.audit = AuditLog(self.dataset_root, self.state_dir)

        self.config_path = self.state_dir / CONFIG_FILE_NAME
        self.config = config or QCServerConfig.load(self.config_path)
        self.config.save(self.config_path)

        self.private_key = auth.load_or_create_private_key(self.credentials_dir)
        self.admin_key, self.admin_key_generated = auth.load_or_create_admin_key(self.credentials_dir)

        self._lock = threading.RLock()
        self._users_lock = threading.RLock()
        #: Serialises the writes into the dataset, which only an approval makes.
        self._dataset_lock = threading.Lock()

        self._users: dict[str, User] = self._load_users()
        self._users_stamp = self._users_file_stamp()
        self._assignments: dict[str, Assignment] = self._load_assignments()
        self._cases: dict[str, Case] = self._load_cases()
        #: Subjects whose approval is being written into the dataset right now.
        self._applying: set[str] = set()

        self._record_session()

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
        """Where the segmentation belongs in the dataset, whether or not it exists yet."""
        key = subject_key_of(dataset_id, subject_id)
        return self.dataset_path(dataset_id) / "Segmentation" / f"{key}{SEGMENTATION_SUFFIX}"

    def staged_path(self, dataset_id: int, subject_id: int) -> Path:
        """Where an editor's corrected segmentation of the subject waits for approval."""
        key = subject_key_of(dataset_id, subject_id)
        folder = self.state_dir / STAGED_DIR_NAME / f"Dataset_{str(dataset_id).zfill(DATASET_ZFILL)}"
        return folder / f"{key}{SEGMENTATION_SUFFIX}"

    def current_segmentation_path(self, dataset_id: int, subject_id: int) -> Path | None:
        """The segmentation under quality check: an editor's correction waiting for approval,
        else the dataset's own. None when there is neither."""
        with self._lock:
            case = self._cases.get(subject_key_of(dataset_id, subject_id))
            staged = case is not None and case.staged
        path = self.staged_path(dataset_id, subject_id) if staged else self.segmentation_path(dataset_id, subject_id)
        return path if path.exists() else None

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

    def _claimed_by_other_sessions(self) -> set[str]:
        """Subjects another server on this dataset has out right now, or has in progress.

        A subject in progress there carries verdicts that wait for that server's
        administrator, and are not in the dataset yet, so it must not be reviewed here too.
        """
        now = datetime.now(timezone.utc)
        claimed: set[str] = set()
        for path in self.state_root.glob(f"*/{ASSIGNMENTS_FILE_NAME}"):
            if path.parent == self.state_dir:
                continue
            entries = _read_json(path)
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or entry.get("state") != "assigned":
                    continue
                try:
                    if _parse_iso(str(entry.get("expires_at"))) > now:
                        claimed.add(str(entry.get("subject_key")))
                except ValueError:
                    continue
        for path in self.state_root.glob(f"*/{CASES_FILE_NAME}"):
            if path.parent == self.state_dir:
                continue
            entries = _read_json(path)
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and entry.get("stage") in workflow.OPEN_STAGES:
                    claimed.add(str(entry.get("subject_key")))
        return claimed

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
            user.update(counts.get(user["name"], {"open": 0, "reviewed": 0, "edited": 0}))
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

    def _assignment_counts_per_user(self) -> dict[str, dict]:
        counts: dict[str, dict] = {}
        with self._lock:
            for assignment in self._assignments.values():
                bucket = counts.setdefault(assignment.user, {"open": 0, "reviewed": 0, "edited": 0})
                if assignment.state == "assigned":
                    bucket["open"] += 1
                elif assignment.state == "submitted":
                    bucket["edited" if assignment.role == EDITOR else "reviewed"] += 1
        return counts

    def _leased_subjects(self) -> set[str]:
        """Caller holds the lock and has expired stale leases. The subjects out right now."""
        return {assignment.subject_key for assignment in self._assignments.values() if assignment.state == "assigned"}

    def _held(self, user_name: str, role: str | None) -> list[Assignment]:
        """Caller holds the lock. What a user holds, in ``role`` or in any."""
        return [
            a
            for a in self._assignments.values()
            if a.user == user_name and a.state == "assigned" and (role is None or a.role == role)
        ]

    def open_assignments_of(self, user_name: str, role: str | None = None) -> list[Assignment]:
        """The subjects a user holds; with ``role``, those handed to them in that role, which
        is what each client lists."""
        with self._lock:
            self._expire_stale_assignments()
            return self._held(user_name, role)

    def next_subject(self, user: User, role: str) -> Assignment:
        """Pick a subject for this user, in the role of their client, and lease it to them.

        A reviewer is handed the subjects waiting for a review: first those already in
        progress, then those nobody has looked at yet. An editor is handed the subjects a
        reviewer sent back, and those without any segmentation. Nobody reviews their own
        correction, and nobody is handed a subject they could do nothing with. The limit on
        subjects held applies per role.
        """
        if role not in ROLES:
            raise QCError(f"'{role}' is not a role. Use 'reviewer' or 'editor'.")
        if role not in user.roles:
            raise QCError(f"'{user.name}' is not {_a(role)}.", status_code=403)
        self._ensure_fresh_index()
        # Read from the share before the lock is taken: the other servers' state is another
        # process's, which no lock here holds still anyway.
        claimed_elsewhere = self._claimed_by_other_sessions()
        with self._lock:
            self._expire_stale_assignments()

            held = self._held(user.name, role)
            if len(held) >= self.config.max_concurrent_assignments_per_user:
                # Hand back what they already hold rather than refusing outright, so a client
                # that lost its local copy can pick the same work back up.
                return sorted(held, key=lambda a: a.assigned_at)[0]

            leased = self._leased_subjects()
            chosen = next(
                (
                    (ref, case)
                    for ref, case in self._candidates(user, role)
                    if ref.subject_key not in leased and ref.subject_key not in claimed_elsewhere
                ),
                None,
            )
            if chosen is None:
                waiting = "a review" if role == REVIEWER else "an editor"
                raise QCError(f"No subject is waiting for {waiting} right now.", status_code=404)
            ref, case = chosen
            now = datetime.now(timezone.utc)
            assignment = Assignment(
                assignment_id=uuid.uuid4().hex,
                subject_key=ref.subject_key,
                dataset_id=ref.dataset_id,
                subject_id=ref.subject_id,
                user=user.name,
                role=role,
                case_revision=case.revision if case is not None else 0,
                state="assigned",
                assigned_at=_to_iso(now),
                expires_at=_to_iso(now + timedelta(seconds=self.config.lease_ttl_seconds)),
            )
            self._assignments[assignment.assignment_id] = assignment
            self._save_assignments()
            stage = case.stage if case is not None else workflow.STAGE_OF_ROLE[role]

        self.audit.record(
            "assigned",
            {
                "assignment_id": assignment.assignment_id,
                "user": user.name,
                "role": role,
                "stage": stage,
                "dataset_id": ref.dataset_id,
                "subject_id": ref.subject_id,
                "subject_key": ref.subject_key,
                "expires_at": assignment.expires_at,
            },
            summary=(
                f"Assigned subject {ref.subject_key} to '{user.name}' as {role} "
                f"(assignment {assignment.assignment_id})."
            ),
        )
        return assignment

    def _candidates(self, user: User, role: str) -> list[tuple[SubjectRef, Case | None]]:
        """Caller holds the lock. The subjects this user could be handed in this role, in order:
        those in progress at the role's stage, then those nobody has given a verdict on."""
        stage = workflow.STAGE_OF_ROLE[role]
        in_progress: list[tuple[SubjectRef, Case | None]] = []
        for case in self._cases.values():
            if case.stage != stage or not self._user_may_access(user, case.dataset_id):
                continue
            if role == REVIEWER and case.edited_by == user.name:
                continue  # nobody reviews their own correction
            has_segmentation = case.staged or case.base is not None
            if not _could_work_on(user, role, has_segmentation):
                continue
            ref = SubjectRef(case.dataset_id, case.subject_id, case.subject_key, True, has_segmentation)
            in_progress.append((ref, case))
        in_progress.sort(key=lambda item: item[0].sort_key)

        # A subject nobody has looked at goes to the reviewers, unless it has no segmentation to review.
        fresh: list[tuple[SubjectRef, Case | None]] = [
            (ref, None)
            for ref in self._index
            if ref.subject_key not in self._cases
            and self._user_may_access(user, ref.dataset_id)
            and ref.has_segmentation == (role == REVIEWER)
            and _could_work_on(user, role, ref.has_segmentation)
        ]
        if self.config.assignment_strategy == "random":
            random.shuffle(in_progress)
            random.shuffle(fresh)
        return in_progress + fresh

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
                "role": assignment.role,
                "subject_key": assignment.subject_key,
                "dataset_id": assignment.dataset_id,
                "subject_id": assignment.subject_id,
            },
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

    # ------------------------------------------------------------------ cases
    @property
    def cases_path(self) -> Path:
        return self.state_dir / CASES_FILE_NAME

    @property
    def done_cases_path(self) -> Path:
        return self.state_dir / DONE_CASES_FILE_NAME

    def _load_cases(self) -> dict[str, Case]:
        """Every case of this server. Where a subject is in both files -- finished, then reopened,
        or a crash between the two writes -- the case of the later revision is the one."""
        cases: dict[str, Case] = {}

        def keep(case: Case) -> None:
            known = cases.get(case.subject_key)
            if known is None or case.revision >= known.revision:
                cases[case.subject_key] = case

        if self.done_cases_path.exists():
            with open(self.done_cases_path, "r", encoding="utf-8") as f:
                for number, line in enumerate(f, start=1):
                    if not line.strip():
                        continue
                    try:
                        keep(Case(**json.loads(line)))
                    except ValueError as exc:  # a line cut short by a crash, or edited by hand
                        self.audit.event(
                            f"Line {number} of {DONE_CASES_FILE_NAME} could not be read and was skipped: {exc}",
                            level=logging.WARNING,
                        )
        if self.cases_path.exists():
            with open(self.cases_path, "r", encoding="utf-8") as f:
                for entry in json.load(f):
                    keep(Case(**entry))
        return cases

    def _save_cases(self) -> None:
        """Caller holds the lock. Only the cases in progress are rewritten; a finished case was
        appended to the done file as it finished, so this file stays as small as the work."""
        _atomic_write_json(
            self.cases_path,
            [case.model_dump() for case in self._cases.values() if case.stage not in workflow.FINISHED_STAGES],
        )

    def _archive(self, case: Case) -> None:
        """Caller holds the lock. Record a case that has just finished, then drop it from the
        cases in progress."""
        with open(self.done_cases_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(case.model_dump(), ensure_ascii=False) + "\n")
        self._save_cases()

    def case_of(self, subject_key: str) -> Case | None:
        """A copy of the subject's case, or None when nobody has given a verdict on it."""
        with self._lock:
            case = self._cases.get(subject_key)
            return case.model_copy(deep=True) if case is not None else None

    def handout_case(self, assignment: Assignment) -> Case:
        """The subject's case as it stands, or as it would begin, for a handout."""
        return self.case_of(assignment.subject_key) or self._new_case(
            assignment.dataset_id, assignment.subject_id, fingerprint=False
        )

    def _new_case(self, dataset_id: int, subject_id: int, fingerprint: bool = True) -> Case:
        """The case a subject nobody has given a verdict on begins with, read from the dataset.

        ``fingerprint`` records the dataset's segmentation as it is now, so that approving the
        case can tell whether anything else wrote it meanwhile. That reads the whole file.
        """
        subject = self.subject_info(dataset_id, subject_id)
        path = self.segmentation_path(dataset_id, subject_id)
        painted = [segment.label for segment in self._segment_table_of(path)]
        return workflow.start_case(
            dataset_id,
            subject_id,
            subject_key_of(dataset_id, subject_id),
            dict(subject.segmentation or {}),
            painted,
            self.config.eligible_label_values,
            file_fingerprint(path) if fingerprint else None,
            utc_now_iso(),
        )

    def cases(self, stages: Iterable[str] | None = None, limit: int = 500) -> list[dict]:
        """The cases of this server, most recently changed first, each with ``leased_to``: who
        holds the subject right now, if anybody."""
        wanted = set(stages) if stages else None
        with self._lock:
            self._expire_stale_assignments()
            holders = {a.subject_key: a.user for a in self._assignments.values() if a.state == "assigned"}
            found = [case for case in self._cases.values() if wanted is None or case.stage in wanted]
            found.sort(key=lambda case: case.updated_at, reverse=True)
            return [{**case.model_dump(), "leased_to": holders.get(case.subject_key)} for case in found[:limit]]

    def case_segmentation_path(self, subject_key: str) -> Path:
        """The segmentation a case is about: its editor's correction, or the dataset's own."""
        case = self._require_case(subject_key)
        path = self.current_segmentation_path(case.dataset_id, case.subject_id)
        if path is None:
            raise QCError(f"Subject {subject_key} has no segmentation.", status_code=404)
        return path

    def _require_case(self, subject_key: str) -> Case:
        with self._lock:
            case = self._cases.get(subject_key)
        if case is None:
            raise QCError(f"Nobody has given a verdict on subject {subject_key} on this server.", status_code=404)
        return case

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
        """Refuse a dataset that was regenerated under another schema meanwhile."""
        version = self._read_dataset_info(dataset_id).get("schema_version")
        if not is_compatible_schema_version(version):
            raise QCError(
                f"Dataset {dataset_id} is now at schema version {version or '(not recorded)'}, but this server "
                f"writes {SCHEMA_VERSION}; nothing was recorded.",
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
        """The segments of the subject's segmentation under quality check, from its header alone.

        Empty when there is no segmentation, and when its header cannot be read: a client
        that downloads the file still gets its handout, and the problem is logged.
        """
        path = self.current_segmentation_path(dataset_id, subject_id)
        return self._segment_table_of(path) if path is not None else []

    def _segment_table_of(self, path: Path) -> list[SegmentDescription]:
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
        """Why the segmentation under quality check cannot be accepted as it is, or None.

        Checked from the file headers, so a client can be told before anyone spends time on a
        subject whose acceptance the server would refuse.
        """
        path = self.current_segmentation_path(dataset_id, subject_id)
        image_file = self.image_path(dataset_id, subject_id)
        if path is None:
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
        """Validate a ``.seg.nrrd``; its ``labels`` are the labels it contains."""
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
        rejected_labels: dict[str, str] | None = None,
        missing_labels: list[str] | None = None,
        role: str | None = None,
    ) -> "SubmissionOutcome":
        """Record a user's verdict on the subject they hold, in this server's state folder.

        Nothing in the dataset changes: the verdict moves the subject along its quality check
        (see :mod:`workflow`), and only the administrator's approval (:meth:`approve`) writes
        it into the dataset. A verdict is given in the role the subject was handed out in,
        which ``role``, the role of the client, must match:

        * a reviewer judges the segmentation as it is (``use_stored_segmentation``), label by
          label: ``confirmed_labels`` accepted, ``rejected_labels`` rejected with a reason,
          ``missing_labels`` reported missing. ``quality_check_confirmed=False`` rejects every
          label under review.
        * an editor uploads the corrected segmentation (``segmentation_tmp_path``), vouching for
          ``confirmed_labels``. ``quality_check_confirmed=False`` sends the subject to the
          administrator.

        Nobody accepts or replaces a segmentation they have not been sent.
        """
        assignment = self.get_assignment(assignment_id, user)
        if assignment.state not in {"assigned", "expired"}:
            raise QCError(f"Assignment '{assignment_id}' was already submitted ({assignment.state}).", status_code=409)
        if quality_check_confirmed and use_stored_segmentation and segmentation_tmp_path is not None:
            raise QCError("Send a segmentation file or set use_stored_segmentation, not both.")
        comment = (comment or "").strip() or None
        rejected_labels = dict(rejected_labels or {})
        missing_labels = list(missing_labels or [])
        as_role = self._verdict_role(
            assignment,
            user,
            role,
            quality_check_confirmed,
            segmentation_tmp_path,
            use_stored_segmentation,
            bool(rejected_labels or missing_labels),
        )
        if as_role == REVIEWER:
            return self._submit_review(
                assignment,
                user,
                quality_check_confirmed,
                segmentation_tmp_path,
                use_stored_segmentation,
                confirmed_labels,
                rejected_labels,
                missing_labels,
                comment,
            )
        return self._submit_edit(
            assignment,
            user,
            quality_check_confirmed,
            segmentation_tmp_path,
            use_stored_segmentation,
            confirmed_labels,
            bool(rejected_labels or missing_labels),
            comment,
        )

    def _verdict_role(
        self,
        assignment: Assignment,
        user: User,
        role: str | None,
        confirmed: bool,
        upload: Path | None,
        use_stored: bool,
        judges_labels: bool,
    ) -> str:
        """The role a verdict is given in: the one the subject was handed out in.

        What an account may do is checked against its roles first, whichever client the
        request says it comes from: only an editor uploads a segmentation, and only a reviewer
        judges the segmentation as it is.
        """
        if confirmed and upload is not None and not user.is_editor:
            raise QCError(
                f"'{user.name}' is not an editor, so cannot upload a segmentation: segmentations are corrected "
                "in 3D Slicer, by an editor. Accept or reject each label of the segmentation as it is instead, "
                "or reject the subject with a comment.",
                status_code=403,
            )
        if (confirmed and use_stored or judges_labels) and not user.is_reviewer:
            raise QCError(
                f"'{user.name}' is not a reviewer, so cannot judge the stored segmentation as it is. Upload the "
                "corrected segmentation from 3D Slicer.",
                status_code=403,
            )
        handed_as = assignment.role
        if role is not None and role != handed_as:
            raise QCError(
                f"{assignment.subject_key} was handed to '{user.name}' as {_a(handed_as)}; submit it "
                f"{'on the review page' if handed_as == REVIEWER else 'from 3D Slicer'}.",
                status_code=409,
            )
        if handed_as not in user.roles:
            raise QCError(
                f"'{user.name}' is no longer {_a(handed_as)}, the role {assignment.subject_key} was handed to "
                "them in. Release it.",
                status_code=403,
            )
        return handed_as

    def _submit_review(
        self,
        assignment: Assignment,
        user: User,
        confirmed: bool,
        upload: Path | None,
        use_stored: bool,
        confirmed_labels: list[str] | None,
        rejected: dict[str, str],
        missing: list[str],
        comment: str | None,
    ) -> "SubmissionOutcome":
        key, dataset_id, subject_id = assignment.subject_key, assignment.dataset_id, assignment.subject_id
        if confirmed and upload is not None:
            raise QCError(
                f"{key} was handed to '{user.name}' for review, which judges the segmentation as it is: send the "
                "verdict with use_stored_segmentation, and no file. Corrections are made in 3D Slicer, once a "
                "reviewer has rejected a label.",
                status_code=409,
            )
        case, revision = self._case_for_verdict(assignment, workflow.REVIEW)
        painted = set(workflow.painted_labels(case))
        request_edit = not confirmed
        if not confirmed:
            # The subject as a whole: whatever is under review goes to the editors.
            accepted = []
            rejected = {
                name: workflow.QUALITY if name in painted else workflow.MISSING
                for name in workflow.labels_in(case, workflow.PENDING)
            }
        else:
            if not use_stored:
                raise QCError(
                    "A reviewer's verdict is about the segmentation as it is: set use_stored_segmentation. A "
                    "confirmed submission with a corrected segmentation file takes an editor, in 3D Slicer."
                )
            _check_label_names([*(confirmed_labels or []), *rejected, *missing])
            wrong = sorted({reason for reason in rejected.values() if reason not in REJECT_REASONS})
            if wrong:
                raise QCError(f"Unknown reasons to reject a label: {wrong}. Give one of {sorted(REJECT_REASONS)}.")
            already = sorted(name for name in missing if name in painted)
            already += sorted(name for name, reason in rejected.items() if reason == workflow.MISSING and name in painted)
            if already:
                raise QCError(
                    f"These labels are in the segmentation of {key} already: {already}. Reject them if they need "
                    "correcting; 'missing' is for bones the segmentation lacks."
                )
            not_there = sorted(name for name, reason in rejected.items() if reason != workflow.MISSING and name not in painted)
            if not_there:
                raise QCError(
                    f"These rejected labels are not in the segmentation of {key}: {not_there}. Report a bone the "
                    "segmentation lacks as missing."
                )
            rejected = {**rejected, **{name: workflow.MISSING for name in missing}}
            if confirmed_labels is None:
                # Accepting "the subject" accepts what is under review, not what the dataset has
                # as reviewed already.
                accepted = sorted(set(workflow.labels_in(case, workflow.PENDING)) - set(rejected))
            else:
                accepted = sorted(set(confirmed_labels))
            unknown = [name for name in accepted if name not in case.labels]
            if unknown:
                raise QCError(f"These confirmed labels are not present in the stored segmentation: {unknown}.")
            both = sorted(set(accepted) & set(rejected))
            if both:
                raise QCError(f"These labels are both accepted and rejected: {both}.")
            if not (accepted or rejected) and workflow.labels_in(case, workflow.PENDING):
                raise QCError(
                    "The verdict judges no label: accept or reject the labels under review, or report a missing one."
                )

        # Accepting a painted label vouches for the segmentation as it is, so it is held to the
        # same rules as an upload. That reads the whole file, before the lock is taken.
        path = self.current_segmentation_path(dataset_id, subject_id)
        if any(name in painted for name in accepted):
            if path is not None and not user.receives_segmentation:
                raise QCError(
                    f"'{user.name}' is sent {DATA_ACCESS_DESCRIPTIONS[user.data_access]}, so cannot accept labels of "
                    f"the segmentation of {key}, which they have not seen. Reject the subject with a comment instead.",
                    status_code=403,
                )
            if path is None:
                raise QCError(f"Subject {key} has no segmentation to accept.", status_code=409)
            try:
                stored = self.inspect_segmentation(path, dataset_id, subject_id)
            except QCError as exc:
                raise QCError(
                    f"The stored segmentation of {key} cannot be accepted as it is. {exc.message} An editor can "
                    "correct it in 3D Slicer, which writes it back on the image's voxel grid: reject its labels "
                    "with a comment.",
                    status_code=409,
                ) from exc
            if not stored.labels:
                raise QCError("The stored segmentation is empty; there is nothing to accept.")
            not_painted = [name for name in accepted if name in painted and name not in stored.labels]
            if not_painted:
                raise QCError(f"These confirmed labels are not present in the stored segmentation: {not_painted}.")
        self._require_compatible_dataset(dataset_id)

        now = utc_now_iso()
        with self._lock:
            live = self._check_turn(assignment, revision)
            case = live if live is not None else case
            details = workflow.apply_review(
                case, user.name, accepted, rejected, comment, assignment.assignment_id, now, request_edit
            )
            self._cases[key] = case
            self._save_cases()
            self._finish_assignment(
                assignment,
                confirmed,
                comment,
                now,
                case.stage,
                accepted_labels=details["accepted"],
                rejected_labels=details["rejected"],
                missing_labels=details["missing"],
            )
            snapshot = case.model_copy(deep=True)

        outcome = SubmissionOutcome(
            assignment=assignment,
            case=snapshot,
            accepted_labels=details["accepted"],
            rejected_labels=details["rejected"],
            missing_labels=details["missing"],
            pending_labels=workflow.labels_in(snapshot, workflow.PENDING),
            message=_review_message(snapshot, details),
        )
        verdict = []
        if details["accepted"]:
            verdict.append(f"accepted {', '.join(details['accepted'])}")
        rejected_painted = {name: why for name, why in details["rejected"].items() if why != workflow.MISSING}
        if rejected_painted:
            verdict.append(
                "rejected " + ", ".join(f"{name} ({REJECT_REASONS[why]})" for name, why in rejected_painted.items())
            )
        if details["missing"]:
            verdict.append(f"reported missing {', '.join(details['missing'])}")
        self._record_submission(
            outcome,
            user,
            f"Subject {key} reviewed by '{user.name}'"
            + (" (the subject as a whole rejected)" if not confirmed else "")
            + f": {'; '.join(verdict) or 'no label judged'}. It now waits for {_STAGE_WAITS_FOR[snapshot.stage]}.",
            comment,
        )
        return outcome

    def _submit_edit(
        self,
        assignment: Assignment,
        user: User,
        confirmed: bool,
        upload: Path | None,
        use_stored: bool,
        confirmed_labels: list[str] | None,
        judges_labels: bool,
        comment: str | None,
    ) -> "SubmissionOutcome":
        key, dataset_id, subject_id = assignment.subject_key, assignment.dataset_id, assignment.subject_id
        if judges_labels:
            raise QCError(
                f"{key} was handed to '{user.name}' for editing: an editor corrects the segmentation, and reviewers "
                "reject labels or report them missing.",
                status_code=409,
            )
        case, revision = self._case_for_verdict(assignment, workflow.EDIT)

        if not confirmed:
            # The editor could not, or would not, correct it: the administrator decides.
            now = utc_now_iso()
            with self._lock:
                live = self._check_turn(assignment, revision)
                case = live if live is not None else case
                workflow.escalate(case, user.name, comment, assignment.assignment_id, now)
                self._cases[key] = case
                self._save_cases()
                self._finish_assignment(assignment, False, comment, now, case.stage)
                snapshot = case.model_copy(deep=True)
            outcome = SubmissionOutcome(
                assignment=assignment,
                case=snapshot,
                message="Sent to the administrator with your comment. Nothing in the dataset changed.",
            )
            self._record_submission(
                outcome, user, f"Subject {key} sent to the administrator by '{user.name}', who could not correct it.", comment
            )
            return outcome

        if use_stored:
            raise QCError(
                f"{key} was handed to '{user.name}' for editing: upload the corrected segmentation from 3D Slicer -- "
                "unchanged, if it needs no correction.",
                status_code=409,
            )
        if upload is None:
            raise QCError("A confirmed submission must include the corrected segmentation file.")
        current = self.current_segmentation_path(dataset_id, subject_id)
        if current is not None and not user.receives_segmentation:
            raise QCError(
                f"'{user.name}' is sent {DATA_ACCESS_DESCRIPTIONS[user.data_access]}, so cannot confirm or replace "
                f"the segmentation of {key}, which they have not seen. Reject the subject with a comment instead.",
                status_code=403,
            )

        # Checking the upload, comparing it with the segmentation it replaces and writing its
        # canonical form are the slow part, so they happen before the lock is taken.
        uploaded = self.inspect_segmentation(upload, dataset_id, subject_id)
        present = uploaded.labels
        if not present:
            raise QCError("The uploaded segmentation is empty; there is nothing to confirm.")
        if confirmed_labels is None:
            vouched = set(present)
        else:
            _check_label_names(confirmed_labels)
            not_painted = [name for name in confirmed_labels if name not in present]
            if not_painted:
                raise QCError(f"These confirmed labels are not present in the uploaded segmentation: {not_painted}.")
            vouched = set(confirmed_labels)
        changed = changed_labels(current, uploaded) if current is not None else None
        self._require_compatible_dataset(dataset_id)

        staged_tmp = self.new_upload_path(prefix="staged")
        try:
            write_segmentation(uploaded, staged_tmp)
            now = utc_now_iso()
            with self._lock:
                live = self._check_turn(assignment, revision)
                case = live if live is not None else case
                target = self.staged_path(dataset_id, subject_id)
                target.parent.mkdir(parents=True, exist_ok=True)
                _replace(staged_tmp, target)
                details = workflow.apply_edit(
                    case,
                    user.name,
                    present,
                    changed,
                    vouched,
                    self.config.edits_need_review,
                    comment,
                    assignment.assignment_id,
                    now,
                )
                self._cases[key] = case
                self._save_cases()
                self._finish_assignment(
                    assignment,
                    True,
                    comment,
                    now,
                    case.stage,
                    accepted_labels=details["accepted"],
                    edited_labels=details["edited"],
                    removed_labels=details["removed"],
                    segmentation_staged=True,
                )
                snapshot = case.model_copy(deep=True)
        finally:
            staged_tmp.unlink(missing_ok=True)

        outcome = SubmissionOutcome(
            assignment=assignment,
            case=snapshot,
            accepted_labels=details["accepted"],
            edited_labels=details["edited"],
            removed_labels=details["removed"],
            pending_labels=details["pending"],
            segmentation_staged=True,
            message=_edit_message(snapshot, details),
        )
        self._record_submission(
            outcome,
            user,
            f"Subject {key} corrected by '{user.name}': "
            + (f"changed or added {', '.join(details['edited'])}" if details["edited"] else "no label changed")
            + (f"; removed {', '.join(details['removed'])}" if details["removed"] else "")
            + f". The correction waits in '{self.staged_path(dataset_id, subject_id)}', and the subject for "
            + f"{_STAGE_WAITS_FOR[snapshot.stage]}.",
            comment,
        )
        return outcome

    def _case_for_verdict(self, assignment: Assignment, stage: str) -> tuple[Case, int]:
        """A working copy of the subject's case, and the revision it was copied at: 0 for a
        subject nobody has given a verdict on, whose case begins with this one.

        Refuses a subject whose case has moved on from the stage the verdict belongs to.
        """
        with self._lock:
            live = self._cases.get(assignment.subject_key)
            case = live.model_copy(deep=True) if live is not None else None
        if case is None:
            return self._new_case(assignment.dataset_id, assignment.subject_id), 0
        if case.stage != stage:
            raise QCError(
                f"{assignment.subject_key} no longer waits for {_STAGE_WAITS_FOR[stage]}: it waits for "
                f"{_STAGE_WAITS_FOR[case.stage]}. The verdict was not recorded.",
                status_code=409,
            )
        return case, case.revision

    def _check_turn(self, assignment: Assignment, revision: int) -> Case | None:
        """Caller holds the lock. The subject's live case, once it is sure the verdict counts.

        A user whose lease ran out may still submit, so their work is not lost -- unless the
        subject has been handed to someone else since, or its case changed after the user was
        handed it, or after the verdict was checked against it.
        """
        key = assignment.subject_key
        if assignment.state == "expired":
            later = False
            for other in reversed(self._assignments.values()):
                if other is assignment:
                    break
                later = later or other.subject_key == key
            if later:
                raise QCError(
                    f"The lease of '{assignment.user}' on {key} ran out, and the subject has been handed out again "
                    "since; the verdict was not recorded.",
                    status_code=409,
                )
        live = self._cases.get(key)
        current = live.revision if live is not None else 0
        if current != revision or current != assignment.case_revision:
            raise QCError(
                f"{key} changed after it was handed out -- someone else gave a verdict on it, or the administrator "
                "acted on it -- so the verdict was not recorded. Ask for the next subject.",
                status_code=409,
            )
        return live

    def _finish_assignment(
        self, assignment: Assignment, confirmed: bool, comment: str | None, now: str, stage: str, **details
    ) -> None:
        """Caller holds the lock. Close the lease with what the verdict did."""
        assignment.state = "submitted"
        assignment.quality_check_confirmed = confirmed
        assignment.submitted_at = now
        assignment.comment = comment
        assignment.stage_after = stage
        for name, value in details.items():
            setattr(assignment, name, value)
        self._save_assignments()

    def _record_submission(self, outcome: "SubmissionOutcome", user: User, summary: str, comment: str | None) -> None:
        assignment = outcome.assignment
        self.audit.record(
            "submission",
            {
                "assignment_id": assignment.assignment_id,
                "user": assignment.user,
                "role": assignment.role,
                "dataset_id": assignment.dataset_id,
                "subject_id": assignment.subject_id,
                "subject_key": assignment.subject_key,
                "quality_check_confirmed": bool(assignment.quality_check_confirmed),
                "stage": outcome.case.stage,
                "accepted_labels": outcome.accepted_labels,
                "rejected_labels": outcome.rejected_labels,
                "missing_labels": outcome.missing_labels,
                "edited_labels": outcome.edited_labels,
                "removed_labels": outcome.removed_labels,
                "pending_labels": outcome.pending_labels,
                "segmentation_staged": outcome.segmentation_staged,
                "data_access": user.data_access,
                "comment": comment,
            },
            summary=summary + _seen(user) + (f" Comment: {comment}" if comment else ""),
        )

    def new_upload_path(self, prefix: str = "upload") -> Path:
        return self.state_dir / TMP_DIR_NAME / f"{prefix}_{uuid.uuid4().hex}{SEGMENTATION_SUFFIX}"

    # --------------------------------------------------------------- approval
    def approve(self, subject_key: str) -> "ApprovalOutcome":
        """Write an approved subject into the dataset.

        The accepted labels become reviewed (2) in ``Subject_info``, labels no longer in the
        segmentation not available (0) under ``mark_removed_labels_absent``, and an editor's
        correction replaces the dataset's segmentation, which is backed up first. Refused when
        the subject is not waiting for approval, and when the dataset's segmentation changed
        after its quality check began: another server or tool wrote it, and approving would
        overwrite what they wrote.
        """
        with self._lock:
            case = self._cases.get(subject_key)
            if case is None:
                raise QCError(f"Nobody has given a verdict on subject {subject_key} on this server.", status_code=404)
            if case.stage != workflow.APPROVAL:
                raise QCError(
                    f"Subject {subject_key} is not waiting for approval: it waits for {_STAGE_WAITS_FOR[case.stage]}.",
                    status_code=409,
                )
            if subject_key in self._applying:
                raise QCError(f"Subject {subject_key} is being approved already.", status_code=409)
            self._applying.add(subject_key)
            snapshot = case.model_copy(deep=True)
        try:
            with self._dataset_lock:
                updated, backup_path = self._write_approved(snapshot)
                now = utc_now_iso()
                with self._lock:
                    case = self._cases[subject_key]
                    written = case.staged
                    workflow.mark_applied(case, updated, str(backup_path) if backup_path else None, now)
                    self._archive(case)
                    # Keep the cached index honest until the next full rebuild.
                    self._index = [ref for ref in self._index if ref.subject_key != subject_key]
                    self._index_by_key.pop(subject_key, None)
                    applied = case.model_copy(deep=True)
        finally:
            with self._lock:
                self._applying.discard(subject_key)

        target = self.segmentation_path(applied.dataset_id, applied.subject_id)
        reviewed = sorted(name for name, status in updated.items() if status == 2)
        removed = sorted(name for name, status in updated.items() if status == 0)
        self.audit.record(
            "approved",
            {
                "subject_key": subject_key,
                "dataset_id": applied.dataset_id,
                "subject_id": applied.subject_id,
                "updated_labels": updated,
                "segmentation_written": written,
                "segmentation_path": str(target),
                "backup_path": applied.backup_path,
                "verdicts": _verdicts_of(applied),
            },
            dataset_id=applied.dataset_id,
            summary=(
                f"Subject {subject_key} approved: {len(reviewed)} label(s) set to 2 (reviewed)"
                + (f" ({', '.join(reviewed)})" if reviewed else "")
                + (f"; {', '.join(removed)} set to 0 (not available)" if removed else "")
                + (
                    f"; the corrected segmentation by '{snapshot.edited_by}' written to '{target}'."
                    if written
                    else "; the segmentation is the dataset's own, unchanged."
                )
                + " "
                + _verdicts_text(applied)
            ),
        )
        return ApprovalOutcome(
            case=applied,
            updated_labels=updated,
            segmentation_written=written,
            backup_path=applied.backup_path,
            message=(
                f"Approved: {len(reviewed)} label(s) set to reviewed"
                + (" and the corrected segmentation written into the dataset." if written else ".")
            ),
        )

    def _write_approved(self, case: Case) -> tuple[dict[str, int], Path | None]:
        """Caller holds the dataset lock. Write a case into the dataset; returns the statuses set
        and where the dataset's previous segmentation was backed up.

        If ``Subject_info`` cannot be written after the segmentation was, the segmentation is
        put back, so that the dataset is not left half approved.
        """
        dataset_id, subject_id, key = case.dataset_id, case.subject_id, case.subject_key
        self._require_compatible_dataset(dataset_id)
        target = self.segmentation_path(dataset_id, subject_id)
        if not same_file(case.base, target):
            raise QCError(
                f"The dataset's segmentation of {key} changed after its quality check began -- another server or "
                "tool wrote it -- so approving would overwrite that. Send the subject back to review.",
                status_code=409,
            )
        staged = self.staged_path(dataset_id, subject_id) if case.staged else None
        if staged is not None:
            if not staged.exists():
                raise QCError(
                    f"The corrected segmentation of {key} is missing from '{staged.parent}'. Send the subject back "
                    "to the editors.",
                    status_code=409,
                )
            image_file = self.image_path(dataset_id, subject_id)
            if self.config.require_geometry_match and image_file.exists():
                try:
                    check_stored_geometry(staged, image_file)
                except SegmentationError as exc:
                    raise QCError(
                        f"The corrected segmentation of {key} no longer fits its image: {exc} Send the subject back "
                        "to the editors.",
                        status_code=409,
                    ) from exc

        statuses = dict(self.subject_info(dataset_id, subject_id).segmentation or {})
        updated = workflow.approval_statuses(case, statuses, self.config.mark_removed_labels_absent)

        previous: Path | None = None
        if staged is not None:
            if target.exists():
                previous = self.new_upload_path(prefix="previous")
                shutil.copy2(target, previous)
            target.parent.mkdir(parents=True, exist_ok=True)
            _replace(staged, target)
        try:

            def apply(subject: SubjectInfo) -> None:
                for label, status in updated.items():
                    subject.set_segmentation_value(label, status)

            self._mutate_subject_info(dataset_id, subject_id, apply)
        except Exception:
            if staged is not None:
                _replace(target, staged)
                if previous is not None:
                    _replace(previous, target)
            raise

        backup_path = None
        if previous is not None:
            if self.config.keep_segmentation_backups:
                backup_path = self._backup_destination(target)
                _replace(previous, backup_path)
            else:
                previous.unlink(missing_ok=True)
        return updated, backup_path

    def approve_all(self, subject_keys: Iterable[str] | None = None) -> list[dict]:
        """Approve each subject named, or every one waiting for approval; one that cannot be
        approved is reported and the rest go ahead."""
        if subject_keys is None:
            with self._lock:
                subject_keys = sorted(k for k, case in self._cases.items() if case.stage == workflow.APPROVAL)
        results = []
        for key in subject_keys:
            try:
                outcome = self.approve(key)
                results.append({"subject_key": key, "approved": True, "message": outcome.message})
            except QCError as exc:
                results.append({"subject_key": key, "approved": False, "message": exc.message})
            except Exception as exc:  # the share, for one subject, need not stop the others
                self.audit.event(f"Approving {key} failed: {exc!r}", level=logging.ERROR)
                results.append({"subject_key": key, "approved": False, "message": f"Could not be written: {exc}"})
        return results

    def return_case(self, subject_key: str, to: str, comment: str | None = None) -> Case:
        """Send a subject back, to the reviewers (every verdict is reviewed again) or to the
        editors (with the administrator's word). Reopens a closed subject."""
        if to not in (workflow.REVIEW, workflow.EDIT):
            raise QCError(f"A subject goes back to 'review' or to 'edit', not to '{to}'.")
        comment = (comment or "").strip() or None
        with self._lock:
            case = self._admin_case(subject_key)
            now = utc_now_iso()
            if to == workflow.REVIEW:
                workflow.return_to_review(case, comment, now)
            else:
                workflow.return_to_edit(case, comment, now)
            self._save_cases()
            snapshot = case.model_copy(deep=True)
        self.audit.record(
            "returned",
            {"subject_key": subject_key, "to": to, "stage": snapshot.stage, "comment": comment},
            summary=(
                f"Subject {subject_key} sent back to the {'reviewers' if to == workflow.REVIEW else 'editors'} by the "
                f"administrator; it waits for {_STAGE_WAITS_FOR[snapshot.stage]}."
                + (f" Comment: {comment}" if comment else "")
            ),
        )
        return snapshot

    def close_case(self, subject_key: str, comment: str | None = None) -> Case:
        """Finish a subject's quality check without writing anything into the dataset. It is
        not handed out again, unless it is sent back."""
        comment = (comment or "").strip() or None
        with self._lock:
            case = self._admin_case(subject_key)
            if case.stage == workflow.CLOSED:
                raise QCError(f"Subject {subject_key} is closed already.", status_code=409)
            workflow.close(case, comment, utc_now_iso())
            self._archive(case)
            snapshot = case.model_copy(deep=True)
        self.audit.record(
            "closed",
            {"subject_key": subject_key, "comment": comment},
            summary=f"Subject {subject_key} closed by the administrator; nothing was written into the dataset."
            + (f" Comment: {comment}" if comment else ""),
        )
        return snapshot

    def _admin_case(self, subject_key: str) -> Case:
        """Caller holds the lock. A case the administrator may act on: not approved, not being
        approved, and not in anybody's hands."""
        case = self._cases.get(subject_key)
        if case is None:
            raise QCError(f"Nobody has given a verdict on subject {subject_key} on this server.", status_code=404)
        if case.stage == workflow.APPLIED:
            raise QCError(f"Subject {subject_key} is approved and in the dataset already.", status_code=409)
        if subject_key in self._applying:
            raise QCError(f"Subject {subject_key} is being approved right now.", status_code=409)
        self._expire_stale_assignments()
        holder = next(
            (a.user for a in self._assignments.values() if a.subject_key == subject_key and a.state == "assigned"),
            None,
        )
        if holder is not None:
            raise QCError(
                f"'{holder}' holds subject {subject_key} right now. Release their lease first (Assignments).",
                status_code=409,
            )
        return case

    def _backup_destination(self, target_path: Path) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dir = self.state_dir / BACKUP_DIR_NAME / target_path.parent.parent.name
        backup_dir.mkdir(parents=True, exist_ok=True)
        name = target_path.name
        stem = name[: -len(SEGMENTATION_SUFFIX)] if name.endswith(SEGMENTATION_SUFFIX) else target_path.stem
        return backup_dir / f"{stem}_{stamp}{SEGMENTATION_SUFFIX}"

    # ------------------------------------------------------------------ stats
    def stats(self) -> QueueStats:
        """Where every eligible subject stands; each counts once."""
        self._ensure_fresh_index()
        claimed_elsewhere = self._claimed_by_other_sessions()
        with self._lock:
            self._expire_stale_assignments()
            eligible = list(self._index)
            leased = self._leased_subjects()
            stages = {stage: 0 for stage in (*workflow.OPEN_STAGES, *workflow.FINISHED_STAGES)}
            for case in self._cases.values():
                if case.stage in workflow.FINISHED_STAGES or case.subject_key not in leased:
                    stages[case.stage] += 1
            per_dataset: dict[int, int] = {}
            for ref in eligible:
                per_dataset[ref.dataset_id] = per_dataset.get(ref.dataset_id, 0) + 1
            eligible_keys = {ref.subject_key for ref in eligible}
            elsewhere = (claimed_elsewhere & eligible_keys) - leased
            available = len(eligible_keys - set(self._cases) - leased - elsewhere)
            built_at = self._index_built_at
            total = self._total_subjects
        return QueueStats(
            total_subjects=total,
            eligible_subjects=len(eligible),
            available=available,
            assigned=len(leased),
            assigned_by_other_servers=len(elsewhere),
            to_review=stages[workflow.REVIEW],
            to_edit=stages[workflow.EDIT],
            awaiting_approval=stages[workflow.APPROVAL],
            escalated=stages[workflow.ESCALATED],
            applied=stages[workflow.APPLIED],
            closed=stages[workflow.CLOSED],
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
    case: Case
    message: str
    accepted_labels: list[str] = field(default_factory=list)
    rejected_labels: dict[str, str] = field(default_factory=dict)
    missing_labels: list[str] = field(default_factory=list)
    edited_labels: list[str] = field(default_factory=list)
    removed_labels: list[str] = field(default_factory=list)
    pending_labels: list[str] = field(default_factory=list)
    segmentation_staged: bool = False

    @property
    def stage(self) -> str:
        return self.case.stage


@dataclass
class ApprovalOutcome:
    """What ``QCStore.approve`` wrote into the dataset."""

    case: Case
    updated_labels: dict[str, int]
    segmentation_written: bool
    backup_path: str | None
    message: str


# --------------------------------------------------------------------- helpers
def _could_work_on(user: User, role: str, has_segmentation: bool) -> bool:
    """Whether a user could do anything with a subject in this role: nobody is handed one
    they could not."""
    if role == REVIEWER:
        # A review judges a segmentation; without one there is nothing to judge.
        return has_segmentation
    if has_segmentation:
        # Correcting a segmentation takes seeing it.
        return user.receives_segmentation
    # A segmentation made from scratch is painted on the image.
    return user.receives_image


def _check_label_names(names: Iterable[str]) -> None:
    unknown = sorted({name for name in names if name not in LABEL_NAME_TO_VALUE})
    if unknown:
        raise QCError(f"Unknown label names: {unknown}. See 'bonehub_data_schema/labelmap.py'.")


def _review_message(case: Case, details: dict) -> str:
    """What a reviewer is told after their verdict."""
    if case.stage == workflow.EDIT:
        parts = []
        rejected = len(details["rejected"]) - len(details["missing"])
        if rejected:
            parts.append(f"{rejected} label(s) rejected")
        if details["missing"]:
            parts.append(f"{len(details['missing'])} reported missing")
        return (
            f"Sent to the editors ({', '.join(parts) or 'the subject rejected'}). Nothing is written into the "
            "dataset before the administrator approves the subject."
        )
    if case.stage == workflow.APPROVAL:
        return (
            f"Accepted: {len(details['accepted'])} label(s). The subject waits for the administrator's approval; "
            "nothing is written into the dataset before then."
        )
    return (
        f"Recorded: {len(details['accepted'])} label(s) accepted. "
        f"{len(workflow.labels_in(case, workflow.PENDING))} still wait for a reviewer."
    )


def _edit_message(case: Case, details: dict) -> str:
    """What an editor is told after uploading a correction."""
    kept = "The correction waits on the server; nothing is written into the dataset before the administrator approves it."
    if case.stage == workflow.REVIEW:
        return f"Uploaded. {len(details['pending'])} label(s) go to a reviewer. {kept}"
    if case.stage == workflow.APPROVAL:
        return f"Uploaded. The corrected labels are accepted, and the subject waits for approval. {kept}"
    return f"Uploaded. The subject waits for {_STAGE_WAITS_FOR[case.stage]}. {kept}"


def _verdicts_of(case: Case) -> dict[str, dict]:
    """Who stood behind each label of a case, for the audit trail."""
    return {
        name: {key: value for key, value in label.model_dump().items() if value is not None}
        for name, label in case.labels.items()
    }


def _verdicts_text(case: Case) -> str:
    """Who stood behind the accepted labels, in words."""
    by: dict[str, list[str]] = {}
    for name, label in case.labels.items():
        if label.state == workflow.ACCEPTED:
            who = f"accepted by '{label.by}'" + (f", corrected by '{label.edited_by}'" if label.edited_by else "")
            by.setdefault(who, []).append(name)
    return " ".join(f"{', '.join(names)}: {who}." for who, names in sorted(by.items()))


def _a(role: str) -> str:
    return "a reviewer" if role == REVIEWER else "an editor"


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


def file_fingerprint(path: Path) -> FileFingerprint | None:
    """A file as it is now, or None when there is none. Reads the whole file."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return FileFingerprint(size=stat.st_size, mtime_ns=stat.st_mtime_ns, sha256=_sha256(path))


def same_file(fingerprint: FileFingerprint | None, path: Path) -> bool:
    """Whether a file is still the one fingerprinted: still missing, or of the same content.

    Its size and time answer for an untouched file without reading it; a file whose time moved
    -- copied, or restored -- is the same when its content is.
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return fingerprint is None
    if fingerprint is None or stat.st_size != fingerprint.size:
        return False
    return stat.st_mtime_ns == fingerprint.mtime_ns or _sha256(path) == fingerprint.sha256


def _sha256(path: Path) -> str:
    """The file's SHA-256, read a piece at a time without read-ahead, as downloads are, so
    that reading it does not hold up everyone else's requests on the share."""
    digest = hashlib.sha256()
    with open(path, "rb", buffering=0) as f:
        if hasattr(os, "posix_fadvise"):  # Linux, where the server runs; the tests also run on Windows
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_RANDOM)
        while chunk := f.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


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

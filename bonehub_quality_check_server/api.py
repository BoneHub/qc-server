"""Client-facing REST API, used by the 3D Slicer extension and the browser review page.

Every endpoint authenticates with an ``X-API-Key`` header holding a user's key issued from
the admin panel, and an ``X-Client-Role`` header naming the role the client works in:
``editor`` from 3D Slicer, ``reviewer`` from the review page. The account must hold that
role, or every request is refused: a reviewer connecting from 3D Slicer is pointed to the
review page, and an editor cannot sign in to the review page. The flow a client follows is:

1. ``GET  /api/v1/ping``                          - check the key, its role and the server
2. ``POST /api/v1/subjects/next``                 - lease the next subject for this role
3. ``GET  /api/v1/assignments/{id}/image``        - download the image
4. ``GET  /api/v1/assignments/{id}/segmentation`` - download the segmentation, if any
5. ``POST /api/v1/assignments/{id}/submit``       - send the verdict back

A reviewer is handed subjects waiting for a review, and judges each label of the segmentation
as it is: ``use_stored_segmentation``, with labels accepted, rejected, or reported missing. An
editor is handed the subjects reviewers sent back, and uploads the corrected segmentation.
Verdicts are kept in the server's state folder; nothing reaches the dataset before the
administrator approves the subject. The segmentation handed out is the one under review: an
editor's correction waiting for approval, or the dataset's own.

Segmentations travel in BoneHub's own format, ``.seg.nrrd``, both ways. What a user is sent of
a subject follows their account's ``data_access``: a file left out is missing from the handout
and refused at its download endpoint.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import StreamingResponse

from bonehub_data_schema import SEGMENTATION_SUFFIX, VALID_LABEL_VALUES, __version__ as SCHEMA_VERSION

from . import __version__
from .models import (
    DATA_ACCESS_DESCRIPTIONS,
    EDITOR,
    REJECT_REASONS,
    ROLES,
    Assignment,
    HandoutLabel,
    HandoutSegment,
    SubjectHandout,
    SubmissionRequest,
    SubmissionResult,
    User,
)
from .store import LABEL_NAME_TO_VALUE, QCError, QCStore

router = APIRouter(prefix="/api/v1", tags=["client"])

UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024

#: How much of a file a download reads from the share at a time; see :func:`_send_from_share`.
DOWNLOAD_CHUNK_BYTES = 2 * 1024 * 1024

#: The header in which a client names the role it works in.
ROLE_HEADER = "X-Client-Role"


def get_store(request: Request) -> QCStore:
    store: QCStore | None = getattr(request.app.state, "store", None)
    if store is None:
        raise QCError("The server is not initialised.", status_code=503)
    return store


def client_role(x_client_role: str | None = Header(None, alias=ROLE_HEADER)) -> str | None:
    """The role the calling client says it works in, as sent. :func:`get_user` checks it."""
    return x_client_role


def get_user(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    role: str | None = Depends(client_role),
) -> User:
    """The user behind the key, who must hold the role of the client the request comes from.

    The key is checked first, so a request without one is told that before anything else.
    """
    user = get_store(request).authenticate(x_api_key)
    _require_role(user, role, review_page=f"{request.base_url}review")
    return user


@router.get("/ping")
def ping(request: Request, user: User = Depends(get_user), role: str = Depends(client_role)) -> dict:
    """Confirm the key works in this client's role, and report what this user is allowed to see."""
    store = get_store(request)
    return {
        "status": "ok",
        "server": "bonehub-dataset-quality-check-server",
        "server_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "user": user.name,
        "role": role,
        "roles": user.roles,
        "allowed_dataset_ids": user.allowed_dataset_ids,
        "data_access": user.data_access,
        "edits_need_review": store.config.edits_need_review,
        "mark_removed_labels_absent": store.config.mark_removed_labels_absent,
        "lease_ttl_seconds": store.config.lease_ttl_seconds,
        "max_concurrent_assignments": store.config.max_concurrent_assignments_per_user,
    }


@router.get("/labels")
def labels(user: User = Depends(get_user)) -> dict:
    """The BoneHub label map and label statuses, so the client can name and check segments."""
    return {
        "schema_version": SCHEMA_VERSION,
        "label_name_to_value": LABEL_NAME_TO_VALUE,
        "label_status_values": {str(k): v for k, v in VALID_LABEL_VALUES.items()},
        "reject_reasons": REJECT_REASONS,
        "segmentation_suffix": SEGMENTATION_SUFFIX,
    }


@router.post("/subjects/next", response_model=SubjectHandout)
def next_subject(
    request: Request, user: User = Depends(get_user), role: str = Depends(client_role)
) -> SubjectHandout:
    """Lease the next subject for this user, in the role of their client.

    A reviewer is handed a subject waiting for a review, an editor one a reviewer sent back,
    or one without any segmentation. If the user already holds their maximum number of
    subjects in this role, the oldest open one is returned again instead of an error, so a
    client that lost its local copy can simply ask for the next subject again.
    """
    store = get_store(request)
    return _handout(store, store.next_subject(user, role), user)


@router.get("/assignments", response_model=list[Assignment])
def my_assignments(
    request: Request, user: User = Depends(get_user), role: str = Depends(client_role)
) -> list[Assignment]:
    """The subjects this user holds in the role of this client."""
    return get_store(request).open_assignments_of(user.name, role)


@router.get("/assignments/{assignment_id}", response_model=SubjectHandout)
def assignment_detail(assignment_id: str, request: Request, user: User = Depends(get_user)) -> SubjectHandout:
    store = get_store(request)
    return _handout(store, store.get_assignment(assignment_id, user), user)


@router.get("/assignments/{assignment_id}/image")
def download_image(assignment_id: str, request: Request, user: User = Depends(get_user)) -> StreamingResponse:
    store = get_store(request)
    assignment = store.get_assignment(assignment_id, user)
    _require_sent(user, "image", user.receives_image)
    path = store.image_path(assignment.dataset_id, assignment.subject_id)
    if not path.exists():
        raise QCError(f"The image for {assignment.subject_key} is missing on the server.", status_code=404)
    return _send_from_share(path, media_type="application/gzip")


@router.get("/assignments/{assignment_id}/segmentation")
def download_segmentation(assignment_id: str, request: Request, user: User = Depends(get_user)) -> StreamingResponse:
    """The segmentation under quality check (``.seg.nrrd``): an editor's correction waiting for
    approval, or else the dataset's own."""
    store = get_store(request)
    assignment = store.get_assignment(assignment_id, user)
    _require_sent(user, "segmentation", user.receives_segmentation)
    path = store.current_segmentation_path(assignment.dataset_id, assignment.subject_id)
    if path is None:
        raise QCError(f"Subject {assignment.subject_key} has no segmentation yet.", status_code=404)
    return _send_from_share(path, media_type="application/octet-stream")


@router.post("/assignments/{assignment_id}/extend", response_model=Assignment)
def extend_assignment(assignment_id: str, request: Request, user: User = Depends(get_user)) -> Assignment:
    """Keep the lease alive while the user is still working on the subject."""
    return get_store(request).extend_assignment(assignment_id, user)


@router.post("/assignments/{assignment_id}/release", response_model=Assignment)
def release_assignment(assignment_id: str, request: Request, user: User = Depends(get_user)) -> Assignment:
    """Return a subject to the queue without judging it."""
    return get_store(request).release_assignment(assignment_id, user)


# A plain def, like every other endpoint, so FastAPI runs it in a worker thread. Reading,
# checking and rewriting a segmentation takes seconds for a real scan; on the event loop
# that would hold up every other request, a ping included.
@router.post("/assignments/{assignment_id}/submit", response_model=SubmissionResult)
def submit(
    assignment_id: str,
    request: Request,
    metadata: str = Form(..., description="JSON body matching SubmissionRequest"),
    segmentation: UploadFile | None = File(
        None, description="An editor's corrected segmentation as .seg.nrrd, on the image's voxel grid"
    ),
    user: User = Depends(get_user),
    role: str = Depends(client_role),
) -> SubmissionResult:
    """Receive a user's verdict, in the role the subject was handed out in.

    A reviewer sends ``use_stored_segmentation`` and no file, with each label under review
    accepted (``confirmed_labels``) or rejected (``rejected_labels``), and bones the
    segmentation lacks in ``missing_labels``. An editor uploads the corrected segmentation.
    ``quality_check_confirmed=false`` rejects the subject: from a reviewer, every label under
    review goes to the editors; from an editor, the subject goes to the administrator.

    The verdict is kept in the server's state folder. Nothing in the dataset changes until the
    administrator approves the subject.
    """
    store = get_store(request)

    try:
        payload = SubmissionRequest(**json.loads(metadata))
    except json.JSONDecodeError as exc:
        raise QCError(f"The 'metadata' part is not valid JSON: {exc}") from exc
    except Exception as exc:
        raise QCError(f"Invalid submission metadata: {exc}") from exc

    tmp_path: Path | None = None
    try:
        if segmentation is not None:
            tmp_path = _spool_upload(store, segmentation)
        outcome = store.submit(
            assignment_id=assignment_id,
            user=user,
            quality_check_confirmed=payload.quality_check_confirmed,
            segmentation_tmp_path=tmp_path,
            confirmed_labels=payload.confirmed_labels,
            comment=payload.comment,
            use_stored_segmentation=payload.use_stored_segmentation,
            rejected_labels=payload.rejected_labels,
            missing_labels=payload.missing_labels,
            role=role,
        )
    finally:
        # What waits for approval is a rewritten copy, so the upload itself is never kept.
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    return SubmissionResult(
        assignment_id=outcome.assignment.assignment_id,
        subject_key=outcome.assignment.subject_key,
        quality_check_confirmed=bool(outcome.assignment.quality_check_confirmed),
        state=outcome.assignment.state,
        stage=outcome.stage,
        segmentation_staged=outcome.segmentation_staged,
        accepted_labels=outcome.accepted_labels,
        rejected_labels=outcome.rejected_labels,
        missing_labels=outcome.missing_labels,
        edited_labels=outcome.edited_labels,
        removed_labels=outcome.removed_labels,
        pending_labels=outcome.pending_labels,
        message=outcome.message,
    )


# --------------------------------------------------------------------- helpers
def _require_role(user: User, role: str | None, review_page: str) -> None:
    """Refuse a client whose role the account does not hold, and say where the user can work."""
    if not role:
        raise QCError(
            f"The client did not say which role it works in: send the '{ROLE_HEADER}' header, 'editor' from "
            "3D Slicer or 'reviewer' from the review page.",
            status_code=400,
        )
    if role not in ROLES:
        raise QCError(
            f"'{role}' in the '{ROLE_HEADER}' header is not a role. Send 'editor' or 'reviewer'.", status_code=400
        )
    if role in user.roles:
        return
    # Every account holds at least one role, so one without this role holds the other.
    if role == EDITOR:
        raise QCError(
            f"'{user.name}' is a reviewer, not an editor, so cannot work in 3D Slicer, where segmentations are "
            f"corrected. Review subjects in the browser instead, on the review page: {review_page}",
            status_code=403,
        )
    raise QCError(
        f"'{user.name}' is an editor, not a reviewer, so cannot sign in to the review page. Work on subjects "
        "in 3D Slicer instead, with the BoneHub Quality Check extension.",
        status_code=403,
    )


def _handout(store: QCStore, assignment: Assignment, user: User) -> SubjectHandout:
    dataset_id, subject_id = assignment.dataset_id, assignment.subject_id
    subject = store.subject_info(dataset_id, subject_id)
    segmentation_labels = dict(subject.segmentation or {})
    case = store.handout_case(assignment)
    current = store.current_segmentation_path(dataset_id, subject_id)
    # Only what this user is sent is offered; the download endpoints refuse the rest.
    has_image = user.receives_image and store.image_path(dataset_id, subject_id).exists()
    has_segmentation = user.receives_segmentation and current is not None
    segments = store.segment_table(dataset_id, subject_id) if has_segmentation else []
    issue = store.stored_segmentation_issue(dataset_id, subject_id) if has_segmentation else None
    base = f"/api/v1/assignments/{assignment.assignment_id}"
    return SubjectHandout(
        assignment_id=assignment.assignment_id,
        dataset_id=dataset_id,
        subject_id=subject_id,
        subject_key=assignment.subject_key,
        expires_at=assignment.expires_at,
        role=assignment.role,
        stage=case.stage,
        data_access=user.data_access,
        has_image=has_image,
        has_segmentation=has_segmentation,
        segmentation_source=None if current is None else "staged" if case.staged else "dataset",
        segmentation_labels=segmentation_labels,
        labels=[
            HandoutLabel(
                name=name,
                value=LABEL_NAME_TO_VALUE.get(name),
                dataset_status=segmentation_labels.get(name),
                state=label.state,
                painted=label.painted,
                reason=label.reason,
                by=label.by,
                edited_by=label.edited_by,
            )
            for name, label in case.labels.items()
        ],
        requests=case.requests,
        history=case.events,
        segments=[
            HandoutSegment(
                number=segment.number,
                label=segment.label,
                value=segment.value,
                color=list(segment.color),
                extent=list(segment.extent) if segment.extent else None,
            )
            for segment in segments
        ],
        stored_segmentation_issue=issue,
        subject_info=subject.sorted_dict(),
        dataset_info=store.dataset_info(dataset_id),
        image_url=f"{base}/image" if has_image else None,
        segmentation_url=f"{base}/segmentation" if has_segmentation else None,
    )


def _send_from_share(path: Path, media_type: str) -> StreamingResponse:
    """Stream a file from the dataset share without holding up everyone else's requests.

    The server reaches the share over one SMB connection. Read the usual way, a download fills
    that connection with megabytes of read-ahead, and every other request waits behind them:
    on a 17 MB/s share, another user's next subject took as long as the whole download, 13 s
    instead of 0.2 s. Read 2 MB at a time with read-ahead off, it took 1.8 s, and the download
    was as fast.
    """
    return StreamingResponse(
        _read_in_chunks(path),
        media_type=media_type,
        headers={
            "Content-Length": str(path.stat().st_size),
            "Content-Disposition": f'attachment; filename="{path.name}"',
        },
    )


def _read_in_chunks(path: Path) -> Iterator[bytes]:
    """The file, :data:`DOWNLOAD_CHUNK_BYTES` at a time; Starlette reads it in a worker thread."""
    with open(path, "rb", buffering=0) as f:
        if hasattr(os, "posix_fadvise"):  # Linux, where the server runs; the tests also run on Windows
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_RANDOM)  # "random" access: no read-ahead
        while chunk := f.read(DOWNLOAD_CHUNK_BYTES):
            yield chunk


def _require_sent(user: User, what: str, sent: bool) -> None:
    if not sent:
        raise QCError(
            f"'{user.name}' is sent {DATA_ACCESS_DESCRIPTIONS[user.data_access]}, so the {what} is not sent.",
            status_code=403,
        )


def _spool_upload(store: QCStore, upload: UploadFile) -> Path:
    """Stream an upload to the state folder, refusing anything over the configured cap."""
    tmp_path = store.new_upload_path()
    written = 0
    try:
        with open(tmp_path, "wb") as f:
            while True:
                chunk = upload.file.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > store.config.max_upload_bytes:
                    raise QCError(
                        f"The uploaded segmentation is larger than the "
                        f"{store.config.max_upload_bytes} byte limit.",
                        status_code=413,
                    )
                f.write(chunk)
        if written == 0:
            raise QCError("The uploaded segmentation file is empty.")
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path

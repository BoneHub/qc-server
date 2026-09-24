"""Client-facing REST API, used by the 3D Slicer extension and the browser review page.

Every endpoint authenticates with an ``X-API-Key`` header holding a reviewer key issued
from the admin panel. The flow a client follows is:

1. ``GET  /api/v1/ping``                          - check the key and the server
2. ``POST /api/v1/subjects/next``                 - lease the next subject
3. ``GET  /api/v1/assignments/{id}/image``        - download the image
4. ``GET  /api/v1/assignments/{id}/segmentation`` - download the segmentation, if any
5. ``POST /api/v1/assignments/{id}/submit``       - send the verdict back

Segmentations travel in BoneHub's own format, ``.seg.nrrd``, both ways. A client that only
looks, like the review page, confirms with ``use_stored_segmentation`` instead of uploading.

What a reviewer is sent of a subject follows their account's ``data_access``: a file left
out is missing from the handout and refused at its download endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import FileResponse

from bonehub_data_schema import SEGMENTATION_SUFFIX, VALID_LABEL_VALUES, __version__ as SCHEMA_VERSION

from .config import STATUS_REVIEWED
from .models import (
    DATA_ACCESS_DESCRIPTIONS,
    Assignment,
    HandoutSegment,
    SubjectHandout,
    SubmissionRequest,
    SubmissionResult,
    User,
)
from .store import LABEL_NAME_TO_VALUE, QCError, QCStore

router = APIRouter(prefix="/api/v1", tags=["client"])

UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024


def get_store(request: Request) -> QCStore:
    store: QCStore | None = getattr(request.app.state, "store", None)
    if store is None:
        raise QCError("The server is not initialised.", status_code=503)
    return store


def get_user(request: Request, x_api_key: str | None = Header(None, alias="X-API-Key")) -> User:
    return get_store(request).authenticate(x_api_key)


@router.get("/ping")
def ping(request: Request, user: User = Depends(get_user)) -> dict:
    """Confirm the key works and report what this reviewer is allowed to see."""
    store = get_store(request)
    return {
        "status": "ok",
        "server": "bonehub-dataset-quality-check-server",
        "schema_version": SCHEMA_VERSION,
        "user": user.name,
        "allowed_dataset_ids": user.allowed_dataset_ids,
        "data_access": user.data_access,
        "confirmed_label_status": STATUS_REVIEWED,
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
        "confirmed_label_status": STATUS_REVIEWED,
        "segmentation_suffix": SEGMENTATION_SUFFIX,
    }


@router.post("/subjects/next", response_model=SubjectHandout)
def next_subject(request: Request, user: User = Depends(get_user)) -> SubjectHandout:
    """Lease the next subject for this reviewer.

    If the reviewer already holds their maximum number of subjects, the oldest open one is
    returned again instead of an error, so a client that lost its local copy can simply
    ask for the next subject again.
    """
    store = get_store(request)
    return _handout(store, store.next_subject(user), user)


@router.get("/assignments", response_model=list[Assignment])
def my_assignments(request: Request, user: User = Depends(get_user)) -> list[Assignment]:
    return get_store(request).open_assignments_of(user.name)


@router.get("/assignments/{assignment_id}", response_model=SubjectHandout)
def assignment_detail(assignment_id: str, request: Request, user: User = Depends(get_user)) -> SubjectHandout:
    store = get_store(request)
    return _handout(store, store.get_assignment(assignment_id, user), user)


@router.get("/assignments/{assignment_id}/image")
def download_image(assignment_id: str, request: Request, user: User = Depends(get_user)) -> FileResponse:
    store = get_store(request)
    assignment = store.get_assignment(assignment_id, user)
    _require_sent(user, "image", user.receives_image)
    path = store.image_path(assignment.dataset_id, assignment.subject_id)
    if not path.exists():
        raise QCError(f"The image for {assignment.subject_key} is missing on the server.", status_code=404)
    return FileResponse(path, media_type="application/gzip", filename=path.name)


@router.get("/assignments/{assignment_id}/segmentation")
def download_segmentation(assignment_id: str, request: Request, user: User = Depends(get_user)) -> FileResponse:
    """The stored segmentation, as the dataset keeps it (``.seg.nrrd``)."""
    store = get_store(request)
    assignment = store.get_assignment(assignment_id, user)
    _require_sent(user, "segmentation", user.receives_segmentation)
    path = store.segmentation_path(assignment.dataset_id, assignment.subject_id)
    if not path.exists():
        raise QCError(f"Subject {assignment.subject_key} has no segmentation yet.", status_code=404)
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@router.post("/assignments/{assignment_id}/extend", response_model=Assignment)
def extend_assignment(assignment_id: str, request: Request, user: User = Depends(get_user)) -> Assignment:
    """Keep the lease alive while a reviewer is still working on the subject."""
    return get_store(request).extend_assignment(assignment_id, user)


@router.post("/assignments/{assignment_id}/release", response_model=Assignment)
def release_assignment(assignment_id: str, request: Request, user: User = Depends(get_user)) -> Assignment:
    """Return a subject to the queue without judging it."""
    return get_store(request).release_assignment(assignment_id, user)


@router.post("/assignments/{assignment_id}/submit", response_model=SubmissionResult)
async def submit(
    assignment_id: str,
    request: Request,
    metadata: str = Form(..., description="JSON body matching SubmissionRequest"),
    segmentation: UploadFile | None = File(
        None, description="The reviewed segmentation as .seg.nrrd, on the image's voxel grid; required when confirming"
    ),
    user: User = Depends(get_user),
) -> SubmissionResult:
    """Receive a reviewer's verdict.

    ``quality_check_confirmed=true`` stores the uploaded segmentation and sets the reviewed
    labels in ``Subject_info_XXX.json`` to status 2, "available, reviewed and corrected".
    With ``use_stored_segmentation`` and no file, the stored segmentation is confirmed as it
    is and left untouched. ``false`` leaves the dataset untouched and only writes the audit
    trail.
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
            tmp_path = await _spool_upload(store, segmentation)
        outcome = store.submit(
            assignment_id=assignment_id,
            user=user,
            quality_check_confirmed=payload.quality_check_confirmed,
            segmentation_tmp_path=tmp_path,
            confirmed_labels=payload.confirmed_labels,
            comment=payload.comment,
            use_stored_segmentation=payload.use_stored_segmentation,
        )
    finally:
        # The dataset receives a rewritten copy, so the upload itself is never kept.
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    return SubmissionResult(
        assignment_id=outcome.assignment.assignment_id,
        subject_key=outcome.assignment.subject_key,
        quality_check_confirmed=bool(outcome.assignment.quality_check_confirmed),
        state=outcome.assignment.state,
        segmentation_written=outcome.assignment.segmentation_written,
        updated_labels=outcome.updated_labels,
        removed_labels=outcome.removed_labels,
        backup_path=outcome.backup_path,
        message=outcome.message,
    )


# --------------------------------------------------------------------- helpers
def _handout(store: QCStore, assignment: Assignment, user: User) -> SubjectHandout:
    dataset_id, subject_id = assignment.dataset_id, assignment.subject_id
    subject = store.subject_info(dataset_id, subject_id)
    segmentation_labels = dict(subject.segmentation or {})
    # Only what this reviewer is sent is offered; the download endpoints refuse the rest.
    has_image = user.receives_image and store.image_path(dataset_id, subject_id).exists()
    has_segmentation = user.receives_segmentation and store.segmentation_path(dataset_id, subject_id).exists()
    segments = store.segment_table(dataset_id, subject_id) if has_segmentation else []
    issue = store.stored_segmentation_issue(dataset_id, subject_id) if has_segmentation else None
    base = f"/api/v1/assignments/{assignment.assignment_id}"
    return SubjectHandout(
        assignment_id=assignment.assignment_id,
        dataset_id=dataset_id,
        subject_id=subject_id,
        subject_key=assignment.subject_key,
        expires_at=assignment.expires_at,
        data_access=user.data_access,
        has_image=has_image,
        has_segmentation=has_segmentation,
        segmentation_labels=segmentation_labels,
        label_values={name: LABEL_NAME_TO_VALUE[name] for name in segmentation_labels if name in LABEL_NAME_TO_VALUE},
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


def _require_sent(user: User, what: str, sent: bool) -> None:
    if not sent:
        raise QCError(
            f"'{user.name}' is sent {DATA_ACCESS_DESCRIPTIONS[user.data_access]}, so the {what} is not sent.",
            status_code=403,
        )


async def _spool_upload(store: QCStore, upload: UploadFile) -> Path:
    """Stream an upload to the state folder, refusing anything over the configured cap."""
    tmp_path = store.new_upload_path()
    written = 0
    try:
        with open(tmp_path, "wb") as f:
            while True:
                chunk = await upload.read(UPLOAD_CHUNK_BYTES)
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

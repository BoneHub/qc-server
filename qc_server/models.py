"""Pydantic models shared by the persisted server state and the REST API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: A lease is ``assigned`` until the user gives a verdict (``submitted``), gives the subject
#: back (``released``), or runs out of time (``expired``).
AssignmentState = Literal["assigned", "submitted", "released", "expired"]

#: What a user may do, and so which client they may use. A reviewer looks at subjects on the
#: browser review page and accepts or rejects each label of the segmentation as it is; an editor
#: corrects the segmentations reviewers rejected, in 3D Slicer. A user holds one role or both.
Role = Literal["reviewer", "editor"]

REVIEWER: Role = "reviewer"
EDITOR: Role = "editor"

#: Every role, in the order a user's roles are listed.
ROLES: tuple[Role, ...] = (REVIEWER, EDITOR)

#: The roles of a new user unless others are chosen.
DEFAULT_ROLES: tuple[Role, ...] = ROLES

#: What a user is sent of each subject. A file a user is not sent is left out of the handout
#: and refused at its download endpoint.
DataAccess = Literal["image_and_segmentation", "segmentation", "image"]

DEFAULT_DATA_ACCESS: DataAccess = "image_and_segmentation"

DATA_ACCESS_DESCRIPTIONS: dict[str, str] = {
    "image_and_segmentation": "the image and its segmentation",
    "segmentation": "the segmentation only",
    "image": "the image only",
}

#: Where a subject stands in the quality check; see ``workflow``. The first four are open.
CaseStage = Literal["review", "edit", "approval", "escalated", "applied", "closed"]

#: What has become of one label of a subject; see ``workflow``.
LabelState = Literal["pending", "accepted", "rejected", "removed", "kept"]

#: Why a reviewer rejected a label: its segmentation needs correcting, the bone should not be
#: segmented at all, or it should be and is not.
RejectReason = Literal["quality", "absent", "missing"]

REJECT_REASONS: dict[str, str] = {
    "quality": "needs correction",
    "absent": "should not be there",
    "missing": "is missing",
}


class User(BaseModel):
    """A user: a reviewer, an editor, or both. The API key itself is never stored, only its HMAC digest."""

    name: str = Field(..., description="Unique user name, used as the login identity")
    key_prefix: str = Field(..., description="First characters of the API key, shown in the admin panel")
    key_hash: str = Field(..., description="HMAC-SHA256 of the API key, keyed with the server private key")
    created_at: str = Field(..., description="ISO-8601 UTC timestamp")
    active: bool = Field(True, description="Disabled users are rejected at authentication")
    roles: list[Role] = Field(
        ..., description="What this user may do: 'reviewer' (the browser review page), 'editor' (3D Slicer), or both."
    )
    allowed_dataset_ids: list[int] | None = Field(
        None, description="Restrict this user to these dataset ids. None means every dataset the server serves."
    )
    data_access: DataAccess = Field(
        ...,
        description=(
            "What this user is sent of each subject: 'image_and_segmentation', 'segmentation' "
            "(the segmentation only) or 'image' (the image only)."
        ),
    )
    note: str = Field("", description="Free-form note for the administrator")

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("roles")
    @classmethod
    def _each_role_once(cls, roles: list[str]) -> list[str]:
        """Each role once, in the order of ROLES. A user without a role could use no client at all."""
        ordered = [role for role in ROLES if role in roles]
        if not ordered:
            raise ValueError("a user needs at least one role")
        return ordered

    @property
    def is_reviewer(self) -> bool:
        return REVIEWER in self.roles

    @property
    def is_editor(self) -> bool:
        return EDITOR in self.roles

    @property
    def receives_image(self) -> bool:
        return self.data_access != "segmentation"

    @property
    def receives_segmentation(self) -> bool:
        return self.data_access != "image"

    def public_dict(self) -> dict:
        """Everything about the user except the key digest."""
        return self.model_dump(exclude={"key_hash"})


class Assignment(BaseModel):
    """One subject handed to one user, in one role."""

    assignment_id: str
    subject_key: str = Field(..., description="'<dataset_id:03d>_<subject_id:06d>'")
    dataset_id: int
    subject_id: int
    user: str
    role: Role = Field(..., description="The role the subject was handed out in")
    case_revision: int = Field(
        ..., description="The revision of the subject's case when it was handed out, 0 when it had none"
    )
    state: AssignmentState = "assigned"
    assigned_at: str
    expires_at: str
    submitted_at: str | None = None
    quality_check_confirmed: bool | None = None
    comment: str | None = None
    # What the verdict did; the subject's case tells the whole story.
    accepted_labels: list[str] | None = None
    rejected_labels: dict[str, str] | None = None
    missing_labels: list[str] | None = None
    edited_labels: list[str] | None = None
    removed_labels: list[str] | None = None
    segmentation_staged: bool = False
    stage_after: CaseStage | None = Field(None, description="Where the verdict sent the subject")

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class FileFingerprint(BaseModel):
    """A file as it was: its size and time, and its content's digest for when the time moved."""

    size: int
    mtime_ns: int
    sha256: str


class CaseLabel(BaseModel):
    """One label of a subject, on its way through the quality check."""

    state: LabelState
    painted: bool = Field(True, description="The label is in the segmentation under quality check")
    reason: RejectReason | None = Field(None, description="Why a reviewer rejected it")
    by: str | None = Field(None, description="Who gave it this state; None for the state the case began with")
    at: str | None = None
    edited_by: str | None = Field(None, description="The editor whose correction this label now comes from")

    model_config = ConfigDict(extra="forbid")


class CaseRequest(BaseModel):
    """Something an editor must see to that is no single label: a reviewer's rejection of the
    subject as a whole when nothing in it was under review, or the administrator's word."""

    by: str
    role: str = Field(..., description="'reviewer', or 'admin'")
    at: str
    comment: str | None = None

    model_config = ConfigDict(extra="forbid")


class CaseEvent(BaseModel):
    """One step in a subject's quality check: a verdict, or something the administrator did."""

    at: str
    by: str
    role: str = Field(..., description="'reviewer', 'editor' or 'admin'")
    action: str = Field(..., description="'review', 'edit', 'escalate', 'approve', 'return' or 'close'")
    stage: CaseStage = Field(..., description="Where the subject went")
    comment: str | None = None
    assignment_id: str | None = None
    details: dict = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class Case(BaseModel):
    """A subject's quality check on this server: every verdict so far, kept apart from the dataset
    until the administrator approves it."""

    subject_key: str
    dataset_id: int
    subject_id: int
    stage: CaseStage
    revision: int = Field(0, description="Counts the changes to the case")
    labels: dict[str, CaseLabel] = Field(default_factory=dict)
    requests: list[CaseRequest] = Field(default_factory=list, description="Open requests for an editor")
    staged: bool = Field(False, description="An editor's segmentation waits in this server's state folder")
    edited_by: str | None = Field(None, description="The editor of the staged segmentation")
    base: FileFingerprint | None = Field(
        None, description="The dataset's segmentation when the case began; None when it had none"
    )
    created_at: str
    updated_at: str
    applied_at: str | None = None
    backup_path: str | None = None
    events: list[CaseEvent] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class HandoutSegment(BaseModel):
    """One segment of the segmentation handed out, as its file header describes it."""

    number: int = Field(..., description="The segment number its voxels hold in the .seg.nrrd")
    label: str = Field(..., description="BoneLabelMap name")
    value: int = Field(..., description="BoneLabelMap value")
    color: list[float] = Field(..., description="RGB in 0..1, the colour the file gives the segment")
    extent: list[int] | None = Field(
        None,
        description=(
            "Bounding box in voxel indices of the file, [i_min, i_max, j_min, j_max, k_min, k_max], "
            "or None when the header does not record it"
        ),
    )


class HandoutLabel(BaseModel):
    """One label of the subject, with what the quality check has made of it so far."""

    name: str
    value: int | None = Field(None, description="BoneLabelMap value")
    dataset_status: int | None = Field(None, description="The label's status in Subject_info (0, 1 or 2), if listed")
    state: LabelState = Field(
        ...,
        description=(
            "'pending' waits for a reviewer, 'accepted' waits for approval, 'rejected' waits for an editor, "
            "'removed' is not in the segmentation, 'kept' is not under review and stays as the dataset has it"
        ),
    )
    painted: bool = Field(
        True,
        description=(
            "Whether the segmentation holds the label. A 'pending' label that is not painted is an editor's "
            "removal, waiting for a reviewer to agree; a 'rejected' one was reported missing"
        ),
    )
    reason: RejectReason | None = None
    by: str | None = None
    edited_by: str | None = None


class SubjectHandout(BaseModel):
    """What a client receives when it asks for the next subject.

    ``has_image`` and ``has_segmentation`` say whether there is a file for this user to
    download: the server has it and the user's ``data_access`` includes it. The segmentation
    is the one under review: an editor's correction waiting in the server's state folder, or
    else the dataset's own.
    """

    assignment_id: str
    dataset_id: int
    subject_id: int
    subject_key: str
    expires_at: str
    role: Role = Field(..., description="The role the subject was handed out in")
    stage: CaseStage = Field(..., description="What is asked: 'review' the labels, or 'edit' the segmentation")
    data_access: DataAccess = Field(..., description="What this user is sent of each subject")
    has_image: bool
    has_segmentation: bool
    segmentation_source: Literal["dataset", "staged"] | None = Field(
        None,
        description=(
            "'staged' when the segmentation is an editor's correction, not in the dataset yet; 'dataset' when "
            "it is the dataset's own; None when there is none"
        ),
    )
    segmentation_labels: dict[str, int] = Field(
        default_factory=dict, description="Label name -> current label status in Subject_info (0, 1 or 2)"
    )
    labels: list[HandoutLabel] = Field(
        default_factory=list, description="Every label of the subject, with its state in the quality check"
    )
    requests: list[CaseRequest] = Field(
        default_factory=list, description="Open requests for an editor that are about no single label"
    )
    history: list[CaseEvent] = Field(default_factory=list, description="The subject's quality check so far")
    segments: list[HandoutSegment] = Field(
        default_factory=list,
        description="The segments of the segmentation, read from its header; empty unless has_segmentation",
    )
    stored_segmentation_issue: str | None = Field(
        None,
        description=(
            "Why the segmentation cannot be accepted as it is (use_stored_segmentation), for instance because "
            "it is not on its image's voxel grid; None when it can. Only given with has_segmentation."
        ),
    )
    subject_info: dict = Field(default_factory=dict, description="The subject's entry from Subject_info_XXX.json")
    dataset_info: dict = Field(default_factory=dict, description="The dataset's Dataset_info_XXX.json")
    image_url: str | None = None
    segmentation_url: str | None = None


class SubmissionRequest(BaseModel):
    """Body of a submission, sent as the ``metadata`` part of a multipart request.

    A reviewer judges the segmentation as it is (``use_stored_segmentation``): each label is
    accepted or rejected, and bones it lacks are reported missing. An editor uploads the
    corrected segmentation. ``quality_check_confirmed=false`` rejects the subject as a whole:
    from a reviewer, every label under review goes to the editors; from an editor, the
    subject goes to the administrator.
    """

    quality_check_confirmed: bool = Field(
        ..., description="True gives the verdicts below, or the corrected segmentation; false rejects the subject"
    )
    confirmed_labels: list[str] | None = Field(
        None,
        description=(
            "Labels the user accepts (a reviewer) or vouches for (an editor). Defaults to every label under "
            "review that is not rejected (a reviewer), or every label in the upload (an editor). Ignored when "
            "quality_check_confirmed is false."
        ),
    )
    rejected_labels: dict[str, RejectReason] | None = Field(
        None,
        description=(
            "A reviewer's rejections, label -> 'quality' (needs correction) or 'absent' (should not be there) "
            "for a label in the segmentation, 'missing' for one that is not. They go to the editors."
        ),
    )
    missing_labels: list[str] | None = Field(
        None,
        description=(
            "Labels a reviewer reports missing from the segmentation; the same as rejecting them as 'missing'. "
            "They go to the editors."
        ),
    )
    use_stored_segmentation: bool = Field(
        False,
        description=(
            "Judge the segmentation the server holds, as it is, instead of uploading one: how a reviewer "
            "gives a verdict, on the browser review page. Uploading a segmentation takes an editor."
        ),
    )
    comment: str | None = Field(None, description="Free-form comment, kept with the subject and in the audit log")

    model_config = ConfigDict(extra="forbid")


class SubmissionResult(BaseModel):
    """What the server did with a submission. Nothing reaches the dataset before the
    administrator approves the subject."""

    assignment_id: str
    subject_key: str
    quality_check_confirmed: bool
    state: AssignmentState
    stage: CaseStage = Field(..., description="Where the subject went: review, edit, approval or escalated")
    segmentation_staged: bool = Field(False, description="The uploaded segmentation waits in the server's state folder")
    accepted_labels: list[str] = Field(default_factory=list)
    rejected_labels: dict[str, str] = Field(default_factory=dict)
    missing_labels: list[str] = Field(default_factory=list)
    edited_labels: list[str] = Field(default_factory=list, description="Labels the upload changed or added")
    removed_labels: list[str] = Field(default_factory=list)
    pending_labels: list[str] = Field(default_factory=list, description="Labels now waiting for a reviewer")
    message: str = ""


class QueueStats(BaseModel):
    """Snapshot of the queue, for the admin panel. Each eligible subject counts once."""

    total_subjects: int
    eligible_subjects: int
    available: int = Field(..., description="Not looked at yet, and free: waiting for its first review")
    assigned: int = Field(..., description="Handed out on this server right now")
    assigned_by_other_servers: int = Field(0, description="Eligible subjects out, or in progress, on another server")
    to_review: int = Field(0, description="In progress, waiting for a reviewer")
    to_edit: int = Field(0, description="In progress, waiting for an editor")
    awaiting_approval: int = Field(0, description="Every label accepted, waiting for the administrator")
    escalated: int = Field(0, description="An editor sent it to the administrator")
    applied: int = Field(0, description="Approved and written into the dataset")
    closed: int = Field(0, description="Closed by the administrator without writing anything")
    datasets: dict[int, int] = Field(default_factory=dict, description="dataset id -> eligible subject count")
    index_built_at: str | None = None

"""Pydantic models shared by the persisted server state and the REST API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AssignmentState = Literal["assigned", "confirmed", "rejected", "released", "expired"]

#: States in which a subject is finished and is never handed out again.
TERMINAL_STATES: set[str] = {"confirmed"}

#: What a user may do, and so which client they may use. A reviewer looks at subjects on the
#: browser review page and confirms the stored segmentation as it is, or rejects it; an editor
#: corrects segmentations in 3D Slicer and uploads them. A user holds one role or both.
Role = Literal["reviewer", "editor"]

REVIEWER: Role = "reviewer"
EDITOR: Role = "editor"

#: Every role, in the order a user's roles are listed.
ROLES: tuple[Role, ...] = (REVIEWER, EDITOR)

#: The roles of a new user unless others are chosen, and of an account written before roles
#: existed, which could use both clients.
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


class User(BaseModel):
    """A user: a reviewer, an editor, or both. The API key itself is never stored, only its HMAC digest."""

    name: str = Field(..., description="Unique user name, used as the login identity")
    key_prefix: str = Field(..., description="First characters of the API key, shown in the admin panel")
    key_hash: str = Field(..., description="HMAC-SHA256 of the API key, keyed with the server private key")
    created_at: str = Field(..., description="ISO-8601 UTC timestamp")
    active: bool = Field(True, description="Disabled users are rejected at authentication")
    roles: list[Role] = Field(
        default_factory=lambda: list(DEFAULT_ROLES),
        description="What this user may do: 'reviewer' (the browser review page), 'editor' (3D Slicer), or both.",
    )
    allowed_dataset_ids: list[int] | None = Field(
        None, description="Restrict this user to these dataset ids. None means every dataset the server serves."
    )
    data_access: DataAccess = Field(
        DEFAULT_DATA_ACCESS,
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
    """One subject handed to one user."""

    assignment_id: str
    subject_key: str = Field(..., description="'<dataset_id:03d>_<subject_id:06d>'")
    dataset_id: int
    subject_id: int
    user: str
    state: AssignmentState = "assigned"
    assigned_at: str
    expires_at: str
    submitted_at: str | None = None
    quality_check_confirmed: bool | None = None
    confirmed_labels: list[str] | None = None
    removed_labels: list[str] | None = None
    comment: str | None = None
    segmentation_written: bool = False
    backup_path: str | None = None

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class HandoutSegment(BaseModel):
    """One segment of the stored segmentation, as its file header describes it."""

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


class SubjectHandout(BaseModel):
    """What a client receives when it asks for the next subject.

    ``has_image`` and ``has_segmentation`` say whether there is a file for this user to
    download: the server has it and the user's ``data_access`` includes it.
    """

    assignment_id: str
    dataset_id: int
    subject_id: int
    subject_key: str
    expires_at: str
    data_access: DataAccess = Field(DEFAULT_DATA_ACCESS, description="What this user is sent of each subject")
    has_image: bool
    has_segmentation: bool
    segmentation_labels: dict[str, int] = Field(
        default_factory=dict, description="Label name -> current label status in Subject_info (0, 1 or 2)"
    )
    label_values: dict[str, int] = Field(
        default_factory=dict, description="Label name -> BoneLabelMap voxel value, for the labels of this subject"
    )
    segments: list[HandoutSegment] = Field(
        default_factory=list,
        description="The segments of the stored segmentation, read from its header; empty unless has_segmentation",
    )
    stored_segmentation_issue: str | None = Field(
        None,
        description=(
            "Why the stored segmentation cannot be confirmed as it is (use_stored_segmentation), for "
            "instance because it is not on its image's voxel grid; None when it can. Only given with "
            "has_segmentation."
        ),
    )
    subject_info: dict = Field(default_factory=dict, description="The subject's entry from Subject_info_XXX.json")
    dataset_info: dict = Field(default_factory=dict, description="The dataset's Dataset_info_XXX.json")
    image_url: str | None = None
    segmentation_url: str | None = None


class SubmissionRequest(BaseModel):
    """Body of a submission, sent as the ``metadata`` part of a multipart request."""

    quality_check_confirmed: bool = Field(..., description="True sets the reviewed labels to status 2 (reviewed)")
    confirmed_labels: list[str] | None = Field(
        None,
        description=(
            "Labels the user vouches for. Defaults to every label found in the confirmed segmentation. "
            "Ignored when quality_check_confirmed is false."
        ),
    )
    use_stored_segmentation: bool = Field(
        False,
        description=(
            "Confirm the segmentation the server already holds, as it is, instead of uploading one: how "
            "a reviewer confirms, on the browser review page. Uploading a segmentation takes an editor. "
            "Ignored when quality_check_confirmed is false."
        ),
    )
    comment: str | None = Field(None, description="Free-form comment, kept in the audit log")

    model_config = ConfigDict(extra="forbid")


class SubmissionResult(BaseModel):
    """What the server did with a submission."""

    assignment_id: str
    subject_key: str
    quality_check_confirmed: bool
    state: AssignmentState
    segmentation_written: bool
    updated_labels: dict[str, int] = Field(default_factory=dict)
    removed_labels: list[str] = Field(default_factory=list)
    backup_path: str | None = None
    message: str = ""


class QueueStats(BaseModel):
    """Snapshot of the queue, for the admin panel."""

    total_subjects: int
    eligible_subjects: int
    available: int
    assigned: int
    assigned_by_other_servers: int = Field(0, description="Eligible subjects out for review on another server")
    confirmed: int
    rejected: int
    datasets: dict[int, int] = Field(default_factory=dict, description="dataset id -> eligible subject count")
    index_built_at: str | None = None

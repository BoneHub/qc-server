"""Pydantic models shared by the persisted server state and the REST API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AssignmentState = Literal["assigned", "confirmed", "rejected", "released", "expired"]

#: States in which a subject is finished and is never handed out again.
TERMINAL_STATES: set[str] = {"confirmed"}


class User(BaseModel):
    """A reviewer. The API key itself is never stored, only its HMAC digest."""

    name: str = Field(..., description="Unique reviewer name, used as the login identity")
    key_prefix: str = Field(..., description="First characters of the API key, shown in the admin panel")
    key_hash: str = Field(..., description="HMAC-SHA256 of the API key, keyed with the server private key")
    created_at: str = Field(..., description="ISO-8601 UTC timestamp")
    active: bool = Field(True, description="Disabled users are rejected at authentication")
    allowed_dataset_ids: list[int] | None = Field(
        None, description="Restrict this reviewer to these dataset ids. None means every dataset the server serves."
    )
    note: str = Field("", description="Free-form note for the administrator")

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    def public_dict(self) -> dict:
        """Everything about the user except the key digest."""
        return self.model_dump(exclude={"key_hash"})


class Assignment(BaseModel):
    """One subject handed to one reviewer."""

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


class SubjectHandout(BaseModel):
    """What a client receives when it asks for the next subject."""

    assignment_id: str
    dataset_id: int
    subject_id: int
    subject_key: str
    expires_at: str
    has_image: bool
    has_segmentation: bool
    segmentation_labels: dict[str, int] = Field(
        default_factory=dict, description="Label name -> current label status in Subject_info (0, 1 or 2)"
    )
    label_values: dict[str, int] = Field(
        default_factory=dict, description="Label name -> BoneLabelMap voxel value, for the labels of this subject"
    )
    subject_info: dict = Field(default_factory=dict, description="The subject's entry from Subject_info_XXX.json")
    dataset_info: dict = Field(default_factory=dict, description="The dataset's Dataset_info_XXX.json")
    image_url: str
    segmentation_url: str | None = None


class SubmissionRequest(BaseModel):
    """Body of a submission, sent as the ``metadata`` part of a multipart request."""

    quality_check_confirmed: bool = Field(..., description="True sets the reviewed labels to status 2 (reviewed)")
    confirmed_labels: list[str] | None = Field(
        None,
        description=(
            "Labels the reviewer vouches for. Defaults to every label found in the uploaded segmentation. "
            "Ignored when quality_check_confirmed is false."
        ),
    )
    comment: str | None = Field(None, description="Free-form reviewer comment, kept in the audit log")

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
    confirmed: int
    rejected: int
    datasets: dict[int, int] = Field(default_factory=dict, description="dataset id -> eligible subject count")
    index_built_at: str | None = None

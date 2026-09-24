"""Server configuration, and where the server keeps things.

Two places, on purpose:

* the credentials folder, inside the container (``BONEHUB_QC_CREDENTIALS_DIR``): the
  server's id, private key, admin key and user accounts. Never on the dataset share.
* ``<dataset_root>/<state_dir_name>/<server_id>/`` on the share: this server's policy
  (``config.json``), assignments, the verdicts and corrected segmentations waiting for the
  administrator's approval, logs and backups. One folder per server, so servers with
  different admins working on one dataset do not overwrite each other.

Any config field can be overridden at startup through an environment variable named
``BONEHUB_QC_<FIELD>``, which is how the Docker image is configured.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bonehub_data_schema import VALID_LABEL_VALUES, __version__ as SCHEMA_VERSION

ENV_PREFIX = "BONEHUB_QC_"
DEFAULT_STATE_DIR_NAME = ".bonehub_qc"
#: Inside the container; the Docker image and docker-compose.yml mount a volume here.
DEFAULT_CREDENTIALS_DIR = "/var/lib/bonehub-qc"
CONFIG_FILE_NAME = "config.json"

#: The schema whose label statuses and segmentation format this server implements. The
#: schema is installed from its repository's main branch, so a newer one must stop the
#: server rather than have its statuses read with this version's meaning.
WRITTEN_FOR_SCHEMA = "0.3"
if SCHEMA_VERSION.split(".")[:2] != WRITTEN_FOR_SCHEMA.split("."):
    raise ImportError(
        f"bonehub_data_schema {SCHEMA_VERSION} is installed, but this server is written for schema "
        f"{WRITTEN_FOR_SCHEMA}.x, whose label statuses and segmentation format may differ. Install the "
        "matching schema, or update the server."
    )

#: Label statuses in ``Subject_info_XXX.json``, as ``bonehub_data_schema`` defines them.
STATUS_NOT_AVAILABLE = 0
STATUS_NOT_REVIEWED = 1
STATUS_REVIEWED = 2

#: Statuses a label can have while it has a segmentation to look at.
REVIEWABLE_STATUSES = {STATUS_NOT_REVIEWED, STATUS_REVIEWED}


class QCServerConfig(BaseModel):
    """Policy that decides which subjects are handed out and what a submission does."""

    # --- queue policy -------------------------------------------------------
    eligible_label_values: list[int] = Field(
        default_factory=lambda: [STATUS_NOT_REVIEWED],
        description=(
            "A subject is queued for quality check when at least one of its segmentation "
            "labels has one of these statuses. Defaults to [1] = 'available, not reviewed or "
            "corrected'; add 2 to review already reviewed subjects again."
        ),
    )
    include_subjects_without_segmentation: bool = Field(
        False,
        description=(
            "Also queue subjects that have an image but no segmentation at all, so editors can create one "
            "in 3D Slicer. Reviewers are not handed them."
        ),
    )
    allowed_dataset_ids: list[int] | None = Field(
        None,
        description="Restrict the whole server to these dataset ids. None means every dataset under the root.",
    )

    # --- assignment policy --------------------------------------------------
    assignment_strategy: Literal["sequential", "random"] = Field(
        "sequential",
        description="'sequential' hands out the lowest free subject, 'random' picks a free subject at random.",
    )
    lease_ttl_seconds: int = Field(
        24 * 3600,
        ge=60,
        description="How long a user keeps a subject before it returns to the queue.",
    )
    max_concurrent_assignments_per_user: int = Field(
        1,
        ge=1,
        description="How many subjects a single user may hold at the same time.",
    )
    index_refresh_seconds: int = Field(
        300,
        ge=0,
        description="How often the subject index is rebuilt from disk. 0 rebuilds on every request.",
    )

    # --- submission policy --------------------------------------------------
    edits_need_review: bool = Field(
        True,
        description=(
            "Send the labels an editor corrected back to the reviewers. Off, the corrected labels the editor "
            "vouches for are accepted as they are and wait for the administrator's approval."
        ),
    )
    mark_removed_labels_absent: bool = Field(
        True,
        description=(
            "When an approved subject's segmentation no longer contains a label that used to be in it, "
            "set that label to 0 ('not available') instead of leaving it."
        ),
    )
    require_geometry_match: bool = Field(
        True,
        description="Reject a submitted segmentation whose voxel grid does not match the subject's image.",
    )
    keep_segmentation_backups: bool = Field(
        True,
        description="Copy the dataset's segmentation into this server's folder before an approval overwrites it.",
    )
    max_upload_bytes: int = Field(
        512 * 1024 * 1024,
        ge=1,
        description="Largest segmentation upload accepted from a client.",
    )

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("eligible_label_values")
    @classmethod
    def check_eligible_label_values(cls, values: list[int]) -> list[int]:
        if not values:
            raise ValueError("eligible_label_values must not be empty.")
        for value in values:
            if value not in REVIEWABLE_STATUSES:
                raise ValueError(
                    f"Invalid label status {value}. A subject can be queued on "
                    + ", ".join(f"{s} ('{VALID_LABEL_VALUES[s]}')" for s in sorted(REVIEWABLE_STATUSES))
                    + "."
                )
        return sorted(set(values))

    # --- persistence --------------------------------------------------------
    @classmethod
    def load(cls, config_path: Path) -> "QCServerConfig":
        """Read the config file if it exists, then apply ``BONEHUB_QC_*`` overrides."""
        data: dict = {}
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        data.update(cls._env_overrides())
        return cls(**data)

    def save(self, config_path: Path) -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = config_path.with_name(config_path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.model_dump(), f, indent=4)
        os.replace(tmp_path, config_path)

    @classmethod
    def _env_overrides(cls) -> dict:
        """Collect ``BONEHUB_QC_<FIELD>`` variables that match a config field."""
        overrides: dict = {}
        for name, field in cls.model_fields.items():
            raw = os.environ.get(f"{ENV_PREFIX}{name.upper()}")
            if raw is None or raw == "":
                continue
            overrides[name] = _parse_env_value(raw, field.annotation)
        return overrides


def _parse_env_value(raw: str, annotation) -> object:
    """Turn an environment string into the type the config field expects."""
    text = raw.strip()
    annotation_text = str(annotation)
    if "bool" in annotation_text:
        return text.lower() in {"1", "true", "yes", "on"}
    if "list" in annotation_text:
        if text.lower() in {"none", "null"}:
            return None
        items = [item.strip() for item in text.split(",") if item.strip()]
        return [int(item) for item in items] if "int" in annotation_text else items
    if "int" in annotation_text:
        return int(text)
    return text


def resolve_dataset_root() -> Path:
    """Dataset root the server operates on, from ``BONEHUB_QC_DATASET_ROOT``."""
    raw = os.environ.get(f"{ENV_PREFIX}DATASET_ROOT")
    if not raw:
        raise RuntimeError(
            f"{ENV_PREFIX}DATASET_ROOT is not set. Point it at a folder in BoneHub data structure format."
        )
    return Path(raw)


def resolve_state_root(dataset_root: Path) -> Path:
    """Folder inside the dataset root that holds one sub-folder per server."""
    name = os.environ.get(f"{ENV_PREFIX}STATE_DIR_NAME", DEFAULT_STATE_DIR_NAME)
    return dataset_root / name


def resolve_credentials_dir() -> Path:
    """Folder inside the container for the server's credentials, from ``BONEHUB_QC_CREDENTIALS_DIR``."""
    return Path(os.environ.get(f"{ENV_PREFIX}CREDENTIALS_DIR") or DEFAULT_CREDENTIALS_DIR)

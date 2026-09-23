"""Server configuration.

The configuration lives in ``<dataset_root>/<state_dir_name>/config.json`` so that a
dataset folder carries its own quality-check policy around with it. Any field can be
overridden at startup through an environment variable named ``BONEHUB_QC_<FIELD>``,
which is how the Docker image is configured.

The file records the ``bonehub_data_schema`` version it was written under, because label
statuses are part of the policy and their meaning changes between schema versions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bonehub_data_schema import VALID_LABEL_VALUES, __version__ as SCHEMA_VERSION, is_compatible_schema_version

ENV_PREFIX = "BONEHUB_QC_"
DEFAULT_STATE_DIR_NAME = ".bonehub_qc"
CONFIG_FILE_NAME = "config.json"
SCHEMA_VERSION_KEY = "schema_version"

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

#: Label statuses before schema 0.3, mapped to their meaning since: -1/0 not available,
#: 1 from the original source and 2 generated but unchecked are both "not reviewed", and
#: 3 "passed quality check" is "reviewed".
_PRE_0_3_STATUSES = {
    -1: STATUS_NOT_AVAILABLE,
    0: STATUS_NOT_AVAILABLE,
    1: STATUS_NOT_REVIEWED,
    2: STATUS_NOT_REVIEWED,
    3: STATUS_REVIEWED,
}


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
        description="Also queue subjects that have an image but no segmentation at all, so reviewers can create one.",
    )
    requeue_rejected: bool = Field(
        False,
        description="Hand a subject out again after a reviewer submitted it with quality_check_confirmed=false.",
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
        description="How long a reviewer keeps a subject before it returns to the queue.",
    )
    max_concurrent_assignments_per_user: int = Field(
        1,
        ge=1,
        description="How many subjects a single reviewer may hold at the same time.",
    )
    index_refresh_seconds: int = Field(
        300,
        ge=0,
        description="How often the subject index is rebuilt from disk. 0 rebuilds on every request.",
    )

    # --- submission policy --------------------------------------------------
    mark_removed_labels_absent: bool = Field(
        True,
        description=(
            "When a confirmed submission no longer contains a label that used to be in the "
            "segmentation, set that label to 0 ('not available') instead of leaving it."
        ),
    )
    require_geometry_match: bool = Field(
        True,
        description="Reject a submitted segmentation whose voxel grid does not match the subject's image.",
    )
    keep_segmentation_backups: bool = Field(
        True,
        description="Copy the previous segmentation into the state folder before overwriting it.",
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
    def load(cls, config_path: Path, notify: Callable[[str], None] | None = None) -> "QCServerConfig":
        """Read the config file if it exists, then apply ``BONEHUB_QC_*`` overrides.

        A file written under another schema version has its label statuses translated or
        reset, see :func:`_upgrade`; ``notify`` receives a line for each change.
        """
        data: dict = {}
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = _upgrade(data, notify or (lambda message: None))
        data.update(cls._env_overrides())
        return cls(**data)

    def save(self, config_path: Path) -> None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = config_path.with_name(config_path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({SCHEMA_VERSION_KEY: SCHEMA_VERSION, **self.model_dump()}, f, indent=4)
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


def _upgrade(data: dict, notify: Callable[[str], None]) -> dict:
    """Bring a stored config written under another schema version up to this one.

    A file without a version was written before schema 0.3, whose label statuses are
    translated. A file from any other version has its label statuses reset to the default,
    since there is no telling what they meant.
    """
    data = dict(data)
    version = data.pop(SCHEMA_VERSION_KEY, None)
    if is_compatible_schema_version(version):
        return data

    if "confirmed_label_value" in data:
        dropped = data.pop("confirmed_label_value")
        notify(
            f"config.json: dropped confirmed_label_value={dropped}; confirmed labels are always set to "
            f"{STATUS_REVIEWED} ('{VALID_LABEL_VALUES[STATUS_REVIEWED]}')."
        )
    if "eligible_label_values" in data:
        old = data["eligible_label_values"]
        if version is None:
            new = sorted({_PRE_0_3_STATUSES.get(value, STATUS_NOT_AVAILABLE) for value in old} & REVIEWABLE_STATUSES)
        else:
            new = []
        data["eligible_label_values"] = new or [STATUS_NOT_REVIEWED]
        notify(
            f"config.json was written under schema {version or 'older than 0.3'}: eligible_label_values "
            f"{old} is now {data['eligible_label_values']} in the label statuses of schema {SCHEMA_VERSION}."
        )
    return data


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


def resolve_state_dir(dataset_root: Path) -> Path:
    """Folder inside the dataset root where users, assignments, logs and backups live."""
    name = os.environ.get(f"{ENV_PREFIX}STATE_DIR_NAME", DEFAULT_STATE_DIR_NAME)
    return dataset_root / name

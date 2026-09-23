"""Fixtures shared by the tests: a throw-away dataset in BoneHub data structure format.

The tests never touch a real dataset. Every test case builds its own dataset root in a
temporary folder, which is what the server is pointed at::

    <root>/Dataset_001/Dataset_info_001.json
    <root>/Dataset_001/Subject_info_001.json
    <root>/Dataset_001/Image/001_000001.nii.gz
    <root>/Dataset_001/Segmentation/001_000001.seg.nrrd

Masks are written and read with ``bonehub_data_schema``'s own functions, so the fixtures are
in exactly the format the converters produce.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import SimpleITK as sitk

from bonehub_data_schema import (
    SEGMENTATION_SUFFIX,
    BoneLabelMap,
    __version__ as SCHEMA_VERSION,
    read_segmentation,
    write_segmentation,
)
from bonehub_data_schema.bonehub_dataset_io import DATASET_ZFILL, SUBJECT_ZFILL

from bonehub_quality_check_server.audit import AuditLog
from bonehub_quality_check_server.config import ENV_PREFIX, QCServerConfig
from bonehub_quality_check_server.store import QCStore


def _silence_audit_console() -> None:
    """Keep the audit trail's console echo out of the test report.

    ``AuditLog`` logs to a file and to the console. The file is what the tests assert
    against; the console copy would bury the unittest output, and the store logs during
    its own construction, so the handler is dropped as it is added rather than after.
    """
    original_init = AuditLog.__init__

    def quiet_init(self, dataset_root, state_dir):
        original_init(self, dataset_root, state_dir)
        for handler in list(self.logger.handlers):
            if type(handler) is logging.StreamHandler:  # a FileHandler is a subclass
                self.logger.removeHandler(handler)

    AuditLog.__init__ = quiet_init


_silence_audit_console()

#: Voxel grid used for every fixture image, as a numpy (z, y, x) shape. Small enough that
#: a suite run stays fast.
SHAPE = (6, 6, 6)
SPACING = (1.0, 1.0, 1.0)

LABEL_VALUE = {label.name: label.value for label in BoneLabelMap}


def subject_key(dataset_id: int, subject_id: int) -> str:
    return f"{str(dataset_id).zfill(DATASET_ZFILL)}_{str(subject_id).zfill(SUBJECT_ZFILL)}"


def clear_qc_env() -> None:
    """Drop every ``BONEHUB_QC_*`` variable so a developer's shell cannot steer a test."""
    for name in [key for key in os.environ if key.startswith(ENV_PREFIX)]:
        del os.environ[name]


def reference_image(shape=SHAPE, spacing=SPACING) -> sitk.Image:
    """An empty CT-like image on the fixture grid."""
    image = sitk.GetImageFromArray(np.zeros(shape, dtype=np.int16))
    image.SetSpacing(spacing)
    return image


def write_image(path: Path, shape=SHAPE, spacing=SPACING) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sitk.WriteImage(reference_image(shape, spacing), str(path))
    return path


def segmentation_array(labels, shape=SHAPE) -> np.ndarray:
    """A volume of BoneLabelMap values holding one small block per label."""
    data = np.zeros(shape, dtype=np.int32)
    for index, label in enumerate(sorted(labels)):
        if index >= shape[0]:
            raise ValueError(f"The fixture volume {shape} has room for at most {shape[0]} labels.")
        data[index, 0:2, 0:2] = LABEL_VALUE[label]
    return data


def write_mask(path: Path, labels, shape=SHAPE, spacing=SPACING) -> Path:
    """A ``.seg.nrrd`` holding the given labels, written the way the converters write one."""
    write_segmentation(segmentation_array(labels, shape), reference_image(shape, spacing), path)
    return path


def write_raw_mask(path: Path, numbers: np.ndarray, header: dict, spacing=SPACING) -> Path:
    """A ``.seg.nrrd`` with arbitrary segment numbers and header, for uploads that are wrong."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = sitk.GetImageFromArray(numbers)
    image.SetSpacing(spacing)
    for key, value in header.items():
        image.SetMetaData(key, str(value))
    sitk.WriteImage(image, str(path), useCompression=True)
    return path


def segment_header(*segments) -> dict:
    """Header entries for ``(number, name, tags)`` segments; ``tags`` may be ``None``."""
    header = {}
    for i, (number, name, tags) in enumerate(segments):
        header[f"Segment{i}_ID"] = name
        header[f"Segment{i}_Name"] = name
        header[f"Segment{i}_LabelValue"] = number
        header[f"Segment{i}_Layer"] = 0
        if tags is not None:
            header[f"Segment{i}_Tags"] = tags
    return header


def labels_in_mask(path: Path) -> set:
    """Label names actually painted into a mask, read back through the schema."""
    values = sitk.GetArrayViewFromImage(read_segmentation(path))
    return {BoneLabelMap(int(v)).name for v in np.unique(values) if int(v) != 0}


class DatasetBuilder:
    """Builds a dataset root the server can be pointed at."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._subjects: dict[int, list[dict]] = {}

    def add_dataset(
        self, dataset_id: int, name: str | None = None, schema_version: str | None = SCHEMA_VERSION
    ) -> "DatasetBuilder":
        padded = str(dataset_id).zfill(DATASET_ZFILL)
        dataset_dir = self.root / f"Dataset_{padded}"
        dataset_dir.mkdir(parents=True, exist_ok=True)
        info = {"dataset_id": dataset_id, "name": name or f"Test dataset {dataset_id}", "modality": "CT"}
        if schema_version is not None:
            info["schema_version"] = schema_version
        (dataset_dir / f"Dataset_info_{padded}.json").write_text(json.dumps(info, indent=4), encoding="utf-8")
        self._subjects.setdefault(dataset_id, [])
        self._write_subject_info(dataset_id)
        return self

    def add_subject(
        self,
        dataset_id: int,
        subject_id: int,
        segmentation: dict | None = None,
        image: bool = True,
        write_image_file: bool | None = None,
        write_segmentation_file: bool | None = None,
        **extra,
    ) -> "DatasetBuilder":
        """Add one subject, and by default the image and segmentation files that go with it.

        ``segmentation`` maps label names to label statuses (0, 1 or 2). The mask holds the
        labels with status 1 or 2. ``write_image_file`` / ``write_segmentation_file``
        default to what the metadata claims, so a test can create the mismatch of a
        subject whose file is missing.
        """
        if dataset_id not in self._subjects:
            self.add_dataset(dataset_id)

        entry = {"dataset_id": dataset_id, "subject_id": subject_id, "image": image}
        if segmentation is not None:
            entry["segmentation"] = dict(segmentation)
        entry.update(extra)
        self._subjects[dataset_id].append(entry)
        self._write_subject_info(dataset_id)

        if image if write_image_file is None else write_image_file:
            write_image(self.image_file(dataset_id, subject_id))

        present = sorted(label for label, status in (segmentation or {}).items() if status > 0)
        if bool(present) if write_segmentation_file is None else write_segmentation_file:
            write_mask(self.segmentation_file(dataset_id, subject_id), present)
        return self

    def _write_subject_info(self, dataset_id: int) -> None:
        padded = str(dataset_id).zfill(DATASET_ZFILL)
        path = self.root / f"Dataset_{padded}" / f"Subject_info_{padded}.json"
        path.write_text(json.dumps(self._subjects[dataset_id], indent=4), encoding="utf-8")

    # --- reading back, for assertions ---------------------------------------
    def subject_info(self, dataset_id: int, subject_id: int) -> dict:
        """The subject's entry as it stands on disk right now."""
        padded = str(dataset_id).zfill(DATASET_ZFILL)
        path = self.root / f"Dataset_{padded}" / f"Subject_info_{padded}.json"
        for entry in json.loads(path.read_text(encoding="utf-8")):
            if entry.get("subject_id") == subject_id:
                return entry
        raise AssertionError(f"Subject {subject_key(dataset_id, subject_id)} is not in {path}.")

    def all_subject_info(self, dataset_id: int) -> list:
        padded = str(dataset_id).zfill(DATASET_ZFILL)
        path = self.root / f"Dataset_{padded}" / f"Subject_info_{padded}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def segmentation_file(self, dataset_id: int, subject_id: int) -> Path:
        padded = str(dataset_id).zfill(DATASET_ZFILL)
        key = subject_key(dataset_id, subject_id)
        return self.root / f"Dataset_{padded}" / "Segmentation" / f"{key}{SEGMENTATION_SUFFIX}"

    def image_file(self, dataset_id: int, subject_id: int) -> Path:
        padded = str(dataset_id).zfill(DATASET_ZFILL)
        return self.root / f"Dataset_{padded}" / "Image" / f"{subject_key(dataset_id, subject_id)}.nii.gz"

    def dataset_log(self, dataset_id: int) -> Path:
        padded = str(dataset_id).zfill(DATASET_ZFILL)
        return self.root / f"Dataset_{padded}" / f"Dataset_{padded}_qualitycheck.log"

    def labels_in_segmentation(self, dataset_id: int, subject_id: int) -> set:
        """Label names actually painted into the stored segmentation file."""
        return labels_in_mask(self.segmentation_file(dataset_id, subject_id))


def close_logging(store) -> None:
    """Release the log files a store opened.

    The audit log keeps a ``FileHandler`` per dataset for the lifetime of the process,
    which on Windows keeps the file open and blocks the temporary folder from being
    removed. Tests therefore tear the handlers down explicitly.
    """
    if store is None:
        return
    for logger in [store.audit.logger, *store.audit._dataset_loggers.values()]:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - a handler that was already closed
                pass
        logging.Logger.manager.loggerDict.pop(logger.name, None)


class QCTestCase(unittest.TestCase):
    """Base case: a temporary dataset root, torn down completely afterwards."""

    def setUp(self) -> None:
        clear_qc_env()
        self._tmp = tempfile.mkdtemp(prefix="bonehub_qc_test_")
        self.tmp_path = Path(self._tmp)
        self.dataset_root = self.tmp_path / "BoneHub_Dataset"
        self.state_dir = self.dataset_root / ".bonehub_qc"
        self.builder = DatasetBuilder(self.dataset_root)
        self._stores: list = []
        self._upload_counter = itertools.count()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for store in self._stores:
            close_logging(store)
        clear_qc_env()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def track(self, store):
        """Register a store so its log files are closed during teardown."""
        self._stores.append(store)
        return store

    def make_store(self, config: QCServerConfig | None = None, **config_kwargs) -> QCStore:
        if config is None and config_kwargs:
            config = QCServerConfig(**config_kwargs)
        return self.track(QCStore(dataset_root=self.dataset_root, state_dir=self.state_dir, config=config))

    def default_dataset(self, n_subjects: int = 3, dataset_id: int = 1) -> None:
        """One dataset whose subjects all carry segmentations not reviewed yet (status 1)."""
        self.builder.add_dataset(dataset_id)
        for subject_id in range(1, n_subjects + 1):
            self.builder.add_subject(dataset_id, subject_id, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})

    def upload_path(self) -> Path:
        """A fresh path for an upload, outside the dataset folder."""
        return self.tmp_path / "uploads" / f"upload_{next(self._upload_counter)}{SEGMENTATION_SUFFIX}"

    def upload_file(self, labels, shape=SHAPE, spacing=SPACING) -> Path:
        """A segmentation as a client would submit it, written outside the dataset folder."""
        return write_mask(self.upload_path(), labels, shape, spacing)

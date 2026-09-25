"""Logging of everything the clients do, written into the dataset folder itself.

Three destinations, on purpose:

* ``<state_dir>/submissions.jsonl`` - append-only machine-readable record of every
  submission, which is what the admin panel reads back.
* ``<state_dir>/server.log`` - server lifecycle and administrative events.
* ``<dataset_root>/Dataset_XXX/Dataset_XXX_qualitycheck.log`` - a human-readable log
  next to the dataset it describes, in the same format the converter already writes.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from bonehub_data_schema.bonehub_dataset_io import DATASET_ZFILL

SUBMISSION_LOG_NAME = "submissions.jsonl"
SERVER_LOG_NAME = "server.log"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def utc_now_iso() -> str:
    """Timestamps are stored as UTC ISO-8601 with a trailing 'Z'."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class AuditLog:
    """Writes the client-facing audit trail. Safe to call from several threads."""

    def __init__(self, dataset_root: Path, state_dir: Path):
        self.dataset_root = dataset_root
        self.state_dir = state_dir
        self.submissions_path = state_dir / SUBMISSION_LOG_NAME
        self._lock = threading.Lock()
        self._dataset_loggers: dict[int, logging.Logger] = {}

        state_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(f"qc_server.{state_dir.resolve().as_posix()}")
        self.logger.propagate = False
        if not self.logger.handlers:
            handler = logging.FileHandler(state_dir / SERVER_LOG_NAME, encoding="utf-8")
            handler.setFormatter(logging.Formatter(LOG_FORMAT))
            self.logger.addHandler(handler)
            self.logger.addHandler(logging.StreamHandler())
            self.logger.setLevel(logging.INFO)

    # --- server / admin events ---------------------------------------------
    def event(self, message: str, level: int = logging.INFO) -> None:
        self.logger.log(level, message)

    # --- client events ------------------------------------------------------
    def record(self, kind: str, payload: dict, dataset_id: int | None = None, summary: str | None = None) -> dict:
        """Append one entry to the JSONL trail and mirror a readable line per dataset."""
        entry = {"timestamp": utc_now_iso(), "kind": kind, **payload}
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with open(self.submissions_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if dataset_id is not None:
                self._dataset_logger(dataset_id).info(summary or line)
        self.logger.info(summary or line)
        return entry

    def read_recent(self, limit: int = 100, kind: str | None = None) -> list[dict]:
        """Most recent entries first. The trail is small enough to scan from disk."""
        if not self.submissions_path.exists():
            return []
        with self._lock:
            with open(self.submissions_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        entries: list[dict] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kind and entry.get("kind") != kind:
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        return entries

    # --- internals ----------------------------------------------------------
    def _dataset_logger(self, dataset_id: int) -> logging.Logger:
        """One file handler per dataset, created lazily and kept for the process lifetime."""
        if dataset_id in self._dataset_loggers:
            return self._dataset_loggers[dataset_id]

        padded = str(dataset_id).zfill(DATASET_ZFILL)
        dataset_path = self.dataset_root / f"Dataset_{padded}"
        dataset_path.mkdir(parents=True, exist_ok=True)

        logger = logging.getLogger(f"{self.logger.name}.dataset.{padded}")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.FileHandler(dataset_path / f"Dataset_{padded}_qualitycheck.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter(LOG_FORMAT))
            logger.addHandler(handler)
        self._dataset_loggers[dataset_id] = logger
        return logger

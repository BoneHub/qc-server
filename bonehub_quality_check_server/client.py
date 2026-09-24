"""A reference client for the quality-check server, written against the standard library only.

This module deliberately avoids ``requests`` and every other third-party package: the same
file is shipped inside the 3D Slicer extension, where pip-installing into Slicer's bundled
Python is fragile. Keep it dependency-free.

    client = BoneHubQCClient("http://localhost:8000", "bhqc_...")
    handout = client.next_subject()
    client.download_image(handout["assignment_id"], Path("image.nii.gz"))
    client.download_segmentation(handout["assignment_id"], Path("segmentation.seg.nrrd"))
    client.submit(handout["assignment_id"], quality_check_confirmed=True,
                  segmentation_path=Path("reviewed.seg.nrrd"))

Segmentations go both ways in BoneHub's segmentation format (``.seg.nrrd``); the server
refuses an upload in any other format.

A client works in one role, which it names in every request: ``editor``, the role of 3D
Slicer, by default. The server refuses the key of an account that does not hold the role.
"""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DEFAULT_TIMEOUT = 300

#: The header in which a client names the role it works in.
ROLE_HEADER = "X-Client-Role"

#: The roles a client can work in: an editor corrects segmentations and uploads them, as
#: 3D Slicer does; a reviewer confirms or rejects them as they are, as the review page does.
EDITOR = "editor"
REVIEWER = "reviewer"


class QCClientError(RuntimeError):
    """An error reported by the server, or a transport failure."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BoneHubQCClient:
    """Thin wrapper over the server's REST API, working in one role."""

    def __init__(self, base_url: str, api_key: str, timeout: int = DEFAULT_TIMEOUT, role: str = EDITOR):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.role = role

    # --- endpoints ----------------------------------------------------------
    def ping(self) -> dict:
        """Check the server, the API key and its role. Raises ``QCClientError`` if any is wrong."""
        return self._request("GET", "/api/v1/ping")

    def labels(self) -> dict:
        """The BoneHub label map and label statuses: ``{'label_name_to_value': {...}, ...}``."""
        return self._request("GET", "/api/v1/labels")

    def next_subject(self) -> dict:
        """Lease the next subject. Raises with status 404 when the queue is empty."""
        return self._request("POST", "/api/v1/subjects/next")

    def my_assignments(self) -> list:
        return self._request("GET", "/api/v1/assignments")

    def assignment(self, assignment_id: str) -> dict:
        return self._request("GET", f"/api/v1/assignments/{assignment_id}")

    def download_image(self, assignment_id: str, destination: Path) -> Path:
        return self._download(f"/api/v1/assignments/{assignment_id}/image", destination)

    def download_segmentation(self, assignment_id: str, destination: Path) -> Path:
        """Download the stored segmentation. Raises with status 404 when there is none."""
        return self._download(f"/api/v1/assignments/{assignment_id}/segmentation", destination)

    def extend(self, assignment_id: str) -> dict:
        return self._request("POST", f"/api/v1/assignments/{assignment_id}/extend")

    def release(self, assignment_id: str) -> dict:
        return self._request("POST", f"/api/v1/assignments/{assignment_id}/release")

    def submit(
        self,
        assignment_id: str,
        quality_check_confirmed: bool,
        segmentation_path: Path | None = None,
        confirmed_labels: list[str] | None = None,
        comment: str | None = None,
    ) -> dict:
        """Send the verdict back.

        A confirmed submission must carry the reviewed segmentation; a rejection changes
        nothing in the dataset and needs no file.
        """
        if quality_check_confirmed and segmentation_path is None:
            raise QCClientError("A confirmed submission must include the reviewed segmentation file.")

        metadata = {
            "quality_check_confirmed": bool(quality_check_confirmed),
            "confirmed_labels": confirmed_labels,
            "comment": comment,
        }
        fields = {"metadata": json.dumps(metadata)}
        files = {}
        if segmentation_path is not None:
            path = Path(segmentation_path)
            if not path.exists():
                raise QCClientError(f"Segmentation file '{path}' does not exist.")
            files["segmentation"] = path
        body, content_type = _encode_multipart(fields, files)
        return self._request(
            "POST",
            f"/api/v1/assignments/{assignment_id}/submit",
            body=body,
            content_type=content_type,
        )

    # --- transport ----------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str | None = None,
    ):
        request = urllib.request.Request(self.base_url + path, data=body, method=method)
        request.add_header("X-API-Key", self.api_key)
        request.add_header(ROLE_HEADER, self.role)
        request.add_header("Accept", "application/json")
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise QCClientError(_error_detail(exc), status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise QCClientError(f"Could not reach {self.base_url}: {exc.reason}") from exc
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))

    def _download(self, path: str, destination: Path) -> Path:
        """Stream a file to disk, writing to a sibling temp file first."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = destination.with_name(destination.name + f".part-{uuid.uuid4().hex[:8]}")

        request = urllib.request.Request(self.base_url + path, method="GET")
        request.add_header("X-API-Key", self.api_key)
        request.add_header(ROLE_HEADER, self.role)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response, open(tmp_path, "wb") as f:
                shutil.copyfileobj(response, f, length=1024 * 1024)
        except urllib.error.HTTPError as exc:
            tmp_path.unlink(missing_ok=True)
            raise QCClientError(_error_detail(exc), status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            tmp_path.unlink(missing_ok=True)
            raise QCClientError(f"Could not reach {self.base_url}: {exc.reason}") from exc
        os.replace(tmp_path, destination)
        return destination


# --------------------------------------------------------------------- helpers
def _error_detail(exc: urllib.error.HTTPError) -> str:
    """Pull the server's ``detail`` message out of an error response when there is one."""
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except Exception:
        return f"HTTP {exc.code}: {exc.reason}"
    detail = payload.get("detail", payload)
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail)
    return f"HTTP {exc.code}: {detail}"


def _encode_multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    """Build a ``multipart/form-data`` body. Files are read whole; segmentations are small."""
    boundary = f"----BoneHubQC{uuid.uuid4().hex}"
    line_break = b"\r\n"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        chunks.append(b"")
        chunks.append(str(value).encode("utf-8"))

    for name, path in files.items():
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.append(f"--{boundary}".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"'.encode())
        chunks.append(f"Content-Type: {mime}".encode())
        chunks.append(b"")
        chunks.append(path.read_bytes())

    chunks.append(f"--{boundary}--".encode())
    chunks.append(b"")
    return line_break.join(chunks), f"multipart/form-data; boundary={boundary}"

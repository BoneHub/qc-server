"""Browser review page: look at a subject, tick the labels that are right, confirm or reject.

``/review`` is a single static page, like the admin panel. It asks for the reviewer's API key
once -- or takes it from an invite link, ``/review#key=bhqc_...`` -- keeps it in the browser,
and talks to the same ``/api/v1`` endpoints as the 3D Slicer extension. The images are
rendered in the reviewer's browser with NiiVue, so the server only sends files.

The page works in the reviewer role: every request it makes says so in ``X-Client-Role``,
and the server refuses the key of an account that is not a reviewer. It cannot edit a
segmentation: a reviewer confirms the stored one as it is (``use_stored_segmentation``) or
rejects it. Corrections are made in 3D Slicer, by an editor.

The page's script and NiiVue itself are served from ``/static``. NiiVue is vendored as one
pinned file (``static/vendor/``, see ``tools/update_niivue.py``), so the page works on a
network that cannot reach a CDN.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["review"])

STATIC_DIR = Path(__file__).parent / "static"

# Browsers refuse to run a module script served as anything but JavaScript, and on Windows
# Python's MIME table comes from the registry, where ".js" can be "text/plain".
mimetypes.add_type("text/javascript", ".js")

#: Only this server's own scripts run on the page; it holds the reviewer's key.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'wasm-unsafe-eval'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ]
)


@router.get("/review", include_in_schema=False)
@router.get("/review/", include_in_schema=False)
def review_page() -> FileResponse:
    """The page itself is public; everything it fetches needs the reviewer's key."""
    return FileResponse(
        STATIC_DIR / "review.html",
        media_type="text/html",
        headers={"Content-Security-Policy": CONTENT_SECURITY_POLICY, "Referrer-Policy": "no-referrer"},
    )

"""Admin panel: create users -- reviewers, editors or both -- hand out API keys and watch the queue.

Authentication is the server's admin key, sent as an ``X-Admin-Key`` header. The panel at
``/admin`` is a single static page that asks for the key once and keeps it in the
browser's session storage, so the server stores no sessions of its own.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse

from .models import DEFAULT_DATA_ACCESS, DEFAULT_ROLES, User
from .store import UNSET, QCError, QCStore

router = APIRouter(prefix="/admin", tags=["admin"])

STATIC_DIR = Path(__file__).parent / "static"


def get_store(request: Request) -> QCStore:
    store: QCStore | None = getattr(request.app.state, "store", None)
    if store is None:
        raise QCError("The server is not initialised.", status_code=503)
    return store


def require_admin(request: Request, x_admin_key: str | None = Header(None, alias="X-Admin-Key")) -> QCStore:
    store = get_store(request)
    if not store.is_admin_key(x_admin_key):
        raise QCError("Invalid or missing admin key. Send it in the 'X-Admin-Key' header.", status_code=401)
    return store


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def admin_panel() -> FileResponse:
    """The page itself is public; every action on it needs the admin key."""
    return FileResponse(STATIC_DIR / "admin.html", media_type="text/html")


@router.get("/api/session")
def check_session(store: QCStore = Depends(require_admin)) -> dict:
    """Used by the panel to validate the key the administrator typed in."""
    return {
        "status": "ok",
        "server_id": store.server_id,
        "dataset_root": str(store.dataset_root),
        "state_dir": str(store.state_dir),
        "config": store.config.model_dump(),
        "sessions": store.sessions(),
    }


@router.get("/api/users")
def list_users(store: QCStore = Depends(require_admin)) -> list[dict]:
    return store.list_users()


@router.post("/api/users")
def create_user(payload: dict, store: QCStore = Depends(require_admin)) -> dict:
    """Create a user. The plaintext API key is in the response and nowhere else.

    ``roles`` defaults to both, reviewer and editor.
    """
    name = str(payload.get("name", "")).strip()
    allowed = _parse_dataset_ids(payload.get("allowed_dataset_ids"))
    note = str(payload.get("note", "") or "")
    data_access = str(payload.get("data_access") or DEFAULT_DATA_ACCESS)
    roles = DEFAULT_ROLES if payload.get("roles") is None else _parse_roles(payload["roles"])
    user, api_key = store.create_user(
        name=name, allowed_dataset_ids=allowed, note=note, data_access=data_access, roles=roles
    )
    return {
        "user": user.public_dict(),
        "api_key": api_key,
        "warning": "This key is shown only once. Copy it now and give it to the user.",
    }


@router.patch("/api/users/{name}")
def update_user(name: str, payload: dict, store: QCStore = Depends(require_admin)) -> dict:
    """Change a user. A field absent from the body is left exactly as it was."""
    allowed = _parse_dataset_ids(payload["allowed_dataset_ids"]) if "allowed_dataset_ids" in payload else UNSET
    note = payload.get("note")
    data_access = payload.get("data_access")
    roles = payload.get("roles")
    user = store.update_user(
        name,
        allowed,
        None if note is None else str(note),
        data_access=None if data_access is None else str(data_access),
        roles=None if roles is None else _parse_roles(roles),
    )
    return user.public_dict()


@router.post("/api/users/{name}/rotate-key")
def rotate_key(name: str, store: QCStore = Depends(require_admin)) -> dict:
    api_key = store.rotate_user_key(name)
    return {
        "name": name,
        "api_key": api_key,
        "warning": "The previous key stopped working. This one is shown only once.",
    }


@router.post("/api/users/{name}/active")
def set_active(name: str, payload: dict, store: QCStore = Depends(require_admin)) -> dict:
    user: User = store.set_user_active(name, bool(payload.get("active", True)))
    return user.public_dict()


@router.delete("/api/users/{name}")
def delete_user(name: str, store: QCStore = Depends(require_admin)) -> dict:
    store.delete_user(name)
    return {"status": "deleted", "name": name}


@router.get("/api/stats")
def stats(store: QCStore = Depends(require_admin)) -> dict:
    return store.stats().model_dump()


@router.get("/api/assignments")
def assignments(limit: int = 200, state: str | None = None, store: QCStore = Depends(require_admin)) -> list[dict]:
    states = [s.strip() for s in state.split(",")] if state else None
    return store.all_assignments(limit=limit, states=states)


@router.post("/api/assignments/{assignment_id}/release")
def release(assignment_id: str, store: QCStore = Depends(require_admin)) -> dict:
    """Take a subject back from a user who is not going to finish it."""
    return store.release_assignment(assignment_id).model_dump()


@router.get("/api/submissions")
def submissions(limit: int = 100, kind: str | None = None, store: QCStore = Depends(require_admin)) -> list[dict]:
    return store.audit.read_recent(limit=limit, kind=kind)


@router.post("/api/refresh-index")
def refresh_index(store: QCStore = Depends(require_admin)) -> dict:
    """Re-scan the dataset folder, for when subjects were added outside the server."""
    store.refresh_index()
    return store.stats().model_dump()


@router.get("/api/config")
def get_config(store: QCStore = Depends(require_admin)) -> dict:
    return store.config.model_dump()


@router.put("/api/config")
def put_config(payload: dict, store: QCStore = Depends(require_admin)) -> dict:
    """Update the queue policy and rebuild the index so the change takes effect at once."""
    current = store.config.model_dump()
    unknown = [key for key in payload if key not in current]
    if unknown:
        raise QCError(f"Unknown configuration fields: {unknown}.")
    current.update(payload)
    try:
        store.config = type(store.config)(**current)
    except Exception as exc:
        raise QCError(f"Invalid configuration: {exc}") from exc
    store.config.save(store.config_path)
    store.refresh_index()
    store.audit.event(f"Configuration updated: {sorted(payload)}")
    return store.config.model_dump()


def _parse_dataset_ids(raw) -> list[int] | None:
    """Accept a list, a comma-separated string, or nothing at all."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        items = [item.strip() for item in raw.split(",") if item.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise QCError("allowed_dataset_ids must be a list of dataset ids or a comma-separated string.")
    if not items:
        return None
    try:
        return sorted({int(item) for item in items})
    except (TypeError, ValueError) as exc:
        raise QCError(f"allowed_dataset_ids must contain integers: {exc}") from exc


def _parse_roles(raw) -> list[str]:
    """Accept a list, or a comma-separated string; the store checks the roles themselves."""
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw]
    raise QCError("roles must be a list of roles or a comma-separated string: reviewer, editor, or both.")

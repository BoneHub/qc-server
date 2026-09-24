"""FastAPI application factory."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from bonehub_data_schema import __version__ as SCHEMA_VERSION

from . import __version__, admin, api, auth, review
from .config import QCServerConfig, resolve_credentials_dir, resolve_dataset_root, resolve_state_root
from .store import QCError, QCStore

#: How an administrator reaches the CLI of the running container.
EXEC_CLI = "docker compose exec bonehub-qc-server bonehub-qc-server"


def create_app(
    dataset_root: Path | None = None,
    credentials_dir: Path | None = None,
    config: QCServerConfig | None = None,
) -> FastAPI:
    """Build the application around one dataset root.

    ``dataset_root`` defaults to ``BONEHUB_QC_DATASET_ROOT`` and ``credentials_dir`` to
    ``BONEHUB_QC_CREDENTIALS_DIR``, so that ``uvicorn bonehub_quality_check_server.app:app``
    works inside the container with no arguments of its own.
    """
    dataset_root = Path(dataset_root) if dataset_root else resolve_dataset_root()
    credentials_dir = Path(credentials_dir) if credentials_dir else resolve_credentials_dir()

    app = FastAPI(
        title="BoneHub Dataset Quality Check",
        version=__version__,
        description=(
            "Distributes BoneHub subjects to reviewers -- in 3D Slicer or on the browser review "
            "page -- and writes confirmed segmentations back into the dataset folder."
        ),
    )
    app.state.store = QCStore(
        dataset_root=dataset_root,
        credentials_dir=credentials_dir,
        config=config,
        state_root=resolve_state_root(dataset_root),
    )
    app.state.store.mark_started()

    app.include_router(api.router)
    app.include_router(admin.router)
    app.include_router(review.router)
    app.mount("/static", StaticFiles(directory=review.STATIC_DIR), name="static")

    @app.exception_handler(QCError)
    async def handle_qc_error(request: Request, exc: QCError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/admin")

    @app.get("/health", tags=["server"])
    def health() -> dict:
        """Unauthenticated liveness probe, used by the container healthcheck."""
        return {
            "status": "ok",
            "version": __version__,
            "schema_version": SCHEMA_VERSION,
            "server_id": app.state.store.server_id,
            "dataset_root": str(app.state.store.dataset_root),
        }

    _announce(app.state.store)
    return app


def _announce(store: QCStore) -> None:
    """Print the startup banner, with the admin key when this server has just generated it.

    The banner goes to the container's output only. What is logged to the share leaves the
    key out.
    """
    stats = store.stats()
    details = [
        f"server id    : {store.server_id}" + (" (new server)" if store.server_created else ""),
        f"dataset root : {store.dataset_root}",
        f"state folder : {store.state_dir}",
        f"credentials  : {store.credentials_dir} (inside the container)",
        f"eligible     : {stats.eligible_subjects} of {stats.total_subjects} subjects "
        f"(label statuses {store.config.eligible_label_values})",
        f"reviewers    : {len(store.list_users())}",
        f"data schema  : {SCHEMA_VERSION}",
    ]
    lines = [
        "BoneHub Dataset Quality Check server",
        *(f"  {line}" for line in details),
        "  admin panel  : /admin",
        "  review page  : /review",
    ]

    if os.environ.get(auth.ENV_ADMIN_KEY):
        lines.append("  admin key    : BONEHUB_QC_ADMIN_KEY from .env")
    elif store.admin_key_generated:
        lines += [
            "",
            "  A new admin key was generated for this server:",
            f"      {store.admin_key}",
            f"  It is kept inside the container, in '{store.credentials_dir / auth.ADMIN_KEY_FILE_NAME}', and is",
            "  not printed again. To print it later:",
            f"      {EXEC_CLI} show-admin-key",
            "  To choose your own instead, set BONEHUB_QC_ADMIN_KEY in .env.",
        ]
    else:
        lines.append(f"  admin key    : kept inside the container; `{EXEC_CLI} show-admin-key` prints it")

    if store.credentials_on_share:
        lines += [
            "",
            "  WARNING: credentials of an older server are on the dataset share, where they are not safe:",
            *(f"      {path}" for path in store.credentials_on_share),
            "  This server does not use them. Delete them from the share.",
        ]
    print("\n".join(lines), flush=True)
    store.audit.event("Server started. " + " | ".join(details))


# Module-level `app` so `uvicorn bonehub_quality_check_server.app:app` works. It is built
# lazily through __getattr__ so that merely importing this module needs no environment.
def __getattr__(name: str):
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

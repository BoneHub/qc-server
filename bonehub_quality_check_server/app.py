"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from bonehub_data_schema import __version__ as SCHEMA_VERSION

from . import __version__, admin, api
from .config import QCServerConfig, resolve_dataset_root, resolve_state_dir
from .store import QCError, QCStore


def create_app(
    dataset_root: Path | None = None,
    state_dir: Path | None = None,
    config: QCServerConfig | None = None,
) -> FastAPI:
    """Build the application around one dataset root.

    ``dataset_root`` defaults to ``BONEHUB_QC_DATASET_ROOT`` so that
    ``uvicorn bonehub_quality_check_server.app:app`` works inside the container with no
    arguments of its own.
    """
    dataset_root = Path(dataset_root) if dataset_root else resolve_dataset_root()
    state_dir = Path(state_dir) if state_dir else resolve_state_dir(dataset_root)

    app = FastAPI(
        title="BoneHub Dataset Quality Check",
        version=__version__,
        description=(
            "Distributes BoneHub subjects to 3D Slicer reviewers and writes confirmed "
            "segmentations back into the dataset folder."
        ),
    )
    app.state.store = QCStore(dataset_root=dataset_root, state_dir=state_dir, config=config)

    app.include_router(api.router)
    app.include_router(admin.router)

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
            "dataset_root": str(app.state.store.dataset_root),
        }

    _announce(app.state.store)
    return app


def _announce(store: QCStore) -> None:
    """Print the startup banner, including the admin key when it was just generated."""
    stats = store.stats()
    lines = [
        "BoneHub Dataset Quality Check server",
        f"  dataset root : {store.dataset_root}",
        f"  state folder : {store.state_dir}",
        f"  eligible     : {stats.eligible_subjects} of {stats.total_subjects} subjects "
        f"(label statuses {store.config.eligible_label_values})",
        f"  reviewers    : {len(store.list_users())}",
        f"  data schema  : {SCHEMA_VERSION}",
        "  admin panel  : /admin",
    ]
    if store.admin_key_generated:
        lines += [
            "",
            "  A new admin key was generated for this dataset folder:",
            f"      {store.admin_key}",
            f"  It is stored in '{store.state_dir / 'admin_key'}'.",
            "  Set BONEHUB_QC_ADMIN_KEY to choose your own instead.",
        ]
    print("\n".join(lines), flush=True)
    store.audit.event("Server started. " + " | ".join(line.strip() for line in lines[1:6]))


# Module-level `app` so `uvicorn bonehub_quality_check_server.app:app` works. It is built
# lazily through __getattr__ so that merely importing this module needs no environment.
def __getattr__(name: str):
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

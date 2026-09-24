"""Command line entry point: the container's own command, and how it is administered.

The container runs ``bonehub-qc-server serve``. Everything else is run inside it, where the
dataset root and the credentials folder come from the environment:

    docker compose exec bonehub-qc-server bonehub-qc-server add-user --name alice
    docker compose exec bonehub-qc-server bonehub-qc-server add-user --name bob --roles editor
    docker compose exec bonehub-qc-server bonehub-qc-server show-admin-key
    docker compose exec bonehub-qc-server bonehub-qc-server sessions
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .config import ENV_PREFIX, QCServerConfig, resolve_credentials_dir, resolve_state_root
from .models import DATA_ACCESS_DESCRIPTIONS, DEFAULT_DATA_ACCESS, DEFAULT_ROLES, ROLES
from .store import QCStore


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=os.environ.get(f"{ENV_PREFIX}DATASET_ROOT"),
        help="Folder in BoneHub data structure format. Defaults to $BONEHUB_QC_DATASET_ROOT.",
    )
    parser.add_argument(
        "--credentials-dir",
        type=Path,
        default=None,
        help="Where the server keeps its credentials, inside the container. Defaults to $BONEHUB_QC_CREDENTIALS_DIR.",
    )


def _roles(text: str) -> list[str]:
    """``--roles reviewer,editor`` as a list, checked here so that a typo is a usage error."""
    roles = [role.strip() for role in text.split(",") if role.strip()]
    if not roles or any(role not in ROLES for role in roles):
        raise argparse.ArgumentTypeError(f"'{text}' is not one of: reviewer, editor, reviewer,editor")
    return roles


def _dataset_root(args: argparse.Namespace) -> Path:
    if not args.dataset_root:
        raise SystemExit("A dataset root is required: pass --dataset-root or set BONEHUB_QC_DATASET_ROOT.")
    return Path(args.dataset_root)


def _open_store(args: argparse.Namespace) -> QCStore:
    dataset_root = _dataset_root(args)
    return QCStore(
        dataset_root=dataset_root,
        credentials_dir=args.credentials_dir or resolve_credentials_dir(),
        state_root=resolve_state_root(dataset_root),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bonehub-qc-server", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the quality-check server (the container's command).")
    _add_common(serve)
    serve.add_argument("--host", default=os.environ.get(f"{ENV_PREFIX}HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get(f"{ENV_PREFIX}PORT", "8000")))
    serve.add_argument("--reload", action="store_true", help="Reload on code changes, for development.")

    add_user = subparsers.add_parser("add-user", help="Create a user and print its API key.")
    _add_common(add_user)
    add_user.add_argument("--name", required=True)
    add_user.add_argument(
        "--roles",
        type=_roles,
        default=list(DEFAULT_ROLES),
        help=(
            "What the user may do: reviewer (on the review page), editor (in 3D Slicer), or "
            "reviewer,editor for both (the default)."
        ),
    )
    add_user.add_argument("--datasets", default=None, help="Comma-separated dataset ids this user may see.")
    add_user.add_argument(
        "--data-access",
        choices=list(DATA_ACCESS_DESCRIPTIONS),
        default=DEFAULT_DATA_ACCESS,
        help="What the user is sent of each subject (default: %(default)s).",
    )
    add_user.add_argument("--note", default="")

    list_users = subparsers.add_parser("list-users", help="List users, their roles and their progress.")
    _add_common(list_users)

    rotate = subparsers.add_parser("rotate-key", help="Issue a new API key for a user.")
    _add_common(rotate)
    rotate.add_argument("--name", required=True)

    show_key = subparsers.add_parser("show-admin-key", help="Print this server's admin key.")
    _add_common(show_key)

    sessions = subparsers.add_parser("sessions", help="List the servers that have kept state in this dataset.")
    _add_common(sessions)

    stats = subparsers.add_parser("stats", help="Print queue statistics.")
    _add_common(stats)

    show_config = subparsers.add_parser("show-config", help="Print the effective server configuration.")
    _add_common(show_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "serve":
        import uvicorn

        from .app import create_app

        dataset_root = _dataset_root(args)
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(dataset_root)
        if args.credentials_dir:
            os.environ[f"{ENV_PREFIX}CREDENTIALS_DIR"] = str(args.credentials_dir)
        if args.reload:
            # The reloader needs an import string rather than a live application object.
            uvicorn.run("bonehub_quality_check_server.app:app", host=args.host, port=args.port, reload=True)
        else:
            app = create_app(dataset_root=dataset_root, credentials_dir=args.credentials_dir)
            uvicorn.run(app, host=args.host, port=args.port)
        return 0

    store = _open_store(args)

    if args.command == "add-user":
        datasets = [int(x) for x in args.datasets.split(",")] if args.datasets else None
        user, api_key = store.create_user(
            name=args.name,
            allowed_dataset_ids=datasets,
            note=args.note,
            data_access=args.data_access,
            roles=args.roles,
        )
        print(
            f"Created user '{user.name}', {' and '.join(user.roles)}, who is sent "
            f"{DATA_ACCESS_DESCRIPTIONS[user.data_access]}."
        )
        print(f"API key (shown once): {api_key}")
        return 0

    if args.command == "list-users":
        users = store.list_users()
        if not users:
            print("No users yet.")
            return 0
        print(
            f"{'name':<24}{'key':<16}{'active':<8}{'roles':<17}{'open':<6}{'reviewed':<10}{'edited':<8}"
            f"{'receives':<24}datasets"
        )
        for user in users:
            datasets = "all" if user["allowed_dataset_ids"] is None else ",".join(map(str, user["allowed_dataset_ids"]))
            print(
                f"{user['name']:<24}{user['key_prefix']:<16}{str(user['active']):<8}{','.join(user['roles']):<17}"
                f"{user['open']:<6}{user['reviewed']:<10}{user['edited']:<8}{user['data_access']:<24}{datasets}"
            )
        return 0

    if args.command == "rotate-key":
        print(f"New API key for '{args.name}' (shown once): {store.rotate_user_key(args.name)}")
        return 0

    if args.command == "show-admin-key":
        print(store.admin_key)
        return 0

    if args.command == "sessions":
        print(f"{'server id':<24}{'created':<23}{'last started':<23}{'host':<16}")
        for session in store.sessions():
            marker = "  <- this server" if session["this_server"] else ""
            print(
                f"{session.get('server_id') or '?':<24}{session.get('created_at') or '?':<23}"
                f"{session.get('last_started_at') or 'never':<23}{session.get('host') or '?':<16}{marker}"
            )
        return 0

    if args.command == "stats":
        for key, value in store.stats().model_dump().items():
            print(f"{key}: {value}")
        return 0

    if args.command == "show-config":
        for key, value in QCServerConfig.load(store.config_path).model_dump().items():
            print(f"{key}: {value}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

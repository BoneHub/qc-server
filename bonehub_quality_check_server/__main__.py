"""Command line entry point.

    bonehub-qc-server serve          --dataset-root Z:/BoneHub/BoneHub_Dataset
    bonehub-qc-server add-user       --dataset-root Z:/... --name alice
    bonehub-qc-server show-admin-key --dataset-root Z:/...

The same commands are available as ``python -m bonehub_quality_check_server <command>``.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .config import ENV_PREFIX, QCServerConfig, resolve_state_dir
from .store import QCStore


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=os.environ.get(f"{ENV_PREFIX}DATASET_ROOT"),
        help="Folder in BoneHub data structure format. Defaults to $BONEHUB_QC_DATASET_ROOT.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Where server state is kept. Defaults to '<dataset-root>/.bonehub_qc'.",
    )


def _open_store(args: argparse.Namespace) -> QCStore:
    if not args.dataset_root:
        raise SystemExit("A dataset root is required: pass --dataset-root or set BONEHUB_QC_DATASET_ROOT.")
    dataset_root = Path(args.dataset_root)
    state_dir = Path(args.state_dir) if args.state_dir else resolve_state_dir(dataset_root)
    return QCStore(dataset_root=dataset_root, state_dir=state_dir)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bonehub-qc-server", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the quality-check server.")
    _add_common(serve)
    serve.add_argument("--host", default=os.environ.get(f"{ENV_PREFIX}HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get(f"{ENV_PREFIX}PORT", "8000")))
    serve.add_argument("--reload", action="store_true", help="Reload on code changes, for development.")

    add_user = subparsers.add_parser("add-user", help="Create a reviewer and print its API key.")
    _add_common(add_user)
    add_user.add_argument("--name", required=True)
    add_user.add_argument("--datasets", default=None, help="Comma-separated dataset ids this reviewer may see.")
    add_user.add_argument("--note", default="")

    list_users = subparsers.add_parser("list-users", help="List reviewers and their progress.")
    _add_common(list_users)

    rotate = subparsers.add_parser("rotate-key", help="Issue a new API key for a reviewer.")
    _add_common(rotate)
    rotate.add_argument("--name", required=True)

    show_key = subparsers.add_parser("show-admin-key", help="Print the admin key for this dataset folder.")
    _add_common(show_key)

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

        if not args.dataset_root:
            raise SystemExit("A dataset root is required: pass --dataset-root or set BONEHUB_QC_DATASET_ROOT.")
        dataset_root = Path(args.dataset_root)
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(dataset_root)
        if args.reload:
            # The reloader needs an import string rather than a live application object.
            uvicorn.run("bonehub_quality_check_server.app:app", host=args.host, port=args.port, reload=True)
        else:
            app = create_app(dataset_root=dataset_root, state_dir=args.state_dir)
            uvicorn.run(app, host=args.host, port=args.port)
        return 0

    store = _open_store(args)

    if args.command == "add-user":
        datasets = [int(x) for x in args.datasets.split(",")] if args.datasets else None
        user, api_key = store.create_user(name=args.name, allowed_dataset_ids=datasets, note=args.note)
        print(f"Created reviewer '{user.name}'.")
        print(f"API key (shown once): {api_key}")
        return 0

    if args.command == "list-users":
        users = store.list_users()
        if not users:
            print("No reviewers yet.")
            return 0
        print(f"{'name':<24}{'key':<16}{'active':<8}{'open':<6}{'confirmed':<11}{'rejected':<10}datasets")
        for user in users:
            datasets = "all" if user["allowed_dataset_ids"] is None else ",".join(map(str, user["allowed_dataset_ids"]))
            print(
                f"{user['name']:<24}{user['key_prefix']:<16}{str(user['active']):<8}"
                f"{user['open']:<6}{user['confirmed']:<11}{user['rejected']:<10}{datasets}"
            )
        return 0

    if args.command == "rotate-key":
        print(f"New API key for '{args.name}' (shown once): {store.rotate_user_key(args.name)}")
        return 0

    if args.command == "show-admin-key":
        print(store.admin_key)
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

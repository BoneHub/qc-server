"""The container's start-up path: configuration by environment variable only.

Requirement under test: "it must serve as a docker application that runs a server on a
folder that is already in BoneHub data structure". Docker never passes command line
arguments, so everything must be reachable through ``BONEHUB_QC_*``.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import unittest
from pathlib import Path

from bonehub_quality_check_server.app import create_app
from bonehub_quality_check_server.config import ENV_PREFIX, QCServerConfig

from tests.support import QCTestCase, close_logging

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: ``BONEHUB_QC_*`` names that steer the process rather than the queue policy, so they
#: are deliberately absent from :class:`QCServerConfig`.
NON_CONFIG_ENV_NAMES = {
    "DATASET_ROOT",
    "STATE_DIR_NAME",
    "HOST",
    "PORT",
    "ADMIN_KEY",
    "PRIVATE_KEY",
}


class EnvironmentStartupTests(QCTestCase):
    def build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            app = create_app()
        self.track(app.state.store)
        return app

    def test_the_server_starts_from_the_environment_alone(self):
        self.default_dataset(n_subjects=2)
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        store = self.build().state.store
        self.assertEqual(store.dataset_root, self.dataset_root)
        self.assertEqual(store.stats().eligible_subjects, 2)

    def test_all_server_state_lives_inside_the_mounted_dataset(self):
        """The dataset folder is the only volume, so nothing may be kept outside it."""
        self.default_dataset()
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        store = self.build().state.store
        self.assertEqual(store.state_dir, self.dataset_root / ".bonehub_qc")
        self.assertTrue(store.state_dir.is_dir())

    def test_the_policy_is_taken_from_the_environment(self):
        for dataset_id in (1, 2, 3):
            self.builder.add_subject(dataset_id, 1, segmentation={"FEMUR_LEFT": 1})
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        os.environ[f"{ENV_PREFIX}ALLOWED_DATASET_IDS"] = "1,3"
        os.environ[f"{ENV_PREFIX}LEASE_TTL_SECONDS"] = "600"

        store = self.build().state.store
        self.assertEqual(store.config.allowed_dataset_ids, [1, 3])
        self.assertEqual(store.config.lease_ttl_seconds, 600)
        self.assertEqual(sorted(store.stats().datasets), [1, 3])

    def test_fixed_keys_survive_a_wiped_state_folder(self):
        """Set both keys in .env and a recreated container keeps working for everyone."""
        import shutil

        self.default_dataset()
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        os.environ[f"{ENV_PREFIX}PRIVATE_KEY"] = "fixed-private-key"
        os.environ[f"{ENV_PREFIX}ADMIN_KEY"] = "fixed-admin-key"

        store = self.build().state.store
        _, api_key = store.create_user("alice")
        users_file = (self.state_dir / "users.json").read_bytes()

        close_logging(store)  # the old container has stopped; release its open log files
        shutil.rmtree(self.state_dir)
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "users.json").write_bytes(users_file)

        restarted = self.build().state.store
        self.assertEqual(restarted.admin_key, "fixed-admin-key")
        self.assertEqual(restarted.authenticate(api_key).name, "alice")

    def test_a_missing_dataset_root_fails_loudly_at_startup(self):
        with self.assertRaises(RuntimeError):
            create_app()

    def test_the_state_folder_name_can_be_moved(self):
        self.default_dataset()
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        os.environ[f"{ENV_PREFIX}STATE_DIR_NAME"] = "_qc_state"
        store = self.build().state.store
        self.assertEqual(store.state_dir, self.dataset_root / "_qc_state")


class DeploymentFileTests(unittest.TestCase):
    """The shipped docker files must not drift from the configuration they steer."""

    def env_names(self, text: str) -> set:
        return {name for name in re.findall(rf"{ENV_PREFIX}([A-Z0-9_]+)", text)}

    def assert_names_are_real(self, path: Path) -> None:
        fields = set(QCServerConfig.model_fields)
        unknown = {
            name
            for name in self.env_names(path.read_text(encoding="utf-8"))
            if name not in NON_CONFIG_ENV_NAMES and name.lower() not in fields
        }
        self.assertEqual(unknown, set(), f"{path.name} sets variables that match no config field")

    def test_docker_compose_only_sets_variables_that_exist(self):
        self.assert_names_are_real(PROJECT_ROOT / "docker-compose.yml")

    def test_the_env_example_only_documents_variables_that_exist(self):
        self.assert_names_are_real(PROJECT_ROOT / ".env.example")

    def test_the_dockerfile_only_sets_variables_that_exist(self):
        self.assert_names_are_real(PROJECT_ROOT / "Dockerfile")

    def test_the_dataset_is_mounted_where_the_image_expects_it(self):
        compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("BONEHUB_QC_DATASET_ROOT: /data", compose)
        self.assertIn(":/data", compose)

    def test_the_image_runs_the_server_command(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('CMD ["bonehub-qc-server", "serve"]', dockerfile)
        self.assertIn("/health", dockerfile, "the healthcheck should poll the health endpoint")

    def test_the_package_exposes_that_command(self):
        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('bonehub-qc-server = "bonehub_quality_check_server.__main__:main"', pyproject)

    def test_the_state_folder_is_not_committed(self):
        gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", gitignore, "the filled-in .env holds a share password")


if __name__ == "__main__":
    unittest.main()

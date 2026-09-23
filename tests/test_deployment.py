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
    "CREDENTIALS_DIR",
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

    def test_state_goes_to_the_share_and_credentials_stay_in_the_container(self):
        """Two volumes: the dataset share for state, the credentials volume for secrets."""
        self.default_dataset()
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        store = self.build().state.store
        self.assertEqual(store.state_dir, self.dataset_root / ".bonehub_qc" / "qc_test_server")
        self.assertTrue(store.state_dir.is_dir())
        self.assertEqual(store.credentials_dir, self.credentials_dir)

    def test_credentials_inside_the_dataset_folder_are_refused(self):
        """A misconfigured volume must not quietly put the keys back on the share."""
        self.default_dataset()
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        os.environ[f"{ENV_PREFIX}CREDENTIALS_DIR"] = str(self.dataset_root / "keys")
        with self.assertRaises(RuntimeError) as ctx:
            self.build()
        self.assertIn("credentials", str(ctx.exception).lower())
        self.assertFalse((self.dataset_root / "keys").exists())

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

    def test_a_recreated_container_keeps_its_credentials_volume(self):
        """``docker compose up`` recreates the container but keeps the volume: same server."""
        self.default_dataset()
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        store = self.build().state.store
        _, api_key = store.create_user("alice")
        admin_key = store.admin_key

        close_logging(store)  # the old container has stopped; release its open log files
        restarted = self.build().state.store
        self.assertEqual(restarted.server_id, store.server_id)
        self.assertEqual(restarted.admin_key, admin_key)
        self.assertFalse(restarted.admin_key_generated, "the key is printed once, not on every start")
        self.assertEqual(restarted.authenticate(api_key).name, "alice")

    def test_wiping_the_share_state_loses_no_credentials(self):
        """The share holds nothing secret, so losing its state folder locks nobody out."""
        import shutil

        self.default_dataset()
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        store = self.build().state.store
        _, api_key = store.create_user("alice")

        close_logging(store)
        shutil.rmtree(self.state_root)
        restarted = self.build().state.store
        self.assertEqual(restarted.authenticate(api_key).name, "alice")

    def test_a_new_credentials_volume_is_a_new_server(self):
        """``docker compose down -v`` then ``up``: a new admin key, and a new state folder."""
        self.default_dataset()
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        old = self.build().state.store
        _, api_key = old.create_user("alice")

        close_logging(old)
        os.environ[f"{ENV_PREFIX}CREDENTIALS_DIR"] = str(self.tmp_path / "new_volume")
        new = self.build().state.store
        self.assertNotEqual(new.server_id, old.server_id)
        self.assertNotEqual(new.state_dir, old.state_dir)
        self.assertTrue(new.admin_key_generated)
        self.assertNotEqual(new.admin_key, old.admin_key)
        with self.assertRaises(Exception):
            new.authenticate(api_key)
        self.assertTrue((old.state_dir / "session.json").exists(), "the old server's history stays")

    def test_a_missing_dataset_root_fails_loudly_at_startup(self):
        with self.assertRaises(RuntimeError):
            create_app()

    def test_the_state_folder_name_can_be_moved(self):
        self.default_dataset()
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        os.environ[f"{ENV_PREFIX}STATE_DIR_NAME"] = "_qc_state"
        store = self.build().state.store
        self.assertEqual(store.state_dir, self.dataset_root / "_qc_state" / "qc_test_server")


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

    def test_the_credentials_have_a_volume_of_their_own(self):
        """Without it, recreating the container would silently make a new server."""
        compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('"bonehub-qc-credentials:/var/lib/bonehub-qc"', compose)
        self.assertIn("BONEHUB_QC_CREDENTIALS_DIR: /var/lib/bonehub-qc", compose)
        self.assertIn("BONEHUB_QC_CREDENTIALS_DIR=/var/lib/bonehub-qc", dockerfile)

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

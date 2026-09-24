"""The container's start-up path: configuration by environment variable only.

Requirement under test: "it must serve as a docker application that runs a server on a
folder that is already in BoneHub data structure". Docker never passes command line
arguments, so everything must be reachable through ``BONEHUB_QC_*``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
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
        self.assertIn('"credentials:/var/lib/bonehub-qc"', compose)
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

    def test_the_env_example_offers_a_local_folder_and_a_share_blank(self):
        """A share filled in by the example would demand SMB credentials for a local folder too."""
        env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        for name in ("BONEHUB_DATASET_PATH", "BONEHUB_DATASET_SHARE", "BONEHUB_SMB_USERNAME", "BONEHUB_SMB_PASSWORD"):
            self.assertRegex(env_example, rf"(?m)^{name}=$", f".env.example should offer {name}, blank")


class ComposeDatasetLocationTests(unittest.TestCase):
    """What ``docker compose`` makes of docker-compose.yml for each way .env gives the dataset.

    ``docker compose config`` only reads the files, so it needs the docker CLI but no running
    daemon. Skipped where there is none, as inside the server's own image.
    """

    SHARE = "//192.168.0.10/Data/BoneHub/BoneHub_Dataset"

    @classmethod
    def setUpClass(cls):
        cls.docker = shutil.which("docker")
        if not cls.docker or subprocess.run([cls.docker, "compose", "version"], capture_output=True).returncode:
            raise unittest.SkipTest("needs the docker CLI with Compose")

    def compose_config(self, **values: str) -> subprocess.CompletedProcess:
        """``docker compose config`` with .env.example, ``values`` filled in, as the .env."""
        text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        for name, value in values.items():
            text, count = re.subn(rf"(?m)^{name}=.*$", lambda _: f"{name}={value}", text)
            self.assertEqual(count, 1, f".env.example has no {name}= line")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env_file = tmp / ".env"
        env_file.write_text(text, encoding="utf-8")
        # The env file alone speaks: none of this shell's own BONEHUB_* or COMPOSE_* variables.
        environment = {k: v for k, v in os.environ.items() if not k.upper().startswith(("BONEHUB_", "COMPOSE_"))}
        command = [self.docker, "compose", "--project-directory", str(PROJECT_ROOT), "--env-file", str(env_file)]
        return subprocess.run(
            [*command, "config", "--format", "json"], capture_output=True, text=True, env=environment, timeout=120
        )

    def project(self, **values: str) -> dict:
        result = self.compose_config(**values)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def dataset_mount(self, project: dict) -> dict:
        return next(m for m in project["services"]["bonehub-qc-server"]["volumes"] if m["target"] == "/data")

    FOLDER = str(Path(tempfile.gettempdir()) / "BoneHub_Dataset")

    def test_a_local_folder_is_bind_mounted(self):
        project = self.project(BONEHUB_DATASET_PATH=self.FOLDER)
        mount = self.dataset_mount(project)
        self.assertEqual(mount["type"], "bind")
        self.assertEqual(Path(mount["source"]), Path(self.FOLDER))
        self.assertNotIn("dataset", project["volumes"], "no share volume is made for a local folder")

    def test_a_share_is_mounted_over_cifs(self):
        project = self.project(BONEHUB_DATASET_SHARE=self.SHARE, BONEHUB_SMB_USERNAME="alice", BONEHUB_SMB_PASSWORD="secret")
        mount = self.dataset_mount(project)
        self.assertEqual((mount["type"], mount["source"]), ("volume", "dataset"))
        options = project["volumes"]["dataset"]["driver_opts"]
        self.assertEqual((options["type"], options["device"]), ("cifs", self.SHARE))
        self.assertTrue(options["o"].startswith("username=alice,password=secret,"), options["o"])

    def test_a_server_without_a_name_is_bonehub_qc(self):
        project = self.project(BONEHUB_DATASET_PATH=self.FOLDER)
        self.assertEqual(project["name"], "bonehub_qc")
        self.assertEqual(project["volumes"]["credentials"]["name"], "bonehub_qc_credentials")

    def test_a_second_server_on_this_computer_shares_nothing_with_the_first(self):
        """Sharing the credentials volume would make it the first server running twice."""
        project = self.project(
            COMPOSE_PROJECT_NAME="bonehub_qc_2",
            BONEHUB_DATASET_SHARE=self.SHARE,
            BONEHUB_SMB_USERNAME="alice",
            BONEHUB_SMB_PASSWORD="secret",
        )
        self.assertEqual(project["name"], "bonehub_qc_2")
        names = {key: volume["name"] for key, volume in project["volumes"].items()}
        self.assertEqual(names, {"credentials": "bonehub_qc_2_credentials", "dataset": "bonehub_qc_2_dataset"})
        service = project["services"]["bonehub-qc-server"]
        self.assertNotIn("container_name", service, "a fixed container name would collide")
        self.assertNotIn("image", service, "a shared image would give one server the other's update")

    def test_no_dataset_location_stops_compose(self):
        result = self.compose_config()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BONEHUB_DATASET_PATH", result.stderr)
        self.assertIn("BONEHUB_DATASET_SHARE", result.stderr)

    def test_a_share_without_its_credentials_stops_compose(self):
        for given, missing in (("BONEHUB_SMB_PASSWORD", "BONEHUB_SMB_USERNAME"), ("BONEHUB_SMB_USERNAME", "BONEHUB_SMB_PASSWORD")):
            with self.subTest(missing=missing):
                result = self.compose_config(BONEHUB_DATASET_SHARE=self.SHARE, **{given: "x"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(missing, result.stderr)


if __name__ == "__main__":
    unittest.main()

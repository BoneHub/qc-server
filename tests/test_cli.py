"""The command line entry point, which is how the container is administered."""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest import mock

from bonehub_quality_check_server.__main__ import main
from bonehub_quality_check_server.config import ENV_PREFIX

from tests.support import QCTestCase


class CommandLineTests(QCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.default_dataset(n_subjects=2)

    def cli(self, command: str, *argv: str) -> str:
        """Run one command against the temporary dataset and return what it printed."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main([command, "--dataset-root", str(self.dataset_root), *argv])
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_add_user_prints_a_usable_key(self):
        output = self.cli("add-user", "--name", "alice")
        self.assertIn("alice", output)
        api_key = output.split("API key (shown once):")[1].strip()
        self.assertTrue(api_key.startswith("bhqc_"))

        store = self.make_store()
        self.assertEqual(store.authenticate(api_key).name, "alice")

    def test_add_user_can_scope_datasets_and_set_a_note(self):
        self.cli("add-user", "--name", "carol", "--datasets", "1,2", "--note", "knee")
        store = self.make_store()
        self.assertEqual(store._users["carol"].allowed_dataset_ids, [1, 2])
        self.assertEqual(store._users["carol"].note, "knee")

    def test_list_users_reports_each_reviewer(self):
        self.cli("add-user", "--name", "alice")
        self.cli("add-user", "--name", "bob")
        output = self.cli("list-users")
        self.assertIn("alice", output)
        self.assertIn("bob", output)

    def test_list_users_says_so_when_there_are_none(self):
        self.assertIn("No reviewers yet", self.cli("list-users"))

    def test_rotate_key_issues_a_working_replacement(self):
        old = self.cli("add-user", "--name", "alice").split("API key (shown once):")[1].strip()
        new = self.cli("rotate-key", "--name", "alice").split("(shown once):")[1].strip()
        self.assertNotEqual(old, new)

        store = self.make_store()
        self.assertEqual(store.authenticate(new).name, "alice")
        with self.assertRaises(Exception):
            store.authenticate(old)

    def test_show_admin_key_prints_the_key_kept_in_the_container(self):
        printed = self.cli("show-admin-key").strip()
        self.assertEqual(printed, (self.credentials_dir / "admin_key").read_text(encoding="utf-8").strip())

    def test_sessions_lists_the_servers_of_the_dataset(self):
        self.cli("stats")
        other = self.make_credentials_dir("other_credentials", "qc_other_server")
        self.cli("stats", "--credentials-dir", str(other))
        output = self.cli("sessions")
        self.assertIn("qc_other_server", output)
        this_server = next(line for line in output.splitlines() if "qc_test_server" in line)
        self.assertIn("<- this server", this_server)

    def test_the_credentials_folder_comes_from_the_environment(self):
        """Inside the container: BONEHUB_QC_CREDENTIALS_DIR, set by the image."""
        self.cli("add-user", "--name", "alice")
        self.assertTrue((self.credentials_dir / "users.json").exists())
        self.assertFalse((self.state_dir / "users.json").exists())

    def test_stats_prints_the_queue(self):
        output = self.cli("stats")
        self.assertIn("total_subjects: 2", output)
        self.assertIn("eligible_subjects: 2", output)

    def test_show_config_prints_the_policy(self):
        output = self.cli("show-config")
        self.assertIn("eligible_label_values: [1]", output)
        self.assertIn("mark_removed_labels_absent: True", output)

    def test_the_dataset_root_can_come_from_the_environment(self):
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(main(["stats"]), 0)
        self.assertIn("total_subjects: 2", buffer.getvalue())

    def test_a_missing_dataset_root_is_a_clear_error(self):
        with self.assertRaises(SystemExit):
            main(["stats"])

    def test_serve_builds_the_application_and_starts_uvicorn(self):
        with mock.patch("uvicorn.run") as run:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(["serve", "--dataset-root", str(self.dataset_root), "--host", "127.0.0.1", "--port", "9123"]),
                    0,
                )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(run.call_args.kwargs["port"], 9123)

    def test_serve_with_reload_passes_an_import_string(self):
        with mock.patch("uvicorn.run") as run:
            with contextlib.redirect_stdout(io.StringIO()):
                main(["serve", "--dataset-root", str(self.dataset_root), "--reload"])
        self.assertEqual(run.call_args.args[0], "bonehub_quality_check_server.app:app")
        self.assertTrue(run.call_args.kwargs["reload"])

    def test_an_unknown_command_is_rejected(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                main(["not-a-command"])

    def tearDown(self) -> None:
        # The CLI opens stores of its own; close every audit handler pointing at the
        # temporary folder so Windows lets it be deleted.
        import logging

        prefix = f"bonehub_quality_check_server.{self.state_root.resolve().as_posix()}"
        for name in [n for n in list(logging.Logger.manager.loggerDict) if n.startswith(prefix)]:
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            logging.Logger.manager.loggerDict.pop(name, None)
        super().tearDown()


if __name__ == "__main__":
    unittest.main()

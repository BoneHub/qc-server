"""Credentials kept off the share, and several servers on one dataset.

Requirements under test: "server private key and other credentials should not be saved on
the dataset location [...] the credentials must be saved inside the container", "once the
server is run, it must print out the admin key (if not provided by user in the .env
file)", and "it must keep track of different server sessions, because different servers
with different admins could be created and we don't want them to overwrite each other".
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from bonehub_data_schema import __version__ as SCHEMA_VERSION
from qc_server import auth
from qc_server.app import create_app
from qc_server.config import ENV_PREFIX
from qc_server.models import REVIEWER
from qc_server.store import QCError

from tests.support import QCTestCase, close_logging

SECRET_FILE_NAMES = ("server_id", "server_private_key", "admin_key", "users.json")


def start_server(test: QCTestCase, credentials_dir=None) -> tuple:
    """Start the application as the container does; return ``(store, what it printed)``."""
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        app = create_app(dataset_root=test.dataset_root, credentials_dir=credentials_dir or test.credentials_dir)
    return test.track(app.state.store), printed.getvalue()


def everything_on_the_share(test: QCTestCase) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="ignore") for path in test.dataset_root.rglob("*") if path.is_file()
    )


class CredentialsOffTheShareTests(QCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.default_dataset()

    def test_no_credential_file_ever_reaches_the_share(self):
        store = self.make_store()
        alice, _api_key = store.create_user("alice")
        self.review(store, alice, rejected={"FEMUR_RIGHT": "quality"})
        self.edit(store, alice, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"])
        store.create_user("bob")
        self.review(store, store._users["bob"])
        store.approve("001_000001")

        on_share = {path.name for path in self.dataset_root.rglob("*")}
        for name in SECRET_FILE_NAMES:
            self.assertNotIn(name, on_share)
            self.assertTrue((self.credentials_dir / name).exists(), name)

    def test_no_key_is_written_anywhere_on_the_share(self):
        store, _printed = start_server(self)
        _, api_key = store.create_user("alice")
        store.rotate_user_key("alice")
        text = everything_on_the_share(self)
        for secret in (store.admin_key, store.private_key, api_key):
            self.assertNotIn(secret, text)

    def test_a_generated_admin_key_is_printed_once(self):
        store, printed = start_server(self)
        self.assertIn(store.admin_key, printed)
        self.assertIn("show-admin-key", printed, "the banner says how to find it again")

        close_logging(store)
        restarted, printed_again = start_server(self)
        self.assertEqual(restarted.admin_key, store.admin_key)
        self.assertNotIn(store.admin_key, printed_again)
        self.assertIn("show-admin-key", printed_again)

    def test_an_admin_key_from_the_env_file_is_never_printed(self):
        os.environ[f"{ENV_PREFIX}ADMIN_KEY"] = "chosen-in-the-env-file"
        store, printed = start_server(self)
        self.assertEqual(store.admin_key, "chosen-in-the-env-file")
        self.assertNotIn("chosen-in-the-env-file", printed)
        self.assertIn("BONEHUB_QC_ADMIN_KEY", printed)


class ServerSessionTests(QCTestCase):
    """Two servers with their own admins on one dataset, as two containers would be."""

    def setUp(self) -> None:
        super().setUp()
        self.default_dataset(n_subjects=3)
        self.first = self.make_store(lease_ttl_seconds=3600, max_concurrent_assignments_per_user=5)
        second_credentials = self.make_credentials_dir("second_volume", "qc_second_server")
        self.second = self.make_store(credentials_dir=second_credentials, lease_ttl_seconds=600)
        self.alice = self.first.create_user("alice")[0]
        self.bob = self.second.create_user("bob")[0]

    def test_each_server_keeps_its_state_in_its_own_folder(self):
        self.assertEqual(self.first.state_dir.parent, self.second.state_dir.parent)
        self.assertNotEqual(self.first.state_dir, self.second.state_dir)
        self.first.next_subject(self.alice, REVIEWER)
        self.second.next_subject(self.bob, REVIEWER)

        def stored(store, name):
            return json.loads((store.state_dir / name).read_text(encoding="utf-8"))

        self.assertEqual(stored(self.first, "config.json")["lease_ttl_seconds"], 3600)
        self.assertEqual(stored(self.second, "config.json")["lease_ttl_seconds"], 600)
        self.assertEqual([a["user"] for a in stored(self.first, "assignments.json")], ["alice"])
        self.assertEqual([a["user"] for a in stored(self.second, "assignments.json")], ["bob"])

    def test_each_server_has_its_own_admin_and_reviewers(self):
        self.assertNotEqual(self.first.admin_key, self.second.admin_key)
        self.assertFalse(self.first.is_admin_key(self.second.admin_key))
        self.assertEqual([u["name"] for u in self.first.list_users()], ["alice"])
        self.assertEqual([u["name"] for u in self.second.list_users()], ["bob"])
        bob_key = self.second.rotate_user_key("bob")
        with self.assertRaises(QCError):
            self.first.authenticate(bob_key)

    def test_a_subject_out_on_one_server_is_not_handed_out_by_the_other(self):
        on_first = self.first.next_subject(self.alice, REVIEWER)
        on_second = self.second.next_subject(self.bob, REVIEWER)
        self.assertNotEqual(on_first.subject_key, on_second.subject_key)

        stats = self.second.stats()
        self.assertEqual((stats.assigned, stats.assigned_by_other_servers, stats.available), (1, 1, 1))

    def test_a_subject_given_back_on_one_server_is_free_for_the_other(self):
        leases = [self.first.next_subject(self.alice, REVIEWER) for _ in range(3)]
        with self.assertRaises(QCError) as ctx:
            self.second.next_subject(self.bob, REVIEWER)
        self.assertEqual(ctx.exception.status_code, 404)

        self.first.release_assignment(leases[1].assignment_id)
        self.assertEqual(self.second.next_subject(self.bob, REVIEWER).subject_key, leases[1].subject_key)

    def test_an_expired_lease_on_another_server_blocks_nothing(self):
        lease = self.first.next_subject(self.alice, REVIEWER)
        lease.expires_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
        self.first._save_assignments()
        self.assertEqual(self.second.next_subject(self.bob, REVIEWER).subject_key, lease.subject_key)

    def test_a_subject_in_progress_on_one_server_is_not_handed_out_by_the_other(self):
        """Its verdicts wait for the first server's administrator; the dataset does not show them yet."""
        self.review(self.first, self.alice, rejected={"FEMUR_RIGHT": "quality"})
        self.assertEqual(self.second.next_subject(self.bob, REVIEWER).subject_key, "001_000002")
        self.assertEqual(self.second.stats().assigned_by_other_servers, 1)

    def test_once_approved_there_it_is_done_here_too(self):
        self.review(self.first, self.alice)
        self.first.approve("001_000001")
        self.second.refresh_index()
        self.assertEqual(self.second.next_subject(self.bob, REVIEWER).subject_key, "001_000002")
        self.assertEqual(self.second.stats().eligible_subjects, 2, "the dataset says it is reviewed")

    def test_a_subject_closed_on_one_server_is_free_for_the_other(self):
        self.review(self.first, self.alice, rejected={"FEMUR_RIGHT": "quality"})
        self.first.close_case("001_000001")
        self.assertEqual(self.second.next_subject(self.bob, REVIEWER).subject_key, "001_000001")

    def test_a_broken_state_file_of_another_server_is_ignored(self):
        """Another server may be halfway through replacing its file."""
        (self.first.state_dir / "assignments.json").write_text("[{ truncated", encoding="utf-8")
        (self.first.state_dir / "cases.json").write_text("[{ truncated", encoding="utf-8")
        self.assertEqual(self.second.next_subject(self.bob, REVIEWER).subject_key, "001_000001")


class SessionRecordTests(QCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.default_dataset()

    def session(self) -> dict:
        return json.loads((self.state_dir / "session.json").read_text(encoding="utf-8"))

    def test_every_server_of_the_dataset_is_listed(self):
        store = self.make_store()
        self.make_store(credentials_dir=self.make_credentials_dir("second_volume", "qc_second_server"))
        sessions = {session["server_id"]: session for session in store.sessions()}
        self.assertEqual(sorted(sessions), ["qc_second_server", "qc_test_server"])
        self.assertTrue(sessions["qc_test_server"]["this_server"])
        self.assertFalse(sessions["qc_second_server"]["this_server"])

    def test_the_record_says_which_server_it_is_and_when_it_ran(self):
        store, _printed = start_server(self)
        session = self.session()
        self.assertEqual(session["server_id"], store.server_id)
        self.assertEqual(session["schema_version"], SCHEMA_VERSION)
        self.assertIsNotNone(session["last_started_at"])
        self.assertTrue(session["created_at"] <= session["last_started_at"])

    def test_the_cli_opening_the_server_state_is_not_a_start(self):
        """Admin commands run next to the server; they must not rewrite its start time."""
        self.make_store()
        self.assertIsNone(self.session()["last_started_at"])
        created = self.session()["created_at"]

        close_logging(self._stores[-1])
        start_server(self)
        self.assertIsNotNone(self.session()["last_started_at"])
        self.assertEqual(self.session()["created_at"], created)

    def test_the_server_id_is_the_folder_name_on_the_share(self):
        store = self.make_store()
        self.assertEqual(store.state_dir.name, (self.credentials_dir / auth.SERVER_ID_FILE_NAME).read_text().strip())


if __name__ == "__main__":
    unittest.main()

"""The server private key, and the API keys derived from it.

Requirement under test: "the server is created with a private key and there must be a
simple admin panel that lets creating users with api keys".
"""

from __future__ import annotations

import json
import os
import unittest

from qc_server import auth
from qc_server.models import REVIEWER
from qc_server.store import QCError

from tests.support import QCTestCase


class PrivateKeyTests(QCTestCase):
    def test_a_private_key_is_generated_and_reused(self):
        first = auth.load_or_create_private_key(self.credentials_dir)
        self.assertTrue(first)
        self.assertTrue((self.credentials_dir / auth.PRIVATE_KEY_FILE_NAME).exists())
        self.assertEqual(auth.load_or_create_private_key(self.credentials_dir), first)

    def test_two_servers_get_different_private_keys(self):
        other = self.tmp_path / "other_credentials"
        self.assertNotEqual(
            auth.load_or_create_private_key(self.credentials_dir),
            auth.load_or_create_private_key(other),
        )

    def test_the_environment_key_wins_over_the_stored_one(self):
        auth.load_or_create_private_key(self.credentials_dir)
        os.environ[auth.ENV_PRIVATE_KEY] = "a-fixed-private-key"
        self.assertEqual(auth.load_or_create_private_key(self.credentials_dir), "a-fixed-private-key")

    def test_an_admin_key_is_generated_once_and_then_reused(self):
        key, generated = auth.load_or_create_admin_key(self.credentials_dir)
        self.assertTrue(generated)
        again, generated_again = auth.load_or_create_admin_key(self.credentials_dir)
        self.assertEqual(again, key)
        self.assertFalse(generated_again)

    def test_an_admin_key_from_the_environment_is_not_reported_as_generated(self):
        os.environ[auth.ENV_ADMIN_KEY] = "chosen-by-the-administrator"
        key, generated = auth.load_or_create_admin_key(self.credentials_dir)
        self.assertEqual(key, "chosen-by-the-administrator")
        self.assertFalse(generated)
        self.assertFalse((self.credentials_dir / auth.ADMIN_KEY_FILE_NAME).exists(), "a chosen key is not stored")


class ServerIdTests(QCTestCase):
    def test_a_new_credentials_folder_is_a_new_server(self):
        server_id, generated = auth.load_or_create_server_id(self.tmp_path / "fresh")
        self.assertTrue(generated)
        self.assertTrue(server_id.startswith("qc_"))
        self.assertEqual(auth.load_or_create_server_id(self.tmp_path / "fresh"), (server_id, False))
        self.assertNotEqual(auth.load_or_create_server_id(self.tmp_path / "other")[0], server_id)

    def test_a_server_id_that_is_not_a_safe_folder_name_is_refused(self):
        """The id names a folder on the share, so it must not climb out of it."""
        folder = self.make_credentials_dir("tampered", "../../escape")
        with self.assertRaises(RuntimeError):
            auth.load_or_create_server_id(folder)


class ApiKeyTests(unittest.TestCase):
    def test_generated_keys_are_prefixed_and_unique(self):
        keys = {auth.generate_api_key() for _ in range(50)}
        self.assertEqual(len(keys), 50)
        self.assertTrue(all(key.startswith(auth.API_KEY_PREFIX) for key in keys))

    def test_the_digest_depends_on_the_server_private_key(self):
        api_key = auth.generate_api_key()
        self.assertNotEqual(auth.hash_api_key(api_key, "private-a"), auth.hash_api_key(api_key, "private-b"))

    def test_hashing_is_stable_and_comparison_works(self):
        api_key = auth.generate_api_key()
        digest = auth.hash_api_key(api_key, "private")
        self.assertTrue(auth.keys_match(auth.hash_api_key(api_key, "private"), digest))
        self.assertFalse(auth.keys_match(auth.hash_api_key("other", "private"), digest))

    def test_the_displayed_prefix_reveals_no_secret(self):
        api_key = auth.generate_api_key()
        prefix = auth.key_prefix(api_key)
        self.assertTrue(api_key.startswith(prefix))
        self.assertLess(len(prefix), len(api_key))


class UserAccountTests(QCTestCase):
    """Creating reviewers, and what is written to disk when we do."""

    def setUp(self) -> None:
        super().setUp()
        self.default_dataset()
        self.store = self.make_store()

    def test_creating_a_user_returns_a_key_that_is_never_stored_in_plaintext(self):
        user, api_key = self.store.create_user("alice")
        self.assertEqual(user.name, "alice")
        self.assertTrue(api_key.startswith(auth.API_KEY_PREFIX))

        raw = (self.credentials_dir / "users.json").read_text(encoding="utf-8")
        self.assertNotIn(api_key, raw)
        self.assertIn(user.key_hash, raw)

    def test_the_public_view_of_a_user_hides_the_digest(self):
        user, _ = self.store.create_user("alice")
        self.assertNotIn("key_hash", user.public_dict())
        self.assertIn("key_prefix", user.public_dict())

    def test_each_client_gets_its_own_name_and_key(self):
        _, alice_key = self.store.create_user("alice")
        _, bob_key = self.store.create_user("bob")
        self.assertNotEqual(alice_key, bob_key)
        self.assertEqual(self.store.authenticate(alice_key).name, "alice")
        self.assertEqual(self.store.authenticate(bob_key).name, "bob")

    def test_duplicate_names_are_refused(self):
        self.store.create_user("alice")
        with self.assertRaises(QCError) as ctx:
            self.store.create_user("alice")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_a_blank_name_is_refused(self):
        with self.assertRaises(QCError):
            self.store.create_user("   ")

    def test_an_unknown_key_is_rejected(self):
        with self.assertRaises(QCError) as ctx:
            self.store.authenticate("bhqc_not-a-real-key")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_a_missing_key_is_rejected(self):
        with self.assertRaises(QCError) as ctx:
            self.store.authenticate(None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_a_disabled_user_cannot_authenticate(self):
        _, api_key = self.store.create_user("alice")
        self.store.set_user_active("alice", False)
        with self.assertRaises(QCError) as ctx:
            self.store.authenticate(api_key)
        self.assertEqual(ctx.exception.status_code, 403)
        self.store.set_user_active("alice", True)
        self.assertEqual(self.store.authenticate(api_key).name, "alice")

    def test_rotating_a_key_invalidates_the_previous_one(self):
        _, old_key = self.store.create_user("alice")
        new_key = self.store.rotate_user_key("alice")
        self.assertNotEqual(old_key, new_key)
        self.assertEqual(self.store.authenticate(new_key).name, "alice")
        with self.assertRaises(QCError):
            self.store.authenticate(old_key)

    def test_deleting_a_user_revokes_the_key_and_frees_their_subject(self):
        _, api_key = self.store.create_user("alice")
        user = self.store.authenticate(api_key)
        assignment = self.store.next_subject(user, REVIEWER)

        self.store.delete_user("alice")
        with self.assertRaises(QCError):
            self.store.authenticate(api_key)
        self.assertEqual(self.store.get_assignment(assignment.assignment_id).state, "released")

    def test_operations_on_an_unknown_user_are_a_404(self):
        for call in (
            lambda: self.store.rotate_user_key("nobody"),
            lambda: self.store.set_user_active("nobody", True),
            lambda: self.store.delete_user("nobody"),
        ):
            with self.assertRaises(QCError) as ctx:
                call()
            self.assertEqual(ctx.exception.status_code, 404)

    def test_users_survive_a_server_restart(self):
        _, api_key = self.store.create_user("alice", allowed_dataset_ids=[1], note="knee study")
        reopened = self.make_store()
        user = reopened.authenticate(api_key)
        self.assertEqual(user.name, "alice")
        self.assertEqual(user.allowed_dataset_ids, [1])
        self.assertEqual(user.note, "knee study")

    def test_updating_only_the_note_leaves_the_dataset_restriction_alone(self):
        """A partial edit must not quietly widen a reviewer's access."""
        self.store.create_user("alice", allowed_dataset_ids=[1])
        updated = self.store.update_user("alice", note="on leave")
        self.assertEqual(updated.allowed_dataset_ids, [1])
        self.assertEqual(updated.note, "on leave")

    def test_the_restriction_can_be_cleared_explicitly(self):
        self.store.create_user("alice", allowed_dataset_ids=[1])
        self.assertIsNone(self.store.update_user("alice", None).allowed_dataset_ids)

    def test_the_admin_key_is_recognised_and_nothing_else_is(self):
        self.assertTrue(self.store.is_admin_key(self.store.admin_key))
        self.assertFalse(self.store.is_admin_key("wrong"))
        self.assertFalse(self.store.is_admin_key(None))
        self.assertFalse(self.store.is_admin_key(""))

    def test_a_reviewer_key_is_not_an_admin_key(self):
        _, api_key = self.store.create_user("alice")
        self.assertFalse(self.store.is_admin_key(api_key))

    def test_listing_users_reports_their_progress(self):
        self.store.create_user("alice")
        self.store.create_user("bob")
        listed = self.store.list_users()
        self.assertEqual([u["name"] for u in listed], ["alice", "bob"])
        for entry in listed:
            self.assertEqual((entry["open"], entry["reviewed"], entry["edited"]), (0, 0, 0))
            self.assertNotIn("key_hash", entry)

    def test_changing_the_private_key_invalidates_every_issued_key(self):
        """Keys are digests under the private key, so rotating it locks everyone out."""
        _, api_key = self.store.create_user("alice")
        os.environ[auth.ENV_PRIVATE_KEY] = "a-brand-new-server-private-key"
        reopened = self.make_store()
        with self.assertRaises(QCError):
            reopened.authenticate(api_key)

    def test_the_users_file_is_valid_json_on_disk(self):
        self.store.create_user("alice")
        entries = json.loads((self.credentials_dir / "users.json").read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "alice")


class AccountsChangedByAnotherProcessTests(QCTestCase):
    """The CLI runs in the container next to the live server, on the same credentials."""

    def setUp(self) -> None:
        super().setUp()
        self.default_dataset()
        self.server = self.make_store()
        self.cli = self.make_store()  # a second process on the same credentials folder

    def test_a_reviewer_added_by_the_cli_can_log_in_at_once(self):
        _, api_key = self.cli.create_user("alice")
        self.assertEqual(self.server.authenticate(api_key).name, "alice")

    def test_the_server_does_not_erase_a_reviewer_the_cli_added(self):
        _, alice_key = self.cli.create_user("alice")
        self.server.create_user("bob")
        self.assertEqual(sorted(u["name"] for u in self.make_store().list_users()), ["alice", "bob"])
        self.assertEqual(self.server.authenticate(alice_key).name, "alice")

    def test_a_key_rotated_by_the_cli_stops_working_on_the_server(self):
        _, old_key = self.server.create_user("alice")
        self.cli.rotate_user_key("alice")
        with self.assertRaises(QCError):
            self.server.authenticate(old_key)


if __name__ == "__main__":
    unittest.main()

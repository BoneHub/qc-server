"""What a reviewer is sent of each subject: the image, the segmentation, or both.

Requirement under test: "an option in the admin panel for the reviewers to say what data they
will receive for each subject, only segmentations? only images? or both". The option is the
account's ``data_access``. A file a reviewer is not sent is left out of the handout and refused
at its download endpoint, so the restriction holds for any client, not only the review page.
"""

from __future__ import annotations

import contextlib
import io
import unittest

from bonehub_quality_check_server.__main__ import main
from bonehub_quality_check_server.config import QCServerConfig
from bonehub_quality_check_server.models import EDITOR, REVIEWER, User
from bonehub_quality_check_server.store import QCError

from tests.support import QCTestCase
from tests.test_api import ApiTestCase


class AccountTests(QCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.default_dataset(n_subjects=1)
        self.store = self.make_store()

    def test_a_reviewer_is_sent_both_by_default(self):
        user, _ = self.store.create_user("alice")
        self.assertEqual(user.data_access, "image_and_segmentation")
        self.assertTrue(user.receives_image)
        self.assertTrue(user.receives_segmentation)

    def test_each_setting_says_what_is_sent(self):
        expected = {
            "image_and_segmentation": (True, True),
            "segmentation": (False, True),
            "image": (True, False),
        }
        for access, (image, segmentation) in expected.items():
            user, _ = self.store.create_user(f"user_{access}", data_access=access)
            self.assertEqual((user.receives_image, user.receives_segmentation), (image, segmentation), access)

    def test_an_unknown_setting_is_refused(self):
        with self.assertRaises(QCError) as ctx:
            self.store.create_user("alice", data_access="everything")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertNotIn("alice", [u["name"] for u in self.store.list_users()])

    def test_the_setting_can_be_changed_and_is_kept(self):
        self.store.create_user("alice")
        self.store.update_user("alice", data_access="segmentation")
        restarted = self.make_store()
        self.assertEqual(restarted._require_user("alice").data_access, "segmentation")

    def test_changing_something_else_leaves_the_setting_alone(self):
        self.store.create_user("alice", data_access="image")
        self.store.update_user("alice", note="on leave")
        self.assertEqual(self.store._require_user("alice").data_access, "image")

    def test_an_invalid_change_is_refused_and_changes_nothing(self):
        self.store.create_user("alice", data_access="image")
        with self.assertRaises(QCError):
            self.store.update_user("alice", data_access="all of it")
        self.assertEqual(self.store._require_user("alice").data_access, "image")

    def test_the_setting_is_in_the_public_view_of_a_user(self):
        user, _ = self.store.create_user("alice", data_access="segmentation")
        self.assertEqual(user.public_dict()["data_access"], "segmentation")
        self.assertIsInstance(user, User)


class HandoutAndDownloadTests(ApiTestCase):
    """What each kind of account is told about a subject, and may download."""

    def setUp(self) -> None:
        super().setUp()
        self.sam_key = self.create_client_user("sam", data_access="segmentation")
        self.ian_key = self.create_client_user("ian", data_access="image")

    def download(self, key: str, handout: dict, what: str):
        return self.client.get(f"/api/v1/assignments/{handout['assignment_id']}/{what}", headers=self.headers(key))

    def test_a_reviewer_sent_both_gets_both(self):
        handout = self.next_subject(self.alice_key)
        self.assertEqual(handout["data_access"], "image_and_segmentation")
        self.assertTrue(handout["has_image"] and handout["has_segmentation"])
        self.assertEqual(self.download(self.alice_key, handout, "image").status_code, 200)
        self.assertEqual(self.download(self.alice_key, handout, "segmentation").status_code, 200)

    def test_a_reviewer_sent_the_segmentation_only_gets_no_image(self):
        handout = self.next_subject(self.sam_key)
        self.assertEqual(handout["data_access"], "segmentation")
        self.assertFalse(handout["has_image"])
        self.assertIsNone(handout["image_url"])
        self.assertTrue(handout["has_segmentation"])
        self.assertTrue(handout["segments"], "the segment table comes with the segmentation")

        refused = self.download(self.sam_key, handout, "image")
        self.assertEqual(refused.status_code, 403)
        self.assertIn("segmentation only", refused.json()["detail"])
        self.assertEqual(self.download(self.sam_key, handout, "segmentation").status_code, 200)

    def test_a_reviewer_sent_the_image_only_gets_no_segmentation(self):
        handout = self.next_subject(self.ian_key)
        self.assertEqual(handout["data_access"], "image")
        self.assertTrue(handout["has_image"])
        self.assertFalse(handout["has_segmentation"])
        self.assertIsNone(handout["segmentation_url"])
        self.assertEqual(handout["segments"], [])
        self.assertIsNone(handout["stored_segmentation_issue"])

        refused = self.download(self.ian_key, handout, "segmentation")
        self.assertEqual(refused.status_code, 403)
        self.assertIn("image only", refused.json()["detail"])
        self.assertEqual(self.download(self.ian_key, handout, "image").status_code, 200)

    def test_what_subject_info_says_is_sent_to_everyone(self):
        """The label statuses are metadata, not the segmentation itself."""
        handout = self.next_subject(self.ian_key)
        self.assertEqual(handout["segmentation_labels"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})

    def test_a_change_takes_effect_on_a_subject_already_held(self):
        handout = self.next_subject(self.alice_key)
        self.client.patch("/admin/api/users/alice", json={"data_access": "segmentation"}, headers=self.admin_headers)
        self.assertEqual(self.download(self.alice_key, handout, "image").status_code, 403)
        again = self.client.get(f"/api/v1/assignments/{handout['assignment_id']}", headers=self.headers(self.alice_key))
        self.assertFalse(again.json()["has_image"])

    def test_ping_reports_the_setting(self):
        body = self.client.get("/api/v1/ping", headers=self.headers(self.sam_key)).json()
        self.assertEqual(body["data_access"], "segmentation")
        self.assertIn("mark_removed_labels_absent", body)


class QueueTests(QCTestCase):
    def test_a_user_sent_the_segmentation_only_skips_subjects_without_one(self):
        """They would have nothing at all to look at."""
        self.builder.add_subject(1, 1, segmentation=None)
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1})
        store = self.make_store(QCServerConfig(include_subjects_without_segmentation=True))
        sam, _ = store.create_user("sam", data_access="segmentation")
        alice, _ = store.create_user("alice")

        self.assertEqual(store.next_subject(sam, REVIEWER).subject_id, 2)
        self.assertEqual(store.next_subject(alice, EDITOR).subject_id, 1)

    def test_with_only_such_subjects_left_the_queue_is_empty_for_them(self):
        self.builder.add_subject(1, 1, segmentation=None)
        store = self.make_store(QCServerConfig(include_subjects_without_segmentation=True))
        sam, _ = store.create_user("sam", data_access="segmentation")
        for role in (REVIEWER, EDITOR):
            with self.assertRaises(QCError) as ctx:
                store.next_subject(sam, role)
            self.assertEqual(ctx.exception.status_code, 404, role)


class AdminPanelTests(ApiTestCase):
    def test_the_setting_is_chosen_when_a_reviewer_is_created(self):
        body = self.client.post(
            "/admin/api/users", json={"name": "carol", "data_access": "image"}, headers=self.admin_headers
        ).json()
        self.assertEqual(body["user"]["data_access"], "image")

    def test_a_reviewer_created_without_it_is_sent_both(self):
        body = self.client.post("/admin/api/users", json={"name": "carol"}, headers=self.admin_headers).json()
        self.assertEqual(body["user"]["data_access"], "image_and_segmentation")

    def test_the_setting_can_be_changed_from_the_panel(self):
        body = self.client.patch(
            "/admin/api/users/alice", json={"data_access": "segmentation"}, headers=self.admin_headers
        ).json()
        self.assertEqual(body["data_access"], "segmentation")
        listed = self.client.get("/admin/api/users", headers=self.admin_headers).json()
        self.assertEqual(next(u for u in listed if u["name"] == "alice")["data_access"], "segmentation")

    def test_an_unknown_setting_is_a_400(self):
        created = self.client.post(
            "/admin/api/users", json={"name": "carol", "data_access": "x"}, headers=self.admin_headers
        )
        self.assertEqual(created.status_code, 400)
        patched = self.client.patch("/admin/api/users/alice", json={"data_access": "x"}, headers=self.admin_headers)
        self.assertEqual(patched.status_code, 400)

    def test_the_change_is_in_the_audit_trail(self):
        self.client.patch("/admin/api/users/alice", json={"data_access": "image"}, headers=self.admin_headers)
        entries = self.client.get("/admin/api/submissions?kind=user_updated", headers=self.admin_headers).json()
        self.assertEqual(entries[0]["data_access"], "image")


class CommandLineTests(QCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.default_dataset(n_subjects=1)

    def cli(self, command: str, *argv: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main([command, "--dataset-root", str(self.dataset_root), *argv])
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_add_user_takes_the_setting_and_list_users_shows_it(self):
        self.assertIn("segmentation only", self.cli("add-user", "--name", "sam", "--data-access", "segmentation"))
        self.assertEqual(self.make_store()._users["sam"].data_access, "segmentation")
        self.assertIn("segmentation", self.cli("list-users"))

    def test_add_user_refuses_an_unknown_setting(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(["add-user", "--name", "sam", "--data-access", "all", "--dataset-root", str(self.dataset_root)])


if __name__ == "__main__":
    unittest.main()

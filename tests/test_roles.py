"""Reviewers and editors: what each role may do, and which client it works in.

Requirement under test: "two types of users, reviewers and editors, both created by the admin,
so a user can be a reviewer, an editor or both. The 3D Slicer extension serves the editing role
only and the NiiVue review page the reviewing role only: a reviewer-only user connecting from
3D Slicer is refused and told to use the review page, an editor-only user cannot log in to the
review page, and a user with both roles can use both."

Each client names the role it works in, in the ``X-Client-Role`` header, and the server checks
it against the account on every request. What a role may do to the dataset is checked against
the account itself, whichever client a request claims to come from: only an editor uploads a
segmentation, and only a reviewer confirms the stored one as it is.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import unittest

from bonehub_quality_check_server import api
from bonehub_quality_check_server import client as reference_client
from bonehub_quality_check_server.__main__ import main
from bonehub_quality_check_server.client import BoneHubQCClient, QCClientError
from bonehub_quality_check_server.config import QCServerConfig
from bonehub_quality_check_server.models import EDITOR, REVIEWER, ROLES
from bonehub_quality_check_server.review import STATIC_DIR
from bonehub_quality_check_server.store import QCError

from tests.support import QCTestCase
from tests.test_api import ApiTestCase
from tests.test_client import LiveServerTestCase

BOTH = [REVIEWER, EDITOR]


class AccountTests(QCTestCase):
    """Roles as part of an account."""

    def setUp(self) -> None:
        super().setUp()
        self.default_dataset(n_subjects=1)
        self.store = self.make_store()

    def test_a_new_user_is_a_reviewer_and_an_editor(self):
        user, _ = self.store.create_user("alice")
        self.assertEqual(user.roles, BOTH)
        self.assertTrue(user.is_reviewer and user.is_editor)

    def test_a_user_can_be_given_either_role_or_both(self):
        for roles, reviewer, editor in (([REVIEWER], True, False), ([EDITOR], False, True), (BOTH, True, True)):
            user, _ = self.store.create_user("_".join(roles), roles=roles)
            self.assertEqual(user.roles, roles)
            self.assertEqual((user.is_reviewer, user.is_editor), (reviewer, editor), roles)

    def test_each_role_is_held_once_in_a_fixed_order(self):
        user, _ = self.store.create_user("alice", roles=[EDITOR, REVIEWER, EDITOR])
        self.assertEqual(user.roles, BOTH)

    def test_a_user_without_a_role_is_refused(self):
        """They could use no client at all."""
        with self.assertRaises(QCError) as ctx:
            self.store.create_user("alice", roles=[])
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertNotIn("alice", [u["name"] for u in self.store.list_users()])

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(QCError) as ctx:
            self.store.create_user("alice", roles=[REVIEWER, "admin"])
        self.assertIn("admin", ctx.exception.message)
        self.assertNotIn("alice", [u["name"] for u in self.store.list_users()])

    def test_the_roles_can_be_changed_and_are_kept(self):
        self.store.create_user("alice")
        self.store.update_user("alice", roles=[EDITOR])
        self.assertEqual(self.make_store()._require_user("alice").roles, [EDITOR])

    def test_changing_something_else_leaves_the_roles_alone(self):
        self.store.create_user("alice", roles=[REVIEWER])
        self.store.update_user("alice", note="on leave", data_access="segmentation")
        self.assertEqual(self.store._require_user("alice").roles, [REVIEWER])

    def test_an_invalid_change_is_refused_and_changes_nothing(self):
        self.store.create_user("alice", roles=[REVIEWER])
        for roles in ([], ["admin"]):
            with self.assertRaises(QCError):
                self.store.update_user("alice", roles=roles)
        self.assertEqual(self.make_store()._require_user("alice").roles, [REVIEWER])

    def test_accounts_written_before_roles_existed_keep_both(self):
        """A users.json from server 0.2 records no roles, and its users could use both clients."""
        self.store.create_user("alice")
        entries = json.loads(self.store.users_path.read_text(encoding="utf-8"))
        for entry in entries:
            entry.pop("roles")
        self.store.users_path.write_text(json.dumps(entries), encoding="utf-8")
        self.assertEqual(self.make_store()._require_user("alice").roles, BOTH)

    def test_the_roles_are_in_the_public_view_and_the_audit_trail(self):
        user, _ = self.store.create_user("alice", roles=[EDITOR])
        self.assertEqual(user.public_dict()["roles"], [EDITOR])
        self.store.update_user("alice", roles=[REVIEWER])
        self.assertEqual(self.store.audit.read_recent(kind="user_created")[0]["roles"], [EDITOR])
        self.assertEqual(self.store.audit.read_recent(kind="user_updated")[0]["roles"], [REVIEWER])


class ClientRoleTests(ApiTestCase):
    """Which client each account may use. alice and bob hold both roles."""

    def setUp(self) -> None:
        super().setUp()
        self.reviewer_key = self.create_client_user("rita", roles=[REVIEWER])
        self.editor_key = self.create_client_user("eddie", roles=[EDITOR])

    def ping(self, key: str, role: str):
        return self.client.get("/api/v1/ping", headers=self.headers(key, role))

    def test_a_reviewer_signs_in_to_the_review_page(self):
        response = self.ping(self.reviewer_key, REVIEWER)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["role"], response.json()["roles"]), (REVIEWER, [REVIEWER]))

    def test_a_reviewer_cannot_connect_from_3d_slicer_and_is_sent_to_the_review_page(self):
        response = self.ping(self.reviewer_key, EDITOR)
        self.assertEqual(response.status_code, 403)
        detail = response.json()["detail"]
        self.assertIn("not an editor", detail)
        self.assertIn("http://testserver/review", detail, "the address of this server's review page")

    def test_an_editor_connects_from_3d_slicer(self):
        response = self.ping(self.editor_key, EDITOR)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["role"], response.json()["roles"]), (EDITOR, [EDITOR]))

    def test_an_editor_cannot_sign_in_to_the_review_page(self):
        response = self.ping(self.editor_key, REVIEWER)
        self.assertEqual(response.status_code, 403)
        detail = response.json()["detail"]
        self.assertIn("not a reviewer", detail)
        self.assertIn("3D Slicer", detail)

    def test_a_user_with_both_roles_can_use_both_clients(self):
        for role in ROLES:
            response = self.ping(self.alice_key, role)
            self.assertEqual(response.status_code, 200, role)
            self.assertEqual((response.json()["role"], response.json()["roles"]), (role, BOTH))

    def test_every_client_endpoint_checks_the_role(self):
        """Not only the sign-in: a client in the wrong role gets nowhere with the key."""
        handout = self.next_subject(self.reviewer_key, REVIEWER)
        base = f"/api/v1/assignments/{handout['assignment_id']}"
        wrong = self.headers(self.reviewer_key, EDITOR)
        for method, path in [
            ("get", "/api/v1/ping"),
            ("get", "/api/v1/labels"),
            ("post", "/api/v1/subjects/next"),
            ("get", "/api/v1/assignments"),
            ("get", base),
            ("get", f"{base}/image"),
            ("get", f"{base}/segmentation"),
            ("post", f"{base}/extend"),
            ("post", f"{base}/release"),
        ]:
            self.assertEqual(getattr(self.client, method)(path, headers=wrong).status_code, 403, f"{method} {path}")
        self.assertEqual(self.submit(self.reviewer_key, handout["assignment_id"], False, role=EDITOR).status_code, 403)
        self.assertEqual(self.store.get_assignment(handout["assignment_id"]).state, "assigned", "nothing happened")

    def test_a_client_that_does_not_name_its_role_is_told_to_update(self):
        """An extension from before roles, for instance."""
        response = self.client.get("/api/v1/ping", headers={"X-API-Key": self.alice_key})
        self.assertEqual(response.status_code, 400)
        self.assertIn("X-Client-Role", response.json()["detail"])
        self.assertIn("update it", response.json()["detail"])

    def test_an_unknown_role_is_refused(self):
        response = self.ping(self.alice_key, "admin")
        self.assertEqual(response.status_code, 400)
        self.assertIn("'admin'", response.json()["detail"])

    def test_the_key_is_checked_before_the_role(self):
        self.assertEqual(self.client.get("/api/v1/ping").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/ping", headers={"X-API-Key": "bhqc_wrong"}).status_code, 401)
        self.assertEqual(self.ping("bhqc_wrong", REVIEWER).status_code, 401)

    def test_a_role_taken_away_applies_at_once(self):
        """alice holds a subject in 3D Slicer when the admin makes her a reviewer only."""
        handout = self.next_subject(self.alice_key, EDITOR)
        self.client.patch("/admin/api/users/alice", json={"roles": [REVIEWER]}, headers=self.admin_headers)

        refused = self.client.get(handout["image_url"], headers=self.headers(self.alice_key, EDITOR))
        self.assertEqual(refused.status_code, 403)
        held = self.client.get("/api/v1/assignments", headers=self.headers(self.alice_key, REVIEWER)).json()
        self.assertEqual([a["assignment_id"] for a in held], [handout["assignment_id"]], "the review page sees it")


class VerdictTests(QCTestCase):
    """How each role may confirm, checked against the account whatever client a request names."""

    def setUp(self) -> None:
        super().setUp()
        for subject_id in (1, 2):
            self.builder.add_subject(1, subject_id, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.store = self.make_store()
        self.before_bytes = self.builder.segmentation_file(1, 1).read_bytes()

    def lease(self, name: str, roles: list):
        user = self.store.create_user(name, roles=roles)[0]
        return user, self.store.next_subject(user, roles[0])

    def assert_untouched(self):
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.before_bytes)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})

    def test_a_reviewer_cannot_upload_a_segmentation(self):
        rita, assignment = self.lease("rita", [REVIEWER])
        with self.assertRaises(QCError) as ctx:
            self.store.submit(assignment.assignment_id, rita, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("not an editor", ctx.exception.message)
        self.assert_untouched()

    def test_a_reviewer_confirms_the_stored_segmentation_as_it_is(self):
        rita, assignment = self.lease("rita", [REVIEWER])
        self.store.submit(assignment.assignment_id, rita, True, None, use_stored_segmentation=True)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.before_bytes)

    def test_an_editor_cannot_confirm_the_stored_segmentation_as_it_is(self):
        eddie, assignment = self.lease("eddie", [EDITOR])
        with self.assertRaises(QCError) as ctx:
            self.store.submit(assignment.assignment_id, eddie, True, None, use_stored_segmentation=True)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("not a reviewer", ctx.exception.message)
        self.assert_untouched()

    def test_an_editor_uploads_the_corrected_segmentation(self):
        eddie, assignment = self.lease("eddie", [EDITOR])
        outcome = self.store.submit(assignment.assignment_id, eddie, True, self.upload_file(["FEMUR_LEFT", "TIBIA_LEFT"]))
        self.assertTrue(outcome.assignment.segmentation_written)
        self.assertEqual(self.builder.labels_in_segmentation(1, 1), {"FEMUR_LEFT", "TIBIA_LEFT"})

    def test_either_role_can_reject(self):
        for name, roles in (("rita", [REVIEWER]), ("eddie", [EDITOR])):
            user, assignment = self.lease(name, roles)
            outcome = self.store.submit(assignment.assignment_id, user, False, None, comment="motion blur")
            self.assertEqual(outcome.assignment.state, "rejected", name)
        self.assert_untouched()

    def test_a_user_with_both_roles_may_confirm_either_way(self):
        alice = self.store.create_user("alice")[0]
        first = self.store.next_subject(alice, REVIEWER)
        self.store.submit(first.assignment_id, alice, True, None, use_stored_segmentation=True)
        second = self.store.next_subject(alice, EDITOR)
        self.store.submit(second.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.assertEqual(self.builder.subject_info(1, 2)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 0})


class ReviewerUploadOverHttpTests(ApiTestCase):
    def test_naming_the_reviewer_role_does_not_let_a_reviewer_upload(self):
        """The header is the client's word; the account decides what may be done to the dataset."""
        key = self.create_client_user("rita", roles=[REVIEWER])
        handout = self.next_subject(key, REVIEWER)
        before = self.builder.segmentation_file(1, 1).read_bytes()
        response = self.submit(key, handout["assignment_id"], True, ["FEMUR_LEFT"], role=REVIEWER)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), before)
        self.assertEqual(list((self.state_dir / "tmp").glob("*")), [], "the refused upload is not kept")


class QueueTests(QCTestCase):
    """A reviewer can do nothing with a subject that has no segmentation, so is not handed one."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation=None)
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1})
        self.store = self.make_store(include_subjects_without_segmentation=True)
        self.alice = self.store.create_user("alice")[0]
        self.bob = self.store.create_user("bob")[0]

    def test_a_reviewer_is_handed_only_subjects_with_a_segmentation(self):
        self.assertEqual(self.store.next_subject(self.alice, REVIEWER).subject_id, 2)
        self.assertEqual(self.store.next_subject(self.bob, EDITOR).subject_id, 1)

    def test_with_only_such_subjects_left_the_queue_is_empty_for_reviewers(self):
        self.store.next_subject(self.alice, REVIEWER)
        with self.assertRaises(QCError) as ctx:
            self.store.next_subject(self.bob, REVIEWER)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.store.next_subject(self.bob, EDITOR).subject_id, 1)

    def test_the_lease_records_the_role_it_was_asked_in(self):
        self.store.next_subject(self.alice, REVIEWER)
        self.assertEqual(self.store.audit.read_recent(kind="assigned")[0]["role"], REVIEWER)
        self.assertIn("to 'alice' as reviewer", self.builder.dataset_log(1).read_text(encoding="utf-8"))


class ReviewPageQueueTests(ApiTestCase):
    def prepare_dataset(self) -> None:
        self.builder.add_subject(1, 1, segmentation=None)

    def build_config(self):
        return QCServerConfig(include_subjects_without_segmentation=True)

    def test_the_review_page_is_not_handed_a_subject_without_a_segmentation(self):
        refused = self.client.post("/api/v1/subjects/next", headers=self.headers(self.alice_key, REVIEWER))
        self.assertEqual(refused.status_code, 404)
        self.assertFalse(self.next_subject(self.alice_key, EDITOR)["has_segmentation"])


class AdminPanelTests(ApiTestCase):
    def create(self, **payload):
        return self.client.post("/admin/api/users", json={"name": "carol", **payload}, headers=self.admin_headers)

    def roles_of(self, name: str) -> list:
        listed = self.client.get("/admin/api/users", headers=self.admin_headers).json()
        return next(u for u in listed if u["name"] == name)["roles"]

    def test_the_roles_are_chosen_when_a_user_is_created(self):
        self.assertEqual(self.create(roles=[EDITOR]).json()["user"]["roles"], [EDITOR])

    def test_the_roles_may_be_a_comma_separated_string(self):
        self.assertEqual(self.create(roles="editor, reviewer").json()["user"]["roles"], BOTH)

    def test_a_user_created_without_roles_is_both(self):
        self.assertEqual(self.create().json()["user"]["roles"], BOTH)

    def test_no_role_or_an_unknown_one_is_a_400(self):
        for roles in ([], "", ["admin"], 42):
            self.assertEqual(self.create(roles=roles).status_code, 400, roles)
        listed = self.client.get("/admin/api/users", headers=self.admin_headers).json()
        self.assertNotIn("carol", [u["name"] for u in listed])

    def test_the_roles_can_be_changed_from_the_panel(self):
        body = self.client.patch("/admin/api/users/alice", json={"roles": [REVIEWER]}, headers=self.admin_headers)
        self.assertEqual(body.json()["roles"], [REVIEWER])
        self.assertEqual(self.roles_of("alice"), [REVIEWER])

    def test_taking_away_the_last_role_is_a_400_and_changes_nothing(self):
        response = self.client.patch("/admin/api/users/alice", json={"roles": []}, headers=self.admin_headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.roles_of("alice"), BOTH)

    def test_editing_something_else_leaves_the_roles_alone(self):
        self.client.patch("/admin/api/users/alice", json={"roles": [EDITOR]}, headers=self.admin_headers)
        body = self.client.patch("/admin/api/users/alice", json={"note": "back soon"}, headers=self.admin_headers)
        self.assertEqual(body.json()["roles"], [EDITOR])

    def test_the_panel_offers_both_roles(self):
        page = self.client.get("/admin").text
        self.assertIn('id="newReviewer"', page)
        self.assertIn('id="newEditor"', page)


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

    def test_add_user_takes_the_roles(self):
        self.assertIn("'rita', reviewer,", self.cli("add-user", "--name", "rita", "--roles", "reviewer"))
        self.assertEqual(self.make_store()._users["rita"].roles, [REVIEWER])

    def test_add_user_makes_both_by_default(self):
        self.assertIn("reviewer and editor", self.cli("add-user", "--name", "alice"))
        self.assertEqual(self.make_store()._users["alice"].roles, BOTH)

    def test_list_users_shows_the_roles(self):
        self.cli("add-user", "--name", "eddie", "--roles", "editor")
        self.cli("add-user", "--name", "alice", "--roles", "editor,reviewer")
        lines = self.cli("list-users").splitlines()
        self.assertIn("reviewer,editor", next(line for line in lines if line.startswith("alice")))
        eddie = next(line for line in lines if line.startswith("eddie"))
        self.assertIn("editor", eddie)
        self.assertNotIn("reviewer", eddie)

    def test_add_user_refuses_a_role_that_does_not_exist(self):
        for roles in ("admin", "reviewer,admin", ""):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                main(["add-user", "--name", "x", "--roles", roles, "--dataset-root", str(self.dataset_root)])

    def tearDown(self) -> None:
        # The CLI opens stores of its own; close their log files so Windows lets the folder go.
        prefix = f"bonehub_quality_check_server.{self.state_root.resolve().as_posix()}"
        for name in [n for n in list(logging.Logger.manager.loggerDict) if n.startswith(prefix)]:
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            logging.Logger.manager.loggerDict.pop(name, None)
        super().tearDown()


class ReferenceClientTests(LiveServerTestCase):
    """client.py, as shipped inside the 3D Slicer extension, over a real socket."""

    def setUp(self) -> None:
        super().setUp()
        _, self.reviewer_key = self.store.create_user("rita", roles=[REVIEWER])

    def test_it_works_as_an_editor_so_a_reviewer_is_sent_to_the_review_page(self):
        with self.assertRaises(QCClientError) as ctx:
            BoneHubQCClient(self.base_url, self.reviewer_key, timeout=30).ping()
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn(f"{self.base_url}/review", str(ctx.exception))

    def test_it_can_work_as_a_reviewer(self):
        client = BoneHubQCClient(self.base_url, self.reviewer_key, timeout=30, role=REVIEWER)
        self.assertEqual(client.ping()["role"], REVIEWER)
        handout = client.next_subject()
        image = client.download_image(handout["assignment_id"], self.tmp_path / "work" / "image.nii.gz")
        self.assertEqual(image.read_bytes(), self.builder.image_file(1, handout["subject_id"]).read_bytes())


class SharedNamesTests(unittest.TestCase):
    """The clients cannot import the server's names, so they must spell them the same way."""

    def test_the_reference_client_names_the_roles_as_the_server_does(self):
        self.assertEqual(reference_client.ROLE_HEADER, api.ROLE_HEADER)
        self.assertEqual({reference_client.REVIEWER, reference_client.EDITOR}, set(ROLES))
        self.assertEqual(BoneHubQCClient("http://localhost:8000", "bhqc_x").role, EDITOR, "3D Slicer's role")

    def test_the_review_page_works_as_a_reviewer(self):
        script = (STATIC_DIR / "review.js").read_text(encoding="utf-8")
        self.assertIn(f'const ROLE_HEADER = "{api.ROLE_HEADER}";', script)
        self.assertIn(f'const ROLE = "{REVIEWER}";', script)


if __name__ == "__main__":
    unittest.main()

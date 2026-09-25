"""The REST API the clients talk to, driven over HTTP.

This walks the flow both clients follow: authenticate, ask for the next subject, download the
image and segmentation, then submit the verdict back -- a reviewer's from the review page, an
editor's from the 3D Slicer extension -- and the administrator's approval that ends it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from bonehub_data_schema import __version__ as SCHEMA_VERSION
from qc_server import api
from qc_server.app import create_app
from qc_server.models import EDITOR, REVIEWER

from tests.support import LABEL_VALUE, QCTestCase, labels_in_mask, write_mask


class ApiTestCase(QCTestCase):
    """A running application over a temporary dataset, plus two logged-in clients.

    Both users are reviewers and editors. Requests go out in the reviewer role, as from the
    review page, since every subject starts with the reviewers -- except a submission that
    uploads a segmentation, which goes out in the editor role, as from 3D Slicer -- unless a
    test names another.
    """

    n_subjects = 3
    config_kwargs: dict = {}

    def setUp(self) -> None:
        super().setUp()
        self.prepare_dataset()
        with contextlib.redirect_stdout(io.StringIO()):  # the startup banner
            self.app = create_app(
                dataset_root=self.dataset_root,
                credentials_dir=self.credentials_dir,
                config=self.build_config(),
            )
        self.store = self.track(self.app.state.store)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

        self.admin_headers = {"X-Admin-Key": self.store.admin_key}
        self.alice_key = self.create_client_user("alice")
        self.bob_key = self.create_client_user("bob")

    def prepare_dataset(self) -> None:
        self.default_dataset(n_subjects=self.n_subjects)

    def build_config(self):
        from qc_server.config import QCServerConfig

        return QCServerConfig(**self.config_kwargs) if self.config_kwargs else None

    def create_client_user(self, name: str, **payload) -> str:
        response = self.client.post("/admin/api/users", json={"name": name, **payload}, headers=self.admin_headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["api_key"]

    def headers(self, api_key: str, role: str = REVIEWER) -> dict:
        """What a client sends with every request: its key, and the role it works in."""
        return {"X-API-Key": api_key, "X-Client-Role": role}

    def next_subject(self, api_key: str, role: str = REVIEWER) -> dict:
        response = self.client.post("/api/v1/subjects/next", headers=self.headers(api_key, role))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def submit(self, api_key: str, assignment_id: str, confirmed: bool, labels=None, role: str | None = None, **metadata):
        """Post a submission the way the clients do: multipart. With ``labels`` it uploads a
        segmentation holding them, from 3D Slicer."""
        body = {"quality_check_confirmed": confirmed, **metadata}
        files = {"metadata": (None, json.dumps(body))}
        if labels is not None:
            payload = write_mask(self.tmp_path / "post.seg.nrrd", labels).read_bytes()
            files["segmentation"] = ("segmentation.seg.nrrd", payload, "application/octet-stream")
        role = role or (EDITOR if labels is not None else REVIEWER)
        return self.client.post(
            f"/api/v1/assignments/{assignment_id}/submit", files=files, headers=self.headers(api_key, role)
        )

    def judge(self, api_key: str, **metadata) -> dict:
        """The next subject waiting for a review, judged on the review page: every label
        accepted unless ``rejected_labels`` says otherwise."""
        handout = self.next_subject(api_key, REVIEWER)
        response = self.submit(api_key, handout["assignment_id"], True, use_stored_segmentation=True, **metadata)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class HealthAndAuthTests(ApiTestCase):
    def test_health_needs_no_key(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_the_root_redirects_to_the_admin_panel(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertIn(response.status_code, (302, 307))
        self.assertEqual(response.headers["location"], "/admin")

    def test_ping_reports_the_user_and_the_policy(self):
        body = self.client.get("/api/v1/ping", headers=self.headers(self.alice_key)).json()
        self.assertEqual(body["user"], "alice")
        self.assertTrue(body["edits_need_review"])
        self.assertEqual(body["schema_version"], SCHEMA_VERSION, "the extension checks it before loading anything")

    def test_every_client_endpoint_rejects_a_missing_key(self):
        for method, path in [
            ("get", "/api/v1/ping"),
            ("get", "/api/v1/labels"),
            ("post", "/api/v1/subjects/next"),
            ("get", "/api/v1/assignments"),
        ]:
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 401, f"{method} {path}")

    def test_a_wrong_key_is_rejected(self):
        response = self.client.get("/api/v1/ping", headers=self.headers("bhqc_wrong"))
        self.assertEqual(response.status_code, 401)

    def test_a_disabled_user_is_locked_out(self):
        self.client.post("/admin/api/users/alice/active", json={"active": False}, headers=self.admin_headers)
        response = self.client.get("/api/v1/ping", headers=self.headers(self.alice_key))
        self.assertEqual(response.status_code, 403)

    def test_the_label_map_is_served_to_clients(self):
        body = self.client.get("/api/v1/labels", headers=self.headers(self.alice_key)).json()
        self.assertEqual(body["label_name_to_value"]["FEMUR_LEFT"], LABEL_VALUE["FEMUR_LEFT"])
        self.assertNotIn("BACKGROUND", body["label_name_to_value"], "no segment can be background")
        self.assertEqual(sorted(body["label_status_values"]), ["0", "1", "2"])
        self.assertEqual(sorted(body["reject_reasons"]), ["absent", "missing", "quality"])
        self.assertEqual(body["segmentation_suffix"], ".seg.nrrd")
        self.assertEqual(body["schema_version"], SCHEMA_VERSION)


class HandoutTests(ApiTestCase):
    def test_the_handout_describes_the_subject_and_where_to_fetch_it(self):
        handout = self.next_subject(self.alice_key)
        self.assertEqual(handout["subject_key"], "001_000001")
        self.assertEqual((handout["role"], handout["stage"]), (REVIEWER, "review"))
        self.assertTrue(handout["has_image"])
        self.assertTrue(handout["has_segmentation"])
        self.assertEqual(handout["segmentation_source"], "dataset")
        self.assertEqual(handout["segmentation_labels"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.assertEqual([(label["name"], label["state"]) for label in handout["labels"]],
                         [("FEMUR_LEFT", "pending"), ("FEMUR_RIGHT", "pending")])
        self.assertEqual(handout["history"], [])
        self.assertEqual(handout["subject_info"]["subject_id"], 1)
        self.assertEqual(handout["dataset_info"]["dataset_id"], 1)
        self.assertTrue(handout["image_url"].endswith("/image"))
        self.assertTrue(handout["segmentation_url"].endswith("/segmentation"))

    def test_two_clients_are_given_different_subjects(self):
        self.assertNotEqual(
            self.next_subject(self.alice_key)["subject_key"],
            self.next_subject(self.bob_key)["subject_key"],
        )

    def test_the_image_downloads(self):
        handout = self.next_subject(self.alice_key)
        response = self.client.get(handout["image_url"], headers=self.headers(self.alice_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.builder.image_file(1, 1).read_bytes())

    def test_the_segmentation_downloads_when_there_is_one(self):
        handout = self.next_subject(self.alice_key)
        response = self.client.get(handout["segmentation_url"], headers=self.headers(self.alice_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.builder.segmentation_file(1, 1).read_bytes())

    def test_a_download_names_the_file_and_its_size(self):
        handout = self.next_subject(self.alice_key)
        response = self.client.get(handout["image_url"], headers=self.headers(self.alice_key))
        image = self.builder.image_file(1, 1)
        self.assertEqual(response.headers["content-length"], str(image.stat().st_size))
        self.assertEqual(response.headers["content-disposition"], f'attachment; filename="{image.name}"')

    def test_a_file_larger_than_one_chunk_arrives_whole(self):
        handout = self.next_subject(self.alice_key)
        with mock.patch.object(api, "DOWNLOAD_CHUNK_BYTES", 7):
            response = self.client.get(handout["image_url"], headers=self.headers(self.alice_key))
        self.assertEqual(response.content, self.builder.image_file(1, 1).read_bytes())

    @unittest.skipUnless(hasattr(os, "posix_fadvise"), "read-ahead is turned off on Linux, where the server runs")
    def test_a_download_reads_the_share_without_read_ahead(self):
        """Read-ahead fills the one connection to the share, and every other user's request waits behind it."""
        handout = self.next_subject(self.alice_key)
        with mock.patch("os.posix_fadvise") as fadvise:
            response = self.client.get(handout["image_url"], headers=self.headers(self.alice_key))
        self.assertEqual(response.status_code, 200)
        fadvise.assert_called_once()
        self.assertEqual(fadvise.call_args.args[3], os.POSIX_FADV_RANDOM)

    def test_one_client_cannot_download_another_clients_subject(self):
        handout = self.next_subject(self.alice_key)
        response = self.client.get(handout["image_url"], headers=self.headers(self.bob_key))
        self.assertEqual(response.status_code, 403)

    def test_a_client_can_list_and_re_fetch_what_it_holds(self):
        handout = self.next_subject(self.alice_key)
        listed = self.client.get("/api/v1/assignments", headers=self.headers(self.alice_key)).json()
        self.assertEqual([a["assignment_id"] for a in listed], [handout["assignment_id"]])

        again = self.client.get(
            f"/api/v1/assignments/{handout['assignment_id']}", headers=self.headers(self.alice_key)
        )
        self.assertEqual(again.json()["subject_key"], handout["subject_key"])

    def test_each_client_lists_only_what_it_was_handed(self):
        self.next_subject(self.alice_key, REVIEWER)
        listed = self.client.get("/api/v1/assignments", headers=self.headers(self.alice_key, EDITOR)).json()
        self.assertEqual(listed, [])

    def test_extend_and_release_work_over_http(self):
        handout = self.next_subject(self.alice_key)
        assignment_id = handout["assignment_id"]
        extended = self.client.post(f"/api/v1/assignments/{assignment_id}/extend", headers=self.headers(self.alice_key))
        self.assertEqual(extended.status_code, 200)

        released = self.client.post(
            f"/api/v1/assignments/{assignment_id}/release", headers=self.headers(self.alice_key)
        )
        self.assertEqual(released.json()["state"], "released")
        self.assertEqual(self.next_subject(self.bob_key)["subject_key"], handout["subject_key"])

    def test_an_empty_queue_answers_404(self):
        for key in [self.alice_key, self.bob_key, self.create_client_user("carol")]:
            self.next_subject(key)
        response = self.client.post("/api/v1/subjects/next", headers=self.headers(self.create_client_user("dave")))
        self.assertEqual(response.status_code, 404)
        self.assertIn("review", response.json()["detail"])

    def test_an_editor_is_not_handed_a_subject_nobody_has_reviewed(self):
        response = self.client.post("/api/v1/subjects/next", headers=self.headers(self.alice_key, EDITOR))
        self.assertEqual(response.status_code, 404)


class NoSegmentationHandoutTests(ApiTestCase):
    """A subject without a segmentation goes straight to the editors."""

    def prepare_dataset(self) -> None:
        self.builder.add_subject(1, 1, segmentation=None)

    def build_config(self):
        from qc_server.config import QCServerConfig

        return QCServerConfig(include_subjects_without_segmentation=True)

    def test_the_handout_says_there_is_no_segmentation_to_fetch(self):
        handout = self.next_subject(self.alice_key, EDITOR)
        self.assertEqual(handout["stage"], "edit")
        self.assertFalse(handout["has_segmentation"])
        self.assertIsNone(handout["segmentation_url"])
        self.assertIsNone(handout["segmentation_source"])

    def test_downloading_a_segmentation_that_does_not_exist_is_a_404(self):
        handout = self.next_subject(self.alice_key, EDITOR)
        response = self.client.get(
            f"/api/v1/assignments/{handout['assignment_id']}/segmentation", headers=self.headers(self.alice_key, EDITOR)
        )
        self.assertEqual(response.status_code, 404)


class ReviewOverHttpTests(ApiTestCase):
    def test_a_verdict_waits_for_the_administrator(self):
        body = self.judge(self.alice_key, comment="clean")
        self.assertTrue(body["quality_check_confirmed"])
        self.assertEqual((body["state"], body["stage"]), ("submitted", "approval"))
        self.assertEqual(body["accepted_labels"], ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertIn("approval", body["message"])
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})

        approved = self.client.post("/admin/api/cases/001_000001/approve", headers=self.admin_headers)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})

    def test_a_rejected_subject_changes_nothing(self):
        before = self.builder.all_subject_info(1)
        before_bytes = self.builder.segmentation_file(1, 1).read_bytes()

        handout = self.next_subject(self.alice_key)
        response = self.submit(self.alice_key, handout["assignment_id"], False, comment="wrong side")
        self.assertEqual(response.status_code, 200, response.text)

        body = response.json()
        self.assertEqual((body["state"], body["stage"]), ("submitted", "edit"))
        self.assertEqual(body["rejected_labels"], {"FEMUR_LEFT": "quality", "FEMUR_RIGHT": "quality"})
        self.assertEqual(self.builder.all_subject_info(1), before)
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), before_bytes)

    def test_labels_are_judged_one_by_one(self):
        body = self.judge(self.alice_key, rejected_labels={"FEMUR_RIGHT": "absent"}, missing_labels=["TIBIA_LEFT"])
        self.assertEqual(body["accepted_labels"], ["FEMUR_LEFT"])
        self.assertEqual(body["rejected_labels"], {"FEMUR_RIGHT": "absent", "TIBIA_LEFT": "missing"})
        self.assertEqual(body["missing_labels"], ["TIBIA_LEFT"])
        self.assertEqual(body["stage"], "edit")

    def test_a_verdict_from_the_wrong_client_is_refused(self):
        handout = self.next_subject(self.alice_key, REVIEWER)
        response = self.submit(self.alice_key, handout["assignment_id"], False, role=EDITOR)
        self.assertEqual(response.status_code, 409)
        self.assertIn("review page", response.json()["detail"])


class EditOverHttpTests(ApiTestCase):
    """An editor's side, once a reviewer has rejected the right femur of subject 1."""

    def setUp(self) -> None:
        super().setUp()
        self.judge(self.bob_key, rejected_labels={"FEMUR_RIGHT": "quality"}, comment="head cut off")
        self.handout = self.next_subject(self.alice_key, EDITOR)

    def test_the_editor_is_told_what_was_rejected_and_why(self):
        self.assertEqual((self.handout["subject_key"], self.handout["stage"]), ("001_000001", "edit"))
        labels = {label["name"]: label for label in self.handout["labels"]}
        self.assertEqual((labels["FEMUR_RIGHT"]["state"], labels["FEMUR_RIGHT"]["reason"]), ("rejected", "quality"))
        self.assertEqual(labels["FEMUR_LEFT"]["state"], "accepted")
        self.assertEqual(self.handout["history"][0]["comment"], "head cut off")

    def test_the_upload_waits_on_the_server(self):
        before = self.dataset_state()
        response = self.submit(self.alice_key, self.handout["assignment_id"], True, ["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["segmentation_staged"])
        self.assertEqual(body["stage"], "review")
        self.assertEqual(body["edited_labels"], ["FEMUR_RIGHT", "TIBIA_LEFT"])
        self.assertEqual(self.dataset_state(), before)
        staged = self.state_dir / "staged" / "Dataset_001" / "001_000001.seg.nrrd"
        self.assertEqual(labels_in_mask(staged), {"FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"})

    def test_the_next_reviewer_downloads_the_correction(self):
        self.submit(self.alice_key, self.handout["assignment_id"], True, ["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"])
        handout = self.next_subject(self.bob_key, REVIEWER)
        self.assertEqual((handout["subject_key"], handout["segmentation_source"]), ("001_000001", "staged"))
        response = self.client.get(handout["segmentation_url"], headers=self.headers(self.bob_key))
        staged = self.state_dir / "staged" / "Dataset_001" / "001_000001.seg.nrrd"
        self.assertEqual(response.content, staged.read_bytes())

    def test_a_subset_of_labels_can_be_vouched_for(self):
        self.store.config.edits_need_review = False
        response = self.submit(
            self.alice_key, self.handout["assignment_id"], True, ["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"],
            confirmed_labels=["TIBIA_LEFT"],
        )
        body = response.json()
        self.assertEqual((body["accepted_labels"], body["pending_labels"]), (["TIBIA_LEFT"], ["FEMUR_RIGHT"]))

    def test_confirming_without_a_file_is_a_400(self):
        response = self.submit(self.alice_key, self.handout["assignment_id"], True, role=EDITOR)
        self.assertEqual(response.status_code, 400)

    def test_a_corrupt_upload_is_refused(self):
        response = self.client.post(
            f"/api/v1/assignments/{self.handout['assignment_id']}/submit",
            files={
                "metadata": (None, json.dumps({"quality_check_confirmed": True})),
                "segmentation": ("seg.seg.nrrd", b"garbage", "application/octet-stream"),
            },
            headers=self.headers(self.alice_key, EDITOR),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.case_of("001_000001").stage, "edit")

    def test_an_upload_over_the_size_cap_is_refused(self):
        self.store.config.max_upload_bytes = 64
        response = self.submit(self.alice_key, self.handout["assignment_id"], True, ["FEMUR_LEFT"])
        self.assertEqual(response.status_code, 413)

    def test_no_temporary_upload_is_left_behind(self):
        self.submit(self.alice_key, self.handout["assignment_id"], True, ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual(list((self.state_dir / "tmp").glob("*")), [])


class SubmissionRequestTests(ApiTestCase):
    def test_malformed_metadata_is_a_400(self):
        handout = self.next_subject(self.alice_key)
        response = self.client.post(
            f"/api/v1/assignments/{handout['assignment_id']}/submit",
            files={"metadata": (None, "not json at all")},
            headers=self.headers(self.alice_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_metadata_missing_the_verdict_is_a_400(self):
        handout = self.next_subject(self.alice_key)
        response = self.client.post(
            f"/api/v1/assignments/{handout['assignment_id']}/submit",
            files={"metadata": (None, json.dumps({"comment": "forgot the verdict"}))},
            headers=self.headers(self.alice_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_one_client_cannot_submit_for_another(self):
        handout = self.next_subject(self.alice_key)
        response = self.submit(self.bob_key, handout["assignment_id"], False)
        self.assertEqual(response.status_code, 403)


class FullRoundTripTests(ApiTestCase):
    """The whole journey: two reviewers, an editor in 3D Slicer, and the administrator."""

    n_subjects = 2

    def test_two_subjects_from_first_review_to_the_dataset(self):
        carol_key = self.create_client_user("carol", roles=["editor"])
        # alice rejects the right femur of subject 1; bob accepts subject 2 as it is.
        self.judge(self.alice_key, rejected_labels={"FEMUR_RIGHT": "quality"}, comment="head cut off")
        self.judge(self.bob_key)

        # carol corrects subject 1 in 3D Slicer.
        handout = self.next_subject(carol_key, EDITOR)
        for url in (handout["image_url"], handout["segmentation_url"]):
            self.assertEqual(self.client.get(url, headers=self.headers(carol_key, EDITOR)).status_code, 200)
        response = self.submit(carol_key, handout["assignment_id"], True, ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual(response.json()["stage"], "review")

        # bob, not the editor, reviews the correction.
        self.assertEqual(self.judge(self.bob_key)["stage"], "approval")
        stats = self.client.get("/admin/api/stats", headers=self.admin_headers).json()
        self.assertEqual(stats["awaiting_approval"], 2)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})

        # The administrator approves both, and only now does the dataset change.
        body = self.client.post("/admin/api/cases/approve", json={}, headers=self.admin_headers).json()
        self.assertEqual(body["approved"], 2)
        for subject_id in (1, 2):
            self.assertEqual(self.builder.subject_info(1, subject_id)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        log = self.builder.dataset_log(1).read_text(encoding="utf-8")
        self.assertIn("corrected by 'carol'", log)
        stats = self.client.get("/admin/api/stats", headers=self.admin_headers).json()
        self.assertEqual((stats["applied"], stats["awaiting_approval"]), (2, 0))


if __name__ == "__main__":
    unittest.main()

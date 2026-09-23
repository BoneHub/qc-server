"""The REST API a 3D Slicer client talks to, driven over HTTP.

This walks the flow the extension follows: authenticate, ask for the next subject,
download the image and segmentation, then submit the verdict back.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from fastapi.testclient import TestClient

from bonehub_data_schema import __version__ as SCHEMA_VERSION
from bonehub_quality_check_server.app import create_app

from tests.support import LABEL_VALUE, QCTestCase, write_mask


class ApiTestCase(QCTestCase):
    """A running application over a temporary dataset, plus two logged-in clients."""

    n_subjects = 3
    config_kwargs: dict = {}

    def setUp(self) -> None:
        super().setUp()
        self.prepare_dataset()
        with contextlib.redirect_stdout(io.StringIO()):  # the startup banner
            self.app = create_app(
                dataset_root=self.dataset_root,
                state_dir=self.state_dir,
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
        from bonehub_quality_check_server.config import QCServerConfig

        return QCServerConfig(**self.config_kwargs) if self.config_kwargs else None

    def create_client_user(self, name: str, **payload) -> str:
        response = self.client.post("/admin/api/users", json={"name": name, **payload}, headers=self.admin_headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["api_key"]

    def headers(self, api_key: str) -> dict:
        return {"X-API-Key": api_key}

    def next_subject(self, api_key: str) -> dict:
        response = self.client.post("/api/v1/subjects/next", headers=self.headers(api_key))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def submit(self, api_key: str, assignment_id: str, confirmed: bool, labels=None, **metadata):
        """Post a submission the way the 3D Slicer extension does: multipart."""
        body = {"quality_check_confirmed": confirmed, **metadata}
        files = {"metadata": (None, json.dumps(body))}
        if labels is not None:
            payload = write_mask(self.tmp_path / "post.seg.nrrd", labels).read_bytes()
            files["segmentation"] = ("segmentation.seg.nrrd", payload, "application/octet-stream")
        return self.client.post(
            f"/api/v1/assignments/{assignment_id}/submit", files=files, headers=self.headers(api_key)
        )


class HealthAndAuthTests(ApiTestCase):
    def test_health_needs_no_key(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_the_root_redirects_to_the_admin_panel(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertIn(response.status_code, (302, 307))
        self.assertEqual(response.headers["location"], "/admin")

    def test_ping_reports_the_reviewer_and_the_policy(self):
        body = self.client.get("/api/v1/ping", headers=self.headers(self.alice_key)).json()
        self.assertEqual(body["user"], "alice")
        self.assertEqual(body["confirmed_label_status"], 2)
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

    def test_a_disabled_reviewer_is_locked_out(self):
        self.client.post("/admin/api/users/alice/active", json={"active": False}, headers=self.admin_headers)
        response = self.client.get("/api/v1/ping", headers=self.headers(self.alice_key))
        self.assertEqual(response.status_code, 403)

    def test_the_label_map_is_served_to_clients(self):
        body = self.client.get("/api/v1/labels", headers=self.headers(self.alice_key)).json()
        self.assertEqual(body["label_name_to_value"]["FEMUR_LEFT"], LABEL_VALUE["FEMUR_LEFT"])
        self.assertNotIn("BACKGROUND", body["label_name_to_value"], "no segment can be background")
        self.assertEqual(sorted(body["label_status_values"]), ["0", "1", "2"])
        self.assertEqual(body["confirmed_label_status"], 2)
        self.assertEqual(body["segmentation_suffix"], ".seg.nrrd")
        self.assertEqual(body["schema_version"], SCHEMA_VERSION)


class HandoutTests(ApiTestCase):
    def test_the_handout_describes_the_subject_and_where_to_fetch_it(self):
        handout = self.next_subject(self.alice_key)
        self.assertEqual(handout["subject_key"], "001_000001")
        self.assertTrue(handout["has_image"])
        self.assertTrue(handout["has_segmentation"])
        self.assertEqual(handout["segmentation_labels"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.assertEqual(handout["label_values"]["FEMUR_LEFT"], LABEL_VALUE["FEMUR_LEFT"])
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


class NoSegmentationHandoutTests(ApiTestCase):
    def prepare_dataset(self) -> None:
        self.builder.add_subject(1, 1, segmentation=None)

    def build_config(self):
        from bonehub_quality_check_server.config import QCServerConfig

        return QCServerConfig(include_subjects_without_segmentation=True)

    def test_the_handout_says_there_is_no_segmentation_to_fetch(self):
        handout = self.next_subject(self.alice_key)
        self.assertFalse(handout["has_segmentation"])
        self.assertIsNone(handout["segmentation_url"])

    def test_downloading_a_segmentation_that_does_not_exist_is_a_404(self):
        handout = self.next_subject(self.alice_key)
        response = self.client.get(
            f"/api/v1/assignments/{handout['assignment_id']}/segmentation", headers=self.headers(self.alice_key)
        )
        self.assertEqual(response.status_code, 404)


class SubmitOverHttpTests(ApiTestCase):
    def test_a_confirmed_submission_marks_the_labels_reviewed(self):
        handout = self.next_subject(self.alice_key)
        response = self.submit(self.alice_key, handout["assignment_id"], True, ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual(response.status_code, 200, response.text)

        body = response.json()
        self.assertTrue(body["quality_check_confirmed"])
        self.assertEqual(body["state"], "confirmed")
        self.assertTrue(body["segmentation_written"])
        self.assertEqual(body["updated_labels"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})

    def test_a_rejected_submission_changes_nothing(self):
        before = self.builder.all_subject_info(1)
        before_bytes = self.builder.segmentation_file(1, 1).read_bytes()

        handout = self.next_subject(self.alice_key)
        response = self.submit(self.alice_key, handout["assignment_id"], False, comment="wrong side")
        self.assertEqual(response.status_code, 200, response.text)

        body = response.json()
        self.assertEqual(body["state"], "rejected")
        self.assertFalse(body["segmentation_written"])
        self.assertEqual(body["updated_labels"], {})
        self.assertEqual(self.builder.all_subject_info(1), before)
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), before_bytes)

    def test_the_uploaded_segmentation_is_the_one_stored(self):
        handout = self.next_subject(self.alice_key)
        self.submit(self.alice_key, handout["assignment_id"], True, ["FEMUR_LEFT", "TIBIA_LEFT"])
        self.assertEqual(self.builder.labels_in_segmentation(1, 1), {"FEMUR_LEFT", "TIBIA_LEFT"})

    def test_a_subset_of_labels_can_be_confirmed(self):
        handout = self.next_subject(self.alice_key)
        response = self.submit(
            self.alice_key,
            handout["assignment_id"],
            True,
            ["FEMUR_LEFT", "FEMUR_RIGHT"],
            confirmed_labels=["FEMUR_LEFT"],
        )
        self.assertEqual(response.json()["updated_labels"], {"FEMUR_LEFT": 2})
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 1})

    def test_confirming_without_a_file_is_a_400(self):
        handout = self.next_subject(self.alice_key)
        response = self.submit(self.alice_key, handout["assignment_id"], True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})

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

    def test_a_corrupt_upload_is_refused_and_leaves_the_dataset_alone(self):
        handout = self.next_subject(self.alice_key)
        response = self.client.post(
            f"/api/v1/assignments/{handout['assignment_id']}/submit",
            files={
                "metadata": (None, json.dumps({"quality_check_confirmed": True})),
                "segmentation": ("seg.seg.nrrd", b"garbage", "application/octet-stream"),
            },
            headers=self.headers(self.alice_key),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})

    def test_an_upload_over_the_size_cap_is_refused(self):
        self.store.config.max_upload_bytes = 64
        handout = self.next_subject(self.alice_key)
        response = self.submit(self.alice_key, handout["assignment_id"], True, ["FEMUR_LEFT"])
        self.assertEqual(response.status_code, 413)

    def test_one_client_cannot_submit_for_another(self):
        handout = self.next_subject(self.alice_key)
        response = self.submit(self.bob_key, handout["assignment_id"], False)
        self.assertEqual(response.status_code, 403)

    def test_no_temporary_upload_is_left_behind(self):
        handout = self.next_subject(self.alice_key)
        self.submit(self.alice_key, handout["assignment_id"], True, ["FEMUR_LEFT"])
        self.assertEqual(list((self.state_dir / "tmp").glob("*")), [])


class FullClientRoundTripTests(ApiTestCase):
    """The whole journey, as the 3D Slicer extension performs it."""

    n_subjects = 2

    def test_two_clients_review_the_dataset_end_to_end(self):
        alice_handout = self.next_subject(self.alice_key)
        bob_handout = self.next_subject(self.bob_key)
        self.assertNotEqual(alice_handout["subject_key"], bob_handout["subject_key"])

        for handout, key in [(alice_handout, self.alice_key), (bob_handout, self.bob_key)]:
            self.assertEqual(self.client.get(handout["image_url"], headers=self.headers(key)).status_code, 200)
            self.assertEqual(self.client.get(handout["segmentation_url"], headers=self.headers(key)).status_code, 200)

        # Alice confirms after editing; Bob rejects.
        self.submit(self.alice_key, alice_handout["assignment_id"], True, ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.submit(self.bob_key, bob_handout["assignment_id"], False, comment="motion artefacts")

        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.assertEqual(self.builder.subject_info(1, 2)["segmentation"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})

        log = self.builder.dataset_log(1).read_text(encoding="utf-8")
        self.assertIn("alice", log)
        self.assertIn("motion artefacts", log)

        stats = self.client.get("/admin/api/stats", headers=self.admin_headers).json()
        self.assertEqual((stats["confirmed"], stats["rejected"]), (1, 1))


if __name__ == "__main__":
    unittest.main()

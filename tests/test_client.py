"""The reference client, driven against a real server over a real socket.

``client.py`` is the file that ships inside the 3D Slicer extension, so it is tested the
way the extension uses it -- a live HTTP server, a real multipart upload, real downloads --
and also in the reviewer role, which the review page takes in the browser.
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import uvicorn

from bonehub_quality_check_server import client as client_module
from bonehub_quality_check_server.app import create_app
from bonehub_quality_check_server.client import REVIEWER, BoneHubQCClient, QCClientError
from bonehub_quality_check_server.models import SubmissionRequest

from tests.support import QCTestCase


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiveServerTestCase(QCTestCase):
    """Runs the application in uvicorn on a background thread for the length of a test.

    alice and bob hold both roles. ``self.client`` is alice in 3D Slicer, an editor, and
    ``self.reviewer`` is alice on the review page.
    """

    n_subjects = 2

    def setUp(self) -> None:
        super().setUp()
        self.default_dataset(n_subjects=self.n_subjects)
        with contextlib.redirect_stdout(io.StringIO()):
            self.app = create_app(dataset_root=self.dataset_root, credentials_dir=self.credentials_dir)
        self.store = self.track(self.app.state.store)

        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.wait_until_started()

        _, self.alice_key = self.store.create_user("alice")
        _, self.bob_key = self.store.create_user("bob")
        self.client = BoneHubQCClient(self.base_url, self.alice_key, timeout=30)
        self.reviewer = BoneHubQCClient(self.base_url, self.alice_key, timeout=30, role=REVIEWER)

    def wait_until_started(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.started:
                return
            time.sleep(0.02)
        raise AssertionError("The test server did not start in time.")

    def stop_server(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)

    def reject_right_femur(self, client: BoneHubQCClient | None = None) -> dict:
        """A reviewer sends the next subject to the editors."""
        client = client or BoneHubQCClient(self.base_url, self.bob_key, timeout=30, role=REVIEWER)
        handout = client.next_subject()
        return client.submit(
            handout["assignment_id"], quality_check_confirmed=True, use_stored_segmentation=True,
            rejected_labels={"FEMUR_RIGHT": "quality"},
        )


class ClientHappyPathTests(LiveServerTestCase):
    def test_ping_confirms_the_key(self):
        self.assertEqual(self.client.ping()["user"], "alice")

    def test_the_label_map_is_fetched(self):
        self.assertIn("FEMUR_LEFT", self.client.labels()["label_name_to_value"])

    def test_the_whole_cycle_works(self):
        # bob rejects the right femur of subject 1, on the review page.
        self.assertEqual(self.reject_right_femur()["stage"], "edit")

        # alice corrects it in 3D Slicer.
        handout = self.client.next_subject()
        self.assertEqual((handout["subject_key"], handout["stage"]), ("001_000001", "edit"))
        image = self.client.download_image(handout["assignment_id"], self.tmp_path / "work" / "image.nii.gz")
        segmentation = self.client.download_segmentation(
            handout["assignment_id"], self.tmp_path / "work" / "segmentation.seg.nrrd"
        )
        self.assertEqual(image.read_bytes(), self.builder.image_file(1, 1).read_bytes())
        self.assertEqual(segmentation.read_bytes(), self.builder.segmentation_file(1, 1).read_bytes())
        result = self.client.submit(
            handout["assignment_id"], quality_check_confirmed=True,
            segmentation_path=self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"]),
            comment="head redrawn",
        )
        self.assertTrue(result["segmentation_staged"])
        self.assertEqual(result["stage"], "review")

        # bob reviews the correction, and the administrator approves it.
        bob = BoneHubQCClient(self.base_url, self.bob_key, timeout=30, role=REVIEWER)
        handout = bob.next_subject()
        self.assertEqual((handout["subject_key"], handout["segmentation_source"]), ("001_000001", "staged"))
        result = bob.submit(handout["assignment_id"], quality_check_confirmed=True, use_stored_segmentation=True)
        self.assertEqual(result["stage"], "approval")
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.store.approve("001_000001")
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})

    def test_a_rejection_needs_no_file_and_changes_nothing(self):
        before = self.builder.all_subject_info(1)
        handout = self.reviewer.next_subject()
        result = self.reviewer.submit(handout["assignment_id"], quality_check_confirmed=False, comment="too noisy")
        self.assertEqual((result["state"], result["stage"]), ("submitted", "edit"))
        self.assertEqual(self.builder.all_subject_info(1), before)

    def test_named_labels_can_be_judged_one_at_a_time(self):
        handout = self.reviewer.next_subject()
        result = self.reviewer.submit(
            handout["assignment_id"], quality_check_confirmed=True, use_stored_segmentation=True,
            confirmed_labels=["FEMUR_RIGHT"], missing_labels=["TIBIA_LEFT"],
        )
        self.assertEqual(result["accepted_labels"], ["FEMUR_RIGHT"])
        self.assertEqual(result["missing_labels"], ["TIBIA_LEFT"])
        self.assertEqual(result["stage"], "edit")

    def test_assignments_can_be_listed_and_re_read(self):
        handout = self.reviewer.next_subject()
        self.assertEqual([a["assignment_id"] for a in self.reviewer.my_assignments()], [handout["assignment_id"]])
        self.assertEqual(self.reviewer.assignment(handout["assignment_id"])["subject_key"], handout["subject_key"])
        self.assertEqual(self.client.my_assignments(), [], "3D Slicer lists what it was handed")

    def test_a_lease_can_be_extended_and_the_subject_given_back(self):
        handout = self.reviewer.next_subject()
        self.reviewer.extend(handout["assignment_id"])
        self.assertEqual(self.reviewer.release(handout["assignment_id"])["state"], "released")

    def test_two_clients_receive_different_subjects(self):
        bob = BoneHubQCClient(self.base_url, self.bob_key, timeout=30, role=REVIEWER)
        self.assertNotEqual(self.reviewer.next_subject()["subject_key"], bob.next_subject()["subject_key"])

    def test_a_user_can_pick_work_back_up_after_a_crash(self):
        first = self.reviewer.next_subject()
        restarted = BoneHubQCClient(self.base_url, self.alice_key, timeout=30, role=REVIEWER)
        self.assertEqual(restarted.next_subject()["assignment_id"], first["assignment_id"])

    def test_a_download_leaves_no_partial_file_behind(self):
        handout = self.reviewer.next_subject()
        destination = self.tmp_path / "work" / "image.nii.gz"
        self.reviewer.download_image(handout["assignment_id"], destination)
        self.assertEqual([p.name for p in destination.parent.iterdir()], ["image.nii.gz"])


class ClientErrorTests(LiveServerTestCase):
    def test_a_bad_key_is_reported_with_its_status(self):
        client = BoneHubQCClient(self.base_url, "bhqc_nope", timeout=30)
        with self.assertRaises(QCClientError) as ctx:
            client.ping()
        self.assertEqual(ctx.exception.status_code, 401)

    def test_the_servers_message_reaches_the_caller(self):
        self.reject_right_femur()
        handout = self.client.next_subject()
        with self.assertRaises(QCClientError) as ctx:
            self.client.submit(
                handout["assignment_id"],
                quality_check_confirmed=True,
                segmentation_path=self.upload_file(["FEMUR_LEFT"], shape=(5, 5, 5)),
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("does not match", str(ctx.exception))

    def test_an_empty_queue_is_reported_as_404(self):
        with self.assertRaises(QCClientError) as ctx:
            self.client.next_subject()  # nobody has sent an editor anything yet
        self.assertEqual(ctx.exception.status_code, 404)

    def test_the_client_refuses_to_confirm_without_a_file(self):
        """Caught locally, so a mistake never reaches the server."""
        with self.assertRaises(QCClientError) as ctx:
            self.client.submit("any-assignment", quality_check_confirmed=True)
        self.assertIsNone(ctx.exception.status_code)

    def test_a_segmentation_path_that_does_not_exist_is_caught_locally(self):
        with self.assertRaises(QCClientError) as ctx:
            self.client.submit(
                "any-assignment",
                quality_check_confirmed=True,
                segmentation_path=self.tmp_path / "missing.seg.nrrd",
            )
        self.assertIn("does not exist", str(ctx.exception))

    def test_an_unreachable_server_is_reported_clearly(self):
        client = BoneHubQCClient(f"http://127.0.0.1:{free_port()}", self.alice_key, timeout=3)
        with self.assertRaises(QCClientError) as ctx:
            client.ping()
        self.assertIn("Could not reach", str(ctx.exception))

    def test_a_trailing_slash_in_the_base_url_is_tolerated(self):
        client = BoneHubQCClient(self.base_url + "/", self.alice_key, timeout=30)
        self.assertEqual(client.ping()["user"], "alice")


class ClientSubjectWithoutSegmentationTests(LiveServerTestCase):
    """A subject an editor must segment from scratch."""

    n_subjects = 0  # the only subject is the one added below, and it has no segmentation

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 9, segmentation=None)
        self.store.config.include_subjects_without_segmentation = True
        self.store.refresh_index()

    def test_the_handout_says_there_is_nothing_to_download(self):
        handout = self.client.next_subject()
        self.assertEqual(handout["subject_id"], 9)
        self.assertFalse(handout["has_segmentation"])
        self.assertIsNone(handout["segmentation_url"])

        with self.assertRaises(QCClientError) as ctx:
            self.client.download_segmentation(handout["assignment_id"], self.tmp_path / "seg.seg.nrrd")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_a_segmentation_created_from_scratch_waits_for_review(self):
        handout = self.client.next_subject()
        result = self.client.submit(
            handout["assignment_id"],
            quality_check_confirmed=True,
            segmentation_path=self.upload_file(["FEMUR_LEFT"]),
        )
        self.assertEqual((result["stage"], result["pending_labels"]), ("review", ["FEMUR_LEFT"]))
        self.assertFalse(self.builder.segmentation_file(1, 9).exists(), "not before the approval")


class WhatTheClientSendsTests(unittest.TestCase):
    """The metadata part of a submission, as the server's SubmissionRequest reads it."""

    def sent(self, **kwargs) -> dict:
        captured = {}

        def fake_encode(fields, files):
            captured.update(json.loads(fields["metadata"]))
            return b"", "multipart/form-data"

        client = BoneHubQCClient("http://localhost:8000", "bhqc_x")
        with mock.patch.object(client_module, "_encode_multipart", side_effect=fake_encode), mock.patch.object(
            client, "_request", return_value={}
        ):
            client.submit("a1", **kwargs)
        return captured

    def test_every_field_the_server_reads_is_sent(self):
        sent = self.sent(quality_check_confirmed=True, segmentation_path=Path(__file__), confirmed_labels=["FEMUR_LEFT"])
        self.assertEqual(set(sent), set(SubmissionRequest.model_fields))
        SubmissionRequest(**sent)

    def test_a_reviewer_sends_the_verdict_on_each_label(self):
        sent = self.sent(
            quality_check_confirmed=True, use_stored_segmentation=True,
            rejected_labels={"FEMUR_RIGHT": "absent"}, missing_labels=["TIBIA_LEFT"],
        )
        self.assertEqual(sent["rejected_labels"], {"FEMUR_RIGHT": "absent"})
        self.assertEqual(sent["missing_labels"], ["TIBIA_LEFT"])
        self.assertTrue(sent["use_stored_segmentation"])


if __name__ == "__main__":
    unittest.main()

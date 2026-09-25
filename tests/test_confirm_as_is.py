"""Judging the stored segmentation as it is, which is how the browser review page gives a verdict.

The page cannot edit, so it sends ``use_stored_segmentation`` instead of a file. Accepting a
label vouches for the segmentation as it is, so the file is held to the same rules as an
upload; it is left exactly as it is, and the accepted labels become reviewed (status 2) once
the administrator approves the subject. Nobody accepts or replaces a segmentation their
account is not sent.
"""

from __future__ import annotations

import json
import unittest

from bonehub_data_schema import write_segmentation
from qc_server.models import EDITOR, REVIEWER
from qc_server.store import QCError

from tests.support import QCTestCase, reference_image, segmentation_array, write_mask
from tests.test_api import ApiTestCase

KEY = "001_000001"


def write_shifted_mask(path, labels, shift_mm: float) -> None:
    """A mask whose origin is off the image's by ``shift_mm``, as some converted data is."""
    image = reference_image()
    image.SetOrigin((shift_mm, 0.0, 0.0))
    write_segmentation(segmentation_array(labels), image, path)


class ConfirmStoredSegmentationTests(QCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]
        self.assignment = self.store.next_subject(self.alice, REVIEWER)
        self.before_bytes = self.builder.segmentation_file(1, 1).read_bytes()

    def confirm(self, **kwargs):
        return self.store.submit(
            self.assignment.assignment_id, self.alice, True, None, use_stored_segmentation=True, **kwargs
        )

    def test_the_labels_become_reviewed_on_approval_and_the_file_stays_as_it_is(self):
        outcome = self.confirm()
        self.assertEqual(outcome.accepted_labels, ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.store.approve(KEY)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.before_bytes)

    def test_a_label_left_unaccepted_waits_for_another_reviewer(self):
        outcome = self.confirm(confirmed_labels=["FEMUR_RIGHT"])
        self.assertEqual(outcome.stage, "review")
        self.assertEqual(outcome.pending_labels, ["FEMUR_LEFT"])

    def test_nothing_is_backed_up_staged_or_left_behind(self):
        self.confirm()
        self.store.approve(KEY)
        for folder in ("backups", "staged"):
            self.assertEqual(list((self.state_dir / folder).rglob("*.seg.nrrd")), [], folder)
        self.assertEqual(list((self.state_dir / "tmp").glob("*")), [])

    def test_the_assignment_is_closed_without_a_file(self):
        self.confirm()
        assignment = self.store.get_assignment(self.assignment.assignment_id)
        self.assertEqual(assignment.state, "submitted")
        self.assertFalse(assignment.segmentation_staged)
        self.assertEqual(assignment.accepted_labels, ["FEMUR_LEFT", "FEMUR_RIGHT"])

    def test_the_audit_trail_records_the_verdict(self):
        self.confirm(comment="looked fine in the browser")
        entries = [
            json.loads(line)
            for line in (self.state_dir / "submissions.jsonl").read_text(encoding="utf-8").splitlines()
            if '"submission"' in line
        ]
        self.assertEqual(entries[-1]["role"], REVIEWER)
        self.assertFalse(entries[-1]["segmentation_staged"])
        self.assertEqual(entries[-1]["data_access"], "image_and_segmentation")
        self.assertIn("looked fine in the browser", (self.state_dir / "server.log").read_text(encoding="utf-8"))

    def test_a_label_not_in_the_segmentation_cannot_be_confirmed(self):
        with self.assertRaises(QCError) as ctx:
            self.confirm(confirmed_labels=["TIBIA_LEFT"])
        self.assertIn("stored segmentation", ctx.exception.message)
        self.assertIsNone(self.store.case_of(KEY))


class ListedButNotPaintedTests(QCTestCase):
    def test_a_listed_label_the_file_does_not_paint_becomes_not_available(self):
        """The same rule as for an upload: the file is the truth about the subject."""
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1}, write_segmentation_file=False)
        write_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT"])
        store = self.make_store()
        alice = store.create_user("alice")[0]
        assignment = store.next_subject(alice, REVIEWER)

        outcome = store.submit(assignment.assignment_id, alice, True, None, use_stored_segmentation=True)
        self.assertEqual(outcome.stage, "approval")
        self.assertEqual(store.case_of(KEY).labels["FEMUR_RIGHT"].state, "removed")
        store.approve(KEY)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 0})


class ConfirmStoredRefusalTests(QCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})

    def open(self, **config):
        store = self.make_store(**config)
        alice = store.create_user("alice")[0]
        return store, alice, store.next_subject(alice, REVIEWER)

    def assert_untouched(self, store, before: bytes | None = None) -> None:
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1})
        if before is not None:
            self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), before)
        self.assertIsNone(store.case_of(KEY), "nothing was recorded")

    def test_a_file_and_the_flag_together_are_refused(self):
        store, alice, assignment = self.open()
        with self.assertRaises(QCError) as ctx:
            store.submit(
                assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]), use_stored_segmentation=True
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assert_untouched(store)

    def test_without_the_flag_a_reviewer_cannot_confirm(self):
        store, alice, assignment = self.open()
        with self.assertRaises(QCError) as ctx:
            store.submit(assignment.assignment_id, alice, True, None)
        self.assertIn("use_stored_segmentation", ctx.exception.message)
        self.assert_untouched(store)

    def test_a_segmentation_off_its_image_grid_is_refused_with_advice(self):
        write_shifted_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT"], 0.4)
        before = self.builder.segmentation_file(1, 1).read_bytes()
        store, alice, assignment = self.open()
        with self.assertRaises(QCError) as ctx:
            store.submit(assignment.assignment_id, alice, True, None, use_stored_segmentation=True)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("0.4 mm", ctx.exception.message)
        self.assertIn("3D Slicer", ctx.exception.message)
        self.assert_untouched(store, before)

    def test_but_its_labels_can_be_rejected_for_an_editor_to_correct(self):
        write_shifted_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT"], 0.4)
        store, alice, assignment = self.open()
        outcome = store.submit(
            assignment.assignment_id, alice, True, None, use_stored_segmentation=True,
            rejected_labels={"FEMUR_LEFT": "quality"}, comment="off the image's grid",
        )
        self.assertEqual(outcome.stage, "edit")

    def test_the_geometry_policy_applies_to_it_like_to_an_upload(self):
        write_shifted_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT"], 0.4)
        store, alice, assignment = self.open(require_geometry_match=False)
        store.submit(assignment.assignment_id, alice, True, None, use_stored_segmentation=True)
        store.approve(KEY)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2})

    def test_the_issue_is_known_before_anyone_submits(self):
        store, _, _ = self.open()
        self.assertIsNone(store.stored_segmentation_issue(1, 1))
        write_shifted_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT"], 0.4)
        self.assertIn("affine", store.stored_segmentation_issue(1, 1))

    def test_a_rejection_ignores_the_flag(self):
        store, alice, assignment = self.open()
        outcome = store.submit(assignment.assignment_id, alice, False, None, use_stored_segmentation=True)
        self.assertEqual((outcome.assignment.state, outcome.stage), ("submitted", "edit"))
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1})


class NoStoredSegmentationTests(QCTestCase):
    def test_there_is_nothing_to_confirm_as_it_is(self):
        self.builder.add_subject(1, 1, segmentation=None)
        store = self.make_store(include_subjects_without_segmentation=True)
        alice = store.create_user("alice")[0]
        assignment = store.next_subject(alice, EDITOR)
        with self.assertRaises(QCError) as ctx:
            store.submit(assignment.assignment_id, alice, True, None, use_stored_segmentation=True)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(store.stored_segmentation_issue(1, 1), "There is no stored segmentation.")


class DataAccessVerdictTests(QCTestCase):
    """Nobody accepts or replaces a segmentation their account is not sent."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.before_bytes = self.builder.segmentation_file(1, 1).read_bytes()

    def open(self, access: str, **config):
        store = self.make_store(**config)
        user = store.create_user(f"user_{access}", data_access=access)[0]
        return store, user, store.next_subject(user, REVIEWER)

    def test_a_reviewer_sent_the_image_only_cannot_accept_the_stored_segmentation(self):
        store, ian, assignment = self.open("image")
        with self.assertRaises(QCError) as ctx:
            store.submit(assignment.assignment_id, ian, True, None, use_stored_segmentation=True)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("Reject", ctx.exception.message)

    def test_but_can_reject_it(self):
        store, ian, assignment = self.open("image")
        outcome = store.submit(assignment.assignment_id, ian, False, None, comment="motion blur")
        self.assertEqual(outcome.stage, "edit")

    def test_an_editor_who_is_no_longer_sent_it_cannot_replace_it(self):
        """They would be overwriting labels they have never seen, and marking them absent."""
        store = self.make_store()
        alice = store.create_user("alice")[0]
        self.review(store, alice, rejected={"FEMUR_RIGHT": "quality"})
        assignment = store.next_subject(alice, EDITOR)
        alice = store.update_user("alice", data_access="image")
        with self.assertRaises(QCError) as ctx:
            store.submit(assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("not seen", ctx.exception.message)
        self.assertFalse((self.state_dir / "staged" / "Dataset_001" / f"{KEY}.seg.nrrd").exists())

    def test_an_editor_sent_the_image_only_can_create_a_segmentation_where_there_is_none(self):
        self.builder.add_subject(1, 2, segmentation=None)
        store = self.make_store(include_subjects_without_segmentation=True)
        ian = store.create_user("ian", data_access="image")[0]
        assignment = store.next_subject(ian, EDITOR)
        self.assertEqual(assignment.subject_id, 2, "the subject with a segmentation is not for them")
        outcome = store.submit(assignment.assignment_id, ian, True, self.upload_file(["TIBIA_LEFT"]))
        self.assertTrue(outcome.segmentation_staged)
        self.assertEqual(outcome.pending_labels, ["TIBIA_LEFT"])

    def test_a_reviewer_sent_the_segmentation_only_can_accept_it_as_it_is(self):
        store, sam, assignment = self.open("segmentation")
        outcome = store.submit(assignment.assignment_id, sam, True, None, use_stored_segmentation=True)
        self.assertEqual(outcome.stage, "approval")
        self.assertIn("segmentation only", (self.state_dir / "server.log").read_text(encoding="utf-8"))


class ConfirmOverHttpTests(ApiTestCase):
    """As the review page does it: in the reviewer role."""

    def submit_stored(self, key: str, assignment_id: str, **metadata):
        body = {"quality_check_confirmed": True, "use_stored_segmentation": True, **metadata}
        return self.client.post(
            f"/api/v1/assignments/{assignment_id}/submit",
            files={"metadata": (None, json.dumps(body))},
            headers=self.headers(key, REVIEWER),
        )

    def test_the_review_page_gives_its_verdict_without_a_file(self):
        handout = self.next_subject(self.alice_key, REVIEWER)
        self.assertIsNone(handout["stored_segmentation_issue"])
        response = self.submit_stored(
            self.alice_key, handout["assignment_id"], confirmed_labels=["FEMUR_LEFT"],
            rejected_labels={"FEMUR_RIGHT": "absent"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["accepted_labels"], body["rejected_labels"]), (["FEMUR_LEFT"], {"FEMUR_RIGHT": "absent"}))
        self.assertEqual(body["stage"], "edit")

    def test_an_unknown_reason_is_refused_by_the_api(self):
        handout = self.next_subject(self.alice_key, REVIEWER)
        response = self.submit_stored(self.alice_key, handout["assignment_id"], rejected_labels={"FEMUR_RIGHT": "ugly"})
        self.assertEqual(response.status_code, 400)

    def test_the_handout_warns_about_a_segmentation_off_its_grid(self):
        write_shifted_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT", "FEMUR_RIGHT"], 0.4)
        handout = self.next_subject(self.alice_key, REVIEWER)
        self.assertIn("0.4 mm", handout["stored_segmentation_issue"])
        response = self.submit_stored(self.alice_key, handout["assignment_id"])
        self.assertEqual(response.status_code, 409)
        self.assertIn("3D Slicer", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()

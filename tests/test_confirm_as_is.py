"""Confirming the stored segmentation as it is, which is how the browser review page confirms.

The page cannot edit, so it sends ``use_stored_segmentation`` instead of a file. The stored
file is then held to the same rules as an upload and left exactly as it is, and the confirmed
labels become reviewed (status 2). Nobody confirms or replaces a segmentation their account is
not sent.
"""

from __future__ import annotations

import json
import unittest

from bonehub_data_schema import write_segmentation
from bonehub_quality_check_server.models import REVIEWER
from bonehub_quality_check_server.store import QCError

from tests.support import QCTestCase, reference_image, segmentation_array, write_mask
from tests.test_api import ApiTestCase


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
        self.assignment = self.store.next_subject(self.alice)
        self.before_bytes = self.builder.segmentation_file(1, 1).read_bytes()

    def confirm(self, **kwargs):
        return self.store.submit(
            self.assignment.assignment_id, self.alice, True, None, use_stored_segmentation=True, **kwargs
        )

    def test_the_labels_become_reviewed_and_the_file_stays_as_it_is(self):
        outcome = self.confirm()
        self.assertEqual(outcome.updated_labels, {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.before_bytes)

    def test_only_the_labels_vouched_for_are_marked_reviewed(self):
        self.confirm(confirmed_labels=["FEMUR_RIGHT"])
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 2})

    def test_nothing_is_backed_up_or_left_behind(self):
        outcome = self.confirm()
        self.assertIsNone(outcome.backup_path)
        self.assertEqual(list((self.state_dir / "backups").rglob("*.seg.nrrd")), [])
        self.assertEqual(list((self.state_dir / "tmp").glob("*")), [])

    def test_the_assignment_is_confirmed_without_a_write(self):
        self.confirm()
        assignment = self.store.get_assignment(self.assignment.assignment_id)
        self.assertEqual(assignment.state, "confirmed")
        self.assertFalse(assignment.segmentation_written)
        self.assertEqual(assignment.confirmed_labels, ["FEMUR_LEFT", "FEMUR_RIGHT"])

    def test_the_audit_trail_says_the_stored_segmentation_was_confirmed(self):
        self.confirm(comment="looked fine in the browser")
        entries = [
            json.loads(line)
            for line in (self.state_dir / "submissions.jsonl").read_text(encoding="utf-8").splitlines()
            if '"submission"' in line
        ]
        self.assertTrue(entries[-1]["use_stored_segmentation"])
        self.assertFalse(entries[-1]["segmentation_written"])
        self.assertEqual(entries[-1]["data_access"], "image_and_segmentation")
        log = self.builder.dataset_log(1).read_text(encoding="utf-8")
        self.assertIn("confirmed as it is", log)
        self.assertIn("looked fine in the browser", log)

    def test_a_label_not_in_the_segmentation_cannot_be_confirmed(self):
        with self.assertRaises(QCError) as ctx:
            self.confirm(confirmed_labels=["TIBIA_LEFT"])
        self.assertIn("stored segmentation", ctx.exception.message)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})


class ListedButNotPaintedTests(QCTestCase):
    def test_a_listed_label_the_file_does_not_paint_becomes_not_available(self):
        """The same rule as for an upload: the confirmed file is the truth about the subject."""
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1}, write_segmentation_file=False)
        write_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT"])
        store = self.make_store()
        alice = store.create_user("alice")[0]
        assignment = store.next_subject(alice)

        outcome = store.submit(assignment.assignment_id, alice, True, None, use_stored_segmentation=True)
        self.assertEqual(outcome.removed_labels, ["FEMUR_RIGHT"])
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 0})


class ConfirmStoredRefusalTests(QCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})

    def open(self, **config):
        store = self.make_store(**config)
        alice = store.create_user("alice")[0]
        return store, alice, store.next_subject(alice)

    def assert_untouched(self, before: bytes | None = None) -> None:
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1})
        if before is not None:
            self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), before)

    def test_a_file_and_the_flag_together_are_refused(self):
        store, alice, assignment = self.open()
        with self.assertRaises(QCError) as ctx:
            store.submit(
                assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]), use_stored_segmentation=True
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assert_untouched()

    def test_without_the_flag_a_confirmation_still_needs_a_file(self):
        store, alice, assignment = self.open()
        with self.assertRaises(QCError) as ctx:
            store.submit(assignment.assignment_id, alice, True, None)
        self.assertIn("use_stored_segmentation", ctx.exception.message)
        self.assert_untouched()

    def test_a_segmentation_off_its_image_grid_is_refused_with_advice(self):
        write_shifted_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT"], 0.4)
        before = self.builder.segmentation_file(1, 1).read_bytes()
        store, alice, assignment = self.open()
        with self.assertRaises(QCError) as ctx:
            store.submit(assignment.assignment_id, alice, True, None, use_stored_segmentation=True)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("0.4 mm", ctx.exception.message)
        self.assertIn("3D Slicer", ctx.exception.message)
        self.assert_untouched(before)

    def test_the_geometry_policy_applies_to_it_like_to_an_upload(self):
        write_shifted_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT"], 0.4)
        store, alice, assignment = self.open(require_geometry_match=False)
        store.submit(assignment.assignment_id, alice, True, None, use_stored_segmentation=True)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2})

    def test_the_issue_is_known_before_anyone_submits(self):
        store, _, _ = self.open()
        self.assertIsNone(store.stored_segmentation_issue(1, 1))
        write_shifted_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT"], 0.4)
        self.assertIn("affine", store.stored_segmentation_issue(1, 1))

    def test_a_rejection_ignores_the_flag(self):
        store, alice, assignment = self.open()
        outcome = store.submit(assignment.assignment_id, alice, False, None, use_stored_segmentation=True)
        self.assertEqual(outcome.assignment.state, "rejected")
        self.assert_untouched()


class NoStoredSegmentationTests(QCTestCase):
    def test_there_is_nothing_to_confirm_as_it_is(self):
        self.builder.add_subject(1, 1, segmentation=None)
        store = self.make_store(include_subjects_without_segmentation=True)
        alice = store.create_user("alice")[0]
        assignment = store.next_subject(alice)
        with self.assertRaises(QCError) as ctx:
            store.submit(assignment.assignment_id, alice, True, None, use_stored_segmentation=True)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(store.stored_segmentation_issue(1, 1), "There is no stored segmentation.")


class DataAccessVerdictTests(QCTestCase):
    """Nobody confirms or replaces a segmentation their account is not sent."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.before_bytes = self.builder.segmentation_file(1, 1).read_bytes()

    def open(self, access: str, **config):
        store = self.make_store(**config)
        user = store.create_user(f"user_{access}", data_access=access)[0]
        return store, user, store.next_subject(user)

    def test_a_reviewer_sent_the_image_only_cannot_confirm_the_stored_segmentation(self):
        store, ian, assignment = self.open("image")
        with self.assertRaises(QCError) as ctx:
            store.submit(assignment.assignment_id, ian, True, None, use_stored_segmentation=True)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("Reject", ctx.exception.message)

    def test_nor_replace_it_with_an_upload(self):
        """They would be overwriting labels they have never seen, and marking them absent."""
        store, ian, assignment = self.open("image")
        with self.assertRaises(QCError) as ctx:
            store.submit(assignment.assignment_id, ian, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.before_bytes)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})

    def test_but_can_reject_it(self):
        store, ian, assignment = self.open("image")
        outcome = store.submit(assignment.assignment_id, ian, False, None, comment="motion blur")
        self.assertEqual(outcome.assignment.state, "rejected")

    def test_and_can_create_a_segmentation_where_there_is_none(self):
        self.builder.add_subject(1, 2, segmentation=None)
        store = self.make_store(include_subjects_without_segmentation=True)
        ian = store.create_user("ian", data_access="image")[0]
        first = store.next_subject(ian)
        store.submit(first.assignment_id, ian, False, None)
        second = store.next_subject(ian)
        self.assertEqual(second.subject_id, 2)
        store.submit(second.assignment_id, ian, True, self.upload_file(["TIBIA_LEFT"]))
        self.assertEqual(self.builder.subject_info(1, 2)["segmentation"], {"TIBIA_LEFT": 2})

    def test_a_reviewer_sent_the_segmentation_only_can_confirm_it_as_it_is(self):
        store, sam, assignment = self.open("segmentation")
        store.submit(assignment.assignment_id, sam, True, None, use_stored_segmentation=True)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.assertIn("segmentation only", self.builder.dataset_log(1).read_text(encoding="utf-8"))


class ConfirmOverHttpTests(ApiTestCase):
    """As the review page does it: in the reviewer role."""

    def submit_stored(self, key: str, assignment_id: str, **metadata):
        body = {"quality_check_confirmed": True, "use_stored_segmentation": True, **metadata}
        return self.client.post(
            f"/api/v1/assignments/{assignment_id}/submit",
            files={"metadata": (None, json.dumps(body))},
            headers=self.headers(key, REVIEWER),
        )

    def test_the_review_page_confirms_without_a_file(self):
        handout = self.next_subject(self.alice_key, REVIEWER)
        self.assertIsNone(handout["stored_segmentation_issue"])
        response = self.submit_stored(self.alice_key, handout["assignment_id"], confirmed_labels=["FEMUR_LEFT"])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["segmentation_written"])
        self.assertEqual(body["updated_labels"], {"FEMUR_LEFT": 2})

    def test_the_handout_warns_about_a_segmentation_off_its_grid(self):
        write_shifted_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT", "FEMUR_RIGHT"], 0.4)
        handout = self.next_subject(self.alice_key, REVIEWER)
        self.assertIn("0.4 mm", handout["stored_segmentation_issue"])
        response = self.submit_stored(self.alice_key, handout["assignment_id"])
        self.assertEqual(response.status_code, 409)
        self.assertIn("3D Slicer", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()

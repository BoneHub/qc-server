"""What a submission does to the dataset.

The central requirement: "if quality_check_confirmed = True is given by the client, the
label values for those bones must be changed to 3 meaning that they have been quality
checked. quality_check_confirmed = False then basically server must not make any changes."
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from bonehub_quality_check_server.store import QCError

from tests.support import QCTestCase, write_nifti


class ConfirmedSubmissionTests(QCTestCase):
    """quality_check_confirmed = True."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]
        self.assignment = self.store.next_subject(self.alice)

    def test_confirmed_labels_become_three_in_subject_info(self):
        upload = self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"])
        outcome = self.store.submit(self.assignment.assignment_id, self.alice, True, upload)

        self.assertEqual(outcome.updated_labels, {"FEMUR_LEFT": 3, "FEMUR_RIGHT": 3})
        stored = self.builder.subject_info(1, 1)["segmentation"]
        self.assertEqual(stored, {"FEMUR_LEFT": 3, "FEMUR_RIGHT": 3})

    def test_the_reviewed_segmentation_replaces_the_one_in_the_dataset(self):
        upload = self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"])
        self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertEqual(
            self.builder.labels_in_segmentation(1, 1),
            {"FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"},
        )

    def test_only_the_labels_the_reviewer_vouches_for_are_promoted(self):
        upload = self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.store.submit(self.assignment.assignment_id, self.alice, True, upload, confirmed_labels=["FEMUR_LEFT"])
        stored = self.builder.subject_info(1, 1)["segmentation"]
        self.assertEqual(stored["FEMUR_LEFT"], 3)
        self.assertEqual(stored["FEMUR_RIGHT"], 2, "an unvouched label keeps the value it had")

    def test_a_label_the_reviewer_added_is_recorded_as_unchecked(self):
        """A new bone drawn but not confirmed is 'available, generated, without QC'."""
        upload = self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT", "SACRUM"])
        self.store.submit(self.assignment.assignment_id, self.alice, True, upload, confirmed_labels=["FEMUR_LEFT"])
        stored = self.builder.subject_info(1, 1)["segmentation"]
        self.assertEqual(stored["SACRUM"], 2)

    def test_a_label_the_reviewer_added_and_confirmed_is_promoted(self):
        upload = self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT", "SACRUM"])
        self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"]["SACRUM"], 3)

    def test_a_label_the_reviewer_deleted_becomes_a_confirmed_absence(self):
        """Value 0 means 'not available, confirmed'."""
        upload = self.upload_file(["FEMUR_LEFT"])
        outcome = self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertEqual(outcome.removed_labels, ["FEMUR_RIGHT"])
        stored = self.builder.subject_info(1, 1)["segmentation"]
        self.assertEqual(stored, {"FEMUR_LEFT": 3, "FEMUR_RIGHT": 0})

    def test_removed_labels_can_be_left_untouched_by_policy(self):
        store = self.make_store(mark_removed_labels_absent=False)
        alice = store._users["alice"]
        assignment = store.get_assignment(self.assignment.assignment_id)
        store.submit(assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"]["FEMUR_RIGHT"], 2)

    def test_the_previous_segmentation_is_backed_up_before_being_overwritten(self):
        upload = self.upload_file(["FEMUR_LEFT"])
        outcome = self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertIsNotNone(outcome.backup_path)
        backups = list((self.state_dir / "backups").rglob("*.nii.gz"))
        self.assertEqual(len(backups), 1)

    def test_backups_can_be_switched_off(self):
        store = self.make_store(keep_segmentation_backups=False)
        alice = store._users["alice"]
        outcome = store.submit(self.assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertIsNone(outcome.backup_path)
        self.assertEqual(list((self.state_dir / "backups").rglob("*.nii.gz")), [])

    def test_the_assignment_is_closed_as_confirmed(self):
        self.store.submit(self.assignment.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT"]))
        assignment = self.store.get_assignment(self.assignment.assignment_id)
        self.assertEqual(assignment.state, "confirmed")
        self.assertTrue(assignment.quality_check_confirmed)
        self.assertTrue(assignment.segmentation_written)
        self.assertIsNotNone(assignment.submitted_at)

    def test_the_confirmed_value_is_configurable(self):
        store = self.make_store(confirmed_label_value=1)
        alice = store._users["alice"]
        store.submit(self.assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"]))
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"]["FEMUR_LEFT"], 1)

    def test_a_confirmed_subject_leaves_the_queue(self):
        self.store.submit(self.assignment.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(self.store.stats().available, 0)
        self.assertEqual([ref.subject_key for ref in self.store._index], [])

    def test_other_subject_fields_are_preserved(self):
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 2}, age=44, gender="F", imaging_modality="CT")
        store = self.make_store()
        alice = store._users["alice"]
        # Finish the subject held from setUp, so the next request hands out subject 2.
        store.submit(self.assignment.assignment_id, alice, False, None)
        assignment = store.next_subject(alice)
        self.assertEqual(assignment.subject_id, 2)
        store.submit(assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))

        entry = self.builder.subject_info(1, 2)
        self.assertEqual((entry["age"], entry["gender"], entry["imaging_modality"]), (44, "F", "CT"))
        self.assertEqual(entry["segmentation"], {"FEMUR_LEFT": 3})

    def test_other_subjects_in_the_file_are_untouched(self):
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 2})
        store = self.make_store()
        alice = store._users["alice"]
        store.submit(self.assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"]))
        self.assertEqual(self.builder.subject_info(1, 2)["segmentation"], {"FEMUR_LEFT": 2})
        self.assertEqual(len(self.builder.all_subject_info(1)), 2)


class RejectedSubmissionTests(QCTestCase):
    """quality_check_confirmed = False must change nothing in the dataset."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]
        self.assignment = self.store.next_subject(self.alice)
        self.before_info = self.builder.all_subject_info(1)
        self.before_bytes = self.builder.segmentation_file(1, 1).read_bytes()

    def test_subject_info_is_left_exactly_as_it_was(self):
        self.store.submit(self.assignment.assignment_id, self.alice, False, None)
        self.assertEqual(self.builder.all_subject_info(1), self.before_info)

    def test_the_segmentation_file_is_left_exactly_as_it_was(self):
        self.store.submit(self.assignment.assignment_id, self.alice, False, None)
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.before_bytes)

    def test_an_uploaded_file_is_ignored_when_rejecting(self):
        upload = self.upload_file(["TIBIA_LEFT"])
        outcome = self.store.submit(self.assignment.assignment_id, self.alice, False, upload)
        self.assertFalse(outcome.assignment.segmentation_written)
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.before_bytes)
        self.assertEqual(self.builder.all_subject_info(1), self.before_info)

    def test_no_backup_is_taken(self):
        self.store.submit(self.assignment.assignment_id, self.alice, False, None)
        self.assertEqual(list((self.state_dir / "backups").rglob("*.nii.gz")), [])

    def test_the_assignment_is_closed_as_rejected(self):
        outcome = self.store.submit(self.assignment.assignment_id, self.alice, False, None, comment="too noisy")
        assignment = self.store.get_assignment(self.assignment.assignment_id)
        self.assertEqual(assignment.state, "rejected")
        self.assertFalse(assignment.quality_check_confirmed)
        self.assertEqual(assignment.comment, "too noisy")
        self.assertEqual(outcome.updated_labels, {})


class SubmissionValidationTests(QCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]
        self.bob = self.store.create_user("bob")[0]
        self.assignment = self.store.next_subject(self.alice)

    def assert_dataset_untouched(self) -> None:
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})

    def test_confirming_without_a_file_is_refused(self):
        with self.assertRaises(QCError):
            self.store.submit(self.assignment.assignment_id, self.alice, True, None)
        self.assert_dataset_untouched()

    def test_an_empty_segmentation_cannot_be_confirmed(self):
        upload = self.upload_file([])
        with self.assertRaises(QCError) as ctx:
            self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertIn("empty", ctx.exception.message)
        self.assert_dataset_untouched()

    def test_a_file_that_is_not_nifti_is_refused(self):
        bad = self.tmp_path / "not_a_nifti.nii.gz"
        bad.write_bytes(b"this is not a NIfTI file")
        with self.assertRaises(QCError) as ctx:
            self.store.submit(self.assignment.assignment_id, self.alice, True, bad)
        self.assertIn("NIfTI", ctx.exception.message)
        self.assert_dataset_untouched()

    def test_voxel_values_that_are_not_bonehub_labels_are_refused(self):
        bad = self.tmp_path / "unknown_values.nii.gz"
        data = np.zeros((6, 6, 6), dtype=np.int16)
        data[0, 0, 0] = 31337
        write_nifti(bad, data)
        with self.assertRaises(QCError) as ctx:
            self.store.submit(self.assignment.assignment_id, self.alice, True, bad)
        self.assertIn("31337", ctx.exception.message)
        self.assert_dataset_untouched()

    def test_a_segmentation_whose_shape_differs_from_the_image_is_refused(self):
        upload = self.upload_file(["FEMUR_LEFT"], shape=(5, 5, 5))
        with self.assertRaises(QCError) as ctx:
            self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertIn("does not match", ctx.exception.message)
        self.assert_dataset_untouched()

    def test_a_segmentation_whose_affine_differs_from_the_image_is_refused(self):
        upload = self.upload_file(["FEMUR_LEFT"], affine=np.diag([2.0, 2.0, 2.0, 1.0]))
        with self.assertRaises(QCError) as ctx:
            self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertIn("affine", ctx.exception.message)
        self.assert_dataset_untouched()

    def test_the_geometry_check_can_be_switched_off(self):
        store = self.make_store(require_geometry_match=False)
        alice = store._users["alice"]
        upload = self.upload_file(["FEMUR_LEFT"], shape=(5, 5, 5))
        store.submit(self.assignment.assignment_id, alice, True, upload)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"]["FEMUR_LEFT"], 3)

    def test_an_unknown_confirmed_label_name_is_refused(self):
        upload = self.upload_file(["FEMUR_LEFT"])
        with self.assertRaises(QCError) as ctx:
            self.store.submit(self.assignment.assignment_id, self.alice, True, upload, confirmed_labels=["NOT_A_BONE"])
        self.assertIn("NOT_A_BONE", ctx.exception.message)
        self.assert_dataset_untouched()

    def test_confirming_a_label_absent_from_the_upload_is_refused(self):
        upload = self.upload_file(["FEMUR_LEFT"])
        with self.assertRaises(QCError) as ctx:
            self.store.submit(
                self.assignment.assignment_id, self.alice, True, upload, confirmed_labels=["FEMUR_LEFT", "SACRUM"]
            )
        self.assertIn("SACRUM", ctx.exception.message)
        self.assert_dataset_untouched()

    def test_a_reviewer_cannot_submit_another_reviewers_assignment(self):
        with self.assertRaises(QCError) as ctx:
            self.store.submit(self.assignment.assignment_id, self.bob, False, None)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_submitting_twice_is_refused(self):
        self.store.submit(self.assignment.assignment_id, self.alice, False, None)
        with self.assertRaises(QCError) as ctx:
            self.store.submit(self.assignment.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_an_expired_assignment_can_still_be_submitted(self):
        """A reviewer who finishes late should not lose their work."""
        self.assignment.state = "expired"
        outcome = self.store.submit(self.assignment.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(outcome.assignment.state, "confirmed")

    def test_an_unknown_assignment_is_a_404(self):
        with self.assertRaises(QCError) as ctx:
            self.store.submit("nope", self.alice, False, None)
        self.assertEqual(ctx.exception.status_code, 404)


class SubjectWithoutSegmentationTests(QCTestCase):
    """A reviewer creating a segmentation where the dataset had none."""

    def test_a_new_segmentation_is_written_and_recorded(self):
        self.builder.add_subject(1, 1, segmentation=None)
        store = self.make_store(include_subjects_without_segmentation=True)
        alice = store.create_user("alice")[0]
        assignment = store.next_subject(alice)
        self.assertFalse(self.builder.segmentation_file(1, 1).exists())

        outcome = store.submit(assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertTrue(self.builder.segmentation_file(1, 1).exists())
        self.assertIsNone(outcome.backup_path, "there was nothing to back up")
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 3})


class PartialUploadTests(QCTestCase):
    """What a confirmed submission does when the client uploads only some of the bones.

    These pin down a sharp edge rather than endorse it. With the default policy a
    confirmed submission is read as the complete truth about the subject: every label
    missing from the uploaded volume is recorded as a confirmed absence (0) and the
    stored segmentation is replaced by what was sent. A 3D Slicer client that exports
    only its visible segments therefore erases the rest. The previous file is kept in
    ``.bonehub_qc/backups/``, and ``mark_removed_labels_absent=False`` disarms the
    Subject_info half of it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(
            1, 1, segmentation={"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2, "TIBIA_LEFT": 2, "SACRUM": 2}
        )

    def test_bones_missing_from_the_upload_are_marked_absent(self):
        store = self.make_store()
        alice = store.create_user("alice")[0]
        assignment = store.next_subject(alice)
        store.submit(assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))

        self.assertEqual(
            self.builder.subject_info(1, 1)["segmentation"],
            {"FEMUR_LEFT": 3, "FEMUR_RIGHT": 0, "TIBIA_LEFT": 0, "SACRUM": 0},
        )
        self.assertEqual(self.builder.labels_in_segmentation(1, 1), {"FEMUR_LEFT"})

    def test_the_overwritten_segmentation_is_recoverable_from_the_backup(self):
        store = self.make_store()
        alice = store.create_user("alice")[0]
        assignment = store.next_subject(alice)
        original = self.builder.segmentation_file(1, 1).read_bytes()

        outcome = store.submit(assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(Path(outcome.backup_path).read_bytes(), original)

    def test_the_policy_flag_keeps_the_other_bones_untouched(self):
        store = self.make_store(mark_removed_labels_absent=False)
        alice = store.create_user("alice")[0]
        assignment = store.next_subject(alice)
        store.submit(assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))

        self.assertEqual(
            self.builder.subject_info(1, 1)["segmentation"],
            {"FEMUR_LEFT": 3, "FEMUR_RIGHT": 2, "TIBIA_LEFT": 2, "SACRUM": 2},
        )


class ExpiredLeaseHandoverTests(QCTestCase):
    """Two reviewers holding one subject, after a lease expired mid-review.

    A reviewer whose lease ran out may still submit, so their work is not lost -- but the
    subject has meanwhile gone to somebody else, and the later submission wins. Both are
    recorded in the audit trail, which is what makes the overlap visible afterwards.
    """

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]
        self.bob = self.store.create_user("bob")[0]

    def test_the_last_submission_wins_and_both_are_logged(self):
        alice_assignment = self.store.next_subject(self.alice)
        alice_assignment.expires_at = (
            (datetime.now(timezone.utc) - timedelta(seconds=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        bob_assignment = self.store.next_subject(self.bob)
        self.assertEqual(bob_assignment.subject_key, alice_assignment.subject_key)

        self.store.submit(alice_assignment.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.store.submit(bob_assignment.assignment_id, self.bob, True, self.upload_file(["FEMUR_RIGHT"]))

        self.assertEqual(self.builder.labels_in_segmentation(1, 1), {"FEMUR_RIGHT"})
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"]["FEMUR_RIGHT"], 3)

        log = self.builder.dataset_log(1).read_text(encoding="utf-8")
        self.assertIn("alice", log)
        self.assertIn("bob", log)


class AuditTrailTests(QCTestCase):
    """"Every submission from the clients must be logged into the dataset folder"."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 2})
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]

    def submissions(self) -> list:
        path = self.state_dir / "submissions.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_a_confirmed_submission_is_written_into_the_dataset_folder(self):
        assignment = self.store.next_subject(self.alice)
        self.store.submit(assignment.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT"]), comment="ok")

        log = self.builder.dataset_log(1).read_text(encoding="utf-8")
        self.assertIn("001_000001", log)
        self.assertIn("alice", log)
        self.assertIn("confirmed", log)
        self.assertIn("ok", log)

    def test_a_rejected_submission_is_logged_too(self):
        assignment = self.store.next_subject(self.alice)
        self.store.submit(assignment.assignment_id, self.alice, False, None, comment="bad contrast")
        log = self.builder.dataset_log(1).read_text(encoding="utf-8")
        self.assertIn("quality_check_confirmed=False", log)
        self.assertIn("bad contrast", log)

    def test_the_machine_readable_trail_records_the_verdict(self):
        assignment = self.store.next_subject(self.alice)
        self.store.submit(assignment.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"]))

        entries = [e for e in self.submissions() if e["kind"] == "submission"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["user"], "alice")
        self.assertEqual(entry["subject_key"], "001_000001")
        self.assertTrue(entry["quality_check_confirmed"])
        self.assertEqual(entry["updated_labels"], {"FEMUR_LEFT": 3, "FEMUR_RIGHT": 3})
        self.assertIn("timestamp", entry)

    def test_assignments_are_recorded_as_well_as_submissions(self):
        assignment = self.store.next_subject(self.alice)
        kinds = [entry["kind"] for entry in self.submissions()]
        self.assertIn("assigned", kinds)
        self.assertIn("user_created", kinds)
        self.store.release_assignment(assignment.assignment_id, self.alice)
        self.assertIn("released", [entry["kind"] for entry in self.submissions()])

    def test_the_trail_reads_back_newest_first_and_can_be_filtered(self):
        first = self.store.next_subject(self.alice)
        self.store.submit(first.assignment_id, self.alice, False, None)
        second = self.store.next_subject(self.alice)
        self.store.submit(second.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT"]))

        recent = self.store.audit.read_recent(limit=10, kind="submission")
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["subject_key"], "001_000002", "newest first")
        self.assertEqual(len(self.store.audit.read_recent(limit=1)), 1)

    def test_each_dataset_gets_its_own_log_next_to_the_data(self):
        self.builder.add_subject(2, 1, segmentation={"FEMUR_LEFT": 2})
        store = self.make_store(max_concurrent_assignments_per_user=5)
        alice = store._users["alice"]
        for _ in range(3):
            assignment = store.next_subject(alice)
            store.submit(assignment.assignment_id, alice, False, None)

        self.assertTrue(self.builder.dataset_log(1).exists())
        self.assertTrue(self.builder.dataset_log(2).exists())
        self.assertIn("002_000001", self.builder.dataset_log(2).read_text(encoding="utf-8"))
        self.assertNotIn("002_000001", self.builder.dataset_log(1).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

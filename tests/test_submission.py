"""What a submission does to the dataset.

The central requirement: "if quality_check_confirmed = True is given by the client, the
labels of those bones must be marked as reviewed. quality_check_confirmed = False then
basically server must not make any changes." In the label statuses of the BoneHub schema,
reviewed is status 2, whatever status the label had before.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import SimpleITK as sitk

from bonehub_data_schema import read_segmentation_labels
from bonehub_quality_check_server.store import QCError

from tests.support import LABEL_VALUE, QCTestCase, segment_header, write_image, write_raw_mask


class ConfirmedSubmissionTests(QCTestCase):
    """quality_check_confirmed = True."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]
        self.assignment = self.store.next_subject(self.alice)

    def test_confirmed_labels_become_reviewed_in_subject_info(self):
        upload = self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"])
        outcome = self.store.submit(self.assignment.assignment_id, self.alice, True, upload)

        self.assertEqual(outcome.updated_labels, {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        stored = self.builder.subject_info(1, 1)["segmentation"]
        self.assertEqual(stored, {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})

    def test_the_reviewed_segmentation_replaces_the_one_in_the_dataset(self):
        upload = self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"])
        self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertEqual(
            self.builder.labels_in_segmentation(1, 1),
            {"FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"},
        )

    def test_only_the_labels_the_reviewer_vouches_for_are_marked_reviewed(self):
        upload = self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.store.submit(self.assignment.assignment_id, self.alice, True, upload, confirmed_labels=["FEMUR_LEFT"])
        stored = self.builder.subject_info(1, 1)["segmentation"]
        self.assertEqual(stored["FEMUR_LEFT"], 2)
        self.assertEqual(stored["FEMUR_RIGHT"], 1, "an unvouched label keeps the status it had")

    def test_a_label_the_reviewer_added_is_recorded_as_not_reviewed(self):
        """A new bone drawn but not confirmed is 'available, not reviewed or corrected'."""
        upload = self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT", "SACRUM"])
        self.store.submit(self.assignment.assignment_id, self.alice, True, upload, confirmed_labels=["FEMUR_LEFT"])
        stored = self.builder.subject_info(1, 1)["segmentation"]
        self.assertEqual(stored["SACRUM"], 1)

    def test_a_label_the_reviewer_added_and_confirmed_is_marked_reviewed(self):
        upload = self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT", "SACRUM"])
        self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"]["SACRUM"], 2)

    def test_a_label_the_reviewer_deleted_becomes_not_available(self):
        """Status 0 means 'not available'."""
        upload = self.upload_file(["FEMUR_LEFT"])
        outcome = self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertEqual(outcome.removed_labels, ["FEMUR_RIGHT"])
        stored = self.builder.subject_info(1, 1)["segmentation"]
        self.assertEqual(stored, {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 0})

    def test_removed_labels_can_be_left_untouched_by_policy(self):
        store = self.make_store(mark_removed_labels_absent=False)
        alice = store._users["alice"]
        assignment = store.get_assignment(self.assignment.assignment_id)
        store.submit(assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"]["FEMUR_RIGHT"], 1)

    def test_the_previous_segmentation_is_backed_up_before_being_overwritten(self):
        upload = self.upload_file(["FEMUR_LEFT"])
        outcome = self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertIsNotNone(outcome.backup_path)
        backups = list((self.state_dir / "backups").rglob("*.seg.nrrd"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].name.startswith("001_000001_"))

    def test_backups_can_be_switched_off(self):
        store = self.make_store(keep_segmentation_backups=False)
        alice = store._users["alice"]
        outcome = store.submit(self.assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertIsNone(outcome.backup_path)
        self.assertEqual(list((self.state_dir / "backups").rglob("*.seg.nrrd")), [])

    def test_the_assignment_is_closed_as_confirmed(self):
        self.store.submit(self.assignment.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT"]))
        assignment = self.store.get_assignment(self.assignment.assignment_id)
        self.assertEqual(assignment.state, "confirmed")
        self.assertTrue(assignment.quality_check_confirmed)
        self.assertTrue(assignment.segmentation_written)
        self.assertIsNotNone(assignment.submitted_at)

    def test_a_confirmed_subject_leaves_the_queue(self):
        self.store.submit(self.assignment.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(self.store.stats().available, 0)
        self.assertEqual([ref.subject_key for ref in self.store._index], [])

    def test_the_upload_itself_is_left_for_the_caller(self):
        """The dataset receives a rewritten copy; the API layer deletes its own spooled file."""
        upload = self.upload_file(["FEMUR_LEFT"])
        self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertTrue(upload.exists())
        self.assertEqual(list((self.state_dir / "tmp").glob("*")), [])

    def test_other_subject_fields_are_preserved(self):
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1}, age=44, gender="F", imaging_modality="CT")
        store = self.make_store()
        alice = store._users["alice"]
        # Finish the subject held from setUp, so the next request hands out subject 2.
        store.submit(self.assignment.assignment_id, alice, False, None)
        assignment = store.next_subject(alice)
        self.assertEqual(assignment.subject_id, 2)
        store.submit(assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))

        entry = self.builder.subject_info(1, 2)
        self.assertEqual((entry["age"], entry["gender"], entry["imaging_modality"]), (44, "F", "CT"))
        self.assertEqual(entry["segmentation"], {"FEMUR_LEFT": 2})

    def test_other_subjects_in_the_file_are_untouched(self):
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1})
        store = self.make_store()
        alice = store._users["alice"]
        store.submit(self.assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"]))
        self.assertEqual(self.builder.subject_info(1, 2)["segmentation"], {"FEMUR_LEFT": 1})
        self.assertEqual(len(self.builder.all_subject_info(1)), 2)


class StatusTransitionTests(QCTestCase):
    """Confirmation always ends at status 2, whatever status a label started from."""

    def confirm(self, segmentation: dict, painted: list, confirmed: list | None = None) -> dict:
        self.builder.add_subject(1, 1, segmentation=segmentation)
        store = self.make_store(eligible_label_values=[1, 2])
        alice = store.create_user("alice")[0]
        assignment = store.next_subject(alice)
        store.submit(assignment.assignment_id, alice, True, self.upload_file(painted), confirmed_labels=confirmed)
        return self.builder.subject_info(1, 1)["segmentation"]

    def test_an_already_reviewed_label_stays_reviewed(self):
        stored = self.confirm({"FEMUR_LEFT": 2, "FEMUR_RIGHT": 1}, ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual(stored, {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})

    def test_a_label_recorded_as_not_available_but_painted_and_confirmed_is_reviewed(self):
        stored = self.confirm({"FEMUR_LEFT": 1, "SACRUM": 0}, ["FEMUR_LEFT", "SACRUM"])
        self.assertEqual(stored["SACRUM"], 2)

    def test_a_label_recorded_as_not_available_but_painted_unconfirmed_is_not_reviewed(self):
        """Painted means available: status 0 would contradict the file."""
        stored = self.confirm({"FEMUR_LEFT": 1, "SACRUM": 0}, ["FEMUR_LEFT", "SACRUM"], confirmed=["FEMUR_LEFT"])
        self.assertEqual(stored, {"FEMUR_LEFT": 2, "SACRUM": 1})

    def test_a_label_already_not_available_is_not_reported_as_removed(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "SACRUM": 0})
        store = self.make_store()
        alice = store.create_user("alice")[0]
        assignment = store.next_subject(alice)
        outcome = store.submit(assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(outcome.removed_labels, [])


class StoredFormatTests(QCTestCase):
    """What reaches the dataset is the canonical BoneHub segmentation, whatever was sent."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]
        self.assignment = self.store.next_subject(self.alice)

    def stored_header(self) -> dict:
        reader = sitk.ImageFileReader()
        reader.SetFileName(str(self.builder.segmentation_file(1, 1)))
        reader.ReadImageInformation()
        return {key: reader.GetMetaData(key) for key in reader.GetMetaDataKeys()}

    def test_the_stored_mask_is_a_bonehub_seg_nrrd(self):
        self.store.submit(self.assignment.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT", "SACRUM"]))
        path = self.builder.segmentation_file(1, 1)
        self.assertTrue(path.name.endswith(".seg.nrrd"))
        self.assertEqual(read_segmentation_labels(path), ["SACRUM", "FEMUR_LEFT"], "numbered in label map order")
        header = self.stored_header()
        self.assertEqual(header["Segment1_Tags"], f"BoneHubLabel:FEMUR_LEFT|BoneHubValue:{LABEL_VALUE['FEMUR_LEFT']}|")

    def test_an_upload_numbered_its_own_way_is_renumbered(self):
        """Segments named only, numbered 7 and 3, listed out of order: stored as 1 and 2."""
        numbers = np.zeros((6, 6, 6), dtype=np.uint8)
        numbers[0, 0:2, 0:2] = 7
        numbers[3, 0:2, 0:2] = 3
        upload = write_raw_mask(
            self.upload_path(), numbers, segment_header((7, "FEMUR_RIGHT", None), (3, "FEMUR_LEFT", None))
        )
        self.store.submit(self.assignment.assignment_id, self.alice, True, upload)

        header = self.stored_header()
        self.assertEqual((header["Segment0_Name"], header["Segment0_LabelValue"]), ("FEMUR_LEFT", "1"))
        self.assertEqual((header["Segment1_Name"], header["Segment1_LabelValue"]), ("FEMUR_RIGHT", "2"))
        self.assertEqual(self.builder.labels_in_segmentation(1, 1), {"FEMUR_LEFT", "FEMUR_RIGHT"})

    def test_segments_of_one_label_are_merged(self):
        numbers = np.zeros((6, 6, 6), dtype=np.uint8)
        numbers[0, 0:2, 0:2] = 1
        numbers[3, 0:2, 0:2] = 2
        copy_tags = f"BoneHubValue:{LABEL_VALUE['FEMUR_LEFT']}|"
        header = segment_header((1, "FEMUR_LEFT", None), (2, "FEMUR_LEFT_copy", copy_tags))
        upload = write_raw_mask(self.upload_path(), numbers, header)
        self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertEqual(read_segmentation_labels(self.builder.segmentation_file(1, 1)), ["FEMUR_LEFT"])

    def test_the_stored_mask_takes_the_images_exact_geometry(self):
        """A client that rounds the geometry within tolerance must not shift the dataset's mask."""
        numbers = np.zeros((6, 6, 6), dtype=np.uint8)
        numbers[0, 0:2, 0:2] = 1
        header = segment_header((1, "FEMUR_LEFT", None))
        upload = write_raw_mask(self.upload_path(), numbers, header, spacing=(1.0002, 1.0002, 1.0002))
        self.store.submit(self.assignment.assignment_id, self.alice, True, upload)

        stored = sitk.ReadImage(str(self.builder.segmentation_file(1, 1)))
        image = sitk.ReadImage(str(self.builder.image_file(1, 1)))
        self.assertEqual(stored.GetSpacing(), image.GetSpacing())
        self.assertEqual(stored.GetOrigin(), image.GetOrigin())
        self.assertEqual(stored.GetDirection(), image.GetDirection())


class RejectedSubmissionTests(QCTestCase):
    """quality_check_confirmed = False must change nothing in the dataset."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
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
        self.assertEqual(list((self.state_dir / "backups").rglob("*.seg.nrrd")), [])

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
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]
        self.bob = self.store.create_user("bob")[0]
        self.assignment = self.store.next_subject(self.alice)
        self.before_bytes = self.builder.segmentation_file(1, 1).read_bytes()

    def assert_dataset_untouched(self) -> None:
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.before_bytes)

    def assert_refused(self, upload: Path, *fragments: str) -> None:
        with self.assertRaises(QCError) as ctx:
            self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertEqual(ctx.exception.status_code, 400)
        for fragment in fragments:
            self.assertIn(fragment, ctx.exception.message)
        self.assert_dataset_untouched()

    def mask(self, *segments, numbers: np.ndarray | None = None) -> Path:
        if numbers is None:
            numbers = np.zeros((6, 6, 6), dtype=np.uint8)
            for index, (number, _name, _tags) in enumerate(segments):
                numbers[index, 0:2, 0:2] = number
        return write_raw_mask(self.upload_path(), numbers, segment_header(*segments))

    def test_confirming_without_a_file_is_refused(self):
        with self.assertRaises(QCError):
            self.store.submit(self.assignment.assignment_id, self.alice, True, None)
        self.assert_dataset_untouched()

    def test_an_empty_segmentation_cannot_be_confirmed(self):
        self.assert_refused(self.upload_file([]), "empty")

    def test_a_file_that_is_not_a_segmentation_is_refused(self):
        bad = self.tmp_path / "not_a_mask.seg.nrrd"
        bad.write_bytes(b"this is not a NRRD file")
        self.assert_refused(bad, ".seg.nrrd")

    def test_a_nifti_segmentation_of_the_old_format_is_refused(self):
        old = write_image(self.tmp_path / "old_client_upload.nii.gz")
        self.assert_refused(old, ".seg.nrrd")

    def test_a_segment_that_is_not_a_bonehub_label_is_refused(self):
        self.assert_refused(self.mask((1, "FEMUR_LEFT", None), (2, "MY_SCRATCH_SEGMENT", None)), "MY_SCRATCH_SEGMENT")

    def test_background_is_not_a_label_a_segment_can_be(self):
        self.assert_refused(self.mask((1, "BACKGROUND", None)), "BACKGROUND")

    def test_a_tag_naming_no_bonehub_label_is_refused(self):
        self.assert_refused(self.mask((1, "Segment_1", "BoneHubValue:31337|")), "31337")

    def test_a_tag_is_enough_to_name_a_segment(self):
        """As in the schema's reader: the BoneHubValue tag, else the segment name."""
        upload = self.mask((1, "Segment_1", f"BoneHubValue:{LABEL_VALUE['FEMUR_LEFT']}|"))
        outcome = self.store.submit(self.assignment.assignment_id, self.alice, True, upload)
        self.assertEqual(outcome.updated_labels, {"FEMUR_LEFT": 2})

    def test_a_segment_named_one_label_and_tagged_another_is_refused(self):
        tags = f"BoneHubLabel:FEMUR_LEFT|BoneHubValue:{LABEL_VALUE['FEMUR_LEFT']}|"
        self.assert_refused(self.mask((1, "FEMUR_RIGHT", tags)), "FEMUR_RIGHT", "FEMUR_LEFT")

    def test_voxels_the_header_does_not_describe_are_refused(self):
        numbers = np.zeros((6, 6, 6), dtype=np.uint8)
        numbers[0, 0:2, 0:2] = 1
        numbers[1, 0:2, 0:2] = 5
        self.assert_refused(self.mask((1, "FEMUR_LEFT", None), numbers=numbers), "[5]")

    def test_two_labels_sharing_a_segment_number_are_refused(self):
        self.assert_refused(self.mask((1, "FEMUR_LEFT", None), (1, "FEMUR_RIGHT", None)), "segment number 1")

    def test_a_multi_layer_segmentation_is_refused(self):
        """Slicer stores overlapping segments as layers; the dataset format has one."""
        layers = sitk.Compose([sitk.GetImageFromArray(np.ones((6, 6, 6), dtype=np.uint8))] * 2)
        path = self.upload_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for key, value in segment_header((1, "FEMUR_LEFT", None)).items():
            layers.SetMetaData(key, str(value))
        sitk.WriteImage(layers, str(path), useCompression=True)
        self.assert_refused(path, "layers")

    def test_a_segmentation_whose_shape_differs_from_the_image_is_refused(self):
        self.assert_refused(self.upload_file(["FEMUR_LEFT"], shape=(5, 5, 5)), "does not match")

    def test_a_segmentation_whose_affine_differs_from_the_image_is_refused(self):
        self.assert_refused(self.upload_file(["FEMUR_LEFT"], spacing=(2.0, 2.0, 2.0)), "affine")

    def test_the_geometry_check_can_be_switched_off(self):
        store = self.make_store(require_geometry_match=False)
        alice = store._users["alice"]
        upload = self.upload_file(["FEMUR_LEFT"], shape=(5, 5, 5))
        store.submit(self.assignment.assignment_id, alice, True, upload)
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"]["FEMUR_LEFT"], 2)

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

    def test_a_dataset_regenerated_under_another_schema_is_not_written(self):
        """The dataset changed format while the subject was out for review."""
        self.builder.add_dataset(1, schema_version="0.2.0")
        with self.assertRaises(QCError) as ctx:
            self.store.submit(self.assignment.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("0.2.0", ctx.exception.message)
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.before_bytes)


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
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2})


class PartialUploadTests(QCTestCase):
    """What a confirmed submission does when the client uploads only some of the bones.

    These pin down a sharp edge rather than endorse it. With the default policy a
    confirmed submission is read as the complete truth about the subject: every label
    missing from the uploaded volume is recorded as not available (0) and the stored
    segmentation is replaced by what was sent. A 3D Slicer client that exports only its
    visible segments therefore erases the rest. The previous file is kept in
    ``.bonehub_qc/backups/``, and ``mark_removed_labels_absent=False`` disarms the
    Subject_info half of it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(
            1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1, "TIBIA_LEFT": 1, "SACRUM": 1}
        )

    def test_bones_missing_from_the_upload_are_marked_not_available(self):
        store = self.make_store()
        alice = store.create_user("alice")[0]
        assignment = store.next_subject(alice)
        store.submit(assignment.assignment_id, alice, True, self.upload_file(["FEMUR_LEFT"]))

        self.assertEqual(
            self.builder.subject_info(1, 1)["segmentation"],
            {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 0, "TIBIA_LEFT": 0, "SACRUM": 0},
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
            {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 1, "TIBIA_LEFT": 1, "SACRUM": 1},
        )


class ExpiredLeaseHandoverTests(QCTestCase):
    """Two reviewers holding one subject, after a lease expired mid-review.

    A reviewer whose lease ran out may still submit, so their work is not lost -- but the
    subject has meanwhile gone to somebody else, and the later submission wins. Both are
    recorded in the audit trail, which is what makes the overlap visible afterwards.
    """

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
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
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"]["FEMUR_RIGHT"], 2)

        log = self.builder.dataset_log(1).read_text(encoding="utf-8")
        self.assertIn("alice", log)
        self.assertIn("bob", log)


class AuditTrailTests(QCTestCase):
    """"Every submission from the clients must be logged into the dataset folder"."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1})
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
        self.assertEqual(entry["updated_labels"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.assertTrue(entry["segmentation_path"].endswith("001_000001.seg.nrrd"))
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
        self.builder.add_subject(2, 1, segmentation={"FEMUR_LEFT": 1})
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

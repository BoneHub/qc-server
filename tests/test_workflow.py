"""A subject's way through the quality check: reviewers first, editors for what they reject.

Requirements under test: "collect all the submissions in the server created workspace folder
[...] only after admin approval, things are overwritten in the bonehub dataset"; "all the bone
labels whose value is 1 need review and they must be passed to the reviewers"; "reviewer rejects
a segmentation quality for a bone label (either bad quality or should not even exist for that
subject) -> server tags it as a subject that must be passed to editor"; "reviewer accepts a
segmentation quality for a bone label -> server tags it as accepted -> keeps it for the final
admin approval"; "all the rejected subjects by the reviewers are leased to the editors ->
editors submit the new segmentations -> server tags them as subjects that need to be reviewed
after edit (optional and controllable in admin panel [...]) -> passed to reviewers or waits for
admin final approval".

The approval itself is in ``test_approval.py``.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from bonehub_quality_check_server import segmentation as segmentation_module
from bonehub_quality_check_server.models import EDITOR, REVIEWER
from bonehub_quality_check_server.store import QCError

from tests.support import QCTestCase, labels_in_mask, write_mask

KEY = "001_000001"


class WorkflowTestCase(QCTestCase):
    """One subject whose two labels nobody has reviewed yet, a reviewer and an editor."""

    config: dict = {}

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        self.store = self.make_store(**self.config)
        self.rita = self.store.create_user("rita", roles=[REVIEWER])[0]
        self.eddie = self.store.create_user("eddie", roles=[EDITOR])[0]

    def case(self):
        return self.store.case_of(KEY)

    def states(self) -> dict:
        return {name: label.state for name, label in self.case().labels.items()}

    def reject_right_femur(self, reason: str = "quality"):
        return self.review(self.store, self.rita, rejected={"FEMUR_RIGHT": reason}, comment="femoral head cut off")


class NothingReachesTheDatasetTests(WorkflowTestCase):
    """Verdicts are kept in the server's state folder; the dataset waits for the approval."""

    def test_a_review_changes_nothing_in_the_dataset(self):
        before = self.dataset_state()
        self.reject_right_femur()
        self.assertEqual(self.dataset_state(), before)

    def test_a_correction_changes_nothing_in_the_dataset(self):
        before = self.dataset_state()
        self.reject_right_femur()
        self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"])
        self.assertEqual(self.dataset_state(), before)
        self.assertTrue((self.state_dir / "staged" / "Dataset_001" / f"{KEY}.seg.nrrd").exists())

    def test_accepting_everything_changes_nothing_in_the_dataset_either(self):
        before = self.dataset_state()
        outcome = self.review(self.store, self.rita)
        self.assertEqual(outcome.stage, "approval")
        self.assertEqual(self.dataset_state(), before)

    def test_the_dataset_gets_no_log_line_before_the_approval(self):
        self.reject_right_femur()
        self.assertFalse(self.builder.dataset_log(1).exists())
        self.assertIn(KEY, (self.state_dir / "server.log").read_text(encoding="utf-8"))

    def test_the_verdicts_are_in_the_state_folder(self):
        self.reject_right_femur()
        stored = json.loads((self.state_dir / "cases.json").read_text(encoding="utf-8"))
        self.assertEqual([case["subject_key"] for case in stored], [KEY])
        self.assertEqual(stored[0]["stage"], "edit")
        self.assertEqual(stored[0]["labels"]["FEMUR_RIGHT"]["state"], "rejected")
        self.assertEqual(stored[0]["events"][0]["comment"], "femoral head cut off")

    def test_a_case_survives_a_restart(self):
        self.reject_right_femur()
        reopened = self.make_store()
        self.assertEqual(reopened.case_of(KEY).model_dump(), self.case().model_dump())


class ReviewersFirstTests(QCTestCase):
    """Labels nobody has reviewed yet go to the reviewers, not to the editors."""

    def open(self, **config):
        store = self.make_store(**config)
        return store, store.create_user("rita", roles=[REVIEWER])[0], store.create_user("eddie", roles=[EDITOR])[0]

    def test_a_subject_nobody_has_looked_at_goes_to_a_reviewer(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        store, rita, eddie = self.open()
        with self.assertRaises(QCError) as ctx:
            store.next_subject(eddie, EDITOR)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("an editor", ctx.exception.message)
        self.assertEqual(store.next_subject(rita, REVIEWER).subject_key, KEY)

    def test_only_labels_not_reviewed_yet_are_under_review(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 2, "FEMUR_RIGHT": 1})
        store, rita, _ = self.open()
        case = store.handout_case(store.next_subject(rita, REVIEWER))
        self.assertEqual({name: label.state for name, label in case.labels.items()},
                         {"FEMUR_LEFT": "kept", "FEMUR_RIGHT": "pending"})

    def test_labels_already_reviewed_are_under_review_when_the_policy_says_so(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 2, "FEMUR_RIGHT": 1})
        store, rita, _ = self.open(eligible_label_values=[1, 2])
        case = store.handout_case(store.next_subject(rita, REVIEWER))
        self.assertEqual({label.state for label in case.labels.values()}, {"pending"})

    def test_a_painted_label_the_dataset_does_not_list_is_under_review(self):
        """Nobody has looked at it: the file paints it, and Subject_info says nothing of it."""
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1}, write_segmentation_file=False)
        write_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT", "SACRUM"])
        store, rita, _ = self.open()
        case = store.handout_case(store.next_subject(rita, REVIEWER))
        self.assertEqual(case.labels["SACRUM"].state, "pending")

    def test_a_listed_label_the_segmentation_does_not_paint_is_removed(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1}, write_segmentation_file=False)
        write_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT"])
        store, rita, _ = self.open()
        case = store.handout_case(store.next_subject(rita, REVIEWER))
        self.assertEqual(case.labels["FEMUR_RIGHT"].state, "removed")

    def test_a_subject_without_a_segmentation_goes_straight_to_the_editors(self):
        self.builder.add_subject(1, 1, segmentation=None)
        store, rita, eddie = self.open(include_subjects_without_segmentation=True)
        with self.assertRaises(QCError):
            store.next_subject(rita, REVIEWER)
        assignment = store.next_subject(eddie, EDITOR)
        self.assertEqual(store.handout_case(assignment).stage, "edit")


class ReviewVerdictTests(WorkflowTestCase):
    def test_accepting_every_label_sends_the_subject_to_the_administrator(self):
        outcome = self.review(self.store, self.rita)
        self.assertEqual(outcome.stage, "approval")
        self.assertEqual(outcome.accepted_labels, ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual(self.states(), {"FEMUR_LEFT": "accepted", "FEMUR_RIGHT": "accepted"})
        self.assertEqual(self.case().labels["FEMUR_LEFT"].by, "rita")
        self.assertEqual(self.store.get_assignment(outcome.assignment.assignment_id).state, "submitted")

    def test_a_rejected_label_sends_the_subject_to_the_editors(self):
        outcome = self.reject_right_femur()
        self.assertEqual(outcome.stage, "edit")
        self.assertEqual(outcome.rejected_labels, {"FEMUR_RIGHT": "quality"})
        self.assertEqual(self.states(), {"FEMUR_LEFT": "accepted", "FEMUR_RIGHT": "rejected"})
        self.assertEqual(self.case().labels["FEMUR_RIGHT"].reason, "quality")

    def test_a_label_that_should_not_be_there_is_rejected_as_such(self):
        self.reject_right_femur(reason="absent")
        self.assertEqual(self.case().labels["FEMUR_RIGHT"].reason, "absent")
        self.assertEqual(self.case().stage, "edit")

    def test_rejecting_the_subject_rejects_every_label_under_review(self):
        assignment = self.store.next_subject(self.rita, REVIEWER)
        outcome = self.store.submit(assignment.assignment_id, self.rita, False, None, comment="wrong patient")
        self.assertEqual(outcome.stage, "edit")
        self.assertEqual(outcome.rejected_labels, {"FEMUR_LEFT": "quality", "FEMUR_RIGHT": "quality"})

    def test_a_missing_bone_sends_the_subject_to_the_editors(self):
        outcome = self.review(self.store, self.rita, missing=["TIBIA_LEFT"], comment="the tibia is in the scan")
        self.assertEqual(outcome.stage, "edit")
        self.assertEqual(outcome.missing_labels, ["TIBIA_LEFT"])
        self.assertEqual(self.states(), {"FEMUR_LEFT": "accepted", "FEMUR_RIGHT": "accepted", "TIBIA_LEFT": "rejected"})
        label = self.case().labels["TIBIA_LEFT"]
        self.assertEqual((label.painted, label.reason, label.by), (False, "missing", "rita"))
        self.assertEqual(self.case().events[-1].comment, "the tibia is in the scan")

    def test_a_listed_bone_that_is_not_painted_can_be_reported_missing(self):
        """Subject_info says the dataset has it; the file does not paint it."""
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1, "SACRUM": 1}, write_segmentation_file=False)
        write_mask(self.builder.segmentation_file(1, 2), ["FEMUR_LEFT"])
        store = self.make_store()
        self.review(store, self.rita)  # subject 1
        outcome = self.review(store, self.rita, rejected={"SACRUM": "missing"})
        self.assertEqual(outcome.stage, "edit")
        self.assertEqual(store.case_of("001_000002").labels["SACRUM"].reason, "missing")

    def test_a_label_left_unjudged_keeps_the_subject_waiting_for_a_reviewer(self):
        outcome = self.review(self.store, self.rita, accepted=["FEMUR_LEFT"])
        self.assertEqual(outcome.stage, "review")
        self.assertEqual(self.states(), {"FEMUR_LEFT": "accepted", "FEMUR_RIGHT": "pending"})
        bob = self.store.create_user("bob", roles=[REVIEWER])[0]
        self.assertEqual(self.store.next_subject(bob, REVIEWER).subject_key, KEY)

    def test_a_label_the_dataset_has_as_reviewed_can_still_be_rejected(self):
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 2, "FEMUR_RIGHT": 1})
        store = self.make_store()
        self.review(store, self.rita)  # subject 1 first
        outcome = self.review(store, self.rita, rejected={"FEMUR_LEFT": "quality"})
        self.assertEqual(outcome.stage, "edit")
        self.assertEqual(store.case_of("001_000002").labels["FEMUR_LEFT"].state, "rejected")

    def test_a_subject_waiting_for_an_editor_is_not_handed_to_a_reviewer(self):
        self.reject_right_femur()
        bob = self.store.create_user("bob", roles=[REVIEWER])[0]
        with self.assertRaises(QCError) as ctx:
            self.store.next_subject(bob, REVIEWER)
        self.assertEqual(ctx.exception.status_code, 404)

    def assert_refused(self, fragment: str, **verdict) -> None:
        assignment = self.store.next_subject(self.rita, REVIEWER)
        with self.assertRaises(QCError) as ctx:
            self.store.submit(assignment.assignment_id, self.rita, True, None, use_stored_segmentation=True, **verdict)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn(fragment, ctx.exception.message)
        self.assertIsNone(self.case(), "nothing was recorded")

    def test_a_label_not_in_the_segmentation_cannot_be_rejected(self):
        self.assert_refused("TIBIA_LEFT", rejected_labels={"TIBIA_LEFT": "quality"})

    def test_a_painted_label_cannot_be_reported_missing(self):
        self.assert_refused("Reject them", missing_labels=["FEMUR_RIGHT"])

    def test_a_reason_must_be_one_the_server_knows(self):
        self.assert_refused("ugly", rejected_labels={"FEMUR_RIGHT": "ugly"})

    def test_a_label_cannot_be_accepted_and_rejected_at_once(self):
        self.assert_refused("both", confirmed_labels=["FEMUR_RIGHT"], rejected_labels={"FEMUR_RIGHT": "quality"})

    def test_a_verdict_must_judge_something(self):
        self.assert_refused("no label", confirmed_labels=[])

    def test_an_unknown_label_name_is_refused(self):
        self.assert_refused("NOT_A_BONE", missing_labels=["NOT_A_BONE"])


class EditTests(WorkflowTestCase):
    """What an editor's correction does to each label."""

    def setUp(self) -> None:
        super().setUp()
        self.reject_right_femur()

    def test_the_editor_is_handed_the_rejected_subject_and_told_why(self):
        assignment = self.store.next_subject(self.eddie, EDITOR)
        case = self.store.handout_case(assignment)
        self.assertEqual(case.stage, "edit")
        label = case.labels["FEMUR_RIGHT"]
        self.assertEqual((label.state, label.reason, label.by), ("rejected", "quality", "rita"))
        self.assertEqual(case.events[0].comment, "femoral head cut off")

    def test_the_corrected_labels_go_back_to_a_reviewer_by_default(self):
        outcome = self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"])
        self.assertEqual(outcome.stage, "review")
        self.assertEqual(outcome.edited_labels, ["FEMUR_RIGHT"])
        self.assertEqual(outcome.pending_labels, ["FEMUR_RIGHT"])
        self.assertEqual(self.states(), {"FEMUR_LEFT": "accepted", "FEMUR_RIGHT": "pending"})
        self.assertEqual(self.case().labels["FEMUR_RIGHT"].edited_by, "eddie")
        self.assertEqual(self.case().labels["FEMUR_LEFT"].by, "rita", "the untouched label keeps its acceptance")

    def test_the_next_reviewer_sees_the_correction(self):
        self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"])
        path = self.store.current_segmentation_path(1, 1)
        self.assertEqual(path, self.state_dir / "staged" / "Dataset_001" / f"{KEY}.seg.nrrd")
        self.assertEqual(labels_in_mask(path), {"FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"})
        assignment = self.store.next_subject(self.rita, REVIEWER)
        self.assertEqual(assignment.subject_key, KEY)
        self.assertEqual([s.label for s in self.store.segment_table(1, 1)], ["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"])

    def test_a_correction_that_spills_into_an_accepted_label_takes_its_acceptance_away(self):
        self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual(self.states(), {"FEMUR_LEFT": "pending", "FEMUR_RIGHT": "pending"})

    def test_a_rejected_label_the_editor_left_as_it_was_is_reviewed_again(self):
        """The editor disagreed with the reviewer; somebody must look again."""
        outcome = self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"])
        self.assertEqual(outcome.edited_labels, ["FEMUR_RIGHT"])
        self.assertEqual(self.states(), {"FEMUR_LEFT": "accepted", "FEMUR_RIGHT": "pending"})

    def test_an_editor_is_never_handed_a_subject_waiting_for_a_review(self):
        self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"])
        with self.assertRaises(QCError) as ctx:
            self.store.next_subject(self.eddie, EDITOR)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_nobody_reviews_their_own_correction(self):
        alice = self.store.create_user("alice")[0]
        self.edit(self.store, alice, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"])
        with self.assertRaises(QCError) as ctx:
            self.store.next_subject(alice, REVIEWER)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.store.next_subject(self.rita, REVIEWER).subject_key, KEY)

    def test_an_editor_who_cannot_correct_it_sends_it_to_the_administrator(self):
        assignment = self.store.next_subject(self.eddie, EDITOR)
        outcome = self.store.submit(assignment.assignment_id, self.eddie, False, None, comment="the image is cropped")
        self.assertEqual(outcome.stage, "escalated")
        self.assertIn("administrator", outcome.message)
        for user, role in ((self.eddie, EDITOR), (self.rita, REVIEWER)):
            with self.assertRaises(QCError):
                self.store.next_subject(user, role)

    def test_an_editor_not_sent_the_segmentation_is_not_handed_one_to_correct(self):
        ian = self.store.create_user("ian", roles=[EDITOR], data_access="image")[0]
        with self.assertRaises(QCError) as ctx:
            self.store.next_subject(ian, EDITOR)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_an_upload_the_server_cannot_compare_counts_as_changing_everything(self):
        """Without the comparison, keeping an acceptance would be a guess."""
        with mock.patch("bonehub_quality_check_server.store.changed_labels", return_value=None) as compare:
            self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"])
        compare.assert_called_once()
        self.assertEqual(self.states(), {"FEMUR_LEFT": "pending", "FEMUR_RIGHT": "pending"})


class EditsNeedNoReviewTests(WorkflowTestCase):
    """The admin panel's switch: an editor's correction may be accepted as it is."""

    config = {"edits_need_review": False}

    def setUp(self) -> None:
        super().setUp()
        self.reject_right_femur()

    def test_the_corrections_the_editor_vouches_for_are_accepted(self):
        outcome = self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"])
        self.assertEqual(outcome.stage, "approval")
        label = self.case().labels["FEMUR_RIGHT"]
        self.assertEqual((label.state, label.by, label.edited_by), ("accepted", "eddie", "eddie"))

    def test_a_correction_the_editor_does_not_vouch_for_waits_for_a_reviewer(self):
        outcome = self.edit(
            self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"], confirmed=["FEMUR_LEFT"]
        )
        self.assertEqual(outcome.stage, "review")
        self.assertEqual(self.states(), {"FEMUR_LEFT": "accepted", "FEMUR_RIGHT": "pending"})

    def test_a_label_nobody_reviewed_still_waits_for_a_reviewer(self):
        """An editor's word counts for what they corrected, not for a review nobody gave."""
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})
        store = self.make_store(edits_need_review=False)
        # rita judged subject 1 in setUp; on subject 2 she only reports a missing bone.
        self.review(store, self.rita, accepted=["FEMUR_LEFT"], missing=["TIBIA_LEFT"])
        self.edit(store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"])  # subject 1
        outcome = self.edit(store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"])  # subject 2
        self.assertEqual(outcome.stage, "review")
        states = {name: label.state for name, label in store.case_of("001_000002").labels.items()}
        self.assertEqual(states, {"FEMUR_LEFT": "accepted", "FEMUR_RIGHT": "pending", "TIBIA_LEFT": "accepted"})


class MissingBoneTests(WorkflowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.review(self.store, self.rita, missing=["TIBIA_LEFT"])

    def test_an_editor_adds_the_bone_and_a_reviewer_checks_it(self):
        assignment = self.store.next_subject(self.eddie, EDITOR)
        label = self.store.handout_case(assignment).labels["TIBIA_LEFT"]
        self.assertEqual((label.state, label.reason, label.painted), ("rejected", "missing", False))
        self.store.submit(assignment.assignment_id, self.eddie, True, self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"]))
        self.assertEqual(self.states(), {"FEMUR_LEFT": "accepted", "FEMUR_RIGHT": "accepted", "TIBIA_LEFT": "pending"})
        self.assertTrue(self.case().labels["TIBIA_LEFT"].painted)
        self.assertEqual(self.case().stage, "review")

    def test_a_bone_the_editor_did_not_add_goes_back_to_a_reviewer(self):
        """The editor disagreed: the tibia is not in the scan after all."""
        self.edit(self.store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], comment="no tibia in this scan")
        label = self.case().labels["TIBIA_LEFT"]
        self.assertEqual((label.state, label.painted, label.edited_by), ("pending", False, "eddie"))
        self.assertEqual(self.case().stage, "review")


class RemovalTests(WorkflowTestCase):
    """A label an editor takes out of the segmentation."""

    def test_a_label_a_reviewer_said_should_not_be_there_is_removed(self):
        self.reject_right_femur(reason="absent")
        outcome = self.edit(self.store, self.eddie, ["FEMUR_LEFT"])
        self.assertEqual(outcome.removed_labels, ["FEMUR_RIGHT"])
        self.assertEqual(outcome.stage, "approval", "nothing is left to review")
        self.assertEqual(self.states(), {"FEMUR_LEFT": "accepted", "FEMUR_RIGHT": "removed"})

    def test_a_removal_nobody_asked_for_waits_for_a_reviewer(self):
        """A bone dropped from the upload -- on purpose or not -- is an edit like any other."""
        self.reject_right_femur()
        self.edit(self.store, self.eddie, ["FEMUR_RIGHT"], grown=["FEMUR_RIGHT"])
        label = self.case().labels["FEMUR_LEFT"]
        self.assertEqual((label.state, label.painted, label.edited_by), ("pending", False, "eddie"))
        self.assertEqual(self.case().stage, "review")

    def test_a_reviewer_agrees_to_a_removal_by_accepting_it(self):
        self.reject_right_femur()
        self.edit(self.store, self.eddie, ["FEMUR_LEFT"])
        outcome = self.review(self.store, self.rita)
        self.assertEqual(outcome.stage, "approval")
        self.assertEqual(self.states(), {"FEMUR_LEFT": "accepted", "FEMUR_RIGHT": "removed"})

    def test_a_reviewer_undoes_a_removal_by_reporting_the_bone_missing(self):
        self.reject_right_femur()
        self.edit(self.store, self.eddie, ["FEMUR_LEFT"])
        outcome = self.review(self.store, self.rita, rejected={"FEMUR_RIGHT": "missing"})
        self.assertEqual(outcome.stage, "edit")
        self.assertEqual(self.case().labels["FEMUR_RIGHT"].reason, "missing")

    def test_with_edits_needing_no_review_a_removal_is_final(self):
        store = self.make_store(edits_need_review=False)
        self.review(store, self.rita, rejected={"FEMUR_RIGHT": "quality"})
        outcome = self.edit(store, self.eddie, ["FEMUR_LEFT"])
        self.assertEqual(outcome.stage, "approval")
        self.assertEqual(store.case_of(KEY).labels["FEMUR_RIGHT"].state, "removed")


class LeasesPerRoleTests(WorkflowTestCase):
    """A user with both roles works in both clients, a subject at a time in each."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1})
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]
        self.reject_right_femur()  # subject 1 waits for an editor, subject 2 for a reviewer

    def test_one_subject_to_review_and_one_to_edit_at_the_same_time(self):
        to_review = self.store.next_subject(self.alice, REVIEWER)
        to_edit = self.store.next_subject(self.alice, EDITOR)
        self.assertEqual((to_review.subject_key, to_edit.subject_key), ("001_000002", KEY))
        self.assertEqual((to_review.role, to_edit.role), (REVIEWER, EDITOR))

    def test_each_client_lists_what_it_was_handed(self):
        to_review = self.store.next_subject(self.alice, REVIEWER)
        to_edit = self.store.next_subject(self.alice, EDITOR)
        self.assertEqual([a.assignment_id for a in self.store.open_assignments_of("alice", REVIEWER)], [to_review.assignment_id])
        self.assertEqual([a.assignment_id for a in self.store.open_assignments_of("alice", EDITOR)], [to_edit.assignment_id])

    def test_a_subject_is_submitted_in_the_role_it_was_handed_out_in(self):
        to_edit = self.store.next_subject(self.alice, EDITOR)
        with self.assertRaises(QCError) as ctx:
            self.store.submit(to_edit.assignment_id, self.alice, True, None, use_stored_segmentation=True, role=REVIEWER)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("3D Slicer", ctx.exception.message)
        with self.assertRaises(QCError) as ctx:
            self.store.submit(to_edit.assignment_id, self.alice, True, None, use_stored_segmentation=True)
        self.assertEqual(ctx.exception.status_code, 409, "an editor uploads, even to agree")
        to_review = self.store.next_subject(self.alice, REVIEWER)
        with self.assertRaises(QCError) as ctx:
            self.store.submit(to_review.assignment_id, self.alice, True, self.upload_file(["FEMUR_LEFT"]))
        self.assertEqual(ctx.exception.status_code, 409, "a review judges the segmentation as it is")

    def test_a_role_taken_away_stops_the_verdict_in_it(self):
        to_edit = self.store.next_subject(self.alice, EDITOR)
        self.store.update_user("alice", roles=[REVIEWER])
        alice = self.store._users["alice"]
        with self.assertRaises(QCError) as ctx:
            self.store.submit(to_edit.assignment_id, alice, False, None)
        self.assertEqual(ctx.exception.status_code, 403)


class LateVerdictTests(WorkflowTestCase):
    """A lease that ran out: the verdict counts only while the subject is still the user's."""

    def expire(self, assignment) -> None:
        assignment.expires_at = "2000-01-01T00:00:00Z"

    def test_a_late_verdict_on_a_subject_nobody_else_took_counts(self):
        assignment = self.store.next_subject(self.rita, REVIEWER)
        self.expire(assignment)
        self.store.stats()  # the lease expires
        outcome = self.store.submit(assignment.assignment_id, self.rita, True, None, use_stored_segmentation=True)
        self.assertEqual(outcome.stage, "approval")

    def test_a_late_verdict_on_a_subject_handed_to_someone_else_is_refused(self):
        assignment = self.store.next_subject(self.rita, REVIEWER)
        self.expire(assignment)
        bob = self.store.create_user("bob", roles=[REVIEWER])[0]
        self.assertEqual(self.store.next_subject(bob, REVIEWER).subject_key, KEY)
        with self.assertRaises(QCError) as ctx:
            self.store.submit(assignment.assignment_id, self.rita, True, None, use_stored_segmentation=True)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("handed out again", ctx.exception.message)
        self.assertIsNone(self.case())

    def test_a_verdict_on_a_subject_that_moved_on_is_refused(self):
        """The administrator took the subject back and someone else judged it meanwhile."""
        assignment = self.store.next_subject(self.rita, REVIEWER)
        self.store.release_assignment(assignment.assignment_id)
        bob = self.store.create_user("bob", roles=[REVIEWER])[0]
        self.review(self.store, bob, rejected={"FEMUR_LEFT": "quality"})
        assignment.state = "expired"  # as if rita's lease had merely run out
        with self.assertRaises(QCError) as ctx:
            self.store.submit(assignment.assignment_id, self.rita, True, None, use_stored_segmentation=True)
        self.assertEqual(ctx.exception.status_code, 409)


class ChangedLabelsTests(QCTestCase):
    """Which labels an upload changed, voxel by voxel, whatever numbers the files use."""

    def setUp(self) -> None:
        super().setUp()
        self.base = write_mask(self.tmp_path / "base.seg.nrrd", ["FEMUR_LEFT", "FEMUR_RIGHT"])

    def changed(self, labels, **kwargs):
        upload = segmentation_module.read_segmentation_upload(self.upload_file(labels, **kwargs))
        return segmentation_module.changed_labels(self.base, upload)

    def test_the_same_voxels_change_nothing(self):
        self.assertEqual(self.changed(["FEMUR_LEFT", "FEMUR_RIGHT"]), set())

    def test_a_voxel_more_changes_its_label(self):
        self.assertEqual(self.changed(["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"]), {"FEMUR_RIGHT"})

    def test_a_label_added_or_taken_away_changed(self):
        self.assertEqual(self.changed(["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"]), {"TIBIA_LEFT"})
        self.assertEqual(self.changed(["FEMUR_LEFT"]), {"FEMUR_RIGHT"})

    def test_segment_numbers_do_not_matter(self):
        """The base numbers FEMUR_RIGHT 2; an upload holding it alone numbers it 1."""
        base = write_mask(self.tmp_path / "right.seg.nrrd", ["FEMUR_LEFT", "FEMUR_RIGHT"])
        only_right = write_mask(self.upload_path(), ["FEMUR_RIGHT"])
        upload = segmentation_module.read_segmentation_upload(only_right)
        self.assertEqual(segmentation_module.changed_labels(base, upload), {"FEMUR_LEFT"})

    def test_grids_of_different_sizes_cannot_be_compared(self):
        self.assertIsNone(self.changed(["FEMUR_LEFT", "FEMUR_RIGHT"], shape=(5, 5, 5)))


if __name__ == "__main__":
    unittest.main()

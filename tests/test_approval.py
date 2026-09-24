"""The administrator's approval, the one step that writes into the dataset, and the other
things the administrator does with a subject in progress.

Requirement under test: "in admin panel, admin can approve all the submission and only after
admin approval, things are overwritten in the bonehub dataset".
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from bonehub_quality_check_server.models import EDITOR, REVIEWER
from bonehub_quality_check_server.store import QCError

from tests.support import QCTestCase, labels_in_mask, write_mask
from tests.test_api import ApiTestCase

KEY = "001_000001"


class ApprovalTestCase(QCTestCase):
    """One subject whose two labels nobody has reviewed yet, a reviewer and an editor."""

    config: dict = {}

    def setUp(self) -> None:
        super().setUp()
        self.prepare()
        self.store = self.make_store(**self.config)
        self.rita = self.store.create_user("rita", roles=[REVIEWER])[0]
        self.eddie = self.store.create_user("eddie", roles=[EDITOR])[0]
        self.original = self.builder.segmentation_file(1, 1).read_bytes()

    def prepare(self) -> None:
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1})

    def statuses(self, subject_id: int = 1) -> dict:
        return self.builder.subject_info(1, subject_id)["segmentation"]

    def correct_right_femur(self, store=None) -> None:
        """rita rejects the right femur, eddie corrects it, and rita accepts the correction."""
        store = store or self.store
        self.review(store, self.rita, rejected={"FEMUR_RIGHT": "quality"})
        self.edit(store, self.eddie, ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_RIGHT"], comment="head redrawn")
        self.review(store, self.rita)


class ApproveAcceptedSubjectTests(ApprovalTestCase):
    def test_accepted_labels_become_reviewed_and_the_file_stays_as_it_is(self):
        self.review(self.store, self.rita)
        outcome = self.store.approve(KEY)
        self.assertEqual(outcome.updated_labels, {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.assertFalse(outcome.segmentation_written)
        self.assertEqual(self.statuses(), {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.original)
        self.assertEqual(self.store.case_of(KEY).stage, "applied")

    def test_the_approval_is_logged_next_to_the_dataset(self):
        self.review(self.store, self.rita, comment="clean")
        self.store.approve(KEY)
        log = self.builder.dataset_log(1).read_text(encoding="utf-8")
        self.assertIn(f"Subject {KEY} approved", log)
        self.assertIn("accepted by 'rita'", log)

    def test_the_audit_trail_says_who_stood_behind_each_label(self):
        self.correct_right_femur()
        self.store.approve(KEY)
        entry = self.store.audit.read_recent(kind="approved")[0]
        self.assertEqual(entry["verdicts"]["FEMUR_LEFT"]["by"], "rita")
        self.assertEqual(entry["verdicts"]["FEMUR_RIGHT"]["edited_by"], "eddie")
        self.assertTrue(entry["segmentation_written"])

    def test_other_subjects_and_fields_are_left_as_they_were(self):
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1}, age=44, gender="F")
        store = self.make_store()
        self.review(store, self.rita)
        store.approve(KEY)
        entry = self.builder.subject_info(1, 2)
        self.assertEqual((entry["segmentation"], entry["age"], entry["gender"]), ({"FEMUR_LEFT": 1}, 44, "F"))
        self.assertEqual(len(self.builder.all_subject_info(1)), 2)


class LabelStatusTests(QCTestCase):
    """What an approval writes for each kind of label."""

    def approve(self, segmentation: dict, painted: list, **config) -> dict:
        self.builder.add_subject(1, 1, segmentation=segmentation, write_segmentation_file=False)
        write_mask(self.builder.segmentation_file(1, 1), painted)
        store = self.make_store(**config)
        rita = store.create_user("rita", roles=[REVIEWER])[0]
        self.review(store, rita)
        store.approve(KEY)
        return self.builder.subject_info(1, 1)["segmentation"]

    def test_a_label_not_under_review_keeps_its_status(self):
        """Only the right femur is queued on; the left one, not reviewed yet, is left alone."""
        stored = self.approve({"FEMUR_LEFT": 1, "FEMUR_RIGHT": 2}, ["FEMUR_LEFT", "FEMUR_RIGHT"],
                              eligible_label_values=[2])
        self.assertEqual(stored, {"FEMUR_LEFT": 1, "FEMUR_RIGHT": 2})

    def test_a_listed_label_that_is_not_painted_becomes_not_available(self):
        stored = self.approve({"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1}, ["FEMUR_LEFT"])
        self.assertEqual(stored, {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 0})

    def test_the_policy_can_leave_it_as_it_was(self):
        stored = self.approve({"FEMUR_LEFT": 1, "FEMUR_RIGHT": 1}, ["FEMUR_LEFT"], mark_removed_labels_absent=False)
        self.assertEqual(stored, {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 1})

    def test_a_painted_label_the_dataset_did_not_list_is_recorded(self):
        stored = self.approve({"FEMUR_LEFT": 1}, ["FEMUR_LEFT", "SACRUM"])
        self.assertEqual(stored, {"FEMUR_LEFT": 2, "SACRUM": 2})


class ApproveCorrectionTests(ApprovalTestCase):
    def test_the_correction_replaces_the_datasets_segmentation(self):
        staged = self.state_dir / "staged" / "Dataset_001" / f"{KEY}.seg.nrrd"
        self.correct_right_femur()
        corrected = staged.read_bytes()
        outcome = self.store.approve(KEY)
        self.assertTrue(outcome.segmentation_written)
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), corrected)
        self.assertFalse(staged.exists(), "the correction moved into the dataset")
        self.assertEqual(self.statuses(), {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})

    def test_the_datasets_segmentation_is_backed_up_first(self):
        self.correct_right_femur()
        outcome = self.store.approve(KEY)
        self.assertEqual(Path(outcome.backup_path).read_bytes(), self.original)
        self.assertEqual(Path(outcome.backup_path).parent, self.state_dir / "backups" / "Dataset_001")
        self.assertEqual(list((self.state_dir / "tmp").glob("*")), [])

    def test_backups_can_be_switched_off(self):
        store = self.make_store(keep_segmentation_backups=False)
        self.correct_right_femur(store)
        self.assertIsNone(store.approve(KEY).backup_path)
        self.assertEqual(list((self.state_dir / "backups").rglob("*.seg.nrrd")), [])
        self.assertEqual(list((self.state_dir / "tmp").glob("*")), [])

    def test_a_label_the_editor_removed_becomes_not_available(self):
        self.review(self.store, self.rita, rejected={"FEMUR_RIGHT": "absent"})
        self.edit(self.store, self.eddie, ["FEMUR_LEFT"])
        self.store.approve(KEY)
        self.assertEqual(self.statuses(), {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 0})
        self.assertEqual(self.builder.labels_in_segmentation(1, 1), {"FEMUR_LEFT"})

    def test_a_segmentation_made_where_there_was_none_is_written(self):
        self.builder.add_subject(1, 2, segmentation=None)
        store = self.make_store(include_subjects_without_segmentation=True, edits_need_review=False)
        self.edit(store, self.eddie, ["FEMUR_LEFT"])
        self.assertEqual(store.case_of("001_000002").stage, "approval")
        outcome = store.approve("001_000002")
        self.assertIsNone(outcome.backup_path, "there was nothing to back up")
        self.assertEqual(self.builder.labels_in_segmentation(1, 2), {"FEMUR_LEFT"})
        self.assertEqual(self.statuses(2), {"FEMUR_LEFT": 2})


class ApprovalRefusalTests(ApprovalTestCase):
    def assert_refused(self, fragment: str, status: int = 409) -> None:
        info_before = self.builder.all_subject_info(1)
        with self.assertRaises(QCError) as ctx:
            self.store.approve(KEY)
        self.assertEqual(ctx.exception.status_code, status)
        self.assertIn(fragment, ctx.exception.message)
        self.assertEqual(self.builder.all_subject_info(1), info_before)

    def test_a_subject_not_waiting_for_approval_is_refused(self):
        self.review(self.store, self.rita, rejected={"FEMUR_RIGHT": "quality"})
        self.assert_refused("an editor")

    def test_a_subject_nobody_judged_is_unknown(self):
        self.assert_refused("Nobody", status=404)

    def test_approving_twice_is_refused(self):
        self.review(self.store, self.rita)
        self.store.approve(KEY)
        self.assert_refused("approved")

    def test_a_segmentation_written_meanwhile_is_not_overwritten(self):
        """Another server or a converter wrote the dataset's file after the review began."""
        self.review(self.store, self.rita)
        write_mask(self.builder.segmentation_file(1, 1), ["FEMUR_LEFT", "FEMUR_RIGHT"], grown=["FEMUR_LEFT"])
        self.assert_refused("changed after")
        self.assertEqual(self.store.case_of(KEY).stage, "approval")

    def test_a_segmentation_only_touched_meanwhile_is_still_approved(self):
        """Copied, or restored with a new time: the same content is the same file."""
        self.review(self.store, self.rita)
        path = self.builder.segmentation_file(1, 1)
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 7_000_000_000))
        self.store.approve(KEY)
        self.assertEqual(self.statuses(), {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})

    def test_a_dataset_regenerated_under_another_schema_is_not_written(self):
        self.review(self.store, self.rita)
        self.builder.add_dataset(1, schema_version="0.2.0")
        self.assert_refused("0.2.0")

    def test_a_failed_subject_info_write_puts_the_segmentation_back(self):
        """The dataset is not left half approved: the correction goes back to wait."""
        self.correct_right_femur()
        staged = self.state_dir / "staged" / "Dataset_001" / f"{KEY}.seg.nrrd"
        corrected = staged.read_bytes()
        with mock.patch.object(self.store, "_mutate_subject_info", side_effect=OSError("the share went away")):
            with self.assertRaises(OSError):
                self.store.approve(KEY)
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), self.original)
        self.assertEqual(staged.read_bytes(), corrected)
        self.assertEqual(self.store.case_of(KEY).stage, "approval")
        self.store.approve(KEY)  # and once the share is back, it goes through
        self.assertEqual(self.builder.segmentation_file(1, 1).read_bytes(), corrected)


class ApproveAllTests(QCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.default_dataset(n_subjects=4)
        self.store = self.make_store()
        self.rita = self.store.create_user("rita", roles=[REVIEWER])[0]
        for _ in range(4):
            self.review(self.store, self.rita)

    def test_every_subject_waiting_for_approval_is_approved(self):
        results = self.store.approve_all()
        self.assertEqual([r["approved"] for r in results], [True] * 4)
        for entry in self.builder.all_subject_info(1):
            self.assertEqual(entry["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})

    def test_one_that_cannot_be_approved_does_not_stop_the_others(self):
        write_mask(self.builder.segmentation_file(1, 2), ["FEMUR_LEFT"])
        results = {r["subject_key"]: r for r in self.store.approve_all()}
        self.assertFalse(results["001_000002"]["approved"])
        self.assertIn("changed after", results["001_000002"]["message"])
        self.assertEqual(sum(r["approved"] for r in results.values()), 3)

    def test_approvals_at_the_same_time_all_land_in_subject_info(self):
        """Four subjects of one Subject_info file, approved from four threads."""
        start = threading.Barrier(4)

        def approve(key):
            start.wait(timeout=10)
            return self.store.approve(key)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(approve, [f"001_00000{i}" for i in range(1, 5)]))
        for entry in self.builder.all_subject_info(1):
            self.assertEqual(entry["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})


class ApprovedSubjectTests(ApprovalTestCase):
    def test_an_approved_subject_is_never_handed_out_again(self):
        """Not even when its statuses queue it once more."""
        store = self.make_store(eligible_label_values=[1, 2])
        self.review(store, self.rita)
        store.approve(KEY)
        store.refresh_index()
        with self.assertRaises(QCError) as ctx:
            store.next_subject(self.rita, REVIEWER)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_a_finished_subject_moves_to_the_done_file_and_stays_finished(self):
        self.review(self.store, self.rita)
        self.store.approve(KEY)
        self.assertEqual(json.loads((self.state_dir / "cases.json").read_text(encoding="utf-8")), [])
        done = (self.state_dir / "cases_done.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["stage"] for line in done], ["applied"])
        self.assertEqual(self.make_store().case_of(KEY).stage, "applied")

    def test_the_stats_follow_each_stage(self):
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1})
        self.builder.add_subject(1, 3, segmentation={"FEMUR_LEFT": 1})
        store = self.make_store()
        self.review(store, self.rita)  # subject 1 waits for approval
        self.review(store, self.rita, rejected={"FEMUR_LEFT": "quality"})  # subject 2 for an editor
        stats = store.stats()
        self.assertEqual(
            (stats.available, stats.to_review, stats.to_edit, stats.awaiting_approval, stats.applied), (1, 0, 1, 1, 0)
        )
        store.approve(KEY)
        store.next_subject(self.eddie, EDITOR)
        stats = store.stats()
        self.assertEqual((stats.assigned, stats.to_edit, stats.awaiting_approval, stats.applied), (1, 0, 0, 1))


class AdminActionTests(ApprovalTestCase):
    """Sending a subject back, and closing it."""

    def test_sent_back_to_review_every_verdict_is_reviewed_again(self):
        self.review(self.store, self.rita)
        case = self.store.return_case(KEY, "review", "look at the left femur again")
        self.assertEqual(case.stage, "review")
        self.assertEqual({label.state for label in case.labels.values()}, {"pending"})
        self.assertEqual(case.events[-1].comment, "look at the left femur again")
        self.assertEqual(self.store.next_subject(self.rita, REVIEWER).subject_key, KEY)

    def test_sent_back_to_the_editors_with_the_administrators_word(self):
        self.review(self.store, self.rita)
        case = self.store.return_case(KEY, "edit", "the femoral heads are too small")
        self.assertEqual(case.stage, "edit")
        self.assertEqual((case.requests[0].role, case.requests[0].comment), ("admin", "the femoral heads are too small"))
        assignment = self.store.next_subject(self.eddie, EDITOR)
        self.assertEqual(self.store.handout_case(assignment).requests[0].comment, "the femoral heads are too small")

    def test_a_subject_an_editor_gave_up_on_can_go_back(self):
        self.review(self.store, self.rita, rejected={"FEMUR_RIGHT": "quality"})
        assignment = self.store.next_subject(self.eddie, EDITOR)
        self.store.submit(assignment.assignment_id, self.eddie, False, None, comment="beyond me")
        self.assertEqual(self.store.case_of(KEY).stage, "escalated")
        self.assertEqual(self.store.return_case(KEY, "edit", "try the threshold tool").stage, "edit")

    def test_a_closed_subject_is_left_alone_and_nothing_is_written(self):
        before = self.dataset_state()
        self.review(self.store, self.rita, rejected={"FEMUR_RIGHT": "quality"})
        case = self.store.close_case(KEY, "not a usable scan")
        self.assertEqual(case.stage, "closed")
        self.assertEqual(self.dataset_state(), before)
        with self.assertRaises(QCError):
            self.store.next_subject(self.eddie, EDITOR)
        with self.assertRaises(QCError):
            self.store.next_subject(self.rita, REVIEWER)
        self.assertEqual(self.make_store().case_of(KEY).stage, "closed")

    def test_a_closed_subject_can_be_reopened(self):
        self.review(self.store, self.rita)
        self.store.close_case(KEY)
        self.store.return_case(KEY, "review")
        self.assertEqual(self.make_store().case_of(KEY).stage, "review", "the reopened case outlives a restart")
        self.assertEqual(self.store.next_subject(self.rita, REVIEWER).subject_key, KEY)

    def test_a_subject_in_somebodys_hands_is_left_to_them(self):
        self.review(self.store, self.rita, rejected={"FEMUR_RIGHT": "quality"})
        self.store.next_subject(self.eddie, EDITOR)
        for act in (lambda: self.store.return_case(KEY, "review"), lambda: self.store.close_case(KEY)):
            with self.assertRaises(QCError) as ctx:
                act()
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("eddie", ctx.exception.message)

    def test_an_approved_subject_cannot_be_sent_back_or_closed(self):
        self.review(self.store, self.rita)
        self.store.approve(KEY)
        for act in (lambda: self.store.return_case(KEY, "review"), lambda: self.store.close_case(KEY)):
            with self.assertRaises(QCError) as ctx:
                act()
            self.assertEqual(ctx.exception.status_code, 409)

    def test_a_subject_goes_back_to_review_or_edit_only(self):
        self.review(self.store, self.rita)
        with self.assertRaises(QCError) as ctx:
            self.store.return_case(KEY, "approval")
        self.assertEqual(ctx.exception.status_code, 400)


class AdminApiTests(ApiTestCase):
    """The approvals as the admin panel makes them."""

    n_subjects = 2

    def accept(self, key: str) -> dict:
        """alice accepts every label of the next subject waiting for a review, on the review page."""
        handout = self.next_subject(key, REVIEWER)
        response = self.submit(key, handout["assignment_id"], True, role=REVIEWER, use_stored_segmentation=True)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_the_cases_are_listed_by_stage(self):
        self.accept(self.alice_key)
        listed = self.client.get("/admin/api/cases?stage=approval", headers=self.admin_headers).json()
        self.assertEqual([case["subject_key"] for case in listed], [KEY])
        self.assertIsNone(listed[0]["leased_to"])
        self.assertEqual(self.client.get("/admin/api/cases?stage=edit", headers=self.admin_headers).json(), [])
        self.assertEqual(self.client.get("/admin/api/cases", headers={}).status_code, 401)

    def test_a_subject_is_approved_from_the_panel(self):
        self.accept(self.alice_key)
        response = self.client.post(f"/admin/api/cases/{KEY}/approve", headers=self.admin_headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["updated_labels"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.assertEqual(self.builder.subject_info(1, 1)["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})
        self.assertEqual(self.client.post(f"/admin/api/cases/{KEY}/approve", headers=self.admin_headers).status_code, 409)

    def test_every_subject_waiting_is_approved_at_once(self):
        self.accept(self.alice_key)
        self.accept(self.bob_key)
        body = self.client.post("/admin/api/cases/approve", json={}, headers=self.admin_headers).json()
        self.assertEqual(body["approved"], 2)
        named = self.client.post("/admin/api/cases/approve", json={"subject_keys": [KEY]}, headers=self.admin_headers)
        self.assertEqual(named.json()["results"][0]["approved"], False, "approved already")

    def test_a_subject_is_sent_back_or_closed_from_the_panel(self):
        self.accept(self.alice_key)
        sent = self.client.post(
            f"/admin/api/cases/{KEY}/return", json={"to": "edit", "comment": "redo"}, headers=self.admin_headers
        )
        self.assertEqual(sent.json()["stage"], "edit")
        closed = self.client.post(f"/admin/api/cases/{KEY}/close", json={}, headers=self.admin_headers)
        self.assertEqual(closed.json()["stage"], "closed")
        self.assertEqual(
            self.client.post(f"/admin/api/cases/{KEY}/return", json={"to": "nowhere"}, headers=self.admin_headers)
            .status_code,
            400,
        )

    def test_the_segmentation_can_be_downloaded_to_look_at_first(self):
        self.accept(self.alice_key)
        response = self.client.get(f"/admin/api/cases/{KEY}/segmentation", headers=self.admin_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.builder.segmentation_file(1, 1).read_bytes())
        self.assertEqual(self.client.get("/admin/api/cases/001_000099", headers=self.admin_headers).status_code, 404)

    def test_the_editors_correction_is_what_is_downloaded(self):
        handout = self.next_subject(self.alice_key, REVIEWER)
        self.submit(self.alice_key, handout["assignment_id"], True, role=REVIEWER, use_stored_segmentation=True,
                    rejected_labels={"FEMUR_RIGHT": "quality"})
        handout = self.next_subject(self.bob_key, EDITOR)
        self.assertEqual(handout["stage"], "edit")
        self.submit(self.bob_key, handout["assignment_id"], True, ["FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"])
        response = self.client.get(f"/admin/api/cases/{KEY}/segmentation", headers=self.admin_headers)
        staged = self.state_dir / "staged" / "Dataset_001" / f"{KEY}.seg.nrrd"
        self.assertEqual(response.content, staged.read_bytes())
        self.assertEqual(labels_in_mask(staged), {"FEMUR_LEFT", "FEMUR_RIGHT", "TIBIA_LEFT"})


if __name__ == "__main__":
    unittest.main()

"""How the server decides which subject to give to which client.

Requirement under test: "the users will ask for next subject, then server decides which
subject to give to which client". Two reviewers must never be handed the same subject.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from qc_server.models import EDITOR, REVIEWER
from qc_server.store import QCError

from tests.support import QCTestCase


class NextSubjectTests(QCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.default_dataset(n_subjects=3)
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]
        self.bob = self.store.create_user("bob")[0]

    def test_sequential_strategy_hands_out_the_lowest_free_subject(self):
        self.assertEqual(self.store.next_subject(self.alice, REVIEWER).subject_key, "001_000001")
        self.assertEqual(self.store.next_subject(self.bob, REVIEWER).subject_key, "001_000002")

    def test_two_reviewers_never_hold_the_same_subject(self):
        store = self.make_store(max_concurrent_assignments_per_user=3)
        alice, bob = store._users["alice"], store._users["bob"]
        handed_out = [store.next_subject(alice if i % 2 == 0 else bob, REVIEWER).subject_key for i in range(3)]
        self.assertEqual(len(set(handed_out)), 3)

    def test_the_queue_runs_out_with_a_clear_404(self):
        store = self.make_store(max_concurrent_assignments_per_user=5)
        alice = store._users["alice"]
        for _ in range(3):
            store.next_subject(alice, REVIEWER)
        with self.assertRaises(QCError) as ctx:
            store.next_subject(alice, REVIEWER)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_asking_again_returns_the_subject_already_held(self):
        """A client that lost its local copy can simply ask for the next subject again."""
        first = self.store.next_subject(self.alice, REVIEWER)
        second = self.store.next_subject(self.alice, REVIEWER)
        self.assertEqual(first.assignment_id, second.assignment_id)

    def test_the_concurrency_limit_is_honoured(self):
        store = self.make_store(max_concurrent_assignments_per_user=2)
        alice = store._users["alice"]
        first = store.next_subject(alice, REVIEWER)
        second = store.next_subject(alice, REVIEWER)
        self.assertNotEqual(first.assignment_id, second.assignment_id)
        self.assertEqual(len(store.open_assignments_of("alice")), 2)
        # A third request returns the oldest open assignment instead of a new subject.
        self.assertEqual(store.next_subject(alice, REVIEWER).assignment_id, first.assignment_id)

    def test_the_random_strategy_still_hands_out_a_real_free_subject(self):
        store = self.make_store(assignment_strategy="random", max_concurrent_assignments_per_user=3)
        alice = store._users["alice"]
        keys = {store.next_subject(alice, REVIEWER).subject_key for _ in range(3)}
        self.assertEqual(keys, {"001_000001", "001_000002", "001_000003"})

    def test_an_assignment_carries_the_reviewer_and_an_expiry(self):
        assignment = self.store.next_subject(self.alice, REVIEWER)
        self.assertEqual(assignment.user, "alice")
        self.assertEqual(assignment.state, "assigned")
        self.assertEqual((assignment.dataset_id, assignment.subject_id), (1, 1))
        self.assertGreater(assignment.expires_at, assignment.assigned_at)

    def test_assignments_survive_a_restart(self):
        assignment = self.store.next_subject(self.alice, REVIEWER)
        reopened = self.make_store()
        held = reopened.open_assignments_of("alice")
        self.assertEqual([a.assignment_id for a in held], [assignment.assignment_id])


class LeaseTests(QCTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.default_dataset(n_subjects=2)
        self.store = self.make_store(lease_ttl_seconds=60)
        self.alice = self.store.create_user("alice")[0]
        self.bob = self.store.create_user("bob")[0]

    def _expire(self, assignment) -> None:
        past = datetime.now(timezone.utc) - timedelta(seconds=5)
        assignment.expires_at = past.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def test_an_expired_lease_returns_the_subject_to_the_queue(self):
        assignment = self.store.next_subject(self.alice, REVIEWER)
        self._expire(assignment)
        self.assertEqual(self.store.next_subject(self.bob, REVIEWER).subject_key, assignment.subject_key)
        self.assertEqual(self.store.get_assignment(assignment.assignment_id).state, "expired")

    def test_extending_a_lease_pushes_the_expiry_out(self):
        assignment = self.store.next_subject(self.alice, REVIEWER)
        self._expire(assignment)
        extended = self.store.extend_assignment(assignment.assignment_id, self.alice)
        self.assertGreater(extended.expires_at, assignment.assigned_at)
        # Still held by alice, so bob gets a different subject.
        self.assertNotEqual(self.store.next_subject(self.bob, REVIEWER).subject_key, assignment.subject_key)

    def test_releasing_puts_a_subject_back_for_someone_else(self):
        assignment = self.store.next_subject(self.alice, REVIEWER)
        released = self.store.release_assignment(assignment.assignment_id, self.alice)
        self.assertEqual(released.state, "released")
        self.assertEqual(self.store.next_subject(self.bob, REVIEWER).subject_key, assignment.subject_key)

    def test_a_released_subject_can_come_back_to_the_same_reviewer(self):
        assignment = self.store.next_subject(self.alice, REVIEWER)
        self.store.release_assignment(assignment.assignment_id, self.alice)
        self.assertEqual(self.store.next_subject(self.alice, REVIEWER).subject_key, assignment.subject_key)

    def test_releasing_twice_is_refused(self):
        assignment = self.store.next_subject(self.alice, REVIEWER)
        self.store.release_assignment(assignment.assignment_id, self.alice)
        with self.assertRaises(QCError) as ctx:
            self.store.release_assignment(assignment.assignment_id, self.alice)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_a_reviewer_cannot_touch_another_reviewers_assignment(self):
        assignment = self.store.next_subject(self.alice, REVIEWER)
        for call in (
            lambda: self.store.get_assignment(assignment.assignment_id, self.bob),
            lambda: self.store.release_assignment(assignment.assignment_id, self.bob),
            lambda: self.store.extend_assignment(assignment.assignment_id, self.bob),
        ):
            with self.assertRaises(QCError) as ctx:
                call()
            self.assertEqual(ctx.exception.status_code, 403)

    def test_an_unknown_assignment_is_a_404(self):
        with self.assertRaises(QCError) as ctx:
            self.store.get_assignment("does-not-exist")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_an_administrator_can_take_a_subject_back(self):
        """The admin panel releases without passing a user, so no ownership check applies."""
        assignment = self.store.next_subject(self.alice, REVIEWER)
        self.assertEqual(self.store.release_assignment(assignment.assignment_id).state, "released")


class AfterAVerdictTests(QCTestCase):
    """Where a subject goes once judged: never back to the queue it came from."""

    def setUp(self) -> None:
        super().setUp()
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        self.store = self.make_store()
        self.alice = self.store.create_user("alice")[0]
        self.bob = self.store.create_user("bob")[0]

    def test_a_rejected_subject_goes_to_the_editors_not_to_another_reviewer(self):
        assignment = self.store.next_subject(self.alice, REVIEWER)
        self.store.submit(assignment.assignment_id, self.alice, False, None)
        with self.assertRaises(QCError):
            self.store.next_subject(self.bob, REVIEWER)
        self.assertEqual(self.store.next_subject(self.bob, EDITOR).subject_key, assignment.subject_key)

    def test_a_subject_waiting_for_approval_is_handed_to_nobody(self):
        self.review(self.store, self.alice)
        for role in (REVIEWER, EDITOR):
            with self.assertRaises(QCError):
                self.store.next_subject(self.bob, role)


class StatsTests(QCTestCase):
    def test_the_queue_snapshot_tracks_every_stage(self):
        self.default_dataset(n_subjects=4)
        store = self.make_store()
        alice = store.create_user("alice")[0]
        bob = store.create_user("bob")[0]

        self.assertEqual(store.stats().available, 4)
        self.assertEqual(store.stats().assigned, 0)

        self.review(store, alice)  # subject 1 waits for approval
        self.review(store, bob, rejected={"FEMUR_LEFT": "quality"})  # subject 2 for an editor
        store.next_subject(alice, REVIEWER)  # subject 3 is out

        stats = store.stats()
        self.assertEqual(stats.total_subjects, 4)
        self.assertEqual(
            (stats.available, stats.assigned, stats.to_edit, stats.awaiting_approval), (1, 1, 1, 1)
        )
        self.assertEqual(stats.datasets, {1: stats.eligible_subjects})

    def test_listing_assignments_can_be_filtered_and_capped(self):
        self.default_dataset(n_subjects=3)
        store = self.make_store(max_concurrent_assignments_per_user=3)
        alice = store.create_user("alice")[0]
        first = store.next_subject(alice, REVIEWER)
        store.next_subject(alice, REVIEWER)
        store.release_assignment(first.assignment_id, alice)

        self.assertEqual(len(store.all_assignments()), 2)
        self.assertEqual(len(store.all_assignments(states=["released"])), 1)
        self.assertEqual(len(store.all_assignments(limit=1)), 1)


if __name__ == "__main__":
    unittest.main()

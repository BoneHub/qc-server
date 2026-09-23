"""Several clients hitting the server at the same time.

The reviewers are people in 3D Slicer, so the load is tiny, but two of them pressing
"next subject" at the same moment must still never be handed the same subject.
"""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from bonehub_quality_check_server.store import QCError

from tests.support import QCTestCase


class ConcurrentHandoutTests(QCTestCase):
    def test_simultaneous_requests_never_hand_out_the_same_subject(self):
        self.default_dataset(n_subjects=12)
        store = self.make_store()
        users = [store.create_user(f"reviewer_{i}")[0] for i in range(12)]

        start = threading.Barrier(len(users))

        def grab(user):
            start.wait(timeout=10)
            return store.next_subject(user).subject_key

        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            keys = list(pool.map(grab, users))

        self.assertEqual(len(keys), 12)
        self.assertEqual(len(set(keys)), 12, "a subject was handed to two reviewers at once")

    def test_more_reviewers_than_subjects_means_some_get_a_clean_refusal(self):
        self.default_dataset(n_subjects=3)
        store = self.make_store()
        users = [store.create_user(f"reviewer_{i}")[0] for i in range(8)]
        start = threading.Barrier(len(users))

        def grab(user):
            start.wait(timeout=10)
            try:
                return store.next_subject(user).subject_key
            except QCError as exc:
                return f"refused:{exc.status_code}"

        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            results = list(pool.map(grab, users))

        handed_out = [r for r in results if not r.startswith("refused")]
        self.assertEqual(len(handed_out), 3)
        self.assertEqual(len(set(handed_out)), 3)
        self.assertTrue(all(r == "refused:404" for r in results if r.startswith("refused")))

    def test_concurrent_submissions_all_land_in_subject_info(self):
        """Each reviewer writes a different subject in the same Subject_info file."""
        self.default_dataset(n_subjects=6)
        store = self.make_store()
        users = [store.create_user(f"reviewer_{i}")[0] for i in range(6)]
        assignments = [store.next_subject(user) for user in users]
        uploads = [self.upload_file(["FEMUR_LEFT", "FEMUR_RIGHT"]) for _ in users]

        start = threading.Barrier(len(users))

        def send(item):
            user, assignment, upload = item
            start.wait(timeout=10)
            return store.submit(assignment.assignment_id, user, True, upload)

        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            outcomes = list(pool.map(send, zip(users, assignments, uploads)))

        self.assertTrue(all(o.assignment.state == "confirmed" for o in outcomes))
        stored = self.builder.all_subject_info(1)
        self.assertEqual(len(stored), 6, "a concurrent write dropped a subject from the file")
        for entry in stored:
            self.assertEqual(entry["segmentation"], {"FEMUR_LEFT": 2, "FEMUR_RIGHT": 2})

    def test_concurrent_user_creation_keeps_every_reviewer(self):
        self.default_dataset(n_subjects=1)
        store = self.make_store()
        start = threading.Barrier(10)

        def create(index):
            start.wait(timeout=10)
            return store.create_user(f"reviewer_{index}")[1]

        with ThreadPoolExecutor(max_workers=10) as pool:
            keys = list(pool.map(create, range(10)))

        self.assertEqual(len(set(keys)), 10)
        self.assertEqual(len(store.list_users()), 10)
        reopened = self.make_store()
        self.assertEqual(len(reopened.list_users()), 10, "a concurrent write lost a user on disk")

    def test_the_audit_trail_keeps_every_concurrent_submission(self):
        self.default_dataset(n_subjects=8)
        store = self.make_store()
        users = [store.create_user(f"reviewer_{i}")[0] for i in range(8)]
        assignments = [store.next_subject(user) for user in users]
        start = threading.Barrier(len(users))

        def send(item):
            user, assignment = item
            start.wait(timeout=10)
            store.submit(assignment.assignment_id, user, False, None)

        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            list(pool.map(send, zip(users, assignments)))

        entries = store.audit.read_recent(limit=100, kind="submission")
        self.assertEqual(len(entries), 8)
        self.assertEqual(len({e["subject_key"] for e in entries}), 8)


if __name__ == "__main__":
    unittest.main()

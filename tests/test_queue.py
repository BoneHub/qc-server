"""Which subjects the server puts in the quality-check queue.

Requirements under test: the server reads a folder that is already in BoneHub data
structure format, and "it must work on specific datasets not all datasets in the root".
"""

from __future__ import annotations

import unittest

from bonehub_data_schema import __version__ as SCHEMA_VERSION
from bonehub_quality_check_server.config import QCServerConfig

from tests.support import QCTestCase


class IndexEligibilityTests(QCTestCase):
    def keys(self, store) -> list:
        return [ref.subject_key for ref in store._index]

    def test_only_segmentations_not_reviewed_yet_are_queued(self):
        self.builder.add_dataset(1)
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})  # available, not reviewed
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 2})  # already reviewed
        self.builder.add_subject(1, 3, segmentation={"FEMUR_LEFT": 0})  # not available
        store = self.make_store()
        self.assertEqual(self.keys(store), ["001_000001"])
        self.assertEqual(store.stats().total_subjects, 3)
        self.assertEqual(store.stats().eligible_subjects, 1)

    def test_a_subject_with_any_label_not_reviewed_is_queued(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 2, "FEMUR_RIGHT": 1})
        self.assertEqual(self.keys(self.make_store()), ["001_000001"])

    def test_subjects_without_a_segmentation_are_skipped_by_default(self):
        self.builder.add_subject(1, 1, segmentation=None)
        self.assertEqual(self.keys(self.make_store()), [])

    def test_subjects_without_a_segmentation_can_be_queued_on_request(self):
        """So a reviewer can create a segmentation from scratch."""
        self.builder.add_subject(1, 1, segmentation=None)
        store = self.make_store(include_subjects_without_segmentation=True)
        self.assertEqual(self.keys(store), ["001_000001"])
        self.assertFalse(store._index[0].has_segmentation)

    def test_labels_that_are_all_not_available_make_no_segmentation(self):
        """Status 0 is the same as the label being absent."""
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 0, "FEMUR_RIGHT": 0})
        self.assertEqual(self.keys(self.make_store()), [])
        store = self.make_store(include_subjects_without_segmentation=True)
        self.assertEqual(self.keys(store), ["001_000001"])

    def test_a_subject_with_no_image_is_never_queued(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1}, image=False)
        self.assertEqual(self.keys(self.make_store()), [])

    def test_a_subject_whose_image_file_is_missing_is_skipped(self):
        """The metadata claims an image but the file is not there; do not hand it out."""
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1}, write_image_file=False)
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1})
        self.assertEqual(self.keys(self.make_store()), ["001_000002"])

    def test_reviewed_subjects_can_be_queued_for_a_second_review(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 2})
        self.assertEqual(self.keys(self.make_store(eligible_label_values=[2])), ["001_000002"])
        self.assertEqual(self.keys(self.make_store(eligible_label_values=[1, 2])), ["001_000001", "001_000002"])

    def test_the_queue_is_ordered_by_dataset_then_subject(self):
        self.builder.add_subject(2, 5, segmentation={"FEMUR_LEFT": 1})
        self.builder.add_subject(1, 9, segmentation={"FEMUR_LEFT": 1})
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1})
        self.assertEqual(self.keys(self.make_store()), ["001_000002", "001_000009", "002_000005"])


class SchemaVersionTests(QCTestCase):
    """Only datasets written under this server's schema are served.

    In another schema version the label values, the label statuses and the segmentation
    format may all mean something else, so such a dataset is left alone entirely.
    """

    def server_log(self) -> str:
        return (self.state_dir / "server.log").read_text(encoding="utf-8")

    def test_a_dataset_of_the_current_schema_is_served(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        self.assertEqual(self.make_store().stats().datasets, {1: 1})

    def test_a_dataset_of_another_patch_version_is_served(self):
        self.builder.add_dataset(1, schema_version=f"{SCHEMA_VERSION.rsplit('.', 1)[0]}.99")
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        self.assertEqual(self.make_store().stats().eligible_subjects, 1)

    def test_a_dataset_of_an_older_schema_is_skipped(self):
        self.builder.add_dataset(1, schema_version="0.2.0")
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        self.builder.add_subject(2, 1, segmentation={"FEMUR_LEFT": 1})
        store = self.make_store()
        self.assertEqual(store.stats().datasets, {2: 1})
        self.assertEqual(store.stats().total_subjects, 1)
        self.assertIn("Dataset_001", self.server_log())
        self.assertIn("0.2.0", self.server_log())

    def test_a_dataset_that_records_no_schema_version_is_skipped(self):
        """Datasets converted before schema versions were recorded."""
        self.builder.add_dataset(1, schema_version=None)
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        self.assertEqual(self.make_store().stats().eligible_subjects, 0)
        self.assertIn("(not recorded)", self.server_log())

    def test_a_dataset_whose_info_file_is_broken_is_skipped(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        self.builder.add_subject(2, 1, segmentation={"FEMUR_LEFT": 1})
        (self.dataset_root / "Dataset_002" / "Dataset_info_002.json").write_text("{ truncated", encoding="utf-8")
        self.assertEqual(self.make_store().stats().datasets, {1: 1})


class DatasetScopeTests(QCTestCase):
    """"It must work on specific datasets, not all datasets in the root"."""

    def setUp(self) -> None:
        super().setUp()
        for dataset_id in (1, 2, 3):
            self.builder.add_subject(dataset_id, 1, segmentation={"FEMUR_LEFT": 1})

    def test_without_a_restriction_every_dataset_is_served(self):
        store = self.make_store()
        self.assertEqual(sorted(store.stats().datasets), [1, 2, 3])

    def test_the_server_can_be_pinned_to_chosen_datasets(self):
        store = self.make_store(allowed_dataset_ids=[1, 3])
        self.assertEqual(sorted(store.stats().datasets), [1, 3])
        self.assertEqual([ref.dataset_id for ref in store._index], [1, 3])

    def test_excluded_datasets_are_never_handed_out(self):
        store = self.make_store(allowed_dataset_ids=[2])
        _, key = store.create_user("alice")
        user = store.authenticate(key)
        self.assertEqual(store.next_subject(user).dataset_id, 2)

    def test_excluded_subjects_are_not_counted_as_total_either(self):
        store = self.make_store(allowed_dataset_ids=[1])
        self.assertEqual(store.stats().total_subjects, 1)

    def test_a_reviewer_can_be_restricted_further_than_the_server(self):
        store = self.make_store(allowed_dataset_ids=[1, 2])
        store.create_user("alice", allowed_dataset_ids=[2])
        _, key = store.create_user("bob")
        alice = store._users["alice"]
        self.assertEqual(store.next_subject(alice).dataset_id, 2)
        self.assertEqual(store.next_subject(store.authenticate(key)).dataset_id, 1)

    def test_a_reviewer_cannot_reach_beyond_the_servers_own_restriction(self):
        """A per-user allowance widens nothing: the server's list is the outer bound."""
        store = self.make_store(allowed_dataset_ids=[1])
        store.create_user("alice", allowed_dataset_ids=[2, 3])
        self.assertFalse(store._user_may_access(store._users["alice"], 2))
        with self.assertRaises(Exception):
            store.next_subject(store._users["alice"])


class MalformedDatasetTests(QCTestCase):
    """A broken dataset folder must not take the whole server down."""

    def test_a_folder_without_a_subject_info_file_is_skipped(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        (self.dataset_root / "Dataset_002").mkdir(parents=True)
        store = self.make_store()
        self.assertEqual(store.stats().eligible_subjects, 1)

    def test_an_unparsable_subject_info_file_is_skipped(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        self.builder.add_subject(2, 1, segmentation={"FEMUR_LEFT": 1})
        (self.dataset_root / "Dataset_002" / "Subject_info_002.json").write_text("{ truncated", encoding="utf-8")
        store = self.make_store()
        self.assertEqual([ref.dataset_id for ref in store._index], [1])

    def test_a_folder_whose_name_carries_no_dataset_id_is_skipped(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        (self.dataset_root / "Dataset_notanumber").mkdir(parents=True)
        self.assertEqual(self.make_store().stats().eligible_subjects, 1)

    def test_unrelated_folders_in_the_root_are_ignored(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        (self.dataset_root / "scratch").mkdir(parents=True)
        (self.dataset_root / "README.txt").write_text("hello", encoding="utf-8")
        self.assertEqual(self.make_store().stats().eligible_subjects, 1)

    def test_a_dataset_root_that_does_not_exist_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.track(
                __import__("bonehub_quality_check_server.store", fromlist=["QCStore"]).QCStore(
                    dataset_root=self.tmp_path / "nowhere",
                    state_dir=self.state_dir,
                )
            )


class IndexRefreshTests(QCTestCase):
    def test_subjects_added_on_disk_appear_after_a_refresh(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        store = self.make_store(index_refresh_seconds=3600)
        self.assertEqual(store.stats().eligible_subjects, 1)

        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1})
        store.refresh_index()
        self.assertEqual(store.stats().eligible_subjects, 2)

    def test_a_zero_ttl_rebuilds_the_index_on_every_request(self):
        self.builder.add_subject(1, 1, segmentation={"FEMUR_LEFT": 1})
        store = self.make_store(index_refresh_seconds=0)
        self.builder.add_subject(1, 2, segmentation={"FEMUR_LEFT": 1})
        self.assertEqual(store.stats().eligible_subjects, 2)


class StateFolderTests(QCTestCase):
    """Everything the server owns lives inside the dataset folder."""

    def test_the_state_folder_is_laid_out_on_first_start(self):
        self.default_dataset()
        store = self.make_store()
        for name in ["config.json", "server_private_key", "admin_key"]:
            self.assertTrue((self.state_dir / name).exists(), name)
        for name in ["backups", "tmp"]:
            self.assertTrue((self.state_dir / name).is_dir(), name)
        self.assertEqual(store.state_dir, self.state_dir)

    def test_the_config_file_is_written_so_it_can_be_edited_later(self):
        self.default_dataset()
        self.make_store(lease_ttl_seconds=120)
        self.assertEqual(QCServerConfig.load(self.state_dir / "config.json").lease_ttl_seconds, 120)

    def test_the_state_folder_is_not_mistaken_for_a_dataset(self):
        self.default_dataset()
        store = self.make_store()
        self.assertEqual(sorted(store.stats().datasets), [1])


if __name__ == "__main__":
    unittest.main()

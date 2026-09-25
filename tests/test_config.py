"""The policy file and its environment overrides, which is how the container is steered."""

from __future__ import annotations

import json
import os
import unittest

from qc_server.config import (
    DEFAULT_CREDENTIALS_DIR,
    ENV_PREFIX,
    QCServerConfig,
    resolve_credentials_dir,
    resolve_dataset_root,
    resolve_state_root,
)

from tests.support import QCTestCase, clear_qc_env


class ConfigDefaultsTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_qc_env()
        self.addCleanup(clear_qc_env)

    def test_defaults_match_the_intended_policy(self):
        config = QCServerConfig()
        # Segmentations nobody has reviewed yet (status 1) are what needs reviewing.
        self.assertEqual(config.eligible_label_values, [1])
        self.assertTrue(config.mark_removed_labels_absent)
        self.assertFalse(config.include_subjects_without_segmentation)
        self.assertTrue(config.edits_need_review, "an editor's correction goes back to a reviewer")
        self.assertIsNone(config.allowed_dataset_ids)
        self.assertEqual(config.assignment_strategy, "sequential")
        self.assertEqual(config.max_concurrent_assignments_per_user, 1)

    def test_rejects_a_label_status_the_schema_does_not_define(self):
        with self.assertRaises(ValueError):
            QCServerConfig(eligible_label_values=[7])
        with self.assertRaises(ValueError):
            QCServerConfig(eligible_label_values=[3])

    def test_rejects_queueing_on_labels_that_have_no_segmentation(self):
        """Status 0 is 'not available': there is nothing to review."""
        with self.assertRaises(ValueError):
            QCServerConfig(eligible_label_values=[0])

    def test_rejects_an_empty_eligible_list(self):
        with self.assertRaises(ValueError):
            QCServerConfig(eligible_label_values=[])

    def test_eligible_values_are_de_duplicated_and_sorted(self):
        self.assertEqual(QCServerConfig(eligible_label_values=[2, 1, 2]).eligible_label_values, [1, 2])

    def test_rejects_unknown_fields(self):
        with self.assertRaises(ValueError):
            QCServerConfig(not_a_real_field=1)

    def test_lease_ttl_has_a_floor(self):
        with self.assertRaises(ValueError):
            QCServerConfig(lease_ttl_seconds=5)


class ConfigPersistenceTests(QCTestCase):
    def test_save_then_load_round_trips(self):
        path = self.tmp_path / "config.json"
        original = QCServerConfig(
            eligible_label_values=[1, 2],
            allowed_dataset_ids=[3, 7],
            assignment_strategy="random",
            edits_need_review=False,
        )
        original.save(path)
        self.assertEqual(QCServerConfig.load(path).model_dump(), original.model_dump())

    def test_load_of_a_missing_file_gives_defaults(self):
        config = QCServerConfig.load(self.tmp_path / "nope.json")
        self.assertEqual(config.model_dump(), QCServerConfig().model_dump())

    def test_save_is_atomic_and_leaves_no_temporary_file(self):
        path = self.tmp_path / "state" / "config.json"
        QCServerConfig().save(path)
        self.assertTrue(path.exists())
        self.assertFalse(path.with_name(path.name + ".tmp").exists())
        json.loads(path.read_text(encoding="utf-8"))


class ConfigEnvironmentOverrideTests(QCTestCase):
    """``BONEHUB_QC_*`` wins over the stored file, which is how docker-compose configures."""

    def test_int_bool_and_list_overrides_are_parsed(self):
        path = self.tmp_path / "config.json"
        QCServerConfig().save(path)

        os.environ[f"{ENV_PREFIX}LEASE_TTL_SECONDS"] = "600"
        os.environ[f"{ENV_PREFIX}EDITS_NEED_REVIEW"] = "false"
        os.environ[f"{ENV_PREFIX}ALLOWED_DATASET_IDS"] = "1, 4, 9"
        os.environ[f"{ENV_PREFIX}ASSIGNMENT_STRATEGY"] = "random"

        config = QCServerConfig.load(path)
        self.assertEqual(config.lease_ttl_seconds, 600)
        self.assertFalse(config.edits_need_review)
        self.assertEqual(config.allowed_dataset_ids, [1, 4, 9])
        self.assertEqual(config.assignment_strategy, "random")

    def test_allowed_dataset_ids_can_be_cleared_back_to_every_dataset(self):
        path = self.tmp_path / "config.json"
        QCServerConfig(allowed_dataset_ids=[1]).save(path)
        os.environ[f"{ENV_PREFIX}ALLOWED_DATASET_IDS"] = "none"
        self.assertIsNone(QCServerConfig.load(path).allowed_dataset_ids)

    def test_an_empty_variable_is_ignored_rather_than_parsed(self):
        path = self.tmp_path / "config.json"
        QCServerConfig(lease_ttl_seconds=999).save(path)
        os.environ[f"{ENV_PREFIX}LEASE_TTL_SECONDS"] = ""
        self.assertEqual(QCServerConfig.load(path).lease_ttl_seconds, 999)

    def test_falsey_words_all_turn_a_flag_off(self):
        path = self.tmp_path / "config.json"
        QCServerConfig(edits_need_review=True).save(path)
        for word in ["false", "0", "no", "off"]:
            os.environ[f"{ENV_PREFIX}EDITS_NEED_REVIEW"] = word
            self.assertFalse(QCServerConfig.load(path).edits_need_review, word)


class PathResolutionTests(QCTestCase):
    def test_dataset_root_comes_from_the_environment(self):
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        self.assertEqual(resolve_dataset_root(), self.dataset_root)

    def test_a_missing_dataset_root_is_a_clear_startup_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            resolve_dataset_root()
        self.assertIn("DATASET_ROOT", str(ctx.exception))

    def test_state_root_defaults_to_a_hidden_folder_inside_the_dataset(self):
        self.assertEqual(resolve_state_root(self.dataset_root), self.dataset_root / ".bonehub_qc")

    def test_state_root_name_is_overridable(self):
        os.environ[f"{ENV_PREFIX}STATE_DIR_NAME"] = "_qc"
        self.assertEqual(resolve_state_root(self.dataset_root), self.dataset_root / "_qc")

    def test_the_credentials_folder_comes_from_the_environment(self):
        os.environ[f"{ENV_PREFIX}CREDENTIALS_DIR"] = str(self.tmp_path / "keys")
        self.assertEqual(resolve_credentials_dir(), self.tmp_path / "keys")

    def test_the_credentials_folder_defaults_to_the_containers_own(self):
        del os.environ[f"{ENV_PREFIX}CREDENTIALS_DIR"]
        self.assertEqual(resolve_credentials_dir().as_posix(), DEFAULT_CREDENTIALS_DIR)


if __name__ == "__main__":
    unittest.main()

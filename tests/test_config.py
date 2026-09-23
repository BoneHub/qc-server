"""The policy file and its environment overrides, which is how the container is steered."""

from __future__ import annotations

import json
import os
import unittest

from bonehub_data_schema import __version__ as SCHEMA_VERSION
from bonehub_quality_check_server.config import (
    ENV_PREFIX,
    QCServerConfig,
    resolve_dataset_root,
    resolve_state_dir,
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
        self.assertFalse(config.requeue_rejected)
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

    def test_the_confirmed_value_is_no_longer_a_setting(self):
        """A confirmed label is always 2, 'available, reviewed and corrected'."""
        with self.assertRaises(ValueError):
            QCServerConfig(confirmed_label_value=2)

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
            requeue_rejected=True,
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

    def test_the_file_records_the_schema_its_statuses_belong_to(self):
        path = self.tmp_path / "config.json"
        QCServerConfig().save(path)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], SCHEMA_VERSION)


class ConfigUpgradeTests(QCTestCase):
    """A config.json left behind by a server of an older schema must not stop the new one."""

    def load_legacy(self, **fields) -> tuple:
        path = self.tmp_path / "config.json"
        path.write_text(json.dumps({"lease_ttl_seconds": 7200, **fields}), encoding="utf-8")
        notes = []
        return QCServerConfig.load(path, notify=notes.append), notes

    def test_a_pre_0_3_file_loads_with_its_statuses_translated(self):
        """Old default: 2, 'generated, without quality check'. In schema 0.3 that is 1."""
        config, notes = self.load_legacy(eligible_label_values=[2], confirmed_label_value=3)
        self.assertEqual(config.eligible_label_values, [1])
        self.assertEqual(config.lease_ttl_seconds, 7200, "the rest of the policy is kept")
        self.assertTrue(any("confirmed_label_value" in note for note in notes))
        self.assertTrue(any("eligible_label_values" in note for note in notes))

    def test_every_old_status_maps_onto_the_new_ones(self):
        self.assertEqual(self.load_legacy(eligible_label_values=[1, 2])[0].eligible_label_values, [1])
        self.assertEqual(self.load_legacy(eligible_label_values=[3])[0].eligible_label_values, [2])
        self.assertEqual(self.load_legacy(eligible_label_values=[2, 3])[0].eligible_label_values, [1, 2])
        # Only 'not available' values: nothing to review, so back to the default.
        self.assertEqual(self.load_legacy(eligible_label_values=[-1, 0])[0].eligible_label_values, [1])

    def test_a_file_of_another_schema_has_its_statuses_reset(self):
        config, notes = self.load_legacy(schema_version="9.0.0", eligible_label_values=[2])
        self.assertEqual(config.eligible_label_values, [1])
        self.assertTrue(notes)

    def test_a_current_file_is_read_as_it_is(self):
        config, notes = self.load_legacy(schema_version=SCHEMA_VERSION, eligible_label_values=[2])
        self.assertEqual(config.eligible_label_values, [2])
        self.assertEqual(notes, [])

    def test_the_store_upgrades_the_file_on_disk_and_logs_it(self):
        self.default_dataset()
        self.state_dir.mkdir(parents=True)
        legacy = {"eligible_label_values": [2], "confirmed_label_value": 3, "lease_ttl_seconds": 7200}
        (self.state_dir / "config.json").write_text(json.dumps(legacy), encoding="utf-8")

        store = self.make_store()
        self.assertEqual(store.config.eligible_label_values, [1])
        self.assertEqual(store.stats().eligible_subjects, 3)

        saved = json.loads((self.state_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["schema_version"], SCHEMA_VERSION)
        self.assertNotIn("confirmed_label_value", saved)
        self.assertIn("confirmed_label_value", (self.state_dir / "server.log").read_text(encoding="utf-8"))

    def test_an_environment_override_uses_the_new_statuses(self):
        """docker-compose sets BONEHUB_QC_ELIGIBLE_LABEL_VALUES; it is not translated."""
        os.environ[f"{ENV_PREFIX}ELIGIBLE_LABEL_VALUES"] = "2"
        config, _notes = self.load_legacy(eligible_label_values=[2])
        self.assertEqual(config.eligible_label_values, [2])


class ConfigEnvironmentOverrideTests(QCTestCase):
    """``BONEHUB_QC_*`` wins over the stored file, which is how docker-compose configures."""

    def test_int_bool_and_list_overrides_are_parsed(self):
        path = self.tmp_path / "config.json"
        QCServerConfig().save(path)

        os.environ[f"{ENV_PREFIX}LEASE_TTL_SECONDS"] = "600"
        os.environ[f"{ENV_PREFIX}REQUEUE_REJECTED"] = "true"
        os.environ[f"{ENV_PREFIX}ALLOWED_DATASET_IDS"] = "1, 4, 9"
        os.environ[f"{ENV_PREFIX}ASSIGNMENT_STRATEGY"] = "random"

        config = QCServerConfig.load(path)
        self.assertEqual(config.lease_ttl_seconds, 600)
        self.assertTrue(config.requeue_rejected)
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
        QCServerConfig(requeue_rejected=True).save(path)
        for word in ["false", "0", "no", "off"]:
            os.environ[f"{ENV_PREFIX}REQUEUE_REJECTED"] = word
            self.assertFalse(QCServerConfig.load(path).requeue_rejected, word)


class PathResolutionTests(QCTestCase):
    def test_dataset_root_comes_from_the_environment(self):
        os.environ[f"{ENV_PREFIX}DATASET_ROOT"] = str(self.dataset_root)
        self.assertEqual(resolve_dataset_root(), self.dataset_root)

    def test_a_missing_dataset_root_is_a_clear_startup_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            resolve_dataset_root()
        self.assertIn("DATASET_ROOT", str(ctx.exception))

    def test_state_dir_defaults_to_a_hidden_folder_inside_the_dataset(self):
        self.assertEqual(resolve_state_dir(self.dataset_root), self.dataset_root / ".bonehub_qc")

    def test_state_dir_name_is_overridable(self):
        os.environ[f"{ENV_PREFIX}STATE_DIR_NAME"] = "_qc"
        self.assertEqual(resolve_state_dir(self.dataset_root), self.dataset_root / "_qc")


if __name__ == "__main__":
    unittest.main()

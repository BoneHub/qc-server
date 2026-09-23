"""The admin panel.

Requirement under test: "there must be a simple admin panel that lets creating users
with api keys". The panel is a static page; everything it does goes through these
endpoints, each guarded by the server's admin key.
"""

from __future__ import annotations

import unittest

from tests.test_api import ApiTestCase


class AdminAuthTests(ApiTestCase):
    def test_the_panel_page_is_served(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])

    def test_every_admin_endpoint_needs_the_admin_key(self):
        for method, path in [
            ("get", "/admin/api/session"),
            ("get", "/admin/api/users"),
            ("get", "/admin/api/stats"),
            ("get", "/admin/api/assignments"),
            ("get", "/admin/api/submissions"),
            ("get", "/admin/api/config"),
        ]:
            self.assertEqual(getattr(self.client, method)(path).status_code, 401, path)

    def test_a_reviewer_key_does_not_open_the_admin_panel(self):
        response = self.client.get("/admin/api/users", headers={"X-Admin-Key": self.alice_key})
        self.assertEqual(response.status_code, 401)

    def test_the_session_check_describes_the_server(self):
        body = self.client.get("/admin/api/session", headers=self.admin_headers).json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["dataset_root"], str(self.dataset_root))
        self.assertEqual(body["config"]["eligible_label_values"], [1])


class AdminUserManagementTests(ApiTestCase):
    def test_creating_a_user_returns_the_key_exactly_once(self):
        response = self.client.post("/admin/api/users", json={"name": "carol"}, headers=self.admin_headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["api_key"].startswith("bhqc_"))
        self.assertIn("only once", body["warning"])
        self.assertNotIn("key_hash", body["user"])

        listed = self.client.get("/admin/api/users", headers=self.admin_headers).json()
        carol = next(u for u in listed if u["name"] == "carol")
        self.assertNotIn("key_hash", carol)
        self.assertTrue(body["api_key"].startswith(carol["key_prefix"]))

    def test_a_created_key_works_immediately_as_a_client(self):
        key = self.client.post("/admin/api/users", json={"name": "carol"}, headers=self.admin_headers).json()[
            "api_key"
        ]
        self.assertEqual(self.client.get("/api/v1/ping", headers={"X-API-Key": key}).json()["user"], "carol")

    def test_a_duplicate_name_is_a_409(self):
        response = self.client.post("/admin/api/users", json={"name": "alice"}, headers=self.admin_headers)
        self.assertEqual(response.status_code, 409)

    def test_a_blank_name_is_a_400(self):
        response = self.client.post("/admin/api/users", json={"name": "  "}, headers=self.admin_headers)
        self.assertEqual(response.status_code, 400)

    def test_a_user_can_be_scoped_to_specific_datasets(self):
        body = self.client.post(
            "/admin/api/users",
            json={"name": "carol", "allowed_dataset_ids": "2, 5", "note": "spine only"},
            headers=self.admin_headers,
        ).json()
        self.assertEqual(body["user"]["allowed_dataset_ids"], [2, 5])
        self.assertEqual(body["user"]["note"], "spine only")

    def test_dataset_scope_accepts_a_list_too(self):
        body = self.client.post(
            "/admin/api/users", json={"name": "carol", "allowed_dataset_ids": [3, 1]}, headers=self.admin_headers
        ).json()
        self.assertEqual(body["user"]["allowed_dataset_ids"], [1, 3])

    def test_a_non_numeric_dataset_scope_is_refused(self):
        response = self.client.post(
            "/admin/api/users", json={"name": "carol", "allowed_dataset_ids": "abc"}, headers=self.admin_headers
        )
        self.assertEqual(response.status_code, 400)

    def test_editing_the_note_does_not_widen_the_dataset_scope(self):
        self.client.post(
            "/admin/api/users", json={"name": "carol", "allowed_dataset_ids": [1]}, headers=self.admin_headers
        )
        body = self.client.patch("/admin/api/users/carol", json={"note": "back soon"}, headers=self.admin_headers)
        self.assertEqual(body.json()["allowed_dataset_ids"], [1])
        self.assertEqual(body.json()["note"], "back soon")

    def test_the_scope_can_be_cleared_explicitly(self):
        self.client.post(
            "/admin/api/users", json={"name": "carol", "allowed_dataset_ids": [1]}, headers=self.admin_headers
        )
        body = self.client.patch(
            "/admin/api/users/carol", json={"allowed_dataset_ids": None}, headers=self.admin_headers
        )
        self.assertIsNone(body.json()["allowed_dataset_ids"])

    def test_rotating_a_key_locks_out_the_old_one(self):
        new_key = self.client.post("/admin/api/users/alice/rotate-key", headers=self.admin_headers).json()["api_key"]
        self.assertEqual(self.client.get("/api/v1/ping", headers={"X-API-Key": new_key}).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/ping", headers=self.headers(self.alice_key)).status_code, 401)

    def test_a_user_can_be_disabled_and_enabled_again(self):
        self.client.post("/admin/api/users/alice/active", json={"active": False}, headers=self.admin_headers)
        self.assertEqual(self.client.get("/api/v1/ping", headers=self.headers(self.alice_key)).status_code, 403)
        self.client.post("/admin/api/users/alice/active", json={"active": True}, headers=self.admin_headers)
        self.assertEqual(self.client.get("/api/v1/ping", headers=self.headers(self.alice_key)).status_code, 200)

    def test_deleting_a_user_revokes_their_key(self):
        response = self.client.delete("/admin/api/users/alice", headers=self.admin_headers)
        self.assertEqual(response.json()["status"], "deleted")
        self.assertEqual(self.client.get("/api/v1/ping", headers=self.headers(self.alice_key)).status_code, 401)

    def test_acting_on_an_unknown_user_is_a_404(self):
        self.assertEqual(self.client.delete("/admin/api/users/nobody", headers=self.admin_headers).status_code, 404)
        self.assertEqual(
            self.client.post("/admin/api/users/nobody/rotate-key", headers=self.admin_headers).status_code, 404
        )

    def test_the_user_list_shows_progress_per_reviewer(self):
        handout = self.next_subject(self.alice_key)
        self.submit(self.alice_key, handout["assignment_id"], True, ["FEMUR_LEFT"])

        alice = next(u for u in self.client.get("/admin/api/users", headers=self.admin_headers).json()
                     if u["name"] == "alice")
        self.assertEqual(alice["confirmed"], 1)
        self.assertEqual(alice["open"], 0)


class AdminMonitoringTests(ApiTestCase):
    def test_stats_report_the_queue(self):
        body = self.client.get("/admin/api/stats", headers=self.admin_headers).json()
        self.assertEqual(body["total_subjects"], 3)
        self.assertEqual(body["eligible_subjects"], 3)
        self.assertEqual(body["available"], 3)
        self.assertEqual(body["datasets"], {"1": 3})

    def test_assignments_can_be_listed_and_filtered(self):
        handout = self.next_subject(self.alice_key)
        self.submit(self.alice_key, handout["assignment_id"], False)
        self.next_subject(self.bob_key)

        everything = self.client.get("/admin/api/assignments", headers=self.admin_headers).json()
        self.assertEqual(len(everything), 2)
        rejected = self.client.get("/admin/api/assignments?state=rejected", headers=self.admin_headers).json()
        self.assertEqual([a["user"] for a in rejected], ["alice"])

    def test_an_administrator_can_take_a_subject_back_from_a_reviewer(self):
        handout = self.next_subject(self.alice_key)
        response = self.client.post(
            f"/admin/api/assignments/{handout['assignment_id']}/release", headers=self.admin_headers
        )
        self.assertEqual(response.json()["state"], "released")
        self.assertEqual(self.next_subject(self.bob_key)["subject_key"], handout["subject_key"])

    def test_submissions_are_readable_from_the_panel(self):
        handout = self.next_subject(self.alice_key)
        self.submit(self.alice_key, handout["assignment_id"], True, ["FEMUR_LEFT"], comment="clean")

        entries = self.client.get("/admin/api/submissions?kind=submission", headers=self.admin_headers).json()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["user"], "alice")
        self.assertEqual(entries[0]["comment"], "clean")

    def test_the_index_can_be_rebuilt_on_demand(self):
        self.builder.add_subject(1, 99, segmentation={"FEMUR_LEFT": 1})
        body = self.client.post("/admin/api/refresh-index", headers=self.admin_headers).json()
        self.assertEqual(body["eligible_subjects"], 4)


class AdminConfigTests(ApiTestCase):
    def test_the_policy_can_be_read_and_changed(self):
        self.assertEqual(self.client.get("/admin/api/config", headers=self.admin_headers).json()["requeue_rejected"],
                         False)
        body = self.client.put(
            "/admin/api/config", json={"requeue_rejected": True, "lease_ttl_seconds": 120}, headers=self.admin_headers
        ).json()
        self.assertTrue(body["requeue_rejected"])
        self.assertEqual(body["lease_ttl_seconds"], 120)

    def test_a_policy_change_is_persisted_for_the_next_start(self):
        self.client.put("/admin/api/config", json={"lease_ttl_seconds": 300}, headers=self.admin_headers)
        from bonehub_quality_check_server.config import QCServerConfig

        self.assertEqual(QCServerConfig.load(self.state_dir / "config.json").lease_ttl_seconds, 300)

    def test_a_policy_change_takes_effect_at_once(self):
        """Narrowing the eligible statuses must re-scope the queue immediately."""
        self.client.put("/admin/api/config", json={"eligible_label_values": [2]}, headers=self.admin_headers)
        self.assertEqual(self.client.get("/admin/api/stats", headers=self.admin_headers).json()["eligible_subjects"], 0)
        self.assertEqual(self.client.post("/api/v1/subjects/next", headers=self.headers(self.alice_key)).status_code,
                         404)

    def test_restricting_the_served_datasets_at_runtime_works(self):
        self.builder.add_subject(2, 1, segmentation={"FEMUR_LEFT": 1})
        self.client.post("/admin/api/refresh-index", headers=self.admin_headers)
        self.assertEqual(
            sorted(self.client.get("/admin/api/stats", headers=self.admin_headers).json()["datasets"]), ["1", "2"]
        )
        self.client.put("/admin/api/config", json={"allowed_dataset_ids": [2]}, headers=self.admin_headers)
        self.assertEqual(
            list(self.client.get("/admin/api/stats", headers=self.admin_headers).json()["datasets"]), ["2"]
        )

    def test_an_unknown_config_field_is_refused(self):
        response = self.client.put("/admin/api/config", json={"nonsense": 1}, headers=self.admin_headers)
        self.assertEqual(response.status_code, 400)

    def test_an_invalid_label_status_is_refused(self):
        for statuses in ([3], [0], [42]):
            response = self.client.put(
                "/admin/api/config", json={"eligible_label_values": statuses}, headers=self.admin_headers
            )
            self.assertEqual(response.status_code, 400, statuses)

    def test_the_retired_confirmed_value_setting_is_refused(self):
        """Confirmed labels are always status 2, so there is nothing left to set."""
        response = self.client.put("/admin/api/config", json={"confirmed_label_value": 2}, headers=self.admin_headers)
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()

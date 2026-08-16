import unittest

from omniflow.config import AIRepairSettings
from omniflow.exceptions import OmniAPIError, SecurityPolicyError
from omniflow.repair.safety import compare_snapshots, inspect_repair_change, repair_target_files
from omniflow.repair.snapshot import fetch_authored_snapshot, restore_snapshot


class MemoryYamlClient:
    def __init__(self, files):
        self.files = dict(files)
        self.checksums = {name: f"checksum-{index}" for index, name in enumerate(self.files, start=1)}
        self.counter = len(self.files) + 1
        self.calls = []

    def get_model_yaml(self, *args, **kwargs):
        self.calls.append(("get", args, kwargs))
        return {
            "files": {
                name: {"contents": text, "checksum": self.checksums[name]}
                for name, text in self.files.items()
            }
        }

    def update_model_yaml(self, model_id, **kwargs):
        self.calls.append(("update", model_id, kwargs))
        name = kwargs["file_name"]
        previous = kwargs["previous_checksum"]
        if name in self.files and previous != self.checksums[name]:
            raise OmniAPIError("checksum conflict")
        self.files[name] = kwargs["yaml_text"]
        self.checksums[name] = f"checksum-{self.counter}"
        self.counter += 1
        return {"file_name": name, "success": True}

    def delete_model_yaml(self, model_id, **kwargs):
        self.calls.append(("delete", model_id, kwargs))
        name = kwargs["file_name"]
        del self.files[name]
        del self.checksums[name]
        return {"file_name": name, "success": True}


def snapshot(client):
    return fetch_authored_snapshot(client=client, model_id="model-1", branch_id="branch-1")


class RepairSafetyTests(unittest.TestCase):
    def test_snapshot_requires_checksums_and_keeps_yaml_in_memory(self):
        client = MemoryYamlClient({"orders.view": "name: orders\n"})
        captured = snapshot(client)
        self.assertEqual(captured.files["orders.view"].checksum, "checksum-1")
        self.assertEqual(len(captured.files["orders.view"].sha256), 64)

        class MissingChecksumClient:
            def get_model_yaml(self, *args, **kwargs):
                return {"files": {"orders.view": "name: orders\n"}}

        with self.assertRaises(SecurityPolicyError):
            snapshot(MissingChecksumClient())

    def test_validation_targets_must_be_existing_documented_files(self):
        captured = snapshot(MemoryYamlClient({"orders.view": "name: orders\n"}))
        issues = [{"message": "bad", "is_warning": False, "yaml_path": "orders.view,measures,total"}]
        self.assertEqual(repair_target_files(issues, captured), ("orders.view",))
        with self.assertRaises(SecurityPolicyError):
            repair_target_files([{"is_warning": False, "yaml_path": "missing.view"}], captured)
        with self.assertRaises(SecurityPolicyError):
            repair_target_files([{"is_warning": False, "yaml_path": "nested/orders.view"}], captured)

    def test_validation_targets_resolve_omni_semantic_names_to_nested_authored_files(self):
        captured = snapshot(
            MemoryYamlClient(
                {"omni_dbt_marts/fact_order_items.view": "name: omni_dbt_marts__fact_order_items\n"}
            )
        )
        issues = [
            {
                "message": "Field not found",
                "is_warning": False,
                "yaml_path": (
                    "omni_dbt_marts__fact_order_items.view,"
                    "measures,omniflow_ai_repair_test_revenue"
                ),
            }
        ]
        self.assertEqual(
            repair_target_files(issues, captured),
            ("omni_dbt_marts/fact_order_items.view",),
        )

    def test_validation_target_aliases_must_resolve_uniquely(self):
        captured = snapshot(
            MemoryYamlClient(
                {
                    "a__b/c.view": "name: first\n",
                    "a/b__c.view": "name: second\n",
                }
            )
        )
        with self.assertRaisesRegex(SecurityPolicyError, "ambiguously"):
            repair_target_files(
                [{"is_warning": False, "yaml_path": "a__b__c.view,dimensions,id"}],
                captured,
            )

    def test_validation_target_resolves_unique_nested_topic_basename(self):
        captured = snapshot(
            MemoryYamlClient(
                {
                    "examples/sample_command_center.topic": (
                        "base_view: sample_fact_order_items\n"
                    )
                }
            )
        )
        issues = [
            {
                "message": "No such view having base_view",
                "is_warning": False,
                "yaml_path": "sample_command_center.topic,base_view",
            }
        ]
        self.assertEqual(
            repair_target_files(issues, captured),
            ("examples/sample_command_center.topic",),
        )

    def test_validation_target_basename_must_resolve_uniquely(self):
        captured = snapshot(
            MemoryYamlClient(
                {
                    "Finance/orders.topic": "base_view: finance_orders\n",
                    "Operations/orders.topic": "base_view: operations_orders\n",
                }
            )
        )
        with self.assertRaisesRegex(SecurityPolicyError, "ambiguously"):
            repair_target_files(
                [{"is_warning": False, "yaml_path": "orders.topic,base_view"}],
                captured,
            )

    def test_safe_scoped_metadata_change_passes_inspection(self):
        client = MemoryYamlClient({"orders.view": "name: orders\ndescription: old\n"})
        before = snapshot(client)
        client.files["orders.view"] = "name: orders\ndescription: corrected\n"
        client.checksums["orders.view"] = "checksum-after"
        after = snapshot(client)
        result = inspect_repair_change(
            before=before,
            after=after,
            allowed_files=("orders.view",),
            settings=AIRepairSettings(),
        )
        self.assertEqual(result.modified_files, ("orders.view",))
        self.assertEqual(result.changed_lines, 2)
        self.assertNotIn("old", str(result.report()))

    def test_inspection_rejects_structural_scope_and_size_violations(self):
        model_file = "name: model\n"
        base_client = MemoryYamlClient(
            {"model": model_file, "orders.view": "name: orders\ndescription: old\n"}
        )
        before = snapshot(base_client)
        cases = [
            (
                {
                    "model": model_file,
                    "orders.view": "name: orders\ndescription: new\n",
                    "extra.view": "name: extra\n",
                },
                ("orders.view",),
            ),
            ({"model": model_file}, ("orders.view",)),
            ({"model": model_file, "orders.view": "name: orders\ndescription: new\n"}, ("other.view",)),
        ]
        for files, allowed in cases:
            with self.subTest(files=files, allowed=allowed):
                with self.assertRaises(SecurityPolicyError):
                    inspect_repair_change(
                        before=before,
                        after=snapshot(MemoryYamlClient(files)),
                        allowed_files=allowed,
                        settings=AIRepairSettings(),
                    )

        changed = snapshot(
            MemoryYamlClient({"model": model_file, "orders.view": "name: orders\ndescription: new\n"})
        )
        with self.assertRaises(SecurityPolicyError):
            inspect_repair_change(
                before=before,
                after=changed,
                allowed_files=("orders.view",),
                settings=AIRepairSettings(max_changed_lines=1),
            )

    def test_inspection_rejects_sensitive_keys_and_secret_like_values(self):
        before = snapshot(MemoryYamlClient({"orders.view": "name: orders\ndescription: old\n"}))
        unsafe_documents = (
            "name: orders\nsql: select * from private_table\n",
            "name: orders\ndescription: 'Bearer sample-value'\n",  # pragma: allowlist secret
            "name: orders\napi_key: sample\n",  # pragma: allowlist secret
        )
        for document in unsafe_documents:
            with self.subTest(document=document):
                with self.assertRaises(SecurityPolicyError):
                    inspect_repair_change(
                        before=before,
                        after=snapshot(MemoryYamlClient({"orders.view": document})),
                        allowed_files=("orders.view",),
                        settings=AIRepairSettings(),
                    )

    def test_rollback_restores_modified_deleted_and_added_files_exactly(self):
        client = MemoryYamlClient(
            {
                "orders.view": "name: orders\ndescription: original\n",
                "orders.topic": "name: orders\nlabel: Orders\n",
            }
        )
        before = snapshot(client)
        client.files["orders.view"] = "name: orders\ndescription: changed\n"
        client.checksums["orders.view"] = "checksum-changed"
        del client.files["orders.topic"]
        del client.checksums["orders.topic"]
        client.files["temporary.view"] = "name: temporary\n"
        client.checksums["temporary.view"] = "checksum-temporary"
        after = snapshot(client)

        result = restore_snapshot(
            client=client,
            model_id="model-1",
            branch_id="branch-1",
            desired=before,
            expected_current=after,
        )
        self.assertTrue(result["verified"])
        self.assertTrue(snapshot(client).content_matches(before))
        self.assertEqual(result["restored_files"], 1)
        self.assertEqual(result["recreated_files"], 1)
        self.assertEqual(result["deleted_files"], 1)

    def test_rollback_stops_on_concurrent_branch_change(self):
        client = MemoryYamlClient({"orders.view": "name: orders\ndescription: original\n"})
        before = snapshot(client)
        client.files["orders.view"] = "name: orders\ndescription: ai change\n"
        client.checksums["orders.view"] = "checksum-ai"
        after = snapshot(client)
        client.files["orders.view"] = "name: orders\ndescription: human change\n"
        client.checksums["orders.view"] = "checksum-human"
        with self.assertRaises(SecurityPolicyError):
            restore_snapshot(
                client=client,
                model_id="model-1",
                branch_id="branch-1",
                desired=before,
                expected_current=after,
            )
        self.assertFalse(any(call[0] in {"update", "delete"} for call in client.calls))

    def test_snapshot_diff_report_contains_metadata_only(self):
        before = snapshot(MemoryYamlClient({"orders.view": "description: private before\n"}))
        after = snapshot(MemoryYamlClient({"orders.view": "description: private after\n"}))
        report = compare_snapshots(before, after).report()
        self.assertNotIn("private before", str(report))
        self.assertNotIn("private after", str(report))


if __name__ == "__main__":
    unittest.main()

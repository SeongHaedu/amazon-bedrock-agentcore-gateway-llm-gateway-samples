"""Offline checks for retaining and recovering a demonstration deployment."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from runtime_v2 import manage, verify


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.d = Mock()
        self.d.path = Path(self.directory.name) / "state.json"
        self.d.run = "llmgw-v2-123456abcdef"
        self.d.state = {
            "gateway_id": "gateway", "rate_id": "owned-rate",
            "pool_id": "pool", "config": {"rpm": 60, "concurrency": 2},
        }
        self.entries = manage.rate_entries("*", rpm=60, concurrency=2)
        self.recovery = self.d.path.with_name("verification-rate-recovery.json")
        manage.save_private(self.recovery, {
            "gateway_id": "gateway", "rate_id": "owned-rate",
            "entries": self.entries, "probe_username": "probe-12345678",
        })
        self.normal = {
            "status": "ACTIVE", "description": "Owned by " + self.d.run, "entries": self.entries,
        }
        self.changed = dict(self.normal, entries=self.entries + manage.rate_entries("probe", rpm=0))
        self.cognito = self.d.client.return_value
        self.cognito.exceptions.UserNotFoundException = type("UserNotFoundException", (Exception,), {})
        self.cognito.describe_user_pool.return_value = {"UserPool": {"Name": self.d.run + "-users"}}

    def test_recovery_restores_limits_and_disables_probe_without_deleting_resources(self):
        self.d.control.get_gateway_rate_limit.side_effect = [self.changed, self.normal]
        with patch.object(manage, "set_rate") as setter, patch.object(manage, "emit"):
            manage.restore_verification(self.d)
        setter.assert_called_once_with(self.d, "owned-rate", self.entries)
        self.cognito.admin_disable_user.assert_called_once_with(
            UserPoolId="pool", Username="probe-12345678")
        self.assertFalse(self.recovery.exists())
        self.d.control.delete_gateway.assert_not_called()
        self.d.control.delete_agent_runtime.assert_not_called()

    def test_failed_confirmation_keeps_recovery_and_still_disables_probe(self):
        self.d.control.get_gateway_rate_limit.return_value = self.changed
        with patch.object(manage, "set_rate"), self.assertRaisesRegex(ValueError, "restoration failed"):
            manage.restore_verification(self.d)
        self.assertTrue(self.recovery.exists())
        self.cognito.admin_disable_user.assert_called_once()

    def test_initial_rate_read_failure_still_disables_owned_probe(self):
        self.d.control.get_gateway_rate_limit.side_effect = RuntimeError("Rate API unavailable")
        with self.assertRaisesRegex(RuntimeError, "Rate API unavailable"):
            manage.restore_verification(self.d)
        self.assertTrue(self.recovery.exists())
        self.cognito.describe_user_pool.assert_called_once_with(UserPoolId="pool")
        self.cognito.admin_disable_user.assert_called_once_with(
            UserPoolId="pool", Username="probe-12345678")

    def test_initial_rate_failure_cannot_disable_a_user_in_a_foreign_pool(self):
        self.d.control.get_gateway_rate_limit.side_effect = RuntimeError("Rate API unavailable")
        self.cognito.describe_user_pool.return_value = {"UserPool": {"Name": "another-pool"}}
        with self.assertRaisesRegex(ValueError, "pool ownership mismatch"):
            manage.restore_verification(self.d)
        self.cognito.admin_disable_user.assert_not_called()
        self.assertTrue(self.recovery.exists())

    def test_disable_failure_keeps_evidence_even_when_rates_are_restored(self):
        self.d.control.get_gateway_rate_limit.return_value = self.normal
        self.cognito.admin_disable_user.side_effect = RuntimeError("Cognito unavailable")
        with self.assertRaisesRegex(RuntimeError, "Cognito unavailable"):
            manage.restore_verification(self.d)
        self.assertTrue(self.recovery.exists())

    def test_foreign_gateway_recovery_is_rejected_before_aws_changes(self):
        record = json.loads(self.recovery.read_text())
        record["gateway_id"] = "different-gateway"
        manage.save_private(self.recovery, record)
        with self.assertRaisesRegex(ValueError, "Gateway does not belong"):
            manage.restore_verification(self.d)
        self.d.control.get_gateway_rate_limit.assert_not_called()
        self.cognito.admin_disable_user.assert_not_called()

    def test_foreign_live_rate_resource_is_not_modified(self):
        self.d.control.get_gateway_rate_limit.return_value = dict(
            self.changed, description="Owned by a different deployment")
        with patch.object(manage, "set_rate") as setter, self.assertRaisesRegex(ValueError, "ownership"):
            manage.restore_verification(self.d)
        setter.assert_not_called()
        self.cognito.admin_disable_user.assert_not_called()

    def test_pending_recovery_prevents_new_verification(self):
        with self.assertRaisesRegex(ValueError, "restore-verification"):
            verify.verify(self.d)
        self.d.control.get_agent_runtime.assert_not_called()
        self.assertTrue(self.recovery.exists())

    def test_failed_run_does_not_leave_an_old_passing_report(self):
        report = self.d.path.with_name("verification.json")
        manage.save_private(report, {"passed": True, "checks": [{"name": "previous"}]})
        with self.assertRaisesRegex(ValueError, "restore-verification"):
            verify.run_verification(self.d)
        result = json.loads(report.read_text())
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(result["recovery_pending"])
        self.assertIn("started_at", result)
        archives = list((report.parent / "verification-history").glob("*.json"))
        self.assertEqual(len(archives), 1)
        self.assertTrue(json.loads(archives[0].read_text())["passed"])

    def test_keyboard_interrupt_is_recorded_as_incomplete(self):
        with patch.object(verify, "verify", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            verify.run_verification(self.d)
        result = json.loads(self.d.path.with_name("verification.json").read_text())
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "INTERRUPTED")

    def test_authentication_failure_replaces_and_archives_previous_success(self):
        report = self.d.path.with_name("verification.json")
        manage.save_private(report, {"passed": True, "status": "PASSED"})
        with patch.object(verify, "Deployment", side_effect=ValueError("AWS account mismatch")):
            with self.assertRaisesRegex(ValueError, "AWS account mismatch"):
                verify.run_verification(state_path=self.d.path)
        result = json.loads(report.read_text())
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(result["passed"])
        self.assertEqual(result["error"]["message"], "AWS account mismatch")
        self.assertEqual(len(list((report.parent / "verification-history").glob("*.json"))), 1)

    def test_config_export_excludes_identity_and_refuses_to_overwrite(self):
        self.d.state["account_id"] = "private-account"
        destination = Path(self.directory.name) / "local/config.json"
        arguments = SimpleNamespace(state=str(self.d.path), output=str(destination))
        with patch.object(manage, "Deployment", return_value=self.d), patch.object(manage, "emit"):
            manage.export_config(arguments)
            self.assertEqual(json.loads(destination.read_text()), self.d.state["config"])
            with self.assertRaisesRegex(ValueError, "already exists"):
                manage.export_config(arguments)


if __name__ == "__main__":
    unittest.main()

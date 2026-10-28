"""Regression tests for owned-resource cleanup and verification state."""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cleanup
import configure_limits
import verify_controls


class CleanupSafetyTests(unittest.TestCase):
    def environment(self, resources=()):
        client = Mock()
        env = SimpleNamespace(
            run="fixture-run", state={"resources": list(resources)},
            client=Mock(return_value=client))
        return env, client

    def test_recreated_role_prevents_child_policy_deletion(self):
        role = {"kind": "role", "id": "fixture-role", "arn": "fixture-arn",
                "role_id": "original-id", "deleted": False}
        env, client = self.environment([role])
        client.get_role.return_value = {"Role": {
            "RoleId": "replacement-id", "Arn": "fixture-arn",
            "Tags": [{"Key": "ManagedBy", "Value": cleanup.MANAGED_BY},
                     {"Key": "RunId", "Value": env.run}]}}
        with self.assertRaisesRegex(ValueError, "identity"):
            cleanup.remove(env, {"kind": "role_policy", "id": "fixture-policy",
                                 "role_name": "fixture-role"})
        client.delete_role_policy.assert_not_called()

    def test_pool_is_retained_when_a_child_failed(self):
        env, client = self.environment([
            {"kind": "user", "pool_id": "fixture-pool", "deleted": False}])
        with self.assertRaisesRegex(ValueError, "child"):
            cleanup.remove(env, {"kind": "pool", "id": "fixture-pool"})
        client.delete_user_pool.assert_not_called()

    def test_async_engine_delete_failure_is_not_success(self):
        env, client = self.environment()
        client.get_policy_engine.side_effect = [
            {"name": "fixture", "policyEngineArn": "fixture-arn", "status": "ACTIVE"},
            {"status": "DELETE_FAILED"}]
        with self.assertRaisesRegex(ValueError, "deletion failed"):
            cleanup.remove(env, {"kind": "engine", "id": "fixture", "name": "fixture", "arn": "fixture-arn"})

    def test_async_engine_delete_waits_for_absence(self):
        env, client = self.environment()
        client.get_policy_engine.side_effect = [
            {"name": "fixture", "policyEngineArn": "fixture-arn", "status": "ACTIVE"},
            {"status": "DELETING"},
            ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetPolicyEngine")]
        with patch.object(cleanup.time, "sleep"):
            cleanup.remove(env, {"kind": "engine", "id": "fixture", "name": "fixture", "arn": "fixture-arn"})
        self.assertEqual(client.get_policy_engine.call_count, 3)


class VerificationStateTests(unittest.TestCase):
    def test_failed_new_access_check_invalidates_previous_pass(self):
        env = SimpleNamespace(
            state={"verification": {"access": "passed"}, "resources": [{"kind": "rate"}]},
            save=Mock())
        with self.assertRaises(ValueError):
            verify_controls.verify_access(env, [])
        self.assertEqual(env.state["verification"]["access"], "running")
        env.save.assert_called_once()

    def test_positive_rate_recovery_clears_restore_flag(self):
        env = Mock()
        env.state = {"verification": {"access": "passed"}, "resources": [{"kind": "rate"}],
                     "rate_restore_required": True}
        with patch.object(configure_limits, "Environment", return_value=env), \
             patch.object(configure_limits, "update_rate") as update, \
             patch.object(sys, "argv", ["configure_limits.py", "--premium-rpm", "240"]):
            configure_limits.main()
        update.assert_called_once_with(env, 240.0)
        self.assertFalse(env.state["rate_restore_required"])
        self.assertEqual(env.state["verified_fallback_limit_keys"], [])


if __name__ == "__main__":
    unittest.main()

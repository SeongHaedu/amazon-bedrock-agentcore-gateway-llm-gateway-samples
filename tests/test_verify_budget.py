"""Budget verification must invalidate stale results and preserve concurrent edits."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_budget


class VerificationTests(unittest.TestCase):
    def test_preflight_failure_invalidates_success_and_writes_report(self):
        with tempfile.TemporaryDirectory() as folder:
            env = SimpleNamespace(
                state={"budget": {"verification": "native_accounting_and_zero_cap_passed"}},
                path=Path(folder) / "resources.json", save=Mock(),
                one=Mock(side_effect=ValueError("missing owned function")))
            with self.assertRaisesRegex(ValueError, "missing owned"):
                verify_budget.verify(env)
            self.assertEqual(env.state["budget"]["verification"], "failed")
            self.assertFalse(json.loads((Path(folder) / "verify-budget.json").read_text())["passed"])

    def test_restoration_refuses_concurrent_access_configuration_change(self):
        client = Mock()
        client.get_function_configuration.return_value = {
            "FunctionArn": "fixture-arn", "RevisionId": "someone-elses-revision",
            "Environment": {"Variables": {"MODEL_ACCESS_JSON": "changed"}}}
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            verify_budget.restore_environment(
                client, {"id": "fixture", "arn": "fixture-arn"},
                {"MODEL_ACCESS_JSON": "original", "DAILY_BUDGET_USD": "0"},
                {"MODEL_ACCESS_JSON": "original", "DAILY_BUDGET_USD": "1"}, "our-revision")
        client.update_function_configuration.assert_not_called()

    def test_restoration_supplies_revision_precondition(self):
        client = Mock()
        current = {"DAILY_BUDGET_USD": "0", "MODEL_ACCESS_JSON": "original"}
        original = {**current, "DAILY_BUDGET_USD": "1"}
        client.get_function_configuration.side_effect = [{
            "FunctionArn": "fixture-arn", "RevisionId": "our-revision",
            "Environment": {"Variables": current}}, {
            "FunctionArn": "fixture-arn", "RevisionId": "completed-restoration-revision",
            "Environment": {"Variables": original}}]
        verify_budget.restore_environment(
            client, {"id": "fixture", "arn": "fixture-arn"}, current, original, "our-revision")
        client.update_function_configuration.assert_called_once_with(
            FunctionName="fixture", RevisionId="our-revision", Environment={"Variables": original})

    def test_unknown_update_result_is_not_blindly_rolled_back(self):
        client = Mock()
        with self.assertRaisesRegex(ValueError, "unknown"):
            verify_budget.restore_environment(client, {"id": "fixture"}, {}, {}, None)
        client.update_function_configuration.assert_not_called()

    def test_completed_revision_is_read_after_asynchronous_update(self):
        client = Mock()
        client.update_function_configuration.return_value = {"RevisionId": "transient-revision"}
        expected = {"DAILY_BUDGET_USD": "0"}
        before = {"CodeSha256": "same-code", "Handler": "access.handler"}
        client.get_function_configuration.return_value = {
            **before, "FunctionArn": "fixture-arn", "RevisionId": "completed-revision",
            "Environment": {"Variables": expected}}
        revision = verify_budget.completed_environment(
            client, {"id": "fixture", "arn": "fixture-arn"}, expected, before)
        self.assertEqual(revision, "completed-revision")
        client.get_waiter.return_value.wait.assert_called_once()

    def test_completed_update_refuses_changed_code(self):
        client = Mock()
        client.get_function_configuration.return_value = {
            "FunctionArn": "fixture-arn", "RevisionId": "revision",
            "CodeSha256": "different-code", "Environment": {"Variables": {}}}
        with self.assertRaisesRegex(ValueError, "differs from the expected"):
            verify_budget.completed_environment(
                client, {"id": "fixture", "arn": "fixture-arn"}, {},
                {"CodeSha256": "original-code"})


if __name__ == "__main__":
    unittest.main()

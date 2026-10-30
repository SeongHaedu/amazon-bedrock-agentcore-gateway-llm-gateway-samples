"""Offline regressions for deployment recovery and credential-free invocation."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from botocore.session import Session
from botocore.validate import validate_parameters
from botocore.exceptions import ClientError
import httpx

from runtime_v2 import cleanup, client, manage


class ManagementTests(unittest.TestCase):
    def test_saved_policy_is_checked_before_reusing_it(self):
        d = Mock()
        d.run = "llmgw-v2-123456abcdef"
        model = {"model_id": "test-model", "api": "messages"}
        name = "llmgw_v2_123456abcdef_employee_standard"
        d.state = {"engine_id": "engine", "gateway_arn": "test-gateway",
                   "users": {"employee": {"sub": "user-sub"}},
                   "config": {"models": {"standard": model}},
                   "policies": [{"name": name, "id": "policy"}]}
        d.wait.return_value = {"definition": {"cedar": {"statement": "different permission"}}}
        with self.assertRaisesRegex(ValueError, "definition differs"):
            manage.permit_user(d, "employee", ["standard"])
        d.control.create_policy.assert_not_called()
        d.wait.assert_called_once_with("get_policy", "ACTIVE", policyEngineId="engine", policyId="policy")

    def test_policy_creation_has_deployment_scope_and_valid_sdk_shape(self):
        d = Mock()
        d.run = "llmgw-v2-123456abcdef"
        d.state = {"engine_id": "engine-1234567890", "gateway_arn": "test-gateway",
                   "users": {"probe-01234567": {"sub": "user-sub"}},
                   "config": {"models": {"standard": {"model_id": "test-model", "api": "messages"}}},
                   "policies": []}
        d.control.create_policy.return_value = {"policyId": "policy"}
        manage.permit_user(d, "probe-01234567", ["standard"])
        parameters = d.control.create_policy.call_args.kwargs
        validate_parameters(parameters, Session().get_service_model("bedrock-agentcore-control")
                            .operation_model("CreatePolicy").input_shape)
        self.assertTrue(parameters["name"].startswith(d.run.replace("-", "_")))
        self.assertLessEqual(len(parameters["name"]), 48)
        self.assertGreaterEqual(len(parameters["clientToken"]), 33)
        statement = parameters["definition"]["cedar"]["statement"]
        for field in ('OAuthUser::"user-sub"', 'Gateway::"test-gateway"', 'context.input.model == "test-model"'):
            self.assertIn(field, statement)

    def test_interrupted_rate_creation_resumes_the_existing_resource(self):
        d = Mock()
        d.run = "llmgw-v2-123456abcdef"
        d.state = {"gateway_id": "gateway", "config": {"rpm": 60, "concurrency": 2}}
        d.control.get_gateway_rate_limit.return_value = {
            "description": "Owned by " + d.run, "entries": manage.rate_entries("*", rpm=60, concurrency=2)}
        manage.configure_rate(d)
        self.assertEqual(d.state["rate_id"], d.run + "-per-sub")
        d.control.create_gateway_rate_limit.assert_not_called()
        d.wait.assert_called_once()

    def test_private_state_replacement_keeps_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("{}")
            path.chmod(0o644)
            manage.save_private(path, {"step": 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {"step": 1})

    def test_upstream_template_matches_pinned_digest(self):
        vendor = manage.HERE / "vendor/usage-interceptor"
        self.assertEqual(manage.hashlib.sha256((vendor / "standalone.yaml").read_bytes()).hexdigest(),
                         json.loads((vendor / "SOURCE.json").read_text())["sha256"])

    def test_budget_read_uses_upstream_user_key(self):
        d = Mock()
        d.state = {"users": {"employee": {"sub": "subject-id"}}, "spend_table": "sample-table"}
        manage.spend(d, "employee")
        d.client.return_value.get_item.assert_called_once_with(
            TableName="sample-table", Key={"pk": {"S": "USER#subject-id"}}, ConsistentRead=True)


class EndUserClientTests(unittest.TestCase):
    def test_login_and_runtime_call_need_no_aws_credentials(self):
        seen = []

        def handler(request):
            seen.append(request)
            if len(seen) == 1:
                self.assertNotIn("authorization", request.headers)
                self.assertEqual(json.loads(request.content)["AuthFlow"], "USER_PASSWORD_AUTH")
                return httpx.Response(200, json={"AuthenticationResult": {"AccessToken": "user-jwt"}})
            self.assertEqual(request.headers["Authorization"], "Bearer user-jwt")
            self.assertEqual(json.loads(request.content), {"task": "Write a plan.", "model": "standard"})
            return httpx.Response(200, json={"status": "completed"})

        real_client = httpx.Client
        with patch.dict("os.environ", {}, clear=True), patch.object(
            client.httpx, "Client",
            side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
        ):
            status, body = client.invoke(
                {"region": "us-test-1", "client_id": "demo",
                 "runtime_url": "https://bedrock-agentcore.us-test-1.amazonaws.com/runtimes/demo/invocations"},
                "employee", "example-password", "Write a plan.", "standard")
        self.assertEqual((status, body["status"], len(seen)), (200, "completed", 2))

    def test_untrusted_runtime_url_is_rejected_before_sending_the_password(self):
        with patch.object(client.httpx, "Client") as http, self.assertRaises(ValueError):
            client.invoke({"region": "us-test-1", "client_id": "demo",
                           "runtime_url": "https://untrusted.example/runtimes/demo/invocations"},
                          "employee", "example-password", "task", "standard")
        http.assert_not_called()


class CleanupRecoveryTests(unittest.TestCase):
    def deployment(self):
        d = Mock()
        d.state = {"stack_name": "owned-stack", "created": {}}
        d.tags = {"ManagedBy": "sample", "RunId": "owned-run"}
        clients = {}
        d.client.side_effect = lambda service: clients.setdefault(service, Mock())
        d.client("cloudformation").describe_stacks.side_effect = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
            "DescribeStacks",
        )
        return d

    def test_role_deletion_resumes_after_inline_policy_was_deleted(self):
        d = self.deployment()
        d.state["runtime_role_name"] = "owned-role"
        iam = d.client("iam")
        missing = type("NoSuchEntityException", (Exception,), {})
        iam.exceptions.NoSuchEntityException = missing
        iam.get_role.return_value = {"Role": {
            "Tags": [{"Key": k, "Value": v} for k, v in d.tags.items()]}}
        iam.delete_role_policy.side_effect = missing()
        with patch.object(cleanup, "emit"):
            cleanup.cleanup(d)
        iam.delete_role.assert_called_once_with(RoleName="owned-role")
        self.assertTrue(d.state["deleted"])

    def test_already_deleted_bucket_does_not_block_completion(self):
        d = self.deployment()
        d.state.update(bucket="owned-bucket", created={"bucket": True})
        s3 = d.client("s3")
        missing = type("NoSuchBucket", (Exception,), {})
        s3.exceptions.NoSuchBucket = missing
        s3.get_bucket_tagging.side_effect = missing()
        with patch.object(cleanup, "emit"):
            cleanup.cleanup(d)
        s3.delete_bucket.assert_not_called()
        self.assertFalse(d.state["created"]["bucket"])
        self.assertTrue(d.state["deleted"])

    def test_existing_bucket_must_still_pass_ownership_checks(self):
        d = self.deployment()
        d.state.update(bucket="unowned-bucket", created={"bucket": True})
        s3 = d.client("s3")
        s3.exceptions.NoSuchBucket = type("NoSuchBucket", (Exception,), {})
        s3.get_bucket_tagging.return_value = {
            "TagSet": [{"Key": "RunId", "Value": "different-run"}]}
        with self.assertRaisesRegex(ValueError, "ownership tags mismatch"):
            cleanup.cleanup(d)
        s3.get_paginator.assert_not_called()
        s3.delete_bucket.assert_not_called()
        self.assertNotIn("deleted", d.state)


if __name__ == "__main__":
    unittest.main()

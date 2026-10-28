"""Offline authorization checks with synthetic identities and native URI paths."""
import base64
import copy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote

import access_interceptor


def event(user, model="standard", **changes):
    payload = base64.urlsafe_b64encode(json.dumps({"sub": user}).encode()).decode().rstrip("=")
    request = {
        "httpMethod": "POST",
        "path": "/inference/model/" + quote(model, safe="") + "/invoke-with-response-stream",
        "headers": {"Authorization": "Bearer fixture." + payload + ".fixture"},
        "body": base64.b64encode(b'{"messages":[]}').decode(),
    }
    request.update(changes)
    return {"interceptorInputVersion": "1.0", "http": {"gatewayRequest": request}}


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.context = SimpleNamespace(client_context=SimpleNamespace(custom={
            "REQUEST_ID": "fixture-request", "GATEWAY_ARN": "fixture-gateway"}))
        self.settings = patch.dict(os.environ, {
            "EXPECTED_GATEWAY_ARN": "fixture-gateway", "ENABLE_BUDGET": "false",
            "MODEL_ACCESS_JSON": json.dumps({
                "a": ["standard", "premium", "target/standard", "target/premium"],
                "b": ["standard", "target/standard"],
                "c": ["standard", "premium", "target/standard", "target/premium"]})})
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def invoke(self, data):
        original = copy.deepcopy(data)
        result = access_interceptor.request_handler(data, self.context)
        self.assertEqual(data, original, "Authorization must not transform request input")
        return result["http"].get("transformedGatewayResponse", {}).get("statusCode", 200)

    def test_six_user_model_combinations(self):
        for user in ("a", "b", "c"):
            for model in ("standard", "premium"):
                with self.subTest(user=user, model=model):
                    self.assertEqual(self.invoke(event(user, model)),
                                     403 if (user, model) == ("b", "premium") else 200)

    def test_body_model_cannot_override_uri(self):
        body = base64.b64encode(b'{"model":"standard","messages":[]}').decode()
        self.assertEqual(self.invoke(event("b", "premium", body=body)), 403)

    def test_qualified_uri_is_checked_against_exact_allowlist(self):
        self.assertEqual(self.invoke(event("b", "target/standard")), 200)
        self.assertEqual(self.invoke(event("b", "other/standard")), 403)
        self.assertEqual(self.invoke(event("b", "target/premium")), 403)

    def test_unknown_user_model_and_operation_fail_closed(self):
        self.assertEqual(self.invoke(event("unknown")), 403)
        self.assertEqual(self.invoke(event("a", "unknown")), 403)
        self.assertEqual(self.invoke(event("a", httpMethod="GET")), 403)
        self.assertEqual(self.invoke(event("a", path="/inference/v1/messages")), 400)

    def test_missing_or_ambiguous_bearer_fails_closed(self):
        self.assertEqual(self.invoke(event("a", headers={})), 401)
        e = event("a")
        e["http"]["gatewayRequest"]["headers"]["authorization"] = "Bearer extra.value.value"
        self.assertEqual(self.invoke(e), 401)

    def test_unexpected_gateway_fails_closed(self):
        self.context.client_context.custom["GATEWAY_ARN"] = "other-gateway"
        self.assertEqual(self.invoke(event("a")), 403)

    def test_bad_config_fails_closed(self):
        with patch.dict(os.environ, {"MODEL_ACCESS_JSON": '{"a":"standard"}'}):
            self.assertEqual(self.invoke(event("a")), 503)

    def test_budget_runs_only_after_access_is_allowed(self):
        with patch.dict(os.environ, {"ENABLE_BUDGET": "true"}), \
             patch.object(access_interceptor.protocol, "request_handler") as budget:
            budget.return_value = {"interceptorOutputVersion": "1.0", "http": {}}
            self.assertEqual(self.invoke(event("b", "premium")), 403)
            budget.assert_not_called()
            self.assertEqual(self.invoke(event("b", "standard")), 200)
            budget.assert_called_once()


if __name__ == "__main__":
    unittest.main()

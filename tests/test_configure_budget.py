"""Offline opt-in deployment tests; real SDK shapes, fake AWS and owned ledger."""
import ast
import base64
import copy
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from botocore import xform_name
from botocore.exceptions import ClientError, ParamValidationError
from botocore.session import Session
from botocore.validate import validate_parameters

from scripts import configure_budget as deploy
from scripts.common import CONTROL, Environment, MANAGED_BY


MODEL_IDS = {"standard": "fixture.standard", "premium": "fixture.premium"}
RATES = {
    "input_per_million_usd": "1", "output_per_million_usd": "2",
    "cache_read_per_million_usd": "0.1", "cache_write_5m_per_million_usd": "1.25",
    "cache_write_1h_per_million_usd": "2",
}
PRICES = {model: copy.deepcopy(RATES) for model in MODEL_IDS.values()}
SDK = Session()
REAL_REPO = deploy.REPO


def failure(operation):
    return ClientError({"Error": {"Code": "FixtureFailure", "Message": "simulated"}}, operation)


class FakeClient:
    def __init__(self, env, service):
        self.env, self.service = env, service
        model = SDK.get_service_model(service)
        self.meta = SimpleNamespace(
            service_model=model,
            method_to_api_mapping={xform_name(name): name for name in model.operation_names},
        )

    def get_waiter(self, name):
        return SimpleNamespace(wait=lambda **params: self.env.calls.append(
            (self.service, "wait:" + name, copy.deepcopy(params))))

    def __getattr__(self, operation):
        if operation not in self.meta.method_to_api_mapping:
            raise AttributeError(operation)

        def call(**params):
            validate_parameters(params, self.meta.service_model.operation_model(
                self.meta.method_to_api_mapping[operation]).input_shape)
            return self.env.api(self.service, operation, params)
        return call


class FakeEnvironment(Environment):
    """Reuse common.create/one/gateway_update; only API calls and saves are fake."""

    def __init__(self, path):
        self.path = path
        self.region, self.account, self.partition = "fixture-region", "0" * 12, "aws"
        self.clients, self.calls, self.snapshots = {}, [], []
        self.failure = None
        self.ttl_status = "DISABLED"
        self.response_hash_mismatch = False
        self.concurrent_gateway = False
        self.concurrent_access = False
        self.revision_race = None
        self.gateway_reads = 0
        run = "llmgw-" + "a" * 12
        self.state = {
            "schema": 1, "run_id": run, "resources": [], "intents": [],
            "models": {label: {"id": model} for label, model in MODEL_IDS.items()},
            "verification": {"existing": "preserve"},
        }

        def row(kind, suffix, label, **fields):
            name = run + suffix
            item = {"kind": kind, "id": name, "name": name, "label": label,
                    "run_id": run, "deleted": False, **fields}
            self.state["resources"].append(item)
            return item

        gateway_role = row("role", "-gateway", "gateway",
                           arn=deploy._arn(self, "iam", "role/" + run + "-gateway", global_service=True),
                           role_id="fixture-gateway-role")
        gateway = row("gateway", "-gateway", None, arn=deploy._arn(self, "bedrock-agentcore", "gateway/fixture"))
        access_role = row("role", "-access", "access",
                          arn=deploy._arn(self, "iam", "role/" + run + "-access", global_service=True),
                          role_id="fixture-access-role")
        access = row("function", "-access-request", "access-request",
                     arn=deploy._arn(self, "lambda", "function:" + run + "-access-request"),
                     handler=deploy.ACCESS_HANDLER)
        row("log_group", "-access-logs", "access-request",
            arn=deploy._arn(self, "logs", "log-group:" + run + "-access-logs"))
        row("role_policy", "-access-logs-policy", "access", role_name=access_role["id"])
        row("role_policy", "-access-invoke-policy", "access", role_name=gateway_role["id"])
        access_config = {
            "FunctionName": access["id"], "FunctionArn": access["arn"], "Handler": deploy.ACCESS_HANDLER,
            "Role": access_role["arn"], "Runtime": "python3.12", "State": "Active",
            "LastUpdateStatus": "Successful", "RevisionId": "revision-1", "CodeSha256": "baseline-code-hash",
            "LoggingConfig": {"LogFormat": "Text", "LogGroup": run + "-access-logs"},
            "Environment": {"Variables": {
                "MODEL_ACCESS_JSON": json.dumps({
                    "fixture-sub-a": list(MODEL_IDS.values()),
                    "fixture-sub-b": [MODEL_IDS["standard"]],
                    "fixture-sub-c": list(MODEL_IDS.values()),
                }),
                "EXPECTED_GATEWAY_ARN": gateway["arn"], "ENABLE_BUDGET": "false",
                "OTHER_ACCESS_SETTING": "preserve",
            }},
        }
        self.functions = {access["id"]: access_config}
        self.access_id = access["id"]
        self.gateway = {
            "gatewayId": gateway["id"], "gatewayArn": gateway["arn"], "name": gateway["name"],
            "roleArn": gateway_role["arn"], "status": "READY", "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": {"customJWTAuthorizer": {
                "discoveryUrl": "https://issuer.example.invalid/.well-known/openid-configuration",
                "allowedClients": ["fixture-client"],
            }},
            "interceptorConfigurations": [{
                "interceptor": {"lambda": {"arn": access["arn"]}}, "interceptionPoints": ["REQUEST"],
                "inputConfiguration": {"passRequestHeaders": True},
            }],
            "policyEngineConfiguration": {
                "arn": deploy._arn(self, "bedrock-agentcore", "policy-engine/fixture"), "mode": "ENFORCE",
            },
            "description": "preserve-existing", "protocolType": "MCP",
        }

    @property
    def access(self):
        return self.functions[self.access_id]

    def client(self, service):
        if service not in self.clients:
            self.clients[service] = FakeClient(self, service)
        return self.clients[service]

    def save(self):
        self.snapshots.append(copy.deepcopy(self.state))

    def wait(self, operation, desired, **params):
        result = getattr(self.client(CONTROL), operation)(**params)
        assert result["status"] == desired
        return result

    def api(self, service, operation, params):
        self.calls.append((service, operation, copy.deepcopy(params)))
        if self.failure == (service, operation):
            raise failure(operation)
        if operation == "get_gateway":
            self.gateway_reads += 1
            if self.concurrent_gateway and self.gateway_reads > 1:
                self.gateway["interceptorConfigurations"][0]["interceptor"]["lambda"]["arn"] = "different-function"
            return copy.deepcopy(self.gateway)
        if operation == "update_gateway":
            self.gateway.update({k: copy.deepcopy(v) for k, v in params.items() if k != "gatewayIdentifier"})
            return copy.deepcopy(self.gateway)
        if operation == "create_table":
            return {"TableDescription": {
                "TableName": params["TableName"], "TableArn": deploy._arn(self, "dynamodb", "table/" + params["TableName"]),
            }}
        if operation == "describe_time_to_live":
            return {"TimeToLiveDescription": {"TimeToLiveStatus": self.ttl_status}}
        if operation == "create_role":
            return {"Role": {
                "RoleName": params["RoleName"], "RoleId": "fixture-budget-role",
                "Arn": deploy._arn(self, "iam", "role/" + params["RoleName"], global_service=True),
            }}
        if operation == "create_function":
            config = {key: copy.deepcopy(params[key]) for key in (
                "FunctionName", "Handler", "Role", "Environment", "Runtime", "LoggingConfig")}
            config.update(FunctionArn=deploy._arn(self, "lambda", "function:" + params["FunctionName"]),
                          CodeSha256=base64.b64encode(hashlib.sha256(params["Code"]["ZipFile"]).digest()).decode(),
                          State="Active", LastUpdateStatus="Successful", RevisionId="response-revision")
            self.functions[params["FunctionName"]] = config
            return copy.deepcopy(config)
        if operation == "get_function_configuration":
            result = copy.deepcopy(self.functions[params["FunctionName"]])
            if self.response_hash_mismatch and params["FunctionName"] != self.access_id:
                result["CodeSha256"] = "wrong-response-code"
            return result
        if operation in ("update_function_code", "update_function_configuration"):
            config = self.functions[params["FunctionName"]]
            if self.revision_race == operation:
                config["RevisionId"] = "concurrent-update-between-read-and-write"
            if params["RevisionId"] != config["RevisionId"]:
                raise failure("revision-conflict")
            if operation == "update_function_code":
                config["CodeSha256"] = base64.b64encode(hashlib.sha256(params["ZipFile"]).digest()).decode()
                config["RevisionId"] = "revision-2"
            else:
                config["Environment"] = copy.deepcopy(params["Environment"])
                config["RevisionId"] = "revision-3"
            return copy.deepcopy(config)
        if operation == "add_permission":
            if self.concurrent_access:
                self.access["RevisionId"] = "another-writer"
            return {}
        if operation in ("put_role_policy", "create_log_group"):
            return {}
        raise AssertionError("Unexpected API " + operation)


class ConfigureBudgetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.prices = self.root / "prices.json"
        self.prices.write_text(json.dumps(PRICES))
        # Packaging fixture only, never an alternative access implementation.
        self.sources = self.root / "sources"
        self.sources.mkdir()
        (self.sources / "access_interceptor.py").write_text(
            'def request_handler(event, context):\n    raise RuntimeError("packaging fixture only")\n')
        (self.sources / "budget_interceptors.py").write_bytes((REAL_REPO / "budget_interceptors.py").read_bytes())
        self.env = FakeEnvironment(self.root / "resources.json")
        self.output = io.StringIO()
        for patcher in (
            patch.object(deploy, "REPO", self.sources),
            patch.object(deploy.time, "sleep"),
            patch("sys.stdout", self.output), patch("sys.stderr", self.output),
            patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
            patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_configure(self, **kwargs):
        deploy.configure(self.env, self.prices, Decimal("1.25"), Decimal("20"), **kwargs)

    def calls(self, operation):
        return [params for _service, name, params in self.env.calls if name == operation]

    def test_zip_is_two_modules_only_and_deterministic(self):
        archive = deploy.build_zip()
        self.assertEqual(archive, deploy.build_zip())
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            self.assertEqual(set(zipped.namelist()), {"access_interceptor.py", "budget_interceptors.py"})
            for name in zipped.namelist():
                self.assertEqual(zipped.read(name), (self.sources / name).read_bytes())
                ast.parse(zipped.read(name), feature_version=(3, 12))

    def test_zip_rejects_missing_access_handler(self):
        (self.sources / "access_interceptor.py").write_text("unrelated = 1\n")
        with self.assertRaises(ValueError):
            deploy.build_zip()

    def test_repository_zip_contains_both_current_real_source_files(self):
        # Parent owns access_interceptor.py; this only checks packaging, not its behavior.
        archive = deploy.build_zip(REAL_REPO)
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            self.assertEqual(set(zipped.namelist()), {"access_interceptor.py", "budget_interceptors.py"})
            for name in zipped.namelist():
                self.assertEqual(zipped.read(name), (REAL_REPO / name).read_bytes())

    def test_exact_two_model_prices_and_required_rates(self):
        for prices in ({}, {"fixture.standard": RATES}, {**PRICES, "extra": RATES},
                       {**PRICES, "fixture.standard": {"input_per_million_usd": "1"}}):
            self.prices.write_text(json.dumps(prices))
            with self.assertRaises(ValueError):
                self.run_configure()
        self.assertEqual(self.env.calls, [])

    def test_invalid_and_duplicate_prices_rejected(self):
        for value in ("NaN", "-1", True, None):
            prices = copy.deepcopy(PRICES)
            prices["fixture.standard"]["input_per_million_usd"] = value
            self.prices.write_text(json.dumps(prices))
            with self.assertRaises(ValueError):
                self.run_configure()
        self.prices.write_text('{"fixture.standard":{},"fixture.standard":{}}')
        with self.assertRaises(ValueError):
            self.run_configure()
        self.assertEqual(self.env.state["intents"], [])

    def test_cli_invalid_amount_never_constructs_environment(self):
        for value in ("NaN", "-1", "Infinity", "invalid"):
            with patch.object(deploy, "Environment") as factory, self.assertRaises(SystemExit):
                deploy.main(["--prices", str(self.prices), "--daily", value, "--monthly", "10"])
            factory.assert_not_called()

    def test_missing_or_unexpected_baseline_interceptors_are_refused(self):
        initial = copy.deepcopy(self.env.gateway["interceptorConfigurations"])
        wrong = copy.deepcopy(initial)
        wrong[0]["interceptor"]["lambda"]["arn"] = "not-owned-access"
        for actual in (None, [], wrong, initial + initial):
            self.env.gateway["interceptorConfigurations"] = actual
            with self.assertRaises(ValueError):
                self.run_configure()
            self.assertFalse(self.calls("create_table"))
        self.env.gateway["interceptorConfigurations"] = initial
        initial[0]["inputConfiguration"]["passRequestHeaders"] = False
        with self.assertRaises(ValueError):
            self.run_configure()

    def test_non_jwt_gateway_and_missing_labels_are_refused(self):
        self.env.gateway["authorizerType"] = "AWS_IAM"
        with self.assertRaises(ValueError):
            self.run_configure()
        self.env.gateway["authorizerType"] = "CUSTOM_JWT"
        self.env.one("role", "access")["label"] = None
        with self.assertRaises(ValueError):
            self.run_configure()
        self.assertFalse(self.calls("create_table"))

    def test_legacy_unlabeled_gateway_role_selected_by_exact_live_arn_without_relabeling(self):
        role = self.env.one("role", "gateway")
        role["label"] = None
        self.run_configure()
        policy = next(p for p in self.calls("put_role_policy") if p["PolicyName"].endswith("-budget-invoke"))
        self.assertEqual(policy["RoleName"], role["id"])
        self.assertIsNone(role["label"])

    def test_ambiguous_or_mislabeled_gateway_role_is_rejected(self):
        role = self.env.one("role", "gateway")
        role["label"] = "access"
        with self.assertRaises(ValueError):
            self.run_configure()
        role["label"] = "gateway"
        self.env.state["resources"].append(copy.deepcopy(role))
        with self.assertRaises(ValueError):
            self.run_configure()
        self.assertFalse(self.calls("create_table"))

    def test_access_handler_role_and_gateway_binding_are_checked_before_creates(self):
        original = copy.deepcopy(self.env.access)
        for field, value in (("Handler", "budget_interceptors.request_handler"),
                             ("Role", "not-access-role"), ("Runtime", "unsupported-runtime"),
                             ("State", "Pending"), ("LastUpdateStatus", "InProgress")):
            self.env.functions[self.env.access_id] = copy.deepcopy(original)
            self.env.access[field] = value
            with self.assertRaises(ValueError):
                self.run_configure()
        self.env.functions[self.env.access_id] = copy.deepcopy(original)
        self.env.access["Environment"]["Variables"]["EXPECTED_GATEWAY_ARN"] = "different-gateway"
        with self.assertRaises(ValueError):
            self.run_configure()
        self.assertFalse(self.calls("create_table"))

    def test_access_mapping_and_disabled_flag_must_be_valid(self):
        values = self.env.access["Environment"]["Variables"]
        original = values["MODEL_ACCESS_JSON"]
        for mapping in ("{}", "[]", "{", '{"fixture-sub":[]}'):
            values["MODEL_ACCESS_JSON"] = mapping
            with self.assertRaises(ValueError):
                self.run_configure()
        values["MODEL_ACCESS_JSON"] = original
        for flag in ("true", "invalid"):
            values["ENABLE_BUDGET"] = flag
            with self.assertRaises(ValueError):
                self.run_configure()
        self.assertFalse(self.calls("create_table"))

    def test_table_tags_keys_default_encryption_and_ttl(self):
        self.run_configure()
        params = self.calls("create_table")[0]
        self.assertEqual(params["BillingMode"], "PAY_PER_REQUEST")
        self.assertEqual(params["KeySchema"], [
            {"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
        self.assertEqual({t["Key"]: t["Value"] for t in params["Tags"]},
                         {"ManagedBy": MANAGED_BY, "RunId": self.env.run})
        self.assertNotIn("SSESpecification", params)
        self.assertFalse(self.calls("update_time_to_live"))
        self.assertTrue(self.calls("describe_time_to_live"))

    def test_only_response_function_role_and_log_are_created(self):
        before = copy.deepcopy(self.env.state["resources"])
        self.run_configure()
        self.assertEqual(len(self.calls("create_function")), 1)
        function = self.calls("create_function")[0]
        self.assertEqual(function["Handler"], "budget_interceptors.response_handler")
        self.assertEqual(function["Role"], self.env.one("role", "budget")["arn"])
        self.assertEqual(len(self.calls("create_log_group")), 1)
        self.assertEqual(len(self.calls("create_role")), 1)
        for row in before:
            actual = next(r for r in self.env.state["resources"] if r["kind"] == row["kind"] and r["id"] == row["id"])
            for key, value in row.items():
                self.assertEqual(actual[key], value)
        self.assertEqual(len(self.env.state["resources"]), len(before) + 7)

    def test_roles_receive_separate_exact_table_and_response_permissions(self):
        self.run_configure()
        policies = {p["PolicyName"]: p for p in self.calls("put_role_policy")}
        access = policies[self.env.run + "-budget-access"]
        self.assertEqual(access["RoleName"], self.env.one("role", "access")["id"])
        statements = json.loads(access["PolicyDocument"])["Statement"]
        self.assertEqual(statements, [{
            "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem"],
            "Resource": self.env.one("table", "budget")["arn"],
        }])
        execution = json.loads(policies[self.env.run + "-budget-execution"]["PolicyDocument"])["Statement"]
        self.assertEqual(execution[0]["Resource"], self.env.one("table", "budget")["arn"])
        self.assertEqual(set(execution[0]["Action"]), {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"})
        self.assertEqual(execution[1]["Resource"],
                         self.env.one("log_group", "budget-response")["arn"] + ":log-stream:*")
        invoke = policies[self.env.run + "-budget-invoke"]
        self.assertEqual(invoke["RoleName"], self.env.one("role", "gateway")["id"])
        self.assertEqual(json.loads(invoke["PolicyDocument"])["Statement"][0]["Resource"],
                         self.env.one("function", "budget-response")["arn"])
        self.assertFalse(any('"Resource": "*"' in p["PolicyDocument"] for p in policies.values()))

    def test_response_permission_has_exact_source_account_and_gateway(self):
        self.run_configure()
        permission = self.calls("add_permission")[0]
        self.assertEqual(permission["SourceAccount"], self.env.account)
        self.assertEqual(permission["SourceArn"], self.env.one("gateway")["arn"])
        self.assertEqual(permission["FunctionName"], self.env.one("function", "budget-response")["id"])
        self.assertEqual(len(self.calls("add_permission")), 1)  # Existing REQUEST permission untouched.

    def test_existing_access_handler_environment_role_and_logs_survive_activation(self):
        original = copy.deepcopy(self.env.access)
        self.run_configure()
        for key in ("FunctionName", "FunctionArn", "Handler", "Role", "Runtime", "LoggingConfig"):
            self.assertEqual(self.env.access[key], original[key])
        old = original["Environment"]["Variables"]
        new = self.env.access["Environment"]["Variables"]
        for key, value in old.items():
            if key != "ENABLE_BUDGET":
                self.assertEqual(new[key], value)
        self.assertEqual(new["ENABLE_BUDGET"], "true")
        self.assertEqual(new["DAILY_BUDGET_USD"], "1.25")
        self.assertEqual(new["MONTHLY_BUDGET_USD"], "20")
        self.assertEqual(new["ENFORCE_BUDGET"], "true")
        self.assertEqual(set(json.loads(new["PRICE_TABLE_JSON"])), set(MODEL_IDS.values()))
        params = self.calls("update_function_configuration")[0]
        self.assertEqual(set(params), {"FunctionName", "RevisionId", "Environment"})

    def test_preparation_code_attachment_activation_order_and_revision_guards(self):
        before = copy.deepcopy(self.env.gateway["interceptorConfigurations"])
        self.run_configure()
        names = [operation for _, operation, _ in self.env.calls]
        self.assertLess(names.index("create_table"), names.index("update_function_code"))
        self.assertLess(names.index("add_permission"), names.index("update_function_code"))
        self.assertLess(names.index("update_function_code"), names.index("update_gateway"))
        self.assertLess(names.index("update_gateway"), names.index("update_function_configuration"))
        self.assertEqual(self.calls("update_function_code")[0]["RevisionId"], "revision-1")
        self.assertEqual(self.calls("update_function_configuration")[0]["RevisionId"], "revision-2")
        self.assertEqual(self.env.gateway["interceptorConfigurations"][0], before[0])
        self.assertEqual(len(self.env.gateway["interceptorConfigurations"]), 2)
        self.assertEqual(self.env.state["budget"]["request_revision_enabled"], "revision-3")

    def test_existing_policy_and_jwt_gateway_settings_preserved(self):
        original = copy.deepcopy(self.env.gateway)
        self.run_configure()
        update = self.calls("update_gateway")[0]
        for key in ("roleArn", "authorizerType", "authorizerConfiguration", "policyEngineConfiguration", "description"):
            self.assertEqual(update[key], original[key])
        self.assertNotIn("protocolType", update)
        self.assertEqual(self.env.state["verification"]["existing"], "preserve")

    def test_observe_still_enables_budget_adapter_and_preserves_model_access(self):
        mapping = self.env.access["Environment"]["Variables"]["MODEL_ACCESS_JSON"]
        with patch.object(deploy, "Environment", return_value=self.env):
            deploy.main(["--prices", str(self.prices), "--daily", "1", "--monthly", "20", "--observe"])
        variables = self.env.access["Environment"]["Variables"]
        self.assertEqual(variables["ENABLE_BUDGET"], "true")
        self.assertEqual(variables["ENFORCE_BUDGET"], "false")
        self.assertEqual(variables["MODEL_ACCESS_JSON"], mapping)

    def test_unset_budget_flag_is_supported_but_auth_settings_are_not_defaulted(self):
        del self.env.access["Environment"]["Variables"]["ENABLE_BUDGET"]
        self.run_configure()
        self.assertEqual(self.env.access["Environment"]["Variables"]["ENABLE_BUDGET"], "true")

    def test_combined_environment_limit_counts_existing_access_values(self):
        self.env.access["Environment"]["Variables"]["OTHER_ACCESS_SETTING"] = "x" * 4096
        with self.assertRaises(ValueError):
            self.run_configure()
        self.assertFalse(self.calls("create_table"))

    def test_api_preflight_and_dynamic_activation_validation_use_real_shapes(self):
        original = deploy._validate
        preflight, activation = [], []

        def check(env, service, operation, params):
            original(env, service, operation, params)
            if "budget" not in env.state:
                self.assertEqual(len(env.state["resources"]), 7)
                preflight.append((service, operation))
            else:
                activation.append((service, operation, params["RevisionId"]))
        with patch.object(deploy, "_validate", side_effect=check):
            self.run_configure()
        self.assertEqual(len(preflight), 11)
        self.assertEqual(activation, [("lambda", "update_function_configuration", "revision-2")])

    def test_old_sdk_validation_failure_precedes_resource_creation(self):
        with patch.object(deploy, "_validate", side_effect=ParamValidationError(report="fixture")):
            with self.assertRaises(ParamValidationError):
                self.run_configure()
        self.assertEqual(self.env.state["intents"], [])

    def test_duplicate_attempt_never_reprovisions_or_reenables(self):
        self.run_configure()
        calls = len(self.env.calls)
        with self.assertRaises(ValueError):
            self.run_configure()
        self.assertEqual(len(self.env.calls), calls)

    def test_concurrent_access_update_is_detected_before_code_write(self):
        self.env.concurrent_access = True
        with self.assertRaises(ValueError):
            self.run_configure()
        self.assertFalse(self.calls("update_function_code"))
        self.assertFalse(self.calls("update_function_configuration"))
        self.assertEqual(self.env.access["Environment"]["Variables"]["ENABLE_BUDGET"], "false")

    def test_concurrent_gateway_change_blocks_attachment_and_activation(self):
        self.env.concurrent_gateway = True
        with self.assertRaises(ValueError):
            self.run_configure()
        self.assertTrue(self.calls("update_function_code"))
        self.assertFalse(self.calls("update_gateway"))
        self.assertFalse(self.calls("update_function_configuration"))
        self.assertEqual(self.env.access["Environment"]["Variables"]["ENABLE_BUDGET"], "false")

    def test_revision_conflict_between_read_and_write_does_not_overwrite(self):
        for operation in ("update_function_code", "update_function_configuration"):
            env = FakeEnvironment(self.env.path)
            original = copy.deepcopy(env.access["Environment"])
            env.revision_race = operation
            with self.subTest(operation=operation), self.assertRaises(ClientError):
                deploy.configure(env, self.prices, Decimal(1), Decimal(10))
            self.assertEqual(env.access["Environment"], original)
            unresolved = [i for i in env.state["intents"] if i["status"] == "unresolved"]
            self.assertEqual(len(unresolved), 1)
            self.assertEqual(unresolved[0]["operation"], operation)
            self.assertNotEqual(env.state["budget"]["status"], "configured")

    def test_prepare_failure_does_not_change_access_code_or_enable_budget(self):
        self.env.failure = ("lambda", "create_function")
        with self.assertRaises(ClientError):
            self.run_configure()
        self.assertEqual(self.env.access["CodeSha256"], "baseline-code-hash")
        self.assertFalse(self.calls("update_function_code"))
        self.assertFalse(self.calls("update_function_configuration"))
        self.assertTrue(self.env.one("table", "budget"))
        unresolved = [i for i in self.env.state["intents"] if i["status"] == "unresolved"]
        self.assertEqual(unresolved[0]["name"], self.env.run + "-budget-response")

    def test_mutation_failures_leave_intents_and_do_not_rollback(self):
        for failed in (("lambda", "add_permission"), ("lambda", "update_function_code"),
                       (CONTROL, "update_gateway"), ("lambda", "update_function_configuration")):
            env = FakeEnvironment(self.env.path)
            env.failure = failed
            with self.subTest(operation=failed), self.assertRaises(ClientError):
                deploy.configure(env, self.prices, Decimal(1), Decimal(10))
            unresolved = [i for i in env.state["intents"] if i["status"] == "unresolved"]
            self.assertEqual(len(unresolved), 1)
            self.assertEqual(unresolved[0]["operation"], failed[1])
            self.assertEqual(env.access["Environment"]["Variables"]["ENABLE_BUDGET"], "false")
            self.assertFalse(any(name.startswith("delete") for _, name, _ in env.calls))
            self.assertNotEqual(env.state["budget"]["status"], "configured")

    def test_response_hash_mismatch_and_ttl_block_before_access_update(self):
        for attr, value in (("response_hash_mismatch", True), ("ttl_status", "ENABLED")):
            env = FakeEnvironment(self.env.path)
            setattr(env, attr, value)
            with self.assertRaises(ValueError):
                deploy.configure(env, self.prices, Decimal(1), Decimal(10))
            self.assertFalse(any(name == "update_function_code" for _, name, _ in env.calls))

    def test_ledger_keeps_code_hash_not_zip_bytes_and_stages_are_saved(self):
        self.run_configure()
        code = next(i for i in self.env.state["intents"] if i.get("operation") == "update_function_code")
        self.assertNotIn("ZipFile", code["parameters"])
        self.assertIn("ZipFileSha256", code["parameters"])
        self.assertIn("ZipFileBytes", code["parameters"])
        json.dumps(self.env.state)  # No un-serializable binary archive in persisted intents.
        stages = [s["budget"]["status"] for s in self.env.snapshots if "budget" in s]
        for stage in ("preparing", "response_ready", "request_code_updated_budget_disabled",
                      "response_attached_budget_disabled", "activation_pending", "configured"):
            self.assertIn(stage, stages)
        self.assertEqual(self.env.state["budget"]["request_function_arn"], self.env.one("function", "access-request")["arn"])
        self.assertEqual(self.env.state["budget"]["runtime_sdk_versions"], "unverified")
        self.assertTrue(all(i["status"] == "recorded" for i in self.env.state["intents"]))

    def test_confirmation_allows_response_defaults_but_never_body_exclusion(self):
        expected = copy.deepcopy(self.env.gateway["interceptorConfigurations"])
        expected.append({"interceptor": {"lambda": {"arn": "fixture-response"}}, "interceptionPoints": ["RESPONSE"]})
        actual = copy.deepcopy(expected)
        actual[1]["inputConfiguration"] = {"passRequestHeaders": False}
        self.assertTrue(deploy._matches_interceptors(actual[::-1], expected))
        actual[1]["inputConfiguration"]["payloadFilter"] = {"exclude": ["RESPONSE_BODY"]}
        self.assertFalse(deploy._matches_interceptors(actual, expected))


if __name__ == "__main__":
    unittest.main()

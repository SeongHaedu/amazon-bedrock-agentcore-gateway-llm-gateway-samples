"""Enable optional budgets without replacing the native access REQUEST gate.

Run configure_access.py first. Required ledger labels: function/access-request
and role/access. Gateway role selection matches the live roleArn to one owned
row (label=gateway, or an unlabeled legacy row). The Gateway must have one REQUEST
interceptor, with passRequestHeaders=true and no payload exclusions.

Usage: python scripts/configure_budget.py --prices prices.json --daily 1 --monthly 10
Prices require exactly the saved standard/premium model IDs and five explicit
USD-per-million-token rates. --observe disables only the budget-cap veto.

Order: create table/RESPONSE/permissions; update the existing REQUEST ZIP while
budgets remain disabled; attach RESPONSE while preserving the REQUEST ARN;
finally enable ENABLE_BUDGET=true. MODEL_ACCESS_JSON, EXPECTED_GATEWAY_ARN,
handler, role, and logging settings of the access function are retained.
Code/environment updates use Lambda RevisionId to reject concurrent changes.
Do not run concurrent Gateway configuration writers: UpdateGateway has no CAS.

The ZIP contains only access_interceptor.py and budget_interceptors.py.
Python 3.12 Lambda's bundled boto3/botocore are NOT pinned to the local SDK.
Validate runtime SDK behavior and native interceptor payloads in AWS separately.
The staging period before ENABLE_BUDGET becomes true is not budget-enforced;
RESPONSE may report missing context for those still-access-only requests.

DynamoDB uses PAY_PER_REQUEST, pk/sk, default AWS-owned-key encryption, and no
TTL. One new, tagged RESPONSE log group and one budget role are created. Access
role DDB permissions are added as a separate, table-scoped inline policy.
No original access/Gateway policies, roles, permissions, or log groups are
replaced. Creates and mutations retain ledger/intents; no rollback or automatic
resume is attempted. A failed activation can leave attached RESPONSE/resources;
retain the state and inspect exact mutation outcomes before manual recovery.
"""
from __future__ import annotations

import argparse
import ast
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import zipfile

import boto3
import botocore
from botocore.validate import validate_parameters

if __package__:
    from .common import CONTROL, MANAGED_BY, REPO, Environment, main_guard, require
else:
    from common import CONTROL, MANAGED_BY, REPO, Environment, main_guard, require
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
import budget_interceptors as adapter


RUNTIME = "python3.12"
ACCESS_HANDLER = "access_interceptor.request_handler"
SDK_STRATEGY = "lambda-runtime-bundled-unpinned"


def amount(value):
    try:
        return adapter._decimal(value)
    except adapter.BudgetError:
        raise argparse.ArgumentTypeError("Budget must be a finite, nonnegative Decimal.") from None


def load_prices(path, models):
    require(set(models) == {"standard", "premium"}
            and all(isinstance(row, dict) and isinstance(row.get("id"), str)
                    for row in models.values()), "State requires standard and premium models.")
    expected = {row["id"] for row in models.values()}
    require(len(expected) == 2, "Standard and premium models must differ.")
    try:
        data = adapter._json_loads(Path(path).read_text(encoding="utf-8"))
        require(isinstance(data, dict) and set(data) == expected,
                "Prices must contain exactly both saved model IDs.")
        return {
            adapter._valid_model(model): {
                name: str(rate) for name, rate in adapter._rates(rates).items()
            } for model, rates in data.items()
        }
    except adapter.BudgetError:
        raise ValueError("Every model requires five finite, nonnegative decimal rates.") from None


def build_zip(source_dir=None):
    """Bundle exactly the local access gate and budget adapter, deterministically."""
    root = REPO if source_dir is None else Path(source_dir)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, handlers in (
            ("access_interceptor.py", {"request_handler"}),
            ("budget_interceptors.py", {"request_handler", "response_handler"}),
        ):
            payload = (root / name).read_bytes()
            tree = ast.parse(payload, filename=name, feature_version=(3, 12))
            require(handlers <= {n.name for n in tree.body if isinstance(n, ast.FunctionDef)},
                    "Required handlers missing from " + name)
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, payload)
    return output.getvalue()


def _arn(env, service, resource, *, global_service=False):
    region = "" if global_service else env.region
    return f"arn:{env.partition}:{service}:{region}:{env.account}:{resource}"


def _matches_interceptors(actual, expected):
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    by_point = {}
    for item in actual:
        if not isinstance(item, dict):
            return False
        points = item.get("interceptionPoints", [])
        if len(points) != 1 or points[0] in by_point:
            return False
        by_point[points[0]] = item
    for desired in expected:
        point = desired["interceptionPoints"][0]
        item = by_point.get(point, {})
        if item.get("interceptor") != desired["interceptor"]:
            return False
        inputs = item.get("inputConfiguration") or {}
        if (inputs.get("payloadFilter") or {}).get("exclude"):
            return False
        if point == "REQUEST" and inputs.get("passRequestHeaders") is not True:
            return False
    return True


def _gateway(env, owned, expected):
    current = env.client(CONTROL).get_gateway(gatewayIdentifier=owned["id"])
    require(current.get("gatewayArn") == owned["arn"] and current.get("name") == owned["name"],
            "Gateway ownership does not match the ledger.")
    require(current.get("status") == "READY", "Gateway must be READY.")
    require(current.get("authorizerType") == "CUSTOM_JWT"
            and current.get("authorizerConfiguration", {}).get("customJWTAuthorizer"),
            "The access adapter requires a JWT-verifying Gateway.")
    require(_matches_interceptors(current.get("interceptorConfigurations"), expected),
            "Gateway interceptors differ from the owned access-request configuration.")
    _gateway_role(env, current)
    return current


def _gateway_role(env, current):
    rows = [
        row for row in env.state["resources"]
        if row["kind"] == "role" and not row.get("deleted")
        and row.get("arn") == current.get("roleArn")
    ]
    require(len(rows) == 1 and rows[0].get("label") in (None, "gateway"),
            "Live Gateway role ARN must match one owned gateway/legacy role.")
    return rows[0]


def _access(env, function, role, gateway, *, variables=None, code_hash=None):
    config = env.client("lambda").get_function_configuration(FunctionName=function["id"])
    require(config.get("FunctionName") == function["id"] and config.get("FunctionArn") == function["arn"]
            and config.get("Handler") == ACCESS_HANDLER and config.get("Role") == role["arn"]
            and config.get("Runtime") == RUNTIME,
            "Owned access-request function identity/handler/role/runtime does not match.")
    require(config.get("State") == "Active" and config.get("LastUpdateStatus") == "Successful",
            "Access-request function must be active with no update in progress.")
    require(config.get("RevisionId") and config.get("CodeSha256"), "Lambda revision/hash unavailable.")
    current = config.get("Environment", {}).get("Variables", {})
    require(current.get("EXPECTED_GATEWAY_ARN") == gateway["arn"],
            "Access-request EXPECTED_GATEWAY_ARN does not match.")
    try:
        mapping = adapter._json_loads(current.get("MODEL_ACCESS_JSON", ""))
        require(isinstance(mapping, dict) and mapping, "MODEL_ACCESS_JSON must be a nonempty map.")
        for subject, models in mapping.items():
            require(isinstance(subject, str) and bool(subject)
                    and isinstance(models, list) and bool(models),
                    "MODEL_ACCESS_JSON contains an invalid subject/model list.")
            for model in models:
                adapter._valid_model(model)
    except (adapter.BudgetError, ValueError, TypeError):
        raise ValueError("Access-request MODEL_ACCESS_JSON is invalid.") from None
    if variables is None:
        require(current.get("ENABLE_BUDGET") in (None, "false"),
                "Budget is already enabled or ENABLE_BUDGET is invalid.")
    else:
        require(current == variables, "Access-request environment changed unexpectedly.")
    if code_hash is not None:
        require(config["CodeSha256"] == code_hash, "Access-request code hash does not match.")
    return config


def _validate(env, service, operation, params):
    client = env.client(service)
    name = client.meta.method_to_api_mapping[operation]
    validate_parameters(params, client.meta.service_model.operation_model(name).input_shape)


def _mutation(env, row, service, operation, params):
    recorded = copy.deepcopy(params)
    if "ZipFile" in recorded:
        payload = recorded.pop("ZipFile")
        recorded["ZipFileSha256"] = hashlib.sha256(payload).hexdigest()
        recorded["ZipFileBytes"] = len(payload)
    intent = {
        "kind": row["kind"], "name": row["name"], "id": row["id"],
        "operation": operation, "parameters": recorded, "status": "pending",
    }
    env.state["intents"].append(intent)
    env.save()
    try:
        result = getattr(env.client(service), operation)(**params)
    except Exception:
        intent["status"] = "unresolved"
        env.save()
        raise
    intent["status"] = "recorded"
    env.save()
    return result


def _attach(env, gateway, before, after):
    _gateway(env, gateway, before)
    intent = {
        "kind": "gateway", "name": gateway["name"], "id": gateway["id"],
        "operation": "update_gateway", "parameters": {"interceptorConfigurations": after},
        "status": "pending",
    }
    env.state["intents"].append(intent)
    env.save()
    try:
        result = env.gateway_update(interceptorConfigurations=after)
        require(_matches_interceptors(result.get("interceptorConfigurations"), after),
                "Gateway did not retain access REQUEST plus budget RESPONSE.")
    except Exception:
        intent["status"] = "unresolved"
        env.save()
        raise
    intent["status"] = "recorded"
    env.save()


def _stage(env, status):
    env.state["budget"]["status"] = status
    env.save()


def configure(env, prices_path, daily, monthly, *, observe=False):
    prices = load_prices(prices_path, env.state["models"])
    daily, monthly = adapter._decimal(daily), adapter._decimal(monthly)
    require(type(observe) is bool, "observe must be boolean.")
    require("budget" not in env.state, "An earlier budget attempt exists; inspect its state first.")
    archive = build_zip()
    code_hash = base64.b64encode(hashlib.sha256(archive).digest()).decode()
    gateway = env.one("gateway")
    request = env.one("function", "access-request")
    access_role = env.one("role", "access")
    expected_request = [{
        "interceptor": {"lambda": {"arn": request["arn"]}},
        "interceptionPoints": ["REQUEST"], "inputConfiguration": {"passRequestHeaders": True},
    }]
    current = _gateway(env, gateway, expected_request)
    gateway_role = _gateway_role(env, current)
    before = copy.deepcopy(current["interceptorConfigurations"])
    baseline = _access(env, request, access_role, gateway)
    original_variables = copy.deepcopy(baseline["Environment"]["Variables"])
    names = {key: env.run + suffix for key, suffix in {
        "table": "-budget", "role": "-budget", "execution_policy": "-budget-execution",
        "access_policy": "-budget-access", "invoke_policy": "-budget-invoke",
        "response": "-budget-response", "logs": "-budget-response-logs",
    }.items()}
    require(not any(row["name"] in set(names.values())
                    for row in env.state["resources"] + env.state["intents"]),
            "Budget resources/intents already exist; inspect the partial run.")
    table_arn = _arn(env, "dynamodb", "table/" + names["table"])
    role_arn = _arn(env, "iam", "role/" + names["role"], global_service=True)
    response_arn = _arn(env, "lambda", "function:" + names["response"])
    log_arn = _arn(env, "logs", "log-group:" + names["logs"])
    tags = {"ManagedBy": MANAGED_BY, "RunId": env.run}
    tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
    variables = {
        "BUDGET_TABLE_NAME": names["table"],
        "PRICE_TABLE_JSON": json.dumps(prices, sort_keys=True, separators=(",", ":")),
        "DAILY_BUDGET_USD": str(daily), "MONTHLY_BUDGET_USD": str(monthly),
        "ENFORCE_BUDGET": "false" if observe else "true",
    }
    enabled_variables = {**original_variables, **variables, "ENABLE_BUDGET": "true"}
    for values in (variables, enabled_variables):
        require(sum(len(k.encode()) + len(v.encode()) for k, v in values.items()) <= 4096,
                "Combined access/budget environment exceeds Lambda's size limit.")
    table_params = {
        "TableName": names["table"], "BillingMode": "PAY_PER_REQUEST", "Tags": tag_list,
        "AttributeDefinitions": [{"AttributeName": k, "AttributeType": "S"} for k in ("pk", "sk")],
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"},
                      {"AttributeName": "sk", "KeyType": "RANGE"}],
    }
    role_params = {
        "RoleName": names["role"], "Tags": tag_list,
        "AssumeRolePolicyDocument": json.dumps({"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": "sts:AssumeRole",
            "Principal": {"Service": "lambda.amazonaws.com"},
        }]}),
    }
    ddb_statement = {
        "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
        "Resource": table_arn,
    }
    policy_specs = [
        ("execution_policy", names["role"], [ddb_statement, {
            "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": log_arn + ":log-stream:*",
        }]),
        ("access_policy", access_role["id"], [{
            **ddb_statement, "Action": ["dynamodb:GetItem", "dynamodb:PutItem"],
        }]),
        ("invoke_policy", gateway_role["id"], [{
            "Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": response_arn,
        }]),
    ]
    policy_params = {
        key: {"RoleName": role, "PolicyName": names[key],
              "PolicyDocument": json.dumps({"Version": "2012-10-17", "Statement": statements})}
        for key, role, statements in policy_specs
    }
    log_params = {"logGroupName": names["logs"], "tags": tags}
    function_params = {
        "FunctionName": names["response"], "Runtime": RUNTIME, "Role": role_arn,
        "Handler": "budget_interceptors.response_handler", "Code": {"ZipFile": archive},
        "PackageType": "Zip", "Timeout": 30, "MemorySize": 256, "Publish": False,
        "Environment": {"Variables": variables},
        "LoggingConfig": {"LogFormat": "Text", "LogGroup": names["logs"]}, "Tags": tags,
    }
    permission_params = {
        "FunctionName": names["response"], "StatementId": "budget-gateway-response",
        "Action": "lambda:InvokeFunction", "Principal": "bedrock-agentcore.amazonaws.com",
        "SourceAccount": env.account, "SourceArn": gateway["arn"],
    }
    code_params = {"FunctionName": request["id"], "ZipFile": archive, "Publish": False,
                   "RevisionId": baseline["RevisionId"]}
    enable_params = {"FunctionName": request["id"], "Environment": {"Variables": enabled_variables},
                     "RevisionId": baseline["RevisionId"]}
    after = before + [{"interceptor": {"lambda": {"arn": response_arn}},
                       "interceptionPoints": ["RESPONSE"]}]
    update_shape = env.client(CONTROL).meta.service_model.operation_model("UpdateGateway").input_shape
    gateway_params = {
        key: value for key, value in current.items()
        if key in update_shape.members and key not in {
            "gatewayIdentifier", "clientToken", "protocolType", "protocolConfiguration",
        }
    }
    gateway_params.update(gatewayIdentifier=gateway["id"], interceptorConfigurations=after)
    calls = [
        ("dynamodb", "create_table", table_params), ("iam", "create_role", role_params),
        *(("iam", "put_role_policy", value) for value in policy_params.values()),
        ("logs", "create_log_group", log_params), ("lambda", "create_function", function_params),
        ("lambda", "add_permission", permission_params),
        ("lambda", "update_function_code", code_params),
        ("lambda", "update_function_configuration", enable_params),
        (CONTROL, "update_gateway", gateway_params),
    ]
    for service, operation, params in calls:
        _validate(env, service, operation, params)
    env.state["budget"] = {
        "status": "preparing", "request_function_arn": request["arn"], "request_role_arn": access_role["arn"],
        "response_function_arn": response_arn, "table_arn": table_arn,
        "request_revision_before": baseline["RevisionId"], "request_code_before": baseline["CodeSha256"],
        "zip_sha256": hashlib.sha256(archive).hexdigest(), "runtime": RUNTIME,
        "sdk_strategy": SDK_STRATEGY, "runtime_sdk_versions": "unverified",
        "deployment_sdk": {"boto3": boto3.__version__, "botocore": botocore.__version__},
        "observe_caps_only": observe, "verification": "requires_live_native_verification",
    }
    env.save()
    table = env.create("table", names["table"], "dynamodb", "create_table",
                       lambda r: (r["TableDescription"]["TableName"], {"arn": r["TableDescription"]["TableArn"]}),
                       label="budget", **table_params)
    require(table["id"] == names["table"] and table["arn"] == table_arn, "Created table identity mismatch.")
    env.client("dynamodb").get_waiter("table_exists").wait(
        TableName=table["id"], WaiterConfig={"Delay": 2, "MaxAttempts": 120})
    require(env.client("dynamodb").describe_time_to_live(TableName=table["id"])
            ["TimeToLiveDescription"]["TimeToLiveStatus"] == "DISABLED", "TTL must remain disabled.")
    role = env.create("role", names["role"], "iam", "create_role",
                      lambda r: (r["Role"]["RoleName"], {"arn": r["Role"]["Arn"], "role_id": r["Role"]["RoleId"]}),
                      label="budget", **role_params)
    require(role["arn"] == role_arn and role["id"] == names["role"], "Created budget role identity mismatch.")
    for key, params in policy_params.items():
        env.create("role_policy", names[key], "iam", "put_role_policy",
                   lambda _r, params=params: (params["PolicyName"], {"role_name": params["RoleName"]}),
                   label="budget", **params)
    env.create("log_group", names["logs"], "logs", "create_log_group",
               lambda _r: (names["logs"], {"arn": log_arn}), label="budget-response", **log_params)
    time.sleep(15)
    response = env.create("function", names["response"], "lambda", "create_function",
                          lambda r: (r["FunctionName"], {"arn": r["FunctionArn"]}),
                          label="budget-response", **function_params)
    require(response["id"] == names["response"] and response["arn"] == response_arn,
            "Created RESPONSE function identity mismatch.")
    lambda_client = env.client("lambda")
    lambda_client.get_waiter("function_active_v2").wait(
        FunctionName=response["id"], WaiterConfig={"Delay": 2, "MaxAttempts": 120})
    actual = lambda_client.get_function_configuration(FunctionName=response["id"])
    require(actual.get("CodeSha256") == code_hash and actual.get("Role") == role_arn
            and actual.get("Handler") == "budget_interceptors.response_handler"
            and actual.get("Runtime") == RUNTIME and actual.get("Environment", {}).get("Variables") == variables
            and actual.get("LoggingConfig", {}).get("LogGroup") == names["logs"],
            "RESPONSE function configuration mismatch.")
    response.update(handler="budget_interceptors.response_handler", log_group_name=names["logs"],
                    permission_statement_id=permission_params["StatementId"])
    env.save()
    _mutation(env, response, "lambda", "add_permission", permission_params)
    _stage(env, "response_ready")
    fresh = _access(env, request, access_role, gateway, variables=original_variables,
                    code_hash=baseline["CodeSha256"])
    require(fresh["RevisionId"] == baseline["RevisionId"], "Access function changed during preparation.")
    _mutation(env, request, "lambda", "update_function_code", code_params)
    lambda_client.get_waiter("function_updated_v2").wait(
        FunctionName=request["id"], WaiterConfig={"Delay": 2, "MaxAttempts": 120})
    updated = _access(env, request, access_role, gateway, variables=original_variables, code_hash=code_hash)
    require(updated.get("LoggingConfig") == baseline.get("LoggingConfig"),
            "Access function logging configuration changed.")
    _stage(env, "request_code_updated_budget_disabled")
    time.sleep(15)
    _attach(env, gateway, before, after)
    _stage(env, "response_attached_budget_disabled")
    fresh = _access(env, request, access_role, gateway, variables=original_variables, code_hash=code_hash)
    require(fresh["RevisionId"] == updated["RevisionId"], "Access function changed before activation.")
    enable_params["RevisionId"] = fresh["RevisionId"]
    _validate(env, "lambda", "update_function_configuration", enable_params)
    _stage(env, "activation_pending")
    _mutation(env, request, "lambda", "update_function_configuration", enable_params)
    lambda_client.get_waiter("function_updated_v2").wait(
        FunctionName=request["id"], WaiterConfig={"Delay": 2, "MaxAttempts": 120})
    enabled = _access(env, request, access_role, gateway, variables=enabled_variables, code_hash=code_hash)
    require(enabled.get("LoggingConfig") == baseline.get("LoggingConfig"), "Access logging changed.")
    _gateway(env, gateway, after)
    request.update(budget_enabled=True, budget_table_name=table["id"], code_sha256=code_hash)
    env.state["budget"]["request_revision_enabled"] = enabled["RevisionId"]
    env.state["verification"]["budget_configuration"] = "requires_live_native_verification"
    _stage(env, "configured")
    print(json.dumps({"status": "configured", "verification": "requires_live_native_verification",
                      "sdk_strategy": SDK_STRATEGY, "state_file": str(env.path)}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--daily", type=amount, required=True, help="Daily USD cap (UTC).")
    parser.add_argument("--monthly", type=amount, required=True, help="Monthly USD cap (UTC).")
    parser.add_argument("--observe", action="store_true", help="Observe caps only; access checks remain enforced.")
    args = parser.parse_args(argv)
    configure(Environment(), args.prices, args.daily, args.monthly, observe=args.observe)


if __name__ == "__main__":
    main_guard(main)

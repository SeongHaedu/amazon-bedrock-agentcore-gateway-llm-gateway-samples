"""Deploy the native URI access-check Lambda using an owned resource ledger."""
import io
import json
import time
import zipfile

from common import CONTROL, MANAGED_BY, REPO, require


def access_map(env):
    standard = env.state["models"]["standard"]["id"]
    premium = env.state["models"]["premium"]["id"]
    return {
        # Keep one spelling per model so alternative target-qualified aliases
        # cannot create a different rate bucket for the same underlying model.
        env.one("user", u)["sub"]: [standard] if u == "b" else [standard, premium]
        for u in ("a", "b", "c")
    }


def deploy_access(env):
    gateway = env.one("gateway")
    current = env.client(CONTROL).get_gateway(gatewayIdentifier=gateway["id"])
    require(current["name"] == gateway["name"] and current["gatewayArn"] == gateway["arn"],
            "Gateway identity mismatch.")
    role = next(r for r in env.state["resources"] if r["kind"] == "role"
                and not r.get("deleted") and r["arn"] == current["roleArn"])
    require(not current.get("interceptorConfigurations"),
            "Interceptors already exist; inspect the current setup before changing them.")
    tags = {"ManagedBy": MANAGED_BY, "RunId": env.run}
    name = env.run + "-access-request"
    logs = env.run + "-access-logs"
    log_arn = f"arn:{env.partition}:logs:{env.region}:{env.account}:log-group:{logs}"
    env.create("log_group", logs, "logs", "create_log_group",
               lambda r: (logs, {"arn": log_arn}), label="access",
               logGroupName=logs, tags=tags)
    access_role = env.create(
        "role", env.run + "-access", "iam", "create_role",
        lambda r: (r["Role"]["RoleName"], {"arn": r["Role"]["Arn"], "role_id": r["Role"]["RoleId"]}),
        label="access", RoleName=env.run + "-access",
        Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
        AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole"}]}))
    env.create("role_policy", env.run + "-access-logs", "iam", "put_role_policy",
               lambda r: (env.run + "-access-logs", {"role_name": access_role["id"]}),
               RoleName=access_role["id"], PolicyName=env.run + "-access-logs",
               PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
                   "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                   "Resource": log_arn + ":log-stream:*"}]}))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for module in ("access_interceptor.py", "budget_interceptors.py"):
            z.writestr(module, (REPO / module).read_bytes())
    time.sleep(15)
    function = env.create(
        "function", name, "lambda", "create_function",
        lambda r: (r["FunctionName"], {"arn": r["FunctionArn"]}),
        label="access-request", FunctionName=name, Runtime="python3.12",
        Role=access_role["arn"], Handler="access_interceptor.request_handler",
        Code={"ZipFile": archive.getvalue()}, Timeout=30, MemorySize=256, Tags=tags,
        LoggingConfig={"LogFormat": "Text", "LogGroup": logs},
        Environment={"Variables": {"MODEL_ACCESS_JSON": json.dumps(access_map(env)),
                                   "EXPECTED_GATEWAY_ARN": gateway["arn"], "ENABLE_BUDGET": "false"}})
    env.client("lambda").get_waiter("function_active_v2").wait(
        FunctionName=function["id"], WaiterConfig={"Delay": 2, "MaxAttempts": 120})
    env.client("lambda").add_permission(
        FunctionName=function["id"], StatementId="access-gateway", Action="lambda:InvokeFunction",
        Principal="bedrock-agentcore.amazonaws.com", SourceAccount=env.account, SourceArn=gateway["arn"])
    env.create("role_policy", env.run + "-access-invoke", "iam", "put_role_policy",
               lambda r: (env.run + "-access-invoke", {"role_name": role["id"]}),
               RoleName=role["id"], PolicyName=env.run + "-access-invoke",
               PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
                   "Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": function["arn"]}]}))
    time.sleep(15)
    return function

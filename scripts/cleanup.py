"""Delete only resources recorded by this sample, after checking their identity."""
from __future__ import annotations

import argparse
import json
import time
from botocore.exceptions import ClientError
from common import CONTROL, MANAGED_BY, Environment, main_guard, require


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    env = Environment()
    pending = [r for r in env.state["resources"] if not r.get("deleted")]
    print(json.dumps({"account_matches": True, "resources": [
        {"kind": r["kind"], "name": r["name"]} for r in pending]}, indent=2))
    if not args.yes:
        print("Dry run. Add --yes to delete these recorded resources.")
        return
    order = ["rate", "target", "gateway", "policy", "engine", "function", "log_group",
             "table", "role_policy", "role", "user", "client", "pool"]
    require(all(r["kind"] in order for r in pending), "Unknown resource kind; cleanup stopped.")
    failures = []
    for kind in order:
        for row in [r for r in pending if r["kind"] == kind]:
            try:
                remove(env, row)
                row["deleted"] = True
                env.save()
                print(json.dumps({"deleted": kind}))
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in {"ResourceNotFoundException", "NoSuchEntity", "ResourceNotFound",
                            "UserNotFoundException"}:
                    row["deleted"] = True
                    env.save()
                else:
                    failures.append({"kind": kind, "error": code})
            except Exception as exc:
                failures.append({"kind": kind, "error": str(exc)})
    remaining = [r["kind"] for r in env.state["resources"] if not r.get("deleted")]
    unresolved = [i for i in env.state["intents"] if i["status"] not in {"recorded", "not_created"}]
    print(json.dumps({"remaining": remaining, "unresolved_create_intents": unresolved,
                      "failures": failures}))
    require(not remaining and not unresolved, "Cleanup incomplete; keep the state file and inspect remaining resources.")


def remove(env, row):
    kind, identifier = row["kind"], row["id"]
    c = env.client(CONTROL)
    if kind == "rate":
        c.delete_gateway_rate_limit(gatewayIdentifier=row["gateway_id"], rateLimitId=identifier)
    elif kind == "target":
        actual = c.get_gateway_target(gatewayIdentifier=row["gateway_id"], targetId=identifier)
        require(actual["name"] == row["name"], "Target name mismatch.")
        c.delete_gateway_target(gatewayIdentifier=row["gateway_id"], targetId=identifier)
        wait_deleted(lambda: c.get_gateway_target(gatewayIdentifier=row["gateway_id"], targetId=identifier))
    elif kind == "gateway":
        require_no_children(env, identifier, "gateway_id")
        actual = c.get_gateway(gatewayIdentifier=identifier)
        require(actual["gatewayArn"] == row["arn"] and actual["name"] == row["name"], "Gateway mismatch.")
        c.delete_gateway(gatewayIdentifier=identifier)
        wait_deleted(lambda: c.get_gateway(gatewayIdentifier=identifier))
    elif kind == "policy":
        actual = c.get_policy(policyEngineId=row["engine_id"], policyId=identifier)
        require(actual["name"] == row["name"], "Policy name mismatch.")
        c.delete_policy(policyEngineId=row["engine_id"], policyId=identifier)
        wait_deleted(lambda: c.get_policy(policyEngineId=row["engine_id"], policyId=identifier))
    elif kind == "engine":
        require_no_children(env, row["id"], "engine_id")
        actual = c.get_policy_engine(policyEngineId=identifier)
        require(actual["policyEngineArn"] == row["arn"] and actual["name"] == row["name"], "Engine mismatch.")
        c.delete_policy_engine(policyEngineId=identifier)
        wait_deleted(lambda: c.get_policy_engine(policyEngineId=identifier))
    elif kind == "function":
        client = env.client("lambda")
        actual = client.get_function(FunctionName=identifier)["Configuration"]
        require(actual["FunctionArn"] == row["arn"], "Function mismatch.")
        client.delete_function(FunctionName=identifier)
    elif kind == "table":
        client = env.client("dynamodb")
        actual = client.describe_table(TableName=identifier)["Table"]
        require(actual["TableArn"] == row["arn"], "Table mismatch.")
        client.delete_table(TableName=identifier)
        wait_deleted(lambda: client.describe_table(TableName=identifier))
    elif kind == "log_group":
        client = env.client("logs")
        actual = client.describe_log_groups(logGroupNamePrefix=identifier)["logGroups"]
        require(any(g["logGroupName"] == identifier for g in actual), "Log group missing.")
        tags = client.list_tags_log_group(logGroupName=identifier).get("tags", {})
        require(tags.get("ManagedBy") == MANAGED_BY and tags.get("RunId") == env.run,
                "Log group tags mismatch.")
        client.delete_log_group(logGroupName=identifier)
    elif kind == "role_policy":
        parents = [r for r in env.state["resources"] if r["kind"] == "role" and r["id"] == row["role_name"]]
        require(len(parents) == 1, "Inline policy has no unambiguous owned parent role.")
        check_role(env, parents[0])
        env.client("iam").delete_role_policy(RoleName=row["role_name"], PolicyName=identifier)
    elif kind == "role":
        client = env.client("iam")
        require_no_children(env, identifier, "role_name")
        check_role(env, row)
        client.delete_role(RoleName=identifier)
    elif kind == "user":
        client = env.client("cognito-idp")
        actual = client.admin_get_user(UserPoolId=row["pool_id"], Username=identifier)
        attrs = {a["Name"]: a["Value"] for a in actual["UserAttributes"]}
        require(attrs["sub"] == row["sub"], "User identity mismatch.")
        client.admin_delete_user(UserPoolId=row["pool_id"], Username=identifier)
    elif kind == "client":
        client = env.client("cognito-idp")
        actual = client.describe_user_pool_client(UserPoolId=row["pool_id"], ClientId=identifier)["UserPoolClient"]
        require(actual["ClientName"] == row["name"], "Client name mismatch.")
        client.delete_user_pool_client(UserPoolId=row["pool_id"], ClientId=identifier)
    elif kind == "pool":
        require_no_children(env, identifier, "pool_id")
        client = env.client("cognito-idp")
        actual = client.describe_user_pool(UserPoolId=identifier)["UserPool"]
        require(actual["Arn"] == row["arn"] and actual["Name"] == row["name"], "Pool mismatch.")
        client.delete_user_pool(UserPoolId=identifier)


def require_no_children(env, identifier, parent_key):
    require(not any(r.get(parent_key) == identifier and not r.get("deleted")
                    for r in env.state["resources"]), "Parent retained because a child was not deleted.")


def check_role(env, row):
    actual = env.client("iam").get_role(RoleName=row["id"])["Role"]
    tags = {t["Key"]: t["Value"] for t in actual.get("Tags", [])}
    require(actual["RoleId"] == row["role_id"] and actual["Arn"] == row["arn"]
            and tags.get("ManagedBy") == MANAGED_BY and tags.get("RunId") == env.run,
            "Role identity/tag mismatch.")


def wait_deleted(read):
    deadline = time.monotonic() + 240
    while True:
        try:
            result = read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                return
            raise
        require("FAILED" not in result.get("status", ""), "Asynchronous deletion failed.")
        require(time.monotonic() < deadline, "Timed out waiting for deletion.")
        time.sleep(4)


if __name__ == "__main__":
    main_guard(main)

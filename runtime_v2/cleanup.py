"""Delete only this state file's owned deployment; retained upstream data remains.

Run with --yes. The upstream stack deliberately retains resources including its
spend table, KMS key and archive bucket. Inspect private state for their IDs.
"""
from __future__ import annotations

import argparse
import time
from botocore.exceptions import ClientError

if __package__:
    from .manage import Deployment, MODEL_TARGET, emit, parser, require
else:
    from manage import Deployment, MODEL_TARGET, emit, parser, require


def wait_deleted(d, operation, **kwargs):
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        try:
            result = getattr(d.control, operation)(**kwargs)
        except d.control.exceptions.ResourceNotFoundException:
            return
        require("FAILED" not in result.get("status", ""), f"{operation}: deletion failed.")
        time.sleep(5)
    raise TimeoutError(operation + ": deletion")


def cleanup(d):
    runtime_id = d.state.get("runtime_id")
    if runtime_id:
        try:
            runtime = d.control.get_agent_runtime(agentRuntimeId=runtime_id)
            require(runtime["agentRuntimeName"] == d.state["runtime_name"]
                    and runtime["agentRuntimeArn"] == d.state["runtime_arn"], "Runtime ownership mismatch.")
            tags = d.control.list_tags_for_resource(resourceArn=runtime["agentRuntimeArn"])["tags"]
            require(tags.items() >= d.tags.items(), "Runtime ownership tags mismatch.")
            if runtime["status"] != "DELETING":
                d.control.delete_agent_runtime(agentRuntimeId=runtime_id)
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                try:
                    current = d.control.get_agent_runtime(agentRuntimeId=runtime_id)
                except d.control.exceptions.ResourceNotFoundException:
                    break
                require(current["status"] != "DELETE_FAILED", "Runtime deletion failed.")
                time.sleep(5)
            else:
                raise TimeoutError("Runtime deletion")
        except d.control.exceptions.ResourceNotFoundException:
            pass
    if d.state.get("gateway_id"):
        try:
            gateway = d.control.get_gateway(gatewayIdentifier=d.state["gateway_id"])
            require(gateway["name"] == d.run and gateway["gatewayArn"] == d.state["gateway_arn"],
                    "Gateway ownership mismatch.")
            if d.state.get("model_target_id"):
                try:
                    target = d.control.get_gateway_target(
                        gatewayIdentifier=d.state["gateway_id"], targetId=d.state["model_target_id"])
                    require(target["name"] == MODEL_TARGET, "Model target ownership mismatch.")
                    d.control.delete_gateway_target(
                        gatewayIdentifier=d.state["gateway_id"], targetId=d.state["model_target_id"])
                    wait_deleted(d, "get_gateway_target",
                                 gatewayIdentifier=d.state["gateway_id"], targetId=d.state["model_target_id"])
                except d.control.exceptions.ResourceNotFoundException:
                    pass
            arguments = {"gatewayIdentifier": d.state["gateway_id"]}
            owned_rates = []
            while True:
                rates = d.control.list_gateway_rate_limits(**arguments)
                owned_rates.extend(rate["rateLimitId"] for rate in rates.get("rateLimits", [])
                                   if rate["rateLimitId"].startswith(d.run + "-"))
                if not rates.get("nextToken"):
                    break
                arguments["nextToken"] = rates["nextToken"]
            for rate_id in owned_rates:
                d.control.delete_gateway_rate_limit(
                    gatewayIdentifier=d.state["gateway_id"], rateLimitId=rate_id)
                wait_deleted(d, "get_gateway_rate_limit",
                             gatewayIdentifier=d.state["gateway_id"], rateLimitId=rate_id)
        except d.control.exceptions.ResourceNotFoundException:
            pass
    if d.state.get("gateway_role"):
        for name in ("RuntimeV2PolicyEvaluation", "RuntimeV2ModelAccess"):
            try:
                d.client("iam").delete_role_policy(RoleName=d.state["gateway_role"], PolicyName=name)
            except d.client("iam").exceptions.NoSuchEntityException:
                pass
    cf = d.client("cloudformation")
    try:
        stack = cf.describe_stacks(StackName=d.state["stack_name"])["Stacks"][0]
        require({t["Key"]: t["Value"] for t in stack.get("Tags", [])}.items() >= d.tags.items(),
                "Stack ownership tags mismatch.")
        cf.delete_stack(StackName=d.state["stack_name"])
        cf.get_waiter("stack_delete_complete").wait(
            StackName=d.state["stack_name"], WaiterConfig={"Delay": 10, "MaxAttempts": 180})
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ValidationError" or "does not exist" not in str(exc):
            raise
    # Delete the engine only after the referencing Gateway has been removed.
    if d.state.get("engine_id"):
        for policy in d.state["policies"]:
            try:
                d.control.delete_policy(policyEngineId=d.state["engine_id"], policyId=policy["id"])
                wait_deleted(d, "get_policy",
                             policyEngineId=d.state["engine_id"], policyId=policy["id"])
            except d.control.exceptions.ResourceNotFoundException:
                pass
        try:
            d.control.delete_policy_engine(policyEngineId=d.state["engine_id"])
            wait_deleted(d, "get_policy_engine", policyEngineId=d.state["engine_id"])
        except d.control.exceptions.ResourceNotFoundException:
            pass
    if d.state.get("runtime_role_name"):
        iam = d.client("iam")
        try:
            role = iam.get_role(RoleName=d.state["runtime_role_name"])["Role"]
            require({t["Key"]: t["Value"] for t in role.get("Tags", [])}.items() >= d.tags.items(),
                    "Runtime role ownership tags mismatch.")
            try:
                iam.delete_role_policy(RoleName=d.state["runtime_role_name"], PolicyName="RuntimeV2Execution")
            except iam.exceptions.NoSuchEntityException:
                pass
            iam.delete_role(RoleName=d.state["runtime_role_name"])
        except iam.exceptions.NoSuchEntityException:
            pass
    if d.state["created"].get("bucket"):
        s3, bucket = d.client("s3"), d.state["bucket"]
        try:
            tags = s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
            require({t["Key"]: t["Value"] for t in tags}.items() >= d.tags.items(),
                    "Artifact bucket ownership tags mismatch.")
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                objects = page.get("Contents", [])
                require(all(o["Key"].startswith("runtime-v2/") for o in objects),
                        "Unexpected object in artifact bucket; refusing to remove it.")
                if objects:
                    result = s3.delete_objects(Bucket=bucket, Delete={
                        "Objects": [{"Key": o["Key"]} for o in objects], "Quiet": True})
                    require(not result.get("Errors"), "Artifact deletion failed.")
            s3.delete_bucket(Bucket=bucket)
        except s3.exceptions.NoSuchBucket:
            pass
        d.state["created"]["bucket"] = False
        d.save()
    d.state["deleted"] = True
    d.save()
    emit("deleted", retained_upstream_data=["SpendTable", "KMS key", "rollover archive bucket"],
         retained_details="Upstream Retain resources and service log groups require separate disposal; "
                          "see stack_resources in the private state.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", default=parser().get_default("state"))
    p.add_argument("--yes", action="store_true", help="Confirm deletion of the owned deployment.")
    args = p.parse_args()
    require(args.yes, "Deletion requires --yes.")
    cleanup(Deployment(args.state))

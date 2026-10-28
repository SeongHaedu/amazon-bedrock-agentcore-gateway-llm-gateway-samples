"""Configure Policy principal admission and URI-based model authorization."""
from __future__ import annotations

import json
import time
from common import CONTROL, Environment, main_guard
from native_access import deploy_access


def policy(env, label, statement):
    engine = env.one("engine")
    name = env.run.replace("-", "_") + "_" + label
    existing = [r for r in env.state["resources"] if r["kind"] == "policy"
                and r.get("label") == label and not r.get("deleted")]
    if existing:
        current = env.client(CONTROL).get_policy(
            policyEngineId=engine["id"], policyId=existing[0]["id"])
        if current.get("definition", {}).get("cedar", {}).get("statement") != statement:
            env.state.setdefault("policy_validation_history", []).append(current)
            env.save()
            env.client(CONTROL).update_policy(
                policyEngineId=engine["id"], policyId=existing[0]["id"],
                definition={"cedar": {"statement": statement}},
                validationMode="FAIL_ON_ANY_FINDINGS", enforcementMode="ACTIVE")
        env.wait("get_policy", "ACTIVE", policyEngineId=engine["id"], policyId=existing[0]["id"])
        return
    result = env.create(
        "policy", name, CONTROL, "create_policy",
        lambda r: (r["policyId"], {"engine_id": engine["id"]}),
        label=label, policyEngineId=engine["id"], name=name,
        definition={"cedar": {"statement": statement}},
        validationMode="FAIL_ON_ANY_FINDINGS", enforcementMode="ACTIVE",
    )
    env.wait("get_policy", "ACTIVE", policyEngineId=engine["id"], policyId=result["id"])
    result["statement"] = statement
    env.save()


def main():
    env = Environment()
    if not any(r["kind"] == "engine" and not r.get("deleted") for r in env.state["resources"]):
        engine_name = env.run.replace("-", "_") + "_engine"
        engine = env.create(
            "engine", engine_name, CONTROL, "create_policy_engine",
            lambda r: (r["policyEngineId"], {"arn": r["policyEngineArn"]}),
            name=engine_name,
        )
        env.wait("get_policy_engine", "ACTIVE", policyEngineId=engine["id"])
        role, gateway = env.one("role"), env.one("gateway")
        permissions = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "bedrock-agentcore:GetPolicyEngine", "Resource": engine["arn"]},
            {"Effect": "Allow", "Action": ["bedrock-agentcore:AuthorizeAction",
                                          "bedrock-agentcore:PartiallyAuthorizeActions"],
             "Resource": [engine["arn"], gateway["arn"]]},
        ]}
        env.create(
            "role_policy", env.run + "-policy", "iam", "put_role_policy",
            lambda r: (env.run + "-policy", {"role_name": role["id"]}),
            RoleName=role["id"], PolicyName=env.run + "-policy",
            PolicyDocument=json.dumps(permissions),
        )
        time.sleep(15)
    access = deploy_access(env)
    for user in ("a", "b", "c"):
        subject = json.dumps(env.one("user", user)["sub"])
        policy(env, "standard_user" if user == "b" else "user_" + user,
               f"permit(principal == AgentCore::OAuthUser::{subject}, "
               "action, resource is AgentCore::Gateway);")
    env.gateway_update(
        policyEngineConfiguration={"arn": env.one("engine")["arn"], "mode": "ENFORCE"},
        interceptorConfigurations=[{
            "interceptor": {"lambda": {"arn": access["arn"]}}, "interceptionPoints": ["REQUEST"],
            "inputConfiguration": {"passRequestHeaders": True}}])
    env.state["verification"]["access_configuration"] = "requires_live_verification"
    env.save()
    print(json.dumps({"status": "configured", "verification": "Run all six user/model checks."}))


if __name__ == "__main__":
    main_guard(main)

"""Create an isolated JWT-authenticated inference Gateway and three test users."""
from __future__ import annotations

import json
import os
import time
from common import CONTROL, MANAGED_BY, Environment, main_guard, require

LIST_MODELS = [
    "bedrock:ListFoundationModels", "bedrock:ListInferenceProfiles",
    "bedrock:ListProvisionedModelThroughputs", "bedrock:ListCustomModelDeployments",
    "bedrock:ListImportedModels", "bedrock:ListMarketplaceModelEndpoints", "bedrock:ListPromptRouters",
]


def main():
    standard = os.environ.get("FALLBACK_MODEL_ID", "").strip()
    premium = os.environ.get("PRIMARY_MODEL_ID", "").strip()
    require(standard and premium and standard != premium,
            "Set distinct FALLBACK_MODEL_ID and PRIMARY_MODEL_ID inference profile identifiers.")
    env = Environment(create=True)
    model_resources = []
    for label, identifier in (("standard", standard), ("premium", premium)):
        result = env.client("bedrock").get_inference_profile(inferenceProfileIdentifier=identifier)
        require(result.get("status") == "ACTIVE", "Inference profile is not ACTIVE: " + label)
        arns = [result["inferenceProfileArn"]] + [m["modelArn"] for m in result["models"]]
        require(arns and all(a.startswith("arn:" + env.partition + ":bedrock:") for a in arns),
                "Unexpected model resource ARN.")
        env.state["models"][label] = {"id": identifier, "resources": arns}
        model_resources.extend(arns)
    env.save()

    pool = env.create(
        "pool", env.run + "-users", "cognito-idp", "create_user_pool",
        lambda r: (r["UserPool"]["Id"], {"arn": r["UserPool"]["Arn"]}),
        PoolName=env.run + "-users",
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
        Policies={"PasswordPolicy": {"MinimumLength": 20, "RequireUppercase": True,
                                    "RequireLowercase": True, "RequireNumbers": True,
                                    "RequireSymbols": True}},
        UserPoolTags={"ManagedBy": MANAGED_BY, "RunId": env.run},
    )
    env.create(
        "client", env.run + "-client", "cognito-idp", "create_user_pool_client",
        lambda r: (r["UserPoolClient"]["ClientId"], {"pool_id": pool["id"]}),
        UserPoolId=pool["id"], ClientName=env.run + "-client", GenerateSecret=False,
        ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        PreventUserExistenceErrors="ENABLED",
    )
    for user in ("a", "b", "c"):
        env.create(
            "user", env.run + "-" + user, "cognito-idp", "admin_create_user",
            lambda r: (r["User"]["Username"], {
                "pool_id": pool["id"],
                "sub": next(a["Value"] for a in r["User"]["Attributes"] if a["Name"] == "sub")}),
            label=user, UserPoolId=pool["id"], Username=env.run + "-" + user,
            MessageAction="SUPPRESS", TemporaryPassword="Aa1!" + __import__("secrets").token_urlsafe(32),
        )
    gateway_arn_prefix = f"arn:{env.partition}:bedrock-agentcore:{env.region}:{env.account}:gateway/"
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": env.account},
                      "ArnLike": {"aws:SourceArn": gateway_arn_prefix + "*"}},
    }]}
    role = env.create(
        "role", env.run + "-gateway", "iam", "create_role",
        lambda r: (r["Role"]["RoleName"], {"arn": r["Role"]["Arn"], "role_id": r["Role"]["RoleId"]}),
        RoleName=env.run + "-gateway", AssumeRolePolicyDocument=json.dumps(trust),
        Tags=[{"Key": "ManagedBy", "Value": MANAGED_BY}, {"Key": "RunId", "Value": env.run}],
    )
    permission = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": sorted(set(model_resources))},
        {"Effect": "Allow", "Action": LIST_MODELS, "Resource": "*"},
    ]}
    env.create(
        "role_policy", env.run + "-invoke", "iam", "put_role_policy",
        lambda r: (env.run + "-invoke", {"role_name": role["id"]}),
        RoleName=role["id"], PolicyName=env.run + "-invoke", PolicyDocument=json.dumps(permission),
    )
    time.sleep(15)
    issuer = env.client("cognito-idp").meta.endpoint_url.rstrip("/") + "/" + pool["id"]
    gateway = env.create(
        "gateway", env.run + "-gateway", CONTROL, "create_gateway",
        lambda r: (r["gatewayId"], {"arn": r["gatewayArn"], "url": r["gatewayUrl"]}),
        name=env.run + "-gateway", roleArn=role["arn"], authorizerType="CUSTOM_JWT",
        authorizerConfiguration={"customJWTAuthorizer": {
            "discoveryUrl": issuer + "/.well-known/openid-configuration",
            "allowedClients": [env.one("client")["id"]]}},
        tags={"ManagedBy": MANAGED_BY, "RunId": env.run},
    )
    env.wait("get_gateway", "READY", gatewayIdentifier=gateway["id"])
    trust["Statement"][0]["Condition"]["ArnLike"]["aws:SourceArn"] = gateway["arn"]
    env.client("iam").update_assume_role_policy(
        RoleName=role["id"], PolicyDocument=json.dumps(trust))
    target = env.create(
        "target", env.run + "-bedrock", CONTROL, "create_gateway_target",
        lambda r: (r["targetId"], {"gateway_id": gateway["id"]}),
        gatewayIdentifier=gateway["id"], name=env.run + "-bedrock",
        targetConfiguration={"inference": {"connector": {"source": {"connectorId": "bedrock-runtime"}}}},
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )
    env.wait("get_gateway_target", "READY", gatewayIdentifier=gateway["id"], targetId=target["id"])
    print(json.dumps({"status": "ready", "state_file": str(env.path),
                      "next": "Configure and verify authorization before using other callers."}))


if __name__ == "__main__":
    main_guard(main)

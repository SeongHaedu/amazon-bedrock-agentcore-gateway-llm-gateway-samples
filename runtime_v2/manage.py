"""Deploy and operate the Runtime V2 + upstream Gateway budget sample.

All account, Region and resource identifiers are discovered or recorded in an
ignored, private state file. The original CloudFormation template is vendored
without modification; this script adds Runtime, Policy and rate limits.
"""
from __future__ import annotations

import argparse
import base64
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote
import uuid
import zipfile

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OWNER = "agentcore-gateway-runtime-v2-sample"
GROUP = "llm-gateway-access"
SUBJECT = "$.context.jwt.sub"
MODEL_TARGET = "bedrock-models"
SDK_CONFIG = Config(connect_timeout=10, read_timeout=40,
                    retries={"total_max_attempts": 3},
                    ignore_configured_endpoint_urls=True)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def save_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")
        temporary = handle.name
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def access_claim():
    return {
        "inboundTokenClaimName": "cognito:groups",
        "inboundTokenClaimValueType": "STRING_ARRAY",
        "authorizingClaimMatchValue": {
            "claimMatchOperator": "CONTAINS",
            "claimMatchValue": {"matchValueString": GROUP},
        },
    }


def read_config(path):
    config = json.loads(Path(path).read_text())
    require(set(config["models"]) == {"standard", "premium"}, "Configure both model aliases.")
    for alias, api in (("standard", "messages"), ("premium", "responses")):
        model = config["models"][alias]
        require(model["api"] == api and re.fullmatch(r"[A-Za-z0-9_.:-]+", model["model_id"]),
                "Use an exact model ID and its supported API.")
        prices = config["price_table"].get(model["model_id"])
        require(isinstance(prices, dict) and set(prices) == {
            "in", "out", "cache_read", "cache_write_5m", "cache_write_1h"
        }, "Every requested model requires all price fields (USD per 1K tokens).")
    for prices in config["price_table"].values():
        require(all(type(v) in (int, float) and math.isfinite(v) and v >= 0
                    for v in prices.values()), "Prices must be finite and nonnegative.")
        require(prices["in"] > 0 and prices["out"] > 0, "Input/output prices must be positive.")
    for name in ("daily_budget_usd", "monthly_budget_usd", "rpm"):
        value = config[name]
        require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= 100000,
                f"Invalid {name}.")
    for name, ceiling in (
        ("concurrency", 100), ("max_model_calls", 32),
        ("max_output_tokens", 8192), ("max_input_chars", 64000),
    ):
        require(type(config[name]) is int and 1 <= config[name] <= ceiling, f"Invalid {name}.")
    return config


class Deployment:
    def __init__(self, path, *, create=False, config=None):
        self.path = Path(path).resolve()
        self.clients = {}
        regional = boto3.client("bedrock-agentcore-control",
                                region_name=os.environ.get("AWS_REGION") or None,
                                config=SDK_CONFIG)
        self.region = regional.meta.region_name
        require(self.region, "Set AWS_REGION or configure the Region in your AWS profile.")
        self.clients["bedrock-agentcore-control"] = regional
        identity = self.client("sts").get_caller_identity()
        self.account = identity["Account"]
        self.partition = identity["Arn"].split(":")[1]
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            require(self.state["schema"] == 1 and self.state["owner"] == OWNER,
                    "State belongs to a different deployment.")
            require(self.state["account_id"] == self.account and self.state["region"] == self.region,
                    "Selected AWS account/Region differs from this deployment.")
            require(re.fullmatch(r"llmgw-v2-[0-9a-f]{12}", self.state["run"]),
                    "Invalid deployment prefix.")
            if config is not None:
                require(config == self.state["config"],
                        "Configuration differs from the saved deployment; use a new state file.")
        else:
            require(create and config is not None, "Run deploy first or select an existing --state.")
            run = "llmgw-v2-" + secrets.token_hex(6)
            self.state = {
                "schema": 1, "owner": OWNER, "run": run, "account_id": self.account,
                "partition": self.partition, "region": self.region, "config": config,
                "bucket": f"{run}-{self.account}-{self.region}",
                "stack_name": run, "runtime_name": run.replace("-", "_"),
                "policies": [], "users": {}, "created": {},
            }
            self.save()

    @property
    def control(self):
        return self.client("bedrock-agentcore-control")

    @property
    def run(self):
        return self.state["run"]

    @property
    def tags(self):
        return {"ManagedBy": OWNER, "RunId": self.run}

    @property
    def credentials_path(self):
        return self.path.with_name(self.path.stem + "-credentials.json")

    def client(self, service):
        if service not in self.clients:
            self.clients[service] = boto3.client(service, region_name=self.region, config=SDK_CONFIG)
        return self.clients[service]

    def save(self):
        save_private(self.path, self.state)

    def wait(self, operation, desired, *, timeout=1800, field="status", **kwargs):
        deadline = time.monotonic() + timeout
        previous = None
        while time.monotonic() < deadline:
            result = getattr(self.control, operation)(**kwargs)
            status = result.get(field)
            if status != previous:
                emit(operation, status=status)
                previous = status
            if status == desired:
                return result
            if status and "FAILED" in status:
                raise RuntimeError(json.dumps({"operation": operation, "status": status,
                                               "reasons": result.get("statusReasons", [])}))
            time.sleep(5)
        raise TimeoutError(operation)

    def gateway_update(self, **changes):
        gateway = self.control.get_gateway(gatewayIdentifier=self.state["gateway_id"])
        require(gateway["name"] == self.run, "Gateway ownership mismatch.")
        require(gateway["status"] == "READY", "Gateway is not READY.")
        keys = self.control.meta.service_model.operation_model("UpdateGateway").input_shape.members
        body = {k: v for k, v in gateway.items() if k in keys and k not in {
            "gatewayIdentifier", "clientToken", "protocolType", "protocolConfiguration",
        }}
        body.update(gatewayIdentifier=self.state["gateway_id"], **changes)
        body = {k: v for k, v in body.items() if v is not None}
        self.control.update_gateway(**body)
        return self.wait("get_gateway", "READY", gatewayIdentifier=self.state["gateway_id"])


def package_agent():
    """Build Linux ARM64 Python 3.11 wheels; never ship host credentials."""
    build = REPO / "build/runtime-v2"
    build.mkdir(parents=True, exist_ok=True)
    target = build / "package"
    source_hash = hashlib.sha256(
        (HERE / "agent.py").read_bytes() + (HERE / "requirements-agent.txt").read_bytes()
    ).hexdigest()
    marker, archive = build / "source.sha256", build / "agent.zip"
    if marker.exists() and marker.read_text() == source_hash and archive.exists():
        return archive
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    package_environment = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
    package_environment["PIP_CONFIG_FILE"] = os.devnull
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
        "--index-url", "https://pypi.org/simple",
        "--platform", "manylinux2014_aarch64", "--python-version", "3.11",
        "--only-binary=:all:", "--no-compile", "--target", str(target),
        "-r", str(HERE / "requirements-agent.txt"),
    ], check=True, env=package_environment)
    shutil.copy2(HERE / "agent.py", target / "agent.py")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(target.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                bundle.write(path, path.relative_to(target))
    marker.write_text(source_hash)
    emit("packaged", bytes=archive.stat().st_size, source_sha256=source_hash)
    return archive


def artifact_bucket(deployment):
    d, s3 = deployment, deployment.client("s3")
    name = d.state["bucket"]
    if not d.state["created"].get("bucket"):
        arguments = {"Bucket": name}
        if d.region != "us-east-1":
            arguments["CreateBucketConfiguration"] = {"LocationConstraint": d.region}
        try:
            s3.create_bucket(**arguments)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                raise
            actual = s3.get_bucket_tagging(Bucket=name)["TagSet"]
            require({v["Key"]: v["Value"] for v in actual} == d.tags, "Existing bucket is not owned.")
        s3.put_bucket_tagging(Bucket=name, Tagging={"TagSet": [
            {"Key": k, "Value": v} for k, v in d.tags.items()
        ]})
        s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        })
        s3.put_bucket_encryption(Bucket=name, ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}],
        })
        d.state["created"]["bucket"] = True
        d.save()
    return name


def deploy_upstream(d):
    vendor = HERE / "vendor/usage-interceptor"
    template = vendor / "standalone.yaml"
    provenance = json.loads((vendor / "SOURCE.json").read_text())
    require(hashlib.sha256(template.read_bytes()).hexdigest() == provenance["sha256"],
            "The vendored upstream template was modified.")
    cf = d.client("cloudformation")
    config = d.state["config"]
    try:
        stack = cf.describe_stacks(StackName=d.state["stack_name"])["Stacks"][0]
        require({t["Key"]: t["Value"] for t in stack.get("Tags", [])}.items() >= d.tags.items(),
                "Existing stack is not owned.")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ValidationError" or "does not exist" not in str(exc):
            raise
        d.client("s3").upload_file(str(template), d.state["bucket"], "runtime-v2/standalone.yaml")
        parameters = {
            "DomainPrefix": d.run,
            "DailyBudgetUsd": str(config["daily_budget_usd"]),
            "MonthlyBudgetUsd": str(config["monthly_budget_usd"]),
            "PriceTableJson": json.dumps(config["price_table"], separators=(",", ":")),
            "UserClaim": "sub", "EnforceBudget": "true",
            "ProvisionedConcurrency": "0", "ReservedConcurrency": "10",
        }
        cf.create_stack(
            StackName=d.state["stack_name"],
            TemplateURL=f"https://s3.{d.region}.amazonaws.com/{d.state['bucket']}/runtime-v2/standalone.yaml",
            Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()],
            Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"],
            Tags=[{"Key": k, "Value": v} for k, v in d.tags.items()],
            ClientRequestToken=d.run,
        )
    deadline, previous = time.monotonic() + 2400, None
    while time.monotonic() < deadline:
        stack = cf.describe_stacks(StackName=d.state["stack_name"])["Stacks"][0]
        status = stack["StackStatus"]
        if status != previous:
            emit("upstream_stack", status=status)
            previous = status
        if status == "CREATE_COMPLETE":
            break
        if "FAILED" in status or "ROLLBACK" in status:
            events = cf.describe_stack_events(StackName=d.state["stack_name"])["StackEvents"]
            failures = [{"resource": e["LogicalResourceId"], "reason": e.get("ResourceStatusReason")}
                        for e in events if "FAILED" in e["ResourceStatus"]]
            save_private(d.path.with_name("stack-failure.json"), failures)
            raise RuntimeError(f"Upstream stack {status}; inspect stack-failure.json.")
        time.sleep(10)
    else:
        raise TimeoutError("CloudFormation")
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    physical = {}
    for page in cf.get_paginator("list_stack_resources").paginate(StackName=d.state["stack_name"]):
        physical.update({r["LogicalResourceId"]: r["PhysicalResourceId"]
                         for r in page["StackResourceSummaries"] if r.get("PhysicalResourceId")})
    d.state.update(
        gateway_id=outputs["GatewayId"], gateway_url=outputs["InferenceBaseUrl"],
        discovery_url=outputs["DiscoveryUrl"], human_client=outputs["ClientId"],
        pool_id=physical["UserPool"], gateway_role=physical["GatewayRole"],
        spend_table=outputs["SpendTableName"], stack_resources=physical, stack_outputs=outputs,
    )
    gateway = d.wait("get_gateway", "READY", gatewayIdentifier=d.state["gateway_id"])
    d.state["gateway_arn"] = gateway["gatewayArn"]
    d.save()


def create_user(d, username, *, member=True):
    """Provision only users in this deployment's dedicated pool."""
    cognito, pool = d.client("cognito-idp"), d.state["pool_id"]
    if username not in d.state["users"]:
        password = "Aa1!" + secrets.token_urlsafe(30)
        secret_data = json.loads(d.credentials_path.read_text()) if d.credentials_path.exists() else {}
        secret_data[username] = password
        save_private(d.credentials_path, secret_data)
        try:
            response = cognito.admin_create_user(
                UserPoolId=pool, Username=username, MessageAction="SUPPRESS")
            person = response["User"]
        except cognito.exceptions.UsernameExistsException:
            # Dedicated pool, exact username; resume an interrupted provisioning.
            person = cognito.admin_get_user(UserPoolId=pool, Username=username)
        cognito.admin_set_user_password(UserPoolId=pool, Username=username, Password=password, Permanent=True)
        attrs = person.get("Attributes") or person.get("UserAttributes")
        d.state["users"][username] = {"sub": next(a["Value"] for a in attrs if a["Name"] == "sub")}
        d.save()
    if member:
        cognito.admin_add_user_to_group(UserPoolId=pool, Username=username, GroupName=GROUP)
    return d.state["users"][username]


def configure_identity(d):
    cognito = d.client("cognito-idp")
    if not d.state.get("cli_client"):
        name = d.run + "-cli"
        clients = cognito.list_user_pool_clients(UserPoolId=d.state["pool_id"], MaxResults=60)["UserPoolClients"]
        matches = [c for c in clients if c["ClientName"] == name]
        require(len(matches) <= 1, "Ambiguous sample client.")
        if matches:
            d.state["cli_client"] = matches[0]["ClientId"]
        else:
            result = cognito.create_user_pool_client(
                UserPoolId=d.state["pool_id"], ClientName=name, GenerateSecret=False,
                ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
                PreventUserExistenceErrors="ENABLED", AccessTokenValidity=1,
                TokenValidityUnits={"AccessToken": "hours"},
            )
            d.state["cli_client"] = result["UserPoolClient"]["ClientId"]
        d.save()
    try:
        cognito.create_group(UserPoolId=d.state["pool_id"], GroupName=GROUP)
    except cognito.exceptions.GroupExistsException:
        pass
    for user in ("employee", "restricted", "outsider"):
        create_user(d, user, member=user != "outsider")
    d.state["jwt_authorizer"] = {
        "discoveryUrl": d.state["discovery_url"],
        "allowedClients": [d.state["human_client"], d.state["cli_client"]],
        "customClaims": [access_claim()],
    }
    d.gateway_update(authorizerConfiguration={"customJWTAuthorizer": d.state["jwt_authorizer"]})
    d.save()


def token(d, username):
    require(username in d.state["users"], "Unknown sample user.")
    password = json.loads(d.credentials_path.read_text())[username]
    result = d.client("cognito-idp").initiate_auth(
        ClientId=d.state["cli_client"], AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    require("AuthenticationResult" in result, "Unexpected Cognito challenge.")
    return result["AuthenticationResult"]["AccessToken"]


def http_call(url, jwt, payload=None, *, runtime=False):
    headers = {"Authorization": "Bearer " + jwt, "Content-Type": "application/json"}
    if runtime:
        headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] = str(uuid.uuid4())
    with httpx.Client(timeout=httpx.Timeout(240, connect=10), trust_env=False,
                      follow_redirects=False) as client:
        response = client.request("POST" if payload is not None else "GET",
                                  url, headers=headers, json=payload)
        try:
            body = response.json()
        except ValueError:
            body = {"message": response.text[:2000]}
        return response.status_code, body


def configure_model_target(d):
    """Add Bedrock Runtime APIs when the desired models are outside Mantle."""
    resources = []
    operations = []
    for model in d.state["config"]["models"].values():
        identifier, api = model["model_id"], model["api"]
        profile = d.client("bedrock").get_inference_profile(inferenceProfileIdentifier=identifier)
        require(profile["status"] == "ACTIVE" and profile["inferenceProfileId"] == identifier,
                "Selected inference profile is not ACTIVE.")
        resources.extend([profile["inferenceProfileArn"],
                          *(item["modelArn"] for item in profile["models"])])
        provider = "openai" if api == "responses" else "anthropic"
        operations.append({"path": "/v1/" + api, "providerPath": f"/{provider}/v1/{api}",
                           "models": [{"model": identifier}]})
    d.client("iam").put_role_policy(
        RoleName=d.state["gateway_role"], PolicyName="RuntimeV2ModelAccess",
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
             "Resource": sorted(set(resources))},
            {"Effect": "Allow", "Action": "bedrock:InvokeModel",
             "Resource": f"arn:{d.partition}:bedrock:{d.region}:{d.account}:project/default"},
        ]}),
    )
    configuration = {"inference": {"provider": {
        "endpoint": d.client("bedrock-runtime").meta.endpoint_url, "operations": operations,
    }}}
    arguments = dict(
        gatewayIdentifier=d.state["gateway_id"], name=MODEL_TARGET,
        targetConfiguration=configuration,
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )
    if not d.state.get("model_target_id"):
        # List first to resume a successful create whose response was not saved.
        targets, pagination = [], {"gatewayIdentifier": d.state["gateway_id"]}
        while True:
            page = d.control.list_gateway_targets(**pagination)
            targets.extend(item for item in page["items"] if item["name"] == MODEL_TARGET)
            if not page.get("nextToken"):
                break
            pagination["nextToken"] = page["nextToken"]
        require(len(targets) <= 1, "Ambiguous model target.")
        if targets:
            target_id = targets[0]["targetId"]
        else:
            result = d.control.create_gateway_target(
                **arguments, clientToken=hashlib.sha256((d.run + "-models").encode()).hexdigest())
            target_id = result["targetId"]
        d.state["model_target_id"] = target_id
        d.save()
    target = d.wait("get_gateway_target", "READY", gatewayIdentifier=d.state["gateway_id"],
                    targetId=d.state["model_target_id"])
    require(target["targetConfiguration"] == configuration, "Existing target configuration differs.")
    time.sleep(15)  # IAM propagation before the first model call.
    d.state["model_profile_resources"] = sorted(set(resources))
    d.save()


def check_models(d):
    status, result = http_call(d.state["gateway_url"] + "/models", token(d, "employee"))
    require(status == 200, f"Gateway model discovery returned HTTP {status}.")
    available = [m["id"].split("/", 1)[-1] for m in result.get("data", [])]
    selected = [m["model_id"] for m in d.state["config"]["models"].values()]
    require(all(m in available for m in selected),
            "Selected model is absent from Gateway's configured model catalog.")
    d.state["model_catalog_verified"] = selected
    d.save()
    emit("model_catalog", selected=selected, verified=True)


def permit(subject, gateway_arn, model):
    action = MODEL_TARGET + "___POST:/v1/" + model["api"]
    values = [json.dumps(v) for v in (subject, action, gateway_arn, model["model_id"])]
    return (
        f"permit(principal == AgentCore::OAuthUser::{values[0]}, "
        f"action == AgentCore::Action::{values[1]}, "
        f"resource == AgentCore::Gateway::{values[2]}) "
        f"when {{ context.input.model == {values[3]} }};"
    )


def permit_user(d, username, aliases):
    for alias in aliases:
        name = (d.run + "_" + username + "_" + alias).replace("-", "_")
        require(len(name) <= 48, "Policy name exceeds 48 characters.")
        existing = next((p for p in d.state["policies"] if p["name"] == name), None)
        statement = permit(d.state["users"][username]["sub"], d.state["gateway_arn"],
                           d.state["config"]["models"][alias])
        if existing:
            policy = d.wait("get_policy", "ACTIVE", policyEngineId=d.state["engine_id"],
                            policyId=existing["id"])
            require(policy["definition"]["cedar"]["statement"] == statement,
                    "Saved policy definition differs from the requested permission.")
            continue
        result = d.control.create_policy(
            policyEngineId=d.state["engine_id"], name=name,
            definition={"cedar": {"statement": statement}},
            validationMode="FAIL_ON_ANY_FINDINGS", enforcementMode="ACTIVE",
            clientToken=hashlib.sha256((d.run + name).encode()).hexdigest(),
        )
        d.state["policies"].append({"name": name, "id": result["policyId"], "user": username})
        d.save()
        d.wait("get_policy", "ACTIVE", policyEngineId=d.state["engine_id"], policyId=result["policyId"])


def configure_policy(d):
    if not d.state.get("engine_id"):
        result = d.control.create_policy_engine(
            name=d.run.replace("-", "_"), tags=d.tags,
            clientToken=hashlib.sha256((d.run + "-engine").encode()).hexdigest(),
        )
        d.state.update(engine_id=result["policyEngineId"], engine_arn=result["policyEngineArn"])
        d.save()
    d.wait("get_policy_engine", "ACTIVE", policyEngineId=d.state["engine_id"])
    d.client("iam").put_role_policy(
        RoleName=d.state["gateway_role"], PolicyName="RuntimeV2PolicyEvaluation",
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Action": "bedrock-agentcore:GetPolicyEngine",
            "Resource": d.state["engine_arn"],
        }, {
            "Effect": "Allow", "Action": [
                "bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions",
            ], "Resource": [d.state["engine_arn"], d.state["gateway_arn"]],
        }]}),
    )
    time.sleep(15)
    d.gateway_update(policyEngineConfiguration={"arn": d.state["engine_arn"], "mode": "ENFORCE"})
    permit_user(d, "employee", ["standard", "premium"])
    permit_user(d, "restricted", ["standard"])


def rate_entries(subject, *, rpm=None, concurrency=None):
    entry = {"dimensions": {SUBJECT: subject}}
    if rpm is not None:
        entry["requests"] = [{"rate": rpm, "period": "minute"}]
    if concurrency is not None:
        entry["connections"] = [{"rate": concurrency, "period": "second"}]
    return [entry]


def set_rate(d, rate_id, entries, *, create=False):
    kwargs = dict(gatewayIdentifier=d.state["gateway_id"], rateLimitId=rate_id,
                  description="Owned by " + d.run, entries=entries)
    if create:
        d.control.create_gateway_rate_limit(
            **kwargs, dimensionKeys=[SUBJECT],
            clientToken=hashlib.sha256((d.run + rate_id).encode()).hexdigest())
    else:
        d.control.update_gateway_rate_limit(**kwargs)
    d.wait("get_gateway_rate_limit", "ACTIVE", gatewayIdentifier=d.state["gateway_id"], rateLimitId=rate_id)


def configure_rate(d):
    rate_id = d.run + "-per-sub"
    config = d.state["config"]
    entries = rate_entries("*", rpm=config["rpm"], concurrency=config["concurrency"])
    d.state["rate_id"] = rate_id
    d.save()
    try:
        rate = d.control.get_gateway_rate_limit(
            gatewayIdentifier=d.state["gateway_id"], rateLimitId=rate_id)
        require(rate.get("description") == "Owned by " + d.run, "Rate limit ownership mismatch.")
        d.wait("get_gateway_rate_limit", "ACTIVE",
               gatewayIdentifier=d.state["gateway_id"], rateLimitId=rate_id)
        require(rate["entries"] == entries, "Rate limit differs from the configured limits.")
    except d.control.exceptions.ResourceNotFoundException:
        set_rate(d, rate_id, entries, create=True)


def runtime_permissions(d):
    base = f"arn:{d.partition}"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"],
         "Resource": f"{base}:s3:::{d.state['bucket']}/runtime-v2/agent-*.zip"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:DescribeLogStreams"],
         "Resource": f"{base}:logs:{d.region}:{d.account}:log-group:/aws/bedrock-agentcore/runtimes/*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"{base}:logs:{d.region}:{d.account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"},
        {"Effect": "Allow", "Action": [
            "bedrock-agentcore:GetWorkloadAccessToken",
            "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
            "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
         ], "Resource": [
             f"{base}:bedrock-agentcore:{d.region}:{d.account}:workload-identity-directory/default",
             f"{base}:bedrock-agentcore:{d.region}:{d.account}:workload-identity-directory/default/workload-identity/*",
         ]},
    ]}


def deploy_runtime(d, archive):
    iam = d.client("iam")
    name = d.run + "-runtime"
    if not d.state.get("runtime_role_arn"):
        trust = {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole", "Condition": {
                "StringEquals": {"aws:SourceAccount": d.account},
                "ArnLike": {"aws:SourceArn": f"arn:{d.partition}:bedrock-agentcore:{d.region}:{d.account}:*"},
            },
        }]}
        try:
            role = iam.create_role(
                RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                Tags=[{"Key": k, "Value": v} for k, v in d.tags.items()],
            )["Role"]
        except iam.exceptions.EntityAlreadyExistsException:
            role = iam.get_role(RoleName=name)["Role"]
            require({t["Key"]: t["Value"] for t in role.get("Tags", [])} == d.tags,
                    "Runtime role ownership mismatch.")
        d.state.update(runtime_role_arn=role["Arn"], runtime_role_name=name)
        d.save()
    iam.put_role_policy(RoleName=name, PolicyName="RuntimeV2Execution",
                        PolicyDocument=json.dumps(runtime_permissions(d)))
    artifact_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    key = "runtime-v2/agent-" + artifact_hash[:16] + ".zip"
    d.client("s3").upload_file(str(archive), d.state["bucket"], key)
    cfg = d.state["config"]
    parameters = {
        "agentRuntimeArtifact": {"codeConfiguration": {
            "code": {"s3": {"bucket": d.state["bucket"], "prefix": key}},
            "runtime": "PYTHON_3_11", "entryPoint": ["agent.py"],
        }},
        "roleArn": d.state["runtime_role_arn"],
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "authorizerConfiguration": {"customJWTAuthorizer": d.state["jwt_authorizer"]},
        "requestHeaderConfiguration": {"requestHeaderAllowlist": ["Authorization"]},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 60, "maxLifetime": 600},
        "environmentVariables": {
            "GATEWAY_URL": d.state["gateway_url"],
            "MODEL_CATALOG_JSON": json.dumps(cfg["models"], separators=(",", ":")),
            "MAX_MODEL_CALLS": str(cfg["max_model_calls"]),
            "MAX_OUTPUT_TOKENS": str(cfg["max_output_tokens"]),
            "MAX_INPUT_CHARS": str(cfg["max_input_chars"]),
        },
        "platformVersion": "V2",
    }
    configuration_hash = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()
    if not d.state.get("runtime_id"):
        time.sleep(15)
        result = d.control.create_agent_runtime(
            agentRuntimeName=d.state["runtime_name"], tags=d.tags,
            clientToken=hashlib.sha256((d.run + "-runtime").encode()).hexdigest(), **parameters,
        )
        d.state.update(runtime_id=result["agentRuntimeId"], runtime_arn=result["agentRuntimeArn"])
        d.save()
    elif d.state.get("runtime_configuration_hash") != configuration_hash:
        d.control.update_agent_runtime(agentRuntimeId=d.state["runtime_id"], **parameters)
    runtime = d.wait("get_agent_runtime", "READY", agentRuntimeId=d.state["runtime_id"])
    require(runtime["platformVersion"] == "V2", "Runtime did not report platform V2.")
    d.state.update(
        artifact_hash=artifact_hash, runtime_version=runtime["agentRuntimeVersion"],
        runtime_configuration_hash=configuration_hash,
        runtime_url=f"{d.client('bedrock-agentcore').meta.endpoint_url}/runtimes/"
                    f"{quote(d.state['runtime_arn'], safe='')}/invocations?qualifier=DEFAULT",
        ready=True,
    )
    d.save()
    emit("runtime_ready", platform_version=runtime["platformVersion"],
         runtime_version=runtime["agentRuntimeVersion"])


def deploy(args):
    config = read_config(args.config)
    archive = package_agent()
    d = Deployment(args.state, create=True, config=config)
    require(not d.state.get("deleted"), "Deployment was deleted; select a new --state.")
    artifact_bucket(d)
    deploy_upstream(d)
    configure_identity(d)
    configure_model_target(d)
    if not d.state.get("engine_id"):
        check_models(d)
    configure_policy(d)
    configure_rate(d)
    deploy_runtime(d, archive)
    save_private(d.path.with_name("client.json"), {
        "region": d.region, "client_id": d.state["cli_client"], "runtime_url": d.state["runtime_url"],
    })
    emit("deployment_complete", state=str(d.path), region=d.region, platform="V2")


def spend(d, username):
    require(username in d.state["users"], "Unknown sample user.")
    return d.client("dynamodb").get_item(
        TableName=d.state["spend_table"], Key={"pk": {"S": "USER#" + d.state["users"][username]["sub"]}},
        ConsistentRead=True,
    ).get("Item", {})


def budget(args):
    d = Deployment(args.state)
    require(args.user in d.state["users"], "Unknown sample user.")
    if args.daily is not None or args.monthly is not None:
        updates, names, values = [], {}, {}
        for label, value in (("daily", args.daily), ("monthly", args.monthly)):
            if value is None:
                continue
            number = Decimal(value)
            require(number.is_finite() and number >= 0, "Budget must be finite and nonnegative.")
            names["#" + label] = label + "_cap_usd"
            values[":" + label] = {"N": str(number)}
            updates.append("#" + label + " = :" + label)
        d.client("dynamodb").update_item(
            TableName=d.state["spend_table"], Key={"pk": {"S": "USER#" + d.state["users"][args.user]["sub"]}},
            UpdateExpression="SET " + ", ".join(updates),
            ExpressionAttributeNames=names, ExpressionAttributeValues=values,
        )
    item = spend(d, args.user)
    emit("budget", user=args.user, **{k: v["N"] for k, v in item.items() if "N" in v})


def invoke(args):
    d = Deployment(args.state)
    status, result = http_call(d.state["runtime_url"], token(d, args.user),
                               {"task": args.task, "model": args.model}, runtime=True)
    print(json.dumps({"http_status": status, "result": result}, ensure_ascii=False, indent=2))
    if status != 200 or result.get("status") != "completed":
        raise SystemExit(2)


def show_status(args):
    d = Deployment(args.state)
    result = d.control.get_agent_runtime(agentRuntimeId=d.state["runtime_id"])
    gateway = d.control.get_gateway(gatewayIdentifier=d.state["gateway_id"])
    rate = d.control.get_gateway_rate_limit(
        gatewayIdentifier=d.state["gateway_id"], rateLimitId=d.state["rate_id"])
    emit("status", runtime_status=result["status"], platform_version=result["platformVersion"],
         runtime_version=result["agentRuntimeVersion"],
         gateway_status=gateway["status"],
         policy_mode=gateway.get("policyEngineConfiguration", {}).get("mode"),
         interception_points=[p for c in gateway.get("interceptorConfigurations", [])
                              for p in c["interceptionPoints"]],
         rate_status=rate["status"], rate_entries=rate["entries"],
         verification_recovery_pending=d.path.with_name("verification-rate-recovery.json").exists(),
         region=d.region)


def export_config(args):
    """Recover the deployment's configuration without exporting its credentials."""
    d = Deployment(args.state)
    destination = Path(args.output).resolve()
    require(not destination.exists(), "Output already exists; choose a new --output path.")
    save_private(destination, d.state["config"])
    emit("configuration_exported", path=str(destination))


def restore_verification(d):
    """Restore only the owned rate limit and disable the recorded probe identity."""
    path = d.path.with_name("verification-rate-recovery.json")
    require(path.is_file(), "No pending verification recovery file.")
    recovery = json.loads(path.read_text())
    require(recovery["rate_id"] == d.state["rate_id"],
            "Recovery rate limit does not belong to this deployment.")
    require(recovery.get("gateway_id", d.state["gateway_id"]) == d.state["gateway_id"],
            "Recovery Gateway does not belong to this deployment.")
    require(recovery["entries"] == rate_entries(
        "*", rpm=d.state["config"]["rpm"], concurrency=d.state["config"]["concurrency"]),
        "Recovery entries differ from the deployment's configured limits.")
    username = recovery.get("probe_username")
    require(username is None or re.fullmatch(r"probe-[0-9a-f]{8}", username),
            "Invalid recovery probe username.")
    arguments = dict(gatewayIdentifier=d.state["gateway_id"], rateLimitId=d.state["rate_id"])
    foreign_rate = False
    try:
        rate = d.control.get_gateway_rate_limit(**arguments)
        foreign_rate = rate.get("description") != "Owned by " + d.run
        require(not foreign_rate, "Rate limit ownership mismatch.")
        if rate["status"] != "ACTIVE":
            rate = d.wait("get_gateway_rate_limit", "ACTIVE", **arguments)
        if rate["entries"] != recovery["entries"] or rate["status"] != "ACTIVE":
            set_rate(d, d.state["rate_id"], recovery["entries"])
        current = d.control.get_gateway_rate_limit(**arguments)
        require(current["status"] == "ACTIVE" and current["entries"] == recovery["entries"],
                "Rate restoration failed; keep the recovery file and retry.")
    finally:
        if username is not None and not foreign_rate:
            cognito = d.client("cognito-idp")
            # Independently establish pool ownership even when the rate API is
            # unavailable. The pinned template names its pool <stack>-users.
            pool = cognito.describe_user_pool(UserPoolId=d.state["pool_id"])["UserPool"]
            require(pool["Name"] == d.run + "-users", "Probe user pool ownership mismatch.")
            try:
                cognito.admin_disable_user(UserPoolId=d.state["pool_id"], Username=username)
            except cognito.exceptions.UserNotFoundException:
                pass
    # Retain evidence until both restoration and probe disabling have succeeded.
    path.unlink()
    emit("verification_restored", resources_retained=True)


def recover(args):
    require(args.yes, "Pass --yes to restore the recorded rate limits and disable the probe user.")
    restore_verification(Deployment(args.state))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", default=os.environ.get("RUNTIME_V2_STATE") or
                   str(REPO / "results/runtime-v2/state.json"))
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("deploy", help="Create the dedicated AWS demonstration.")
    c.add_argument("--config", default=str(HERE / "config.example.json"))
    c.set_defaults(function=deploy)
    c = sub.add_parser("package", help="Build the Runtime artifact without AWS calls.")
    c.set_defaults(function=lambda _: package_agent())
    c = sub.add_parser("status")
    c.set_defaults(function=show_status)
    c = sub.add_parser("export-config", help="Export saved configuration for reusing this deployment.")
    c.add_argument("--output", required=True)
    c.set_defaults(function=export_config)
    c = sub.add_parser("restore-verification", help="Recover an interrupted verification; retain resources.")
    c.add_argument("--yes", action="store_true")
    c.set_defaults(function=recover)
    c = sub.add_parser("invoke")
    c.add_argument("--user", default="employee")
    c.add_argument("--model", choices=["standard", "premium"], default="standard")
    c.add_argument("--task", required=True)
    c.set_defaults(function=invoke)
    c = sub.add_parser("budget")
    c.add_argument("--user", default="employee")
    c.add_argument("--daily")
    c.add_argument("--monthly")
    c.set_defaults(function=budget)
    return p


if __name__ == "__main__":
    arguments = parser().parse_args()
    arguments.function(arguments)

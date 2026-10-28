"""Configuration and resource ledger shared by the deployment examples."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ParamValidationError

REPO = Path(__file__).resolve().parents[1]
MANAGED_BY = "agentcore-llm-gateway-samples"
CONTROL = "bedrock-agentcore-control"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump(data, f, indent=2, default=str)
        f.write("\n")
        name = f.name
    os.chmod(name, 0o600)
    os.replace(name, path)


class Environment:
    def __init__(self, *, create=False):
        self.path = Path(os.environ.get("GATEWAY_STATE_FILE") or REPO / "results/resources.json")
        profile = os.environ.get("AWS_PROFILE") or None
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or None
        self.session = boto3.Session(profile_name=profile, region_name=region)
        self.region = self.session.region_name
        require(self.region, "Set AWS_REGION or configure a Region in your AWS profile.")
        self.config = Config(retries={"total_max_attempts": 1}, connect_timeout=10,
                             read_timeout=30, ignore_configured_endpoint_urls=True)
        self.clients = {}
        identity = self.client("sts").get_caller_identity()
        self.account = identity["Account"]
        self.partition = identity["Arn"].split(":")[1]
        if create:
            require(not self.path.exists(), "State already exists. Inspect or clean up that run first.")
            self.state = {
                "schema": 1, "run_id": "llmgw-" + secrets.token_hex(6),
                "account_id": self.account, "region": self.region,
                "partition": self.partition, "resources": [], "intents": [],
                "models": {}, "verification": {},
            }
            self.save()
        else:
            require(self.path.is_file(), "Run scripts/setup.py first, or set GATEWAY_STATE_FILE.")
            self.state = json.loads(self.path.read_text())
            require(self.state.get("schema") == 1, "Unsupported state schema.")
            require(self.state["account_id"] == self.account, "Credentials belong to another account.")
            require(self.state["region"] == self.region, "Selected Region differs from saved state.")
            require(self.state["partition"] == self.partition, "AWS partition differs from saved state.")
            require(re.fullmatch(r"llmgw-[0-9a-f]{12}", self.state["run_id"]), "Invalid run ID.")
            for row in self.state["resources"]:
                require(row.get("run_id") == self.state["run_id"] and row.get("id"),
                        "Invalid resource ownership ledger.")
                require(row["name"].startswith(self.run) or row["name"].startswith(self.run.replace("-", "_"))
                        or row["name"].startswith("/aws/lambda/" + self.run),
                        "Resource name is outside this run.")
        self.passwords = {}

    @property
    def run(self):
        return self.state["run_id"]

    def client(self, service):
        if service not in self.clients:
            self.clients[service] = self.session.client(service, config=self.config)
        return self.clients[service]

    def save(self):
        write_json(self.path, self.state)

    def one(self, kind, label=None):
        rows = [r for r in self.state["resources"]
                if r["kind"] == kind and not r.get("deleted")
                and (label is None or r.get("label") == label)]
        require(len(rows) == 1, "Expected exactly one owned resource: " + kind + "/" + str(label))
        return rows[0]

    def create(self, kind, ledger_name, service, operation, extract, *, label=None, **params):
        require(not any(r["kind"] == kind and r["name"] == ledger_name
                        for r in self.state["resources"]), "Duplicate create refused.")
        intent = {"kind": kind, "name": ledger_name, "status": "pending"}
        self.state["intents"].append(intent)
        self.save()
        try:
            result = getattr(self.client(service), operation)(**params)
            identifier, fields = extract(result)
            row = {"kind": kind, "name": ledger_name, "id": identifier, "label": label,
                   "run_id": self.run, "deleted": False, **fields}
            self.state["resources"].append(row)
            intent["status"] = "recorded"
            self.save()
            print(json.dumps({"created": kind, "label": label}))
            return row
        except ParamValidationError:
            intent["status"] = "not_created"
            self.save()
            raise
        except ClientError as exc:
            intent["status"] = ("not_created" if exc.response["Error"]["Code"] in {
                "ValidationException", "InvalidParameterException", "AccessDeniedException"
            } else "unresolved")
            self.save()
            raise
        except Exception:
            intent["status"] = "unresolved"
            self.save()
            raise

    def wait(self, operation, desired, **params):
        timeout = int(os.environ.get("GATEWAY_WAIT_SECONDS") or "240")
        require(10 <= timeout <= 1800, "GATEWAY_WAIT_SECONDS must be between 10 and 1800.")
        deadline = time.monotonic() + timeout
        previous = None
        while True:
            result = getattr(self.client(CONTROL), operation)(**params)
            status = result.get("status")
            if status != previous:
                print(json.dumps({"operation": operation, "status": status}))
                previous = status
            if status == desired:
                return result
            if status and "FAILED" in status:
                raise RuntimeError(json.dumps({"operation": operation, "status": status,
                                               "reasons": result.get("statusReasons", [])}))
            require(time.monotonic() < deadline, "Timed out waiting for " + operation)
            time.sleep(4)

    def gateway_update(self, **changes):
        gateway = self.one("gateway")
        current = self.client(CONTROL).get_gateway(gatewayIdentifier=gateway["id"])
        require(current["gatewayArn"] == gateway["arn"] and current["name"] == gateway["name"],
                "Gateway ownership does not match state.")
        require(current["status"] == "READY", "Gateway must be READY before an update.")
        operation = self.client(CONTROL).meta.service_model.operation_model("UpdateGateway")
        fields = operation.input_shape.members
        params = {k: v for k, v in current.items() if k in fields and k not in {
            "gatewayIdentifier", "clientToken", "protocolType", "protocolConfiguration"}}
        # This sample deliberately omits protocolType for mixed/inference targets.
        params["gatewayIdentifier"] = gateway["id"]
        params.update(changes)
        self.client(CONTROL).update_gateway(**params)
        return self.wait("get_gateway", "READY", gatewayIdentifier=gateway["id"])

    def token(self, user):
        require(user in {"a", "b", "c"}, "User must be a, b, or c.")
        pool, client, person = self.one("pool"), self.one("client"), self.one("user", user)
        password = "Aa1!" + secrets.token_urlsafe(32)
        self.client("cognito-idp").admin_set_user_password(
            UserPoolId=pool["id"], Username=person["id"], Password=password, Permanent=True)
        response = self.client("cognito-idp").admin_initiate_auth(
            UserPoolId=pool["id"], ClientId=client["id"], AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": person["id"], "PASSWORD": password})
        require(not response.get("ChallengeName") and "AuthenticationResult" in response,
                "Cognito authentication requires an unexpected challenge.")
        return response["AuthenticationResult"]["AccessToken"]


def main_guard(function):
    try:
        function()
    except Exception as exc:
        # SDK errors identify an operation and reason; request bodies and auth responses are never logged.
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
        raise SystemExit(1)

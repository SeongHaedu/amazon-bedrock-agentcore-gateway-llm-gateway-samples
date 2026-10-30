"""Sign in and invoke the hosted AI coworker without AWS credentials.

The operator shares client.json from the ignored deployment directory and gives
each person their own Cognito username/password. No backend runs on this machine.
"""
from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import re
import uuid
from urllib.parse import urlsplit

import httpx


def invoke(config, username, password, task, model):
    region = config["region"]
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", region):
        raise ValueError("Invalid AWS Region.")
    endpoint = f"https://cognito-idp.{region}.amazonaws.com"
    runtime_url = config["runtime_url"]
    parsed = urlsplit(runtime_url)
    if (parsed.scheme != "https" or parsed.hostname != f"bedrock-agentcore.{region}.amazonaws.com"
            or parsed.username or parsed.password or parsed.port is not None
            or not parsed.path.startswith("/runtimes/") or not parsed.path.endswith("/invocations")):
        raise ValueError("Use the Runtime endpoint exported by the deployment.")
    with httpx.Client(timeout=httpx.Timeout(240, connect=10), trust_env=False,
                      follow_redirects=False) as client:
        login = client.post(endpoint, headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        }, json={"AuthFlow": "USER_PASSWORD_AUTH", "ClientId": config["client_id"],
                 "AuthParameters": {"USERNAME": username, "PASSWORD": password}})
        if login.status_code != 200 or "AuthenticationResult" not in login.json():
            raise RuntimeError(f"Sign-in failed (HTTP {login.status_code}); check your account.")
        access_token = login.json()["AuthenticationResult"]["AccessToken"]
        response = client.post(runtime_url, headers={
            "Authorization": "Bearer " + access_token,
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": str(uuid.uuid4()),
        }, json={"task": task, "model": model})
        try:
            body = response.json()
        except ValueError:
            body = {"error": "Unexpected Runtime response", "http_status": response.status_code}
        return response.status_code, body


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Operator-provided client.json")
    parser.add_argument("--username", required=True)
    parser.add_argument("--model", choices=["standard", "premium"], default="standard")
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    status, result = invoke(json.loads(Path(args.config).read_text()), args.username,
                            getpass.getpass("Cognito password: "), args.task, args.model)
    print(json.dumps({"http_status": status, "result": result}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if status == 200 and result.get("status") == "completed" else 2)

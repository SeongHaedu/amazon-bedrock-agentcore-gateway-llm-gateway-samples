"""Run bounded AWS checks against this deployment and save private evidence.

Expected refusals are assertions. Probe users are disabled afterward. Verification temporarily adds a probe-specific
entry to the owned rate limit, preserves normal limits, and restores its entries.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import secrets
import sys
import time

if __package__:
    from .manage import Deployment, emit, http_call, permit_user, create_user, rate_entries, require, restore_verification, save_private, set_rate, spend, token
else:
    from manage import Deployment, emit, http_call, permit_user, create_user, rate_entries, require, restore_verification, save_private, set_rate, spend, token


TASK = (
    "AI 社員として、新人向けの1日研修を準備するための作業計画を作ってください。"
    "午前はサービス概要、午後は操作練習です。record_work_item を使って作業項目を2件記録し、"
    "最後に日本語150文字以内で計画をまとめてください。実行したとは書かないでください。"
)


def unwrap(status, body):
    """Runtime can wrap a container's non-2xx response in a service error."""
    if isinstance(body, dict) and "stop_reason" in body:
        return body
    # Keep the wire status and raw body in private evidence when this platform
    # returns an envelope we cannot positively classify.
    if isinstance(body, dict):
        for key in ("response", "body", "message", "originalMessage"):
            value = body.get(key)
            if isinstance(value, str):
                try:
                    nested = json.loads(value)
                except ValueError:
                    continue
                if isinstance(nested, dict) and "stop_reason" in nested:
                    return nested
            if isinstance(value, dict) and "stop_reason" in value:
                return value
    return body


def verify(d):
    recovery_path = d.path.with_name("verification-rate-recovery.json")
    require(not recovery_path.exists(),
            "An interrupted verification needs recovery. Run manage.py restore-verification --yes first.")
    checks, evidence = [], {}

    def check(name, condition, **detail):
        checks.append({"name": name, "passed": bool(condition), **detail})
        save_private(d.path.with_name("verification.json"), {
            "checks": checks, "evidence": evidence, "passed": False, "status": "RUNNING",
        })
        emit("check", name=name, passed=bool(condition), **detail)
        require(condition, "Verification failed: " + name)

    runtime = d.control.get_agent_runtime(agentRuntimeId=d.state["runtime_id"])
    gateway = d.control.get_gateway(gatewayIdentifier=d.state["gateway_id"])
    check("runtime_v2_ready", runtime["status"] == "READY" and runtime["platformVersion"] == "V2")
    check("authorization_header_forwarding",
          runtime.get("requestHeaderConfiguration", {}).get("requestHeaderAllowlist") == ["Authorization"])
    check("policy_enforced", gateway.get("policyEngineConfiguration", {}).get("mode") == "ENFORCE")
    check("upstream_request_response_interceptors",
          sorted(p for i in gateway["interceptorConfigurations"] for p in i["interceptionPoints"]) ==
          ["REQUEST", "RESPONSE"])
    role_policy = d.client("iam").get_role_policy(
        RoleName=d.state["runtime_role_name"], PolicyName="RuntimeV2Execution")["PolicyDocument"]
    actions = [
        action
        for statement in role_policy["Statement"]
        for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])
    ]
    check("agent_cannot_write_budget_or_invoke_bedrock_directly",
          not any(a.startswith(("dynamodb:", "bedrock:", "bedrock-mantle:", "iam:")) for a in actions))
    rate = d.control.get_gateway_rate_limit(
        gatewayIdentifier=d.state["gateway_id"], rateLimitId=d.state["rate_id"])
    check("per_subject_rpm_and_concurrency", rate["entries"] == rate_entries(
        "*", rpm=d.state["config"]["rpm"], concurrency=d.state["config"]["concurrency"]))
    invalid = "invalid.invalid.invalid"
    status, body = http_call(d.state["runtime_url"], invalid,
                            {"task": TASK, "model": "standard"}, runtime=True)
    check("runtime_rejects_invalid_jwt", status in (401, 403), http_status=status)
    status, body = http_call(d.state["gateway_url"] + "/messages", invalid,
                            {"model": d.state["config"]["models"]["standard"]["model_id"],
                             "max_tokens": 16, "messages": [{"role": "user", "content": "Hello"}]})
    check("gateway_rejects_invalid_jwt", status in (401, 403), http_status=status)
    outsider = token(d, "outsider")
    status, body = http_call(d.state["runtime_url"], outsider,
                            {"task": TASK, "model": "standard"}, runtime=True)
    check("runtime_rejects_missing_access_group", status in (401, 403), http_status=status)
    status, body = http_call(d.state["gateway_url"] + "/messages", outsider,
                            {"model": d.state["config"]["models"]["standard"]["model_id"],
                             "max_tokens": 16, "messages": [{"role": "user", "content": "Hello"}]})
    check("gateway_rejects_missing_access_group", status in (401, 403), http_status=status)

    for alias in ("standard", "premium"):
        before = spend(d, "employee")
        status, raw = http_call(d.state["runtime_url"], token(d, "employee"),
                               {"task": TASK, "model": alias}, runtime=True)
        body = unwrap(status, raw)
        evidence["employee_" + alias] = {"http_status": status, "body": raw}
        check(alias + "_agent_completed", status == 200 and body.get("status") == "completed",
              http_status=status)
        check(alias + "_tool_loop", bool(body.get("work_items")) and body.get("model_calls", 0) >= 2,
              model_calls=body.get("model_calls"), work_items=len(body.get("work_items", [])))
        check(alias + "_deliverable", isinstance(body.get("answer"), str) and bool(body["answer"].strip()))
        after = spend(d, "employee")
        first = float(before.get("monthly_spend_usd", {}).get("N", 0))
        last = float(after.get("monthly_spend_usd", {}).get("N", 0))
        check(alias + "_usage_accounted_to_jwt_subject", last > first,
              estimated_cost_delta_usd=last - first)

    status, raw = http_call(d.state["runtime_url"], token(d, "restricted"),
                           {"task": "Say hello in one sentence.", "model": "premium"}, runtime=True)
    evidence["policy_denial"] = {"http_status": status, "body": raw}
    body = unwrap(status, raw)
    check("policy_rejects_unauthorized_model",
          body.get("stop_reason") == "POLICY_DENIED" and body.get("gateway_status") == 403,
          http_status=status)
    status, raw = http_call(d.state["runtime_url"], token(d, "restricted"),
                           {"task": "Say hello in one sentence.", "model": "standard"}, runtime=True)
    evidence["restricted_standard"] = {"http_status": status, "body": raw}
    check("same_user_can_use_authorized_model", status == 200 and raw.get("status") == "completed")

    username = "probe-" + secrets.token_hex(4)
    rate_id = d.state["rate_id"]
    original_entries = rate["entries"]
    # Persist recovery data before changing the owned rate limit.
    save_private(recovery_path, {
        "gateway_id": d.state["gateway_id"], "rate_id": rate_id,
        "entries": original_entries, "probe_username": username,
    })
    try:
        probe = create_user(d, username)
        permit_user(d, username, ["standard"])
        probe_token = token(d, username)
        for kind in ("requests", "connections"):
            entries = rate_entries(probe["sub"], **({"rpm": 0} if kind == "requests" else {"concurrency": 0}))
            set_rate(d, rate_id, original_entries + entries)
            # Rate settings are eventually consistent. This wait is not a load test.
            time.sleep(35)
            status, raw = http_call(d.state["runtime_url"], probe_token,
                                   {"task": "Say hello.", "model": "standard"}, runtime=True)
            evidence[kind + "_denial"] = {"http_status": status, "body": raw}
            body = unwrap(status, raw)
            check("rate_limit_" + kind, body.get("stop_reason") == "RATE_LIMITED"
                  and body.get("gateway_status") == 429, http_status=status)
        set_rate(d, rate_id, original_entries)
        time.sleep(35)
        # Artificially reach 10 USD for a dedicated probe identity. This does not
        # spend 10 USD, lower the configured cap, or modify the employee's budget.
        d.client("dynamodb").put_item(TableName=d.state["spend_table"], Item={
            "pk": {"S": "USER#" + probe["sub"]},
            "daily_cap_usd": {"N": "10"}, "monthly_cap_usd": {"N": "10"},
            "daily_spend_usd": {"N": "10"}, "monthly_spend_usd": {"N": "10"},
        })
        time.sleep(65)  # The upstream REQUEST interceptor caches state for 60s.
        status, raw = http_call(d.state["runtime_url"], probe_token,
                               {"task": "Say hello.", "model": "standard"}, runtime=True)
        evidence["budget_denial"] = {"http_status": status, "body": raw}
        body = unwrap(status, raw)
        check("budget_reached_denies_next_model_call",
              body.get("stop_reason") == "BUDGET_EXCEEDED" and body.get("gateway_status") == 403,
              http_status=status)
        item = spend(d, username)
        check("denied_call_adds_no_inference_cost",
              float(item["monthly_spend_usd"]["N"]) == 10)
    finally:
        restore_verification(d)

    function = d.state["stack_resources"]["ResponseInterceptorFunction"]
    result = d.client("logs").filter_log_events(
        logGroupName="/aws/lambda/" + function,
        startTime=int((time.time() - 3600) * 1000),
        filterPattern='{ $.EstimatedCostUSD > 0 }', limit=10,
    )
    check("cloudwatch_usage_emf", bool(result.get("events")))
    report = {"checks": checks, "evidence": evidence, "passed": True,
              "scope": "Bounded functional AWS checks; rate denials use a zero limit for a dedicated probe."}
    save_private(d.path.with_name("verification.json"), report)
    emit("verification_complete", checks=len(checks), passed=True)
    return report


def run_verification(d=None, *, state_path=None):
    """Archive prior evidence and record failed/interrupted runs explicitly."""
    require(d is not None or state_path is not None, "Select an existing deployment state.")
    path = d.path if d is not None else Path(state_path).resolve()
    report_path = path.with_name("verification.json")
    started_at = datetime.now(timezone.utc).isoformat()
    if report_path.exists():
        archive = report_path.parent / "verification-history" / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4) + ".json")
        save_private(archive, json.loads(report_path.read_text()))
    save_private(report_path, {
        "passed": False, "status": "RUNNING", "started_at": started_at, "checks": [], "evidence": {},
    })
    try:
        if d is None:
            d = Deployment(path)
        report = verify(d)
    except (Exception, KeyboardInterrupt) as error:
        report = json.loads(report_path.read_text())
        report.update(
            passed=False, status="INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "FAILED",
            started_at=started_at, completed_at=datetime.now(timezone.utc).isoformat(),
            error={"type": type(error).__name__, "message": str(error)},
            recovery_pending=path.with_name("verification-rate-recovery.json").exists(),
        )
        save_private(report_path, report)
        raise
    report.update(status="PASSED", started_at=started_at,
                  completed_at=datetime.now(timezone.utc).isoformat(), resources_retained=True)
    save_private(report_path, report)
    return report


if __name__ == "__main__":
    from manage import parser
    p = parser()
    # Use the same global state selection; invocation is managed below.
    import argparse
    args_parser = argparse.ArgumentParser(description=__doc__)
    args_parser.add_argument("--state", default=p.get_default("state"))
    args = args_parser.parse_args()
    run_verification(state_path=args.state)

"""Verify server-side model authorization, or the isolated C/premium fallback path."""
import argparse
from datetime import datetime, timezone
import json

from common import CONTROL, Environment, main_guard, require, write_json
from configure_limits import entries, update_rate
from invoke_user import client_for, request_payload
from gateway_fallback import _limit_key


def probe(env, user, model, *, fallback=False, token=None):
    row = {"user": user, "model": model, "expected_stream": True}
    with client_for(env, fallback=fallback).invoke(
        request_payload(), env.token(user) if token is None else token,
        model=env.state["models"][model]["id"],
    ) as result:
        row.update(status=result.response.status, fallback_used=result.fallback_used,
                   attempts=len(result.attempted_models))
        row["limit_key"] = _limit_key(result.response.body) if result.response.status == 429 else None
        row["complete"] = False
        if result.response.status != 200:
            row["error_body"] = result.response.body.decode("utf-8", errors="replace")
        if result.response.status == 200:
            for chunk in result.iter_chunks():
                event = json.loads(chunk)
                require(event.get("type") != "error", "Provider emitted an error event.")
                row["complete"] |= event.get("type") == "message_stop"
            require(row["complete"], "Incomplete stream.")
    print(json.dumps({k: v for k, v in row.items() if k not in {"limit_key", "error_body"}}), flush=True)
    return row


def verify_access(env, rows):
    env.state["verification"]["access"] = "running"
    env.save()
    require(not any(r["kind"] == "rate" and not r.get("deleted") for r in env.state["resources"]),
            "Run access verification before creating the rate limit.")
    current = env.client(CONTROL).get_gateway(gatewayIdentifier=env.one("gateway")["id"])
    require(current.get("policyEngineConfiguration") == {
        "arn": env.one("engine")["arn"], "mode": "ENFORCE"}, "Dedicated Policy must be in ENFORCE mode.")
    row = probe(env, "a", "standard", token="invalid-verification-token")
    rows.append(row)
    require(row["status"] == 401, "Invalid token was not rejected with HTTP 401.")
    for user in ("a", "b", "c"):
        for model in ("standard", "premium"):
            row = probe(env, user, model)
            rows.append(row)
            expected = 403 if (user, model) == ("b", "premium") else 200
            require(row["status"] == expected, "Authorization mismatch for " + user + "/" + model)
    env.state["verification"]["access"] = "passed"
    env.save()


def verify_limits(env, rows):
    require(env.state["verification"].get("access") == "passed", "Verify access first.")
    rate = env.one("rate")
    actual = env.client(CONTROL).get_gateway_rate_limit(
        gatewayIdentifier=rate["gateway_id"], rateLimitId=rate["id"])
    require(actual["dimensionKeys"] == ["qualifiedModelId", "$.context.jwt.sub"]
            and actual["entries"] == entries(env, env.state["premium_rpm"]),
            "Dedicated rate configuration differs from the saved positive rate.")
    env.state["verified_fallback_limit_keys"] = []
    env.state["rate_restore_required"] = True
    env.save()
    try:
        # Deterministic routing/rejection check, NOT a throughput-accuracy test.
        update_rate(env, 0)
        row = probe(env, "c", "premium")
        rows.append(row)
        require(row["status"] == 429 and row["limit_key"], "Expected dedicated C/premium HTTP 429.")
        key = row["limit_key"]
        # The native response exposes the rate ID, not resolved dimension values.
        # Accept it only after checking its sole entry and negative controls.
        require(key == rate["id"], "429 key does not identify the dedicated rate configuration.")
        for user, model, status in (("c", "standard", 200), ("a", "premium", 200), ("b", "premium", 403)):
            row = probe(env, user, model)
            rows.append(row)
            require(row["status"] == status, "Dedicated limit isolation or authorization failed.")
        env.state["verified_fallback_limit_keys"] = [key]
        row = probe(env, "c", "premium", fallback=True)
        rows.append(row)
        require(row["status"] == 200 and row["fallback_used"] and row["attempts"] == 2,
                "Expected one application fallback and a complete standard-model stream.")
        env.state["verification"]["fallback_zero_rate"] = "passed"
    except Exception:
        env.state["verified_fallback_limit_keys"] = []
        raise
    finally:
        update_rate(env, env.state["premium_rpm"])
        env.state["rate_restore_required"] = False
        env.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limits", action="store_true",
                        help="Temporarily set only the dedicated C/premium rate to zero, verify, then restore.")
    args = parser.parse_args()
    env = Environment()
    rows = []
    report = {"date": datetime.now(timezone.utc).isoformat(), "test": "limits" if args.limits else "access",
              "results": rows, "passed": False}
    try:
        (verify_limits if args.limits else verify_access)(env, rows)
        report["passed"] = True
    finally:
        write_json(env.path.parent / ("verify-" + report["test"] + ".json"), report)
    print(json.dumps({"passed": True, "test": report["test"]}))


if __name__ == "__main__":
    main_guard(main)

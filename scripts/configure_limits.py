"""Set an independent request rate limit for user C and the premium model."""
import argparse
import json
import math
import time

from common import CONTROL, Environment, main_guard, require

PROPAGATION_SECONDS = 31


def entries(env, rpm):
    require(isinstance(rpm, (int, float)) and not isinstance(rpm, bool)
            and math.isfinite(rpm) and rpm >= 0, "Rate must be finite and nonnegative.")
    return [{
        "dimensions": {
            "qualifiedModelId": env.state["models"]["premium"]["id"],
            "$.context.jwt.sub": env.one("user", "c")["sub"],
        },
        "requests": [{"rate": rpm, "period": "minute"}],
    }]


def update_rate(env, rpm):
    row = env.one("rate")
    env.client(CONTROL).update_gateway_rate_limit(
        gatewayIdentifier=row["gateway_id"], rateLimitId=row["id"], entries=entries(env, rpm))
    # Published propagation window: up to 30 seconds, including a deletion.
    print(json.dumps({"rate_updated": rpm, "waiting_seconds": PROPAGATION_SECONDS}), flush=True)
    time.sleep(PROPAGATION_SECONDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--premium-rpm", type=float, required=True)
    args = parser.parse_args()
    require(math.isfinite(args.premium_rpm) and args.premium_rpm > 0,
            "--premium-rpm must be positive. Zero is reserved for the isolated verification.")
    env = Environment()
    require(env.state["verification"].get("access") == "passed",
            "Run scripts/verify_controls.py and resolve authorization failures first.")
    gateway = env.one("gateway")
    if any(r["kind"] == "rate" and not r.get("deleted") for r in env.state["resources"]):
        update_rate(env, args.premium_rpm)
    else:
        name = env.run + "-c-premium"
        env.create(
            "rate", name, CONTROL, "create_gateway_rate_limit",
            lambda r: (r["rateLimitId"], {"gateway_id": gateway["id"]}),
            gatewayIdentifier=gateway["id"], rateLimitId=name,
            description="Dedicated premium-model request limit for test user C",
            dimensionKeys=["qualifiedModelId", "$.context.jwt.sub"],
            entries=entries(env, args.premium_rpm),
        )
        print(json.dumps({"waiting_seconds": PROPAGATION_SECONDS}), flush=True)
        time.sleep(PROPAGATION_SECONDS)
    env.state["premium_rpm"] = args.premium_rpm
    env.state["rate_restore_required"] = False
    env.state["verified_fallback_limit_keys"] = []
    env.save()
    print(json.dumps({"configured": True, "fallback_keys": "Verify the dedicated limit before enabling fallback."}))


if __name__ == "__main__":
    main_guard(main)

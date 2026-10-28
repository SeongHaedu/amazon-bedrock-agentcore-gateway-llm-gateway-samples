"""Invoke as one of the three test users, obtaining a short-lived JWT in memory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway_fallback import FallbackConfig, GatewayClient
from common import Environment, main_guard


def client_for(env, *, fallback=False):
    parsed = urlsplit(env.one("gateway")["url"])
    endpoint = parsed.scheme + "://" + parsed.netloc + "/inference"
    keys = env.state.get("verified_fallback_limit_keys", []) if fallback else []
    return GatewayClient(FallbackConfig(
        endpoint=endpoint, primary_model=env.state["models"]["premium"]["id"],
        fallback_model=env.state["models"]["standard"]["id"],
        allowed_limit_keys=frozenset(keys),
    ))


def request_payload(prompt="Reply with OK.", max_tokens=24):
    return {"anthropic_version": "bedrock-2023-05-31", "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", choices=["a", "b", "c"], required=True)
    parser.add_argument("--model", choices=["standard", "high"], required=True)
    parser.add_argument("--prompt", default="Reply with OK.")
    parser.add_argument("--max-tokens", type=int, default=24)
    parser.add_argument("--fallback", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_tokens <= 4096:
        parser.error("--max-tokens must be between 1 and 4096.")
    env = Environment()
    model = env.state["models"]["premium" if args.model == "high" else "standard"]["id"]
    token = env.token(args.user)
    with client_for(env, fallback=args.fallback).invoke(
        request_payload(args.prompt, args.max_tokens), token, model=model
    ) as result:
        print(json.dumps({"user": args.user, "status": result.response.status,
                          "attempted_models": result.attempted_models,
                          "fallback_used": result.fallback_used}), flush=True)
        if result.response.status != 200:
            print(result.response.body.decode("utf-8", errors="replace"))
            raise SystemExit(1)
        complete = False
        for chunk in result.iter_chunks():
            event = json.loads(chunk)
            print(json.dumps(event), flush=True)
            if event.get("type") == "error":
                raise RuntimeError("Model returned an error event.")
            complete |= event.get("type") == "message_stop"
        if not complete:
            raise RuntimeError("Stream ended without message_stop.")


if __name__ == "__main__":
    main_guard(main)

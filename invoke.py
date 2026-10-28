"""Stream native model events through a configured Gateway using a JWT."""

from __future__ import annotations

import argparse
import getpass
from http.client import HTTPException
import json
import os
from pathlib import Path
import sys

from gateway_fallback import (
    FallbackConfig, GatewayClient, ResponseTooLargeError, StreamError,
)


def required_setting(name: str) -> str:
    value = os.environ.get(name) or ""
    if not value.strip() or "<" in value or ">" in value:
        raise ValueError(f"Set {name} to a real value before invoking the Gateway.")
    return value


def load_limit_keys(path: str | None) -> frozenset[str]:
    if not path:
        return frozenset()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(
        isinstance(key, str) and key.strip() and "<" not in key and ">" not in key
        for key in data
    ):
        raise ValueError("The limit-key file must contain an array of exact, verified keys.")
    return frozenset(data)


def emit(event: dict) -> None:
    """Flush each JSON line so downstream tools receive events immediately."""
    print(json.dumps(event, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, default=Path("examples/request.json"))
    parser.add_argument("--model", choices=("high", "standard"), default="high")
    args = parser.parse_args()
    try:
        config = FallbackConfig(
            endpoint=required_setting("GATEWAY_ENDPOINT"),
            primary_model=required_setting("PRIMARY_MODEL_ID"),
            fallback_model=required_setting("FALLBACK_MODEL_ID"),
            allowed_limit_keys=load_limit_keys(
                os.environ.get("FALLBACK_LIMIT_KEYS_FILE") or None
            ),
        )
        payload = json.loads(args.request.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("The request file must contain a JSON object.")
        selected_model = (
            config.primary_model if args.model == "high" else config.fallback_model
        )
        token = os.environ.get("GATEWAY_BEARER_TOKEN") or getpass.getpass(
            "Gateway JWT (not echoed): "
        )
        with GatewayClient(config).invoke(payload, token, model=selected_model) as result:
            emit(
                {
                    "type": "gateway_response",
                    "requested_model": result.requested_model,
                    "attempted_models": result.attempted_models,
                    "final_requested_model": result.attempted_models[-1],
                    "fallback_used": result.fallback_used,
                    "trigger_limit_key": result.trigger_limit_key,
                    "http_status": result.response.status,
                }
            )
            if result.response.status != 200:
                try:
                    error = json.loads(result.response.body)
                except (ValueError, UnicodeError):
                    error = {"body_format": "non-JSON", "bytes": len(result.response.body)}
                emit({"type": "http_error", "response": error})
                return 1

            reported_model = None
            saw_message_stop = False
            for chunk in result.iter_chunks():
                # Keep native Anthropic thinking, text, and usage events separate.
                event = json.loads(chunk)
                if not isinstance(event, dict):
                    raise ValueError("Expected an Anthropic event object.")
                emit({"type": "model_event", "event": event})
                if event.get("type") == "error":
                    raise StreamError("The model reported a stream error.")
                if event.get("type") == "message_start":
                    message = event.get("message")
                    if isinstance(message, dict):
                        reported_model = message.get("model")
                if event.get("type") == "message_stop":
                    saw_message_stop = True
            if not saw_message_stop:
                raise StreamError("The native stream ended without message_stop.")
            emit({"type": "complete", "response_reported_model": reported_model})
            return 0
    except (ValueError, OSError, EOFError, HTTPException, StreamError, ResponseTooLargeError) as exc:
        # Avoid printing exception details that might contain private input.
        print(
            json.dumps(
                {
                    "type": "client_error",
                    "error": type(exc).__name__,
                    "message": "Check configuration, input, connectivity, and stream completion.",
                }
            ),
            file=sys.stderr,
        )
        return 2

if __name__ == "__main__":
    raise SystemExit(main())

"""Deterministic response fixtures, not an emulator of AWS enforcement."""

from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import struct
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway_fallback import FallbackConfig, GatewayClient, HttpResponse  # noqa: E402

HIGH = "us.anthropic.claude-opus-5-5"
STANDARD = "us.anthropic.claude-haiku-5-5"
C_HIGH_LIMIT = "demo-c-high-limit/qualifiedModelId=demo-high,$.context.jwt.sub=demo-user-c"


def response(status: int, body: dict) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(body).encode())


def event_frame(event: dict) -> bytes:
    """Create an AWS EventStream fixture with prelude and message CRCs."""
    headers = b""
    for name, value in (
        (":message-type", "event"),
        (":event-type", "chunk"),
        (":content-type", "application/json"),
    ):
        name_bytes, value_bytes = name.encode(), value.encode()
        headers += bytes([len(name_bytes)]) + name_bytes
        headers += b"\x07" + struct.pack(">H", len(value_bytes)) + value_bytes
    payload = json.dumps(
        {"bytes": base64.b64encode(json.dumps(event).encode()).decode()}
    ).encode()
    prelude = struct.pack(">II", 16 + len(headers) + len(payload), len(headers))
    message = prelude + struct.pack(">I", zlib.crc32(prelude)) + headers + payload
    return message + struct.pack(">I", zlib.crc32(message))


def streaming_response(model: str) -> HttpResponse:
    events = [
        {"type": "message_start", "message": {"model": model}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}},
        {"type": "message_stop"},
    ]
    return HttpResponse(
        status=200,
        headers={"content-type": "application/vnd.amazon.eventstream"},
        stream=BytesIO(b"".join(event_frame(event) for event in events)),
    )


class FixtureTransport:
    def __init__(self, responses: list[HttpResponse]):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def post(self, url: str, headers: dict, body: bytes, timeout: float) -> HttpResponse:
        self.requests.append({"url": url, "body": body, "headers": headers})
        if not self.responses:
            raise AssertionError("Unexpected extra request.")
        return self.responses.pop(0)


def main() -> None:
    config = FallbackConfig(
        endpoint="https://example.invalid/inference",
        primary_model=HIGH,
        fallback_model=STANDARD,
        allowed_limit_keys=frozenset({C_HIGH_LIMIT}),
    )
    fixtures = [
        ("A: high allowed", "a", HIGH, [streaming_response(HIGH)]),
        ("B: standard allowed", "b", STANDARD, [streaming_response(STANDARD)]),
        ("B: high forbidden", "b", HIGH, [response(403, {"error": {"code": "AccessDenied"}})]),
        ("C: high below limit", "c", HIGH, [streaming_response(HIGH)]),
        (
            "C: high limited, standard succeeds",
            "c",
            HIGH,
            [
                response(429, {"error": {"type": "rate_limit_error", "limitKey": C_HIGH_LIMIT}}),
                streaming_response(STANDARD),
            ],
        ),
        ("C: direct standard", "c", STANDARD, [streaming_response(STANDARD)]),
    ]
    output = []
    for scenario, user, model, replies in fixtures:
        transport = FixtureTransport(replies)
        with GatewayClient(config, transport=transport).invoke(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Hello"}],
            },
            token=f"offline-fixture-{user}",
            model=model,
        ) as result:
            events = (
                [json.loads(chunk) for chunk in result.iter_chunks()]
                if result.response.status == 200 else []
            )
            output.append(
                {
                    "scenario": scenario,
                    "requested_model": result.requested_model,
                    "attempted_models": result.attempted_models,
                    "http_status": result.response.status,
                    "fallback_used": result.fallback_used,
                    "stream_event_count": len(events),
                }
            )
            if result.fallback_used:
                first, second = transport.requests
                assert first["body"] == second["body"]
                assert first["headers"] == second["headers"]
        assert not transport.responses
    print(json.dumps({"mode": "offline_fixtures", "scenarios": output}, indent=2))


if __name__ == "__main__":
    main()

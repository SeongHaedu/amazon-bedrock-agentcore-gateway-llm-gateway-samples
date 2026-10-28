"""Application-side model switching for one dedicated customer rate limit.

This is separate from AWS-managed gateway routing/fallback. Only an HTTP 429
whose limitKey exactly matches the configured allowlist can trigger one new
request. Configure that allowlist from the dedicated customer/model limit;
this client cannot establish the scope of a rate limit from its key alone.

Only native InvokeModelWithResponseStream is supported. Both models must
accept the same native payload schema: this client does not translate provider
bodies. The model is selected in the URI; the identical body bytes are reused.
An HTTP 200 permanently ends fallback eligibility, even if the stream fails
before its first chunk. Event-stream frames are decoded incrementally.
Model checks here validate sample inputs; authentication and authorization
remain the gateway's job. The README distinguishes local tests from the native Gateway verification
conditions, including the isolated zero-rate fallback test.
"""

import base64
from dataclasses import dataclass, field
from http.client import HTTPException
from ipaddress import IPv6Address
import json
import math
import re
import ssl
from typing import BinaryIO, Iterator, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from botocore.eventstream import EventStreamBuffer


INFERENCE_PATH = "/inference"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
STREAM_READ_BYTES = 64 * 1024
EVENTSTREAM_CONTENT_TYPE = "application/vnd.amazon.eventstream"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    # Preserve response data for callers without exposing it through repr().
    body: bytes = field(default=b"", repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    stream: BinaryIO | None = field(default=None, repr=False)

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()


def _validate_endpoint(endpoint: str, *, invocation: bool = False) -> str:
    """Validate HTTPS and its path; normalize only a base's trailing slash."""
    invalid = (
        "URL must be an HTTPS native InvokeModelWithResponseStream URL"
        if invocation
        else "endpoint must be an HTTPS URL ending in /inference or /inference/"
    )
    if (
        not isinstance(endpoint, str)
        or not endpoint.isascii()
        or any(ord(char) <= 32 or ord(char) == 127 for char in endpoint)
        or any(char in endpoint for char in "\\?#")
    ):
        raise ValueError(invalid)
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
        valid_path = (
            re.fullmatch(
                r"/inference/model/(?:[A-Za-z0-9_.~-]|%[0-9A-Fa-f]{2})+/invoke-with-response-stream",
                parsed.path,
            )
            if invocation
            else parsed.path in (INFERENCE_PATH, INFERENCE_PATH + "/")
        )
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or not valid_path
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError(invalid)
        if parsed.netloc.startswith("["):
            closing = parsed.netloc.index("]")
            suffix = parsed.netloc[closing + 1 :]
            if "%" in hostname or (suffix and not re.fullmatch(r":[0-9]+", suffix)):
                raise ValueError(invalid)
            IPv6Address(hostname)
        else:
            if parsed.netloc.endswith(":"):
                raise ValueError(invalid)
            dns_name = hostname.removesuffix(".")
            if len(dns_name) > 253 or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in dns_name.split(".")
            ):
                raise ValueError(invalid)
    except ValueError:
        raise ValueError(invalid) from None
    return endpoint if invocation else endpoint.removesuffix("/")


def _invoke_url(endpoint: str, model: str) -> str:
    return endpoint + "/model/" + quote(model, safe="") + "/invoke-with-response-stream"


@dataclass(frozen=True)
class FallbackConfig:
    endpoint: str
    primary_model: str
    fallback_model: str
    allowed_limit_keys: frozenset[str]
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint", _validate_endpoint(self.endpoint))
        if any(
            not isinstance(model, str) or not model.strip()
            for model in (self.primary_model, self.fallback_model)
        ):
            raise ValueError("model names must be nonempty strings")
        if self.primary_model == self.fallback_model:
            raise ValueError("primary_model and fallback_model must differ")
        if not isinstance(self.allowed_limit_keys, frozenset) or any(
            not isinstance(key, str) or not key.strip()
            for key in self.allowed_limit_keys
        ):
            raise ValueError("allowed_limit_keys must be a frozenset of nonempty strings")
        try:
            valid_timeout = (
                not isinstance(self.timeout_seconds, bool)
                and isinstance(self.timeout_seconds, (int, float))
                and math.isfinite(self.timeout_seconds)
                and self.timeout_seconds > 0
            )
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("timeout_seconds must be finite and positive")


@dataclass(frozen=True)
class FallbackResult:
    requested_model: str
    attempted_models: tuple[str, ...]
    response: HttpResponse
    fallback_used: bool
    trigger_limit_key: str | None

    def __enter__(self) -> "FallbackResult":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.response.close()

    def iter_chunks(self) -> Iterator[bytes]:
        """Yield decoded native chunk bytes; never retry a stream failure.

        CRC/frame validation belongs to EventStreamBuffer. Chunk bytes remain
        provider-native; this method does not interpret their completion markers.
        Use the result as a context manager to close an abandoned iterator.
        """
        try:
            if self.response.status != 200:
                raise ValueError("iter_chunks requires an HTTP 200 response")
            content_types = [
                value.split(";", 1)[0].strip().lower()
                for name, value in self.response.headers.items()
                if name.lower() == "content-type"
            ]
            if content_types != [EVENTSTREAM_CONTENT_TYPE]:
                raise StreamError("HTTP 200 response must use application/vnd.amazon.eventstream")
            if self.response.stream is None:
                raise StreamError("HTTP 200 response has no stream")

            buffer = EventStreamBuffer()
            received_bytes = 0
            parsed_bytes = 0
            received_chunk = False
            try:
                while True:
                    data = self.response.stream.read1(STREAM_READ_BYTES)
                    if not isinstance(data, bytes):
                        raise StreamError("stream read1 must return bytes")
                    if not data:
                        if received_bytes != parsed_bytes:
                            raise StreamError("stream ended in an incomplete event-stream frame")
                        if not received_chunk:
                            raise StreamError("stream ended without a chunk")
                        return
                    received_bytes += len(data)
                    buffer.add_data(data)
                    for message in buffer:
                        parsed_bytes += message.prelude.total_length
                        if (
                            message.headers.get(":message-type") != "event"
                            or message.headers.get(":event-type") != "chunk"
                        ):
                            raise StreamError("event stream contains an error or unknown envelope")
                        document = json.loads(
                            message.payload,
                            object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant,
                        )
                        if (
                            not isinstance(document, dict)
                            or not isinstance(document.get("bytes"), str)
                            or not set(document).issubset({"bytes", "p"})
                            or ("p" in document and not isinstance(document["p"], str))
                        ):
                            raise StreamError("chunk payload must contain a base64 bytes string")
                        # Native Bedrock streams may include string padding in "p".
                        # It is envelope metadata, not part of the model event.
                        chunk = base64.b64decode(document["bytes"], validate=True)
                        received_chunk = True
                        yield chunk
            except StreamError:
                raise
            except Exception:
                # Do not echo provider data or request credentials in errors.
                raise StreamError("invalid or interrupted event stream") from None
        finally:
            self.response.close()


class ResponseTooLargeError(RuntimeError):
    """The HTTP error body exceeded MAX_RESPONSE_BYTES; no retry occurs."""


class StreamError(RuntimeError):
    """An accepted HTTP 200 stream is invalid or interrupted; no retry occurs."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Returning None makes urllib expose the original redirect as HTTPError.
        return None


class _Transport(Protocol):
    def post(
        self, url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> HttpResponse: ...


class UrllibTransport:
    """Verified TLS, no environment proxies, no redirects, and no retries."""

    def __init__(self) -> None:
        self._opener = build_opener(
            ProxyHandler({}),
            HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirect(),
        )

    def post(
        self, url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> HttpResponse:
        _validate_endpoint(url, invocation=True)
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            response = self._opener.open(request, timeout=timeout)
        except HTTPError as error:
            # Includes rejected redirects and all HTTP failure statuses.
            response = error
        if response.code == 200:
            # Transfer ownership without reading any body bytes. The caller's
            # FallbackResult context manager closes this response.
            return HttpResponse(
                status=response.code,
                headers=dict(response.headers.items()),
                stream=response,
            )
        with response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise ResponseTooLargeError("response exceeds MAX_RESPONSE_BYTES")
            # HTTPResponse.read(amt) may silently return fewer bytes than
            # Content-Length on early EOF. A complete JSON prefix is not proof
            # of a complete HTTP error response and must not trigger fallback.
            length_header = response.headers.get("Content-Length")
            if length_header is not None:
                try:
                    if not re.fullmatch(r"[0-9]+", length_header.strip()):
                        raise ValueError
                    expected_length = int(length_header)
                except (TypeError, ValueError):
                    raise HTTPException("invalid HTTP response length") from None
                if len(content) != expected_length:
                    raise HTTPException("incomplete HTTP response body")
            return HttpResponse(
                status=response.code,
                body=content,
                headers=dict(response.headers.items()),
            )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _limit_key(body: bytes) -> str | None:
    """Fail closed for malformed, duplicate, ambiguous, or unknown error shapes."""
    try:
        document = json.loads(
            body, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    error = document.get("error")
    if "error" not in document and document.get("message") == "Rate limit exceeded":
        # Native InvokeModel refusals use message and a rateLimitId-only key.
        # Scope must be verified against the dedicated control-plane config.
        key = document.get("limitKey")
        return key if isinstance(key, str) and key.strip() else None
    if "limitKey" in document:
        # The documented flat HTTP shape has a string error. A top-level key
        # combined with a nested error object is ambiguous, even if keys agree.
        if not isinstance(error, str):
            return None
        key = document["limitKey"]
    elif isinstance(error, dict):
        # Both OpenAI-compatible and Anthropic-compatible shapes use this field.
        key = error.get("limitKey")
    else:
        return None
    return key if isinstance(key, str) and key else None


class GatewayClient:
    def __init__(self, config: FallbackConfig, transport=None) -> None:
        if not isinstance(config, FallbackConfig):
            raise ValueError("config must be a FallbackConfig")
        self._config = config
        self._transport: _Transport = (
            transport if transport is not None else UrllibTransport()
        )

    def invoke(
        self, payload: dict, token: str, *, model: str | None = None
    ) -> FallbackResult:
        config = self._config
        if not isinstance(token, str) or not token or any(
            ord(char) <= 32 or ord(char) >= 127 for char in token
        ):
            raise ValueError("token must be a nonempty ASCII token without whitespace")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        model = config.primary_model if model is None else model
        if not isinstance(model, str) or model not in (
            config.primary_model,
            config.fallback_model,
        ):
            raise ValueError("model must match one of the two configured models")
        if "stream" in payload:
            raise ValueError("omit payload.stream; streaming is selected by the API path")

        # Serialize once to snapshot all nested fields before the first POST.
        # Non-JSON inputs and NaN/Infinity fail before any network request.
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": EVENTSTREAM_CONTENT_TYPE,
        }
        response = self._transport.post(
            _invoke_url(config.endpoint, model),
            headers.copy(),
            body,
            config.timeout_seconds,
        )
        key = None
        if response.status == 429 and model == config.primary_model:
            key = _limit_key(response.body)
        if key is None or key not in config.allowed_limit_keys:
            return FallbackResult(model, (model,), response, False, None)

        response.close()
        # No loops or exception retries: the standard model gets exactly one
        # attempt. Only the URI model changes; reuse the same body bytes/JWT
        # and gateway base. Any model field in the payload remains untouched.
        response = self._transport.post(
            _invoke_url(config.endpoint, config.fallback_model),
            headers.copy(),
            body,
            config.timeout_seconds,
        )
        return FallbackResult(
            model, (model, config.fallback_model), response, True, key
        )

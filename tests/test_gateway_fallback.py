"""Offline unit tests. All model names, tokens, limit keys, and hosts are fixtures."""

import base64
import copy
from dataclasses import FrozenInstanceError, replace
from email.message import Message
from http.client import HTTPException, HTTPResponse
import io
import json
import ssl
import struct
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from urllib.response import addinfourl
import zlib

import gateway_fallback as gateway
from gateway_fallback import (
    FallbackConfig,
    FallbackResult,
    GatewayClient,
    HttpResponse,
    ResponseTooLargeError,
    StreamError,
    UrllibTransport,
)


ENDPOINT = "https://gateway.example.invalid/inference"
PRIMARY = "sample-target/high-performance-model:0"
FALLBACK = "sample-target/standard-model:0"
PRIMARY_URL = ENDPOINT + "/model/sample-target%2Fhigh-performance-model%3A0/invoke-with-response-stream"
FALLBACK_URL = ENDPOINT + "/model/sample-target%2Fstandard-model%3A0/invoke-with-response-stream"
LIMIT_KEY = "dedicated-c-model/tenantId=C,qualifiedModelId=" + PRIMARY
TOKEN = "test-only.jwt.value"


def event_frame(chunk=b"fixture", *, headers=None, raw_payload=None):
    """Encode an AWS event-stream fixture with prelude and message CRC32."""
    if headers is None:
        headers = {":message-type": "event", ":event-type": "chunk"}
    encoded_headers = b""
    for name, value in headers.items():
        name_bytes, value_bytes = name.encode(), value.encode()
        encoded_headers += (
            struct.pack("!B", len(name_bytes))
            + name_bytes
            + b"\x07"  # AWS event-stream string header type.
            + struct.pack("!H", len(value_bytes))
            + value_bytes
        )
    data = (
        json.dumps({"bytes": base64.b64encode(chunk).decode("ascii")}).encode()
        if raw_payload is None
        else raw_payload
    )
    prelude = struct.pack("!II", 16 + len(encoded_headers) + len(data), len(encoded_headers))
    frame = prelude + struct.pack("!I", zlib.crc32(prelude)) + encoded_headers + data
    return frame + struct.pack("!I", zlib.crc32(frame))


def stream_response(*chunks):
    return HttpResponse(
        200,
        headers={"Content-Type": gateway.EVENTSTREAM_CONTENT_TYPE},
        stream=io.BytesIO(b"".join(event_frame(chunk) for chunk in chunks)),
    )


class SegmentedStream:
    """Expose scheduled byte segments or failures, never an unbounded read."""

    def __init__(self, *segments):
        self.segments = list(segments)
        self.closed = False
        self.read_sizes = []
        self.eof_seen = False

    def read1(self, size):
        if self.closed:
            raise ValueError("closed fixture stream")
        self.read_sizes.append(size)
        if not self.segments:
            self.eof_seen = True
            return b""
        segment = self.segments.pop(0)
        if isinstance(segment, Exception):
            raise segment
        if len(segment) > size:
            self.segments.insert(0, segment[size:])
        return segment[:size]

    def read(self, *args):
        raise AssertionError("stream must use bounded read1")

    def close(self):
        self.closed = True


def config(**changes):
    return replace(
        FallbackConfig(ENDPOINT, PRIMARY, FALLBACK, frozenset({LIMIT_KEY})), **changes
    )


def payload():
    return {
        "anthropic_version": "bedrock-2023-05-31",
        "messages": [{"role": "user", "content": "日本語のテスト"}],
        "temperature": 0.2,
        "max_tokens": 128,
        "tools": [
            {
                "name": "lookup",
                "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}},
            }
        ],
        "metadata": {"values": [None, True, 2, {"name": "nested"}]},
    }


def throttled(key=LIMIT_KEY, *, shape="nested"):
    error = {
        "message": "Rate limit exceeded",
        "type": "rate_limit_error",
        "code": "429",
        "limitKey": key,
        "metric": "tokens",
        "retryAfter": 60,
    }
    if shape == "flat":
        document = {"error": "Rate limit exceeded", "limitKey": key}
    elif shape == "anthropic":
        document = {"type": "error", "error": error}
    elif shape == "native":
        document = {"message": "Rate limit exceeded", "limitKey": key,
                    "metric": "requests", "retryAfter": 60.0}
    else:
        document = {"error": error}
    return HttpResponse(429, json.dumps(document).encode(), {"Retry-After": "60"})


class FakeTransport:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, headers, body, timeout):
        self.calls.append((url, headers.copy(), body, timeout))
        if not self.outcomes:
            raise AssertionError("unexpected extra POST")
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class GatewayClientTests(unittest.TestCase):
    def test_primary_success_uses_one_post_and_returns_original_response(self):
        response = stream_response(b'{"text":"first"}')
        transport = FakeTransport(response)
        result = GatewayClient(config(), transport).invoke(payload(), TOKEN)
        self.assertEqual(
            result, FallbackResult(PRIMARY, (PRIMARY,), response, False, None)
        )
        self.assertIs(result.response, response)
        self.assertEqual(len(transport.calls), 1)
        url, headers, body, timeout = transport.calls[0]
        self.assertEqual(url, PRIMARY_URL)
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body), payload())
        self.assertNotIn("model", json.loads(body))
        self.assertEqual(timeout, 30.0)
        with result:
            self.assertEqual(list(result.iter_chunks()), [b'{"text":"first"}'])
        self.assertTrue(response.stream.closed)

    def test_documented_error_shapes_switch_once_and_preserve_request(self):
        for shape in ("flat", "nested", "anthropic", "native"):
            with self.subTest(shape=shape):
                response = stream_response(b'{"text":"first"}', b'{"text":"second"}')
                transport = FakeTransport(throttled(shape=shape), response)
                original = payload()
                before = copy.deepcopy(original)
                result = GatewayClient(config(timeout_seconds=2.5), transport).invoke(
                    original, TOKEN
                )
                self.assertEqual(original, before)
                self.assertEqual(len(transport.calls), 2)
                first, second = transport.calls
                self.assertEqual(first[0], PRIMARY_URL)
                self.assertEqual(second[0], FALLBACK_URL)
                self.assertEqual(urlsplit(first[0]).netloc, urlsplit(second[0]).netloc)
                self.assertEqual(first[0].split("/model/")[0], ENDPOINT)
                self.assertEqual(second[0].split("/model/")[0], ENDPOINT)
                self.assertEqual(first[1], second[1])
                self.assertEqual(second[1]["Authorization"], "Bearer " + TOKEN)
                self.assertEqual(first[3], 2.5)
                self.assertEqual(first[3], second[3])
                self.assertIs(first[2], second[2])
                self.assertEqual(json.loads(second[2]), before)
                self.assertNotIn("model", json.loads(second[2]))
                self.assertIs(result.response, response)
                self.assertEqual(result.requested_model, PRIMARY)
                self.assertEqual(result.attempted_models, (PRIMARY, FALLBACK))
                self.assertTrue(result.fallback_used)
                self.assertEqual(result.trigger_limit_key, LIMIT_KEY)
                with result:
                    self.assertEqual(
                        list(result.iter_chunks()), [b'{"text":"first"}', b'{"text":"second"}']
                    )
                self.assertEqual(len(transport.calls), 2)

    def test_uri_model_is_explicit_and_payload_model_is_never_changed(self):
        for body_model in (FALLBACK, "opaque-provider-field", None, {"nested": "value"}):
            with self.subTest(body_model=body_model):
                original = dict(payload(), model=body_model)
                before = copy.deepcopy(original)
                transport = FakeTransport(throttled(), HttpResponse(200, b"ok"))
                result = GatewayClient(config(), transport).invoke(
                    original, TOKEN, model=PRIMARY
                )
                self.assertEqual(original, before)
                self.assertEqual(result.requested_model, PRIMARY)
                self.assertEqual(transport.calls[0][0], PRIMARY_URL)
                self.assertEqual(transport.calls[1][0], FALLBACK_URL)
                self.assertIs(transport.calls[0][2], transport.calls[1][2])
                self.assertEqual(json.loads(transport.calls[1][2]), before)

    def test_model_uri_component_is_percent_encoded(self):
        fixtures = (
            ("bedrock/anthropic.example-v1:0", "bedrock%2Fanthropic.example-v1%3A0"),
            (
                "arn:fixture:bedrock:region:account:inference-profile/example",
                "arn%3Afixture%3Abedrock%3Aregion%3Aaccount%3Ainference-profile%2Fexample",
            ),
            ("target/model?x=1#fragment", "target%2Fmodel%3Fx%3D1%23fragment"),
            ("target/model%2Fpart", "target%2Fmodel%252Fpart"),
            ("https://elsewhere.invalid/model", "https%3A%2F%2Felsewhere.invalid%2Fmodel"),
        )
        for model, encoded in fixtures:
            with self.subTest(model=model):
                transport = FakeTransport(HttpResponse(200, b"ok"))
                GatewayClient(config(primary_model=model), transport).invoke(payload(), TOKEN)
                url = transport.calls[0][0]
                self.assertEqual(url, ENDPOINT + "/model/" + encoded + "/invoke-with-response-stream")
                self.assertEqual(urlsplit(url).netloc, urlsplit(ENDPOINT).netloc)
                self.assertEqual(urlsplit(url).query, "")
                self.assertEqual(urlsplit(url).fragment, "")
                self.assertEqual(json.loads(transport.calls[0][2]), payload())

    def test_explicit_none_uses_primary_model(self):
        transport = FakeTransport(HttpResponse(200, b"ok"))
        result = GatewayClient(config(), transport).invoke(payload(), TOKEN, model=None)
        self.assertEqual(result.requested_model, PRIMARY)
        self.assertEqual(transport.calls[0][0], PRIMARY_URL)

    def test_snapshot_survives_external_mutation_during_first_post(self):
        original = payload()
        before = copy.deepcopy(original)
        transport = FakeTransport(throttled(), HttpResponse(200, b"ok"))
        original_post = transport.post

        def mutate_after_post(url, headers, body, timeout):
            response = original_post(url, headers, body, timeout)
            original["messages"][0]["content"] = "changed externally"
            headers["Authorization"] = "changed by transport"
            return response

        transport.post = mutate_after_post
        GatewayClient(config(), transport).invoke(original, TOKEN)
        second = transport.calls[1]
        self.assertEqual(json.loads(second[2]), before)
        self.assertIs(transport.calls[0][2], second[2])
        self.assertEqual(second[1]["Authorization"], "Bearer " + TOKEN)

    def test_only_exact_allowlisted_key_switches(self):
        for key in (
            "other-limit",
            LIMIT_KEY + "/suffix",
            LIMIT_KEY[:-1],
            LIMIT_KEY.replace("tenantId=C", "tenantId=D"),
            LIMIT_KEY.replace(PRIMARY, FALLBACK),
            "gateway-wide-common-limit",
            "service-managed-limit",
            "*" + LIMIT_KEY,
            " " + LIMIT_KEY,
        ):
            with self.subTest(key=key):
                response = throttled(key)
                transport = FakeTransport(response)
                result = GatewayClient(config(), transport).invoke(payload(), TOKEN)
                self.assertIs(result.response, response)
                self.assertFalse(result.fallback_used)
                self.assertIsNone(result.trigger_limit_key)
                self.assertEqual(len(transport.calls), 1)

    def test_empty_allowlist_disables_switching(self):
        transport = FakeTransport(throttled())
        result = GatewayClient(config(allowed_limit_keys=frozenset()), transport).invoke(
            payload(), TOKEN
        )
        self.assertFalse(result.fallback_used)
        self.assertEqual(len(transport.calls), 1)

    def test_allowlist_entries_are_not_prefixes_or_wildcards(self):
        for entry in ("dedicated-c-model/", "dedicated-c-model/*", "*", "C"):
            with self.subTest(entry=entry):
                transport = FakeTransport(throttled())
                result = GatewayClient(
                    config(allowed_limit_keys=frozenset({entry})), transport
                ).invoke(payload(), TOKEN)
                self.assertFalse(result.fallback_used)
                self.assertEqual(len(transport.calls), 1)

    def test_non_429_statuses_never_switch_even_with_matching_key(self):
        for status in (200, 301, 302, 307, 308, 400, 401, 403, 408, 500, 502, 503, 504):
            with self.subTest(status=status):
                response = replace(throttled(), status=status)
                transport = FakeTransport(response)
                result = GatewayClient(config(), transport).invoke(payload(), TOKEN)
                self.assertIs(result.response, response)
                self.assertFalse(result.fallback_used)
                self.assertEqual(len(transport.calls), 1)

    def test_malformed_ambiguous_or_unknown_errors_never_switch(self):
        invalid_documents = [
            {},
            [],
            None,
            "rate limited",
            {"limitKey": LIMIT_KEY},
            {"error": "Rate limit exceeded"},
            {"error": {}},
            {"error": None, "limitKey": LIMIT_KEY},
            {"error": 429, "limitKey": LIMIT_KEY},
            {"error": [], "limitKey": LIMIT_KEY},
            {"error": {"data": {"limitKey": LIMIT_KEY}}},
            {"error": {"limitKey": LIMIT_KEY}, "limitKey": LIMIT_KEY},
            {"error": {"limitKey": LIMIT_KEY}, "limitKey": "different"},
            {"error": {"limitKey": LIMIT_KEY}, "limitKey": None},
            {"error": {"limitKey": None}, "limitKey": LIMIT_KEY},
            {"error": {"message": "Rate limit exceeded"}, "limitKey": LIMIT_KEY},
        ]
        for value in (None, 0, 429, False, True, [], {}, "", [LIMIT_KEY]):
            invalid_documents.append({"error": "Rate limit exceeded", "limitKey": value})
            invalid_documents.append({"error": {"limitKey": value}})
        invalid_bodies = [
            b"",
            b"<html>429</html>",
            b'{"error":',
            b"\xff",
            ('{"error":{"limitKey":' + json.dumps(LIMIT_KEY) + '},"error":{}}').encode(),
            ('{"error":{"limitKey":"other","limitKey":' + json.dumps(LIMIT_KEY) + "}}").encode(),
            json.dumps({"error": {"limitKey": LIMIT_KEY}, "number": float("nan")}).encode(),
            b"[" * 2000 + b"]" * 2000,
        ] + [json.dumps(document).encode() for document in invalid_documents]
        for body in invalid_bodies:
            with self.subTest(body=body[:100]):
                response = HttpResponse(429, body)
                transport = FakeTransport(response)
                result = GatewayClient(config(), transport).invoke(payload(), TOKEN)
                self.assertIs(result.response, response)
                self.assertFalse(result.fallback_used)
                self.assertEqual(len(transport.calls), 1)

    def test_standard_model_is_allowed_but_never_falls_back(self):
        for response in (HttpResponse(200, b"ok"), throttled()):
            with self.subTest(status=response.status):
                transport = FakeTransport(response)
                original = dict(payload(), model=PRIMARY)
                result = GatewayClient(config(), transport).invoke(
                    original, TOKEN, model=FALLBACK
                )
                self.assertEqual(result.requested_model, FALLBACK)
                self.assertEqual(result.attempted_models, (FALLBACK,))
                self.assertIs(result.response, response)
                self.assertFalse(result.fallback_used)
                self.assertIsNone(result.trigger_limit_key)
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(transport.calls[0][0], FALLBACK_URL)
                self.assertEqual(json.loads(transport.calls[0][2]), original)

    def test_second_failure_is_returned_without_a_third_post(self):
        for response in (
            throttled(),
            HttpResponse(403, b"forbidden", {"x-request-id": "denied"}),
            HttpResponse(503, b"unavailable"),
        ):
            with self.subTest(status=response.status):
                transport = FakeTransport(throttled(), response)
                result = GatewayClient(config(), transport).invoke(payload(), TOKEN)
                self.assertIs(result.response, response)
                self.assertTrue(result.fallback_used)
                self.assertEqual(result.attempted_models, (PRIMARY, FALLBACK))
                self.assertEqual(result.trigger_limit_key, LIMIT_KEY)
                self.assertEqual(len(transport.calls), 2)

    def test_transport_exceptions_propagate_without_retry(self):
        for failure in (
            URLError("offline fixture"),
            TimeoutError("timeout fixture"),
            ConnectionResetError("reset fixture"),
            ResponseTooLargeError("large fixture"),
        ):
            for attempt in (1, 2):
                with self.subTest(failure=type(failure).__name__, attempt=attempt):
                    outcomes = [failure] if attempt == 1 else [throttled(), failure]
                    transport = FakeTransport(*outcomes)
                    with self.assertRaises(type(failure)) as caught:
                        GatewayClient(config(), transport).invoke(payload(), TOKEN)
                    self.assertIs(caught.exception, failure)
                    self.assertEqual(len(transport.calls), attempt)

    def test_payload_stream_must_be_absent_because_api_selects_streaming(self):
        for stream in (True, False, None, 0, 1, "", "false", [], {}):
            with self.subTest(stream=stream):
                transport = FakeTransport()
                with self.assertRaises(ValueError):
                    GatewayClient(config(), transport).invoke(dict(payload(), stream=stream), TOKEN)
                self.assertEqual(transport.calls, [])
        no_stream = payload()
        transport = FakeTransport(throttled(), HttpResponse(200, b"ok"))
        GatewayClient(config(), transport).invoke(no_stream, TOKEN)
        for call in transport.calls:
            self.assertNotIn("stream", json.loads(call[2]))
            self.assertTrue(call[0].endswith("/invoke-with-response-stream"))

    def test_invalid_models_or_payloads_fail_before_post(self):
        for document in (None, [], "text"):
            with self.subTest(payload=document):
                transport = FakeTransport()
                with self.assertRaises(ValueError):
                    GatewayClient(config(), transport).invoke(document, TOKEN)
                self.assertEqual(transport.calls, [])
        for model in ("not-configured", "", False, 12, [], {}):
            with self.subTest(model=model):
                transport = FakeTransport()
                with self.assertRaises(ValueError):
                    GatewayClient(config(), transport).invoke(payload(), TOKEN, model=model)
                self.assertEqual(transport.calls, [])

    def test_non_json_and_non_finite_values_fail_before_post(self):
        for value in (float("nan"), float("inf"), -float("inf"), object(), {1, 2}):
            with self.subTest(value=type(value).__name__):
                transport = FakeTransport()
                document = dict(payload(), metadata={"nested": [value]})
                with self.assertRaises((ValueError, TypeError)):
                    GatewayClient(config(), transport).invoke(document, TOKEN)
                self.assertEqual(transport.calls, [])
        circular = payload()
        circular["circular"] = circular
        transport = FakeTransport()
        with self.assertRaises(ValueError):
            GatewayClient(config(), transport).invoke(circular, TOKEN)
        self.assertEqual(transport.calls, [])

    def test_invalid_tokens_fail_without_including_token_in_error(self):
        for token in (None, b"bytes", "", " ", "secret\nvalue", "secret\rvalue", "secret\tvalue", "a b", "秘密", "x\x00y", "x\x7fy"):
            with self.subTest(token_type=type(token).__name__):
                transport = FakeTransport()
                with self.assertRaises(ValueError) as caught:
                    GatewayClient(config(), transport).invoke(payload(), token)
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(transport.calls, [])

    def test_credentials_and_response_content_are_not_in_reprs(self):
        response = HttpResponse(200, TOKEN.encode(), {"Authorization": "Bearer " + TOKEN})
        client = GatewayClient(config(), FakeTransport(response))
        result = client.invoke(payload(), TOKEN)
        for value in (client, result, result.response):
            self.assertNotIn(TOKEN, repr(value))
        self.assertEqual(result.response.body, TOKEN.encode())
        self.assertEqual(result.response.headers["Authorization"], "Bearer " + TOKEN)


class StreamingTests(unittest.TestCase):
    def invoke_stream(self, stream, *, after_fallback=False, headers=None):
        response = HttpResponse(
            200,
            headers=(
                {"Content-Type": gateway.EVENTSTREAM_CONTENT_TYPE}
                if headers is None else headers
            ),
            stream=stream,
        )
        transport = FakeTransport(
            *([throttled(), response] if after_fallback else [response])
        )
        result = GatewayClient(config(), transport).invoke(payload(), TOKEN)
        return result, transport

    def test_first_chunk_is_yielded_before_eof_and_next_read(self):
        stream = SegmentedStream(event_frame(b"first"), event_frame(b"second"))
        result, transport = self.invoke_stream(stream)
        self.assertEqual(stream.read_sizes, [])
        with result:
            chunks = result.iter_chunks()
            self.assertEqual(next(chunks), b"first")
            self.assertFalse(stream.eof_seen)
            self.assertEqual(stream.read_sizes, [64 * 1024])
            self.assertEqual(next(chunks), b"second")
            self.assertFalse(stream.eof_seen)
            self.assertEqual(list(chunks), [])
        self.assertTrue(stream.closed)
        self.assertTrue(stream.eof_seen)
        self.assertEqual(len(transport.calls), 1)

    def test_frames_can_be_split_at_any_byte(self):
        wire = event_frame(b"\x00\xffnative bytes") + event_frame(b"") + event_frame(b"last")
        stream = SegmentedStream(*(bytes([value]) for value in wire))
        result, transport = self.invoke_stream(stream)
        with result:
            self.assertEqual(
                list(result.iter_chunks()), [b"\x00\xffnative bytes", b"", b"last"]
            )
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(stream.closed)

    def test_large_frame_is_read_incrementally_without_consuming_later_frames(self):
        expected = b"x" * (3 * 64 * 1024)
        stream = SegmentedStream(event_frame(expected), event_frame(b"later"))
        result, transport = self.invoke_stream(stream)
        with result:
            chunks = result.iter_chunks()
            self.assertEqual(next(chunks), expected)
            self.assertFalse(stream.eof_seen)
            self.assertEqual(len(stream.segments), 1)
            self.assertGreater(len(stream.read_sizes), 1)
            self.assertTrue(all(size == 64 * 1024 for size in stream.read_sizes))
        self.assertTrue(stream.closed)
        self.assertEqual(len(transport.calls), 1)

    def test_http_200_envelope_errors_never_retry_even_before_first_chunk(self):
        envelopes = (
            {":message-type": "exception", ":exception-type": "throttlingException"},
            {":message-type": "error", ":error-code": "429"},
            {":message-type": "event", ":event-type": "unknown"},
            {":message-type": "unknown", ":event-type": "chunk"},
            {":message-type": "event"},
            {},
        )
        for headers in envelopes:
            for after_first_chunk in (False, True):
                for after_fallback in (False, True):
                    with self.subTest(
                        headers=headers,
                        after_first_chunk=after_first_chunk,
                        after_fallback=after_fallback,
                    ):
                        segments = (
                            [event_frame(b"first")] if after_first_chunk else []
                        )
                        segments.append(
                            event_frame(headers=headers, raw_payload=throttled().body)
                        )
                        stream = SegmentedStream(*segments)
                        result, transport = self.invoke_stream(
                            stream, after_fallback=after_fallback
                        )
                        with result:
                            chunks = result.iter_chunks()
                            if after_first_chunk:
                                self.assertEqual(next(chunks), b"first")
                            with self.assertRaises(StreamError):
                                list(chunks)
                        self.assertEqual(len(transport.calls), 2 if after_fallback else 1)
                        self.assertTrue(stream.closed)

    def test_read_failures_after_http_200_never_retry(self):
        for failure in (TimeoutError("timeout"), ConnectionResetError("reset"), OSError("read")):
            for after_first_chunk in (False, True):
                with self.subTest(failure=type(failure).__name__, after_first_chunk=after_first_chunk):
                    segments = [event_frame(b"first")] if after_first_chunk else []
                    stream = SegmentedStream(*segments, failure)
                    result, transport = self.invoke_stream(stream)
                    with result:
                        chunks = result.iter_chunks()
                        if after_first_chunk:
                            self.assertEqual(next(chunks), b"first")
                        with self.assertRaises(StreamError):
                            list(chunks)
                    self.assertEqual(len(transport.calls), 1)
                    self.assertTrue(stream.closed)

    def test_truncated_frames_fail_and_close_without_retry(self):
        frame = event_frame(b"truncated")
        for cut in (1, 8, 11, 12, len(frame) - 1):
            for after_first_chunk in (False, True):
                with self.subTest(cut=cut, after_first_chunk=after_first_chunk):
                    segments = [event_frame(b"first")] if after_first_chunk else []
                    stream = SegmentedStream(*segments, frame[:cut])
                    result, transport = self.invoke_stream(stream)
                    with result:
                        chunks = result.iter_chunks()
                        if after_first_chunk:
                            self.assertEqual(next(chunks), b"first")
                        with self.assertRaisesRegex(StreamError, "incomplete"):
                            list(chunks)
                    self.assertEqual(len(transport.calls), 1)
                    self.assertTrue(stream.closed)

    def test_crc_failures_are_stream_errors_without_retry(self):
        frame = event_frame(b"checksummed")
        for index in (8, len(frame) - 1):
            with self.subTest(corrupt_byte=index):
                corrupt = bytearray(frame)
                corrupt[index] ^= 1
                stream = SegmentedStream(bytes(corrupt))
                result, transport = self.invoke_stream(stream)
                with result:
                    with self.assertRaises(StreamError):
                        list(result.iter_chunks())
                self.assertEqual(len(transport.calls), 1)
                self.assertTrue(stream.closed)

    def test_native_string_padding_is_ignored(self):
        transport = FakeTransport(HttpResponse(
            200, headers={"Content-Type": gateway.EVENTSTREAM_CONTENT_TYPE},
            stream=io.BytesIO(event_frame(raw_payload=b'{"bytes":"Zg==","p":"padding"}'))))
        with GatewayClient(config(), transport).invoke(payload(), TOKEN) as result:
            self.assertEqual(list(result.iter_chunks()), [b"f"])
        self.assertEqual(len(transport.calls), 1)

    def test_invalid_json_or_base64_envelopes_fail_without_retry(self):
        payloads = (
            b"{", b"\xff", b"[]", b"null", b"{}", b'{"bytes":null}', b'{"bytes":12}',
            b'{"bytes":[]}', b'{"bytes":false}', b'{"bytes":"%%%"}', b'{"bytes":"Zg"}',
            b'{"bytes":"Z g=="}', b'{"bytes":"Zg==","extra":1}', b'{"bytes":"Zg==","p":1}',
            b'{"bytes":"Zg==","bytes":"Zw=="}', b'{"bytes":NaN}',
        )
        for body in payloads:
            with self.subTest(body=body):
                stream = SegmentedStream(event_frame(raw_payload=body))
                result, transport = self.invoke_stream(stream, after_fallback=True)
                with result:
                    with self.assertRaises(StreamError):
                        list(result.iter_chunks())
                self.assertEqual(len(transport.calls), 2)
                self.assertTrue(stream.closed)

    def test_http_200_requires_eventstream_content_type_without_reading(self):
        for headers in (
            {}, {"Content-Type": "application/json"}, {"Content-Type": "text/event-stream"},
            {"Content-Type": gateway.EVENTSTREAM_CONTENT_TYPE, "content-type": "application/json"},
        ):
            with self.subTest(headers=headers):
                stream = SegmentedStream(event_frame(b"unused"))
                result, transport = self.invoke_stream(stream, headers=headers)
                with result:
                    with self.assertRaises(StreamError):
                        list(result.iter_chunks())
                self.assertEqual(stream.read_sizes, [])
                self.assertTrue(stream.closed)
                self.assertEqual(len(transport.calls), 1)
        stream = SegmentedStream(event_frame(b"valid"))
        result, _ = self.invoke_stream(
            stream, headers={"content-type": "Application/Vnd.Amazon.Eventstream; charset=utf-8"}
        )
        with result:
            self.assertEqual(list(result.iter_chunks()), [b"valid"])

    def test_empty_or_missing_stream_is_rejected(self):
        for stream in (None, io.BytesIO()):
            with self.subTest(stream_type=type(stream).__name__):
                result, transport = self.invoke_stream(stream)
                with result:
                    with self.assertRaises(StreamError):
                        list(result.iter_chunks())
                self.assertEqual(len(transport.calls), 1)
                if stream is not None:
                    self.assertTrue(stream.closed)

    def test_non_200_iter_chunks_is_rejected_and_body_remains_available(self):
        for response in (HttpResponse(401, b"unauthorized"), HttpResponse(403, b"denied"), throttled()):
            for after_fallback in (False, True):
                with self.subTest(status=response.status, after_fallback=after_fallback):
                    transport = FakeTransport(
                        *([throttled(), response] if after_fallback else [response])
                    )
                    settings = config() if after_fallback else config(allowed_limit_keys=frozenset())
                    with GatewayClient(settings, transport).invoke(payload(), TOKEN) as result:
                        self.assertIs(result.response, response)
                        with self.assertRaises(ValueError):
                            list(result.iter_chunks())
                        self.assertEqual(result.response.body, response.body)
                    self.assertEqual(len(transport.calls), 2 if after_fallback else 1)

    def test_context_exit_closes_on_early_break_and_consumer_exception(self):
        for consumer_failure in (False, True):
            with self.subTest(consumer_failure=consumer_failure):
                stream = SegmentedStream(event_frame(b"first"), event_frame(b"unread"))
                result, transport = self.invoke_stream(stream)
                try:
                    with result:
                        chunks = result.iter_chunks()
                        self.assertEqual(next(chunks), b"first")
                        if consumer_failure:
                            raise RuntimeError("consumer fixture")
                except RuntimeError as error:
                    self.assertTrue(consumer_failure)
                    self.assertEqual(str(error), "consumer fixture")
                self.assertTrue(stream.closed)
                self.assertFalse(stream.eof_seen)
                self.assertEqual(stream.read_sizes, [64 * 1024])
                self.assertEqual(len(transport.calls), 1)
                chunks.close()

    def test_result_closes_even_when_iteration_never_starts(self):
        stream = SegmentedStream(event_frame(b"unread"))
        result, _ = self.invoke_stream(stream)
        with result as entered:
            self.assertIs(entered, result)
        self.assertTrue(stream.closed)
        self.assertEqual(stream.read_sizes, [])

    def test_iterator_close_releases_stream(self):
        stream = SegmentedStream(event_frame(b"first"), event_frame(b"unread"))
        result, _ = self.invoke_stream(stream)
        chunks = result.iter_chunks()
        self.assertEqual(next(chunks), b"first")
        chunks.close()
        self.assertTrue(stream.closed)


class ConfigurationTests(unittest.TestCase):
    def test_valid_endpoints_keep_exact_url(self):
        for base in (
            "https://gateway.example.invalid",
            "https://gateway.example.invalid:443",
            "https://gateway.example.invalid:8443",
            "https://127.0.0.1",
            "https://[::1]:8443",
        ):
            endpoint = base + gateway.INFERENCE_PATH
            with self.subTest(endpoint=endpoint):
                self.assertEqual(config(endpoint=endpoint).endpoint, endpoint)

    def test_base_trailing_slash_is_normalized_for_both_attempts(self):
        settings = config(endpoint=ENDPOINT + "/")
        self.assertEqual(settings.endpoint, ENDPOINT)
        transport = FakeTransport(throttled(), HttpResponse(200, b"ok"))
        GatewayClient(settings, transport).invoke(payload(), TOKEN)
        self.assertEqual([call[0] for call in transport.calls], [PRIMARY_URL, FALLBACK_URL])

    def test_invalid_endpoints(self):
        for endpoint in (
            None, 12, "", "http://gateway.example.invalid" + gateway.INFERENCE_PATH,
            ENDPOINT + "//", ENDPOINT + "?x=1", ENDPOINT + "?", ENDPOINT + "#fragment",
            ENDPOINT + "#", "https://user:secret@gateway.example.invalid" + gateway.INFERENCE_PATH,
            "https://user@gateway.example.invalid" + gateway.INFERENCE_PATH,
            "https:///inference", "//gateway.example.invalid" + gateway.INFERENCE_PATH,
            " " + ENDPOINT, ENDPOINT + "\n", ENDPOINT.replace("gateway", "gate\tway"),
            ENDPOINT.replace("gateway", "gate way"), ENDPOINT.replace("gateway", "gate\\way"),
            ENDPOINT.replace("gateway", "gate%20way"), ENDPOINT.replace("gateway", "gate_way"),
            ENDPOINT.replace("gateway", "-gateway"), ENDPOINT.replace("gateway", "gateway-"),
            ENDPOINT.replace("gateway", "a" * 64), ENDPOINT.replace("example.", "example.."),
            "https://[::1" + gateway.INFERENCE_PATH,
            "https://[::1]extra" + gateway.INFERENCE_PATH,
            "https://[not-ipv6]" + gateway.INFERENCE_PATH,
            "https://gateway.example.invalid:0" + gateway.INFERENCE_PATH,
            "https://gateway.example.invalid:65536" + gateway.INFERENCE_PATH,
            "https://gateway.example.invalid:abc" + gateway.INFERENCE_PATH,
            "https://gateway.example.invalid:" + gateway.INFERENCE_PATH,
            ENDPOINT.replace("/inference", "/elsewhere"),
            ENDPOINT + "/../inference",
            ENDPOINT + "/v1/chat/completions",
            PRIMARY_URL,
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    config(endpoint=endpoint)

    def test_models_allowlist_and_timeout_validation(self):
        invalid = [
            {"primary_model": ""}, {"fallback_model": " "}, {"primary_model": None},
            {"fallback_model": 1}, {"fallback_model": PRIMARY},
            {"allowed_limit_keys": {LIMIT_KEY}}, {"allowed_limit_keys": [LIMIT_KEY]},
            {"allowed_limit_keys": LIMIT_KEY}, {"allowed_limit_keys": None},
            {"allowed_limit_keys": frozenset({""})}, {"allowed_limit_keys": frozenset({" "})},
            {"allowed_limit_keys": frozenset({1})},
        ]
        invalid += [
            {"timeout_seconds": timeout}
            for timeout in (0, -1, float("nan"), float("inf"), -float("inf"), True, None, "30", 10**400)
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    config(**changes)
        self.assertEqual(config(timeout_seconds=1).timeout_seconds, 1)
        self.assertEqual(config(timeout_seconds=0.001).timeout_seconds, 0.001)
        self.assertEqual(config(allowed_limit_keys=frozenset()).allowed_limit_keys, frozenset())

    def test_dataclasses_are_frozen_and_header_defaults_are_independent(self):
        response = HttpResponse(200, b"")
        result = FallbackResult(PRIMARY, (PRIMARY,), response, False, None)
        for value, attribute in ((config(), "endpoint"), (response, "status"), (result, "fallback_used")):
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(value, attribute, None)
        response.headers["one"] = "value"
        self.assertEqual(HttpResponse(200, b"").headers, {})


class UrllibTransportTests(unittest.TestCase):
    @staticmethod
    def wire_response(status=200, body=b'{"content":[]}', **headers):
        message = Message()
        for key, value in headers.items():
            message[key] = value
        response = addinfourl(io.BytesIO(body), message, PRIMARY_URL, status)
        response.msg = "fixture"
        return response

    def test_success_uses_post_and_explicit_timeout(self):
        response = self.wire_response(**{"x-request-id": "fixture-id"})
        opener = Mock()
        opener.open.return_value = response
        with patch.object(gateway, "build_opener", return_value=opener):
            transport = UrllibTransport()
        result = transport.post(
            PRIMARY_URL, {"Authorization": "Bearer " + TOKEN}, b'{"prompt":"fixture"}', 7.5
        )
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, PRIMARY_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(request.data, b'{"prompt":"fixture"}')
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 7.5})
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b"")
        self.assertEqual(result.headers["x-request-id"], "fixture-id")
        self.assertIs(result.stream, response)
        self.assertFalse(response.closed)
        result.close()
        self.assertTrue(response.closed)

    def test_http_200_body_is_not_read_or_subject_to_error_body_limit(self):
        response = self.wire_response(body=b"x" * 17)
        response.read = Mock(side_effect=AssertionError("HTTP 200 body was read"))
        response.read1 = Mock(side_effect=AssertionError("HTTP 200 body was read1"))
        opener = Mock()
        opener.open.return_value = response
        with patch.object(gateway, "build_opener", return_value=opener), patch.object(
            gateway, "MAX_RESPONSE_BYTES", 16
        ):
            result = UrllibTransport().post(PRIMARY_URL, {}, b"{}", 1)
        self.assertIs(result.stream, response)
        self.assertEqual(result.body, b"")
        response.read.assert_not_called()
        response.read1.assert_not_called()
        self.assertFalse(response.closed)
        result.close()
        self.assertTrue(response.closed)

    def test_tls_verification_and_environment_proxy_disabled(self):
        with patch.dict(
            "os.environ",
            {"HTTPS_PROXY": "http://proxy.invalid:9999", "HTTP_PROXY": "http://proxy.invalid:9999"},
        ), patch.object(gateway, "build_opener", wraps=build_opener) as factory:
            transport = UrllibTransport()
        handlers = factory.call_args.args
        proxy = next(handler for handler in handlers if isinstance(handler, ProxyHandler))
        tls = next(handler for handler in handlers if isinstance(handler, HTTPSHandler))
        self.assertEqual(proxy.proxies, {})
        self.assertEqual(tls._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(tls._context.check_hostname)
        self.assertEqual(tls._debuglevel, 0)
        redirects = [
            handler for handler in transport._opener.handlers
            if isinstance(handler, HTTPRedirectHandler)
        ]
        self.assertEqual(len(redirects), 1)
        self.assertIsNone(
            redirects[0].redirect_request(
                Request(PRIMARY_URL), None, 302, "Found", {}, "https://elsewhere.invalid/"
            )
        )

    def test_redirects_return_original_response_without_contacting_destination(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                transport = UrllibTransport()
                response = self.wire_response(
                    status, b"redirect fixture", Location="https://elsewhere.invalid/"
                )
                # Exercise the real urllib error/redirect handler chain, replacing
                # only its I/O handlers. No DNS lookup or socket is used.
                with patch.object(HTTPSHandler, "https_open", return_value=response) as https_open, patch.object(
                    HTTPHandler, "http_open", side_effect=AssertionError("unexpected HTTP request")
                ) as http_open:
                    result = transport.post(
                        PRIMARY_URL, {"Authorization": "Bearer " + TOKEN}, b"{}", 3
                    )
                self.assertEqual(https_open.call_count, 1)
                http_open.assert_not_called()
                self.assertEqual(result.status, status)
                self.assertEqual(result.body, b"redirect fixture")
                self.assertEqual(result.headers["Location"], "https://elsewhere.invalid/")
                self.assertTrue(response.closed)

    def test_http_errors_preserve_status_bytes_and_headers(self):
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status):
                message = Message()
                message["x-request-id"] = "error-id"
                stream = io.BytesIO(b"raw error body")
                error = HTTPError(PRIMARY_URL, status, "fixture", message, stream)
                opener = Mock()
                opener.open.side_effect = error
                with patch.object(gateway, "build_opener", return_value=opener):
                    result = UrllibTransport().post(PRIMARY_URL, {}, b"{}", 1)
                self.assertEqual(result.status, status)
                self.assertEqual(result.body, b"raw error body")
                self.assertEqual(result.headers["x-request-id"], "error-id")
                self.assertTrue(stream.closed)
                opener.open.assert_called_once()

    def test_transport_exceptions_are_not_retried(self):
        for failure in (URLError("offline"), TimeoutError("timeout"), ssl.SSLError("TLS failure")):
            with self.subTest(failure=type(failure).__name__):
                opener = Mock()
                opener.open.side_effect = failure
                with patch.object(gateway, "build_opener", return_value=opener):
                    transport = UrllibTransport()
                with self.assertRaises(type(failure)) as caught:
                    transport.post(PRIMARY_URL, {}, b"{}", 1)
                self.assertIs(caught.exception, failure)
                opener.open.assert_called_once()

    def test_response_size_limit_includes_http_error_bodies(self):
        for status in (403, 429):
            for size in (16, 17):
                with self.subTest(status=status, size=size):
                    response = self.wire_response(status, b"x" * size)
                    opener = Mock()
                    if status == 429:
                        opener.open.side_effect = HTTPError(
                            PRIMARY_URL, status, "fixture", response.headers, response
                        )
                    else:
                        opener.open.return_value = response
                    with patch.object(gateway, "build_opener", return_value=opener), patch.object(
                        gateway, "MAX_RESPONSE_BYTES", 16
                    ):
                        transport = UrllibTransport()
                        if size == 17:
                            with self.assertRaises(ResponseTooLargeError):
                                transport.post(PRIMARY_URL, {}, b"{}", 1)
                        else:
                            self.assertEqual(
                                transport.post(PRIMARY_URL, {}, b"{}", 1).body, b"x" * size
                            )
                    self.assertTrue(response.closed)
                    opener.open.assert_called_once()

    def test_invalid_transport_url_never_reaches_opener(self):
        opener = Mock()
        with patch.object(gateway, "build_opener", return_value=opener):
            transport = UrllibTransport()
        for url in (
            PRIMARY_URL.replace("https://", "http://"),
            PRIMARY_URL.replace("/invoke-with-response-stream", "/invoke"),
            PRIMARY_URL.replace("%2F", "/"),
            PRIMARY_URL.replace("%2F", "%XY"),
            ENDPOINT + "/v1/chat/completions",
            ENDPOINT,
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    transport.post(url, {}, b"{}", 1)
        opener.open.assert_not_called()

    def test_truncated_content_length_429_never_triggers_second_post(self):
        body = json.dumps({"error": {"limitKey": LIMIT_KEY}}).encode()
        raw = (
            b"HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body) + 10}\r\n\r\n".encode()
            + body
        )
        sock = Mock()
        sock.makefile.return_value = io.BytesIO(raw)
        response = HTTPResponse(sock)
        response.begin()
        opener = Mock()
        opener.open.side_effect = [response, self.wire_response()]
        with patch.object(gateway, "build_opener", return_value=opener):
            client = GatewayClient(
                FallbackConfig(ENDPOINT, PRIMARY, FALLBACK, frozenset({LIMIT_KEY}))
            )
        with self.assertRaises(HTTPException):
            client.invoke({"messages": []}, TOKEN)
        opener.open.assert_called_once()
        self.assertTrue(response.closed)

    def test_complete_content_length_429_still_allows_one_fallback(self):
        body = json.dumps({"error": {"limitKey": LIMIT_KEY}}).encode()
        response = self.wire_response(429, body, **{"Content-Length": str(len(body))})
        final = self.wire_response()
        opener = Mock()
        opener.open.side_effect = [response, final]
        with patch.object(gateway, "build_opener", return_value=opener):
            client = GatewayClient(
                FallbackConfig(ENDPOINT, PRIMARY, FALLBACK, frozenset({LIMIT_KEY}))
            )
        with client.invoke({"messages": []}, TOKEN) as result:
            self.assertTrue(result.fallback_used)
            self.assertEqual(result.attempted_models, (PRIMARY, FALLBACK))
        self.assertEqual(opener.open.call_count, 2)
        self.assertTrue(response.closed)
        self.assertTrue(final.closed)


if __name__ == "__main__":
    unittest.main()

"""Offline tests: synthetic prices/JWTs/models, real Botocore CRC parser.

No token here authenticates to a service. Fakes replace DynamoDB only; the SDK
serializer and EventStream parser run unchanged. AWS behavior is not verified.
"""

import base64
import copy
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote
import zlib

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.eventstream import ChecksumMismatch, EventStreamBuffer
from botocore.exceptions import ClientError
from botocore.stub import Stubber

import budget_interceptors as budget


PRICES = {
    "fixture.standard": {
        "input_per_million_usd": "1",
        "output_per_million_usd": "2",
        "cache_read_per_million_usd": "0.1",
        "cache_write_5m_per_million_usd": "1.25",
        "cache_write_1h_per_million_usd": "2",
    },
}
ENV = {
    "BUDGET_TABLE_NAME": "fixture-budget",
    "PRICE_TABLE_JSON": json.dumps(PRICES),
    "DAILY_BUDGET_USD": "1",
    "MONTHLY_BUDGET_USD": "10",
    "ENFORCE_BUDGET": "true",
}
NOW = datetime(2030, 1, 31, 23, 59, tzinfo=timezone.utc)
CONTEXT = SimpleNamespace(client_context=SimpleNamespace(custom={
    "GATEWAY_ARN": "fixture-gateway-identity",
    "REQUEST_ID": "fixture-request",
}))
SERIALIZER = TypeSerializer()
DESERIALIZER = TypeDeserializer()


def encode_item(item):
    return {key: SERIALIZER.serialize(value) for key, value in item.items()}


def decode_item(item):
    return {key: DESERIALIZER.deserialize(value) for key, value in item.items()}


def sdk_error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": "sensitive-error-fixture"}}, operation)


class FakeDynamoDB:
    """Execute the three transaction members on a copy, committing atomically."""

    def __init__(self):
        self.items = {}
        self.calls = []
        self.fail_get = self.fail_put = self.fail_transaction = False
        self.timeout_after_commit = False
        self.before_transaction = None

    @staticmethod
    def key(item):
        return (item["pk"], item["sk"])

    def get_item(self, **kwargs):
        self.calls.append(("get_item", copy.deepcopy(kwargs)))
        assert kwargs["ConsistentRead"] is True
        if self.fail_get:
            raise sdk_error("InternalServerError", "GetItem")
        item = self.items.get(self.key(decode_item(kwargs["Key"])))
        return {} if item is None else {"Item": encode_item(copy.deepcopy(item))}

    def put_item(self, **kwargs):
        self.calls.append(("put_item", copy.deepcopy(kwargs)))
        if self.fail_put:
            raise sdk_error("InternalServerError", "PutItem")
        item = decode_item(kwargs["Item"])
        key = self.key(item)
        assert kwargs["ConditionExpression"] == "attribute_not_exists(pk)"
        if key in self.items:
            raise sdk_error("ConditionalCheckFailedException", "PutItem")
        self.items[key] = item
        return {}

    def transact_write_items(self, **kwargs):
        self.calls.append(("transact_write_items", copy.deepcopy(kwargs)))
        assert len(kwargs["TransactItems"]) == 3
        if self.before_transaction:
            hook, self.before_transaction = self.before_transaction, None
            hook()
        if self.fail_transaction:
            raise sdk_error("TransactionCanceledException", "TransactWriteItems")
        working = copy.deepcopy(self.items)
        for operation in kwargs["TransactItems"]:
            if "Put" in operation:
                put = operation["Put"]
                marker = decode_item(put["Item"])
                key = self.key(marker)
                assert put["ConditionExpression"] == "attribute_not_exists(pk)"
                if key in working:
                    raise sdk_error("TransactionCanceledException", "TransactWriteItems")
                working[key] = marker
            else:
                update = operation["Update"]
                key_doc = decode_item(update["Key"])
                key = self.key(key_doc)
                assert update["UpdateExpression"] == "ADD spend_usd :cost"
                cost = decode_item(update["ExpressionAttributeValues"])[":cost"]
                assert isinstance(cost, Decimal)
                row = working.setdefault(key, key_doc)
                row["spend_usd"] = row.get("spend_usd", Decimal(0)) + cost
        self.items = working
        if self.timeout_after_commit:
            raise TimeoutError("sensitive-timeout-fixture")
        return {}


def token_for(subject="private-subject"):
    # Intentionally unsigned test fixture; Gateway verification is out of scope.
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return ".".join((part({"alg": "fixture"}), part({"sub": subject}), "fixture"))


def request_event(model="sample-target/fixture.standard"):
    return {
        "interceptorInputVersion": "1.0",
        "http": {"gatewayRequest": {
            "path": "/inference/model/" + quote(model, safe="") + "/invoke-with-response-stream",
            "httpMethod": "POST",
            "headers": {"Authorization": "Bearer " + token_for()},
            # This body intentionally has NO model property.
            "body": base64.b64encode(b'{"messages":[{"content":"private-prompt"}]}').decode(),
        }},
    }


def frame(document, *, headers=None, raw_payload=None):
    """Same AWS framing contract as the repository native client fixture."""
    headers = headers if headers is not None else {
        ":message-type": "event", ":event-type": "chunk",
    }
    encoded = b""
    for name, value in headers.items():
        name, value = name.encode(), value.encode()
        encoded += struct.pack("!B", len(name)) + name + b"\x07"
        encoded += struct.pack("!H", len(value)) + value
    payload = raw_payload if raw_payload is not None else json.dumps({
        "bytes": base64.b64encode(json.dumps(document).encode()).decode(),
    }).encode()
    prelude = struct.pack("!II", 16 + len(encoded) + len(payload), len(encoded))
    result = prelude + struct.pack("!I", zlib.crc32(prelude)) + encoded + payload
    return result + struct.pack("!I", zlib.crc32(result))


def padded_frame(document, padding="synthetic-padding"):
    """Synthetic counterpart of the native bytes + p envelope; no live IDs."""
    return frame(document, headers={
        ":message-type": "event", ":event-type": "chunk",
        ":content-type": "application/json",
    }, raw_payload=json.dumps({
        "bytes": base64.b64encode(json.dumps(document).encode()).decode(),
        "p": padding,
    }).encode())


def provider_events(*, cache=False, output=100):
    usage = {"input_tokens": 1000, "output_tokens": 0}
    if cache:
        usage.update({
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 30,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 20,
                "ephemeral_1h_input_tokens": 10,
            },
        })
    return [
        {"type": "message_start", "message": {"model": "fixture.standard", "usage": usage}},
        {"type": "content_block_delta", "delta": {"text": "private-model-output"}},
        {"type": "message_delta", "usage": {"output_tokens": output}},
        {"type": "message_stop"},
    ]


def response_event(events=None, *, raw=None):
    if raw is None:
        raw = b"".join(frame(event) for event in (provider_events() if events is None else events))
    return {
        "interceptorInputVersion": "1.0",
        "http": {"gatewayRequest": None, "gatewayResponse": {
            "statusCode": 200, "headers": None,
            "contentType": budget.EVENTSTREAM_TYPE,
            "body": base64.b64encode(raw).decode(),
        }},
    }


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.ddb = FakeDynamoDB()
        self.output = io.StringIO()
        for patcher in (
            patch.dict("os.environ", ENV, clear=True),
            patch.object(budget, "_client", return_value=self.ddb),
            patch.object(budget, "_now", return_value=NOW),
            patch("sys.stdout", self.output),
            patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
            patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def logs(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def call_request(self, event=None, context=CONTEXT):
        return budget.request_handler(request_event() if event is None else event, context)

    def admit(self, event=None, context=CONTEXT):
        self.assertEqual(self.call_request(event, context), budget._pass())
        return next(item for item in self.ddb.items.values() if item["sk"] == "CONTEXT")

    def assert_denied(self, result, code, status):
        response = result["http"]["transformedGatewayResponse"]
        self.assertEqual(response["statusCode"], status)
        self.assertEqual(json.loads(base64.b64decode(response["body"])), {"error": {"code": code}})

    def seed_spend(self, daily="0", monthly="0"):
        user_pk = "USER#" + budget._hash("fixture-gateway-identity", "private-subject")
        for key, value in (("DAY#2030-01-31", daily), ("MONTH#2030-01", monthly)):
            self.ddb.items[(user_pk, key)] = {
                "pk": user_pk, "sk": key, "spend_usd": Decimal(value),
            }

    def ledger_values(self):
        return {
            item["sk"]: item["spend_usd"]
            for item in self.ddb.items.values() if "spend_usd" in item
        }

    def response(self, event=None, context=CONTEXT):
        event = response_event() if event is None else event
        original = copy.deepcopy(event)
        self.assertEqual(budget.response_handler(event, context), budget._pass())
        self.assertEqual(event, original)
        return self.logs()[-1]

    def test_request_uses_uri_and_never_parses_or_rewrites_body(self):
        event = request_event()
        event["http"]["gatewayRequest"]["body"] = "not-base64-and-not-read"
        original = copy.deepcopy(event)
        item = self.admit(event)
        self.assertEqual(item["request_model"], "sample-target/fixture.standard")
        self.assertEqual(item["price_model"], "fixture.standard")
        self.assertEqual(event, original)
        self.assertNotIn("private-subject", str(self.ddb.items))

    def test_uri_encoding_and_documented_native_target_path(self):
        for path in (
            "/inference/model/sample-target%2Ffixture.standard/invoke-with-response-stream",
            "/inference/model/sample-target%2ffixture.standard/invoke-with-response-stream",
            "/model/fixture.standard/invoke-with-response-stream",
        ):
            with self.subTest(path=path):
                event = request_event()
                event["http"]["gatewayRequest"]["path"] = path
                self.ddb.items.clear()
                self.admit(event)
        self.assertEqual(
            budget._model_from_path("/model/fixture.standard%3A0/invoke-with-response-stream"),
            "fixture.standard:0",
        )

    def test_ambiguous_or_malformed_paths_are_rejected(self):
        for middle in (
            "target/fixture.standard", "target%252Ffixture.standard", "%GG", "%FF",
            "%2Ffixture.standard", "%2E%2E", "fixture.standard%00", "fixture.standard%20",
            "fixture.standard%3Fother", "fixture.standard%23fragment", "target%5Cmodel",
        ):
            with self.subTest(middle=middle):
                event = request_event()
                event["http"]["gatewayRequest"]["path"] = (
                    "/inference/model/" + middle + "/invoke-with-response-stream"
                )
                self.assert_denied(self.call_request(event), "MODEL_PATH_UNSUPPORTED", 400)
        for path in ("/v1/chat/completions", "/target/model/fixture.standard/invoke-with-response-stream"):
            event = request_event()
            event["http"]["gatewayRequest"]["path"] = path
            self.assert_denied(self.call_request(event), "MODEL_PATH_UNSUPPORTED", 400)

    def test_exact_qualified_price_wins_no_arbitrary_prefix_stripping(self):
        prices = {"sample-target/fixture.standard": PRICES["fixture.standard"]}
        with patch.dict("os.environ", {"PRICE_TABLE_JSON": json.dumps(prices)}):
            item = self.admit()
            self.assertEqual(item["price_model"], "sample-target/fixture.standard")
        for model in ("arbitrary/path/fixture.standard", "arn:fixture/resource/fixture.standard"):
            self.assert_denied(self.call_request(request_event(model)), "MODEL_NOT_PRICED", 400)

    def test_missing_price_never_succeeds_even_in_observe_mode(self):
        for mode in ("true", "false"):
            for prices in ({}, {"fixture.other": PRICES["fixture.standard"]}):
                with self.subTest(mode=mode, prices=prices), patch.dict("os.environ", {
                    "ENFORCE_BUDGET": mode, "PRICE_TABLE_JSON": json.dumps(prices),
                }):
                    self.assert_denied(self.call_request(), "MODEL_NOT_PRICED", 400)
        self.assertEqual(self.ddb.items, {})

    def test_invalid_settings_and_implicit_missing_cache_rates_are_closed(self):
        cases = [
            {"ENFORCE_BUDGET": "maybe"}, {"DAILY_BUDGET_USD": "NaN"},
            {"MONTHLY_BUDGET_USD": "-1"}, {"DAILY_BUDGET_USD": "Infinity"},
            {"BUDGET_TABLE_NAME": ""}, {"PRICE_TABLE_JSON": "{"},
            {"PRICE_TABLE_JSON": "[]"},
        ]
        for bad in ("NaN", "-1", True, None):
            prices = copy.deepcopy(PRICES)
            prices["fixture.standard"]["cache_read_per_million_usd"] = bad
            cases.append({"PRICE_TABLE_JSON": json.dumps(prices)})
        prices = copy.deepcopy(PRICES)
        del prices["fixture.standard"]["cache_write_1h_per_million_usd"]
        cases.append({"PRICE_TABLE_JSON": json.dumps(prices)})
        for changes in cases:
            with self.subTest(changes=changes), patch.dict("os.environ", changes):
                self.assert_denied(self.call_request(), "CONFIG_INVALID", 503)
        self.assertEqual(self.ddb.calls, [])

    def test_missing_identity_duplicate_authorization_and_request_context(self):
        invalid_headers = [
            None, {}, {"Authorization": "Bearer broken"},
            {"Authorization": "Bearer " + token_for("")},
            {"Authorization": "Bearer " + token_for(None)},
            {"Authorization": "Bearer " + token_for(), "authorization": "Bearer " + token_for()},
        ]
        for headers in invalid_headers:
            with self.subTest(headers=headers):
                event = request_event()
                event["http"]["gatewayRequest"]["headers"] = headers
                self.assert_denied(self.call_request(event), "IDENTITY_UNAVAILABLE", 401)
        self.assert_denied(self.call_request(context=None), "REQUEST_CONTEXT_INVALID", 503)
        self.assertEqual(self.ddb.items, {})

    def test_daily_and_monthly_exact_boundaries_reject_403(self):
        for daily, monthly in (("1", "1"), ("0", "10"), ("2", "12")):
            with self.subTest(daily=daily, monthly=monthly):
                self.seed_spend(daily, monthly)
                self.assert_denied(self.call_request(), "BUDGET_EXCEEDED", 403)
        self.assertFalse(any(item["sk"] == "CONTEXT" for item in self.ddb.items.values()))

    def test_zero_cap_denies_fresh_user_and_below_caps_is_allowed(self):
        with patch.dict("os.environ", {"DAILY_BUDGET_USD": "0"}):
            self.assert_denied(self.call_request(), "BUDGET_EXCEEDED", 403)
        self.seed_spend("0.999999999", "9.999999999")
        self.admit()

    def test_observe_mode_allows_excess_and_still_accounts(self):
        self.seed_spend("2", "12")
        with patch.dict("os.environ", {"ENFORCE_BUDGET": "false"}):
            self.admit()
        self.assertEqual(self.logs()[-1]["outcome"], "observe_allowed")
        self.response()
        self.assertEqual(self.ledger_values()["DAY#2030-01-31"], Decimal("2.0012"))

    def test_no_request_spend_cache_and_no_reservation(self):
        self.admit()
        self.assertEqual(self.ledger_values(), {})
        self.seed_spend("1", "1")
        self.assert_denied(self.call_request(), "BUDGET_EXCEEDED", 403)
        self.assertEqual(sum(name == "get_item" for name, _ in self.ddb.calls), 4)

    def test_new_utc_period_keys_and_late_response_uses_admission_period(self):
        self.admit()
        later = datetime(2030, 2, 1, tzinfo=timezone.utc)
        next_context = copy.deepcopy(CONTEXT)
        next_context.client_context.custom["REQUEST_ID"] = "next-period"
        with patch.object(budget, "_now", return_value=later):
            self.response()
            self.assertEqual(self.ledger_values()["MONTH#2030-01"], Decimal("0.0012"))
            self.admit(context=next_context)
            self.response(context=next_context)
        self.assertEqual(set(self.ledger_values()), {
            "DAY#2030-01-31", "DAY#2030-02-01", "MONTH#2030-01", "MONTH#2030-02",
        })

    def test_decimal_cost_all_anthropic_cache_categories(self):
        self.admit()
        log = self.response(response_event(provider_events(cache=True)))
        self.assertEqual(log["accounting_status"], "recorded")
        self.assertEqual(self.ledger_values(), {
            "DAY#2030-01-31": Decimal("0.00125"),
            "MONTH#2030-01": Decimal("0.00125"),
        })
        self.assertTrue(all(isinstance(value, Decimal) for value in self.ledger_values().values()))

    def test_cumulative_deltas_are_not_added_together(self):
        self.admit()
        events = provider_events()
        events.insert(2, {"type": "message_delta", "usage": {"output_tokens": 40}})
        self.response(response_event(events))
        self.assertEqual(self.ledger_values()["DAY#2030-01-31"], Decimal("0.0012"))

    def test_fractional_token_prices_remain_readable_in_next_request(self):
        prices = copy.deepcopy(PRICES)
        prices["fixture.standard"]["input_per_million_usd"] = "0.000000000001"
        with patch.dict("os.environ", {"PRICE_TABLE_JSON": json.dumps(prices)}):
            self.admit()
            events = provider_events(output=0)
            events[0]["message"]["usage"]["input_tokens"] = 1
            self.response(response_event(events))
            self.assertEqual(self.ledger_values()["DAY#2030-01-31"], Decimal("1e-18"))
            self.admit()

    def test_response_retries_cannot_double_charge(self):
        self.admit()
        self.response()
        snapshot = copy.deepcopy(self.ddb.items)
        log = self.response()
        self.assertEqual(self.ddb.items, snapshot)
        self.assertEqual(log["accounting_status"], "duplicate")
        self.assertEqual(sum(item["sk"] == "CHARGE" for item in self.ddb.items.values()), 1)

    def test_conflicting_response_with_same_request_id_is_not_charged_again(self):
        self.admit()
        self.response()
        snapshot = copy.deepcopy(self.ddb.items)
        log = self.response(response_event(provider_events(output=200)))
        self.assertEqual(log["code"], "ACCOUNTING_CONFLICT")
        self.assertEqual(log["accounting_status"], "unrecorded")
        self.assertEqual(self.ddb.items, snapshot)

    def test_atomic_marker_protects_competing_response_attempts(self):
        self.admit()
        self.ddb.before_transaction = lambda: self.response()
        self.response()
        self.assertEqual(self.ledger_values()["DAY#2030-01-31"], Decimal("0.0012"))
        self.assertEqual(self.logs()[-1]["accounting_status"], "duplicate")

    def test_uncertain_commit_resolved_by_marker(self):
        self.admit()
        self.ddb.timeout_after_commit = True
        log = self.response()
        self.assertEqual(log["accounting_status"], "duplicate")
        self.assertEqual(self.ledger_values()["DAY#2030-01-31"], Decimal("0.0012"))

    def test_context_retry_keeps_price_snapshot_and_detects_identity_conflict(self):
        item = copy.deepcopy(self.admit())
        replacement = copy.deepcopy(PRICES)
        replacement["fixture.standard"]["input_per_million_usd"] = "99"
        with patch.dict("os.environ", {"PRICE_TABLE_JSON": json.dumps(replacement)}):
            self.admit()
            self.assertEqual(self.ddb.items[(item["pk"], "CONTEXT")], item)
            self.response()
        self.assertEqual(self.ledger_values()["DAY#2030-01-31"], Decimal("0.0012"))
        event = request_event()
        event["http"]["gatewayRequest"]["headers"]["Authorization"] = "Bearer " + token_for("other")
        self.assert_denied(self.call_request(event), "REQUEST_CONTEXT_CONFLICT", 503)

    def test_missing_usage_is_unrecorded_not_free_success(self):
        self.admit()
        cases = [
            [
                {"type": "message_start", "message": {}},
                {"type": "message_stop"},
            ],
            [
                {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}},
                {"type": "message_stop"},
            ],
        ]
        for events in cases:
            with self.subTest(events=events):
                log = self.response(response_event(events))
                self.assertEqual(log["code"], "USAGE_MISSING")
                self.assertEqual(log["accounting_status"], "unrecorded")
        self.assertEqual(self.ledger_values(), {})
        self.assertFalse(any(item["sk"] == "CHARGE" for item in self.ddb.items.values()))

    def test_zero_usage_requires_explicit_complete_usage_and_gets_marker(self):
        self.admit()
        events = provider_events(output=0)
        events[0]["message"]["usage"]["input_tokens"] = 0
        log = self.response(response_event(events))
        self.assertEqual(log["accounting_status"], "recorded")
        self.assertEqual(self.ledger_values()["DAY#2030-01-31"], Decimal(0))
        self.assertEqual(sum(item["sk"] == "CHARGE" for item in self.ddb.items.values()), 1)

    def test_invalid_or_ambiguous_cache_usage_is_not_charged(self):
        self.admit()
        changes = [
            {"input_tokens": -1},
            {"input_tokens": True},
            {"input_tokens": 1.5},
            {"cache_creation_input_tokens": 20},
            {"cache_creation_input_tokens": 99, "cache_creation": {
                "ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 2,
            }},
        ]
        for change in changes:
            events = provider_events()
            events[0]["message"]["usage"].update(change)
            log = self.response(response_event(events))
            self.assertEqual(log["accounting_status"], "unrecorded")
        self.assertEqual(self.ledger_values(), {})

    def test_full_eventstream_frames_are_checked_by_real_botocore(self):
        events = provider_events(cache=True)
        raw = b"".join(frame(event) for event in events)
        parser = EventStreamBuffer()
        parser.add_data(raw)
        parsed = list(parser)
        self.assertEqual(len(parsed), len(events))
        self.assertEqual(sum(message.prelude.total_length for message in parsed), len(raw))
        decoded = [
            json.loads(base64.b64decode(json.loads(message.payload)["bytes"]))
            for message in parsed
        ]
        self.assertEqual(decoded, events)
        counts, _ = budget._native_usage(base64.b64encode(raw).decode(), budget.EVENTSTREAM_TYPE)
        self.assertEqual(counts, (1000, 100, 50, 20, 10))

    def test_native_string_padding_and_final_usage_without_repeated_cache_breakdown(self):
        self.admit()
        initial_usage = {
            "input_tokens": 500, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0,
            },
        }
        events = [
            {"type": "message_start", "message": {"model": "fixture.standard", "usage": initial_usage}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "fixture"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "usage": {
                "input_tokens": 1000, "output_tokens": 100,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            }},
            {"type": "message_stop"},
        ]
        raw = b"".join(padded_frame(event) for event in events)
        log = self.response(response_event(raw=raw))
        self.assertEqual(log["accounting_status"], "recorded")
        self.assertEqual(self.ledger_values()["DAY#2030-01-31"], Decimal("0.0012"))
        # Padding may be an empty string; it never becomes provider content.
        empty_padding = b"".join(padded_frame(event, "") for event in events)
        counts, _ = budget._native_usage(base64.b64encode(empty_padding).decode(), budget.EVENTSTREAM_TYPE)
        self.assertEqual(counts, (1000, 100, 0, 0, 0))
        self.assertNotIn("synthetic-padding", self.output.getvalue())

    def test_native_padding_rejects_non_strings_and_unknown_envelope_keys(self):
        self.admit()
        for padding in (None, 7, True, [], {}):
            with self.subTest(padding_type=type(padding).__name__):
                log = self.response(response_event(raw=padded_frame({}, padding)))
                self.assertEqual(log["code"], "STREAM_ENVELOPE_UNSUPPORTED")
                self.assertEqual(log["accounting_status"], "unrecorded")
        for wrapper in (
            {"bytes": "e30=", "p": "fixture-padding", "unknown": "fixture"},
            {"bytes": "e30=", "unknown": "fixture"},
        ):
            log = self.response(response_event(raw=frame({}, raw_payload=json.dumps(wrapper).encode())))
            self.assertEqual(log["code"], "STREAM_ENVELOPE_UNSUPPORTED")
        self.assertEqual(self.ledger_values(), {})

    def test_crc_corruption_and_trailing_truncation_are_unrecorded(self):
        self.admit()
        raw = b"".join(frame(event) for event in provider_events())
        for offset in (8, len(raw) - 1):
            broken = bytearray(raw)
            broken[offset] ^= 1
            parser = EventStreamBuffer()
            parser.add_data(bytes(broken))
            with self.assertRaises(ChecksumMismatch):
                list(parser)
            self.assertEqual(self.response(response_event(raw=bytes(broken)))["code"], "STREAM_INVALID")
        for broken in (b"", raw[:-1], raw + b"x"):
            self.assertEqual(self.response(response_event(raw=broken))["code"], "STREAM_INCOMPLETE")
        self.assertEqual(self.ledger_values(), {})

    def test_provider_stream_error_or_missing_stop_does_not_record_partial_usage(self):
        self.admit()
        raw = b"".join(frame(event) for event in provider_events()[:-1])
        self.assertEqual(self.response(response_event(raw=raw))["code"], "STREAM_INCOMPLETE")
        raw += frame({}, headers={":message-type": "exception", ":exception-type": "fixture-error"})
        self.assertEqual(self.response(response_event(raw=raw))["code"], "UPSTREAM_STREAM_ERROR")
        self.assertEqual(self.ledger_values(), {})

    def test_payload_filter_unsupported_format_and_upstream_error_remain_passthrough(self):
        self.admit()
        for changes, code in (
            ({"body": None}, "RESPONSE_BODY_UNAVAILABLE"),
            ({"contentType": "text/event-stream"}, "CONTENT_TYPE_UNSUPPORTED"),
            ({"statusCode": 503}, "UPSTREAM_HTTP_NOT_SUCCESS"),
            ({"body": "%invalid"}, "STREAM_INVALID"),
        ):
            event = response_event()
            event["http"]["gatewayResponse"].update(changes)
            self.assertEqual(self.response(event)["code"], code)
        self.assertEqual(self.ledger_values(), {})

    def test_missing_or_invalid_price_snapshot_fails_accounting(self):
        item = self.admit()
        item.pop("rates")
        log = self.response()
        self.assertEqual(log["code"], "PRICE_SNAPSHOT_INVALID")
        self.assertEqual(log["accounting_status"], "unrecorded")
        self.assertEqual(self.ledger_values(), {})

    def test_request_ddb_read_and_context_write_failure_are_closed_in_both_modes(self):
        for mode in ("true", "false"):
            with patch.dict("os.environ", {"ENFORCE_BUDGET": mode}):
                self.ddb.fail_get = True
                self.assert_denied(self.call_request(), "STORAGE_READ_FAILED", 503)
                self.ddb.fail_get = False
                self.ddb.fail_put = True
                self.assert_denied(self.call_request(), "CONTEXT_SAVE_FAILED", 503)
                self.ddb.fail_put = False
        self.assertEqual(self.ddb.items, {})

    def test_response_ddb_read_and_transaction_failure_are_unrecorded(self):
        self.admit()
        self.ddb.fail_get = True
        self.assertEqual(self.response()["code"], "STORAGE_READ_FAILED")
        self.ddb.fail_get = False
        self.ddb.fail_transaction = True
        snapshot = copy.deepcopy(self.ddb.items)
        log = self.response()
        self.assertEqual(log["code"], "ACCOUNTING_WRITE_FAILED")
        self.assertEqual(log["accounting_status"], "unrecorded")
        self.assertEqual(self.ddb.items, snapshot)
        self.ddb.fail_transaction = False
        self.response()
        self.assertEqual(self.ledger_values()["DAY#2030-01-31"], Decimal("0.0012"))

    def test_missing_context_and_unsupported_event_are_safe(self):
        self.assertEqual(self.response()["code"], "ACCOUNTING_CONTEXT_MISSING")
        self.assert_denied(self.call_request({}), "EVENT_SHAPE_UNSUPPORTED", 400)
        self.assertEqual(self.response({})["code"], "EVENT_SHAPE_UNSUPPORTED")

    def test_logs_contain_only_fixed_codes_and_outcomes(self):
        self.admit()
        self.ddb.fail_transaction = True
        self.response()
        self.assert_denied(self.call_request(request_event("private-unpriced-model")), "MODEL_NOT_PRICED", 400)
        output = self.output.getvalue()
        for secret in (
            token_for(), "private-subject", "fixture-gateway-identity", "fixture-request",
            "private-prompt", "private-model-output", "sensitive-error-fixture",
            "private-unpriced-model", "0.0012",
        ):
            self.assertNotIn(secret, output)
        for log in self.logs():
            self.assertEqual(set(log), {
                "component", "phase", "code", "level",
                "accounting_status" if log["phase"] == "response" else "outcome",
            })

    def test_example_prices_are_parseable_synthetic_and_complete(self):
        path = Path(budget.__file__).parent / "examples" / "prices.example.json"
        prices = json.loads(path.read_text())
        self.assertEqual(set(prices), {"fixture.standard", "fixture.premium"})
        for rates in prices.values():
            budget._rates(rates)

    def test_transaction_parameters_pass_real_sdk_validation_without_network(self):
        self.admit()
        self.response()
        captured = [
            args for name, args in self.ddb.calls if name == "transact_write_items"
        ][0]
        # The deliberately fictitious region is for a local SDK stub only.
        client = boto3.client(
            "dynamodb", region_name="fixture-region",
            aws_access_key_id="fixture-key", aws_secret_access_key="fixture-secret",
            endpoint_url="https://dynamodb.example.invalid",
        )
        self.addCleanup(client.close)
        with Stubber(client) as stub:
            stub.add_response("transact_write_items", {}, captured)
            client.transact_write_items(**captured)
            stub.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()

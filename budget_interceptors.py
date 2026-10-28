"""Optional native inference budget interceptors.

Deploy request_handler and response_handler as separate Lambda functions with
the same environment and DynamoDB table. This is a new implementation informed
by the fixed usage-interceptor sample's local compatibility findings; it is not
a claim that the original JSON/SSE implementation supports native EventStream.

Reference contracts:
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-inference-invokemodel.html
Original implementation studied at commit d79bd57f6d5461b81f862630961b72c49967a4f9:
https://github.com/aws-samples/sample-agentcore-gateway-usage-interceptor

Trust/input contract
--------------------
The Gateway MUST validate the JWT (including issuer/audience) before REQUEST,
and REQUEST must have passRequestHeaders=true. We only decode its trusted sub;
we do NOT verify a JWT signature. Do not expose this Lambda directly to callers.
Client context must supply GATEWAY_ARN and REQUEST_ID. No body, JWT, subject,
model, request ID, exception text, or monetary value is logged.

Only the documented HTTP envelope version 1.0 is accepted. Supported paths are
/inference/model/{encodedModel}/invoke-with-response-stream and the native
target operation /model/{encodedModel}/invoke-with-response-stream. URI model
identifiers are decoded exactly once. An exact price key wins; otherwise a
simple target/model identifier may use the model price key. ARN/path identifiers
require an exact price key; arbitrary slash prefixes are never stripped.

RESPONSE expects the ENTIRE buffered application/vnd.amazon.eventstream body
as base64. Botocore validates frames and both CRCs. Only Anthropic Messages
message_start/message_delta/message_stop usage is supported. Output counters
are cumulative, not summed across deltas. Nova/OpenAI/SSE/JSON and incomplete
streams are explicitly unsupported/unaccounted, never zero-cost successes.
Native chunk envelopes accept "bytes" plus optional string padding "p"; other
keys and non-string padding are rejected. Padding is not provider JSON.
Local tests cover parsing and accounting edge cases. The deployed sample also
verified native accounting and zero-cap refusals; see README for the scope.
Other providers, concurrency, and exact spending ceilings are not verified.

Configuration/accounting
-------------------------
Required env: BUDGET_TABLE_NAME, PRICE_TABLE_JSON, DAILY_BUDGET_USD,
MONTHLY_BUDGET_USD, ENFORCE_BUDGET ("true"/"false"). PRICE_TABLE_JSON maps exact
model keys to five decimal USD-per-million-token rates; see prices.example.json.
All five rates must be explicit, finite, and nonnegative, even for unused cache
categories. Its fixture rates are synthetic test values, not current AWS prices.
Rates support at most 12 fractional digits. Real prices are supplied by the
operator. boto3 uses its standard
region/credential chain; no region or account is embedded here.

The table has string pk (partition key) and sk (sort key). USER#<hash> partitions
are scoped to Gateway ARN + sub; DAY#YYYY-MM-DD and MONTH#YYYY-MM hold spend_usd.
REQUEST#<hash> partitions (Gateway ARN + REQUEST_ID) hold CONTEXT and CHARGE.
REQUEST freezes UTC admission day/month and rates; late responses accrue to
those periods. No rollover subtraction, archive job, spend cache, or cost
reservation is used. Already-recorded spend >= either cap rejects with 403
BUDGET_EXCEEDED. In-flight/parallel requests CAN overshoot either cap.

ENFORCE_BUDGET=false observes cap violations but still records priced usage.
It only disables the cap veto: bad config, unknown identity/model/price, or
storage failure STILL fail closed. RESPONSE always passes through unchanged;
accounting failures emit an error code with accounting_status="unrecorded".
These are estimated usage costs, not an AWS billing/reconciliation system.

A transaction creates a CHARGE marker and adds to both counters atomically.
Markers have no TTL: deleting them ends deduplication protection. Context has
no TTL either; any future cleanup must preserve markers and allow late delivery.
Request IDs must be unique per billable invocation and stable across delivery
retries. There is no automatic reconciliation after a failed RESPONSE write.

Lambda table IAM actions: dynamodb:GetItem, dynamodb:PutItem,
dynamodb:UpdateItem. The latter two authorize transaction member operations;
TransactWriteItems is the API, not a separate IAM action. Grant logging
permissions separately. No DeleteItem, Scan, Query, or UpdateTimeToLive needed.
"""

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
import re
from urllib.parse import unquote

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.eventstream import EventStreamBuffer
from botocore.exceptions import ClientError


EVENTSTREAM_TYPE = "application/vnd.amazon.eventstream"
RATE_NAMES = (
    "input_per_million_usd",
    "output_per_million_usd",
    "cache_read_per_million_usd",
    "cache_write_5m_per_million_usd",
    "cache_write_1h_per_million_usd",
)
_CLIENT = None
_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()


class BudgetError(Exception):
    """Only fixed codes and status values may cross the handler boundary."""

    def __init__(self, code, status=503):
        super().__init__(code)
        self.code = code
        self.status = status


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _no_constant(_value):
    raise ValueError("nonfinite JSON value")


def _json_loads(value):
    return json.loads(
        value, object_pairs_hook=_unique_object,
        parse_float=Decimal, parse_constant=_no_constant,
    )


def _decimal(value, code="CONFIG_INVALID"):
    # Booleans/floats and nonfinite values must not enter cost calculations.
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise BudgetError(code)
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise BudgetError(code) from None
    if not number.is_finite() or number < 0:
        raise BudgetError(code)
    # All stored amounts must fit the SDK's DynamoDB number representation.
    try:
        _SERIALIZER.serialize(number)
    except Exception:
        raise BudgetError(code)
    return number


def _rates(value, code="CONFIG_INVALID"):
    if not isinstance(value, dict) or set(value) != set(RATE_NAMES):
        raise BudgetError(code)
    rates = {name: _decimal(value[name], code) for name in RATE_NAMES}
    # Rates and token counters are bounded so their products fit 38 digits.
    if any(rate > Decimal("1e12") or rate.as_tuple().exponent < -12 for rate in rates.values()):
        raise BudgetError(code)
    return rates


@dataclass(frozen=True)
class Settings:
    table: str
    prices: dict
    daily_cap: Decimal
    monthly_cap: Decimal
    enforce: bool

    @classmethod
    def load(cls):
        try:
            mode = os.environ["ENFORCE_BUDGET"].lower()
            if mode not in ("true", "false"):
                raise BudgetError("CONFIG_INVALID")
            prices = _json_loads(os.environ["PRICE_TABLE_JSON"])
            if not isinstance(prices, dict):
                raise BudgetError("CONFIG_INVALID")
            for model, rates in prices.items():
                _valid_model(model)
                _rates(rates)
            return cls(
                _table_name(), prices,
                _decimal(os.environ["DAILY_BUDGET_USD"]),
                _decimal(os.environ["MONTHLY_BUDGET_USD"]),
                mode == "true",
            )
        except BudgetError:
            raise
        except Exception:
            raise BudgetError("CONFIG_INVALID") from None


def _table_name():
    name = os.environ.get("BUDGET_TABLE_NAME", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", name):
        raise BudgetError("CONFIG_INVALID")
    return name


def _now():
    return datetime.now(timezone.utc)


def _client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = boto3.client("dynamodb")
    return _CLIENT


def _item(value):
    return {key: _SERIALIZER.serialize(val) for key, val in value.items()}


def _key(pk, sk):
    return _item({"pk": pk, "sk": sk})


def _read(table, pk, sk):
    try:
        response = _client().get_item(
            TableName=table, Key=_key(pk, sk), ConsistentRead=True,
        )
        item = response.get("Item")
        if item is None:
            return None
        return {key: _DESERIALIZER.deserialize(value) for key, value in item.items()}
    except Exception:
        raise BudgetError("STORAGE_READ_FAILED") from None


def _hash(*parts):
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _request_identity(context):
    try:
        custom = context.client_context.custom
        request_id, gateway = custom["REQUEST_ID"], custom["GATEWAY_ARN"]
        if any(
            not isinstance(value, str) or not value or "\0" in value or len(value) > 2048
            for value in (request_id, gateway)
        ):
            raise ValueError
        return "REQUEST#" + _hash(gateway, request_id), gateway
    except Exception:
        raise BudgetError("REQUEST_CONTEXT_INVALID") from None


def _trusted_subject(headers):
    try:
        values = [
            value for name, value in headers.items()
            if name.lower() == "authorization"
        ]
        if len(values) != 1 or not isinstance(values[0], str):
            raise ValueError
        scheme, token = values[0].split()
        if scheme.lower() != "bearer" or len(token) > 16384:
            raise ValueError
        segments = token.split(".")
        if len(segments) != 3 or any(
            not re.fullmatch(r"[A-Za-z0-9_-]+", segment) for segment in segments
        ):
            raise ValueError
        raw = base64.b64decode(
            segments[1] + "=" * (-len(segments[1]) % 4),
            altchars=b"-_", validate=True,
        )
        subject = _json_loads(raw)["sub"]
        if (
            not isinstance(subject, str) or not subject or len(subject) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in subject)
        ):
            raise ValueError
        return subject
    except Exception:
        raise BudgetError("IDENTITY_UNAVAILABLE", 401) from None


def _valid_model(model):
    if (
        not isinstance(model, str) or not model or len(model) > 2048
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in model)
        or any(char in model for char in "\\?#%")
        or any(part in ("", ".", "..") for part in model.split("/"))
    ):
        raise BudgetError("MODEL_PATH_UNSUPPORTED", 400)
    return model


def _model_from_path(path):
    if not isinstance(path, str):
        raise BudgetError("MODEL_PATH_UNSUPPORTED", 400)
    match = re.fullmatch(
        r"/(?:inference/)?model/([^/]+)/invoke-with-response-stream", path,
    )
    if match is None or re.search(r"%(?![0-9A-Fa-f]{2})", match[1]):
        raise BudgetError("MODEL_PATH_UNSUPPORTED", 400)
    try:
        return _valid_model(unquote(match[1], encoding="utf-8", errors="strict"))
    except UnicodeError:
        raise BudgetError("MODEL_PATH_UNSUPPORTED", 400) from None


def _price_model(model, prices):
    if model in prices:
        return model
    # Only the simple documented target/model form has a removable prefix.
    qualified = re.fullmatch(r"[A-Za-z0-9_-]+/([^/:]+(?::[^/]+)?)", model)
    if qualified and not qualified[1].startswith("arn:") and qualified[1] in prices:
        return qualified[1]
    raise BudgetError("MODEL_NOT_PRICED", 400)


def _http_part(event, name):
    try:
        if event["interceptorInputVersion"] != "1.0":
            raise ValueError
        part = event["http"][name]
        if not isinstance(part, dict):
            raise ValueError
        return part
    except Exception:
        raise BudgetError("EVENT_SHAPE_UNSUPPORTED", 400) from None


def _pass():
    return {"interceptorOutputVersion": "1.0", "http": {}}


def _log(phase, code, outcome):
    # Fixed vocabularies only: never log event/context or caught exception text.
    print(json.dumps({
        "component": "native-budget", "phase": phase, "code": code,
        "level": "error" if outcome in ("blocked", "unrecorded") else "info",
        "accounting_status" if phase == "response" else "outcome": outcome,
    }, separators=(",", ":")))


def _deny(error):
    _log("request", error.code, "blocked")
    payload = json.dumps({"error": {"code": error.code}}, separators=(",", ":"))
    return {
        "interceptorOutputVersion": "1.0",
        "http": {"transformedGatewayResponse": {
            "statusCode": error.status,
            "contentType": "application/json",
            "body": base64.b64encode(payload.encode()).decode("ascii"),
        }},
    }


def _spend(table, user_pk, period):
    item = _read(table, user_pk, period)
    if item is None:
        return Decimal(0)
    if "spend_usd" not in item:
        raise BudgetError("LEDGER_INVALID")
    return _decimal(item["spend_usd"], "LEDGER_INVALID")


def _save_context(table, item):
    try:
        _client().put_item(
            TableName=table, Item=_item(item),
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise BudgetError("CONTEXT_SAVE_FAILED") from None
        existing = _read(table, item["pk"], "CONTEXT")
        if existing is None or any(
            existing.get(name) != item[name] for name in ("user_pk", "request_model")
        ):
            raise BudgetError("REQUEST_CONTEXT_CONFLICT") from None
        # Delivery retries retain the first admission period and price snapshot.
    except Exception:
        raise BudgetError("CONTEXT_SAVE_FAILED") from None


def request_handler(event, context):
    """Authorize against recorded spend; do not reserve future inference cost."""
    try:
        settings = Settings.load()
        request = _http_part(event, "gatewayRequest")
        if request.get("httpMethod") != "POST":
            raise BudgetError("EVENT_SHAPE_UNSUPPORTED", 400)
        request_pk, gateway = _request_identity(context)
        user_pk = "USER#" + _hash(gateway, _trusted_subject(request.get("headers")))
        model = _model_from_path(request.get("path"))
        price_model = _price_model(model, settings.prices)
        now = _now()
        day, month = "DAY#" + now.strftime("%Y-%m-%d"), "MONTH#" + now.strftime("%Y-%m")
        daily = _spend(settings.table, user_pk, day)
        monthly = _spend(settings.table, user_pk, month)
        over_budget = daily >= settings.daily_cap or monthly >= settings.monthly_cap
        if over_budget and settings.enforce:
            raise BudgetError("BUDGET_EXCEEDED", 403)
        _save_context(settings.table, {
            "pk": request_pk, "sk": "CONTEXT", "user_pk": user_pk,
            "request_model": model, "price_model": price_model,
            "rates": _rates(settings.prices[price_model]),
            "day_key": day, "month_key": month,
        })
        _log("request", "BUDGET_EXCEEDED" if over_budget else "BUDGET_CHECK_PASSED",
             "observe_allowed" if over_budget else "allowed")
        return _pass()
    except BudgetError as error:
        return _deny(error)
    except Exception:
        return _deny(BudgetError("REQUEST_INTERNAL_ERROR"))


def _counter(value):
    # Provider token counts must be nonnegative integers; avoid bool-as-int.
    if type(value) is not int or not 0 <= value <= 10**12:
        raise BudgetError("USAGE_INVALID")
    return value


def _usage(document):
    if not isinstance(document, dict):
        raise BudgetError("USAGE_MISSING")
    if "input_tokens" not in document or "output_tokens" not in document:
        raise BudgetError("USAGE_MISSING")
    incoming = _counter(document["input_tokens"])
    outgoing = _counter(document["output_tokens"])
    cache_read = _counter(document.get("cache_read_input_tokens", 0))
    creation = _counter(document.get("cache_creation_input_tokens", 0))
    breakdown = document.get("cache_creation")
    if breakdown is not None:
        if not isinstance(breakdown, dict) or set(breakdown) != {
            "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
        }:
            raise BudgetError("CACHE_USAGE_UNSUPPORTED")
        write_5m = _counter(breakdown["ephemeral_5m_input_tokens"])
        write_1h = _counter(breakdown["ephemeral_1h_input_tokens"])
        if "cache_creation_input_tokens" in document and creation != write_5m + write_1h:
            raise BudgetError("CACHE_USAGE_INVALID")
    else:
        # Do not assume a 5m TTL when only an aggregate write count is supplied.
        if creation:
            raise BudgetError("CACHE_USAGE_AMBIGUOUS")
        write_5m = write_1h = 0
    return (incoming, outgoing, cache_read, write_5m, write_1h)


def _native_usage(body, content_type):
    """CRC-check every frame, then extract one complete Anthropic message."""
    if (
        not isinstance(content_type, str)
        or content_type.split(";", 1)[0].strip().lower() != EVENTSTREAM_TYPE
    ):
        raise BudgetError("CONTENT_TYPE_UNSUPPORTED")
    if body is None:
        raise BudgetError("RESPONSE_BODY_UNAVAILABLE")
    try:
        raw = base64.b64decode(body, validate=True)
        buffer = EventStreamBuffer()
        buffer.add_data(raw)
        parsed_bytes = 0
        usage = {}
        started = stopped = final_usage = False
        for message in buffer:
            parsed_bytes += message.prelude.total_length
            if (
                message.headers.get(":message-type") != "event"
                or message.headers.get(":event-type") != "chunk"
            ):
                raise BudgetError("UPSTREAM_STREAM_ERROR")
            wrapper = _json_loads(message.payload)
            if (
                not isinstance(wrapper, dict) or set(wrapper) not in ({"bytes"}, {"bytes", "p"})
                or not isinstance(wrapper["bytes"], str)
                or ("p" in wrapper and not isinstance(wrapper["p"], str))
            ):
                raise BudgetError("STREAM_ENVELOPE_UNSUPPORTED")
            document = _json_loads(base64.b64decode(wrapper["bytes"], validate=True))
            if not isinstance(document, dict) or stopped:
                raise BudgetError("PROVIDER_EVENT_UNSUPPORTED")
            kind = document.get("type")
            if kind == "message_start" and not started:
                initial = document.get("message")
                if not isinstance(initial, dict):
                    raise BudgetError("PROVIDER_EVENT_UNSUPPORTED")
                initial_usage = initial.get("usage")
                if initial_usage is not None:
                    if not isinstance(initial_usage, dict):
                        raise BudgetError("USAGE_INVALID")
                    usage.update(initial_usage)
                started = True
            elif kind == "message_delta" and started:
                delta_usage = document.get("usage")
                if delta_usage is not None:
                    if not isinstance(delta_usage, dict):
                        raise BudgetError("USAGE_INVALID")
                    # Counters are cumulative; decreasing totals are ambiguous.
                    for name, value in delta_usage.items():
                        if name.endswith("_tokens"):
                            _counter(value)
                            if name in usage and value < _counter(usage[name]):
                                raise BudgetError("USAGE_INVALID")
                    usage.update(delta_usage)
                    final_usage = "output_tokens" in delta_usage or final_usage
            elif kind == "message_stop" and started:
                stopped = True
            elif kind in ("content_block_start", "content_block_delta", "content_block_stop", "ping") and started:
                pass
            else:
                raise BudgetError("PROVIDER_EVENT_UNSUPPORTED")
        if parsed_bytes != len(raw) or not raw:
            raise BudgetError("STREAM_INCOMPLETE")
        if not started or not stopped:
            raise BudgetError("STREAM_INCOMPLETE")
        if not final_usage:
            raise BudgetError("USAGE_MISSING")
        return _usage(usage), hashlib.sha256(raw).hexdigest()
    except BudgetError:
        raise
    except Exception:
        # Includes Botocore prelude/message CRC failures; never print raw errors.
        raise BudgetError("STREAM_INVALID") from None


def _cost(counts, rates):
    with localcontext() as ctx:
        ctx.prec = 38
        return sum(
            (Decimal(count) * rates[name] / Decimal(1_000_000)
             for count, name in zip(counts, RATE_NAMES)),
            Decimal(0),
        )


def _charge(table, context_item, cost, fingerprint):
    pk = context_item["pk"]
    existing = _read(table, pk, "CHARGE")
    if existing:
        if existing.get("body_sha256") == fingerprint and existing.get("cost_usd") == cost:
            return "duplicate"
        raise BudgetError("ACCOUNTING_CONFLICT")
    marker = {
        "pk": pk, "sk": "CHARGE", "body_sha256": fingerprint,
        "cost_usd": cost,
    }
    operations = [{
        "Put": {
            "TableName": table, "Item": _item(marker),
            "ConditionExpression": "attribute_not_exists(pk)",
        },
    }]
    for period in ("day_key", "month_key"):
        operations.append({"Update": {
            "TableName": table,
            "Key": _key(context_item["user_pk"], context_item[period]),
            "UpdateExpression": "ADD spend_usd :cost",
            "ExpressionAttributeValues": _item({":cost": cost}),
        }})
    try:
        _client().transact_write_items(
            TransactItems=operations,
            ClientRequestToken=_hash(pk, fingerprint)[:36],
        )
        return "recorded"
    except Exception:
        # A timeout can follow a successful commit. Strong read resolves it;
        # a different body under the same ID is an error, never another charge.
        existing = _read(table, pk, "CHARGE")
        if existing:
            if existing.get("body_sha256") == fingerprint and existing.get("cost_usd") == cost:
                return "duplicate"
            raise BudgetError("ACCOUNTING_CONFLICT") from None
        raise BudgetError("ACCOUNTING_WRITE_FAILED") from None


def response_handler(event, context):
    """Leave the original model response unchanged, even if accounting fails."""
    try:
        response = _http_part(event, "gatewayResponse")
        status = response.get("statusCode")
        if type(status) is not int or not 100 <= status <= 599:
            raise BudgetError("EVENT_SHAPE_UNSUPPORTED")
        if status != 200:
            raise BudgetError("UPSTREAM_HTTP_NOT_SUCCESS")
        table = _table_name()
        request_pk, _gateway = _request_identity(context)
        item = _read(table, request_pk, "CONTEXT")
        if item is None:
            raise BudgetError("ACCOUNTING_CONTEXT_MISSING")
        if (
            not re.fullmatch(r"USER#[0-9a-f]{64}", item.get("user_pk", ""))
            or not re.fullmatch(r"DAY#\d{4}-\d{2}-\d{2}", item.get("day_key", ""))
            or not re.fullmatch(r"MONTH#\d{4}-\d{2}", item.get("month_key", ""))
            or item.get("pk") != request_pk
        ):
            raise BudgetError("ACCOUNTING_CONTEXT_INVALID")
        rates = _rates(item.get("rates"), "PRICE_SNAPSHOT_INVALID")
        counts, fingerprint = _native_usage(
            response.get("body"), response.get("contentType"),
        )
        outcome = _charge(table, item, _cost(counts, rates), fingerprint)
        _log("response", "USAGE_RECORDED" if outcome == "recorded" else "ALREADY_RECORDED", outcome)
    except BudgetError as error:
        _log("response", error.code, "unrecorded")
    except Exception:
        _log("response", "ACCOUNTING_INTERNAL_ERROR", "unrecorded")
    return _pass()

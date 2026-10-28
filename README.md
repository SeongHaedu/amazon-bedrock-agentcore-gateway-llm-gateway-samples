English | [日本語](README_JA.md)

# Amazon Bedrock AgentCore Gateway: LLM Gateway Samples

> [!NOTE]
> This is a sample for experimentation in a personal AWS environment. It is not an official AWS offering. AWS verification status is listed below.

Use one Amazon Bedrock AgentCore Gateway to authenticate three users, control which models they can call, and apply a dedicated rate limit to one user/model pair. The application streams native model events and makes one request to the standard model when the identified high-performance limit refuses a request.

## Scenario

| User | Standard: Claude Haiku 5.5 | High-performance: Claude Opus 5.5 |
|---|---|---|
| A | Allowed, normal limits | Allowed, normal limits |
| B | Allowed, normal limits | Denied by model authorization |
| C | Allowed, normal limits | Allowed, lower dedicated rate limit |

All three users authenticate through the same Cognito user pool. C can select the standard model directly. When C selects the high-performance model and receives its verified dedicated-limit HTTP 429, the application calls the standard model once. B's authorization failure does not trigger this behavior.

These are model roles for the example. Choose inference profiles available to your account and Region; neither model identifier has a default. “Normal limits” includes applicable shared limits and service quotas.

## Architecture

![Shared model governance through AgentCore Gateway (Japanese)](images/ja/01-governance.png)

![Inbound authentication, Policy, and interceptors (Japanese)](images/ja/01b-auth-controls.png)

The figures describe design responsibilities, not the service's evaluation order. Both README versions use the Japanese figures.

| Responsibility | Sample component |
|---|---|
| Identify the caller | Cognito access token and Gateway JWT inbound authorizer |
| Admit the three principals | AgentCore Policy in `ENFORCE` mode |
| Authorize the requested model | REQUEST interceptor checks the validated JWT subject and exact URI model ID |
| Limit C's high-performance traffic | Dedicated Gateway rate limit using user and model dimensions |
| Call Amazon Bedrock | `bedrock-runtime` inference connector and a dedicated Gateway IAM role |
| Continue after the dedicated refusal | Application-side, one-request fallback |
| Optional daily/monthly budget | Budget check after model authorization in REQUEST; RESPONSE accounting and a usage ledger |

The client uses [native InvokeModelWithResponseStream](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-inference-invokemodel.html):

```text
POST /inference/model/{URL-encoded-model-id}/invoke-with-response-stream
```

`examples/request.json` contains an [Anthropic Messages body](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages.html): `anthropic_version`, `max_tokens`, and `messages`. The URI selects the model and streaming operation. There is no body-level `model` or `stream` field.

[Gateway rate limits run before Gateway rules](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-rate-limits-enforcement.html). A request refused there never reaches the Gateway's fallback rules. The application therefore recognizes the dedicated refusal and sends a new request to the same Gateway, with the same JWT and serialized body. Only the URI's model identifier changes. [Gateway fallback rules](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-rules-fallback.html) separately handle configured destination failures.

## Structure

```text
.
├── scripts/
│   ├── common.py              AWS configuration, resource ledger, test-user authentication
│   ├── setup.py               Cognito users, Gateway role, JWT Gateway, inference connector
│   ├── check_sdk_version.py   Local SDK capability check
│   ├── configure_access.py    Principal Policy and URI-based model authorization
│   ├── native_access.py       Deploy the access-check REQUEST Lambda
│   ├── configure_limits.py    Dedicated C/high-model rate limit
│   ├── invoke_user.py         Authenticate and invoke as A/B/C
│   ├── verify_controls.py     Access matrix and isolated fallback checks
│   ├── configure_budget.py    Optional budget deployment
│   ├── verify_budget.py       Native accounting, budget refusals, and cap restoration
│   └── cleanup.py             Inspect/delete only this run's recorded resources
├── gateway_fallback.py        HTTP client, exact-key fallback, incremental EventStream decoding
├── invoke.py                  Low-level streaming CLI for an existing Gateway and JWT
├── access_interceptor.py      Exact subject/model authorization; optional budget composition
├── budget_interceptors.py     Native request budget check and response usage accounting
├── examples/
│   ├── request.json           Native Anthropic request
│   ├── offline_demo.py        Six scenarios using synthetic responses
│   ├── rate-limit.example.json
│   ├── limit-keys.example.json
│   └── prices.example.json    Price-file format; synthetic prices only
├── tests/                    Local tests using fixtures
├── images/ja/                Japanese diagrams
├── requirements.txt          Python dependencies
└── results/                  Resource ledger and private run output (gitignored)
```

## Prerequisites

- Python 3.11 or later. `requirements.txt` pins Boto3 and Botocore to `1.43.111`.
- AWS CLI and a configured AWS profile. Complete the profile's normal sign-in before running the scripts.
- A Region supporting the features used here and two active, accessible Bedrock inference profiles. Both models must accept the native request body and its features.
- Permissions to create and remove this sample's Cognito, IAM, Gateway, Policy, rate-limit, Lambda, and CloudWatch Logs resources, including passing its execution roles. Optional budgets also use DynamoDB.

AWS setup and invocation create resources and can incur charges. The offline demo uses no AWS resources.

## Setup

```bash
git clone https://github.com/SeongHaedu/amazon-bedrock-agentcore-gateway-llm-gateway-samples.git
cd amazon-bedrock-agentcore-gateway-llm-gateway-samples

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run the remaining commands from the repository root.

### Select the AWS environment

Replace the profile placeholder. Set `AWS_REGION` yourself to override the profile's Region; otherwise the command below reads it from that profile. It stops with an instruction if neither is set.

```bash
export AWS_PROFILE='<your-profile>'
export AWS_REGION="${AWS_REGION:-$(aws configure get region --profile "$AWS_PROFILE")}"
: "${AWS_REGION:?Set AWS_REGION to a Region supported by your selected features and models}"
```

The Python scripts call STS `GetCallerIdentity` and derive the account and partition from its response. Gateway endpoints, resource IDs, and ARNs come from AWS responses and the local ledger. No account ID, endpoint, or ARN needs to be copied into this README or source code.

### Select the two inference profiles

```bash
aws bedrock list-inference-profiles \
  --region "$AWS_REGION" \
  --query 'inferenceProfileSummaries[].[inferenceProfileId,inferenceProfileName,status]' \
  --output json

export PRIMARY_MODEL_ID='<high-performance-inference-profile-id-or-arn>'
export FALLBACK_MODEL_ID='<standard-inference-profile-id-or-arn>'
```

Use the high-performance profile for `PRIMARY_MODEL_ID` (`--model high`) and the standard profile for `FALLBACK_MODEL_ID` (`--model standard`). Select two distinct profiles valid in your environment. Do not assume a geographic prefix works in another Region.

`setup.py` resolves each value with `GetInferenceProfile`, requires `ACTIVE`, and grants model-invocation permissions only for the returned profile and model ARNs. Model-listing actions use a separate statement with `Resource: "*"`. A successful profile lookup alone does not prove that model invocation is authorized.

### Keep the run's resource ledger

The default ledger is `results/resources.json`. To use a separate ledger, set this before setup and keep it set for every later command:

```bash
export GATEWAY_STATE_FILE='results/my-run/resources.json'
```

Setup refuses to overwrite an existing ledger. Later scripts verify that the selected account, partition, and Region match it. Keep the ledger after a failed setup so cleanup can inspect recorded resources and unresolved creation attempts. Shared SDK clients disable automatic retries; their maximum total attempts is one.

## Step 0: Check the SDK

```bash
python scripts/check_sdk_version.py
```

Reads the installed Botocore service model without credentials or AWS calls. It checks the Gateway authorizer, Policy, interceptor, inference-target, and rate-limit definitions required by these scripts. An empty `missing_capabilities` list and exit status `0` indicate success. SDK shape availability is separate from regional service availability and AWS authorization.

## Step 1: Create the Gateway and test users

```bash
python scripts/setup.py
```

Creates an isolated run containing a Cognito user pool, an application client, three test users, a dedicated IAM role and inline policy, a JWT-authenticated Gateway, and a `bedrock-runtime` inference connector target. User creation suppresses invitation messages.

The JWT authorizer accepts the sample application's client ID. The role's trust is narrowed to the newly created Gateway, and setup polls until the Gateway and target are `READY`. The ledger records their identities.

Setup establishes authentication and the inference connection. Complete and verify model authorization before making the Gateway available to other callers.

## Step 2: Configure model authorization

```bash
python scripts/configure_access.py
python scripts/verify_controls.py
```

The configuration uses two controls. An enforcing Policy Engine admits the three recorded principals. A REQUEST Lambda then authorizes the model using the Gateway-validated JWT `sub` and the exact model ID decoded from the native URI: A/C may use both models, and B may use only standard. The deployment generates the allowlist and expected Gateway identity from this run's ledger.

In the native Policy probe, `context.input` existed but `context.input has model` was false. A caller-supplied body `model` is not bound to the model actually selected by the URI. The [Chat Completions policy example](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-action-resource.html) therefore does not establish native model authorization. This sample enforces model access in `access_interceptor.py`; it does not claim that Policy alone enforces the model matrix.

The interceptor accepts only the supported native POST path, checks the expected Gateway context, and compares exact model identifiers. It relies on Gateway JWT validation rather than validating a JWT signature itself. Deployment restricts Lambda invocation to the Gateway and passes request headers to it.

Run the access verifier before creating a rate limit. It sends seven inference-endpoint requests: one invalid-token request expecting HTTP 401, then all six user/model pairs. B/high must return HTTP 403; the other five must return HTTP 200 and a complete stream. Success records `verification.access = "passed"` in the ledger. The report is `verify-access.json` beside that ledger, and a failed check exits nonzero. The next step requires this success record.

## Step 3: Configure C's dedicated rate limit

Choose a positive requests-per-minute value. The following example uses 240; the CLI has no default.

```bash
export PREMIUM_RPM='240'
python scripts/configure_limits.py --premium-rpm "$PREMIUM_RPM"
```

Creates or updates one limit combining C's JWT subject and the selected high-performance model, with `period: "minute"`. It waits 31 seconds for propagation and clears any previously verified fallback keys. The CLI accepts finite positive numbers; zero is reserved for the isolated test in Step 5.

[Rate-limit dimensions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-rate-limits-dimensions.html) include `$.context.jwt.sub` and `qualifiedModelId`. Step 5 checks the actual refusal's complete key against this exact configuration. `examples/rate-limit.example.json` illustrates the SDK input shape; it is not applied by this command.

New or idle limits [can initially admit more than their nominal rate](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-rate-limits-enforcement.html). Setting one request per minute does not establish a strict one-request-per-60-second boundary. A fixed request number is not a reliable throttle test.

## Step 4: Invoke as each user

```bash
python scripts/invoke_user.py --user a --model standard
python scripts/invoke_user.py --user a --model high
python scripts/invoke_user.py --user b --model standard
python scripts/invoke_user.py --user b --model high
python scripts/invoke_user.py --user c --model standard
python scripts/invoke_user.py --user c --model high
```

The wrapper obtains a token for the chosen test user and invokes the Gateway using the saved resource identities. The shared authentication helper generates a fresh password for that test user and obtains an access token with Cognito administrative authentication. The token stays in memory.

B's high-performance invocation should fail authorization. C's high-performance invocation should stream normally below the dedicated limit. The six commands above leave fallback disabled to expose the first request's result.

The wrapper prints an initial user/status/attempt summary followed by native event JSON. It exits `0` after a complete stream and nonzero on a refusal or failure. `--prompt` changes the prompt and `--max-tokens` sets a value from 1 to 4096; its default is 24. Step 5 verifies and enables the fallback path.

The existing `invoke.py` emits JSON Lines, flushing each native model event as it arrives:

| Event | Meaning |
|---|---|
| `gateway_response` | Requested/attempted models, final HTTP status, fallback decision |
| `model_event` | Native Anthropic event, retaining text, thinking, and usage structure |
| `complete` | Stream completed; includes the model reported by `message_start`, if present |
| `http_error` / `client_error` | Final HTTP refusal or input/transport/stream failure |

Its exit status is `0` for a completed stream, `1` for a final non-200 HTTP response, and `2` for input, transport, or stream errors. Partial output can precede failure.

For an existing Gateway, `invoke.py` reads `GATEWAY_ENDPOINT`, both model variables, and an optional `FALLBACK_LIMIT_KEYS_FILE`. It prompts for the JWT without echoing it unless `GATEWAY_BEARER_TOKEN` is set. Without a verified complete-key allowlist, fallback is disabled. `.env.example` is a reference and is not loaded automatically.

## Step 5: Verify the controls on AWS

```bash
python scripts/verify_controls.py --limits
```

Run this after Step 2 succeeds for invalid-token rejection and all six user/model pairs. This command verifies isolation and fallback by temporarily changing only C's dedicated high-performance rate to zero. It first checks that the saved positive rate matches the actual dedicated configuration.

| Check while the dedicated rate is zero | Expected behavior |
|---|---|
| C/high | HTTP 429 with the complete key for this exact user/model limit |
| C/standard | HTTP 200 and a complete stream |
| A/high | HTTP 200 and a complete stream |
| B/high | HTTP 403 |
| C/high with application fallback | HTTP 200, one standard-model fallback, exactly two attempts, and a complete stream |

The verifier waits 31 seconds after setting zero and restores the original positive rate in `finally`, followed by another 31-second propagation wait. This is an isolation and switching test, not a sustained-load accuracy test at a positive rate. It makes five probes, totaling six inference-endpoint requests because fallback uses two requests.

In the native response, `limitKey` is the rate identifier alone. The verifier first checks that this rate has exactly the dedicated C/model entry, checks the refusal and isolation controls, and registers the exact returned key in `verified_fallback_limit_keys`. Do not reuse that rate identifier for shared limits; reverify after changing its scope. No manual key copying is needed. The report is `verify-limits.json` beside the ledger. A failed check clears the fallback keys.

After verification succeeds, enable fallback for an interactive invocation:

```bash
python scripts/invoke_user.py --user c --model high --fallback
```

The restored positive rate applies to this request, so it switches only if that dedicated limit actually refuses it. `--fallback` without a matching verified key never switches models.

If interruption or restoration failure leaves `rate_restore_required` in the ledger set to `true`, restore the original positive rate before continuing, or clean up the run:

```bash
python scripts/configure_limits.py --premium-rpm "$PREMIUM_RPM"
```

Use the original value selected in Step 3. Reconfiguration clears verified keys, so run `verify_controls.py --limits` again before using fallback. Keep ledgers and reports in the ignored `results/` directory; they can contain environment identifiers.

## Step 6 (optional): Add daily and monthly budgets

![Response-time accounting and concurrent budget overshoot (Japanese)](images/ja/05-budget-timeline.png)

Rate limits count short-term traffic. Monetary budgets require usage, prices, and accumulated spending. The optional design checks a user's existing daily/monthly total in REQUEST and adds priced usage after the model response in RESPONSE.

The [original public budget sample at the referenced revision](https://github.com/aws-samples/sample-agentcore-gateway-usage-interceptor/tree/d79bd57f6d5461b81f862630961b72c49967a4f9) cannot be used unchanged for this native URI/EventStream path. Six local compatibility cases confirmed that limitation. This repository provides a separate native adapter.

Create `local/prices.json` by entering the actual applicable prices for both selected models. The command reads model IDs from the ledger and asks for all five per-million-token USD rates: input, output, cache read, five-minute cache write, and one-hour cache write. It refuses to overwrite an existing file.

```bash
python -c '
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

path = Path("local/prices.json")
if path.exists():
    raise SystemExit("local/prices.json already exists; review and edit it explicitly.")
state_path = Path(os.environ.get("GATEWAY_STATE_FILE") or "results/resources.json")
state = json.loads(state_path.read_text())
fields = (
    "input_per_million_usd",
    "output_per_million_usd",
    "cache_read_per_million_usd",
    "cache_write_5m_per_million_usd",
    "cache_write_1h_per_million_usd",
)
prices = {}
for label in ("standard", "premium"):
    model_id = state["models"][label]["id"]
    rates = {}
    print(f"Enter applicable prices for {label}: {model_id}")
    for field in fields:
        raw = input(f"{field} (USD per million tokens): ").strip()
        try:
            value = Decimal(raw)
        except InvalidOperation:
            raise SystemExit(f"Invalid decimal for {field}")
        if (not value.is_finite() or value < 0 or value > Decimal("1e12")
                or value.as_tuple().exponent < -12):
            raise SystemExit("Use a finite nonnegative rate with at most 12 decimal places.")
        rates[field] = str(value)
    prices[model_id] = rates
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("x", encoding="utf-8") as output:
    json.dump(prices, output, indent=2)
    output.write("\n")
path.chmod(0o600)
print("Created local/prices.json")
'
```

`examples/prices.example.json` contains synthetic fixture rates, not actual prices. Do not copy them as billing rates or use zero to represent an unknown price. The adapter requires all five finite, nonnegative rates, including unused categories.

```bash
export DAILY_BUDGET_USD='<daily-decimal-usd>'
export MONTHLY_BUDGET_USD='<monthly-decimal-usd>'
python scripts/configure_budget.py \
  --prices local/prices.json \
  --daily "$DAILY_BUDGET_USD" \
  --monthly "$MONTHLY_BUDGET_USD"
```

The deployment keeps the existing `access-request` Lambda as the REQUEST interceptor. Its handler authorizes the exact user/model first, then calls the budget REQUEST handler when `ENABLE_BUDGET=true`. It preserves `MODEL_ACCESS_JSON` and `EXPECTED_GATEWAY_ARN`, and adds a budget RESPONSE Lambda and DynamoDB accounting. Enabling budgets does not replace model authorization.

Add `--observe` to the deployment command to record priced usage without refusing requests solely for exceeding a budget cap. Model authorization and invalid configuration, identity, price, or storage checks still apply. The default enforces the caps.

Choose this mode before the first budget deployment. The script refuses another attempt when a budget attempt is already recorded; it does not automatically resume or roll back a partial activation. Keep the ledger and inspect its recorded stage and mutations if deployment fails, or clean up the run. Run Gateway configuration commands sequentially.

The Lambda ZIP contains the access and budget modules. Its Python 3.12 runtime supplies Boto3/Botocore; the local versions pinned in `requirements.txt` apply to the deployment environment.

[HTTP/inference RESPONSE interceptors buffer the entire response](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html#gateway-interceptors-types-http). Adding this step changes the streaming experience: the client may still parse events, but the Gateway waits for the complete response before the interceptor processes it. Do not use the base client's incremental decoding as evidence of live token delivery with this option enabled.

The adapter associates requests with the Gateway and JWT subject, fixes the admission day's UTC date/month and price table, and records daily/monthly model-cost estimates. RESPONSE supports complete native Anthropic Messages EventStream usage. Missing, incomplete, or unsupported usage is unaccounted usage, not zero cost; a response-accounting failure leaves the model response unchanged and reports an unrecorded accounting status.

The timeline illustrates response-time accounting, not a reserved-spend mechanism. Concurrent or already admitted requests can exceed a cap. A budget refusal returns HTTP 403 with `BUDGET_EXCEEDED`; it must not trigger fallback. Model-price estimates are separate from AWS billing and infrastructure charges.

In enforcement mode, verify one native response is charged to both periods, then temporarily set the REQUEST daily cap to zero to verify budget rejection and retained model authorization:

```bash
python scripts/verify_budget.py
```

Use positive original caps and sufficient remaining budget for A's initial standard-model request. The test makes four requests: one successful A/standard call, then A/standard and C/high budget refusals and a B/high model-authorization refusal. No refusal may trigger fallback. The original REQUEST environment is restored in `finally`; the report is `verify-budget.json` beside the ledger. This checks post-response accounting and a zero-cap refusal, not exact spending limits under concurrency. If interrupted with `budget_restore_variables` still in the ledger, retain the file and clean up the run before starting over.

## Step 7: Clean up

Use the same AWS profile, Region, and `GATEWAY_STATE_FILE` as setup.

```bash
python scripts/cleanup.py
python scripts/cleanup.py --yes
```

The first command reads the ledger and lists deletion candidates without deleting them. The second deletes resources recorded for this run. Cleanup checks the selected account/partition/Region and validates resource ownership against saved identities where implemented; it does not perform a general account-wide cleanup.

Cleanup retains the ledger and records deletion progress. If it reports remaining resources or unresolved creation attempts, keep the file and inspect the reported items. A failed or interrupted creation can require reconciliation even when no recorded resources remain. For a new run, choose a new state-file path; setup will not overwrite the old one.

## Verification status

Local client checks can be run without AWS:

```bash
python -m unittest discover -s tests -v
python examples/offline_demo.py
```

The local suite covers client behavior, native access checks, and budget parsing/accounting with fixtures. The offline demo supplies six synthetic A/B/C responses. These checks do not establish AWS JWT enforcement, Policy evaluation, rate-limit accuracy, routing, or budget deployment.

AWS verification on 2026-10-28 used native streaming calls to the two scenario models:

| Check | Observed result |
|---|---|
| Invalid JWT and six user/model pairs | Invalid JWT: 401; A/C: both models complete; B: standard completes, high returns 403 |
| Dedicated C/high rate temporarily set to zero | C/high: 429; C/standard and A/high complete; B/high remains 403 |
| Application fallback for that dedicated refusal | C reaches a complete standard-model response with exactly two attempts |
| Optional native budget accounting | Equal positive charges in the daily and monthly ledgers after one complete response |
| REQUEST daily cap temporarily set to zero | A/standard and C/high: BUDGET_EXCEEDED; B/high: MODEL_ACCESS_DENIED; no fallback |

Rate and budget settings were restored after their checks. Price inputs were synthetic, so this does not validate billing accuracy. These bounded tests do not measure sustained positive-rate enforcement, production concurrency, or a hard spending cap. Raw ledgers and run reports contain environment identifiers and are deliberately not committed.

All resources recorded for the verification run were cleaned up with `scripts/cleanup.py`.

## Notes and current limits

- Fallback requires an initial high-performance request, HTTP 429, a recognized and unambiguous JSON error, and exact equality with an allowed complete `limitKey`. Unknown/shared limits, authorization failures, malformed errors, network failures, and server errors do not qualify. The second request is not retried.
- Once HTTP 200 is accepted, there is no retry or model switch, even before the first text event. Broken EventStream frames, disconnects, native error events, or a missing `message_stop` fail the stream.
- The client incrementally decodes `application/vnd.amazon.eventstream` with Botocore frame/CRC validation. The library's `iter_chunks()` checks the envelope; the CLI additionally checks Anthropic completion events. Use the library result as a context manager to close abandoned streams.
- Both models must support the same request body and features. Switching model IDs does not convert the payload.
- The requested model, attempted URI model, and response-reported model are different evidence. Routing rules can rewrite destinations; use responses and routing logs when determining which model served a request.
- TLS verification stays enabled, redirects are refused, and the HTTP client disables environment proxies.
- Keep ledgers, exact limit keys, JWTs, prices for private runs, and raw responses in local ignored files. A custom state path outside `results/` needs its own exclusion from version control.
- Optional response-time budget accounting changes streaming latency and is not a strict spending ceiling. Its native compatibility must be demonstrated on the deployed configuration.

## References

- [Inference targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-targets-inference.html) and [inference connectors](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-inference-connector.html)
- [Native InvokeModel APIs through Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-inference-invokemodel.html)
- [JWT inbound authorization](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/inbound-jwt-authorizer.html)
- [Policy authorization flow](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-authorization-flow.html) and [action/resource policy examples](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-action-resource.html)
- [Rate-limit dimensions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-rate-limits-dimensions.html) and [enforcement](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-rate-limits-enforcement.html)
- [Gateway model fallback](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-rules-fallback.html)
- [HTTP/inference interceptors and response buffering](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html#gateway-interceptors-types-http)
- [Claude Haiku 5.5 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-5-5.html) and [Claude Opus 5.5 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-5-5.html)
- [Anthropic Messages parameters](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages.html)
- [Gateway observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-gateway-metrics.html) and [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)

# Runtime V2 verification

## Strands migration (2026-10-30)

- The coworker now uses Strands Agents `1.62.0`, a fresh `Agent` for each invocation, and two `@tool` functions. A custom Model adapter preserves the existing nonstreaming Messages / Responses requests through AgentCore Gateway.
- All 49 offline Runtime V2 tests passed. These run the actual Strands loop against mocked Gateway responses. They cover both API histories, native reasoning/signatures, exact JWT forwarding and isolation, ordered tool execution, the 32-call ceiling, refusal/timeout handling without retries, and stopping subsequent work after unexpected tool errors.
- The Linux ARM64 Python 3.11 ZIP was built with Strands included. Its `agent.py` was compared with the working source after deployment.
- The retained Runtime V2 was updated from version `3` to version `4` and reported `READY`. This is a deployment status, not proof of successful model invocation.
- The post-deployment read-only check confirmed Gateway `READY`, Policy `ENFORCE`, and rate limit `ACTIVE`, with both upstream interceptors attached. Resource identities, model configuration, Runtime authentication/header forwarding, and application limits were retained. No verification recovery file was pending.
- Independent read-only review identified an unexpected-tool-exception continuation issue. It was fixed, regression tests were added, and the reviewer confirmed the change.
- AWS model invocations and the E2E suite have not been rerun for this Strands artifact. The earlier AWS results below apply to the previous implementation and must not be used as validation of the migration.

Use the [English operations guide](OPERATIONS.md) or [Japanese operations guide](OPERATIONS_JA.md) with the retained deployment state to run `python runtime_v2/verify.py`. Resources remain deployed.

## Earlier documentation and recovery update (2026-10-30)

- All 44 offline Runtime V2 tests passed, including interrupted-run reporting, rate recovery, configuration export, and failures during AWS initialization.
- All 76 Python source files compiled successfully.
- A read-only AWS status check reported Runtime V2 version `3` and Gateway `READY`, Policy `ENFORCE`, both REQUEST/RESPONSE interceptors, and the configured rate limit `ACTIVE`. No pending verification recovery file was present.
- The AWS E2E suite was not rerun for this update. The historical result below does not validate the new recovery path against live AWS services.
- Resources were retained. The agent and vendored Lambda code were not changed by this update; the new commands run on the operator's machine.

Use the [English operations guide](OPERATIONS.md) or [Japanese operations guide](OPERATIONS_JA.md) to reuse the saved deployment, run the E2E checks, and recover an interrupted run. The latest run writes `verification.json`; previous reports are preserved in `verification-history/`.

## Earlier AWS run (2026-10-30)

On 2026-10-30, the deployed sample passed 25 bounded AWS checks, two end-user client checks, and 32 offline regression tests.

The deployed path was Cognito → AgentCore Runtime V2 → AgentCore Gateway → Bedrock Runtime. It used the upstream REQUEST/RESPONSE interceptors, an added inference provider, enforced AgentCore Policy, and per-user rate limits.

| Area | Observed result |
|---|---|
| Hosting | Runtime reported `READY` and `platformVersion=V2`; the deployment command also completed when rerun against the same deployment |
| Claude Haiku 5.5 | Completed two model calls, recorded two work items, and returned a written answer |
| GPT-6 Sol | Completed two model calls, recorded two work items, and returned a written answer |
| Identity | Invalid JWTs and users lacking the access group were rejected at both Runtime and Gateway |
| Authorization | A restricted user was refused the premium model and could use the standard model |
| Request limit | An exact user entry with a zero request limit produced a Gateway `429` |
| Connection limit | An exact user entry with a zero connection limit produced a Gateway `429` |
| Budget | Recorded daily/monthly spending of USD 10 against USD 10 caps caused the next model call to be refused; recorded spending did not increase |
| Accounting | Both models increased the caller's DynamoDB spending; the RESPONSE Lambda emitted CloudWatch `EstimatedCostUSD` metrics |
| End-user client | With environment variables cleared and no AWS SDK credential use, Cognito login and Runtime invocation returned a completed task; a restricted premium request returned `POLICY_DENIED` |

The inference profiles were `global.anthropic.claude-haiku-5-5` and `global.openai.gpt-6-sol`. Models, prices, account, and Region must be checked for each deployment.

## Interpretation

- Runtime returns handled task outcomes over HTTP `200`. Task success requires `status="completed"`. A refused task returns `status="stopped"`, its `stop_reason`, and the original Gateway status in `gateway_status`. Both CLI entry points exit with code `2` for a stopped task.
- Rate tests exercised refusal paths using a dedicated probe user. They did not measure the exact throughput of 60 RPM or stress-test two concurrent requests. Verification restored the normal entries and disabled the probe user.
- The budget boundary was seeded in the probe's spending row. Verification did not consume USD 10 of inference or establish a strict spending guarantee. The upstream interceptors admit requests using recorded, cached spending and account afterward.
- The deployed resources were retained for use. Destructive cleanup was not executed against this deployment. Offline regressions cover interrupted role/bucket deletion and rejection of an artifact bucket with different ownership tags.

## Reproduction and evidence

Follow the [English](../README.md) or [Japanese](../README_JA.md) setup instructions. Run `python runtime_v2/verify.py` against the resulting deployment and the offline test command in the README.

The live checks write `verification.json` beside the private deployment state. The additional end-user checks were recorded in `client-verification.json`. Those reports contain deployment-specific data and are intentionally excluded from Git.

The original template and Lambda implementation are pinned in [SOURCE.json](vendor/usage-interceptor/SOURCE.json). This record describes functional checks of the sample, not a service performance benchmark or an AWS billing reconciliation.

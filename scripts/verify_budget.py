"""Verify native usage accounting and temporarily set the request budget to zero."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import budget_interceptors as budget
from common import Environment, main_guard, require, write_json
from verify_controls import probe


def completed_environment(client, function, expected, before):
    client.get_waiter("function_updated_v2").wait(
        FunctionName=function["id"], WaiterConfig={"Delay": 2, "MaxAttempts": 120})
    current = client.get_function_configuration(FunctionName=function["id"])
    require(current["FunctionArn"] == function["arn"]
            and current["Environment"]["Variables"] == expected
            and all(current.get(key) == before.get(key)
                    for key in ("CodeSha256", "Role", "Runtime", "Handler")),
            "Completed function configuration differs from the expected update.")
    # Lambda advances RevisionId again when its asynchronous update completes.
    # Guard later writes with the completed revision, not the update response.
    return current["RevisionId"]


def restore_environment(client, function, expected, original, revision):
    require(revision, "Budget update result is unknown; retain restoration information.")
    current = client.get_function_configuration(FunctionName=function["id"])
    require(current["FunctionArn"] == function["arn"]
            and current["RevisionId"] == revision
            and current["Environment"]["Variables"] == expected,
            "Function changed during verification; refusing to overwrite it.")
    client.update_function_configuration(
        FunctionName=function["id"], RevisionId=revision, Environment={"Variables": original})
    completed_environment(client, function, original, current)


def verify(env):
    require(isinstance(env.state.get("budget"), dict), "Configure budgets first.")
    today = datetime.now(timezone.utc)
    report = {"date": today.isoformat(), "passed": False, "results": [], "restoration": "not_needed"}
    env.state["budget"]["verification"] = "running"
    env.state["budget"]["verification_started"] = report["date"]
    env.save()
    changed = False
    success = False
    zero_revision = None
    try:
        require("budget_restore_variables" not in env.state,
                "An earlier verification requires reconciliation; retain the ledger.")
        function = env.one("function", "access-request")
        table = env.one("table", "budget")
        client = env.client("lambda")
        current = client.get_function_configuration(FunctionName=function["id"])
        require(current["FunctionArn"] == function["arn"], "Request function identity mismatch.")
        variables = current["Environment"]["Variables"]
        require(variables.get("ENABLE_BUDGET") == "true" and variables.get("ENFORCE_BUDGET") == "true",
                "Deploy budget enforcement before running this test.")
        require(budget._decimal(variables["DAILY_BUDGET_USD"]) > 0
                and budget._decimal(variables["MONTHLY_BUDGET_USD"]) > 0,
                "Use positive budget caps before the test.")
        budget._CLIENT = env.client("dynamodb")
        day, month = "DAY#" + today.strftime("%Y-%m-%d"), "MONTH#" + today.strftime("%Y-%m")
        user_pk = "USER#" + budget._hash(env.one("gateway")["arn"], env.one("user", "a")["sub"])
        before = [budget._spend(table["id"], user_pk, key) for key in (day, month)]
        row = probe(env, "a", "standard")
        report["results"].append(row)
        require(row["status"] == 200, "A standard invocation must succeed before testing the cap.")
        require(datetime.now(timezone.utc).strftime("%Y-%m-%d") == today.strftime("%Y-%m-%d"),
                "UTC date changed during the test; inspect the adjacent period.")
        after = [budget._spend(table["id"], user_pk, key) for key in (day, month)]
        deltas = [a - b for a, b in zip(after, before)]
        require(deltas[0] > 0 and deltas[0] == deltas[1],
                "Expected one equal, positive charge in the daily and monthly ledgers. Inspect response logs.")
        report["equal_positive_daily_monthly_charge"] = True
        env.state["budget_restore_variables"] = variables
        env.save()
        changed = True
        zero_variables = {**variables, "DAILY_BUDGET_USD": "0"}
        report["restoration"] = "required"
        client.update_function_configuration(
            FunctionName=function["id"], RevisionId=current["RevisionId"],
            Environment={"Variables": zero_variables})
        zero_revision = completed_environment(client, function, zero_variables, current)
        for user, model, expected in (("a", "standard", "BUDGET_EXCEEDED"),
                                     ("c", "premium", "BUDGET_EXCEEDED"),
                                     ("b", "premium", "MODEL_ACCESS_DENIED")):
            row = probe(env, user, model, fallback=True)
            report["results"].append(row)
            code = json.loads(row.get("error_body") or "{}").get("error", {}).get("code")
            require(row["status"] == 403 and code == expected and row["attempts"] == 1,
                    "Budget refusal/model authorization was not preserved, or fallback occurred.")
        success = True
    finally:
        try:
            if changed:
                restore_environment(client, function, zero_variables, variables, zero_revision)
                env.state.pop("budget_restore_variables", None)
                report["restoration"] = "complete"
        finally:
            report["passed"] = success and report["restoration"] in {"not_needed", "complete"}
            env.state["budget"]["verification"] = (
                "native_accounting_and_zero_cap_passed" if report["passed"] else "failed")
            env.state["budget"]["verification_restoration"] = report["restoration"]
            env.save()
            write_json(env.path.parent / "verify-budget.json", report)
    return report


def main():
    verify(Environment())
    print(json.dumps({"passed": True, "test": "budget", "original_caps_restored": True}))


if __name__ == "__main__":
    main_guard(main)

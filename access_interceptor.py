"""URI-based native model authorization after Gateway JWT validation.

MODEL_ACCESS_JSON maps trusted JWT subjects to exact allowed URI model IDs.
Only native InvokeModelWithResponseStream POSTs are accepted. Request-body
model fields never influence authorization. The Lambda is not an authorizer:
the Gateway must validate the JWT and pass its headers to this interceptor.
Its resource policy and execution IAM must restrict invocation to that Gateway.
"""
import os

import budget_interceptors as protocol


def request_handler(event, context):
    try:
        request = protocol._http_part(event, "gatewayRequest")
        _, gateway = protocol._request_identity(context)
        expected = os.environ.get("EXPECTED_GATEWAY_ARN")
        if not expected or gateway != expected:
            raise protocol.BudgetError("GATEWAY_CONTEXT_MISMATCH", 403)
        if request.get("httpMethod") != "POST":
            raise protocol.BudgetError("OPERATION_NOT_ALLOWED", 403)
        model = protocol._model_from_path(request.get("path"))
        subject = protocol._trusted_subject(request.get("headers"))
        access = protocol._json_loads(os.environ["MODEL_ACCESS_JSON"])
        if not isinstance(access, dict) or any(
            not isinstance(key, str) or not key or not isinstance(models, list)
            or not models or any(not isinstance(item, str) or not item for item in models)
            for key, models in access.items()
        ):
            raise protocol.BudgetError("ACCESS_CONFIGURATION_INVALID")
        if model not in access.get(subject, []):
            raise protocol.BudgetError("MODEL_ACCESS_DENIED", 403)
        mode = os.environ.get("ENABLE_BUDGET") or "false"
        if mode not in {"true", "false"}:
            raise protocol.BudgetError("ACCESS_CONFIGURATION_INVALID")
        if mode == "true":
            return protocol.request_handler(event, context)
        return protocol._pass()
    except protocol.BudgetError as error:
        return protocol._deny(error)
    except Exception:
        return protocol._deny(protocol.BudgetError("ACCESS_INTERNAL_ERROR"))

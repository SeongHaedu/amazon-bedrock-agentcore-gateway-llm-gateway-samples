"""Check the installed control-plane SDK without credentials or AWS calls."""
import json
import sys

import boto3
import botocore
import botocore.session


def main():
    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    required = {
        "CreateGateway": {"authorizerConfiguration", "policyEngineConfiguration", "interceptorConfigurations"},
        "CreateGatewayTarget": {"targetConfiguration"},
        "CreatePolicy": {"definition", "validationMode", "enforcementMode"},
        "CreatePolicyEngine": {"name"},
        "CreateGatewayRateLimit": {"dimensionKeys", "entries"},
        "UpdateGatewayRateLimit": {"entries"},
    }
    missing = []
    for name, members in required.items():
        if name not in model.operation_names:
            missing.append(name)
        else:
            missing.extend(name + "." + key for key in
                           members - model.operation_model(name).input_shape.members.keys())
    target = model.shape_for("TargetConfiguration")
    if "inference" not in target.members:
        missing.append("TargetConfiguration.inference")
    print(json.dumps({"boto3": boto3.__version__, "botocore": botocore.__version__,
                      "missing_capabilities": missing, "aws_calls": 0}))
    return bool(missing)


if __name__ == "__main__":
    sys.exit(main())

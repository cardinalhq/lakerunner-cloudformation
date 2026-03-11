#!/usr/bin/env python3
# Copyright (C) 2025 CardinalHQ, Inc
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

from troposphere import (
    AWSObject, GetAtt, Output, Parameter, Ref, Template,
)
from troposphere.iam import Role, Policy
from troposphere.awslambda import Function, Code


class EnableBedrockModel(AWSObject):
    resource_type = "Custom::EnableBedrockModel"
    props = {
        "ServiceToken": (str, True),
        "ModelId": (str, True),
    }


t = Template()
t.set_description(
    "Lakerunner Bedrock Setup: accepts Marketplace model agreements."
    " Deploy once per account/region before using AI features."
)

# -----------------------
# Parameters
# -----------------------
ModelId = t.add_parameter(Parameter(
    "ModelId",
    Type="String",
    Default="anthropic.claude-sonnet-4-6",
    Description="Bedrock foundation model ID to enable access for."
))

# -----------------------
# Lambda Role
# -----------------------
LambdaRole = t.add_resource(Role(
    "BedrockSetupLambdaRole",
    AssumeRolePolicyDocument={
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    },
    Policies=[
        Policy(
            PolicyName="BedrockSetup",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "logs:CreateLogGroup",
                            "logs:CreateLogStream",
                            "logs:PutLogEvents"
                        ],
                        "Resource": "*"
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "bedrock:ListFoundationModelAgreementOffers",
                            "bedrock:CreateFoundationModelAgreement",
                            "bedrock:GetFoundationModelAvailability",
                            "aws-marketplace:ViewSubscriptions",
                            "aws-marketplace:Subscribe"
                        ],
                        "Resource": "*"
                    }
                ]
            }
        )
    ]
))

# -----------------------
# Inline Lambda
# -----------------------
lambda_code = r'''
import json, urllib.request, boto3

bedrock = boto3.client("bedrock")

def send(event, context, status, data=None, reason=""):
    resp = {
        "Status": status,
        "Reason": f"{reason} See CloudWatch Logs: {context.log_stream_name}",
        "PhysicalResourceId": event.get("PhysicalResourceId") or "EnableBedrockModel",
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": False,
        "Data": data or {}
    }
    body = json.dumps(resp).encode("utf-8")
    req = urllib.request.Request(event["ResponseURL"], data=body, method="PUT")
    req.add_header("content-type", "")
    req.add_header("content-length", str(len(body)))
    try:
        with urllib.request.urlopen(req) as r:
            r.read()
    except Exception as e:
        print("Failed to send response:", e)

def handler(event, context):
    print("Event:", json.dumps(event))
    if event["RequestType"] == "Delete":
        send(event, context, "SUCCESS", {"Message": "Delete no-op"})
        return

    model_id = event["ResourceProperties"]["ModelId"]
    try:
        offers = bedrock.list_foundation_model_agreement_offers(modelId=model_id)
        if not offers.get("offers"):
            print("No offers found -- model may already be enabled")
            send(event, context, "SUCCESS", {"Message": "No agreement required"})
            return

        offer_token = offers["offers"][0]["offerToken"]
        bedrock.create_foundation_model_agreement(
            modelId=model_id, offerToken=offer_token
        )
        print(f"Model agreement accepted for {model_id}")
        send(event, context, "SUCCESS", {"Message": f"Model {model_id} enabled"})

    except Exception as e:
        msg = str(e)
        if "already exists" in msg.lower() or "ConflictException" in type(e).__name__:
            print(f"Model {model_id} already has an active agreement")
            send(event, context, "SUCCESS", {"Message": "Already enabled"})
        else:
            print("Exception:", e)
            send(event, context, "FAILED", {"Error": msg}, reason=msg)
'''

SetupFn = t.add_resource(Function(
    "BedrockSetupFunction",
    Runtime="python3.13",
    Handler="index.handler",
    Role=GetAtt(LambdaRole, "Arn"),
    Timeout=30,
    Code=Code(ZipFile=lambda_code)
))

# -----------------------
# Custom Resource
# -----------------------
t.add_resource(EnableBedrockModel(
    "EnableBedrockModelAccess",
    ServiceToken=GetAtt(SetupFn, "Arn"),
    ModelId=Ref(ModelId)
))

# -----------------------
# Outputs
# -----------------------
t.add_output(Output(
    "ModelId",
    Description="Bedrock model ID with access enabled",
    Value=Ref(ModelId)
))

if __name__ == "__main__":
    print(t.to_yaml())

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

"""
Lakerunner Alerting Stack

Creates an SNS topic for CloudWatch alarms and subscribes email addresses.
Deploy this stack first, then provide the SNS topic ARN to the Services stack
to enable task count alarms.

Note: Email subscriptions require manual confirmation. After deploying this
stack, each email address will receive a confirmation email that must be
clicked to activate the subscription.
"""

from troposphere import (
    Template, Parameter, Ref, Sub, GetAtt, Equals, Not, Export, Output, Tags
)
from troposphere.sns import Topic, SubscriptionResource

t = Template()
t.set_description(
    "Lakerunner Alerting: SNS topic for CloudWatch alarms with email subscriptions. "
    "Email addresses must confirm subscription after stack creation."
)

# -----------------------
# Parameters
# -----------------------
# Support up to 5 email addresses (CloudFormation doesn't natively support
# dynamic list iteration, so we use individual parameters with conditions)

email_params = []
for i in range(1, 6):
    param = t.add_parameter(Parameter(
        f"Email{i}",
        Type="String",
        Default="",
        Description=f"Email address {i} for alarm notifications (leave blank to skip)"
    ))
    email_params.append(param)
    t.add_condition(f"HasEmail{i}", Not(Equals(Ref(param), "")))

# -----------------------
# Parameter Groups for Console
# -----------------------
t.set_metadata({
    "AWS::CloudFormation::Interface": {
        "ParameterGroups": [
            {
                "Label": {"default": "Email Notifications"},
                "Parameters": [f"Email{i}" for i in range(1, 6)]
            }
        ],
        "ParameterLabels": {
            f"Email{i}": {"default": f"Email Address {i}"} for i in range(1, 6)
        }
    }
})

# -----------------------
# SNS Topic
# -----------------------
AlertTopic = t.add_resource(Topic(
    "AlertTopic",
    TopicName=Sub("${AWS::StackName}-alerts"),
    Tags=Tags(
        Name=Sub("${AWS::StackName}-alerts"),
        ManagedBy="Lakerunner",
        Environment=Ref("AWS::StackName"),
        Component="Alerting"
    )
))

# -----------------------
# Email Subscriptions (conditional)
# -----------------------
for i in range(1, 6):
    t.add_resource(SubscriptionResource(
        f"EmailSubscription{i}",
        Condition=f"HasEmail{i}",
        TopicArn=Ref(AlertTopic),
        Protocol="email",
        Endpoint=Ref(email_params[i - 1])
    ))

# -----------------------
# Outputs
# -----------------------
t.add_output(Output(
    "AlertTopicArn",
    Description="ARN of the SNS topic for CloudWatch alarms. Provide this to the Services stack.",
    Value=Ref(AlertTopic),
    Export=Export(name=Sub("${AWS::StackName}-AlertTopicArn"))
))

t.add_output(Output(
    "AlertTopicName",
    Description="Name of the SNS topic",
    Value=GetAtt(AlertTopic, "TopicName"),
    Export=Export(name=Sub("${AWS::StackName}-AlertTopicName"))
))

print(t.to_yaml())

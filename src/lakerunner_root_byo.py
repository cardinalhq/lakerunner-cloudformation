#!/usr/bin/env python3
"""Lakerunner root CloudFormation template optimized for BYO VPC scenarios.

This template is designed for users who want to bring their own VPC and uses
AWS parameter types that provide dropdown lists in the CloudFormation console.
"""

import yaml
import os
from troposphere import (
    Template, Parameter, Ref, Equals, Sub, GetAtt, If, Not, And, Or,
    Condition, Output, Export, Tags
)


def load_defaults():
    """Load default configuration from YAML file."""
    config_path = os.path.join(os.path.dirname(__file__), '..', 'lakerunner-stack-defaults.yaml')
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


# Initialize template
TEMPLATE_DESCRIPTION = (
    "Lakerunner infrastructure deployment optimized for bring-your-own VPC scenarios"
)

t = Template()
t.set_description(TEMPLATE_DESCRIPTION)

# Load defaults
defaults = load_defaults()

# Template base URL parameter (required for nested stacks)
base_url = t.add_parameter(
    Parameter(
        "TemplateBaseUrl",
        Type="String",
        Description="Base URL where nested templates are stored (e.g., https://s3.amazonaws.com/bucket/templates/)",
    )
)

# =============================================================================
# BYO VPC Parameters with Dropdown Lists
# =============================================================================

existing_vpc_id = t.add_parameter(
    Parameter(
        "ExistingVPCId",
        Type="AWS::EC2::VPC::Id",
        Description="Select the VPC to use for Lakerunner infrastructure",
    )
)

existing_private_subnet1 = t.add_parameter(
    Parameter(
        "ExistingPrivateSubnet1Id",
        Type="AWS::EC2::Subnet::Id",
        Description="Select the first private subnet for ECS/EKS clusters",
    )
)

existing_private_subnet2 = t.add_parameter(
    Parameter(
        "ExistingPrivateSubnet2Id",
        Type="AWS::EC2::Subnet::Id",
        Description="Select the second private subnet for ECS/EKS clusters (different AZ)",
    )
)

has_public_subnets = t.add_parameter(
    Parameter(
        "HasPublicSubnets",
        Type="String",
        Default="No",
        AllowedValues=["Yes", "No"],
        Description="Do you want to use public subnets for load balancers?",
    )
)

existing_public_subnet1 = t.add_parameter(
    Parameter(
        "ExistingPublicSubnet1Id",
        Type="AWS::EC2::Subnet::Id",
        Description="Select first public subnet for load balancers (required if HasPublicSubnets=Yes)",
    )
)

existing_public_subnet2 = t.add_parameter(
    Parameter(
        "ExistingPublicSubnet2Id", 
        Type="AWS::EC2::Subnet::Id",
        Description="Select second public subnet for load balancers (required if HasPublicSubnets=Yes)",
    )
)

# =============================================================================
# Conditions
# =============================================================================

t.add_condition("HasBYOPublicSubnets", Equals(Ref(has_public_subnets), "Yes"))

# =============================================================================
# Parameter Groups (for CloudFormation console organization)
# =============================================================================

t.set_metadata({
    "AWS::CloudFormation::Interface": {
        "ParameterGroups": [
            {
                "Label": {"default": "Template Configuration"},
                "Parameters": ["TemplateBaseUrl"]
            },
            {
                "Label": {"default": "VPC Configuration (Select Existing Resources)"},
                "Parameters": [
                    "ExistingVPCId",
                    "ExistingPrivateSubnet1Id",
                    "ExistingPrivateSubnet2Id", 
                    "HasPublicSubnets",
                    "ExistingPublicSubnet1Id",
                    "ExistingPublicSubnet2Id"
                ]
            }
        ],
        "ParameterLabels": {
            "ExistingVPCId": {"default": "VPC"},
            "ExistingPrivateSubnet1Id": {"default": "Private Subnet 1"},
            "ExistingPrivateSubnet2Id": {"default": "Private Subnet 2"},
            "HasPublicSubnets": {"default": "Use public subnets for ALB?"},
            "ExistingPublicSubnet1Id": {"default": "Public Subnet 1"},
            "ExistingPublicSubnet2Id": {"default": "Public Subnet 2"}
        }
    }
})

# =============================================================================
# Outputs (using provided resources)
# =============================================================================

# VPC ID from user selection
t.add_output(
    Output(
        "VPCId",
        Description="Selected VPC ID",
        Value=Ref(existing_vpc_id),
        Export=Export(Sub("${AWS::StackName}-VPCId"))
    )
)

# Private Subnets from user selection
t.add_output(
    Output(
        "PrivateSubnets",
        Description="Selected private subnet IDs",
        Value=Sub("${ExistingPrivateSubnet1Id},${ExistingPrivateSubnet2Id}"),
        Export=Export(Sub("${AWS::StackName}-PrivateSubnets"))
    )
)

# Public Subnets from user selection (if provided)
t.add_output(
    Output(
        "PublicSubnets",
        Condition="HasBYOPublicSubnets",
        Description="Selected public subnet IDs",
        Value=Sub("${ExistingPublicSubnet1Id},${ExistingPublicSubnet2Id}"),
        Export=Export(Sub("${AWS::StackName}-PublicSubnets"))
    )
)


if __name__ == "__main__":
    print(t.to_yaml())
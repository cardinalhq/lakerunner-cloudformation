#!/usr/bin/env python3
"""Tests for cross-stack parameter handoffs and integration."""

import pytest
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from lakerunner_root import t as root_template
from lakerunner_vpc import t as vpc_template
from lakerunner_ecs import t as ecs_template
from lakerunner_rds import t as rds_template  
from lakerunner_s3 import t as s3_template
from lakerunner_services import create_services_template

# Create services template instance
services_template = create_services_template()


class TestStackHandoffs:
    """Test suite for cross-stack integration and parameter passing."""

    def test_vpc_to_ecs_handoff(self):
        """VPC stack outputs should match ECS stack input parameters."""
        vpc_json = json.loads(vpc_template.to_json())
        ecs_json = json.loads(ecs_template.to_json())
        
        vpc_outputs = set(vpc_json.get("Outputs", {}).keys())
        ecs_params = set(ecs_json.get("Parameters", {}).keys())
        
        # VPC should output what ECS needs as input (simplified check)
        vpc_has_vpc_id = any("VpcId" in output for output in vpc_outputs)
        ecs_needs_vpc_id = "VpcId" in ecs_params
        assert vpc_has_vpc_id, "VPC should export VpcId"
        assert ecs_needs_vpc_id, "ECS should accept VpcId parameter"

    def test_vpc_to_rds_handoff(self):
        """VPC stack outputs should match RDS stack input parameters."""
        vpc_json = json.loads(vpc_template.to_json())
        rds_json = json.loads(rds_template.to_json())
        
        vpc_outputs = set(vpc_json.get("Outputs", {}).keys())
        rds_params = set(rds_json.get("Parameters", {}).keys())
        
        # VPC should output what RDS needs
        required_handoffs = ["VpcId", "PrivateSubnets"]
        for handoff in required_handoffs:
            vpc_has_output = any(handoff in output for output in vpc_outputs)
            rds_has_param = handoff in rds_params
            assert vpc_has_output, f"VPC should export {handoff}"
            assert rds_has_param, f"RDS should accept {handoff} parameter"

    def test_ecs_to_services_handoff(self):
        """ECS stack outputs should match Services stack requirements."""
        ecs_json = json.loads(ecs_template.to_json())
        services_json = json.loads(services_template.to_json())
        
        ecs_outputs = set(ecs_json.get("Outputs", {}).keys())
        services_params = set(services_json.get("Parameters", {}).keys())
        
        # ECS should export cluster info that services need
        assert "ClusterArn" in ecs_outputs, "ECS should export ClusterArn"
        assert "TaskSGId" in ecs_outputs, "ECS should export TaskSGId"
        
        # Services should accept these as parameters
        assert "ClusterArn" in services_params, "Services should accept ClusterArn parameter"
        assert "TaskSecurityGroupId" in services_params, "Services should accept TaskSecurityGroupId parameter"

    def test_storage_to_services_handoff(self):
        """S3 stack outputs should be importable by Services stack."""
        s3_json = json.loads(s3_template.to_json())
        services_json = json.loads(services_template.to_json())
        
        s3_outputs = set(s3_json.get("Outputs", {}).keys())
        
        # S3 should export storage resources
        storage_outputs = [output for output in s3_outputs if any(keyword in output for keyword in ["Bucket", "Queue", "Role"])]
        assert len(storage_outputs) > 0, "S3 should export storage resources"

    def test_rds_to_services_handoff(self):
        """RDS stack outputs should be importable by Services stack."""
        rds_json = json.loads(rds_template.to_json())
        
        rds_outputs = set(rds_json.get("Outputs", {}).keys())
        
        # RDS should export database connection info
        db_outputs = [output for output in rds_outputs if any(keyword in output for keyword in ["Database", "Endpoint", "Secret"])]
        assert len(db_outputs) > 0, "RDS should export database connection information"

    def test_root_stack_parameter_passing(self):
        """Root stack should properly pass parameters to nested stacks."""
        root_json = json.loads(root_template.to_json())
        
        resources = root_json.get("Resources", {})
        stack_resources = {name: res for name, res in resources.items() if res["Type"] == "AWS::CloudFormation::Stack"}
        
        assert len(stack_resources) > 0, "Root should have nested stack resources"
        
        # Check that nested stacks receive proper parameters
        for stack_name, stack_resource in stack_resources.items():
            properties = stack_resource.get("Properties", {})
            params = properties.get("Parameters", {})
            assert len(params) > 0, f"Stack {stack_name} should receive parameters from root"
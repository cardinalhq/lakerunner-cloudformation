#!/usr/bin/env python3
"""Tests for the ECS infrastructure stack template."""

import pytest
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from lakerunner_ecs import t as ecs_template


class TestEcsStack:
    """Test suite for ECS infrastructure stack template."""

    def test_template_is_valid_json(self):
        """ECS template should generate valid JSON."""
        json_output = ecs_template.to_json()
        parsed = json.loads(json_output)
        assert isinstance(parsed, dict)

    def test_cluster_resource_exists(self):
        """ECS template should have ECS cluster resource."""
        json_output = json.loads(ecs_template.to_json())
        resources = json_output["Resources"]
        
        cluster_resources = [r for r in resources.values() if r["Type"] == "AWS::ECS::Cluster"]
        assert len(cluster_resources) > 0

    def test_required_parameters_exist(self):
        """ECS template should have required input parameters from VPC."""
        json_output = json.loads(ecs_template.to_json())
        params = json_output["Parameters"]
        
        # Parameters that would come from VPC stack
        assert "VpcId" in params

    def test_outputs_for_handoff(self):
        """ECS template should export cluster info for services stack."""
        json_output = json.loads(ecs_template.to_json())
        outputs = json_output.get("Outputs", {})
        
        # Check for ECS cluster exports
        expected_outputs = ["ClusterArn", "TaskSGId"]
        for output_name in expected_outputs:
            assert output_name in outputs, f"Expected output '{output_name}' not found"

    def test_security_group_exists(self):
        """ECS template should create security group for tasks."""
        json_output = json.loads(ecs_template.to_json())
        resources = json_output["Resources"]
        
        sg_resources = [r for r in resources.values() if r["Type"] == "AWS::EC2::SecurityGroup"]
        assert len(sg_resources) > 0
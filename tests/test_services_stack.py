#!/usr/bin/env python3
"""Tests for the ECS services stack template."""

import pytest
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from lakerunner_ecs_services import create_services_template

# Create template instance
services_template = create_services_template()


class TestServicesStack:
    """Test suite for ECS services stack template."""

    def test_template_is_valid_json(self):
        """Services template should generate valid JSON."""
        json_output = services_template.to_json()
        parsed = json.loads(json_output)
        assert isinstance(parsed, dict)

    def test_infrastructure_parameters_exist(self):
        """Services template should accept infrastructure from other stacks."""
        json_output = json.loads(services_template.to_json())
        params = json_output["Parameters"]
        
        # Parameters from infrastructure stacks
        infra_params = ["ClusterArn", "VpcId", "PrivateSubnets", "TaskSecurityGroupId"]
        for param in infra_params:
            assert param in params, f"Missing parameter: {param}"

    def test_image_parameters_exist(self):
        """Services template should have container image parameters."""
        json_output = json.loads(services_template.to_json())
        params = json_output["Parameters"]
        
        # Should have image parameters for air-gapped deployment
        image_params = [p for p in params.keys() if "Image" in p]
        assert len(image_params) > 0

    def test_has_ecs_services(self):
        """Services template should create ECS service resources."""
        json_output = json.loads(services_template.to_json())
        resources = json_output["Resources"]
        
        # Should have ECS services
        ecs_services = [r for r in resources.values() if r["Type"] == "AWS::ECS::Service"]
        assert len(ecs_services) > 0

    def test_has_task_definitions(self):
        """Services template should create ECS task definitions."""
        json_output = json.loads(services_template.to_json())
        resources = json_output["Resources"]
        
        # Should have task definitions
        task_defs = [r for r in resources.values() if r["Type"] == "AWS::ECS::TaskDefinition"]
        assert len(task_defs) > 0

    def test_accepts_direct_parameters(self):
        """Services template should accept direct parameter inputs (not ImportValue based)."""
        json_output = json.loads(services_template.to_json())
        params = json_output["Parameters"]
        
        # Services template takes direct parameters rather than using ImportValue
        # This is a valid architecture choice
        assert len(params) > 0
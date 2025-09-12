#!/usr/bin/env python3
"""Tests for the Lakerunner root template."""

import pytest
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from lakerunner_root import t as root_template


class TestRootTemplate:
    """Test suite for root template structure and functionality."""

    def test_template_is_valid_json(self):
        """Root template should generate valid JSON."""
        json_output = root_template.to_json()
        parsed = json.loads(json_output)
        assert isinstance(parsed, dict)
        assert "Parameters" in parsed
        assert "Resources" in parsed

    def test_template_description_exists(self):
        """Root template should have a description."""
        json_output = json.loads(root_template.to_json())
        assert "Description" in json_output
        assert "Lakerunner infrastructure" in json_output["Description"]

    def test_base_url_parameter_exists(self):
        """Root template should have TemplateBaseUrl parameter."""
        json_output = json.loads(root_template.to_json())
        params = json_output["Parameters"]
        assert "TemplateBaseUrl" in params
        assert params["TemplateBaseUrl"]["Type"] == "String"

    def test_create_conditions_exist(self):
        """Root template should have Create* conditions for modular deployment."""
        json_output = json.loads(root_template.to_json())
        conditions = json_output.get("Conditions", {})
        
        expected_conditions = [
            "CreateECSInfraCondition",
            "CreateECSServicesCondition", 
            "CreateRDSCondition",
            "CreateS3StorageCondition",
            "CreateMSKCondition"
        ]
        
        for condition in expected_conditions:
            assert condition in conditions

    def test_has_nested_stacks(self):
        """Root template should define nested stack resources."""
        json_output = json.loads(root_template.to_json())
        resources = json_output["Resources"]
        
        # Should have at least some stack resources
        stack_resources = [r for r in resources.values() if r["Type"] == "AWS::CloudFormation::Stack"]
        assert len(stack_resources) > 0

    def test_template_generates_without_error(self):
        """Root template should generate without throwing exceptions."""
        try:
            output = root_template.to_json()
            assert len(output) > 0
        except Exception as e:
            pytest.fail(f"Template generation failed: {e}")
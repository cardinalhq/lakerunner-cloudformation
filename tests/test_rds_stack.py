#!/usr/bin/env python3
"""Tests for the RDS stack template."""

import pytest
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from lakerunner_rds import t as rds_template


class TestRdsStack:
    """Test suite for RDS stack template."""

    def test_template_is_valid_json(self):
        """RDS template should generate valid JSON."""
        json_output = rds_template.to_json()
        parsed = json.loads(json_output)
        assert isinstance(parsed, dict)

    def test_required_parameters_exist(self):
        """RDS template should have required input parameters."""
        json_output = json.loads(rds_template.to_json())
        params = json_output["Parameters"]
        
        required_params = ["PrivateSubnets", "VpcId"]
        for param in required_params:
            assert param in params
            
    def test_database_resource_exists(self):
        """RDS template should have database instance resource."""
        json_output = json.loads(rds_template.to_json())
        resources = json_output["Resources"]
        
        db_resources = [r for r in resources.values() if r["Type"] == "AWS::RDS::DBInstance"]
        assert len(db_resources) > 0

    def test_security_group_exists(self):
        """RDS template should create security group for database."""
        json_output = json.loads(rds_template.to_json())
        resources = json_output["Resources"]
        
        sg_resources = [r for r in resources.values() if r["Type"] == "AWS::EC2::SecurityGroup"]
        assert len(sg_resources) > 0

    def test_outputs_for_handoff(self):
        """RDS template should export database connection details."""
        json_output = json.loads(rds_template.to_json())
        outputs = json_output.get("Outputs", {})
        
        # Check for database connection exports (using actual output names)
        expected_outputs = ["DbEndpoint", "DatabaseSecurityGroupId"]
        for output_name in expected_outputs:
            assert output_name in outputs, f"Expected output '{output_name}' not found"
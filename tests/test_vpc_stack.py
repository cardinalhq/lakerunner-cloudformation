#!/usr/bin/env python3
"""Tests for the VPC stack template."""

import pytest
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from lakerunner_vpc import t as vpc_template


class TestVpcStack:
    """Test suite for VPC stack template."""

    def test_template_is_valid_json(self):
        """VPC template should generate valid JSON."""
        json_output = vpc_template.to_json()
        parsed = json.loads(json_output)
        assert isinstance(parsed, dict)

    def test_template_has_vpc_resources(self):
        """VPC template should have VPC-related resources."""
        json_output = json.loads(vpc_template.to_json())
        resources = json_output.get("Resources", {})
        
        # Look for VPC-related resource types
        resource_types = [r["Type"] for r in resources.values()]
        vpc_types = [t for t in resource_types if "EC2::" in t and ("VPC" in t or "Subnet" in t or "Gateway" in t or "Route" in t)]
        assert len(vpc_types) > 0

    def test_outputs_for_handoff(self):
        """VPC template should export values for other stacks."""
        json_output = json.loads(vpc_template.to_json())
        outputs = json_output.get("Outputs", {})
        
        # Check for key exports that other stacks would need
        expected_outputs = ["VpcId", "PrivateSubnets"]
        for output_name in expected_outputs:
            matching_outputs = [name for name in outputs.keys() if output_name in name]
            assert len(matching_outputs) > 0, f"Expected output containing '{output_name}' not found"
#!/usr/bin/env python3
"""Tests for the S3 storage stack template."""

import pytest
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from lakerunner_s3 import t as s3_template


class TestS3Stack:
    """Test suite for S3 storage stack template."""

    def test_template_is_valid_json(self):
        """S3 template should generate valid JSON."""
        json_output = s3_template.to_json()
        parsed = json.loads(json_output)
        assert isinstance(parsed, dict)

    def test_storage_resources_exist(self):
        """S3 template should have S3 bucket and SQS queue."""
        json_output = json.loads(s3_template.to_json())
        resources = json_output["Resources"]
        
        # Check for S3 bucket
        s3_resources = [r for r in resources.values() if r["Type"] == "AWS::S3::Bucket"]
        assert len(s3_resources) > 0
        
        # Check for SQS queue
        sqs_resources = [r for r in resources.values() if r["Type"] == "AWS::SQS::Queue"]
        assert len(sqs_resources) > 0

    def test_iam_role_creation_logic(self):
        """S3 template should handle existing vs new task role logic."""
        json_output = json.loads(s3_template.to_json())
        
        conditions = json_output.get("Conditions", {})
        assert "CreateTaskRole" in conditions
        assert "UseExistingTaskRole" in conditions

    def test_outputs_for_handoff(self):
        """S3 template should export storage configuration for services."""
        json_output = json.loads(s3_template.to_json())
        outputs = json_output.get("Outputs", {})
        
        # Check for storage-related exports (using actual output names)
        expected_outputs = ["BucketArn", "BucketName", "TaskRoleArn"]
        for output_name in expected_outputs:
            assert output_name in outputs, f"Expected output '{output_name}' not found"

    def test_parameters_exist(self):
        """S3 template should have expected parameters."""
        json_output = json.loads(s3_template.to_json())
        params = json_output["Parameters"]
        
        expected_params = ["StorageProfilesOverride", "ExistingTaskRoleArn"]
        for param in expected_params:
            assert param in params
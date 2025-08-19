import pytest
import json
import sys
import os
from unittest.mock import patch
# Add ECS source directory to path for imports
ecs_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'ecs')
if ecs_path not in sys.path:
    sys.path.insert(0, ecs_path)


class TestMigrationTemplateSimple:
    """Simplified test cases for the Migration CloudFormation template"""

    @pytest.fixture
    def minimal_config(self):
        """Minimal configuration for testing"""
        return {
            "images": {
                "migration": "public.ecr.aws/test/migration:latest"
            }
        }

    def test_load_defaults_function(self):
        """Test that the load_defaults function exists and is importable"""
        from lakerunner_migration import load_defaults
        assert callable(load_defaults)

    def test_template_object_exists(self):
        """Test that the template object exists and is valid"""
        from lakerunner_migration import t as template
        assert template is not None

    def test_template_generates_valid_json(self):
        """Test that the template generates valid JSON"""
        from lakerunner_migration import t as template
        
        template_json = template.to_json()
        # Should not raise an exception
        parsed = json.loads(template_json)
        assert isinstance(parsed, dict)

    def test_template_has_basic_sections(self):
        """Test that template has basic CloudFormation sections"""
        from lakerunner_migration import t as template
        
        template_dict = json.loads(template.to_json())
        
        # Check basic structure (not AWSTemplateFormatVersion as troposphere may not include it)
        assert "Description" in template_dict
        assert "Parameters" in template_dict
        assert "Resources" in template_dict

    def test_commoninfra_parameter_exists(self):
        """Test that CommonInfraStackName parameter exists for imports"""
        from lakerunner_migration import t as template
        
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]
        
        # Should have parameter for referencing the common infra stack
        assert "CommonInfraStackName" in parameters

    def test_has_migration_resources(self):
        """Test that migration-related resources exist"""
        from lakerunner_migration import t as template
        
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]
        
        # Should have some resources (at least one)
        assert len(resources) > 0

    def test_template_description(self):
        """Test that template has a description"""
        from lakerunner_migration import t as template
        
        template_dict = json.loads(template.to_json())
        
        # Should have a description
        assert "Description" in template_dict
        assert len(template_dict["Description"]) > 0

    def test_run_ecs_task_class_exists(self):
        """Test that the RunEcsTask custom resource class exists"""
        from lakerunner_migration import RunEcsTask
        assert RunEcsTask is not None
        assert hasattr(RunEcsTask, 'resource_type')
        assert RunEcsTask.resource_type == "Custom::RunEcsTask"
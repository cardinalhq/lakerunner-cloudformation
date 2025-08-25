import pytest
import json
import os
from unittest.mock import patch, mock_open
from cloud_radar.cf.unit import Template as CloudRadarTemplate


class TestMigrationTemplate:
    """Test cases for the Migration CloudFormation template"""

    @pytest.fixture
    def mock_config(self):
        """Mock configuration for testing"""
        return {
            "migration": {
                "image": "migration:latest",
                "cpu": 512,
                "memory": 1024
            },
            "container_images": {
                "migration": "public.ecr.aws/test/migration:latest"
            }
        }

    @patch('lakerunner_migration.load_defaults')
    def test_template_generation_with_mock_config(self, mock_load_defaults, mock_config):
        """Test that migration template can be generated with mocked configuration"""
        mock_load_defaults.return_value = mock_config
        
        # Import here to avoid issues with patching
        from lakerunner_migration import t as template
        
        template_dict = json.loads(template.to_json())
        
        assert "Resources" in template_dict
        assert "Parameters" in template_dict

    def test_template_is_valid_json(self):
        """Test that the template generates valid JSON"""
        from lakerunner_migration import t as template
        
        template_json = template.to_json()
        # Should not raise an exception
        parsed = json.loads(template_json)
        assert isinstance(parsed, dict)

    def test_template_has_required_sections(self):
        """Test that template has required CloudFormation sections"""
        from lakerunner_migration import t as template
        
        template_dict = json.loads(template.to_json())
        
        # Troposphere doesn't always include AWSTemplateFormatVersion
        assert "Description" in template_dict
        assert "Parameters" in template_dict
        assert "Resources" in template_dict

    def test_commoninfra_stack_parameter_exists(self):
        """Test that CommonInfraStackName parameter exists for imports"""
        from lakerunner_migration import t as template
        
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]
        
        # Should have parameter for referencing the common infra stack
        assert "CommonInfraStackName" in parameters

    def test_migration_task_definition_exists(self):
        """Test that migration task definition resource is created"""
        from lakerunner_migration import t as template
        
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]
        
        # Find ECS task definition for migration
        task_definitions = [
            name for name, resource in resources.items()
            if resource["Type"] == "AWS::ECS::TaskDefinition"
        ]
        
        assert len(task_definitions) > 0, "Migration task definition not found"

    def test_lambda_function_exists(self):
        """Test that Lambda function for running ECS task exists"""
        from lakerunner_migration import t as template
        
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]
        
        # Find Lambda function resources
        lambda_functions = [
            name for name, resource in resources.items()
            if resource["Type"] == "AWS::Lambda::Function"
        ]
        
        assert len(lambda_functions) > 0, "Lambda function for migration not found"

    def test_iam_roles_exist(self):
        """Test that IAM roles are created for migration tasks"""
        from lakerunner_migration import t as template
        
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]
        
        # Find IAM role resources
        iam_roles = [
            name for name, resource in resources.items()
            if resource["Type"] == "AWS::IAM::Role"
        ]
        
        assert len(iam_roles) > 0, "IAM roles for migration not found"

    def test_custom_resource_exists(self):
        """Test that custom resource for running migration exists"""
        from lakerunner_migration import t as template
        
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]
        
        # Find custom resources
        custom_resources = [
            name for name, resource in resources.items()
            if resource["Type"].startswith("Custom::")
        ]
        
        assert len(custom_resources) > 0, "Custom resource for migration not found"

    def test_log_groups_exist(self):
        """Test that CloudWatch log groups are created"""
        from lakerunner_migration import t as template
        
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]
        
        # Find log group resources
        log_groups = [
            name for name, resource in resources.items()
            if resource["Type"] == "AWS::Logs::LogGroup"
        ]
        
        assert len(log_groups) > 0, "Log groups for migration not found"

    def test_cloud_radar_validation_basic(self, sample_parameters):
        """Test basic template validation using Cloud-Radar"""
        from lakerunner_migration import t as template
        
        # Parameters for migration template
        test_params = sample_parameters.copy()
        test_params.update({
            "CommonInfraStackName": "test-common-infra"
        })
        
        # Get template JSON and replace ImportValue with static values for testing
        template_json = template.to_json()
        
        # Replace ImportValue functions with mock values for offline testing
        import re
        template_json = re.sub(
            r'"Fn::ImportValue":\s*{[^}]+}',
            '"mock-value"',
            template_json
        )
        
        # Create Cloud-Radar template
        cf_template = CloudRadarTemplate(
            template=json.loads(template_json)
        )
        
        # Validate template structure
        assert cf_template.template is not None
        assert "Resources" in cf_template.template

    def test_migration_container_definition_properties(self):
        """Test that migration container has required properties"""
        from lakerunner_migration import t as template
        
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]
        
        # Find task definition and check container definitions
        for name, resource in resources.items():
            if resource["Type"] == "AWS::ECS::TaskDefinition":
                properties = resource["Properties"]
                
                assert "ContainerDefinitions" in properties
                assert len(properties["ContainerDefinitions"]) > 0
                
                container = properties["ContainerDefinitions"][0]
                
                # Check essential container properties
                assert "Name" in container
                assert "Image" in container
                assert "Cpu" in container
                assert "Memory" in container
                
                break
        else:
            pytest.fail("No ECS TaskDefinition found to validate container properties")

    def test_migration_imports_match_common_exports(self):
        """Test that migration stack only imports values that are exported by common stack"""
        from lakerunner_migration import t as migration_template
        from lakerunner_common import t as common_template
        
        # Parse both templates
        migration_dict = json.loads(migration_template.to_json())
        common_dict = json.loads(common_template.to_json())
        
        # Extract all ImportValue references from migration template
        migration_imports = set()
        
        def extract_import_values(obj, stack_name_param="CommonInfraStackName"):
            """Recursively extract ImportValue references from template object"""
            if isinstance(obj, dict):
                # Handle Fn::ImportValue with Fn::Sub pattern
                if "Fn::ImportValue" in obj:
                    import_ref = obj["Fn::ImportValue"]
                    if isinstance(import_ref, dict) and "Fn::Sub" in import_ref:
                        sub_value = import_ref["Fn::Sub"]
                        if isinstance(sub_value, str) and f"${{{stack_name_param}}}" in sub_value:
                            # Extract the export name (part after the stack name)
                            export_name = sub_value.replace(f"${{{stack_name_param}}}-", "")
                            migration_imports.add(export_name)
                    elif isinstance(import_ref, str):
                        # Handle direct string imports (shouldn't be used but check anyway)
                        migration_imports.add(import_ref)
                
                # Recursively check all values
                for value in obj.values():
                    extract_import_values(value, stack_name_param)
            elif isinstance(obj, list):
                # Recursively check all list items
                for item in obj:
                    extract_import_values(item, stack_name_param)
        
        # Extract imports from migration template
        extract_import_values(migration_dict)
        
        # Extract all exports from common template
        common_exports = set()
        if "Outputs" in common_dict:
            for output_name, output_config in common_dict["Outputs"].items():
                if "Export" in output_config:
                    export_config = output_config["Export"]
                    if "Name" in export_config:
                        export_name = export_config["Name"]
                        # Handle Fn::Sub pattern in export names
                        if isinstance(export_name, dict) and "Fn::Sub" in export_name:
                            sub_value = export_name["Fn::Sub"]
                            if isinstance(sub_value, str) and "${AWS::StackName}-" in sub_value:
                                # Extract the export suffix (part after the stack name)
                                export_suffix = sub_value.replace("${AWS::StackName}-", "")
                                common_exports.add(export_suffix)
        
        # Validate that all migration imports have corresponding common exports
        missing_exports = migration_imports - common_exports
        
        # Print debug information
        print(f"Migration imports: {sorted(migration_imports)}")
        print(f"Common exports: {sorted(common_exports)}")
        print(f"Missing exports: {sorted(missing_exports)}")
        
        # Assert that there are no missing exports
        assert len(missing_exports) == 0, (
            f"Migration stack imports values that are not exported by common stack: {missing_exports}. "
            f"Migration imports: {migration_imports}, Common exports: {common_exports}"
        )
        
        # Ensure we found some imports and exports (sanity check)
        assert len(migration_imports) > 0, "No ImportValue references found in migration template"
        assert len(common_exports) > 0, "No exports found in common template"
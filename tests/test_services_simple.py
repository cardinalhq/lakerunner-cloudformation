import pytest
import json
from unittest.mock import patch


class TestServicesTemplateSimple:
    """Simplified test cases for the Services CloudFormation template that avoid complex template generation"""

    def test_load_service_config_function(self):
        """Test that the load_service_config function exists and is importable"""
        from lakerunner_ecs_services import load_service_config
        assert callable(load_service_config)

    def test_create_services_template_function(self):
        """Test that the create_services_template function exists and is importable"""
        from lakerunner_ecs_services import create_services_template
        assert callable(create_services_template)

    @patch('lakerunner_ecs_services.load_service_config')
    def test_template_generation_basic(self, mock_load_config):
        """Test basic template generation with minimal mock config"""
        # Minimal config that should not cause errors
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        from lakerunner_ecs_services import create_services_template
        
        # This should not raise an exception
        template = create_services_template()
        assert template is not None

    @patch('lakerunner_ecs_services.load_service_config')
    def test_template_has_basic_structure(self, mock_load_config):
        """Test that generated template has basic CloudFormation structure"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        from lakerunner_ecs_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        # Check basic CloudFormation structure
        assert "Description" in template_dict
        assert "Parameters" in template_dict
        assert "Resources" in template_dict

    @patch('lakerunner_ecs_services.load_service_config')
    def test_template_description_correct(self, mock_load_config):
        """Test that template description is correct"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        from lakerunner_ecs_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        expected_desc = "Lakerunner ECS Services: ECS services, task definitions, IAM roles, and ALB integration"
        assert template_dict["Description"] == expected_desc

    @patch('lakerunner_ecs_services.load_service_config')
    def test_infrastructure_parameters_exist(self, mock_load_config):
        """Test that infrastructure input parameters exist"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest",
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }

        from lakerunner_ecs_services import create_services_template

        template = create_services_template()
        params = json.loads(template.to_json())["Parameters"]

        required = {
            "ClusterArn", "DbSecretArn", "DbHost", "DbPort",
            "TaskSecurityGroupId", "VpcId", "PrivateSubnets",
            "PublicSubnets", "BucketArn", "EfsId"
        }
        assert required.issubset(params.keys())

    @patch('lakerunner_ecs_services.load_service_config')
    def test_image_parameters_exist(self, mock_load_config):
        """Test that container image parameters exist"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        from lakerunner_ecs_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        parameters = template_dict["Parameters"]
        
        # Should have image override parameters (Grafana moved to separate stack)
        # All services now use the unified GoServicesImage parameter
        expected_image_params = ["GoServicesImage"]
        for param in expected_image_params:
            assert param in parameters, f"Image parameter {param} not found"
import pytest
import json
import os
import sys
from unittest.mock import patch, mock_open
from cloud_radar.cf.unit import Template as CloudRadarTemplate
# Add ECS source directory to path for imports
ecs_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'ecs')
if ecs_path not in sys.path:
    sys.path.insert(0, ecs_path)


class TestServicesTemplate:
    """Test cases for the Services CloudFormation template"""

    @pytest.fixture
    def mock_config(self):
        """Mock configuration for testing"""
        return {
            "services": {
                "lakerunner-pubsub-sqs": {
                    "image": "public.ecr.aws/test/lakerunner:latest",
                    "command": ["/app/bin/lakerunner", "pubsub", "sqs"],
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 1,
                    "health_check": {
                        "type": "go",
                        "command": ["/app/bin/lakerunner", "sysinfo"]
                    },
                    "environment": {}
                },
                "lakerunner-query-api": {
                    "image": "public.ecr.aws/test/query-api:latest",
                    "cpu": 512,
                    "memory_mib": 1024,
                    "replicas": 1,
                    "health_check": {
                        "type": "http",
                        "path": "/api/health"
                    },
                    "ingress": {
                        "attach_alb": True,
                        "path": "/api/*",
                        "port": 3000
                    },
                    "environment": {
                        "TOKEN_HMAC256_KEY": "test-secret"
                    }
                }
            },
            "images": {
                "go_services": "public.ecr.aws/test/go:latest",
                "query_api": "public.ecr.aws/test/api:latest",
                "query_worker": "public.ecr.aws/test/worker:latest",
                "grafana": "public.ecr.aws/test/grafana:latest",
                "migration": "public.ecr.aws/test/migration:latest"
            }
        }

    @patch('lakerunner_services.load_service_config')
    def test_template_generation_with_mock_config(self, mock_load_config, mock_config):
        """Test that template can be generated with mocked configuration"""
        mock_load_config.return_value = mock_config
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        assert "Resources" in template_dict
        assert "Parameters" in template_dict
        assert "Outputs" in template_dict

    @patch('lakerunner_services.load_service_config')
    def test_template_description(self, mock_load_config, mock_config):
        """Test template description"""
        mock_load_config.return_value = mock_config
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        expected_desc = "Lakerunner Services: ECS services, task definitions, IAM roles, and ALB integration"
        assert template_dict["Description"] == expected_desc

    @patch('lakerunner_services.load_service_config')
    def test_import_parameters_exist(self, mock_load_config, mock_config):
        """Test that cross-stack import parameters are defined"""
        mock_load_config.return_value = mock_config
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        parameters = template_dict["Parameters"]
        
        # Should have CommonInfraStackName parameter for imports
        assert "CommonInfraStackName" in parameters

    @patch('lakerunner_services.load_service_config')
    def test_ecs_services_created(self, mock_load_config, mock_config):
        """Test that ECS services are created for each service in config"""
        mock_load_config.return_value = mock_config
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        resources = template_dict["Resources"]
        
        # Count ECS service resources
        ecs_services = [
            name for name, resource in resources.items()
            if resource["Type"] == "AWS::ECS::Service"
        ]
        
        # Should have services for each configured service
        expected_service_count = len(mock_config["services"])
        assert len(ecs_services) >= expected_service_count

    @patch('lakerunner_services.load_service_config')
    def test_task_definitions_created(self, mock_load_config, mock_config):
        """Test that task definitions are created"""
        mock_load_config.return_value = mock_config
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        resources = template_dict["Resources"]
        
        # Count task definition resources
        task_definitions = [
            name for name, resource in resources.items()
            if resource["Type"] == "AWS::ECS::TaskDefinition"
        ]
        
        # Should have task definitions for each service
        expected_count = len(mock_config["services"])
        assert len(task_definitions) >= expected_count

    @patch('lakerunner_services.load_service_config')
    def test_iam_roles_created(self, mock_load_config, mock_config):
        """Test that IAM roles are created for ECS tasks"""
        mock_load_config.return_value = mock_config
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        resources = template_dict["Resources"]
        
        # Count IAM role resources
        iam_roles = [
            name for name, resource in resources.items()
            if resource["Type"] == "AWS::IAM::Role"
        ]
        
        # Should have IAM roles for ECS tasks
        assert len(iam_roles) > 0

    @patch('lakerunner_services.load_service_config')
    def test_log_groups_created(self, mock_load_config, mock_config):
        """Test that CloudWatch log groups are created"""
        mock_load_config.return_value = mock_config
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        resources = template_dict["Resources"]
        
        # Count log group resources
        log_groups = [
            name for name, resource in resources.items()
            if resource["Type"] == "AWS::Logs::LogGroup"
        ]
        
        # Should have log groups for services
        assert len(log_groups) > 0

    @patch('lakerunner_services.load_service_config')
    def test_cloud_radar_validation(self, mock_load_config, mock_config, sample_parameters):
        """Test template validation using Cloud-Radar with mocked imports"""
        mock_load_config.return_value = mock_config
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        
        # Parameters for services template
        test_params = sample_parameters.copy()
        test_params.update({
            "CommonInfraStackName": "test-common-infra"
        })
        
        # Create mock context for Cloud-Radar to handle ImportValue functions
        template_json = template.to_json()
        
        # Replace ImportValue with static values for testing
        import re
        template_json = re.sub(
            r'"Fn::ImportValue":\s*{[^}]+}',
            '"vpc-12345678"',  # Mock VPC ID
            template_json
        )
        
        # Create Cloud-Radar template
        cf_template = CloudRadarTemplate(
            template=json.loads(template_json)
        )
        
        # Validate template structure
        assert cf_template.template is not None
        assert "Resources" in cf_template.template

    @patch('lakerunner_services.load_service_config')
    def test_alb_target_groups_for_ingress_services(self, mock_load_config, mock_config):
        """Test that ALB target groups are created for services with ALB ingress"""
        mock_load_config.return_value = mock_config
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        resources = template_dict["Resources"]
        
        # Count target group resources
        target_groups = [
            name for name, resource in resources.items()
            if resource["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"
        ]
        
        # Should have target groups for services with ALB attachment
        services_with_alb = [
            name for name, config in mock_config["services"].items()
            if config.get("ingress", {}).get("attach_alb", False)
        ]
        
        if services_with_alb:
            assert len(target_groups) >= len(services_with_alb)
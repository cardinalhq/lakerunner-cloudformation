import pytest
import json
from unittest.mock import patch


class TestServicesTemplateSimple:
    """Simplified test cases for the Services CloudFormation template that avoid complex template generation"""

    def test_load_service_config_function(self):
        """Test that the load_service_config function exists and is importable"""
        from lakerunner_services import load_service_config
        assert callable(load_service_config)

    def test_create_services_template_function(self):
        """Test that the create_services_template function exists and is importable"""
        from lakerunner_services import create_services_template
        assert callable(create_services_template)

    @patch('lakerunner_services.load_service_config')
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
        
        from lakerunner_services import create_services_template
        
        # This should not raise an exception
        template = create_services_template()
        assert template is not None

    @patch('lakerunner_services.load_service_config')
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
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        # Check basic CloudFormation structure
        assert "Description" in template_dict
        assert "Parameters" in template_dict
        assert "Resources" in template_dict

    @patch('lakerunner_services.load_service_config')
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
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        expected_desc = "Lakerunner Services: ECS services, task definitions, IAM roles, and ALB integration"
        assert template_dict["Description"] == expected_desc

    @patch('lakerunner_services.load_service_config')
    def test_commoninfra_parameter_exists(self, mock_load_config):
        """Test that CommonInfraStackName parameter exists"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        from lakerunner_services import create_services_template
        
        template = create_services_template()
        template_dict = json.loads(template.to_json())
        
        # Should have CommonInfraStackName parameter for imports
        assert "CommonInfraStackName" in template_dict["Parameters"]

    @patch('lakerunner_services.load_service_config')
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

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())

        parameters = template_dict["Parameters"]

        # Should have image override parameters (Grafana moved to separate stack)
        # All services now use unified GoServicesImage parameter
        expected_image_params = ["GoServicesImage"]
        for param in expected_image_params:
            assert param in parameters, f"Image parameter {param} not found"

    @patch('lakerunner_services.load_service_config')
    def test_signal_type_parameters_exist(self, mock_load_config):
        """Test that signal type parameters exist with correct defaults"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest",
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())

        parameters = template_dict["Parameters"]

        # Check that all signal type parameters exist
        assert "EnableLogs" in parameters, "EnableLogs parameter not found"
        assert "EnableMetrics" in parameters, "EnableMetrics parameter not found"
        assert "EnableTraces" in parameters, "EnableTraces parameter not found"

        # Check parameter types and allowed values
        for param_name in ["EnableLogs", "EnableMetrics", "EnableTraces"]:
            param = parameters[param_name]
            assert param["Type"] == "String", f"{param_name} should be String type"
            assert param["AllowedValues"] == ["Yes", "No"], f"{param_name} should have Yes/No values"

        # Check defaults
        assert parameters["EnableLogs"]["Default"] == "Yes", "EnableLogs should default to Yes"
        assert parameters["EnableMetrics"]["Default"] == "Yes", "EnableMetrics should default to Yes"
        assert parameters["EnableTraces"]["Default"] == "No", "EnableTraces should default to No"

    @patch('lakerunner_services.load_service_config')
    def test_signal_type_conditions_exist(self, mock_load_config):
        """Test that signal type conditions exist"""
        mock_load_config.return_value = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest",
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())

        conditions = template_dict.get("Conditions", {})

        # Check that all signal type conditions exist
        assert "CreateLogsServices" in conditions, "CreateLogsServices condition not found"
        assert "CreateMetricsServices" in conditions, "CreateMetricsServices condition not found"
        assert "CreateTracesServices" in conditions, "CreateTracesServices condition not found"

    @patch('lakerunner_services.load_service_config')
    def test_signal_type_service_creation(self, mock_load_config):
        """Test that services are conditionally created based on signal type"""
        mock_load_config.return_value = {
            "services": {
                "test-logs-service": {
                    "signal_type": "logs",
                    "command": ["/app/bin/lakerunner", "test"],
                    "cpu": 512,
                    "memory_mib": 1024,
                    "replicas": 1,
                    "health_check": {
                        "type": "go",
                        "command": ["/app/bin/lakerunner", "sysinfo"]
                    },
                    "environment": {}
                },
                "test-metrics-service": {
                    "signal_type": "metrics",
                    "command": ["/app/bin/lakerunner", "test"],
                    "cpu": 512,
                    "memory_mib": 1024,
                    "replicas": 1,
                    "health_check": {
                        "type": "go",
                        "command": ["/app/bin/lakerunner", "sysinfo"]
                    },
                    "environment": {}
                },
                "test-traces-service": {
                    "signal_type": "traces",
                    "command": ["/app/bin/lakerunner", "test"],
                    "cpu": 512,
                    "memory_mib": 1024,
                    "replicas": 1,
                    "health_check": {
                        "type": "go",
                        "command": ["/app/bin/lakerunner", "sysinfo"]
                    },
                    "environment": {}
                },
                "test-common-service": {
                    "signal_type": "common",
                    "command": ["/app/bin/lakerunner", "test"],
                    "cpu": 512,
                    "memory_mib": 1024,
                    "replicas": 1,
                    "health_check": {
                        "type": "go",
                        "command": ["/app/bin/lakerunner", "sysinfo"]
                    },
                    "environment": {}
                }
            },
            "images": {
                "go_services": "test:latest"
            }
        }

        from lakerunner_services import create_services_template

        template = create_services_template()
        template_dict = json.loads(template.to_json())

        resources = template_dict.get("Resources", {})

        # Check that services with signal types have conditions
        # LogGroups
        assert resources["LogGroupTestLogsService"].get("Condition") == "CreateLogsServices"
        assert resources["LogGroupTestMetricsService"].get("Condition") == "CreateMetricsServices"
        assert resources["LogGroupTestTracesService"].get("Condition") == "CreateTracesServices"
        assert "Condition" not in resources["LogGroupTestCommonService"]

        # TaskDefinitions
        assert resources["TaskDefTestLogsService"].get("Condition") == "CreateLogsServices"
        assert resources["TaskDefTestMetricsService"].get("Condition") == "CreateMetricsServices"
        assert resources["TaskDefTestTracesService"].get("Condition") == "CreateTracesServices"
        assert "Condition" not in resources["TaskDefTestCommonService"]

        # Services
        assert resources["ServiceTestLogsService"].get("Condition") == "CreateLogsServices"
        assert resources["ServiceTestMetricsService"].get("Condition") == "CreateMetricsServices"
        assert resources["ServiceTestTracesService"].get("Condition") == "CreateTracesServices"
        assert "Condition" not in resources["ServiceTestCommonService"]

        # Outputs
        outputs = template_dict.get("Outputs", {})
        assert outputs["ServiceTestLogsServiceArn"].get("Condition") == "CreateLogsServices"
        assert outputs["ServiceTestMetricsServiceArn"].get("Condition") == "CreateMetricsServices"
        assert outputs["ServiceTestTracesServiceArn"].get("Condition") == "CreateTracesServices"
        assert "Condition" not in outputs["ServiceTestCommonServiceArn"]
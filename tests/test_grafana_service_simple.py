#!/usr/bin/env python3
# Copyright (C) 2025 CardinalHQ, Inc
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import json
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# Add src directory to Python path so we can import the template modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

MOCK_CONFIG = {
    "grafana": {
        "replicas": 1,
        "environment": {
            "GF_SECURITY_ADMIN_USER": "lakerunner",
            "GF_SERVER_HTTP_PORT": "3000",
            "GF_PLUGINS_PREINSTALL_SYNC": "test-plugin@1.0.0@https://example.com/test-plugin.zip",
        },
        "health_check": {
            "command": ["curl", "-f", "http://localhost:3000/api/health"]
        }
    },
    "mcp_gateway": {
        "port": 8080,
        "environment": {
            "HOME": "/tmp",
            "AWS_REGION": "us-east-1"
        }
    },
    "conductor_server": {
        "port": 4100,
        "environment": {
            "MCP_GATEWAY_URL": "http://localhost:8080"
        }
    },
    "maestro_server": {
        "port": 3100,
        "environment": {
            "PORT": "3100",
            "MCP_GATEWAY_URL": "http://localhost:8080"
        }
    },
    "task": {
        "cpu": 2048,
        "memory_mib": 4096
    },
    "images": {
        "grafana": "test:latest",
        "grafana_init": "ghcr.io/cardinalhq/initcontainer-grafana:test",
        "mcp_gateway": "ghcr.io/cardinalhq/mcp-gateway:v0.2.0",
        "conductor_server": "ghcr.io/cardinalhq/conductor-server:v0.1.7",
        "maestro_server": "ghcr.io/cardinalhq/maestro:v0.1.0"
    },
    "api_keys": [{"keys": ["test-key"]}]
}


class TestGrafanaTemplateSimple(unittest.TestCase):
    """Simple smoke tests for Grafana template generation"""

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_load_grafana_config_function(self, mock_load_config):
        """Test that load_grafana_config function can be imported and called"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import load_grafana_config

        config = load_grafana_config()
        assert isinstance(config, dict)
        assert "grafana" in config
        mock_load_config.assert_called_once()

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_create_grafana_template_function(self, mock_load_config):
        """Test that create_grafana_template function can be imported and called"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        assert template is not None
        mock_load_config.assert_called_once()

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_template_generation_basic(self, mock_load_config):
        """Test basic template generation without errors"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()

        # Test that template can be converted to JSON without errors
        template_json = template.to_json()
        assert isinstance(template_json, str)

        # Test that JSON is valid
        template_dict = json.loads(template_json)
        assert isinstance(template_dict, dict)

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_template_has_basic_structure(self, mock_load_config):
        """Test that template has basic CloudFormation structure"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        # Should have basic CloudFormation sections
        assert "Parameters" in template_dict
        assert "Resources" in template_dict
        assert "Outputs" in template_dict
        assert "Conditions" in template_dict

        # Should have metadata
        assert "Metadata" in template_dict
        assert "AWS::CloudFormation::Interface" in template_dict["Metadata"]

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_template_description_correct(self, mock_load_config):
        """Test that template has correct description"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        assert "Grafana" in template_dict["Description"]
        assert "MCP Gateway" in template_dict["Description"]
        assert "Conductor Server" in template_dict["Description"]
        assert "Maestro" in template_dict["Description"]

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_required_parameters_exist(self, mock_load_config):
        """Test that required parameters exist"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        parameters = template_dict["Parameters"]

        # Infrastructure parameters
        assert "CommonInfraStackName" in parameters
        assert "QueryApiUrl" in parameters
        assert "AlbScheme" in parameters

        # AI configuration parameters
        assert "BedrockModel" in parameters
        assert "LakerunnerApiKey" in parameters
        assert "GrafanaResetToken" in parameters

        # Grafana images are fixed from config and not exposed as install parameters
        assert "GrafanaImage" not in parameters
        assert "GrafanaInitImage" not in parameters
        assert "McpGatewayImage" not in parameters
        assert "ConductorServerImage" not in parameters

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_grafana_resources_exist(self, mock_load_config):
        """Test that Grafana-specific resources exist"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        resources = template_dict["Resources"]

        # Core resources
        assert "GrafanaService" in resources
        assert "GrafanaTaskDef" in resources
        assert "GrafanaAlb" in resources
        assert "GrafanaTg" in resources
        assert "GrafanaSecret" in resources
        assert "GrafanaLogGroup" in resources

        # AI-related resources
        assert "McpGatewayLogGroup" in resources
        assert "ConductorServerLogGroup" in resources
        assert "MaestroServerLogGroup" in resources
        assert "AiInternalSecret" in resources
        assert "LakerunnerApiKeySecret" in resources

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_task_definition_has_all_containers(self, mock_load_config):
        """Test that task definition includes all five containers"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        task_def = template_dict["Resources"]["GrafanaTaskDef"]
        containers = task_def["Properties"]["ContainerDefinitions"]

        container_names = [c["Name"] for c in containers]
        assert "GrafanaInit" in container_names
        assert "McpGateway" in container_names
        assert "ConductorServer" in container_names
        assert "MaestroServer" in container_names
        assert "GrafanaContainer" in container_names
        assert len(containers) == 5

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_task_definition_resources(self, mock_load_config):
        """Test that task definition has correct CPU and memory"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        task_def = template_dict["Resources"]["GrafanaTaskDef"]["Properties"]
        assert task_def["Cpu"] == "2048"
        assert task_def["Memory"] == "4096"

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_task_definition_uses_literal_images(self, mock_load_config):
        """Test that Grafana task definition uses literal image strings, not parameters"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        containers = template_dict["Resources"]["GrafanaTaskDef"]["Properties"]["ContainerDefinitions"]
        images_by_name = {container["Name"]: container["Image"] for container in containers}
        for container_name in ["GrafanaInit", "McpGateway", "ConductorServer", "MaestroServer", "GrafanaContainer"]:
            image = images_by_name[container_name]
            assert isinstance(image, str)
            assert image
            assert "Ref" not in image
            assert "Fn::" not in image
            assert ":" in image

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_conductor_depends_on_mcp_gateway(self, mock_load_config):
        """Test that conductor-server depends on mcp-gateway being healthy"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        task_def = template_dict["Resources"]["GrafanaTaskDef"]
        containers = task_def["Properties"]["ContainerDefinitions"]

        conductor = next(c for c in containers if c["Name"] == "ConductorServer")
        depends_on = conductor["DependsOn"]

        # Should depend on init completing and mcp-gateway being healthy
        init_dep = next(d for d in depends_on if d["ContainerName"] == "GrafanaInit")
        assert init_dep["Condition"] == "SUCCESS"

        mcp_dep = next(d for d in depends_on if d["ContainerName"] == "McpGateway")
        assert mcp_dep["Condition"] == "HEALTHY"

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_bedrock_permissions_in_task_role(self, mock_load_config):
        """Test that task role includes Bedrock permissions for AI services"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        task_role = template_dict["Resources"]["GrafanaTaskRole"]
        policies = task_role["Properties"]["Policies"]
        policy_names = [p["PolicyName"] for p in policies]

        assert "BedrockAccess" in policy_names

        # Verify wildcard resource (matches Terraform config)
        bedrock_policy = next(p for p in policies if p["PolicyName"] == "BedrockAccess")
        statements = bedrock_policy["PolicyDocument"]["Statement"]
        assert statements[0]["Resource"] == "*"

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_grafana_outputs_exist(self, mock_load_config):
        """Test that Grafana outputs exist"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        outputs = template_dict["Outputs"]

        assert "GrafanaAlbDNS" in outputs
        assert "GrafanaServiceArn" in outputs
        assert "GrafanaAdminSecretArn" in outputs
        assert "GrafanaUrl" in outputs

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_conditions_exist(self, mock_load_config):
        """Test that conditions exist"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        conditions = template_dict["Conditions"]
        assert "IsInternetFacing" in conditions

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_ai_containers_run_as_nonroot(self, mock_load_config):
        """Test that AI sidecar containers run as non-root"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        task_def = template_dict["Resources"]["GrafanaTaskDef"]
        containers = task_def["Properties"]["ContainerDefinitions"]

        mcp_gw = next(c for c in containers if c["Name"] == "McpGateway")
        conductor = next(c for c in containers if c["Name"] == "ConductorServer")
        maestro = next(c for c in containers if c["Name"] == "MaestroServer")

        assert mcp_gw["User"] == "65532"
        assert conductor["User"] == "65532"
        assert maestro["User"] == "65532"

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_bedrock_model_parameter_has_allowed_values(self, mock_load_config):
        """Test that BedrockModel parameter has regional AllowedValues"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        bedrock_param = template_dict["Parameters"]["BedrockModel"]
        allowed = bedrock_param["AllowedValues"]
        assert "us.anthropic.claude-sonnet-4-6" in allowed
        assert "eu.anthropic.claude-sonnet-4-6" in allowed
        assert "global.anthropic.claude-sonnet-4-6" in allowed
        assert bedrock_param["Default"] == "us.anthropic.claude-sonnet-4-6"


if __name__ == '__main__':
    unittest.main()

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
    "task": {
        "cpu": 2048,
        "memory_mib": 4096
    },
    "images": {
        "grafana": "test:latest",
        "grafana_init": "ghcr.io/cardinalhq/initcontainer-grafana:test",
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

        template_json = template.to_json()
        assert isinstance(template_json, str)

        template_dict = json.loads(template_json)
        assert isinstance(template_dict, dict)

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_template_has_basic_structure(self, mock_load_config):
        """Test that template has basic CloudFormation structure"""
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        assert "Parameters" in template_dict
        assert "Resources" in template_dict
        assert "Outputs" in template_dict
        assert "Conditions" in template_dict

        assert "Metadata" in template_dict
        assert "AWS::CloudFormation::Interface" in template_dict["Metadata"]

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_template_description_correct(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        assert "Grafana" in template_dict["Description"]
        assert "MCP Gateway" not in template_dict["Description"]
        assert "Conductor" not in template_dict["Description"]
        assert "Maestro" not in template_dict["Description"]

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_required_parameters_exist(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        parameters = template_dict["Parameters"]

        assert "CommonInfraStackName" in parameters
        assert "QueryApiUrl" in parameters
        assert "AlbScheme" in parameters
        assert "LakerunnerApiKey" in parameters
        assert "GrafanaResetToken" in parameters

        # Removed — these should be gone
        assert "BedrockModel" not in parameters
        assert "GrafanaImage" not in parameters
        assert "GrafanaInitImage" not in parameters
        assert "McpGatewayImage" not in parameters
        assert "ConductorServerImage" not in parameters

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_grafana_resources_exist(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        resources = json.loads(template.to_json())["Resources"]

        # Kept
        assert "GrafanaService" in resources
        assert "GrafanaTaskDef" in resources
        assert "GrafanaAlb" in resources
        assert "GrafanaTg" in resources
        assert "GrafanaSecret" in resources
        assert "GrafanaLogGroup" in resources

        # Removed
        for name in ["McpGatewayLogGroup", "ConductorServerLogGroup",
                     "MaestroServerLogGroup", "AiInternalSecret",
                     "LakerunnerApiKeySecret"]:
            assert name not in resources, f"{name} should have been removed"

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_task_definition_has_only_init_and_grafana(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        containers = template_dict["Resources"]["GrafanaTaskDef"][
            "Properties"]["ContainerDefinitions"]
        names = [c["Name"] for c in containers]

        assert "GrafanaInit" in names
        assert "GrafanaContainer" in names
        assert "McpGateway" not in names
        assert "ConductorServer" not in names
        assert "MaestroServer" not in names
        assert len(containers) == 2

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
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        containers = template_dict["Resources"]["GrafanaTaskDef"][
            "Properties"]["ContainerDefinitions"]
        images_by_name = {c["Name"]: c["Image"] for c in containers}
        for name in ["GrafanaInit", "GrafanaContainer"]:
            image = images_by_name[name]
            assert isinstance(image, str) and image
            assert "Ref" not in image and "Fn::" not in image
            assert ":" in image

    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_grafana_outputs_exist(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        outputs = json.loads(template.to_json())["Outputs"]

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
    def test_datasource_ssm_param_uses_parameter_refs(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        resources = json.loads(template.to_json())["Resources"]

        ds = resources["GrafanaDatasourceConfig"]["Properties"]
        value = ds["Value"]
        # Value should be an Fn::Sub with vars QUERY_API_URL and LAKERUNNER_API_KEY
        assert "Fn::Sub" in value
        sub_args = value["Fn::Sub"]
        assert isinstance(sub_args, list) and len(sub_args) == 2
        template_str, var_map = sub_args
        assert "${QUERY_API_URL}" in template_str
        assert "${LAKERUNNER_API_KEY}" in template_str
        assert "QUERY_API_URL" in var_map
        assert "LAKERUNNER_API_KEY" in var_map


if __name__ == '__main__':
    unittest.main()

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

class TestGrafanaTemplateSimple(unittest.TestCase):
    """Simple smoke tests for Grafana template generation"""
    
    @patch('lakerunner_ecs_grafana.load_grafana_config')
    def test_load_grafana_config_function(self, mock_load_config):
        """Test that load_grafana_config function can be imported and called"""
        mock_load_config.return_value = {
            "grafana": {
                "cpu": 512,
                "memory_mib": 1024,
                "replicas": 1
            },
            "images": {"grafana": "test:latest"},
            "api_keys": []
        }
        
        from lakerunner_ecs_grafana import load_grafana_config
        
        config = load_grafana_config()
        assert isinstance(config, dict)
        assert "grafana" in config
        mock_load_config.assert_called_once()
    
    @patch('lakerunner_ecs_grafana.load_grafana_config')
    def test_create_grafana_template_function(self, mock_load_config):
        """Test that create_grafana_template function can be imported and called"""
        mock_load_config.return_value = {
            "grafana": {
                "cpu": 512,
                "memory_mib": 1024,
                "replicas": 1,
                "environment": {
                    "GF_SECURITY_ADMIN_USER": "lakerunner"
                }
            },
            "images": {"grafana": "test:latest"},
            "api_keys": [{"keys": ["test-key"]}]
        }
        
        from lakerunner_ecs_grafana import create_grafana_template
        
        template = create_grafana_template()
        assert template is not None
        mock_load_config.assert_called_once()
    
    @patch('lakerunner_ecs_grafana.load_grafana_config')
    def test_template_generation_basic(self, mock_load_config):
        """Test basic template generation without errors"""
        mock_load_config.return_value = {
            "grafana": {
                "cpu": 512,
                "memory_mib": 1024,
                "replicas": 1,
                "environment": {
                    "GF_SECURITY_ADMIN_USER": "lakerunner"
                },
                "health_check": {
                    "command": ["curl", "-f", "http://localhost:3000/api/health"]
                }
            },
            "images": {"grafana": "test:latest"},
            "api_keys": [{"keys": ["test-key"]}]
        }
        
        from lakerunner_ecs_grafana import create_grafana_template
        
        template = create_grafana_template()
        
        # Test that template can be converted to JSON without errors
        template_json = template.to_json()
        assert isinstance(template_json, str)
        
        # Test that JSON is valid
        template_dict = json.loads(template_json)
        assert isinstance(template_dict, dict)
    
    @patch('lakerunner_ecs_grafana.load_grafana_config')
    def test_template_has_basic_structure(self, mock_load_config):
        """Test that template has basic CloudFormation structure"""
        mock_load_config.return_value = {
            "grafana": {
                "cpu": 512,
                "memory_mib": 1024,
                "replicas": 1,
                "environment": {}
            },
            "images": {"grafana": "test:latest"},
            "api_keys": []
        }
        
        from lakerunner_ecs_grafana import create_grafana_template
        
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
    
    @patch('lakerunner_ecs_grafana.load_grafana_config')
    def test_template_description_correct(self, mock_load_config):
        """Test that template has correct description"""
        mock_load_config.return_value = {
            "grafana": {},
            "images": {"grafana": "test:latest"},
            "api_keys": []
        }
        
        from lakerunner_ecs_grafana import create_grafana_template
        
        template = create_grafana_template()
        template_dict = json.loads(template.to_json())
        
        expected_description = "Lakerunner ECS Grafana: Grafana service with ALB, PostgreSQL storage, and datasource configuration"
        assert template_dict["Description"] == expected_description
    
    @patch('lakerunner_ecs_grafana.load_grafana_config')
    def test_required_parameters_exist(self, mock_load_config):
        """Test that required parameters exist"""
        mock_load_config.return_value = {
            "grafana": {},
            "images": {"grafana": "test:latest"},
            "api_keys": []
        }
        
        from lakerunner_ecs_grafana import create_grafana_template
        
        template = create_grafana_template()
        template_dict = json.loads(template.to_json())
        
        parameters = template_dict["Parameters"]
        
        # Should have required stack name parameters
        assert "CommonInfraStackName" in parameters
        assert "ServicesStackName" in parameters
        
        # Should have Grafana-specific parameters
        assert "GrafanaImage" in parameters
        assert "GrafanaInitImage" in parameters
        assert "AlbScheme" in parameters
    
    @patch('lakerunner_ecs_grafana.load_grafana_config')
    def test_grafana_resources_exist(self, mock_load_config):
        """Test that Grafana-specific resources exist"""
        mock_load_config.return_value = {
            "grafana": {
                "cpu": 512,
                "memory_mib": 1024,
                "replicas": 1,
                "environment": {}
            },
            "images": {"grafana": "test:latest"},
            "api_keys": []
        }
        
        from lakerunner_ecs_grafana import create_grafana_template
        
        template = create_grafana_template()
        template_dict = json.loads(template.to_json())
        
        resources = template_dict["Resources"]
        
        # Should have Grafana-specific resources
        assert "GrafanaService" in resources
        assert "GrafanaTaskDef" in resources
        assert "GrafanaAlb" in resources
        assert "GrafanaTg" in resources
        assert "GrafanaSecret" in resources
        # Note: EFS access point removed, using PostgreSQL now
        assert "GrafanaLogGroup" in resources
    
    @patch('lakerunner_ecs_grafana.load_grafana_config')
    def test_grafana_outputs_exist(self, mock_load_config):
        """Test that Grafana outputs exist"""
        mock_load_config.return_value = {
            "grafana": {},
            "images": {"grafana": "test:latest"},
            "api_keys": []
        }
        
        from lakerunner_ecs_grafana import create_grafana_template
        
        template = create_grafana_template()
        template_dict = json.loads(template.to_json())
        
        outputs = template_dict["Outputs"]
        
        # Should have Grafana outputs
        assert "GrafanaAlbDNS" in outputs
        assert "GrafanaServiceArn" in outputs
        assert "GrafanaAdminSecretArn" in outputs
        assert "GrafanaUrl" in outputs

if __name__ == '__main__':
    unittest.main()
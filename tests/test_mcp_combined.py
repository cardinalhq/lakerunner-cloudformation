import pytest
import json
from unittest.mock import patch


class TestMcpCombinedTemplate:
    """Test cases for the MCP Combined CloudFormation template"""

    def test_load_mcp_config_function(self):
        """Test that the load_mcp_config function exists and is importable"""
        from lakerunner_mcp_combined import load_mcp_config
        assert callable(load_mcp_config)

    def test_create_mcp_combined_template_function(self):
        """Test that the create_mcp_combined_template function exists and is importable"""
        from lakerunner_mcp_combined import create_mcp_combined_template
        assert callable(create_mcp_combined_template)

    @patch('lakerunner_mcp_combined.load_mcp_config')
    def test_template_generation_basic(self, mock_load_config):
        """Test basic template generation with minimal mock config"""
        # Minimal config that should not cause errors
        mock_load_config.return_value = {
            "mcp_combined": {
                "cpu": 512,
                "memory_mib": 1024,
                "replicas": 2,
                "ports": {
                    "mcp": 8080,
                    "local_api": 20202
                },
                "environment": {
                    "MCP_HOST": "0.0.0.0",
                    "MCP_TRANSPORT": "http",
                    "LAKERUNNER_STREAM_ATTRIBUTE": "resource_service_name",
                    "WORKING_DIRECTORY": "/app/workdir",
                    "GIN_MODE": "release",
                    "AWS_REGION": "us-east-1"
                }
            },
            "images": {
                "mcp_combined": "docker.flame.org/library/lakerunner-mcp-combined:latest"
            }
        }

        from lakerunner_mcp_combined import create_mcp_combined_template

        # This should not raise an exception
        template = create_mcp_combined_template()
        assert template is not None

        # Convert to YAML to ensure it's valid
        yaml_output = template.to_yaml()
        assert yaml_output is not None
        assert len(yaml_output) > 0

    def test_template_has_required_parameters(self):
        """Test that the template has all required parameters"""
        from lakerunner_mcp_combined import create_mcp_combined_template
        from unittest.mock import patch

        mock_config = {
            "mcp_combined": {
                "cpu": 512,
                "memory_mib": 1024,
                "replicas": 2,
                "ports": {
                    "mcp": 8080,
                    "local_api": 20202
                },
                "environment": {}
            },
            "images": {
                "mcp_combined": "test:latest"
            }
        }

        with patch('lakerunner_mcp_combined.load_mcp_config', return_value=mock_config):
            template = create_mcp_combined_template()
            yaml_output = template.to_yaml()

            # Check for required parameters
            assert 'CommonInfraStackName' in yaml_output
            assert 'QueryApiUrl' in yaml_output
            assert 'LakerunnerApiKey' in yaml_output
            assert 'McpCombinedImage' in yaml_output
            assert 'AlbScheme' in yaml_output
            assert 'McpApiKey' in yaml_output

    def test_template_has_required_resources(self):
        """Test that the template has all required resources"""
        from lakerunner_mcp_combined import create_mcp_combined_template
        from unittest.mock import patch

        mock_config = {
            "mcp_combined": {
                "cpu": 512,
                "memory_mib": 1024,
                "replicas": 2,
                "ports": {
                    "mcp": 8080,
                    "local_api": 20202
                },
                "environment": {}
            },
            "images": {
                "mcp_combined": "test:latest"
            }
        }

        with patch('lakerunner_mcp_combined.load_mcp_config', return_value=mock_config):
            template = create_mcp_combined_template()
            yaml_output = template.to_yaml()

            # Check for required resources
            assert 'McpAlbSecurityGroup' in yaml_output
            assert 'McpAlb' in yaml_output
            assert 'McpTg' in yaml_output
            assert 'LocalApiTg' in yaml_output
            assert 'McpListener' in yaml_output
            assert 'LocalApiListener' in yaml_output
            assert 'McpExecRole' in yaml_output
            assert 'McpTaskRole' in yaml_output
            assert 'McpTaskDef' in yaml_output
            assert 'McpService' in yaml_output
            assert 'LakerunnerApiKeySecret' in yaml_output

    def test_template_has_required_outputs(self):
        """Test that the template has all required outputs"""
        from lakerunner_mcp_combined import create_mcp_combined_template
        from unittest.mock import patch

        mock_config = {
            "mcp_combined": {
                "cpu": 512,
                "memory_mib": 1024,
                "replicas": 2,
                "ports": {
                    "mcp": 8080,
                    "local_api": 20202
                },
                "environment": {}
            },
            "images": {
                "mcp_combined": "test:latest"
            }
        }

        with patch('lakerunner_mcp_combined.load_mcp_config', return_value=mock_config):
            template = create_mcp_combined_template()
            yaml_output = template.to_yaml()

            # Check for required outputs
            assert 'McpAlbDNS' in yaml_output
            assert 'McpAlbArn' in yaml_output
            assert 'McpServiceArn' in yaml_output
            assert 'McpUrl' in yaml_output
            assert 'LocalApiUrl' in yaml_output
            assert 'TaskRoleArn' in yaml_output
            assert 'BedrockPermissionsPolicy' in yaml_output
            assert 'BedrockModelsUsed' in yaml_output

    def test_template_has_bedrock_permissions(self):
        """Test that the task role has required Bedrock permissions"""
        from lakerunner_mcp_combined import create_mcp_combined_template
        from unittest.mock import patch

        mock_config = {
            "mcp_combined": {
                "cpu": 512,
                "memory_mib": 1024,
                "replicas": 2,
                "ports": {
                    "mcp": 8080,
                    "local_api": 20202
                },
                "environment": {}
            },
            "images": {
                "mcp_combined": "test:latest"
            }
        }

        with patch('lakerunner_mcp_combined.load_mcp_config', return_value=mock_config):
            template = create_mcp_combined_template()
            yaml_output = template.to_yaml()

            # Check that the task role has Bedrock permissions
            assert 'bedrock:InvokeModel' in yaml_output
            assert 'bedrock:InvokeModelWithResponseStream' in yaml_output
            assert 'amazon.titan-embed-text-v2:0' in yaml_output
            assert 'us.anthropic.claude-sonnet-4-5-' in yaml_output

    def test_template_has_dual_ports(self):
        """Test that the template configures both MCP and local-api ports"""
        from lakerunner_mcp_combined import create_mcp_combined_template
        from unittest.mock import patch

        mock_config = {
            "mcp_combined": {
                "cpu": 512,
                "memory_mib": 1024,
                "replicas": 2,
                "ports": {
                    "mcp": 8080,
                    "local_api": 20202
                },
                "environment": {}
            },
            "images": {
                "mcp_combined": "test:latest"
            }
        }

        with patch('lakerunner_mcp_combined.load_mcp_config', return_value=mock_config):
            template = create_mcp_combined_template()
            yaml_output = template.to_yaml()

            # Check that both ports are configured
            assert '8080' in yaml_output  # MCP port
            assert '20202' in yaml_output  # local-api port

    def test_template_has_conditions(self):
        """Test that the template has required conditions"""
        from lakerunner_mcp_combined import create_mcp_combined_template
        from unittest.mock import patch

        mock_config = {
            "mcp_combined": {
                "cpu": 512,
                "memory_mib": 1024,
                "replicas": 2,
                "ports": {
                    "mcp": 8080,
                    "local_api": 20202
                },
                "environment": {}
            },
            "images": {
                "mcp_combined": "test:latest"
            }
        }

        with patch('lakerunner_mcp_combined.load_mcp_config', return_value=mock_config):
            template = create_mcp_combined_template()
            yaml_output = template.to_yaml()

            # Check for conditions
            assert 'IsInternetFacing' in yaml_output
            assert 'HasMcpApiKey' in yaml_output

    def test_template_exports_task_role_arn(self):
        """Test that the template exports the task role ARN for additional permissions"""
        from lakerunner_mcp_combined import create_mcp_combined_template
        from unittest.mock import patch

        mock_config = {
            "mcp_combined": {
                "cpu": 512,
                "memory_mib": 1024,
                "replicas": 2,
                "ports": {
                    "mcp": 8080,
                    "local_api": 20202
                },
                "environment": {}
            },
            "images": {
                "mcp_combined": "test:latest"
            }
        }

        with patch('lakerunner_mcp_combined.load_mcp_config', return_value=mock_config):
            template = create_mcp_combined_template()
            yaml_output = template.to_yaml()

            # Check that TaskRoleArn is exported
            assert 'TaskRoleArn' in yaml_output
            assert 'Export' in yaml_output

import pytest
import json
from unittest.mock import patch


class TestDemoOtelCollectorTemplate:
    """Test cases for the Demo OTEL Collector CloudFormation template"""

    def test_load_otel_config_function(self):
        """Test that the load_otel_config function exists and is importable"""
        from demo_otel_collector import load_otel_config
        assert callable(load_otel_config)

    def test_load_default_otel_yaml_function(self):
        """Test that the load_default_otel_yaml function exists and is importable"""
        from demo_otel_collector import load_default_otel_yaml
        assert callable(load_default_otel_yaml)

    def test_create_otel_collector_template_function(self):
        """Test that the create_otel_collector_template function exists and is importable"""
        from demo_otel_collector import create_otel_collector_template
        assert callable(create_otel_collector_template)

    @patch('demo_otel_collector.load_otel_config')
    @patch('demo_otel_collector.load_default_otel_yaml')
    def test_template_generation_basic(self, mock_load_yaml, mock_load_config):
        """Test basic template generation with minimal mock config"""
        # Mock the OTEL config YAML
        mock_load_yaml.return_value = """
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 10s

exporters:
  awss3logs:
    s3uploader:
      region: ${env:AWS_REGION}
      s3_bucket: ${env:AWS_S3_BUCKET}
      s3_prefix: raw-logs/

service:
  pipelines:
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [awss3logs]
"""

        # Minimal config that should not cause errors
        mock_load_config.return_value = {
            "otel_services": {
                "otel-gateway": {
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 1,
                    "environment": {}
                }
            },
            "images": {
                "otel_collector": "test:latest"
            }
        }

        from demo_otel_collector import create_otel_collector_template
        
        # This should not raise an exception
        template = create_otel_collector_template()
        assert template is not None
        
        # Convert to YAML to ensure it's valid
        yaml_output = template.to_yaml()
        assert yaml_output is not None
        assert len(yaml_output) > 0

    def test_template_has_required_parameters(self):
        """Test that the template has all required parameters"""
        from demo_otel_collector import create_otel_collector_template
        from unittest.mock import patch, mock_open
        
        mock_config = {
            "otel_services": {
                "otel-gateway": {
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 1,
                    "environment": {}
                }
            },
            "images": {
                "otel_collector": "test:latest"
            }
        }
        
        mock_yaml_content = "test: config"
        
        with patch('demo_otel_collector.load_otel_config', return_value=mock_config), \
             patch('demo_otel_collector.load_default_otel_yaml', return_value=mock_yaml_content):
            
            template = create_otel_collector_template()
            yaml_output = template.to_yaml()
            
            # Check for required parameters
            assert 'CommonInfraStackName' in yaml_output
            assert 'LoadBalancerType' in yaml_output
            assert 'OtelCollectorImage' in yaml_output
            assert 'OrganizationId' in yaml_output
            assert 'CollectorName' in yaml_output
            assert 'OtelConfigYaml' in yaml_output

    def test_template_has_required_resources(self):
        """Test that the template has all required resources"""
        from demo_otel_collector import create_otel_collector_template
        from unittest.mock import patch
        
        mock_config = {
            "otel_services": {
                "otel-gateway": {
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 1,
                    "environment": {}
                }
            },
            "images": {
                "otel_collector": "test:latest"
            }
        }
        
        mock_yaml_content = "test: config"
        
        with patch('demo_otel_collector.load_otel_config', return_value=mock_config), \
             patch('demo_otel_collector.load_default_otel_yaml', return_value=mock_yaml_content):
            
            template = create_otel_collector_template()
            yaml_output = template.to_yaml()
            
            # Check for required resources
            assert 'AlbSecurityGroup' in yaml_output
            assert 'TaskSecurityGroup' in yaml_output
            assert 'ApplicationLoadBalancer' in yaml_output
            assert 'OtelGrpcTargetGroup' in yaml_output
            assert 'OtelHttpTargetGroup' in yaml_output
            assert 'TaskDefOtelGateway' in yaml_output
            assert 'ServiceOtelGateway' in yaml_output
            assert 'ExecRole' in yaml_output
            assert 'TaskRole' in yaml_output

    def test_template_has_required_outputs(self):
        """Test that the template has all required outputs"""
        from demo_otel_collector import create_otel_collector_template
        from unittest.mock import patch
        
        mock_config = {
            "otel_services": {
                "otel-gateway": {
                    "cpu": 1024,
                    "memory_mib": 2048,
                    "replicas": 1,
                    "environment": {}
                }
            },
            "images": {
                "otel_collector": "test:latest"
            }
        }
        
        mock_yaml_content = "test: config"
        
        with patch('demo_otel_collector.load_otel_config', return_value=mock_config), \
             patch('demo_otel_collector.load_default_otel_yaml', return_value=mock_yaml_content):
            
            template = create_otel_collector_template()
            yaml_output = template.to_yaml()
            
            # Check for required outputs
            assert 'LoadBalancerDNS' in yaml_output
            assert 'GrpcEndpoint' in yaml_output
            assert 'HttpEndpoint' in yaml_output
            assert 'ServiceArn' in yaml_output
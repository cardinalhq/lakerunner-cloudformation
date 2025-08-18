#!/usr/bin/env python3

import unittest
import sys
import os

# Add src to path so we can import our modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from lakerunner_grafana_setup import create_grafana_setup_template

class TestGrafanaSetupTemplate(unittest.TestCase):
    """Test cases for the Grafana Setup CloudFormation template"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.template = create_grafana_setup_template()
        
    def test_template_creation(self):
        """Test that the template can be created without errors"""
        self.assertIsNotNone(self.template)
        
    def test_template_yaml_generation(self):
        """Test that the template can generate valid YAML"""
        yaml_output = self.template.to_yaml()
        self.assertIsInstance(yaml_output, str)
        self.assertGreater(len(yaml_output), 0)
        
    def test_has_required_parameters(self):
        """Test that the template has required parameters"""
        parameters = self.template.parameters
        
        # Check for required parameters
        required_params = [
            'CommonInfraStackName',
            'GrafanaDbName', 
            'GrafanaDbUser'
        ]
        
        for param in required_params:
            self.assertIn(param, parameters, f"Missing required parameter: {param}")
            
    def test_has_required_resources(self):
        """Test that the template has required resources"""
        resources = self.template.resources
        
        # Check for required resources
        required_resources = [
            'GrafanaDbSecret',
            'CreateDbFunction',
            'CreateDbFunctionRole', 
            'CreateDbLogGroup',
            'GrafanaDbSetup'
        ]
        
        for resource in required_resources:
            self.assertIn(resource, resources, f"Missing required resource: {resource}")
            
    def test_has_required_outputs(self):
        """Test that the template has required outputs"""
        outputs = self.template.outputs
        
        # Check for required outputs
        required_outputs = [
            'GrafanaDbSecretArn',
            'GrafanaDbName',
            'GrafanaDbUser',
            'GrafanaDbHost',
            'GrafanaDbPort'
        ]
        
        for output in required_outputs:
            self.assertIn(output, outputs, f"Missing required output: {output}")
            
    def test_secret_has_proper_structure(self):
        """Test that the Grafana database secret has proper configuration"""
        secret = self.template.resources['GrafanaDbSecret']
        
        # Check that secret has GenerateSecretString
        self.assertIn('GenerateSecretString', secret.properties)
        secret_config = secret.properties['GenerateSecretString']
        
        # Check that it excludes special characters
        self.assertTrue(hasattr(secret_config, 'ExcludeCharacters'))
        # Check that it has proper password length
        self.assertTrue(hasattr(secret_config, 'PasswordLength'))
        self.assertEqual(secret_config.PasswordLength, 32)
        
    def test_lambda_function_has_vpc_config(self):
        """Test that the Lambda function has VPC configuration"""
        function = self.template.resources['CreateDbFunction']
        
        # Check that function has VpcConfig
        self.assertIn('VpcConfig', function.properties)
        vpc_config = function.properties['VpcConfig']
        
        # Check that VPC config has SecurityGroupIds and SubnetIds
        self.assertTrue(hasattr(vpc_config, 'SecurityGroupIds'))
        self.assertTrue(hasattr(vpc_config, 'SubnetIds'))
        
    def test_lambda_function_timeout(self):
        """Test that the Lambda function has appropriate timeout"""
        function = self.template.resources['CreateDbFunction']
        
        # Check timeout is set appropriately for database operations
        self.assertIn('Timeout', function.properties)
        self.assertEqual(function.properties['Timeout'], 300)  # 5 minutes
        
    def test_custom_resource_properties(self):
        """Test that the custom resource has required properties"""
        custom_resource = self.template.resources['GrafanaDbSetup']
        
        # Check for required properties
        required_props = [
            'ServiceToken',
            'MainDbSecretArn',
            'GrafanaDbSecretArn', 
            'DbHost',
            'DbPort',
            'GrafanaDbName',
            'GrafanaDbUser'
        ]
        
        for prop in required_props:
            self.assertIn(prop, custom_resource.properties, f"Missing custom resource property: {prop}")
            
    def test_exports_have_proper_names(self):
        """Test that exports have proper naming convention"""
        outputs = self.template.outputs
        
        # Check that all outputs have exports
        for output_name, output_obj in outputs.items():
            self.assertTrue(hasattr(output_obj, 'Export'), f"Output {output_name} should have an export")

if __name__ == '__main__':
    unittest.main()
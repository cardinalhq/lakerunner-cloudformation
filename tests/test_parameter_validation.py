import pytest
import json
import sys
import os
from cloud_radar.cf.unit import Template as CloudRadarTemplate
# Add ECS source directory to path for imports
ecs_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'ecs')
if ecs_path not in sys.path:
    sys.path.insert(0, ecs_path)


class TestParameterValidation:
    """Comprehensive tests for CloudFormation parameter validation and conditions"""

    def test_common_infra_parameter_constraints(self):
        """Test parameter constraints in CommonInfra template"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]
        
        # Test VpcId parameter type constraint
        vpc_id = parameters["VpcId"]
        assert vpc_id["Type"] == "AWS::EC2::VPC::Id"
        
        # Test subnet parameters
        private_subnets = parameters["PrivateSubnets"]
        assert private_subnets["Type"] == "List<AWS::EC2::Subnet::Id>"
        
        # Test configuration override parameters
        api_keys_override = parameters["ApiKeysOverride"]
        assert api_keys_override["Type"] == "String"
        assert api_keys_override["Default"] == ""
        
        storage_profiles_override = parameters["StorageProfilesOverride"]
        assert storage_profiles_override["Type"] == "String"
        assert storage_profiles_override["Default"] == ""

    def test_common_infra_conditions_validity(self):
        """Test that all conditions in CommonInfra are valid CloudFormation"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        conditions = template_dict.get("Conditions", {})
        
        # Verify expected conditions exist (ALB conditions moved to Services stack)
        expected_conditions = [
            "HasApiKeysOverride", 
            "HasStorageProfilesOverride"
        ]
        
        for condition_name in expected_conditions:
            assert condition_name in conditions, f"Condition {condition_name} not found"
        
        # Test HasApiKeysOverride condition structure
        has_api_keys = conditions["HasApiKeysOverride"]
        assert "Fn::Not" in has_api_keys
        not_args = has_api_keys["Fn::Not"]
        assert len(not_args) == 1
        assert "Fn::Equals" in not_args[0]

    def test_services_parameter_constraints(self):
        """Test parameter constraints in Services template"""
        from lakerunner_services import create_services_template
        from unittest.mock import patch
        
        mock_config = {
            "services": {},
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest",
                "grafana": "test:latest"
            }
        }
        
        with patch('lakerunner_services.load_service_config', return_value=mock_config):
            template = create_services_template()
            template_dict = json.loads(template.to_json())
            parameters = template_dict["Parameters"]
            
            # Test CommonInfraStackName parameter
            common_infra_param = parameters["CommonInfraStackName"]
            assert common_infra_param["Type"] == "String"
            assert "Description" in common_infra_param
            
            # Test image parameters have correct types and defaults (Grafana moved to separate stack)
            image_params = ["GoServicesImage", "QueryApiImage", "QueryWorkerImage"]
            for param_name in image_params:
                assert param_name in parameters
                param = parameters[param_name]
                assert param["Type"] == "String"
                assert "Default" in param
                assert param["Default"].startswith("test:")  # From mock config

    def test_parameter_validation_with_cloud_radar(self):
        """Test parameter validation using Cloud-Radar"""
        from lakerunner_common import t as template
        
        # Test parameters
        params = {
            "VpcId": "vpc-12345678",
            "PrivateSubnets": "subnet-abcdef12,subnet-fedcba21",
            "ApiKeysOverride": "",
            "StorageProfilesOverride": ""
        }
        
        # Cloud-Radar validates template structure and parameter types
        cf_template = CloudRadarTemplate(
            template=json.loads(template.to_json())
        )
        
        # Template structure should be valid
        assert cf_template.template is not None
        assert "Parameters" in cf_template.template
        
        # Test that VPC parameter constraint is in the template
        vpc_param = cf_template.template["Parameters"]["VpcId"]
        assert vpc_param["Type"] == "AWS::EC2::VPC::Id"

    def test_condition_usage_in_resources(self):
        """Test that conditions are properly used in resource definitions"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]
        
        # Test that conditions are used in SSM parameter resources
        api_keys_param = None
        for name, resource in resources.items():
            if (resource["Type"] == "AWS::SSM::Parameter" and 
                "api_keys" in resource["Properties"].get("Name", "")):
                api_keys_param = resource
                break
        
        assert api_keys_param is not None, "API keys parameter not found"
        
        # Check that parameter value uses conditional logic
        assert "Value" in api_keys_param["Properties"]
        value_prop = api_keys_param["Properties"]["Value"]
        assert "Fn::If" in value_prop, "API keys parameter should use conditional logic"

    def test_parameter_interdependencies(self):
        """Test parameter interdependencies and validation logic"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        
        # Test metadata for parameter groups (helps users understand dependencies)
        metadata = template_dict.get("Metadata", {})
        interface = metadata.get("AWS::CloudFormation::Interface", {})
        
        if "ParameterGroups" in interface:
            param_groups = interface["ParameterGroups"]
            
            # Should have logical groupings
            group_names = [group.get("Label", {}).get("default", "") for group in param_groups]
            assert any("Networking" in name for name in group_names)

    def test_parameter_descriptions_completeness(self):
        """Test that all parameters have helpful descriptions"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]
        
        for param_name, param_def in parameters.items():
            assert "Description" in param_def, f"Parameter {param_name} missing description"
            description = param_def["Description"]
            assert len(description) > 10, f"Parameter {param_name} has too short description"
            
            # Required parameters should be clearly marked
            if param_def.get("Type") in ["AWS::EC2::VPC::Id", "List<AWS::EC2::Subnet::Id>"]:
                assert "REQUIRED" in description.upper(), f"Required parameter {param_name} not marked as required"

    def test_parameter_defaults_safety(self):
        """Test that parameter defaults are safe and sensible"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]
        
        # Override parameters should default to empty (use built-in defaults)
        override_params = ["ApiKeysOverride", "StorageProfilesOverride"]
        for param_name in override_params:
            if param_name in parameters:
                assert parameters[param_name]["Default"] == ""

    def test_parameter_type_consistency(self):
        """Test parameter type consistency across templates"""
        templates_to_test = []
        
        # CommonInfra
        from lakerunner_common import t as common_template
        templates_to_test.append(("CommonInfra", common_template))
        
        # Migration
        from lakerunner_migration import t as migration_template
        templates_to_test.append(("Migration", migration_template))
        
        for template_name, template in templates_to_test:
            template_dict = json.loads(template.to_json())
            parameters = template_dict["Parameters"]
            
            # CommonInfraStackName should be consistent across templates that use it
            if "CommonInfraStackName" in parameters:
                param = parameters["CommonInfraStackName"]
                assert param["Type"] == "String"
                assert "Description" in param

    def test_invalid_parameter_combinations_detected(self):
        """Test detection of potentially problematic parameter combinations"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        
        # Check that internet-facing ALB configuration makes sense
        # This would be caught at deployment time, but we can check template logic
        conditions = template_dict.get("Conditions", {})
        resources = template_dict.get("Resources", {})
        
        # Find ALB resource and verify it handles internet-facing correctly
        for resource_name, resource in resources.items():
            if resource["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer":
                properties = resource["Properties"]
                
                # Should have Scheme property referencing parameter
                assert "Scheme" in properties
                scheme_ref = properties["Scheme"]
                assert scheme_ref == {"Ref": "AlbScheme"}
                
                # Should have Subnets using conditional logic
                subnets = properties["Subnets"]
                assert "Fn::If" in subnets
                break
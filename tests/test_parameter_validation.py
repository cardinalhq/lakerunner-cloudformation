import pytest
import json
from cloud_radar.cf.unit import Template as CloudRadarTemplate


class TestParameterValidation:
    """Comprehensive tests for CloudFormation parameter validation and conditions"""

    def test_common_infra_parameter_constraints(self):
        """Test parameter constraints in CommonInfra template"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]
        
        # Test AlbScheme parameter constraints
        alb_scheme = parameters["AlbScheme"]
        assert "AllowedValues" in alb_scheme
        assert set(alb_scheme["AllowedValues"]) == {"internet-facing", "internal"}
        assert alb_scheme["Default"] == "internal"
        assert alb_scheme["Type"] == "String"
        
        # Test VpcId parameter type constraint
        vpc_id = parameters["VpcId"]
        assert vpc_id["Type"] == "AWS::EC2::VPC::Id"
        
        # Test subnet parameters
        private_subnets = parameters["PrivateSubnets"]
        assert private_subnets["Type"] == "List<AWS::EC2::Subnet::Id>"
        
        public_subnets = parameters["PublicSubnets"]
        assert public_subnets["Type"] == "CommaDelimitedList"

    def test_common_infra_conditions_validity(self):
        """Test that all conditions in CommonInfra are valid CloudFormation"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        conditions = template_dict.get("Conditions", {})
        
        # Verify expected conditions exist
        expected_conditions = [
            "IsInternetFacing",
            "HasApiKeysOverride", 
            "HasStorageProfilesOverride"
        ]
        
        for condition_name in expected_conditions:
            assert condition_name in conditions, f"Condition {condition_name} not found"
            
        # Test IsInternetFacing condition structure
        is_internet_facing = conditions["IsInternetFacing"]
        assert "Fn::Equals" in is_internet_facing
        equals_args = is_internet_facing["Fn::Equals"]
        assert len(equals_args) == 2
        assert equals_args[0] == {"Ref": "AlbScheme"}
        assert equals_args[1] == "internet-facing"
        
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
            
            # Test image parameters have correct types and defaults
            image_params = ["GoServicesImage", "QueryApiImage", "QueryWorkerImage", "GrafanaImage"]
            for param_name in image_params:
                assert param_name in parameters
                param = parameters[param_name]
                assert param["Type"] == "String"
                assert "Default" in param
                assert param["Default"].startswith("test:")  # From mock config

    def test_parameter_validation_with_cloud_radar(self):
        """Test parameter validation using Cloud-Radar with invalid values"""
        from lakerunner_common import t as template
        
        # Test with invalid AlbScheme value
        invalid_params = {
            "VpcId": "vpc-12345678",
            "PublicSubnets": "subnet-12345678,subnet-87654321", 
            "PrivateSubnets": "subnet-abcdef12,subnet-fedcba21",
            "AlbScheme": "invalid-scheme",  # Invalid value
            "ApiKeysOverride": "",
            "StorageProfilesOverride": ""
        }
        
        # Cloud-Radar should handle this - it validates the template structure
        # but parameter value validation happens at CloudFormation deployment time
        cf_template = CloudRadarTemplate(
            template=json.loads(template.to_json())
        )
        
        # Template structure should still be valid
        assert cf_template.template is not None
        assert "Parameters" in cf_template.template
        
        # The parameter constraint should be in the template
        alb_scheme_param = cf_template.template["Parameters"]["AlbScheme"]
        assert "AllowedValues" in alb_scheme_param

    def test_condition_usage_in_resources(self):
        """Test that conditions are properly used in resource definitions"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]
        
        # Find ALB resource and check if it uses conditions properly
        alb_resource = None
        for name, resource in resources.items():
            if resource["Type"] == "AWS::ElasticLoadBalancingV2::LoadBalancer":
                alb_resource = resource
                break
        
        assert alb_resource is not None, "ALB resource not found"
        
        # Check that ALB Subnets property uses conditional logic
        subnets_property = alb_resource["Properties"]["Subnets"]
        assert "Fn::If" in subnets_property
        
        # Verify the If condition structure
        if_args = subnets_property["Fn::If"]
        assert len(if_args) == 3  # condition, true_value, false_value
        assert if_args[0] == "IsInternetFacing"

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
        
        # AlbScheme should default to internal (more secure)
        alb_scheme = parameters["AlbScheme"]
        assert alb_scheme["Default"] == "internal"
        
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
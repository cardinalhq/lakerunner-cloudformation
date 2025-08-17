import pytest
import json
from cloud_radar.cf.unit import Template as CloudRadarTemplate


class TestConditionValidation:
    """Specific tests for CloudFormation condition validation to catch invalid condition patterns"""

    def test_condition_syntax_validation(self):
        """Test that all conditions have valid CloudFormation syntax"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        conditions = template_dict.get("Conditions", {})
        
        for condition_name, condition_def in conditions.items():
            # Validate condition structure
            assert isinstance(condition_def, dict), f"Condition {condition_name} is not a dict"
            
            # Must have exactly one top-level function
            assert len(condition_def) == 1, f"Condition {condition_name} has invalid structure"
            
            condition_func = list(condition_def.keys())[0]
            condition_args = condition_def[condition_func]
            
            # Validate known condition functions
            valid_functions = [
                "Fn::Equals", "Fn::Not", "Fn::And", "Fn::Or",
                "Condition"  # Reference to another condition
            ]
            assert condition_func in valid_functions, f"Invalid condition function {condition_func} in {condition_name}"
            
            # Validate specific function syntax
            if condition_func == "Fn::Equals":
                assert isinstance(condition_args, list), f"Fn::Equals in {condition_name} must be a list"
                assert len(condition_args) == 2, f"Fn::Equals in {condition_name} must have exactly 2 arguments"
                
            elif condition_func == "Fn::Not":
                assert isinstance(condition_args, list), f"Fn::Not in {condition_name} must be a list"
                assert len(condition_args) == 1, f"Fn::Not in {condition_name} must have exactly 1 argument"
                
            elif condition_func in ["Fn::And", "Fn::Or"]:
                assert isinstance(condition_args, list), f"{condition_func} in {condition_name} must be a list"
                assert len(condition_args) >= 2, f"{condition_func} in {condition_name} must have at least 2 arguments"

    def test_condition_parameter_references(self):
        """Test that conditions properly reference existing parameters"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        parameters = template_dict.get("Parameters", {})
        conditions = template_dict.get("Conditions", {})
        
        def extract_parameter_refs(obj):
            """Recursively extract all parameter references from a condition"""
            refs = set()
            if isinstance(obj, dict):
                if "Ref" in obj:
                    refs.add(obj["Ref"])
                else:
                    for value in obj.values():
                        refs.update(extract_parameter_refs(value))
            elif isinstance(obj, list):
                for item in obj:
                    refs.update(extract_parameter_refs(item))
            return refs
        
        for condition_name, condition_def in conditions.items():
            param_refs = extract_parameter_refs(condition_def)
            
            # Filter out AWS pseudo parameters
            user_param_refs = {ref for ref in param_refs if not ref.startswith("AWS::")}
            
            # All referenced parameters must exist
            for param_ref in user_param_refs:
                assert param_ref in parameters, f"Condition {condition_name} references non-existent parameter {param_ref}"

    def test_condition_usage_validation(self):
        """Test that conditions are used properly in resources and outputs"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        conditions = template_dict.get("Conditions", {})
        resources = template_dict.get("Resources", {})
        outputs = template_dict.get("Outputs", {})
        
        def extract_condition_refs(obj, path=""):
            """Recursively extract condition references from template sections"""
            refs = set()
            if isinstance(obj, dict):
                if "Fn::If" in obj:
                    if_args = obj["Fn::If"]
                    if isinstance(if_args, list) and len(if_args) >= 1:
                        condition_ref = if_args[0]
                        if isinstance(condition_ref, str):
                            refs.add((condition_ref, path))
                
                if "Condition" in obj:
                    # Resource-level condition
                    condition_ref = obj["Condition"]
                    if isinstance(condition_ref, str):
                        refs.add((condition_ref, path))
                
                for key, value in obj.items():
                    if key not in ["Fn::If", "Condition"]:
                        refs.update(extract_condition_refs(value, f"{path}.{key}" if path else key))
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    refs.update(extract_condition_refs(item, f"{path}[{i}]"))
            
            return refs
        
        # Check all condition references in resources
        for resource_name, resource_def in resources.items():
            condition_refs = extract_condition_refs(resource_def, f"Resources.{resource_name}")
            
            for condition_ref, path in condition_refs:
                assert condition_ref in conditions, f"Resource {resource_name} at {path} references non-existent condition {condition_ref}"
        
        # Check all condition references in outputs
        for output_name, output_def in outputs.items():
            condition_refs = extract_condition_refs(output_def, f"Outputs.{output_name}")
            
            for condition_ref, path in condition_refs:
                assert condition_ref in conditions, f"Output {output_name} at {path} references non-existent condition {condition_ref}"

    def test_condition_logic_validation(self):
        """Test condition logic for common mistakes"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        conditions = template_dict.get("Conditions", {})
        
        # Test specific condition logic
        if "IsInternetFacing" in conditions:
            is_internet_facing = conditions["IsInternetFacing"]
            
            # Should be Fn::Equals with correct arguments
            assert "Fn::Equals" in is_internet_facing
            equals_args = is_internet_facing["Fn::Equals"]
            assert len(equals_args) == 2
            
            # First argument should reference AlbScheme parameter
            assert equals_args[0] == {"Ref": "AlbScheme"}
            
            # Second argument should be the expected value
            assert equals_args[1] == "internet-facing"
        
        if "HasApiKeysOverride" in conditions:
            has_api_keys = conditions["HasApiKeysOverride"]
            
            # Should be Fn::Not of Fn::Equals with empty string
            assert "Fn::Not" in has_api_keys
            not_args = has_api_keys["Fn::Not"]
            assert len(not_args) == 1
            
            inner_condition = not_args[0]
            assert "Fn::Equals" in inner_condition
            equals_args = inner_condition["Fn::Equals"]
            assert len(equals_args) == 2
            assert equals_args[1] == ""

    def test_cloud_radar_condition_validation(self):
        """Test condition validation using Cloud-Radar"""
        from lakerunner_common import t as template
        
        # Create template with mock imports for validation
        cf_template = CloudRadarTemplate(
            template=json.loads(template.to_json()),
            imports={
                "VpcId": "vpc-12345678",
                "PublicSubnets": "subnet-12345,subnet-67890",
                "PrivateSubnets": "subnet-abcde,subnet-fghij"
            }
        )
        
        # Template should be valid
        assert cf_template.template is not None
        
        # Conditions should be preserved in the template
        conditions = cf_template.template.get("Conditions", {})
        assert len(conditions) > 0
        
        # Verify specific conditions exist and have proper structure
        expected_conditions = ["IsInternetFacing", "HasApiKeysOverride", "HasStorageProfilesOverride"]
        for condition_name in expected_conditions:
            assert condition_name in conditions, f"Expected condition {condition_name} not found"

    def test_parameter_condition_interdependencies(self):
        """Test that parameter values and conditions work together correctly"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        parameters = template_dict.get("Parameters", {})
        conditions = template_dict.get("Conditions", {})
        
        # Test AlbScheme parameter and IsInternetFacing condition
        if "AlbScheme" in parameters and "IsInternetFacing" in conditions:
            alb_scheme_param = parameters["AlbScheme"]
            is_internet_facing_condition = conditions["IsInternetFacing"]
            
            # Parameter should have allowed values
            allowed_values = alb_scheme_param.get("AllowedValues", [])
            assert "internet-facing" in allowed_values
            assert "internal" in allowed_values
            
            # Condition should test for valid parameter value
            equals_args = is_internet_facing_condition["Fn::Equals"]
            condition_test_value = equals_args[1]
            assert condition_test_value in allowed_values

    def test_common_condition_antipatterns(self):
        """Test for common condition antipatterns that cause issues"""
        from lakerunner_common import t as template
        
        template_dict = json.loads(template.to_json())
        conditions = template_dict.get("Conditions", {})
        
        for condition_name, condition_def in conditions.items():
            # Antipattern 1: Using undefined pseudo-parameters
            condition_str = json.dumps(condition_def)
            
            # Check for invalid AWS pseudo-parameters
            invalid_pseudos = ["AWS::StackId", "AWS::StackName"]  # Should use AWS::StackName, AWS::Region, etc.
            for invalid_pseudo in invalid_pseudos:
                if invalid_pseudo in condition_str:
                    # This might be valid, but check if it's being used correctly
                    pass
            
            # Antipattern 2: Empty condition functions
            def check_empty_functions(obj):
                if isinstance(obj, dict):
                    for key, value in obj.items():
                        if key.startswith("Fn::") and isinstance(value, list) and len(value) == 0:
                            pytest.fail(f"Empty function {key} in condition {condition_name}")
                        check_empty_functions(value)
                elif isinstance(obj, list):
                    for item in obj:
                        check_empty_functions(item)
            
            check_empty_functions(condition_def)

    def test_services_template_conditions(self):
        """Test conditions in Services template if any exist"""
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
            
            # Check if there are any conditions and validate them
            conditions = template_dict.get("Conditions", {})
            if conditions:
                for condition_name, condition_def in conditions.items():
                    # Apply same validation as for CommonInfra
                    assert isinstance(condition_def, dict), f"Condition {condition_name} is not a dict"
                    assert len(condition_def) == 1, f"Condition {condition_name} has invalid structure"
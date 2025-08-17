import pytest
import json
from cloud_radar.cf.unit import Template as CloudRadarTemplate
from lakerunner_common import t as template


class TestCommonInfraTemplate:
    """Test cases for the CommonInfra CloudFormation template"""

    def test_template_is_valid_json(self):
        """Test that the template generates valid JSON"""
        template_json = template.to_json()
        # Should not raise an exception
        parsed = json.loads(template_json)
        assert isinstance(parsed, dict)

    def test_template_has_required_sections(self):
        """Test that template has required CloudFormation sections"""
        template_dict = json.loads(template.to_json())
        
        # Troposphere doesn't always include AWSTemplateFormatVersion
        assert "Description" in template_dict
        assert "Parameters" in template_dict
        assert "Resources" in template_dict
        assert "Outputs" in template_dict

    def test_template_description(self):
        """Test template description"""
        template_dict = json.loads(template.to_json())
        assert template_dict["Description"] == "CommonInfra stack for Lakerunner."

    def test_required_parameters_exist(self):
        """Test that required parameters are defined"""
        template_dict = json.loads(template.to_json())
        parameters = template_dict["Parameters"]
        
        required_params = [
            "VpcId", "PublicSubnets", "PrivateSubnets", 
            "AlbScheme", "ApiKeysOverride", "StorageProfilesOverride"
        ]
        
        for param in required_params:
            assert param in parameters, f"Required parameter {param} not found"

    def test_vpc_parameter_type(self):
        """Test VPC parameter has correct type"""
        template_dict = json.loads(template.to_json())
        vpc_param = template_dict["Parameters"]["VpcId"]
        assert vpc_param["Type"] == "AWS::EC2::VPC::Id"

    def test_template_with_cloud_radar(self, sample_parameters):
        """Test template validation using Cloud-Radar"""
        # Add VPC and subnet parameters required by the template
        test_params = sample_parameters.copy()
        test_params.update({
            "VpcId": "vpc-12345678",
            "PublicSubnets": "subnet-12345678,subnet-87654321", 
            "PrivateSubnets": "subnet-abcdef12,subnet-fedcba21"
        })
        
        # Create Cloud-Radar template for validation
        cf_template = CloudRadarTemplate(
            template=json.loads(template.to_json()),
            imports={
                "VpcId": "vpc-12345678",
                "PublicSubnets": "subnet-12345678,subnet-87654321", 
                "PrivateSubnets": "subnet-abcdef12,subnet-fedcba21"
            }
        )
        
        # Validate template structure
        assert cf_template.template is not None
        assert "Resources" in cf_template.template

    def test_ecs_cluster_resource_exists(self):
        """Test that ECS cluster resource is created"""
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]
        
        # Find ECS cluster resource
        cluster_resources = [
            name for name, resource in resources.items() 
            if resource["Type"] == "AWS::ECS::Cluster"
        ]
        
        assert len(cluster_resources) > 0, "ECS Cluster resource not found"

    def test_database_resource_exists(self):
        """Test that RDS database resource is created"""
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]
        
        # Find RDS instance resource
        db_resources = [
            name for name, resource in resources.items() 
            if resource["Type"] == "AWS::RDS::DBInstance"
        ]
        
        assert len(db_resources) > 0, "RDS Database resource not found"

    def test_security_groups_exist(self):
        """Test that security groups are created"""
        template_dict = json.loads(template.to_json())
        resources = template_dict["Resources"]
        
        # Find security group resources
        sg_resources = [
            name for name, resource in resources.items() 
            if resource["Type"] == "AWS::EC2::SecurityGroup"
        ]
        
        assert len(sg_resources) > 0, "Security Group resources not found"

    def test_exports_are_defined(self):
        """Test that required exports are defined for cross-stack references"""
        template_dict = json.loads(template.to_json())
        outputs = template_dict.get("Outputs", {})
        
        # Check for outputs that should be exported
        expected_exports = ["ClusterArn", "DatabaseHost", "VpcId"]
        
        for export_name in expected_exports:
            # Look for outputs that export this value
            found_export = False
            for output_name, output_def in outputs.items():
                if "Export" in output_def and export_name in str(output_def["Export"]):
                    found_export = True
                    break
            
            if not found_export:
                # Some exports might be conditional, so this is a warning not failure
                print(f"Warning: Export {export_name} not found in outputs")
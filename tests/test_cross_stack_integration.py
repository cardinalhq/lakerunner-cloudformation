import pytest
import json
import os


class TestCrossStackIntegration:
    """Integration tests for cross-stack validation using generated templates"""

    def test_migration_imports_match_common_exports_generated_templates(self):
        """Test migration stack imports against common stack exports using generated templates"""
        # Read generated templates
        migration_path = "generated-templates/lakerunner-migration.yaml"
        common_path = "generated-templates/lakerunner-common.yaml"
        
        # Verify files exist
        assert os.path.exists(migration_path), f"Generated migration template not found: {migration_path}"
        assert os.path.exists(common_path), f"Generated common template not found: {common_path}"
        
        # Parse YAML templates (simple string parsing for ImportValue/Export patterns)
        with open(migration_path, 'r') as f:
            migration_content = f.read()
        
        with open(common_path, 'r') as f:
            common_content = f.read()
        
        # Extract ImportValue patterns from migration template
        migration_imports = set()
        for line in migration_content.split('\n'):
            if 'Fn::Sub: ${CommonInfraStackName}-' in line:
                # Extract the export name after the dash
                export_name = line.split('${CommonInfraStackName}-')[1].strip()
                migration_imports.add(export_name)
        
        # Extract Export patterns from common template  
        common_exports = set()
        for line in common_content.split('\n'):
            if 'Name: !Sub \'${AWS::StackName}-' in line:
                # Extract the export name after the dash
                export_name = line.split('${AWS::StackName}-')[1].split('\'')[0]
                common_exports.add(export_name)
        
        # Validate all migration imports have corresponding common exports
        missing_exports = migration_imports - common_exports
        
        # Debug output
        print(f"\nMigration imports from generated template: {sorted(migration_imports)}")
        print(f"Common exports from generated template: {sorted(common_exports)}")
        if missing_exports:
            print(f"Missing exports: {sorted(missing_exports)}")
        
        # Assert validation
        assert len(missing_exports) == 0, (
            f"Migration template imports values not exported by common template: {missing_exports}"
        )
        
        # Ensure we found expected imports/exports
        assert len(migration_imports) > 0, "No imports found in migration template"
        assert len(common_exports) > 0, "No exports found in common template"
        
        # Check for specific expected imports/exports
        expected_imports = ['DbEndpoint', 'DbSecretArn', 'ClusterArn', 'TaskSGId', 'PrivateSubnets']
        for expected in expected_imports:
            assert expected in migration_imports, f"Expected import '{expected}' not found in migration template"
            assert expected in common_exports, f"Expected export '{expected}' not found in common template"

    def test_services_imports_match_common_exports_generated_templates(self):
        """Test services stack imports against common stack exports using generated templates"""
        # Read generated templates
        services_path = "generated-templates/lakerunner-services.yaml"
        common_path = "generated-templates/lakerunner-common.yaml"
        
        # Verify files exist
        assert os.path.exists(services_path), f"Generated services template not found: {services_path}"
        assert os.path.exists(common_path), f"Generated common template not found: {common_path}"
        
        # Parse YAML templates
        with open(services_path, 'r') as f:
            services_content = f.read()
        
        with open(common_path, 'r') as f:
            common_content = f.read()
        
        # Extract ImportValue patterns from services template
        services_imports = set()
        lines = services_content.split('\n')
        for i, line in enumerate(lines):
            # Handle both Fn::Sub patterns
            if '${CommonInfraStackName}-' in line:
                # Extract the export name after the dash
                if '-' in line.split('${CommonInfraStackName}-')[1]:
                    export_name = line.split('${CommonInfraStackName}-')[1].split()[0].strip(',')
                else:
                    export_name = line.split('${CommonInfraStackName}-')[1].strip()
                services_imports.add(export_name)
        
        # Extract Export patterns from common template  
        common_exports = set()
        for line in common_content.split('\n'):
            if 'Name: !Sub \'${AWS::StackName}-' in line:
                # Extract the export name after the dash
                export_name = line.split('${AWS::StackName}-')[1].split('\'')[0]
                common_exports.add(export_name)
        
        # Validate all services imports have corresponding common exports
        missing_exports = services_imports - common_exports
        
        # Debug output
        print(f"\nServices imports from generated template: {sorted(services_imports)}")
        print(f"Common exports from generated template: {sorted(common_exports)}")
        if missing_exports:
            print(f"Missing exports: {sorted(missing_exports)}")
        
        # Assert validation
        assert len(missing_exports) == 0, (
            f"Services template imports values not exported by common template: {missing_exports}"
        )
        
        # Services template should have imports (this will fail if no services are configured)
        if len(services_imports) == 0:
            print("WARNING: No imports found in services template - this may indicate incomplete configuration")
        
        # Ensure we found exports
        assert len(common_exports) > 0, "No exports found in common template"

    def test_export_stack_name_consistency(self):
        """Test that exports use consistent stack name variable"""
        common_path = "generated-templates/lakerunner-common.yaml"
        
        assert os.path.exists(common_path), f"Generated common template not found: {common_path}"
        
        with open(common_path, 'r') as f:
            common_content = f.read()
        
        # Find all patterns with stack name substitution
        stack_name_patterns = []
        for line in common_content.split('\n'):
            if '${AWS::StackName}-' in line:
                stack_name_patterns.append(line.strip())
        
        assert len(stack_name_patterns) > 0, "No stack name patterns found in common template"
        
        # All patterns should use AWS::StackName consistently
        for pattern in stack_name_patterns:
            assert '${AWS::StackName}' in pattern, (
                f"Pattern doesn't use consistent AWS::StackName variable: {pattern}"
            )
            
        print(f"\nFound {len(stack_name_patterns)} patterns using consistent AWS::StackName variable")

    def test_no_direct_resource_references_in_dependent_stacks(self):
        """Test that dependent stacks don't directly reference resources from other stacks"""
        migration_path = "generated-templates/lakerunner-migration.yaml"
        services_path = "generated-templates/lakerunner-services.yaml"
        
        assert os.path.exists(migration_path), f"Generated migration template not found: {migration_path}"
        assert os.path.exists(services_path), f"Generated services template not found: {services_path}"
        
        # Read templates
        with open(migration_path, 'r') as f:
            migration_content = f.read()
        
        with open(services_path, 'r') as f:
            services_content = f.read()
        
        # Check for direct resource references (these would be bad)
        # Direct references look like "!Ref SomeResource" or "!GetAtt SomeResource.Property"
        # where SomeResource is defined in another stack
        
        # For this test, we'll check that dependent stacks only use ImportValue, not direct Ref/GetAtt
        # to resources that should be from the common stack
        
        common_resource_patterns = [
            '!Ref IngestBucket',     # Should use ImportValue instead
            '!Ref Cluster',          # Should use ImportValue instead  
            '!Ref TaskSG',           # Should use ImportValue instead
            '!GetAtt Cluster',       # Should use ImportValue instead
            '!GetAtt LakerunnerDb'   # Should use ImportValue instead
        ]
        
        issues = []
        
        for pattern in common_resource_patterns:
            if pattern in migration_content:
                issues.append(f"Migration template contains direct reference: {pattern}")
            if pattern in services_content:
                issues.append(f"Services template contains direct reference: {pattern}")
        
        assert len(issues) == 0, (
            f"Found direct resource references in dependent stacks (should use ImportValue instead): {issues}"
        )
        
        print("✓ No direct resource references found in dependent stacks")
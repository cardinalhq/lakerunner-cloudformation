import pytest
import json
from unittest.mock import patch


class TestCrossStackValidation:
    """Test cases for validating cross-stack dependencies and imports/exports"""

    def extract_import_values(self, obj, stack_name_param="CommonInfraStackName"):
        """Recursively extract ImportValue references from template object"""
        imports = set()
        
        def _extract(obj):
            if isinstance(obj, dict):
                # Handle Fn::ImportValue with Fn::Sub pattern
                if "Fn::ImportValue" in obj:
                    import_ref = obj["Fn::ImportValue"]
                    if isinstance(import_ref, dict) and "Fn::Sub" in import_ref:
                        sub_value = import_ref["Fn::Sub"]
                        if isinstance(sub_value, str) and f"${{{stack_name_param}}}" in sub_value:
                            # Extract the export name (part after the stack name)
                            export_name = sub_value.replace(f"${{{stack_name_param}}}-", "")
                            imports.add(export_name)
                    elif isinstance(import_ref, str):
                        # Handle direct string imports (shouldn't be used but check anyway)
                        imports.add(import_ref)
                
                # Recursively check all values
                for value in obj.values():
                    _extract(value)
            elif isinstance(obj, list):
                # Recursively check all list items
                for item in obj:
                    _extract(item)
        
        _extract(obj)
        return imports

    def extract_exports(self, template_dict):
        """Extract all exports from a CloudFormation template"""
        exports = set()
        
        if "Outputs" in template_dict:
            for output_name, output_config in template_dict["Outputs"].items():
                if "Export" in output_config:
                    export_config = output_config["Export"]
                    if "Name" in export_config:
                        export_name = export_config["Name"]
                        # Handle Fn::Sub pattern in export names
                        if isinstance(export_name, dict) and "Fn::Sub" in export_name:
                            sub_value = export_name["Fn::Sub"]
                            if isinstance(sub_value, str) and "${AWS::StackName}-" in sub_value:
                                # Extract the export suffix (part after the stack name)
                                export_suffix = sub_value.replace("${AWS::StackName}-", "")
                                exports.add(export_suffix)
        
        return exports

    @patch('lakerunner_migration.load_defaults')
    @patch('lakerunner_common.load_defaults')
    def test_migration_imports_match_common_exports(self, mock_common_defaults, mock_migration_defaults):
        """Test that migration stack only imports values that are exported by common stack"""
        # Mock configurations to avoid file dependencies
        mock_common_defaults.return_value = {
            "api_keys": [{"organization_id": "test", "keys": ["test-key"]}],
            "storage_profiles": [{"bucket": "test", "region": "us-east-1"}]
        }
        mock_migration_defaults.return_value = {
            "images": {"migration": "test:latest"}
        }
        
        from lakerunner_migration import t as migration_template
        from lakerunner_common import t as common_template
        
        # Parse both templates
        migration_dict = json.loads(migration_template.to_json())
        common_dict = json.loads(common_template.to_json())
        
        # Extract imports and exports
        migration_imports = self.extract_import_values(migration_dict)
        common_exports = self.extract_exports(common_dict)
        
        # Validate that all migration imports have corresponding common exports
        missing_exports = migration_imports - common_exports
        
        # Print debug information for troubleshooting
        print(f"\nMigration imports: {sorted(migration_imports)}")
        print(f"Common exports: {sorted(common_exports)}")
        if missing_exports:
            print(f"Missing exports: {sorted(missing_exports)}")
        
        # Assert that there are no missing exports
        assert len(missing_exports) == 0, (
            f"Migration stack imports values that are not exported by common stack: {missing_exports}. "
            f"Migration imports: {migration_imports}, Common exports: {common_exports}"
        )
        
        # Ensure we found some imports and exports (sanity check)
        assert len(migration_imports) > 0, "No ImportValue references found in migration template"
        assert len(common_exports) > 0, "No exports found in common template"

    @patch('lakerunner_services.load_service_config')
    @patch('lakerunner_common.load_defaults')
    def test_services_imports_match_common_exports(self, mock_common_defaults, mock_services_defaults):
        """Test that services stack only imports values that are exported by common stack"""
        # Mock configurations to avoid file dependencies
        mock_common_defaults.return_value = {
            "api_keys": [{"organization_id": "test", "keys": ["test-key"]}],
            "storage_profiles": [{"bucket": "test", "region": "us-east-1"}]
        }
        mock_services_defaults.return_value = {
            "images": {
                "go_services": "test:latest",
                "query_api": "test:latest", 
                "query_worker": "test:latest"
            },
            "services": {
                "test-service": {
                    "command": ["test"],
                    "cpu": 256,
                    "memory_mib": 512,
                    "replicas": 1,
                    "health_check": {"type": "go", "command": ["test"]},
                    "environment": {}
                }
            }
        }
        
        from lakerunner_services import create_services_template
        from lakerunner_common import t as common_template
        
        # Parse both templates
        services_template = create_services_template()
        services_dict = json.loads(services_template.to_json())
        common_dict = json.loads(common_template.to_json())
        
        # Extract imports and exports
        services_imports = self.extract_import_values(services_dict)
        common_exports = self.extract_exports(common_dict)
        
        # Validate that all services imports have corresponding common exports
        missing_exports = services_imports - common_exports
        
        # Print debug information for troubleshooting
        print(f"\nServices imports: {sorted(services_imports)}")
        print(f"Common exports: {sorted(common_exports)}")
        if missing_exports:
            print(f"Missing exports: {sorted(missing_exports)}")
        
        # Assert that there are no missing exports
        assert len(missing_exports) == 0, (
            f"Services stack imports values that are not exported by common stack: {missing_exports}. "
            f"Services imports: {services_imports}, Common exports: {common_exports}"
        )
        
        # Ensure we found some imports and exports (sanity check)
        assert len(services_imports) > 0, "No ImportValue references found in services template"
        assert len(common_exports) > 0, "No exports found in common template"

    def test_cross_stack_import_naming_convention(self):
        """Test that cross-stack imports follow the expected naming convention"""
        from lakerunner_migration import t as migration_template
        from lakerunner_services import create_services_template
        
        services_template = create_services_template()
        
        # Parse templates
        migration_dict = json.loads(migration_template.to_json())
        services_dict = json.loads(services_template.to_json())
        
        # Extract all ImportValue patterns
        def extract_import_patterns(obj):
            patterns = []
            def _extract(obj):
                if isinstance(obj, dict):
                    if "Fn::ImportValue" in obj:
                        import_ref = obj["Fn::ImportValue"]
                        if isinstance(import_ref, dict) and "Fn::Sub" in import_ref:
                            patterns.append(import_ref["Fn::Sub"])
                    for value in obj.values():
                        _extract(value)
                elif isinstance(obj, list):
                    for item in obj:
                        _extract(item)
            _extract(obj)
            return patterns
        
        migration_patterns = extract_import_patterns(migration_dict)
        services_patterns = extract_import_patterns(services_dict)
        
        # All import patterns should follow the ${CommonInfraStackName}-<ExportName> format
        expected_prefix = "${CommonInfraStackName}-"
        
        for pattern in migration_patterns:
            assert isinstance(pattern, str), f"Import pattern should be string: {pattern}"
            assert pattern.startswith(expected_prefix), (
                f"Migration import pattern should start with '{expected_prefix}': {pattern}"
            )
        
        for pattern in services_patterns:
            assert isinstance(pattern, str), f"Import pattern should be string: {pattern}"
            assert pattern.startswith(expected_prefix), (
                f"Services import pattern should start with '{expected_prefix}': {pattern}"
            )

    def test_no_hardcoded_stack_names_in_imports(self):
        """Test that no imports use hardcoded stack names instead of parameters"""
        from lakerunner_migration import t as migration_template
        from lakerunner_services import create_services_template
        
        services_template = create_services_template()
        
        # Parse templates
        migration_dict = json.loads(migration_template.to_json())
        services_dict = json.loads(services_template.to_json())
        
        # Function to find hardcoded stack references
        def find_hardcoded_imports(obj, template_name):
            hardcoded = []
            def _find(obj, path=""):
                if isinstance(obj, dict):
                    if "Fn::ImportValue" in obj:
                        import_ref = obj["Fn::ImportValue"]
                        if isinstance(import_ref, str):
                            # Direct string imports are hardcoded
                            hardcoded.append(f"{template_name}:{path} - {import_ref}")
                        elif isinstance(import_ref, dict) and "Fn::Sub" in import_ref:
                            sub_value = import_ref["Fn::Sub"]
                            if isinstance(sub_value, str) and "${CommonInfraStackName}" not in sub_value:
                                # Sub value doesn't use parameter
                                hardcoded.append(f"{template_name}:{path} - {sub_value}")
                    
                    for key, value in obj.items():
                        _find(value, f"{path}.{key}" if path else key)
                elif isinstance(obj, list):
                    for i, item in enumerate(obj):
                        _find(item, f"{path}[{i}]")
            _find(obj)
            return hardcoded
        
        migration_hardcoded = find_hardcoded_imports(migration_dict, "migration")
        services_hardcoded = find_hardcoded_imports(services_dict, "services")
        
        all_hardcoded = migration_hardcoded + services_hardcoded
        
        assert len(all_hardcoded) == 0, (
            f"Found hardcoded stack names in imports (should use parameters): {all_hardcoded}"
        )
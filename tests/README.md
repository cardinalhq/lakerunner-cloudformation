# CloudFormation Template Unit Tests

This directory contains unit tests for the Lakerunner CloudFormation templates using pytest and cloud-radar.

## Running Tests

```bash
# Run all tests
make test

# Run specific template tests
make test-common      # CommonInfra template
make test-services    # Services template  
make test-migration   # Migration template

# Run with verbose output
source .venv/bin/activate && pytest tests/ -v
```

## Test Structure

- `conftest.py` - Shared test configuration and fixtures
- `test_common_infra.py` - Tests for the CommonInfra template
- `test_services.py` - Tests for the Services template (with mocked configuration)
- `test_migration.py` - Tests for the Migration template

## Test Types

### Template Generation Tests
- Validate that templates generate valid JSON/YAML
- Check required CloudFormation sections exist
- Verify template descriptions and metadata

### Parameter Tests
- Ensure required parameters are defined
- Validate parameter types and constraints
- Test parameter defaults and allowed values

### Resource Tests
- Confirm expected AWS resources are created
- Validate resource properties and references
- Check conditional resource creation logic

### Cloud-Radar Integration Tests
- Offline validation of template syntax and structure
- Mock external dependencies (imports, secrets)
- Test template rendering with sample parameters

### Cross-Stack Integration Tests
- Validate export/import patterns between stacks
- Test cross-stack references and dependencies
- Ensure consistent naming conventions

## Adding New Tests

When adding new functionality:

1. **Create test cases** for new resources or parameters
2. **Update mock configurations** to match template expectations
3. **Test both success and failure scenarios**
4. **Validate CloudFormation best practices**

## Dependencies

- `pytest` - Test framework
- `cloud-radar` - Offline CloudFormation validation
- Mocked service configurations avoid external file dependencies
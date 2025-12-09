# CloudFormation Development Instructions

This file contains instructions for Claude on how to work with this CloudFormation repository.

## Repository Structure

The deployment consists of three CloudFormation stacks that must be deployed in order:

1. **CommonInfra** (`lakerunner_common.py`) - Core infrastructure
2. **Migration** (`lakerunner_migration.py`) - Database migration task
3. **Services** (`lakerunner_services.py`) - ECS Fargate services

### Stack Dependencies

- Migration depends on CommonInfra exports (database, networking, security groups)
- Services depends on CommonInfra exports (all infrastructure resources)
- Services auto-detects ALB presence from CommonInfra without requiring user input

### Configuration System

- **lakerunner-stack-defaults.yaml** - Contains all default configurations (API keys, storage profiles, service definitions)
- **Cross-stack imports** - Services automatically import values from CommonInfra using CloudFormation exports
- **Parameter minimization** - Only ask users for what cannot be determined from other stacks
- **Air-gapped support** - Container image parameters allow overriding public ECR images

## Key Design Patterns

### Cross-Stack Resource Sharing

Templates use CloudFormation exports/imports extensively:

```python
# Export in CommonInfra
Export=Export(name=Sub("${AWS::StackName}-ClusterArn"))

# Import in Services
ClusterArnValue = ImportValue(ci_export("ClusterArn"))
```

### Conditional ALB Resources

ALB creation is optional in CommonInfra. Services template automatically detects ALB presence:

```python
CreateAlbValue = ImportValue(ci_export("CreateAlb"))
t.add_condition("HasAlb", Equals(CreateAlbValue, "Yes"))
```

### Unified Configuration Loading

All templates load defaults from YAML and allow parameter overrides:

```python
def load_defaults(config_file="defaults.yaml"):
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)
```

## Build and Testing

### Commands

- `./build.sh` - Generate all CloudFormation templates with validation
- `python3 <template>.py` - Generate individual template
- `cfn-lint out/<template>.yaml` - Validate specific template
- `make test` - Run all unit tests
- `make test-common` - Run CommonInfra template tests only
- `make test-services` - Run Services template tests only
- `make test-migration` - Run Migration template tests only
- `make test-params` - Run parameter and condition validation tests only
- `make build` - Generate templates using Makefile
- `make lint` - Run cfn-lint validation on all templates
- `make all` - Run build, test, and lint together

### Environment

The build system uses a Python virtual environment with dependencies in the `requirements.txt` file.

### Testing Changes

When making changes to templates, always use the virtual environment to test:

1. `source .venv/bin/activate` - Activate the virtual environment
1. `./build.sh` - Regenerate all templates and run validation
1. `cfn-lint out/*.yaml` - Run additional validation if needed

All templates must pass cfn-lint validation (errors must be fixed, warnings are acceptable if safe).

### Unit Testing

The repository includes comprehensive unit tests using pytest and cloud-radar for offline CloudFormation template testing:

- **Test Structure**: Tests are organized in the `tests/` directory with separate test files for each template
- **Cloud-Radar**: Enables offline validation of CloudFormation templates without AWS credentials
- **Mock Configuration**: Tests use mocked service configurations to avoid dependencies on external files
- **Coverage**: Tests validate template structure, parameters, resources, exports, and CloudFormation syntax

When adding new templates or modifying existing ones:
1. Run `make test` to ensure all tests pass
2. Add new test cases for new functionality
3. Update test mocks when changing service configurations
4. Ensure tests cover both positive and negative scenarios
5. Run `make test-params` specifically for parameter and condition changes

### Parameter Validation Testing

The repository includes comprehensive parameter validation tests that catch issues cfn-lint may miss:

- **Parameter Constraints**: Validates AllowedValues, Types, and constraint consistency
- **Condition Syntax**: Ensures all CloudFormation conditions use valid syntax
- **Parameter References**: Verifies conditions reference existing parameters
- **Condition Usage**: Validates conditions are used properly in resources and outputs
- **Logic Validation**: Tests condition logic for common mistakes and antipatterns
- **Cross-Template Consistency**: Ensures parameter types are consistent across templates

## Development Guidelines

### Template Modifications

- Always test with `./build.sh` after changes
- Use `cfn-lint` validation - address errors, safe and explainable warnings are acceptable
- Maintain parameter minimization - only ask users for what's necessary
- Follow existing cross-stack import patterns for new resources

### Adding New Services

1. Add service configuration to `defaults.yaml`
1. Services template will automatically create ECS service, task definition, IAM roles
1. For ALB attachment, set `ingress.attach_alb: true` in service config
1. For EFS access, define `efs_mounts` with access point configuration

### Service Configuration Parameters

The Services stack exposes CloudFormation parameters to configure replicas, CPU, and memory for lakerunner services at deployment time. Different service types have different configurable options:

| Service                    | Replicas  | CPU       | Memory    |
|----------------------------|-----------|-----------|-----------|
| **Query Services**         |           |           |           |
| lakerunner-query-api       | Parameter | Parameter | Parameter |
| lakerunner-query-worker    | Parameter | Parameter | Parameter |
| **Worker Services**        |           |           |           |
| lakerunner-ingest-logs     | Parameter | YAML      | Parameter |
| lakerunner-ingest-metrics  | Parameter | YAML      | Parameter |
| lakerunner-ingest-traces   | Parameter | YAML      | Parameter |
| lakerunner-compact-logs    | Parameter | YAML      | Parameter |
| lakerunner-compact-metrics | Parameter | YAML      | Parameter |
| lakerunner-compact-traces  | Parameter | YAML      | Parameter |
| lakerunner-rollup-metrics  | Parameter | YAML      | Parameter |
| **Replicas-Only Services** |           |           |           |
| lakerunner-pubsub-sqs      | Parameter | YAML      | YAML      |
| lakerunner-boxer-common    | Parameter | YAML      | YAML      |
| **Fixed Services**         |           |           |           |
| lakerunner-sweeper         | YAML      | YAML      | YAML      |
| lakerunner-monitoring      | YAML      | YAML      | YAML      |

- **Parameter**: Configurable via CloudFormation parameter at deployment time
- **YAML**: Uses default value from `lakerunner-stack-defaults.yaml`

Parameters are organized in the CloudFormation console into groups:

- **Query Services Configuration**: CPU, Memory, and Replicas for query-api and query-worker
- **Worker Services Configuration**: Memory and Replicas for ingest/compact/rollup services
- **Other Services Configuration**: Replicas only for pubsub and boxer

### Security Considerations

- Never hardcode secrets - use Secrets Manager or SSM parameters
- All ECS tasks run with `AssignPublicIp: DISABLED`
- Follow existing IAM policy patterns for new permissions
- Database connections always use SSL (`LRDB_SSLMODE: require`)
- Database credentials stored in AWS Secrets Manager
- Application secrets (HMAC keys, Grafana passwords) auto-generated
- ECS task roles follow principle of least privilege
- All tasks run in private subnets with no public IP assignment

## Coding style

- Follow existing coding style as much as practical.
- Make sure there are no trailing whitespace or extra blank lines.
- All code should be formatted properly.
- All text-like files should have a final newline, not end on a character, unless that is how that file format usually works.
- Useful comments welcome, verbosity should be minimal, and generally only document non-obvious code.
- It is OK to add "section" style comments.
- Markdown unordered lists should use a "-" not "*".
- Markdown ordered lists should repeat "1." for each item.
- Markdown should have blank lines between header lines, code blocks, etc. and other items.
- Never add advertisements for Claude or Anthropic to any docs or commit messages.
- Don't use emoji

- If my coworker (user) asks me to change ECS containers to non-root, remind them that bind mounts will require root.
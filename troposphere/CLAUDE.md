# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains CloudFormation templates for deploying CardinalHQ's Lakerunner platform on AWS.
It uses the Python-based Troposphere templates for better maintainability and simplified deployment in air-gapped environments.

## Architecture

The deployment is structured as three separate CloudFormation stacks that must be deployed in order:

1. **CommonInfra** (`common_infra.py`) - Core infrastructure including VPC resources, RDS database, EFS, S3 bucket, SQS queue, and optionally an ALB
2. **Migration** (`migration_task.py`) - Database migration task that runs once during initial setup
3. **Services** (`services.py`) - ECS Fargate services for all Lakerunner microservices

### Stack Dependencies

- Migration depends on CommonInfra exports (database, networking, security groups)
- Services depends on CommonInfra exports (all infrastructure resources)
- Services auto-detects ALB presence from CommonInfra without requiring user input

### Configuration System

The templates use a unified configuration approach:

- **defaults.yaml** - Contains all default configurations (API keys, storage profiles, service definitions)
- **Cross-stack imports** - Services automatically import values from CommonInfra using CloudFormation exports
- **Parameter minimization** - Only ask users for what cannot be determined from other stacks
- **Air-gapped support** - Container image parameters allow overriding public ECR images

## Build System

### Commands

- `./build.sh` - Generate all CloudFormation templates with validation
- `python3 <template>.py` - Generate individual template
- `cfn-lint out/<template>.yaml` - Validate specific template

### Environment

The build system uses a Python virtual environment with dependencies in the `requirements.txt` file.

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

### Security Best Practices

- Database credentials stored in AWS Secrets Manager
- Application secrets (HMAC keys, Grafana passwords) auto-generated
- ECS task roles follow principle of least privilege
- All tasks run in private subnets with no public IP assignment

## Service Architecture

Lakerunner consists of microservices that process telemetry data:

- **pubsub-sqs** - Receives SQS notifications from S3 bucket
- **ingest-logs/metrics** - Process raw log and metric files
- **compact-logs/metrics** - Optimize storage format
- **rollup-metrics** - Pre-aggregate metrics for faster queries
- **sweeper** - Clean up temporary files
- **query-api** - REST API for data queries (ALB-attached)
- **query-worker** - Query execution engine
- **grafana** - Visualization dashboard (ALB-attached)

All services share:

- Common task execution and task roles
- Unified secret injection from Secrets Manager and SSM
- Standardized logging to CloudWatch
- EFS mount for shared scratch space (/scratch)
- Health checks appropriate to service type (Go, Scala, cURL)

## Development Guidelines

### Template Modifications

- Always test with `./build.sh` after changes
- Use `cfn-lint` validation - address errors, safe and explainable warnings are acceptable
- Maintain parameter minimization - only ask users for what's necessary
- Follow existing cross-stack import patterns for new resources

### Adding New Services

1. Add service configuration to `defaults.yaml`
2. Services template will automatically create ECS service, task definition, IAM roles
3. For ALB attachment, set `ingress.attach_alb: true` in service config
4. For EFS access, define `efs_mounts` with access point configuration

### Security Considerations

- Never hardcode secrets - use Secrets Manager or SSM parameters
- All ECS tasks run with `AssignPublicIp: DISABLED`
- Follow existing IAM policy patterns for new permissions
- Database connections always use SSL (`LRDB_SSLMODE: require`)

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

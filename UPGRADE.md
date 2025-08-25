# Lakerunner CloudFormation Upgrade Guide

This guide covers upgrading existing Lakerunner deployments using CloudFormation.

## General Upgrade Order

Always follow this order when upgrading multiple stacks:

1. **Common** - Core infrastructure (RDS, ECS cluster, networking)
2. **Migration** - Database migrations and schema updates
3. **Services** - Main Lakerunner services
4. **OTEL Collector** - Telemetry collection (if used)
5. **Grafana** - Monitoring dashboard (if used)

## Upgrade Methods

### Method 1: Parameter-Only Updates

For container image updates or configuration changes that don't require new templates:

1. Navigate to CloudFormation console → Stacks
2. Select your stack → **Update**
3. Choose **Use current template**
4. Modify parameters (e.g., container images, ALB scheme)
5. **Create change set** to preview changes
6. Review change set → **Execute** if satisfied

**Common parameter updates:**

- Container images: `GoServicesImage`, `QueryApiImage`, `QueryWorkerImage`, `GrafanaImage`
- ALB configuration: `AlbScheme` (internal ↔ internet-facing)
- Resource sizing: `Cpu`, `MemoryMiB`
- Telemetry: `OtelEndpoint`

### Method 2: Template Updates

When template code changes (new features, bug fixes):

1. Generate new templates: `make build`
2. Navigate to CloudFormation console → Stacks
3. Select your stack → **Update**
4. Choose **Replace current template** → **Upload template file**
5. Upload new template from `generated-templates/`
6. Update parameters if needed
7. **Create change set** to preview changes
8. Review change set → **Execute** if satisfied

## Common Upgrade Scenarios

### Container Image Updates

**Scope:** Usually Services stack only (Common/OTEL/Grafana rarely change)

**Steps:**

1. Services stack → Update parameters:
   - `GoServicesImage`: `public.ecr.aws/cardinalhq.io/lakerunner:v1.2.2`
   - `QueryApiImage`: `public.ecr.aws/cardinalhq.io/lakerunner/query-api:v1.2.2`
   - `QueryWorkerImage`: `public.ecr.aws/cardinalhq.io/lakerunner/query-worker:v1.2.2`

### ALB Scheme Change (Internal → Internet-Facing)

**Prerequisites:** Common stack must have PublicSubnets configured

**Steps:**

1. Services stack → Update parameter:
   - `AlbScheme`: Change from `internal` to `internet-facing`
2. (Optional) Grafana stack → Update parameter:
   - `AlbScheme`: Change from `internal` to `internet-facing`

### Database Schema Updates

**Steps:**

1. Common stack → Update if database changes needed
2. **Migration stack → Update** (runs schema migrations)
3. Services stack → Update (uses new schema)

### Adding New Features

**Steps (full template upgrade):**

1. Update templates: `make build`
2. Common stack → Upload new template
3. Migration stack → Upload new template
4. Services stack → Upload new template
5. OTEL/Grafana stacks → Upload new templates if changed

## Rollback Strategy

**CloudFormation rollback:**

- Stack updates can be rolled back via console: Stack → **Cancel update** (during execution) or previous template/parameters

**Application rollback:**

- Container images: Update Services stack with previous image versions
- Configuration: Revert parameter values using previous values

## Monitoring Upgrades

**During updates:**

- Monitor CloudFormation Events tab for progress
- Check ECS service health in ECS console
- Verify application logs in CloudWatch

**After updates:**

- Confirm services are healthy in ECS console
- Test application functionality
- Check ALB target group health (if applicable)
- Verify database connectivity and migrations completed

## Troubleshooting

**Common issues:**

- **Stack drift**: Use **Detect drift** to identify manual changes
- **Resource conflicts**: Review change set carefully for resource replacements
- **Parameter validation**: Check parameter constraints if updates fail
- **Dependency issues**: Ensure proper upgrade order (Common → Migration → Services)

**Recovery:**

- Use **Cancel update** during execution if issues detected
- Rollback to previous working configuration
- Check CloudWatch logs for application-specific errors

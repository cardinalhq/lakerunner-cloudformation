# Building Lakerunner CloudFormation Templates

This document covers how to build and modify the CloudFormation templates from source.

## Overview

The Lakerunner CloudFormation templates are generated using Python and the Troposphere library. This allows for better maintainability, code reuse, and simplified deployment in air-gapped environments.

## Requirements

- **Python 3.7+**
- **Virtual environment support**
- **Git** (for cloning the repository)

## Quick Start

1. **Clone the repository:**
   ```bash
   git clone https://github.com/cardinalhq/lakerunner-cloudformation.git
   cd lakerunner-cloudformation/
   ```

2. **Generate all templates:**
   ```bash
   ./build.sh
   ```

   The build script will:
   - Create a Python virtual environment (`.venv/`)
   - Install dependencies from `requirements.txt`
   - Generate all CloudFormation templates in `generated-templates/`
   - Validate templates with `cfn-lint`

## Build System Architecture

### Source Files

The `src/` directory contains Python templates that generate CloudFormation YAML:

**Root Orchestration Stack:**
- **`src/lakerunner_root.py`** - Root stack that orchestrates all nested stacks

**Infrastructure Stacks:**
- **`src/lakerunner_vpc.py`** - VPC with subnets and endpoints
- **`src/lakerunner_rds.py`** - PostgreSQL database
- **`src/lakerunner_storage.py`** - S3 bucket and SQS queue
- **`src/lakerunner_msk.py`** - MSK Kafka cluster
- **`src/lakerunner_ecs.py`** - ECS cluster infrastructure

**Service Stacks:**
- **`src/lakerunner_ecs_setup.py`** - Database and Kafka setup task
- **`src/lakerunner_ecs_services.py`** - Core application services
- **`src/lakerunner_ecs_collector.py`** - OTEL collector service
- **`src/lakerunner_ecs_grafana.py`** - Grafana dashboard service

### Configuration Files

Stack-specific configuration files define defaults and service settings:

- **`lakerunner-stack-defaults.yaml`** - Core Lakerunner services configuration
- **`lakerunner-grafana-defaults.yaml`** - Grafana service configuration

### Generated Output

The `generated-templates/` directory contains the final CloudFormation templates:

- **`lakerunner-root.yaml`** - Root orchestration stack
- **`lakerunner-vpc.yaml`** - VPC infrastructure
- **`lakerunner-rds.yaml`** - Database
- **`lakerunner-storage.yaml`** - S3/SQS
- **`lakerunner-msk.yaml`** - Kafka
- **`lakerunner-ecs.yaml`** - ECS cluster
- **`lakerunner-ecs-setup.yaml`** - Setup task
- **`lakerunner-ecs-services.yaml`** - Application services
- **`lakerunner-ecs-collector.yaml`** - OTEL collector
- **`lakerunner-ecs-grafana.yaml`** - Grafana

## Development Workflow

### Making Changes

1. **Edit source files** in `src/` directory
2. **Update configuration** in relevant YAML files
3. **Regenerate templates** with `./build.sh`
4. **Validate changes** with cfn-lint output
5. **Test deployment** in a development environment

### Adding New Services

To add a new service to the core Lakerunner stack:

1. **Add service configuration** to `lakerunner-stack-defaults.yaml`:
   ```yaml
   services:
     my-new-service:
       image: "public.ecr.aws/cardinalhq.io/my-service:latest"
       command: ["/app/bin/my-service"]
       cpu: 512
       memory_mib: 1024
       replicas: 1
       environment:
         SERVICE_CONFIG: "value"
       health_check:
         type: "go"
         command: ["/app/bin/my-service", "health"]
   ```

2. **Regenerate templates** with `./build.sh`
3. **Deploy updated services stack**

### Adding New Demo Applications

To add a new demo application:

1. **Add app configuration** to `demo-apps-stack-defaults.yaml`:
   ```yaml
   demo_apps:
     my-demo-app:
       image: "public.ecr.aws/cardinalhq.io/demo/my-app:latest"
       cpu: 256
       memory_mib: 512
       environment:
         APP_CONFIG: "demo-value"
       health_check:
         type: "http"
         port: 8080
         path: "/health"
   ```

2. **Regenerate templates** with `./build.sh`
3. **Deploy updated demo apps stack**

## Build Script Details

The `build.sh` script performs these steps:

1. **Environment Setup:**
   - Creates Python virtual environment if needed
   - Installs/updates dependencies from `requirements.txt`

2. **Template Generation:**
   - Runs each Python template file
   - Outputs YAML to `generated-templates/`
   - Validates each template with `cfn-lint`

3. **Validation:**
   - Reports any CloudFormation syntax errors
   - Shows warnings (most are cosmetic and safe to ignore)

### Manual Template Generation

You can also generate templates individually:

```bash
# Activate virtual environment
source .venv/bin/activate

# Generate specific template
python3 src/lakerunner_root.py > generated-templates/lakerunner-root.yaml

# Validate template
cfn-lint generated-templates/lakerunner-common.yaml
```

## Troposphere Usage

### Key Patterns

The templates use consistent patterns for maintainability:

**Cross-Stack Resource Sharing:**
```python
# Export from one stack
Export=Export(name=Sub("${AWS::StackName}-ResourceName"))

# Import in another stack
ResourceValue = ImportValue(Sub("${StackName}-ResourceName"))
```

**Configuration Loading:**
```python
def load_config(config_file="stack-defaults.yaml"):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", config_file)
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)
```

**Service Generation:**
```python
for service_name, service_config in services.items():
    # Create ECS service, task definition, IAM roles
    # Following consistent patterns across all services
```

### Best Practices

- **Use consistent naming** - Resources follow predictable patterns
- **Leverage configuration files** - Don't hardcode values in Python
- **Follow existing patterns** - Match the style of existing services
- **Add proper descriptions** - Help users understand parameters
- **Export important resources** - Enable cross-stack references

## Validation and Testing

### CloudFormation Validation

The build system uses `cfn-lint` to validate templates:

```bash
# Install cfn-lint (included in requirements.txt)
pip install cfn-lint

# Validate all templates
cfn-lint generated-templates/*.yaml

# Validate specific template with context
cfn-lint --template generated-templates/lakerunner-services.yaml
```

### Common Warnings

These warnings are typically safe to ignore:

- **W1030** - Empty PublicSubnets parameter (expected for internal ALB)

The build script suppresses **W1020** (cosmetic Fn::Sub usage) to keep output clean.

### Testing Changes

1. **Syntax validation** - cfn-lint catches CloudFormation errors
2. **Template diff** - Compare generated YAML before/after changes
3. **Development deployment** - Test in isolated AWS environment
4. **Parameter validation** - Ensure all required parameters work correctly

## Dependencies

The build system requires these Python packages (defined in `requirements.txt`):

- **troposphere** - CloudFormation template generation
- **cfn-lint** - CloudFormation template validation  
- **pyyaml** - YAML configuration file parsing

## Troubleshooting

### Common Build Issues

1. **Python virtual environment errors:**
   ```bash
   # Remove and recreate virtual environment
   rm -rf .venv
   ./build.sh
   ```

2. **Missing dependencies:**
   ```bash
   # Force reinstall dependencies
   source .venv/bin/activate
   pip install --force-reinstall -r requirements.txt
   ```

3. **Template validation errors:**
   - Check cfn-lint output for specific line numbers
   - Verify CloudFormation resource syntax
   - Ensure all imports/exports are correctly referenced

4. **Configuration file errors:**
   - Validate YAML syntax in configuration files
   - Ensure all required keys are present
   - Check indentation (YAML is sensitive to whitespace)

### Getting Help

- **CloudFormation Documentation** - AWS CloudFormation resource reference
- **Troposphere Documentation** - Python CloudFormation library docs
- **cfn-lint Documentation** - Template validation rules and checks
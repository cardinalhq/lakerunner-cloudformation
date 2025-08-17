# Demo Applications for Lakerunner

This document covers deploying OTEL-instrumented demo applications to test telemetry collection with Lakerunner.

## Overview

The demo applications stack provides sample applications with OpenTelemetry instrumentation that send telemetry data to the OTEL collector for processing by Lakerunner.

## Prerequisites

Before deploying demo applications, you must have:

1. **Core Lakerunner deployed** - Common infrastructure and services stacks
2. **OTEL Collector deployed** - See [README-OTEL-COLLECTOR.md](README-OTEL-COLLECTOR.md)
3. **Networking configured** - VPC, subnets, and security groups from common infrastructure

## Architecture

The demo applications stack creates:

- **ECS Fargate services** for each demo application
- **OTEL instrumentation** configured to send data to the OTEL collector
- **Shared infrastructure** reusing ECS cluster, security groups, and IAM roles from the services stack
- **CloudWatch logging** for application monitoring

## Deployment

### Step 1: Deploy Demo Applications Stack

Deploy `generated-templates/lakerunner-demo-sample-apps.yaml`. Required parameters:

- **CommonInfraStackName** – Name of the CommonInfra stack (e.g., "lakerunner-common")
- **ServicesStackName** – Name of the Services stack (e.g., "lakerunner-services")  
- **OtelCollectorStackName** – Name of the OTEL Collector stack (e.g., "lakerunner-otel")

Optional parameters:
- **SampleAppImage** – Container image for sample app (default: public.ecr.aws/cardinalhq.io/lakerunner-demo/sample-app:latest)

**Example AWS CLI deployment:**

```bash
aws cloudformation create-stack \\
  --stack-name lakerunner-demo-apps \\
  --template-body file://generated-templates/lakerunner-demo-sample-apps.yaml \\
  --parameters ParameterKey=CommonInfraStackName,ParameterValue=lakerunner-common \\
               ParameterKey=ServicesStackName,ParameterValue=lakerunner-services \\
               ParameterKey=OtelCollectorStackName,ParameterValue=lakerunner-otel \\
  --capabilities CAPABILITY_IAM
```

## Demo Applications

### Sample App

The sample application demonstrates:

- **HTTP server** listening on port 8080
- **OTEL instrumentation** for traces, metrics, and logs
- **Health checks** via `/health` endpoint
- **Automatic telemetry export** to OTEL collector

**Application features:**
- REST API endpoints for testing
- Configurable logging levels
- Custom metrics generation
- Trace span creation
- Error simulation for testing

## OTEL Configuration

### Automatic Configuration

Demo applications are automatically configured with:

```bash
OTEL_SERVICE_NAME=<app-name>
OTEL_EXPORTER_OTLP_ENDPOINT=http://<otel-collector-alb>:4317
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_RESOURCE_ATTRIBUTES=service.name=<app-name>
```

### Manual Configuration

For custom OTEL configuration, modify the application environment variables in `demo-apps-stack-defaults.yaml`:

```yaml
demo_apps:
  sample-app:
    environment:
      OTEL_EXPORTER_OTLP_ENDPOINT: "http://custom-collector:4317"
      OTEL_EXPORTER_OTLP_HEADERS: "api-key=your-key"
      OTEL_RESOURCE_ATTRIBUTES: "service.name=sample-app,environment=demo"
```

## Testing Telemetry Collection

### Generate Test Data

Once deployed, you can generate telemetry data by interacting with the demo applications:

1. **Get application endpoints** from ECS console (private IP addresses)
2. **Send HTTP requests** to trigger telemetry generation
3. **Monitor CloudWatch logs** for application activity
4. **Check S3 bucket** for exported telemetry data

### Example Test Requests

```bash
# Health check
curl http://<app-private-ip>:8080/health

# Generate traces and metrics
curl http://<app-private-ip>:8080/api/test
curl http://<app-private-ip>:8080/api/metrics
curl http://<app-private-ip>:8080/api/error  # Test error handling
```

### Verify Data Flow

1. **Application logs** - Check `/ecs/<app-name>` in CloudWatch
2. **OTEL collector logs** - Check `/ecs/otel-gateway` in CloudWatch  
3. **S3 bucket** - Look for data in `otel-raw/` prefix
4. **Lakerunner services** - Monitor ingestion service logs

## Monitoring

### CloudWatch Logs

Monitor demo application logs:

```bash
aws logs filter-log-events --log-group-name /ecs/sample-app --start-time $(date -d '1 hour ago' +%s)000
```

### ECS Console

- **Service status** - Monitor running tasks and health checks
- **Task definitions** - View container configuration and resource usage
- **Service metrics** - CPU, memory, and network utilization

### OTEL Data

Telemetry data flows through this pipeline:

1. **Demo App** → generates OTEL traces/metrics/logs
2. **OTEL Collector** → receives and processes telemetry  
3. **S3 Storage** → stores raw telemetry data
4. **Lakerunner Services** → process and compact data
5. **Query API** → provides access to processed data
6. **Grafana** → visualizes telemetry data

## Configuration Files

### Demo Apps Configuration

Configuration is stored in `demo-apps-stack-defaults.yaml`:

```yaml
demo_apps:
  sample-app:
    image: "public.ecr.aws/cardinalhq.io/lakerunner-demo/sample-app:latest"
    command: ["./sample-app"]
    cpu: 512
    memory_mib: 1024
    replicas: 1
    environment:
      APP_NAME: "sample-app"
      LOG_LEVEL: "info"
      HTTP_PORT: "8080"
    health_check:
      type: "http"
      port: 8080
      path: "/health"
```

### Adding New Demo Apps

To add additional demo applications:

1. **Add configuration** to `demo-apps-stack-defaults.yaml`
2. **Define container image** and resource requirements
3. **Configure OTEL environment** variables
4. **Set up health checks** and networking
5. **Regenerate templates** with `./build.sh`

Example new app configuration:

```yaml
demo_apps:
  my-new-app:
    image: "my-registry/demo-app:latest"
    command: ["./start-app"]
    cpu: 256
    memory_mib: 512
    replicas: 2
    environment:
      APP_ENV: "demo"
      LOG_LEVEL: "debug"
    health_check:
      type: "http"
      port: 9090
      path: "/status"
```

## Troubleshooting

### Common Issues

1. **Demo app won't start**
   - Check CloudWatch logs for startup errors
   - Verify container image is accessible
   - Ensure sufficient CPU/memory allocation

2. **No telemetry data**
   - Verify OTEL collector is running and healthy
   - Check network connectivity between demo app and collector
   - Validate OTEL environment variables

3. **Health checks failing**
   - Confirm application is listening on configured port
   - Test health endpoint manually if possible
   - Check security group rules

4. **High resource usage**
   - Adjust CPU/memory limits in configuration
   - Monitor CloudWatch metrics for resource utilization
   - Scale replica count as needed

### Logs Analysis

Check demo application logs for OTEL-related messages:

```bash
# Filter for OTEL-related log entries
aws logs filter-log-events \\
  --log-group-name /ecs/sample-app \\
  --filter-pattern "OTEL" \\
  --start-time $(date -d '1 hour ago' +%s)000
```

### Network Connectivity

Verify demo apps can reach the OTEL collector:

1. **Check security groups** - Ensure outbound HTTPS/HTTP allowed
2. **Test DNS resolution** - OTEL collector ALB should resolve
3. **Monitor collector logs** - Look for incoming connections

## Integration Examples

### Custom Application Integration

To integrate your own applications with the demo setup:

1. **Configure OTEL SDK** in your application
2. **Set OTEL environment variables** to point to collector
3. **Deploy to ECS** using similar patterns as demo apps
4. **Monitor telemetry flow** through the pipeline

### Language-Specific Examples

- **Go**: Use `go.opentelemetry.io/otel` SDK
- **Java**: Use `io.opentelemetry:opentelemetry-api` 
- **Python**: Use `opentelemetry-api` and `opentelemetry-sdk`
- **Node.js**: Use `@opentelemetry/api` and `@opentelemetry/sdk-node`

All language SDKs support the same OTEL environment variables for configuration.

## See Also

- **[README.md](README.md)** - Main Lakerunner deployment guide
- **[README-OTEL-COLLECTOR.md](README-OTEL-COLLECTOR.md)** - OTEL collector setup and configuration
- **[README-BUILDING.md](README-BUILDING.md)** - Building and development guide
# Lakerunner Maestro + MCP Gateway Stack

Date: 2026-04-21

## Goal

Deliver a new CloudFormation stack that runs Maestro (v0.23.0) and the MCP
Gateway as a single ECS Fargate service, exposed via a stack-local Application
Load Balancer, and remove the now-defunct MCP Gateway / Conductor / Maestro
sidecars from the Grafana stack.

Scope is intentionally narrow: one replica of each workload, no auto-scaling,
HTTP only on the ALB, OIDC config exposed as plain CloudFormation parameters.

## Context

- The Helm chart at `../charts/maestro` (Chart 0.4.8, appVersion `v0.23.0`)
  deploys two workloads from the unified image
  `public.ecr.aws/cardinalhq.io/maestro`. Maestro serves both the API and the
  UI on port 4200; MCP Gateway runs the alt entrypoint on port 8080.
- Maestro reads its OIDC configuration from environment variables. The server
  is a JWT verifier only — there is no client secret. The relevant envs (see
  `../conductor/packages/maestro/src/index.ts`):
    - `OIDC_ISSUER_URL` — required to enable OIDC.
    - `OIDC_AUDIENCE` — default `maestro-ui`; the web UI also uses this as its
      OAuth `client_id`.
    - `OIDC_SUPERADMIN_GROUP` — default `maestro-superadmin`.
    - `OIDC_JWKS_URL` — optional override.
    - `OIDC_SUPERADMIN_EMAILS` — optional, comma-separated.
    - `OIDC_TRUST_UNVERIFIED_EMAILS` — optional `true` / `false`.
- Maestro requires a PostgreSQL database and self-migrates its schema on
  startup. The chart expects `MAESTRO_DB_*` env vars and builds
  `MAESTRO_DATABASE_URL` from them.
- The existing Grafana stack already carries MCP Gateway, Conductor Server,
  and Maestro Server as sidecars (`lakerunner-grafana-defaults.yaml`). These
  are being removed; Grafana keeps only the datasource plugin.
- The repository's convention for database-backed stacks (see
  `lakerunner_grafana_service.py`) is to reuse the CommonInfra RDS instance
  and run a psql-based init container that creates the per-service database
  and user before the application containers start.

## Architecture

Single ECS Fargate service in private subnets, one stack-local ALB, one task
definition with three containers (init + two apps).

```
CommonInfra (exports)
   |
   +-- ClusterArn, TaskSGId, VpcId, PrivateSubnets, PublicSubnets,
       DbEndpoint, DbPort, DbSecretArn
   |
   v
MaestroStack
   +-- MaestroAlbSG (0.0.0.0/0 :80)
   +-- SecurityGroupIngress on TaskSGId:4200 from MaestroAlbSG
   +-- ALB (scheme = AlbScheme param)
   |     +-- Listener :80 -> TargetGroup (/api/health, 200) -> task :4200
   +-- MaestroDbSecret (Secrets Manager, generated password)
   +-- Log groups: /ecs/<stack>/db-init, /mcp-gateway, /maestro
   +-- ExecutionRole, TaskRole
   +-- TaskDefinition (Fargate, ARM64, 1024 CPU / 2048 MiB by default)
   |     +-- DbInit (essential=false) -> SUCCESS
   |     +-- McpGateway (essential=true) -> HEALTHY
   |     +-- Maestro  (essential=true, DependsOn McpGateway HEALTHY)
   +-- Service (DesiredCount=1, awsvpc, TaskSGId, private subnets)
```

### Container details

- **DbInit**
  - Image: `ghcr.io/cardinalhq/initcontainer-grafana:latest` (the same
    generic `psql` bootstrapper the Grafana stack already uses; it reads
    `GRAFANA_DB_NAME` / `GRAFANA_DB_USER` / `GRAFANA_DB_PASSWORD` and the
    `PG*` envs — the "grafana" in the image name is historical and does
    nothing Grafana-specific).
  - Env: `PGHOST`, `PGPORT`, `PGDATABASE=postgres`, `PGSSLMODE=require`,
    `GRAFANA_DB_NAME=maestro`, `GRAFANA_DB_USER=maestro`.
  - Secrets: `PGUSER`, `PGPASSWORD` from the CommonInfra master DB secret;
    `GRAFANA_DB_PASSWORD` from `MaestroDbSecret:password`.
  - Essential: false. Exits 0 once the DB/user exist (idempotent).
- **McpGateway**
  - Image: the `MaestroImage` parameter
    (default `public.ecr.aws/cardinalhq.io/maestro:v0.23.0`).
  - Command: `["/app/entrypoint.sh", "mcp-gateway"]`.
  - Port: 8080 (container port only; no SG rule — same task ENI as Maestro).
  - Env: `MAESTRO_DB_*` (shared helper), `MCP_PORT=8080`,
    `MCP_DEBUG_PORT=9090`.
  - Health: `wget --spider http://localhost:8080/healthz`, interval 30s.
  - DependsOn `DbInit=SUCCESS`.
  - Security: non-root (65532), read-only rootfs, drop all caps.
- **Maestro**
  - Same image as MCP Gateway; default entrypoint.
  - Port: 4200 (advertised via the ALB target group).
  - Env: `MAESTRO_DB_*`, `MCP_GATEWAY_URL=http://localhost:8080`, `PORT=4200`,
    optional `MAESTRO_BASE_URL` (only emitted when non-empty), plus the OIDC
    envs (only emitted when non-empty).
  - Health: `wget --spider http://localhost:4200/api/health`, interval 30s.
  - DependsOn `DbInit=SUCCESS` and `McpGateway=HEALTHY`.
  - Security: non-root (65532), read-only rootfs, drop all caps. A `tmp`
    emptyDir volume is mounted at `/tmp` for the read-only rootfs.

### Shared `MAESTRO_DB_*` env block

Generated once and attached to both app containers (and the init container's
password secret in the corresponding Grafana-style form):

- `MAESTRO_DB_HOST` = `ImportValue(<common>-DbEndpoint)`
- `MAESTRO_DB_PORT` = `ImportValue(<common>-DbPort)` (the repo convention uses
  5432 but we still import the value).
- `MAESTRO_DB_NAME` = `maestro`
- `MAESTRO_DB_USER` = `maestro`
- `MAESTRO_DB_SSLMODE` = `require`
- `MAESTRO_DB_PASSWORD` = secret injection from
  `${MaestroDbSecret}:password::`
- `MAESTRO_DATABASE_URL` =
  `postgresql://$(MAESTRO_DB_USER):$(MAESTRO_DB_PASSWORD)@$(MAESTRO_DB_HOST):$(MAESTRO_DB_PORT)/$(MAESTRO_DB_NAME)?sslmode=$(MAESTRO_DB_SSLMODE)`
  (plain `Environment` — ECS performs env-var substitution at container start).

## Parameters

Grouped in the CloudFormation console:

- **Infrastructure**
    - `CommonInfraStackName` (String, required) — name of the CommonInfra
      stack to import from.
    - `AlbScheme` (String, AllowedValues `internal` / `internet-facing`,
      default `internal`) — ALB scheme. When `internet-facing` the stack
      uses the imported `PublicSubnets`; otherwise `PrivateSubnets`.
- **Task Sizing**
    - `TaskCpu` (String, default `1024`)
    - `TaskMemoryMiB` (String, default `2048`)
- **Image**
    - `MaestroImage` (String, default
      `public.ecr.aws/cardinalhq.io/maestro:v0.23.0` from
      `lakerunner-maestro-defaults.yaml`).
- **OIDC (all optional)**
    - `OidcIssuerUrl` (String, default `""`) — leaving blank disables OIDC.
    - `OidcAudience` (String, default `maestro-ui`)
    - `OidcSuperadminGroup` (String, default `maestro-superadmin`)
    - `OidcJwksUrl` (String, default `""`)
    - `OidcSuperadminEmails` (String, default `""`) — comma-separated.
    - `OidcTrustUnverifiedEmails` (String, AllowedValues `true` / `false`,
      default `false`)
- **Misc**
    - `MaestroBaseUrl` (String, default `""`) — when set, forwarded to the
      task as `MAESTRO_BASE_URL`.

No secret parameters: Maestro does not consume an OIDC client secret, and the
DB password is auto-generated in Secrets Manager.

### Conditions

- `IsInternetFacing` = `Equals(Ref(AlbScheme), "internet-facing")`.

OIDC envs are emitted unconditionally at template-render time, with defaults
that evaluate to empty strings when the user leaves them blank. Maestro treats
an empty `OIDC_ISSUER_URL` as "OIDC disabled", so no CloudFormation conditions
are needed for OIDC wiring.

## Secrets & IAM

- **`MaestroDbSecret`** — `AWS::SecretsManager::Secret` with
  `SecretString='{"username":"maestro"}'` and `GenerateSecretString`
  (`GenerateStringKey=password`, `PasswordLength=32`, same `ExcludeCharacters`
  set as the Grafana stack).
- **Execution role** — managed
  `arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy` plus
  an inline policy granting `secretsmanager:GetSecretValue` on:
    - `arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${AWS::StackName}-*`
    - the imported `${CommonInfraStackName}-DbSecretArn` (wildcarded).
- **Task role** — inline policy with `logs:CreateLogStream` and
  `logs:PutLogEvents` on `*`. No Bedrock, no SSM, no S3.

## Security Groups

- New `MaestroAlbSG` with egress `-1/0.0.0.0/0` and ingress TCP 80 from
  `0.0.0.0/0` (the same open-ingress pattern as the Grafana ALB — access
  control is at the ALB scheme / network layer).
- A `SecurityGroupIngress` added to the imported `TaskSGId` allowing TCP
  4200 from `MaestroAlbSG`.
- MCP Gateway (8080) and its debug port (9090) are container-local; no SG
  rule is needed because Maestro and MCP Gateway share a single task ENI.

## ALB

- `Scheme = Ref(AlbScheme)`, subnets toggled by `IsInternetFacing`.
- `Listener` on port 80 HTTP, `DefaultActions` forward to `MaestroTg`.
- `TargetGroup` on `VpcId`, port 4200 HTTP, target type `ip`,
  `HealthCheckPath=/api/health`, matcher `200`, `HealthyThresholdCount=2`,
  `UnhealthyThresholdCount=3`, `deregistration_delay=30s`,
  `stickiness.enabled=false`. Name templated like Grafana:
  `If(IsInternetFacing, ${AWS::StackName}-ext, ${AWS::StackName}-int)`.

## ECS Service

- `LaunchType=FARGATE`, `DesiredCount=1`, `awsvpc` networking with the
  imported `TaskSGId` and `PrivateSubnets`.
- `LoadBalancers` binds container `Maestro:4200` to `MaestroTg`.
- `DependsOn` the listener so the target group is attached to the ALB before
  the service registers.
- `EnableExecuteCommand=True`, `EnableECSManagedTags=True`,
  `PropagateTags=SERVICE`, service tags `Name`, `ManagedBy=Lakerunner`,
  `Environment=${AWS::StackName}`, `Component=Service`.

## Outputs

All exported as `${AWS::StackName}-<suffix>`:

- `MaestroAlbDNS` — the ALB DNS name.
- `MaestroAlbArn`
- `MaestroServiceArn`
- `MaestroUrl` (no export; convenience output) — `http://${MaestroAlbDns}`.
- `MaestroDbSecretArn` — handy for operators who need to rotate.

## Build & Test integration

- `build.sh` gains step `07. Generating Lakerunner Maestro Service...`
  writing `generated-templates/lakerunner-07-maestro-service.yaml` and
  running `cfn-lint` on it.
- `Makefile` — inspect during implementation. If it iterates
  `generated-templates/*.yaml` (likely, based on the existing test targets)
  no edit is needed. If it enumerates templates by name, add the new one.
- `lakerunner-maestro-defaults.yaml` added at the repo root:

    ```yaml
    # Defaults for the Maestro + MCP Gateway stack
    images:
      maestro: "public.ecr.aws/cardinalhq.io/maestro:v0.23.0"
      db_init: "ghcr.io/cardinalhq/initcontainer-grafana:latest"
    task:
      cpu: 1024
      memory_mib: 2048
    ports:
      maestro: 4200
      mcp_gateway: 8080
      mcp_gateway_debug: 9090
      alb_listener: 80
    ```

- New test file `tests/test_maestro_service_simple.py` following the
  `test_grafana_service_simple.py` shape. It mocks
  `load_maestro_config` (or equivalent) and asserts:
    - required parameters exist with correct types, defaults, and
      `AllowedValues`;
    - `IsInternetFacing` condition is present;
    - required resources exist: `MaestroAlb`, `MaestroTg`, `MaestroListener`,
      `MaestroAlbSG`, `MaestroTaskDef` with three containers in order
      (`DbInit`, `McpGateway`, `Maestro`), `MaestroService`,
      `MaestroDbSecret`, three log groups;
    - exports `<stack>-MaestroAlbDNS`, `<stack>-MaestroServiceArn`,
      `<stack>-MaestroDbSecretArn` are present;
    - the task definition sets `NetworkMode=awsvpc`,
      `RequiresCompatibilities=["FARGATE"]`, and ARM64 runtime platform.
- Existing test suites (`tests/test_condition_validation.py`,
  `tests/test_parameter_validation.py`) must keep passing with the new
  template included; any param-consistency tests that enumerate templates
  must be extended to include the Maestro stack.

## Grafana stack cleanup

In `src/lakerunner_grafana_service.py`:

- Remove `ContainerDefinition`s `McpGateway`, `ConductorServer`,
  `MaestroServer`.
- Remove their log groups: `McpGatewayLogGroup`,
  `ConductorServerLogGroup`, `MaestroServerLogGroup`.
- Remove secrets `AiInternalSecret` and `LakerunnerApiKeySecret`.
  **Keep** a `LakerunnerApiKey` parameter and inject its value into the
  Grafana datasource `secureJsonData.apiKey` so the datasource still ships
  working out of the box. The `LakerunnerApiKeySecret` is gone because
  nothing else in the stack needs ECS secret injection of it.
- Remove parameter `BedrockModel` and the `BedrockAccess` policy from
  `GrafanaTaskRole`.
- Remove the `DependsOn: McpGateway=HEALTHY` entries from Conductor and
  Maestro, which no longer exist.
- Update the console `ParameterGroups`/`ParameterLabels` metadata to
  drop removed parameters.

In `lakerunner-grafana-defaults.yaml`:

- Remove the `mcp_gateway`, `conductor_server`, `maestro_server`
  top-level sections.
- Remove `images.mcp_gateway`, `images.conductor_server`,
  `images.maestro_server`.
- Keep `api_keys` — it's the default value source for the
  `LakerunnerApiKey` parameter. (If future cleanup wants to drop it
  entirely, switch the parameter to `NoEcho` with no default.)

In `tests/test_grafana_service_simple.py`:

- Drop the `mcp_gateway`, `conductor_server`, `maestro_server` keys from
  `MOCK_CONFIG`.
- Update assertions: container count, container names, log group names,
  secrets list, task role policies.

## Coding conventions followed

- Matches the Grafana stack's use of troposphere, `load_..._config` helper,
  `ci_export`, `ImportValue(Sub(...))`, `SecurityGroupIngress` patterns.
- Markdown uses `-` for unordered lists, `1.` repeated for ordered.
- No Bedrock / no Lakerunner API key paths on this stack.
- No ECS container is rooted; `ReadOnlyRootFilesystem=True`; a `tmp` volume
  is mounted only where Maestro needs it.
- All ECS tasks use `AssignPublicIp=DISABLED` and private subnets, matching
  the repo-wide security convention.

## Out of scope

- Auto-scaling, multiple replicas, canary deploys.
- HTTPS / ACM cert on the ALB (can be added later via a new parameter +
  condition; stays HTTP to match the Grafana stack).
- Secrets Manager storage for any OIDC value — none are secret in Maestro's
  server-side flow.
- Any changes to the Services, Alerting, or OTEL Collector stacks.
- Maestro schema migrations as a separate stack — Maestro self-migrates.

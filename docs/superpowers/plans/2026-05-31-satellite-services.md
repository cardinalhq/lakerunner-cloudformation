# cardinal-satellite-services Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build the standalone `cardinal-satellite-services` CloudFormation template — the otel-collector ECS Fargate service behind its own internal ALB, in a satellite account, writing to the raw bucket created by `satellite-infra-base`.

**Architecture:** A new troposphere generator `src/cardinal_cfn/satellite_services.py` (`build() -> Template` + `__main__`), standalone (not a nested child). It reuses the existing helpers in `src/cardinal_cfn/children/services_common.py` (`build_log_group`, `build_task_definition`, `build_target_group`, `build_listener_rule`) and mirrors the collector taskdef in `src/cardinal_cfn/children/otel.py` and the internal-ALB/OTLP listener in `src/cardinal_cfn/children/alb.py`. Unlike `otel.py` (a child that attaches to a shared ALB and registers Cloud Map), this stack creates its **own** ALB, its **own** collector task/exec roles + security groups, and is **ALB-reachable only** (no Cloud Map, no SQS, no DB).

**Tech Stack:** Python 3, troposphere, pytest, cfn-lint.

## Design anchors (from the spec)

- Collector is a cross-region ingest front door → ALB-fronted. `AlbScheme` parameter, default `internal` (cross-region senders reach it via the customer's TGW/peering). OTLP/HTTP on 4318 is **plain HTTP** (no cert) — same rationale as `alb.py`'s `OtelHttpListener`.
- Self-contained: this stack creates the collector task role, exec role, ALB SG, and task SG (the strict roles-external split is a lakerunner-account concern only).
- Collector writes to `RawBucketName` (from `satellite-infra-base`) using its in-account task role (`s3:PutObject` etc). No SQS consumption (the bucket's own notification → its queue handles that; the lakerunner poller consumes cross-account).

## File Structure

- Create: `src/cardinal_cfn/satellite_services.py` — the generator (~260 lines).
- Create: `tests/templates/test_satellite_services.py` — per-template assertions.
- Modify: `build.sh` — add generation line + cfn-lint entry (after `cardinal-satellite-infra-base.yaml`).
- Modify: `Makefile` — add the template to the `lint:` target list.

`generated-templates/` is gitignored; commit source/tests/build.sh/Makefile only. Stay on branch `design/multi-account-satellite-ingest`.

## Parameters

| Name | Type | Default | Purpose |
|---|---|---|---|
| `RawBucketName` | String | — | Raw bucket to write to (output of satellite-infra-base). `AllowedPattern` `^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$`. |
| `LicenseSecretArn` | String | — | ARN of the Cardinal license secret in this account (collector validates it). |
| `VpcId` | AWS::EC2::VPC::Id | — | VPC. |
| `AlbSubnetsCsv` | String | — | Subnets for the ALB (private when internal; public when internet-facing). |
| `TaskSubnetsCsv` | String | — | Private subnets for the collector tasks. |
| `EcsClusterArn` | String | — | Customer-supplied ECS cluster ARN. |
| `AlbScheme` | String | `internal` | `internal` or `internet-facing` (AllowedValues). |
| `IngestSourceCidr` | String | `10.0.0.0/8` | CIDR allowed to send OTLP to the ALB SG on 4318. |
| `OtelImage` | String | (defaults.images.otel) | via `add_image_override`. |
| `OtelConfigYaml` | String | `""` | Optional inline collector config; blank uses `load_otel_default_config()`. |
| `OtelReplicas` / `OtelCpu` / `OtelMemory` | Number/String | from `defaults["otel"]["otel-gateway"]` | tunables. |

Use `add_parameter_group_metadata` to group (Inputs / Networking / Tunables / Image), mirroring `otel.py`.

## Resources

1. **CollectorExecutionRole** (IAM Role, trust ecs-tasks): managed policy `AmazonECSTaskExecutionRolePolicy`; inline allow `secretsmanager:GetSecretValue` on `Ref(LicenseSecretArn)` (exec role pulls the license secret for the container `secrets` block) and `logs:CreateLogStream`/`logs:PutLogEvents`.
2. **CollectorTaskRole** (IAM Role, trust ecs-tasks): inline policy `cardinal-collector-write` — `s3:PutObject`, `s3:GetObject`, `s3:ListBucket`, `s3:GetBucketLocation` scoped to `arn:${AWS::Partition}:s3:::${RawBucketName}` and `/*` (Sub). No delete, no other buckets.
3. **AlbSecurityGroup** (EC2 SG): ingress tcp 4318 from `Ref(IngestSourceCidr)`; egress all. `GroupDescription` set; `VpcId=Ref(VpcId)`.
4. **TaskSecurityGroup** (EC2 SG): ingress tcp 4318 and 13133 from `AlbSecurityGroup`; egress all.
5. **Alb** (ELBv2 LoadBalancer): `Scheme=Ref(AlbScheme)`, `Subnets=Split(",", Ref(AlbSubnetsCsv))`, `SecurityGroups=[Ref(AlbSecurityGroup)]`, `Type=application`. `apply_policy(alb, "alb")`.
6. **OtelHttpListener** (ELBv2 Listener): Port 4318, Protocol HTTP, default 404 fixed-response — copy from `alb.py`'s `OtelHttpListener`.
7. **TargetGroup**: `services_common.build_target_group(service_key="otel-grpc", vpc_id_param="VpcId", port=4318, health_check_path="/", health_check_port=13133)`.
8. **ListenerRule**: `services_common.build_listener_rule(service_key="otel-grpc", target_group_ref=<tg>, listener_arn_param=<the listener>, path_patterns=["/v1/*"])`. Note `build_listener_rule` takes a `listener_arn_param` (a parameter name); since this stack creates the listener in-template, EITHER (a) add a thin variant that accepts `Ref(listener)`, or (b) keep it simple and inline an `elasticloadbalancingv2.ListenerRule` with `ListenerArn=Ref(otel_listener)`, `Priority=300`, `Conditions` PathPattern `/v1/*`, `Actions` forward to the TG. Prefer (b) inline to avoid touching shared helpers.
9. **LogGroup**: `services_common.build_log_group(service_key="otel-grpc")`.
10. **TaskDefinition**: `services_common.build_task_definition(service_key="otel-grpc", image_ref=<OtelImage>, cpu=Ref("OtelCpu"), memory_mib=Ref("OtelMemory"), command=otel_cfg.get("command"), execution_role_arn_param=<exec role>, task_role_arn=Ref(<task role>), environment=env, secrets=[Secret(Name="LICENSE_DATA", ValueFrom=Ref("LicenseSecretArn"))], log_group_ref=<lg>, container_port=4318)`. `execution_role_arn_param` expects a parameter name string in `otel.py`; since the exec role is created in-template, check the helper — if it does `Ref(name)` internally, pass the role's logical via a small param OR adjust. Inspect `build_task_definition` signature first; if it requires a param name, the cleanest is to keep ExecutionRole/TaskRole as created resources and pass `Ref(role)` — adapt the call or inline the TaskDefinition mirroring `services_common.build_task_definition`'s body. **The implementer must read `services_common.build_task_definition` and use it correctly or inline-mirror it.**
    - `env`: `CHQ_COLLECTOR_CONFIG_YAML` = `If("HasOtelConfigOverride", Ref("OtelConfigYaml"), load_otel_default_config())`; `LRDB_S3_BUCKET=Ref("RawBucketName")`; `LRDB_S3_REGION=Ref("AWS::Region")`; `ORG` and `COLLECTOR` from defaults (mirror otel.py).
11. **CollectorService** (ECS Service): `Cluster=Ref("EcsClusterArn")`, `CapacityProviderStrategy` FARGATE_SPOT weight 1, `DesiredCount=Ref("OtelReplicas")`, `NetworkConfiguration` awsvpc `Subnets=Split(",", Ref("TaskSubnetsCsv"))`, `SecurityGroups=[Ref(TaskSecurityGroup)]`, `AssignPublicIp=DISABLED`, deployment circuit breaker on, `LoadBalancers=[EcsLoadBalancer(ContainerName="otel-grpc", ContainerPort=4318, TargetGroupArn=Ref(tg))]`. No `ServiceRegistries` (no Cloud Map). Tags via `cardinal_tags`.

## Outputs

- `CollectorAlbDnsName` = `GetAtt(alb, "DNSName")`
- `CollectorEndpoint` = `Sub("http://${AlbDns}:4318", AlbDns=GetAtt(alb,"DNSName"))`
- `CollectorServiceName` = `GetAtt(service, "Name")`
- `CollectorTaskRoleArn` = `GetAtt(taskrole, "Arn")`

## Conditions

- `HasOtelConfigOverride` = `Not(Equals(Ref("OtelConfigYaml"), ""))`.

---

### Task 1: Scaffold, parameters, conditions, build wiring

Mirror Task 1 of the satellite-infra-base plan: module docstring (collector behind own ALB, pull-model still holds — the collector only WRITES to its own bucket, nothing reads it cross-account except via S3/SQS pull), constants, `cardinal_tags` import, all parameters above, `HasOtelConfigOverride` condition, `add_parameter_group_metadata`, `__main__`. Wire `build.sh` + `Makefile`.

- [ ] Write `tests/templates/test_satellite_services.py` with a `td` fixture and `test_required_parameters` (asserts RawBucketName, LicenseSecretArn, VpcId, AlbSubnetsCsv, TaskSubnetsCsv, EcsClusterArn, AlbScheme present) + `test_alb_scheme_allowed_values` (AllowedValues == ["internal","internet-facing"], Default "internal"). Run → fail.
- [ ] Implement the scaffold + params + condition + build wiring. Run → pass. Generate via `PYTHONPATH=src python -m cardinal_cfn.satellite_services | head`. Commit `feat(satellite): scaffold satellite-services generator + params`.

### Task 2: Collector IAM roles (exec + task)

- [ ] Tests: `test_task_role_writes_only_raw_bucket` (task role inline policy has s3:PutObject + s3:ListBucket on the RawBucketName ARN/*, and NOT s3:DeleteObject — the collector writes, lakerunner deletes), `test_exec_role_reads_license_secret` (exec role inline allows secretsmanager:GetSecretValue on Ref LicenseSecretArn). Run → fail.
- [ ] Implement CollectorExecutionRole + CollectorTaskRole. Run → pass. Commit `feat(satellite): collector exec + task roles (write-only to raw bucket)`.

### Task 3: Security groups (ALB SG + task SG)

- [ ] Tests: `test_task_sg_ingress_from_alb_sg` (task SG has ingress on 4318 from the ALB SG), `test_alb_sg_ingress_on_4318` (ALB SG ingress 4318 from IngestSourceCidr). Run → fail.
- [ ] Implement AlbSecurityGroup + TaskSecurityGroup with ingress rules (use `SecurityGroupIngress` inline or separate `SecurityGroupIngress` resources; mirror `security.py` style). Run → pass. Commit `feat(satellite): ALB + task security groups`.

### Task 4: ALB + OTLP listener + target group + listener rule

- [ ] Tests: `test_alb_uses_scheme_param` (ALB Scheme == Ref AlbScheme), `test_otel_listener_is_plain_http_4318` (listener Port 4318 Protocol HTTP), `test_listener_rule_v1_path` (rule PathPattern includes /v1/*), `test_target_group_health_on_13133`. Run → fail.
- [ ] Implement Alb (apply_policy "alb"), OtelHttpListener (copy alb.py), build_target_group, inline ListenerRule priority 300. Run → pass. Commit `feat(satellite): internal ALB + OTLP/HTTP listener + target group`.

### Task 5: Log group, task definition, ECS service

- [ ] Tests: `test_service_uses_fargate_spot`, `test_service_no_cloud_map` (Service has no ServiceRegistries key), `test_service_loadbalancer_wires_target_group` (LoadBalancers ContainerPort 4318 → the TG), `test_taskdef_writes_bucket_env` (container env has LRDB_S3_BUCKET == Ref RawBucketName). Run → fail.
- [ ] Implement LogGroup, TaskDefinition (reuse/inline services_common.build_task_definition), CollectorService. Run → pass. Commit `feat(satellite): collector log group, task definition, ECS service`.

### Task 6: Outputs

- [ ] Tests: `test_outputs_present` (CollectorAlbDnsName, CollectorEndpoint, CollectorServiceName, CollectorTaskRoleArn). Run → fail.
- [ ] Implement the four outputs. Run → pass. Commit `feat(satellite): satellite-services outputs`.

### Task 7: Build, cfn-lint, suite green

- [ ] `PYTHONPATH=src python -m cardinal_cfn.satellite_services > /tmp/ss.yaml && cfn-lint /tmp/ss.yaml` → no errors (warnings tolerable; fix errors).
- [ ] `make build` (generates the template) and `make test` (full suite green). Commit any fixups.

## Self-Review notes

- Pull model: this stack only WRITES to its own raw bucket; it does not read/notify anything cross-account. The bucket→queue→poller path lives in satellite-infra-base. So nothing here violates pull. A test asserts no SQS/Cloud Map coupling.
- The implementer MUST read `services_common.build_task_definition`/`build_target_group`/`build_log_group` signatures before calling them, and `otel.py` + `alb.py` as the canonical references for env vars, FARGATE_SPOT, circuit breaker, and the OTLP listener.

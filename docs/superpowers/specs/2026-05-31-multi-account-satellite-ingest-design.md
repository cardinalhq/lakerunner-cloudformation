# Multi-account satellite ingest — design

## Trial-deployment defaults for deferred decisions

These were left open during design and resolved as defaults to unblock
implementation ahead of the first trial deployments. Each is a parameter or an
easily-moved choice; revisit during trials.

- **Collector ALB scheme:** parameter `AlbScheme`, default `internal` (consistent
  with the private-subnets / no-public-IP posture). Cross-region senders reach an
  internal ALB via the customer's inter-region connectivity (TGW/peering). Flip to
  `internet-facing` at deploy time for public ingest — which then requires
  authentication on the endpoint (not enabled by default).
- **Satellite collector roles/SG:** created inside `satellite-services` (the stack is
  self-contained, one small single-team unit), so the reviewed `satellite-infra-base`
  stays frozen. The strict roles-external-to-services split is enforced only on the
  lakerunner account, where multiple teams are involved.
- **ECS cluster:** customer-supplied as a parameter on both `satellite-services` and
  `lakerunner-services`.
- **Secrets placement:** db-master with `lakerunner-infra-rds`; license/admin with
  `lakerunner-infra-base` (both `Retain`).
- **Cloud Map namespace:** owned by `lakerunner-services` only (default
  `cardinal.internal`); the satellite collector is ALB-reachable, no namespace.

## Problem

Lakerunner runs its main services in one "lakerunner account," but telemetry
originates in many AWS accounts. Each source account needs its own S3 bucket and
SQS queue for raw OTEL data, plus a collector to write into that bucket, and must
grant the lakerunner account access to consume that data. Cooked (processed) data
must land centrally in the lakerunner account, never scattered back across source
accounts.

We want "adopting an account" to mean the same thing everywhere, so the lakerunner
account is not special-cased as a data source: if it produces telemetry, it runs
the same satellite stacks any other account does. The only thing unique to the
lakerunner account is where cooked data lives.

This is greenfield — nothing runs this stack yet, so there is no rollout or
backward-compatibility constraint.

## Data-plane facts this design relies on

These are existing product capabilities (lakerunner binary), not things this design
introduces:

- Storage mapping is **DB/registry-driven**: a row says "read raw from bucket X,
  write cooked into this instance's bucket Y," with an option to **delete the source
  object after processing** (kept ON). Input bucket and output bucket are therefore
  independent.
- `pubsub-sqs` consumes **multiple SQS queues**, addressed by suffix
  (`..._1`, `..._2`, `..._54`, ...), and each queue can carry its **own region and
  its own assume-role**. Cross-region and cross-account sources are therefore native;
  no single central queue is required.

## Architecture

The system normalizes to a set of **self-contained, top-level stacks**, each owned
by a distinct team, each with defined inputs and defined outputs. Nothing the system
needs is implicit: a VPC, subnets, SGs, roles, RDS, buckets, and queues are either a
customer-provided input or the explicit product of one owned stack.

### Stacks

| Stack | Team | Creates | On delete |
|---|---|---|---|
| `lakerunner-infra-base` | IT / security | task SGs, ALB SG, ECS execution role + per-tier task roles, the poller's `sts:AssumeRole` grant, and the **cooked-only write bucket** (no SQS, never a notification source) | roles/SGs **Delete**; cooked bucket **Retain** |
| `lakerunner-infra-rds` | DBA / infra | RDS (or adopt an existing cluster) + the SG and ingress rules it wants (ingress from base's task SGs) + db-master secret | RDS **Snapshot**; an adopted existing cluster is untouched |
| `lakerunner-services` | lakerunner app team | ALB + listeners, ECS services / task defs / target groups / listener rules / log groups, Cloud Map namespace, migration service. **Creates no roles, SGs, RDS, or buckets** — references them by ARN/name | all **Delete** |
| `satellite-infra-base` | source-account infra | otel-raw bucket (ephemeral) + lifecycle, SQS queue, S3→SQS notification (all in-account/in-region), per-account assume-role | everything **Delete** (raw bucket is ephemeral) |
| `satellite-services` | source-account app | otel-collector (ECS) behind an **ALB** (stable, cross-region ingest endpoint) writing into the raw bucket | **Delete** |

### Naming

Top-level stack names are `cardinal-` prefixed and share a common suffix grammar
across both account types (`-infra-base` = roles/SGs/buckets, `-infra-rds` =
database, `-services` = app tier; a satellite has no `-rds`):

- lakerunner account: `cardinal-lakerunner-infra-base`,
  `cardinal-lakerunner-infra-rds`, `cardinal-lakerunner-services`
- satellite account: `cardinal-satellite-infra-base`, `cardinal-satellite-services`

CloudFormation stack names allow 128 characters; these (~30) are well within limits.

Nested children cannot have their physical names set — CloudFormation generates
`<parent>-<LogicalId>-<random>` for any `AWS::CloudFormation::Stack`. We control the
middle segment via clean logical IDs (`Alb`, `Migration`, `ServicesQuery`, ...), so a
child reads as `cardinal-lakerunner-services-Alb-AB12CD`; the random suffix is
unavoidable. Resources *within* stacks keep the existing convention — CFN-generated
physical names plus a `Name` tag, `cardinal-` prefix (`chq-` only where an AWS
length cap forces it).

### Team isolation (the load-bearing rule)

Stacks are split along *who is allowed to change what*, not along technical layers:

- `lakerunner-services` creates **no IAM roles, no security groups, no RDS, no
  buckets**. It consumes them as ARN/name parameters. The app team can freely update
  an ECS service, task definition, or listener rule; they cannot mint or alter an IAM
  role.
- Roles and SGs live in `lakerunner-infra-base`, owned by IT/security. Changing or
  adding a role is an IT action run against `base`, in coordination/isolation —
  additive role changes never require touching the running services stack.
- RDS is its own stack because the customer may bring an existing cluster, and DB
  ownership is a separate concern; it configures the SGs/ingress it wants rather than
  having another stack reach into its SG.

### Wiring (driver, not cross-stack references)

Each top-level stack is independently deployable with explicit inputs/outputs. The
Jenkins driver reads one stack's outputs and passes them as parameters to the next —
there are **no `Fn::ImportValue` or cross-stack `GetAtt` references between top-level
stacks**. Deploy order in the lakerunner account is `base → rds → services`
(`rds` and `services` consume `base` outputs; `services` also consumes `rds`
outputs). Satellite stacks deploy independently in their own accounts.

Inside `lakerunner-services`, the existing app-tier children (alb, migration,
services-query/process/control, otel, maestro) remain nested under the services root
and wire to each other via `GetAtt` — they are one deployable unit owned by one team.
Only the `base / rds / services / satellite-*` boundaries are driver-wired.

### VPC

A VPC and subnets are a **customer-provided input**, threaded as parameters into the
stacks that need them (base for SG placement, rds for the subnet group, services for
ECS networking). No `cardinal-*` stack creates a VPC. `lrdev-vpc` remains a standalone
helper — a test-env simulation of a customer VPC, or a true from-scratch build for
customers who want one — and is never part of the `base → rds → services` chain.

## Key decisions

### Pull model (load-bearing invariant)

The only cross-account data path is **lakerunner pulling from a satellite by
assuming that satellite's role** and consuming its own SQS / reading-and-deleting
its own S3. Nothing pushes the other way:

- A satellite bucket **must not** publish notifications to a central queue in the
  lakerunner account, or to any other account. Its S3→SQS notification targets only
  its own in-account, in-region queue.
- A satellite **must not** make any outbound call to the lakerunner account or to
  another satellite. Its only reference to the lakerunner account is the passive
  trust-policy grant on the role it creates.
- Lakerunner contacts a satellite only via the assumed per-account role, scoped to
  one account at a time.

This keeps every account isolated and makes the trust direction one-way
(lakerunner → satellite, pull). The rejected "central queue receiving cross-account
S3 notifications" is a push model and is out of bounds for this reason, independent
of the multi-queue capability that already makes it unnecessary.

### Uniform account adoption

The lakerunner account is not a special data source. If it emits telemetry it runs
the same satellite infra + collector stacks as any other account. The only
lakerunner-account-specific resource is the cooked-only output bucket, which is
simply an instance bucket that is never wired as a notification source — so the
"input work unit" (raw bucket + SQS + assume-role) is identical for every adopted
account, and there is exactly one extra cooked-only bucket in the whole system.

### Cross-account auth = per-queue AssumeRole

The satellite infra stack **creates the role**; the role's permissions cover only
its own bucket and queue, and its trust policy names the lakerunner principal
(passed into the satellite stack as a parameter). The lakerunner poller is granted
`sts:AssumeRole` on those satellite roles and assumes the correct one per queue.

Consequences:

- The lakerunner task/poller role only ever needs `sts:AssumeRole` — it does **not**
  enumerate satellite bucket/queue ARNs and does **not** change when an account is
  adopted.
- The satellite is fully self-contained: it carries its own resource permissions.
- The single unavoidable coupling is that the satellite infra stack must know the
  lakerunner principal to trust — one stack parameter.

**Rejected alternative — resource-policy-direct grants.** Granting the lakerunner
role directly via each satellite's bucket policy + queue policy would force the
lakerunner identity policy to either enumerate every satellite ARN (a lakerunner
stack edit on every adoption) or use a name-convention account-wildcard policy.
AssumeRole avoids both: the lakerunner side stays static and the satellite side stays
explicit.

### Service discovery and ingest endpoints

Two different needs, two different mechanisms:

**Internal service-to-service → Cloud Map DNS (no load balancer).** Only
`lakerunner-services` owns a Cloud Map **private DNS namespace** (name is a parameter,
default `cardinal.internal`). The app tiers discover each other through it
(query-worker ↔ query-api, etc.), and maestro reaches the now-DNS-only APIs at
`query-api.cardinal.internal:8080` / `admin-api.cardinal.internal:9091`. We use plain
DNS rather than a load balancer for these internal hops because a bare record avoids
LB target-registration and connection-draining delays on every task replacement — so
restarts, image bumps, and Fargate Spot recycles recover faster. ~$0.50/month for the
private hosted zone, deleted with the stack.

**Collector ingest → ALB (cross-region front door).** `satellite-services` puts the
collector **behind an ALB** rather than a DNS name, because sources in multiple
regions send to the same collector and the collector is the natural place to let
traffic cross regions. The ALB's own hostname is the stable endpoint, so the satellite
needs **no Cloud Map namespace at all**. An ALB also gives health-aware balancing and
graceful draining, which suits a front door receiving from many senders. The
lakerunner-account collector (its own telemetry, satellite-style) follows the same
ALB-fronted pattern.

Collector-ALB sub-decisions to pin in the plan: scheme (internet-facing with
auth/TLS for cross-region/cross-account senders, vs. internal reachable via
TGW/peering); ALB gRPC (HTTP/2) target group for OTLP `:4317` plus OTLP/HTTP `:4318`
(NLB is the L4/static-IP alternative); TLS via the existing `cert.yaml` machinery;
and authentication on the ingest endpoint, which is required once it is reachable
across regions/accounts.

### ALB surface = maestro only; the APIs are DNS-only

The embedded lakerunner UI is being retired — **maestro is the one and only UI** — so
the only browser-facing surfaces in `lakerunner-services` are maestro's UI and its
bundled DEX OIDC (ALB listener rules, priorities 200/210). Everything else is in-VPC
Cloud Map DNS:

- `query-api` and `admin-api` are **DNS-only**. They already register Cloud Map
  ServiceDiscovery records (maestro reaches them at `query-api.cardinal.internal:8080`
  / `admin-api.cardinal.internal:9091`); they get **no ALB target group or listener
  rule**. Consumers are server-to-server inside the VPC (maestro, and an in-VPC — or
  zone-associated — Grafana datasource backend). A browser never talks to either
  directly.
- The dedicated **admin-api `9443` listener is removed**; it existed only to serve the
  deprecated embedded admin UI. Admin is driven through maestro.
- Listener priorities **100 (query-api) and 110 (admin-api) are freed**. Dropping
  query-api from the ALB also removes its two-slot workaround (its routes exceed the
  5-path-pattern ALB limit) — moot once it is DNS-only.

Net effect: the ALB carries only maestro's browser traffic; query and admin paths are
in-VPC DNS, consistent with the spot-friendly, no-drain rationale above.

### Cooked centralized, raw deletable

Satellite buckets are otel-raw-only. Cooked data is written to the lakerunner
instance bucket. "Delete source after processing" stays ON so raw objects do not
accumulate in satellite accounts.

## Out of scope (TODO — not built in this design)

**Registration / adoption mechanism.** How a row —
`{queue URL, region, assume-role ARN, organization/collector identity}` — actually
lands in the lakerunner DB is deferred. The satellite stacks emit these identifiers
as outputs (for a human to copy); wiring them into the running install is a separate,
later effort. Candidate implementations, neither chosen here:

1. A UI that upserts the DB row.
2. A task (the SQS consumer or the migration task) that reconciles DB rows on each
   run from data carried on the lakerunner stack — adoption then becomes a lakerunner
   stack parameter change rather than a UI action.

This is intentionally kept out of the CFN surface for now so it adds no stack
complexity.

## What changes in the generators

- **Promote `Security` out of the services root.** Today `root.py` nests the
  `Security` child, which mints the ECS execution role, the per-tier task roles, the
  ALB SG, and the task SGs. These move into a standalone `lakerunner-infra-base` stack
  (IT-owned). `lakerunner-services` gains parameters for every role ARN and SG id it
  currently resolves via `GetAtt security_stack.Outputs.*`. The base stack also adds
  the poller's `sts:AssumeRole` grant and owns the cooked-only write bucket.
- **Split infra into `-base` and `-rds`.** RDS, its SG, the RDS ingress rules (now
  fed base's task-SG ids as input), and the db-master secret live in
  `lakerunner-infra-rds`. The current `cardinal_infrastructure.py` no longer owns the
  ingest bucket / ingest SQS / S3→SQS notification at all — those move to
  `satellite-infra`.
- **`lakerunner-services`** (the current `root.py`) keeps its internal app-tier
  nesting but creates no roles/SGs/RDS/buckets; it is fully parameter-driven and
  driver-wired to `base` and `rds` outputs.
- **New**: `satellite-infra-base` generator (raw bucket + SQS + S3→SQS notification +
  per-account assume-role).
- **New**: `satellite-services` generator (otel-collector ECS, in-account write
  path).
- **VPC/subnets** become inputs to base/rds/services; no `cardinal-*` stack creates a
  VPC.

## Open items to pin in the plan

- Internal app-tier nesting stays inside `lakerunner-services` (assumed; confirm).
- Secret placement: db-master with `-rds`; license/admin with `-base` (both
  externally referenced, so `Retain`).
- Collector-ALB scheme + auth: internet-facing (auth/TLS) vs internal
  (TGW/peering), and the authentication mechanism on the ingest endpoint — see
  Service discovery and ingest endpoints.

(Resolved: only `lakerunner-services` owns a Cloud Map namespace, for internal
service-to-service discovery; the collector — satellite and lakerunner-account —
is ALB-fronted for cross-region ingest, no namespace. The `otel-grpc` shared-ALB
listener rule is superseded by the collector's own ALB.)

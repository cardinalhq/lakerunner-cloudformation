# Bundled DEX OIDC Provider in the Maestro Stack

Status: approved (2026-04-22)

## Goal

Let operators turn on a bundled DEX OIDC provider inside the Maestro
CloudFormation stack, mirroring what the `charts/maestro` Helm chart offers.
When enabled, Maestro authenticates its UI against the in-stack DEX without
requiring an external IdP. When disabled, the stack renders exactly as it
does today.

## Non-goals

- High availability for DEX. Upstream's bundled pattern uses in-memory
  storage and is documented as single-replica POC-grade; we inherit that.
- Runtime user management. Static users are baked into DEX's config at
  task start; adding or removing users requires a stack update.
- Bcrypt hashing at deploy time. Operators supply the pre-bcrypted hash
  string themselves; the stack does not hash passwords.

## Architecture

When `DexEnabled=Yes`, the existing single-replica Maestro ECS task grows
two additional containers:

1. `DexInit` (non-essential init, `public.ecr.aws/docker/library/busybox`)
   reads env vars (`DEX_ISSUER_URL`, `DEX_REDIRECT_URI`, `DEX_CLIENT_ID`,
   `DEX_ADMIN_EMAIL`, `DEX_ADMIN_HASH`) and writes a rendered
   `/etc/dex/config.yaml` into a shared named volume (`dex-config`).
   Exits 0.
2. `Dex` (pinned `ghcr.io/dexidp/dex`) listens on `:5556`, mounts the
   shared config volume read-only, mounts the existing `tmp` volume for
   writable scratch (dex templates web assets into /tmp under its
   readOnlyRootFilesystem guard), and `DependsOn: [DexInit -> SUCCESS]`.

A second `TargetGroup` (port 5556, health check `/dex/healthz`) and an
ALB `ListenerRule` at priority 10 with a path pattern of `/dex*` route
browser traffic to the DEX container. The listener's default action
continues to forward to the Maestro target group.

On the Maestro container, the `OIDC_ISSUER_URL`, `OIDC_JWKS_URL`, and
`OIDC_AUDIENCE` env vars switch via `Fn::If(DexEnabled, ...)`:

- `OIDC_ISSUER_URL` -> `<MaestroBaseUrl><DexPathPrefix>`, browser-visible.
- `OIDC_JWKS_URL`   -> `http://localhost:5556<DexPathPrefix>/keys`,
  bypasses the ALB (same task, loopback).
- `OIDC_AUDIENCE`   -> `<DexClientId>` (default `maestro-ui`).

`OIDC_SUPERADMIN_GROUP`, `OIDC_SUPERADMIN_EMAILS`, and
`OIDC_TRUST_UNVERIFIED_EMAILS` keep their existing parameter-driven
behaviour.

`MaestroBaseUrl` auto-derives to `http://<AlbDnsName>` (via
`Fn::GetAtt`) when DEX is enabled and the operator left the parameter
blank. When DEX is disabled the parameter's behaviour is unchanged.

## Parameters

| Name                    | Type   | Default                                       | Notes |
|-------------------------|--------|-----------------------------------------------|-------|
| `DexEnabled`            | String | `No` (`Yes`/`No`)                             | Gates every DEX-specific resource. |
| `DexAdminEmail`         | String | empty                                         | Required when `DexEnabled=Yes`. Single static user. |
| `DexAdminPasswordHash`  | String (`NoEcho`) | empty                              | Bcrypt hash of the admin password, supplied by operator. Required when `DexEnabled=Yes`. |
| `DexClientId`           | String | `maestro-ui`                                  | Public PKCE client id registered with DEX and used as `OIDC_AUDIENCE`. |
| `DexPathPrefix`         | String | `/dex`                                        | Path prefix DEX serves under. Must start with `/`. |
| `DexImage`              | String | `ghcr.io/dexidp/dex:v2.41.1`                  | Air-gapped override. |
| `DexInitImage`          | String | `public.ecr.aws/docker/library/busybox:1.37`  | Air-gapped override. |

Validation: CloudFormation cannot easily express "required when X", so a
blank `DexAdminEmail` or `DexAdminPasswordHash` under `DexEnabled=Yes`
is caught by DEX itself failing to start. Parameter descriptions call
this out.

## Conditions

- `DexEnabled` (existing `IsInternetFacing` pattern).
- `UseAlbBaseUrl`: `DexEnabled=Yes AND MaestroBaseUrl is blank`.

## Resource additions (all conditional on `DexEnabled`)

- `MaestroDexTargetGroup` - port 5556, `/dex/healthz`.
- `MaestroDexListenerRule` - priority 10, path `/dex*`, forwards to
  the DEX target group.
- `SecurityGroupIngress` for the task SG from the ALB SG on port 5556.

## Task-definition changes

Unconditional:

- New named volume `dex-config`.

Conditional on `DexEnabled`:

- Prepend `DexInit` to `ContainerDefinitions`, before `DbInit`.
- Append the `Dex` container, mounting `dex-config` at `/etc/dex`
  read-only and `tmp` at `/tmp` read-write.
- `Maestro.LoadBalancers` stays a single entry (Maestro's target group);
  the DEX target group is wired only via listener rule, not the ECS
  service's LoadBalancers list, which ECS does not allow to be
  conditional per-target-group.

Actually, correction: ECS services can attach to multiple target groups,
and we will add a second `LoadBalancer` entry for the `Dex` container
(port 5556 -> DEX target group) so ECS registers the task IP in the DEX
target group. This entry is conditional via `Fn::If` with `AWS::NoValue`
fallback.

## Outputs

No new exports. Existing `MaestroUrl` already points at the ALB DNS,
which is also the DEX login entry point when enabled.

## Testing

- `tests/test_maestro_service_simple.py` gains checks for:
  - New parameters present with expected defaults and `AllowedValues`.
  - `DexEnabled` and `UseAlbBaseUrl` conditions present.
  - When mocked to DEX-off (default): no DEX target group, no DEX
    listener rule, no DEX container in the task def.
  - When mocked to DEX-on: DEX target group, listener rule, init +
    dex containers, shared `dex-config` volume, correct conditional
    env vars on the Maestro container.
- `make test` green; regenerated templates pass `cfn-lint` with no new
  errors.

## Open risks / follow-ups (not in scope)

- Issuer-URL churn when an operator later moves from ALB DNS to a
  custom hostname invalidates existing sessions. Parameter description
  flags this.
- In-memory storage = login state lost on task restart. Consider a
  later follow-up to swap to a backed storage driver (Postgres) if
  operators push on it.

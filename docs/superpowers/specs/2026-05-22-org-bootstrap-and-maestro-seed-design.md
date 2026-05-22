# Org bootstrap + Maestro seed design

Status: approved 2026-05-22

## Problem

Two gaps surfaced after a fresh install:

1. The canonical org has storage but no ingest API key. `cardinal_infrastructure.py`
   seeds the `storage-profiles` SSM parameter for org
   `12340000-0000-4000-8000-000000000000` but seeds `api-keys` as the empty list
   `[]`. The org can store data but has no key to ingest with.
2. Maestro starts with no org, no owner, and no lakerunner datasource. Org, owner,
   and the "managed lakerunner" datasource are all created by hand through the
   DEX-authenticated superadmin UI. The steady state we actually want -- one org,
   owned by the admin, already wired to the local lakerunner -- has to be rebuilt
   manually every install.

The admin API key itself is already handled: lakerunner reads `ADMIN_INITIAL_API_KEY`
(`internal/adminconfig`), and `services_control.py` already injects it from the
`cardinal-admin-key` secret. No change needed there.

## Canonical org

One single-install org, referenced identically by Lakerunner and Maestro:

- id: `12340000-0000-4000-8000-000000000000` (the existing placeholder; kept)
- name: "My Organization"
- owner: the `DexAdminEmail` parameter (the bundled superadmin)

An `OrganizationId` parameter (default = that UUID) replaces the hardcoded copies
so storage-profiles, api-keys, and the Maestro bootstrap all reference one value.

## Workstream A -- org ingest key (this repo, `cardinal_infrastructure.py`)

- Add `OrganizationId` parameter (default `12340000-0000-4000-8000-000000000000`).
- Add `InitialIngestApiKey` `NoEcho` parameter (default empty). The operator supplies
  it through their `do-not-commit-infra.sh` driver, since those carry the install's
  real values; `data-setup.sh` is only a helper and is not on the operator's path.
- `storage-profiles` SSM value: reference `${OrganizationId}` instead of the literal.
- `api-keys` SSM value: when `InitialIngestApiKey` is set, seed a one-entry YAML list
  (`organization_id` = `OrganizationId`, `keys` = `[InitialIngestApiKey]`); when empty,
  keep `[]` (today's behavior, backward compatible).

`OrganizationId` is not emitted as a stack output: the infra stack's outputs are
contractually 1:1 with `data-setup.sh`, and the org id is a config constant, not a
provisioned resource identifier. The root stack's `OrganizationId` parameter defaults
to the same UUID, so no wiring is required; an operator overriding it must set the same
value on both stacks. `data-setup.sh` (a helper that is off the operator's path) is left
seeding `api-keys` as `[]`; the operator-facing seed lives in the infra stack.

The `api-keys` SSM parameter is plaintext YAML by design (an `AWS::SSM::Parameter`
cannot be a `SecureString` in CloudFormation), so the key lands in plaintext SSM
regardless; `NoEcho` only keeps it out of the CloudFormation console echo. This is
the existing accepted posture for this parameter.

This bootstrap ingest key is independent of any keys Maestro later provisions through
the admin API. Maestro starts with zero ingest keys, so there is no conflict; the two
key sets coexist.

## Workstream B -- Maestro bootstrap (upstream, `cardinalhq/conductor`)

File a feature issue for an idempotent, seed-if-missing startup bootstrap in
`packages/maestro`, gated on the env contract below. When the env vars are present
and the rows are absent, it creates:

1. a user = `MAESTRO_BOOTSTRAP_OWNER_EMAIL`,
2. an org = `MAESTRO_BOOTSTRAP_ORG_ID` / `MAESTRO_BOOTSTRAP_ORG_NAME`, with that user
   as an `owner` membership (`maestro_org_memberships`),
3. a `shared_cardinal` row in `maestro_lakerunner_deployments` with `admin_api_url`,
   `admin_api_key`, `query_api_url`, and `auto_add_to_all_orgs = true`.

It reuses existing machinery: `isAutoAddEligible` + the shared-install reconciler
(`lakerunner-shared-install-reconciler.ts`) already enqueue a `provision_org` job per
org, and `executeProvisionOrg` (`lakerunner-provisioning.ts`) calls lakerunner's admin
API live (`createLakerunnerAdminClient` -> `provisionOrganization`). Because that is an
async, retrying job queue, no click and no CloudFormation ordering dependency are
needed: if admin-api is not reachable yet, the job retries until it is.

Maestro reaches the admin API over the ALB's self-signed cert; the maestro container
already sets `NODE_TLS_REJECT_UNAUTHORIZED=0`, so the call is not rejected.

## Workstream C -- Maestro env wiring (this repo, `maestro.py` + `root.py`)

Thread the bootstrap contract into the `maestro` container. Implemented now even
though conductor does not read the vars yet -- they are inert until the feature ships,
and the same person owns both ends of the contract, so the names will not drift.

| Env var | Source |
|---|---|
| `MAESTRO_BOOTSTRAP_ORG_ID` | `Ref(OrganizationId)` |
| `MAESTRO_BOOTSTRAP_ORG_NAME` | `Ref(OrgName)` (param, default "My Organization") |
| `MAESTRO_BOOTSTRAP_OWNER_EMAIL` | `Ref(DexAdminEmail)` |
| `MAESTRO_BOOTSTRAP_LAKERUNNER_QUERY_API_URL` | `Sub("https://${AlbDnsName}")` |
| `MAESTRO_BOOTSTRAP_LAKERUNNER_ADMIN_API_URL` | `Sub("https://${AlbDnsName}:9443")` |
| `MAESTRO_BOOTSTRAP_LAKERUNNER_ADMIN_API_KEY` | secret -> `${AdminApiKeySecretArn}:key::` |

New maestro.yaml parameters: `AdminApiKeySecretArn` (matches the services-control name;
root passes `Ref("AdminKeySecretArn")`), `OrganizationId`, `OrgName`. Root gains
`OrganizationId` (default UUID) and `OrgName` (default "My Organization") parameters and
forwards all three to the maestro child.

The query endpoint base is `https://<alb>` (query-api serves `/api/v1/...` on the main
443 listener); the admin endpoint is `https://<alb>:9443` (admin-api's dedicated
catch-all listener).

## Testing

- `make build` (cfn-lint clean).
- Per-template assertions: infra api-keys seeded with org+key when the parameter is set
  and `[]` when empty; storage-profiles references `OrganizationId`; maestro container
  carries the six bootstrap env entries and the admin-key secret; root forwards the new
  parameters to the maestro child.

## Out of scope

- The conductor feature implementation (tracked by the workstream-B issue).
- Any change to `ADMIN_INITIAL_API_KEY` on the lakerunner side (already works).

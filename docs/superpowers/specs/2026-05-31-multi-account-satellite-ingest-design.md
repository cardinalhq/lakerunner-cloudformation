# Multi-account satellite ingest — design

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

Three deployment units with distinct team ownership:

| Unit | Owner | Creates |
|---|---|---|
| **Lakerunner setup** | platform team, once | RDS, roles, security groups, secrets, DB/SSM seed, the **cooked-only instance bucket** (no SQS, never a notification source), and the application service stacks. No collector. No ingest bucket or ingest SQS. |
| **Satellite infra stack** (new) | source-account infra team, per account | otel-raw S3 bucket + lifecycle, SQS queue, S3→SQS notification (all in-account / in-region), and a **per-account IAM role** the lakerunner poller assumes — scoped to exactly that bucket (S3 read/delete) and that queue (SQS consume), trusting the lakerunner principal. |
| **Satellite collector stack** (new) | source-account app team, per account | the otel-collector (ECS) writing into the raw bucket via an in-account path. |

The infra and collector stacks are deliberately separate so an infra-like team and
an app-like team can own and update them on independent cadences.

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

Additive, plus one relocation:

- **New**: satellite infra stack generator (bucket + SQS + S3→SQS notification +
  assume-role).
- **New**: satellite collector stack generator (otel-collector ECS, in-account
  write path).
- **Lakerunner setup**: owns a cooked-only instance bucket and does **not** own the
  ingest bucket / ingest SQS / S3→SQS notification (those now live in the satellite
  infra stack). The lakerunner poller role gains `sts:AssumeRole`; it loses any direct
  ingest-bucket/queue grants.

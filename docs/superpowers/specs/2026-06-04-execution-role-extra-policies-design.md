# Execution-role extra IAM policies (customer-supplied)

Date: 2026-06-04
Status: implemented (v0.0.128)

## Problem

Air-gapped / private-registry installs sometimes need permissions on the ECS
task **execution role** that the base `AmazonECSTaskExecutionRolePolicy` does not
grant: ECR pull-through-cache first pull (`ecr:BatchImportUpstreamImage` + repo
auto-create), cross-account ECR, or KMS-encrypted repos. Customers need a way to
add these, driven from the deploy driver, and to grow from "paste a JSON policy"
into "manage proper managed policies."

(The four ECR read actions a private pull needs — `GetAuthorizationToken`,
`BatchCheckLayerAvailability`, `GetDownloadUrlForLayer`, `BatchGetImage` — are
already in the base policy, so they need no extra grant.)

## Constraint

CloudFormation cannot attach a *string* parameter as an IAM `PolicyDocument`
(it must be a structured JSON object), and the usual string→policy workaround
needs a macro/custom resource, i.e. Lambda — which this product bans. So a
pasted JSON policy must become a real IAM policy object before CFN can attach it.

## Design

### Template (both execution-role creators)

`lakerunner_infra_base.py` (`ExecutionRole`) and `satellite_services.py`
(`CollectorExecutionRole`) gain:

- Parameter `ExecutionRoleExtraPolicyArns` (String, default `""`, CSV of
  managed-policy ARNs).
- Condition `HasExecutionRoleExtraPolicies` = the param is non-empty.
- `ManagedPolicyArns = Fn::If(HasExecutionRoleExtraPolicies,
  Split(",", Sub("<base>,${ExecutionRoleExtraPolicyArns}")), ["<base>"])`,
  where `<base>` is `AmazonECSTaskExecutionRolePolicy`.

Attachment is fully CFN-tracked; empty default = today's behavior.

### Drivers (`deploy-lakerunner-infra-base.sh`, `deploy-satellite-services.sh`)

A shared front-half function `resolve_exec_role_policy_arns` sets a CSV from two
optional inputs (both may be combined):

- `EXECUTION_ROLE_POLICY_ARNS` — ready-made managed-policy ARNs, passed through.
- `EXECUTION_ROLE_POLICY_JSON` / `_FILE` — a pasted IAM policy. The driver
  validates+flattens it (`jq -c`), then creates (or version-updates, pruning
  non-default versions under the IAM 5-version cap) a customer-managed policy
  named `<STACK_NAME>-exec-extra`, and appends its ARN. Needs `jq` and IAM write
  permissions for the deployer.

The function is invoked directly (not in `$()`) so validation errors `exit` the
whole script. The resulting CSV is passed as the `ExecutionRoleExtraPolicyArns`
PARAMS override (omitted when empty, preserving the template default).

## Scope / decisions

- **Execution role only** (the image-pull role). Task-role extensibility is not
  included.
- **Managed policy created by the driver from pasted JSON**, attached by ARN
  (chosen over a post-deploy inline `put-role-policy`, which can't run on first
  create and would be CFN drift).
- The created policy object's lifecycle is the driver's (not CFN): re-deploys
  add a new default version; a stack delete detaches it (CFN owns the role) and
  leaves the policy as a harmless orphan.

## Testing

- Template: the param + condition exist; `ManagedPolicyArns` is the conditional
  `Fn::If` referencing both the base policy and the param (both stacks).
- Driver: shellcheck clean; drift gate; behavioral check that
  `EXECUTION_ROLE_POLICY_ARNS` passes through into the `ExecutionRoleExtraPolicyArns`
  param (the JSON path needs live IAM and is covered by inspection).

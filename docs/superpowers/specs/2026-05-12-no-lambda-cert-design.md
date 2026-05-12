# No-Lambda TLS certificate — design

## Problem

`cert.yaml`'s PEM path is Lambda-backed: when `CertificateArn` is empty, a
custom resource (`AWS::Lambda::Function` running `acm:ImportCertificate`)
imports the customer's `CertificateBody` / `CertificatePrivateKey` /
`CertificateChain` into ACM. After the migration change, this was the last
Lambda in the product. Some target environments cannot run Lambda *and* cannot
create resources outside a CloudFormation stack (so `aws acm import-certificate`
out of band isn't an option either) — they need the cert installed *by the
stack*, without a Lambda.

## Approach

CloudFormation has no native resource for `acm:ImportCertificate`. But
`AWS::IAM::ServerCertificate` is a native resource that takes
`CertificateBody` / `PrivateKey` / `CertificateChain` as plain properties (no
Lambda), and an ALB HTTPS listener accepts an IAM server-certificate ARN
exactly like an ACM one. So `cert.yaml` becomes:

- `CertificateArn` non-empty → forward it as-is (works for ACM *or* IAM server
  cert ARNs).
- `CertificateArn` empty + `CertificateBody` non-empty → create an
  `AWS::IAM::ServerCertificate(CertificateBody, PrivateKey, CertificateChain?)`
  and output its `Arn`.
- output `EffectiveCertificateArn = Fn::If(CreateServerCert,
  GetAtt(ServerCertificate.Arn), Ref(CertificateArn))` — same shape as before;
  the ALB child consumes it unchanged.

No Lambda, no custom resource, no out-of-band step. The cert *material* still
has to come from somewhere (CloudFormation can't generate a self-signed cert);
the customer supplies it as the `NoEcho` PEM parameters (the same parameters
the old Lambda path used). On stack delete, the `alb` child (which holds the
listener referencing the cert ARN) is deleted before the `cert` child, so the
server-certificate delete doesn't hit `DeleteConflict`; CloudFormation retries
resource deletions if AWS is briefly slow to release it.

## Changes

- `src/cardinal_cfn/children/cert.py`: rewritten — drop the `CertLambdaRoleArn`
  parameter, the `CertLambdaLogGroup` / `CertLambda` / `ImportedCertificate`
  resources, and the `cert_lambda` import; add condition `CreateServerCert`
  (`CertificateArn` empty AND `CertificateBody` non-empty) and `HasCertChain`;
  add `AWS::IAM::ServerCertificate` (`CertificateBody`/`PrivateKey` from params,
  `CertificateChain` via `Fn::If(HasCertChain, ..., AWS::NoValue)`,
  CFN-generated name, `Name` tag); output `EffectiveCertificateArn` now
  `Fn::If(CreateServerCert, GetAtt(ServerCertificate.Arn), Ref(CertificateArn))`.
- `src/cardinal_cfn/root.py`: remove the `CertLambdaRoleArn` parameter and its
  pass-through to `CertStack`. The "IAM roles + security groups" parameter
  group (derived from `_ROLE_SG_PARAMS`) updates automatically.
- Delete `src/cardinal_cfn/children/cert_lambda.py` and
  `tests/unit/test_cert_lambda.py`.
- Tests: rewrite `tests/templates/test_cert.py`; drop `CertLambdaRoleArn` from
  `tests/templates/test_root.py`; rename `test_no_lambda_except_cert.py` →
  `test_no_lambda.py` and assert *every* template (vpc, infra, root, all
  children) has no `AWS::Lambda::Function` and no custom resource.
- Docs: `CLAUDE.md` (cert table row + the "no Lambdas anywhere" note),
  `docs/operations/permissions-lakerunner.md`,
  `docs/operations/permissions-infrastructure.md` (drop the `lambda` API row;
  add `iam:*ServerCertificate*` actions),
  `docs/operations/install-lakerunner.md`.

## Out of scope / follow-ups

- `docs/operations/tearing-down.md` and `docs/operations/end-to-end-test-plan.md`
  still mention the cert-import Lambda; refresh them for the new mechanism.
- Generating a self-signed cert inside the stack is impossible without a Lambda;
  whoever stands up the install creates it (`openssl` + supply the PEM params)
  or supplies a real cert. The `do-not-commit-lakerunner.sh` scratch script's
  `CERTIFICATE_ARN` comment block documents the `openssl` / `aws acm
  import-certificate` / `aws iam upload-server-certificate` commands and links.

## Test plan

`make build` (cfn-lint clean) and `make test` green. (Not deploy-tested live —
the ALB-listener-with-IAM-server-cert path is well-trodden AWS behavior and the
test environment was torn down; the next full deploy will exercise it.)

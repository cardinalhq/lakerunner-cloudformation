# Maestro + MCP Gateway Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a new CloudFormation stack (`lakerunner-07-maestro-service.yaml`) that deploys Maestro v0.23.0 and the MCP Gateway as one ECS Fargate service with its own ALB, and strip the AI sidecars out of the Grafana stack.

**Architecture:** One ECS service, one task, three containers (DbInit → McpGateway → Maestro). Stack-local ALB with `AlbScheme` toggle. Reuses the CommonInfra RDS — a psql init container creates the `maestro` database and user before the app containers start. OIDC config is exposed as plain CloudFormation parameters; Maestro has no OIDC client secret.

**Tech Stack:** Python 3.13 + troposphere, cfn-lint, pytest + unittest.mock, cloud-radar, AWS ECS Fargate, AWS ALB, AWS Secrets Manager, AWS RDS (PostgreSQL, shared), CloudWatch Logs.

**Spec:** `docs/superpowers/specs/2026-04-21-maestro-mcp-stack-design.md`

---

## File structure

Each file has one responsibility.

- **Create** `lakerunner-maestro-defaults.yaml` — image + port defaults for the new stack (the only file the template generator reads).
- **Create** `src/lakerunner_maestro_service.py` — troposphere generator. Prints YAML on stdout. Structured to match `src/lakerunner_grafana_service.py`: module-level `load_maestro_config()` helper and a `create_maestro_template()` function that takes no args.
- **Create** `tests/test_maestro_service_simple.py` — smoke tests using `unittest.mock.patch` on `load_maestro_config`, mirroring `tests/test_grafana_service_simple.py`.
- **Modify** `build.sh` — add generation + cfn-lint step 07.
- **Modify** `src/lakerunner_grafana_service.py` — remove MCP / Conductor / Maestro sidecars, related secrets, log groups, IAM, and parameters. Keep the datasource plugin path fully working.
- **Modify** `lakerunner-grafana-defaults.yaml` — drop the AI sections and images.
- **Modify** `tests/test_grafana_service_simple.py` — drop assertions for the removed AI resources and parameters; add assertions that those resources are *gone*.

The plan is organized in two phases that are independent and can be verified separately:

1. **Phase A (Tasks 1–10):** Build the Maestro stack, from scaffolding to ECS service, outputs, build integration.
1. **Phase B (Tasks 11–13):** Clean up the Grafana stack.
1. **Phase C (Task 14):** Final green check.

Commit at the end of each task. All commands assume CWD is the repo root `/Users/explorer/git/github/cardinalhq/lakerunner-cloudformation`. Activate the venv once at the start: `source .venv/bin/activate`.

---

## Phase A — Maestro stack

### Task 1: Scaffold defaults YAML + generator skeleton + smoke test

**Files:**

- Create: `lakerunner-maestro-defaults.yaml`
- Create: `src/lakerunner_maestro_service.py`
- Create: `tests/test_maestro_service_simple.py`

- [ ] **Step 1: Write the failing smoke test**

Create `tests/test_maestro_service_simple.py`:

```python
#!/usr/bin/env python3
# Copyright (C) 2026 CardinalHQ, Inc
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3.

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

MOCK_CONFIG = {
    "images": {
        "maestro": "public.ecr.aws/cardinalhq.io/maestro:v0.23.0",
        "db_init": "ghcr.io/cardinalhq/initcontainer-grafana:test",
    },
    "task": {"cpu": 1024, "memory_mib": 2048},
    "ports": {
        "maestro": 4200,
        "mcp_gateway": 8080,
        "mcp_gateway_debug": 9090,
        "alb_listener": 80,
    },
}


class TestMaestroTemplateSimple(unittest.TestCase):
    """Smoke tests for the Maestro + MCP Gateway template generator."""

    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_load_and_create_functions_importable(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_maestro_service import (
            create_maestro_template,
            load_maestro_config,
        )

        config = load_maestro_config()
        assert isinstance(config, dict)
        assert "images" in config

        template = create_maestro_template()
        assert template is not None

        template_json = template.to_json()
        assert isinstance(template_json, str)
        template_dict = json.loads(template_json)
        assert "Parameters" in template_dict
        assert "Resources" in template_dict
        assert "Outputs" in template_dict
        assert "Conditions" in template_dict
        assert "Metadata" in template_dict
        assert "Maestro" in template_dict["Description"]


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Command: `pytest tests/test_maestro_service_simple.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lakerunner_maestro_service'`.

- [ ] **Step 3: Create the defaults YAML**

Create `lakerunner-maestro-defaults.yaml`:

```yaml
# Defaults for the Maestro + MCP Gateway stack
#
# Image pins and port settings used by lakerunner_maestro_service.py.

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

- [ ] **Step 4: Create the minimal generator**

Create `src/lakerunner_maestro_service.py`:

```python
#!/usr/bin/env python3
# Copyright (C) 2026 CardinalHQ, Inc
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import os
import yaml

from troposphere import Template


def load_maestro_config(config_file="lakerunner-maestro-defaults.yaml"):
    """Load default configuration for the Maestro stack from YAML."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "..", config_file)
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def create_maestro_template():
    """Create the CloudFormation template for the Maestro + MCP Gateway stack."""
    t = Template()
    t.set_description(
        "Lakerunner Maestro + MCP Gateway: single ECS Fargate service with a"
        " stack-local ALB. Reuses CommonInfra RDS and runs a psql init"
        " container that creates the maestro DB and user."
    )
    # Parameters, conditions, resources, and outputs are added in later tasks.
    # Console metadata lives here to guarantee its presence in the JSON.
    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [],
            "ParameterLabels": {},
        }
    })
    # Force non-empty Parameters / Resources / Outputs / Conditions so
    # downstream tests have something to serialize. These are replaced in
    # subsequent tasks; they exist only to keep the smoke test green.
    from troposphere import Parameter, Output, Equals, Ref
    _placeholder_param = t.add_parameter(Parameter(
        "CommonInfraStackName", Type="String",
        Description="REQUIRED: Name of the CommonInfra stack to import values from."
    ))
    t.add_condition("PlaceholderAlwaysFalse", Equals(Ref(_placeholder_param), "__never__"))
    t.add_output(Output("PlaceholderOutput", Value=Ref(_placeholder_param)))
    return t


if __name__ == "__main__":
    template = create_maestro_template()
    print(template.to_yaml())
```

- [ ] **Step 5: Run test to verify it passes**

Command: `pytest tests/test_maestro_service_simple.py -v`
Expected: PASS.

- [ ] **Step 6: Sanity-check template renders YAML without error**

Command: `python3 src/lakerunner_maestro_service.py > /tmp/maestro.yaml && head -5 /tmp/maestro.yaml`
Expected: YAML header line; `Description:` contains "Maestro".

- [ ] **Step 7: Commit**

```bash
git add lakerunner-maestro-defaults.yaml \
        src/lakerunner_maestro_service.py \
        tests/test_maestro_service_simple.py
git commit -m "Scaffold Maestro + MCP Gateway stack generator"
```

---

### Task 2: Add parameters + conditions + CommonInfra imports

**Files:**

- Modify: `src/lakerunner_maestro_service.py`
- Modify: `tests/test_maestro_service_simple.py`

- [ ] **Step 1: Extend the test with parameter expectations**

Append to `tests/test_maestro_service_simple.py` inside `TestMaestroTemplateSimple`:

```python
    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_required_parameters_exist(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG
        from lakerunner_maestro_service import create_maestro_template

        parameters = json.loads(create_maestro_template().to_json())["Parameters"]

        for name in [
            "CommonInfraStackName", "AlbScheme",
            "TaskCpu", "TaskMemoryMiB",
            "MaestroImage",
            "OidcIssuerUrl", "OidcAudience", "OidcSuperadminGroup",
            "OidcJwksUrl", "OidcSuperadminEmails", "OidcTrustUnverifiedEmails",
            "MaestroBaseUrl",
        ]:
            assert name in parameters, f"missing parameter {name}"

        assert parameters["AlbScheme"]["AllowedValues"] == ["internet-facing", "internal"]
        assert parameters["AlbScheme"]["Default"] == "internal"
        assert parameters["OidcTrustUnverifiedEmails"]["AllowedValues"] == ["true", "false"]
        assert parameters["OidcTrustUnverifiedEmails"]["Default"] == "false"
        assert parameters["OidcAudience"]["Default"] == "maestro-ui"
        assert parameters["OidcSuperadminGroup"]["Default"] == "maestro-superadmin"
        assert parameters["MaestroImage"]["Default"] == \
            "public.ecr.aws/cardinalhq.io/maestro:v0.23.0"

    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_conditions(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG
        from lakerunner_maestro_service import create_maestro_template

        conditions = json.loads(create_maestro_template().to_json())["Conditions"]
        assert "IsInternetFacing" in conditions
```

- [ ] **Step 2: Run test to verify it fails**

Command: `pytest tests/test_maestro_service_simple.py -v`
Expected: FAIL — new parameters and condition are absent (only the placeholders exist).

- [ ] **Step 3: Replace the placeholder scaffold with real parameters**

Replace the body of `create_maestro_template()` in `src/lakerunner_maestro_service.py`. The whole function becomes:

```python
def create_maestro_template():
    from troposphere import (
        Equals, Export, GetAtt, If, ImportValue, Output, Parameter, Ref, Split, Sub,
    )

    config = load_maestro_config()
    images = config.get("images", {})
    task_cfg = config.get("task", {})
    ports = config.get("ports", {})

    maestro_image_default = images.get(
        "maestro", "public.ecr.aws/cardinalhq.io/maestro:v0.23.0"
    )

    t = Template()
    t.set_description(
        "Lakerunner Maestro + MCP Gateway: single ECS Fargate service with a"
        " stack-local ALB. Reuses CommonInfra RDS and runs a psql init"
        " container that creates the maestro DB and user."
    )

    # -----------------------
    # Parameters
    # -----------------------
    CommonInfraStackName = t.add_parameter(Parameter(
        "CommonInfraStackName", Type="String",
        Description="REQUIRED: Name of the CommonInfra stack to import values from."
    ))
    AlbScheme = t.add_parameter(Parameter(
        "AlbScheme", Type="String",
        AllowedValues=["internet-facing", "internal"],
        Default="internal",
        Description="Load balancer scheme: 'internet-facing' for external access "
                    "or 'internal' for internal access only.",
    ))
    TaskCpu = t.add_parameter(Parameter(
        "TaskCpu", Type="String",
        Default=str(task_cfg.get("cpu", 1024)),
        Description="Fargate CPU units for the Maestro task (e.g., 512/1024/2048).",
    ))
    TaskMemoryMiB = t.add_parameter(Parameter(
        "TaskMemoryMiB", Type="String",
        Default=str(task_cfg.get("memory_mib", 2048)),
        Description="Fargate memory (MiB) for the Maestro task.",
    ))
    MaestroImage = t.add_parameter(Parameter(
        "MaestroImage", Type="String",
        Default=maestro_image_default,
        Description="Container image for both Maestro and the MCP Gateway "
                    "(same image, different entrypoints).",
    ))
    OidcIssuerUrl = t.add_parameter(Parameter(
        "OidcIssuerUrl", Type="String", Default="",
        Description="OIDC issuer URL. Leave blank to disable OIDC (Maestro "
                    "treats an empty value as 'OIDC disabled').",
    ))
    OidcAudience = t.add_parameter(Parameter(
        "OidcAudience", Type="String", Default="maestro-ui",
        Description="OIDC audience. Also used as the web UI OAuth client_id.",
    ))
    OidcSuperadminGroup = t.add_parameter(Parameter(
        "OidcSuperadminGroup", Type="String", Default="maestro-superadmin",
        Description="OIDC group name that grants Maestro superadmin access.",
    ))
    OidcJwksUrl = t.add_parameter(Parameter(
        "OidcJwksUrl", Type="String", Default="",
        Description="Optional OIDC JWKS URL override. Leave blank to use the "
                    "issuer's well-known JWKS endpoint.",
    ))
    OidcSuperadminEmails = t.add_parameter(Parameter(
        "OidcSuperadminEmails", Type="String", Default="",
        Description="Optional comma-separated email allowlist granted "
                    "superadmin access via OIDC.",
    ))
    OidcTrustUnverifiedEmails = t.add_parameter(Parameter(
        "OidcTrustUnverifiedEmails", Type="String",
        AllowedValues=["true", "false"], Default="false",
        Description="When 'true', treat all OIDC emails as verified. Leave "
                    "'false' unless you understand the security implications.",
    ))
    MaestroBaseUrl = t.add_parameter(Parameter(
        "MaestroBaseUrl", Type="String", Default="",
        Description="Optional public base URL for Maestro (forwarded as "
                    "MAESTRO_BASE_URL). Leave blank to let the UI infer.",
    ))

    t.set_metadata({
        "AWS::CloudFormation::Interface": {
            "ParameterGroups": [
                {"Label": {"default": "Infrastructure"},
                 "Parameters": ["CommonInfraStackName", "AlbScheme"]},
                {"Label": {"default": "Task Sizing"},
                 "Parameters": ["TaskCpu", "TaskMemoryMiB"]},
                {"Label": {"default": "Image"},
                 "Parameters": ["MaestroImage"]},
                {"Label": {"default": "OIDC (optional)"},
                 "Parameters": [
                     "OidcIssuerUrl", "OidcAudience", "OidcSuperadminGroup",
                     "OidcJwksUrl", "OidcSuperadminEmails",
                     "OidcTrustUnverifiedEmails",
                 ]},
                {"Label": {"default": "Misc"},
                 "Parameters": ["MaestroBaseUrl"]},
            ],
            "ParameterLabels": {
                "CommonInfraStackName": {"default": "Common Infra Stack Name"},
                "AlbScheme": {"default": "ALB Scheme"},
                "TaskCpu": {"default": "Fargate CPU"},
                "TaskMemoryMiB": {"default": "Fargate Memory (MiB)"},
                "MaestroImage": {"default": "Maestro Image"},
                "OidcIssuerUrl": {"default": "OIDC Issuer URL"},
                "OidcAudience": {"default": "OIDC Audience / UI client_id"},
                "OidcSuperadminGroup": {"default": "OIDC Superadmin Group"},
                "OidcJwksUrl": {"default": "OIDC JWKS URL"},
                "OidcSuperadminEmails": {"default": "OIDC Superadmin Emails"},
                "OidcTrustUnverifiedEmails": {"default": "OIDC Trust Unverified Emails"},
                "MaestroBaseUrl": {"default": "Maestro Base URL"},
            },
        }
    })

    # -----------------------
    # Cross-stack imports
    # -----------------------
    def ci_export(suffix):
        return Sub("${CommonInfraStackName}-%s" % suffix,
                   CommonInfraStackName=Ref(CommonInfraStackName))

    ClusterArnValue = ImportValue(ci_export("ClusterArn"))
    VpcIdValue = ImportValue(ci_export("VpcId"))
    TaskSecurityGroupIdValue = ImportValue(ci_export("TaskSGId"))
    PrivateSubnetsValue = Split(",", ImportValue(ci_export("PrivateSubnets")))
    PublicSubnetsValue = Split(",", ImportValue(ci_export("PublicSubnets")))
    DbEndpointValue = ImportValue(ci_export("DbEndpoint"))
    DbPortValue = ImportValue(ci_export("DbPort"))
    DbSecretArnValue = ImportValue(ci_export("DbSecretArn"))

    # -----------------------
    # Conditions
    # -----------------------
    t.add_condition("IsInternetFacing", Equals(Ref(AlbScheme), "internet-facing"))

    # Stash handles for later tasks on the template object. troposphere
    # templates tolerate arbitrary attribute assignment; we use it to hand
    # off values to subsequent helpers in this same function as it grows.
    t._maestro = {
        "ports": ports,
        "task_cfg": task_cfg,
        "images": images,
        "params": {
            "CommonInfraStackName": CommonInfraStackName,
            "AlbScheme": AlbScheme,
            "TaskCpu": TaskCpu,
            "TaskMemoryMiB": TaskMemoryMiB,
            "MaestroImage": MaestroImage,
            "OidcIssuerUrl": OidcIssuerUrl,
            "OidcAudience": OidcAudience,
            "OidcSuperadminGroup": OidcSuperadminGroup,
            "OidcJwksUrl": OidcJwksUrl,
            "OidcSuperadminEmails": OidcSuperadminEmails,
            "OidcTrustUnverifiedEmails": OidcTrustUnverifiedEmails,
            "MaestroBaseUrl": MaestroBaseUrl,
        },
        "imports": {
            "ClusterArn": ClusterArnValue,
            "VpcId": VpcIdValue,
            "TaskSGId": TaskSecurityGroupIdValue,
            "PrivateSubnets": PrivateSubnetsValue,
            "PublicSubnets": PublicSubnetsValue,
            "DbEndpoint": DbEndpointValue,
            "DbPort": DbPortValue,
            "DbSecretArn": DbSecretArnValue,
        },
    }

    # Resources and outputs are added in later tasks.
    return t
```

Also delete the placeholder `Output("PlaceholderOutput", ...)` — it's no longer there because we rewrote the whole function.

- [ ] **Step 2: Run the tests to verify they pass**

Command: `pytest tests/test_maestro_service_simple.py -v`
Expected: PASS for both the existing smoke test and the two new tests.

- [ ] **Step 3: Commit**

```bash
git add src/lakerunner_maestro_service.py tests/test_maestro_service_simple.py
git commit -m "Add Maestro stack parameters, conditions, and CommonInfra imports"
```

---

### Task 3: Add the Maestro DB secret, log groups, IAM roles

**Files:**

- Modify: `src/lakerunner_maestro_service.py`
- Modify: `tests/test_maestro_service_simple.py`

- [ ] **Step 1: Extend tests**

Append to `tests/test_maestro_service_simple.py`:

```python
    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_secret_and_log_groups(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG
        from lakerunner_maestro_service import create_maestro_template

        resources = json.loads(create_maestro_template().to_json())["Resources"]

        assert "MaestroDbSecret" in resources
        secret = resources["MaestroDbSecret"]["Properties"]
        assert '"username":"maestro"' in secret["GenerateSecretString"]["SecretStringTemplate"]
        assert secret["GenerateSecretString"]["GenerateStringKey"] == "password"
        assert secret["GenerateSecretString"]["PasswordLength"] == 32

        for lg in ["MaestroDbInitLogGroup", "MaestroMcpGatewayLogGroup",
                   "MaestroServerLogGroup"]:
            assert lg in resources

    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_iam_roles_present_with_expected_policies(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG
        from lakerunner_maestro_service import create_maestro_template

        resources = json.loads(create_maestro_template().to_json())["Resources"]

        assert "MaestroExecRole" in resources
        exec_role = resources["MaestroExecRole"]["Properties"]
        assert "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy" \
            in exec_role["ManagedPolicyArns"]
        exec_policies = {p["PolicyName"] for p in exec_role["Policies"]}
        assert "SecretsManagerAccess" in exec_policies

        assert "MaestroTaskRole" in resources
        task_role = resources["MaestroTaskRole"]["Properties"]
        task_policies = {p["PolicyName"] for p in task_role["Policies"]}
        assert "LogAccess" in task_policies
        # Sanity: the task role should NOT grant Bedrock access — Maestro
        # doesn't call Bedrock from this stack.
        assert "BedrockAccess" not in task_policies
```

- [ ] **Step 2: Run tests to verify failures**

Command: `pytest tests/test_maestro_service_simple.py -v`
Expected: both new tests FAIL — resources don't exist yet.

- [ ] **Step 3: Add the secret, log groups, and IAM roles**

In `src/lakerunner_maestro_service.py`, add these imports at the top of `create_maestro_template()` (merge with existing):

```python
    from troposphere.iam import Policy, Role
    from troposphere.logs import LogGroup
    from troposphere.secretsmanager import GenerateSecretString, Secret
```

Before the `return t` line in `create_maestro_template()`, insert:

```python
    # -----------------------
    # Database password secret
    # -----------------------
    maestro_db_secret = t.add_resource(Secret(
        "MaestroDbSecret",
        Name=Sub("${AWS::StackName}-maestro-db"),
        Description="Maestro PostgreSQL user password",
        GenerateSecretString=GenerateSecretString(
            SecretStringTemplate='{"username":"maestro"}',
            GenerateStringKey="password",
            ExcludeCharacters=' !"#$%&\'()*+,./:;<=>?@[\\]^`{|}~',
            PasswordLength=32,
        ),
    ))

    # -----------------------
    # Log groups
    # -----------------------
    db_init_lg = t.add_resource(LogGroup(
        "MaestroDbInitLogGroup",
        LogGroupName=Sub("/ecs/${AWS::StackName}/db-init"),
        RetentionInDays=14,
    ))
    mcp_gw_lg = t.add_resource(LogGroup(
        "MaestroMcpGatewayLogGroup",
        LogGroupName=Sub("/ecs/${AWS::StackName}/mcp-gateway"),
        RetentionInDays=14,
    ))
    maestro_lg = t.add_resource(LogGroup(
        "MaestroServerLogGroup",
        LogGroupName=Sub("/ecs/${AWS::StackName}/maestro"),
        RetentionInDays=14,
    ))

    # -----------------------
    # IAM: execution and task roles
    # -----------------------
    exec_role = t.add_resource(Role(
        "MaestroExecRole",
        RoleName=Sub("${AWS::StackName}-exec-role"),
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        },
        ManagedPolicyArns=[
            "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
        ],
        Policies=[Policy(
            PolicyName="SecretsManagerAccess",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": ["secretsmanager:GetSecretValue"],
                    "Resource": [
                        Sub("arn:aws:secretsmanager:${AWS::Region}:"
                            "${AWS::AccountId}:secret:${AWS::StackName}-*"),
                        Sub("${S}*", S=DbSecretArnValue),
                    ],
                }],
            },
        )],
    ))

    task_role = t.add_resource(Role(
        "MaestroTaskRole",
        RoleName=Sub("${AWS::StackName}-task-role"),
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        },
        Policies=[Policy(
            PolicyName="LogAccess",
            PolicyDocument={
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": "*",
                }],
            },
        )],
    ))

    # Stash for later tasks
    t._maestro["resources"] = {
        "MaestroDbSecret": maestro_db_secret,
        "DbInitLogGroup": db_init_lg,
        "McpGatewayLogGroup": mcp_gw_lg,
        "MaestroServerLogGroup": maestro_lg,
        "ExecRole": exec_role,
        "TaskRole": task_role,
    }
```

- [ ] **Step 4: Run tests to confirm green**

Command: `pytest tests/test_maestro_service_simple.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lakerunner_maestro_service.py tests/test_maestro_service_simple.py
git commit -m "Add Maestro DB secret, log groups, and IAM roles"
```

---

### Task 4: Add ALB security group, ALB, target group, listener

**Files:**

- Modify: `src/lakerunner_maestro_service.py`
- Modify: `tests/test_maestro_service_simple.py`

- [ ] **Step 1: Extend tests**

Append to `tests/test_maestro_service_simple.py`:

```python
    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_alb_resources(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG
        from lakerunner_maestro_service import create_maestro_template

        resources = json.loads(create_maestro_template().to_json())["Resources"]

        assert "MaestroAlbSecurityGroup" in resources
        assert "MaestroAlbListenerIngress" in resources
        assert "MaestroTaskFromAlbIngress" in resources
        assert "MaestroAlb" in resources
        assert "MaestroTg" in resources
        assert "MaestroListener" in resources

        tg = resources["MaestroTg"]["Properties"]
        assert tg["Port"] == 4200
        assert tg["HealthCheckPath"] == "/api/health"
        assert tg["TargetType"] == "ip"

        listener = resources["MaestroListener"]["Properties"]
        assert listener["Port"] == "80"
        assert listener["Protocol"] == "HTTP"
```

- [ ] **Step 2: Run tests to verify failure**

Command: `pytest tests/test_maestro_service_simple.py -v`
Expected: the new test FAILs — ALB resources not yet added.

- [ ] **Step 3: Add ALB resources**

Merge these imports into the existing imports in `create_maestro_template()`:

```python
    from troposphere.ec2 import SecurityGroup, SecurityGroupIngress
    from troposphere.elasticloadbalancingv2 import (
        Action as AlbAction,
        Listener,
        LoadBalancer,
        Matcher,
        TargetGroup,
        TargetGroupAttribute,
    )
```

Before `return t`, insert:

```python
    maestro_port = ports.get("maestro", 4200)
    listener_port = ports.get("alb_listener", 80)

    alb_sg = t.add_resource(SecurityGroup(
        "MaestroAlbSecurityGroup",
        GroupDescription="Security group for Maestro ALB",
        VpcId=VpcIdValue,
        SecurityGroupEgress=[{
            "IpProtocol": "-1",
            "CidrIp": "0.0.0.0/0",
            "Description": "Allow all outbound",
        }],
    ))

    t.add_resource(SecurityGroupIngress(
        "MaestroAlbListenerIngress",
        GroupId=Ref(alb_sg),
        IpProtocol="tcp",
        FromPort=listener_port, ToPort=listener_port,
        CidrIp="0.0.0.0/0",
        Description=f"HTTP {listener_port} for Maestro ALB",
    ))

    t.add_resource(SecurityGroupIngress(
        "MaestroTaskFromAlbIngress",
        GroupId=TaskSecurityGroupIdValue,
        IpProtocol="tcp",
        FromPort=maestro_port, ToPort=maestro_port,
        SourceSecurityGroupId=Ref(alb_sg),
        Description=f"Maestro ALB -> task port {maestro_port}",
    ))

    alb = t.add_resource(LoadBalancer(
        "MaestroAlb",
        Scheme=Ref(AlbScheme),
        SecurityGroups=[Ref(alb_sg)],
        Subnets=If("IsInternetFacing", PublicSubnetsValue, PrivateSubnetsValue),
        Type="application",
    ))

    tg = t.add_resource(TargetGroup(
        "MaestroTg",
        Name=If("IsInternetFacing",
                Sub("${AWS::StackName}-ext"),
                Sub("${AWS::StackName}-int")),
        Port=maestro_port, Protocol="HTTP",
        VpcId=VpcIdValue,
        TargetType="ip",
        HealthCheckPath="/api/health",
        HealthCheckProtocol="HTTP",
        HealthCheckIntervalSeconds=30,
        HealthCheckTimeoutSeconds=5,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=3,
        Matcher=Matcher(HttpCode="200"),
        TargetGroupAttributes=[
            TargetGroupAttribute(Key="stickiness.enabled", Value="false"),
            TargetGroupAttribute(Key="deregistration_delay.timeout_seconds",
                                 Value="30"),
        ],
    ))

    listener = t.add_resource(Listener(
        "MaestroListener",
        LoadBalancerArn=Ref(alb),
        Port=str(listener_port),
        Protocol="HTTP",
        DefaultActions=[AlbAction(Type="forward", TargetGroupArn=Ref(tg))],
    ))

    t._maestro["resources"].update({
        "AlbSg": alb_sg,
        "Alb": alb,
        "Tg": tg,
        "Listener": listener,
    })
```

- [ ] **Step 4: Run tests**

Command: `pytest tests/test_maestro_service_simple.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lakerunner_maestro_service.py tests/test_maestro_service_simple.py
git commit -m "Add Maestro ALB, target group, listener, and SG ingress rules"
```

---

### Task 5: Build the three container definitions

**Files:**

- Modify: `src/lakerunner_maestro_service.py`
- Modify: `tests/test_maestro_service_simple.py`

This task adds the three `ContainerDefinition` objects and a helper that emits the shared `MAESTRO_DB_*` env block. They're built into a list on `t._maestro` so Task 6 can stamp them into the TaskDefinition.

- [ ] **Step 1: Extend tests**

Append to `tests/test_maestro_service_simple.py`:

```python
    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_container_definitions_exist_on_task(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG
        from lakerunner_maestro_service import create_maestro_template

        resources = json.loads(create_maestro_template().to_json())["Resources"]
        assert "MaestroTaskDef" in resources
        containers = resources["MaestroTaskDef"]["Properties"]["ContainerDefinitions"]
        names = [c["Name"] for c in containers]
        assert names == ["DbInit", "McpGateway", "Maestro"]

        by_name = {c["Name"]: c for c in containers}

        # DbInit uses generic psql bootstrapper and exits 0
        db_init = by_name["DbInit"]
        assert db_init["Essential"] is False
        db_init_envs = {e["Name"]: e["Value"] for e in db_init["Environment"]}
        assert db_init_envs["GRAFANA_DB_NAME"] == "maestro"
        assert db_init_envs["GRAFANA_DB_USER"] == "maestro"

        # McpGateway runs the alt entrypoint
        mcp = by_name["McpGateway"]
        assert mcp["Command"] == ["/app/entrypoint.sh", "mcp-gateway"]
        mcp_ports = [p["ContainerPort"] for p in mcp["PortMappings"]]
        assert 8080 in mcp_ports
        assert mcp["User"] == "65532"
        assert mcp["ReadonlyRootFilesystem"] is True
        # Depends on DbInit SUCCESS
        mcp_deps = {d["ContainerName"]: d["Condition"] for d in mcp["DependsOn"]}
        assert mcp_deps["DbInit"] == "SUCCESS"

        # Maestro depends on DbInit SUCCESS + McpGateway HEALTHY
        maestro = by_name["Maestro"]
        assert maestro["Essential"] is True
        maestro_ports = [p["ContainerPort"] for p in maestro["PortMappings"]]
        assert 4200 in maestro_ports
        m_deps = {d["ContainerName"]: d["Condition"] for d in maestro["DependsOn"]}
        assert m_deps["DbInit"] == "SUCCESS"
        assert m_deps["McpGateway"] == "HEALTHY"
        m_envs = {e["Name"]: e["Value"] for e in maestro["Environment"]}
        assert m_envs["MCP_GATEWAY_URL"] == "http://localhost:8080"
        assert m_envs["PORT"] == "4200"
        assert "MAESTRO_DATABASE_URL" in m_envs

        # OIDC envs are attached (as Refs/Subs — just confirm presence by key)
        maestro_env_names = set(m_envs.keys())
        for name in ["OIDC_ISSUER_URL", "OIDC_AUDIENCE", "OIDC_SUPERADMIN_GROUP",
                     "OIDC_JWKS_URL", "OIDC_SUPERADMIN_EMAILS",
                     "OIDC_TRUST_UNVERIFIED_EMAILS", "MAESTRO_BASE_URL"]:
            assert name in maestro_env_names, f"missing Maestro env {name}"
```

Note on `ReadonlyRootFilesystem`: troposphere emits it via `ContainerDefinition(ReadonlyRootFilesystem=True)`. The assertion key is exactly `"ReadonlyRootFilesystem"` (CFN camelCase).

- [ ] **Step 2: Run tests to verify failure**

Command: `pytest tests/test_maestro_service_simple.py -v`
Expected: new test FAILs — `MaestroTaskDef` absent.

- [ ] **Step 3: Build container definitions and task definition**

Merge these imports into the imports at the top of `create_maestro_template()`:

```python
    from troposphere.ecs import (
        ContainerDefinition,
        Environment,
        HealthCheck,
        LogConfiguration,
        MountPoint,
        PortMapping,
        RuntimePlatform,
        Secret as EcsSecret,
        TaskDefinition,
        Volume,
    )
```

Before `return t`, insert:

```python
    # -----------------------
    # Shared env blocks
    # -----------------------
    def _db_env():
        return [
            Environment(Name="MAESTRO_DB_HOST", Value=DbEndpointValue),
            Environment(Name="MAESTRO_DB_PORT", Value=DbPortValue),
            Environment(Name="MAESTRO_DB_NAME", Value="maestro"),
            Environment(Name="MAESTRO_DB_USER", Value="maestro"),
            Environment(Name="MAESTRO_DB_SSLMODE", Value="require"),
            Environment(
                Name="MAESTRO_DATABASE_URL",
                Value=("postgresql://$(MAESTRO_DB_USER):$(MAESTRO_DB_PASSWORD)@"
                       "$(MAESTRO_DB_HOST):$(MAESTRO_DB_PORT)/$(MAESTRO_DB_NAME)"
                       "?sslmode=$(MAESTRO_DB_SSLMODE)"),
            ),
        ]

    def _db_password_secret():
        return EcsSecret(
            Name="MAESTRO_DB_PASSWORD",
            ValueFrom=Sub("${S}:password::", S=Ref(maestro_db_secret)),
        )

    # -----------------------
    # DbInit container (generic psql bootstrapper)
    # -----------------------
    db_init_image = images.get(
        "db_init", "ghcr.io/cardinalhq/initcontainer-grafana:latest"
    )

    db_init_container = ContainerDefinition(
        Name="DbInit",
        Image=db_init_image,
        Essential=False,
        Environment=[
            Environment(Name="PGHOST", Value=DbEndpointValue),
            Environment(Name="PGPORT", Value=DbPortValue),
            Environment(Name="PGDATABASE", Value="postgres"),
            Environment(Name="PGSSLMODE", Value="require"),
            Environment(Name="GRAFANA_DB_NAME", Value="maestro"),
            Environment(Name="GRAFANA_DB_USER", Value="maestro"),
        ],
        Secrets=[
            EcsSecret(
                Name="PGUSER",
                ValueFrom=Sub("${S}:username::", S=DbSecretArnValue),
            ),
            EcsSecret(
                Name="PGPASSWORD",
                ValueFrom=Sub("${S}:password::", S=DbSecretArnValue),
            ),
            EcsSecret(
                Name="GRAFANA_DB_PASSWORD",
                ValueFrom=Sub("${S}:password::", S=Ref(maestro_db_secret)),
            ),
        ],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(db_init_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "db-init",
            },
        ),
    )

    # -----------------------
    # McpGateway container
    # -----------------------
    mcp_port = ports.get("mcp_gateway", 8080)
    mcp_debug_port = ports.get("mcp_gateway_debug", 9090)

    mcp_container = ContainerDefinition(
        Name="McpGateway",
        Image=Ref(MaestroImage),
        Essential=True,
        User="65532",
        ReadonlyRootFilesystem=True,
        Command=["/app/entrypoint.sh", "mcp-gateway"],
        PortMappings=[
            PortMapping(ContainerPort=mcp_port, Protocol="tcp"),
            PortMapping(ContainerPort=mcp_debug_port, Protocol="tcp"),
        ],
        Environment=_db_env() + [
            Environment(Name="MCP_PORT", Value=str(mcp_port)),
            Environment(Name="MCP_DEBUG_PORT", Value=str(mcp_debug_port)),
        ],
        Secrets=[_db_password_secret()],
        HealthCheck=HealthCheck(
            Command=["CMD-SHELL",
                     f"wget --no-verbose --tries=1 --spider "
                     f"http://localhost:{mcp_port}/healthz || exit 1"],
            Interval=30, Timeout=5, Retries=3, StartPeriod=30,
        ),
        DependsOn=[{"ContainerName": "DbInit", "Condition": "SUCCESS"}],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(mcp_gw_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "mcp-gateway",
            },
        ),
    )

    # -----------------------
    # Maestro container
    # -----------------------
    maestro_env = _db_env() + [
        Environment(Name="MCP_GATEWAY_URL",
                    Value=f"http://localhost:{mcp_port}"),
        Environment(Name="PORT", Value=str(maestro_port)),
        Environment(Name="MAESTRO_BASE_URL", Value=Ref(MaestroBaseUrl)),
        Environment(Name="OIDC_ISSUER_URL", Value=Ref(OidcIssuerUrl)),
        Environment(Name="OIDC_AUDIENCE", Value=Ref(OidcAudience)),
        Environment(Name="OIDC_SUPERADMIN_GROUP", Value=Ref(OidcSuperadminGroup)),
        Environment(Name="OIDC_JWKS_URL", Value=Ref(OidcJwksUrl)),
        Environment(Name="OIDC_SUPERADMIN_EMAILS", Value=Ref(OidcSuperadminEmails)),
        Environment(Name="OIDC_TRUST_UNVERIFIED_EMAILS",
                    Value=Ref(OidcTrustUnverifiedEmails)),
    ]

    maestro_container = ContainerDefinition(
        Name="Maestro",
        Image=Ref(MaestroImage),
        Essential=True,
        User="65532",
        ReadonlyRootFilesystem=True,
        PortMappings=[PortMapping(ContainerPort=maestro_port, Protocol="tcp")],
        Environment=maestro_env,
        Secrets=[_db_password_secret()],
        MountPoints=[MountPoint(ContainerPath="/tmp", SourceVolume="tmp",
                                ReadOnly=False)],
        HealthCheck=HealthCheck(
            Command=["CMD-SHELL",
                     f"wget --no-verbose --tries=1 --spider "
                     f"http://localhost:{maestro_port}/api/health || exit 1"],
            Interval=30, Timeout=5, Retries=3, StartPeriod=60,
        ),
        DependsOn=[
            {"ContainerName": "DbInit", "Condition": "SUCCESS"},
            {"ContainerName": "McpGateway", "Condition": "HEALTHY"},
        ],
        LogConfiguration=LogConfiguration(
            LogDriver="awslogs",
            Options={
                "awslogs-group": Ref(maestro_lg),
                "awslogs-region": Ref("AWS::Region"),
                "awslogs-stream-prefix": "maestro",
            },
        ),
    )

    # -----------------------
    # Task Definition
    # -----------------------
    task_def = t.add_resource(TaskDefinition(
        "MaestroTaskDef",
        Family=Sub("${AWS::StackName}-maestro"),
        Cpu=Ref(TaskCpu),
        Memory=Ref(TaskMemoryMiB),
        NetworkMode="awsvpc",
        RequiresCompatibilities=["FARGATE"],
        ExecutionRoleArn=GetAtt(exec_role, "Arn"),
        TaskRoleArn=GetAtt(task_role, "Arn"),
        ContainerDefinitions=[db_init_container, mcp_container, maestro_container],
        Volumes=[Volume(Name="tmp")],
        RuntimePlatform=RuntimePlatform(
            CpuArchitecture="ARM64",
            OperatingSystemFamily="LINUX",
        ),
    ))

    t._maestro["resources"].update({"TaskDef": task_def})
```

- [ ] **Step 4: Run tests**

Command: `pytest tests/test_maestro_service_simple.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lakerunner_maestro_service.py tests/test_maestro_service_simple.py
git commit -m "Add Maestro task definition with DbInit + McpGateway + Maestro containers"
```

---

### Task 6: Add the ECS Service and outputs

**Files:**

- Modify: `src/lakerunner_maestro_service.py`
- Modify: `tests/test_maestro_service_simple.py`

- [ ] **Step 1: Extend tests**

Append to `tests/test_maestro_service_simple.py`:

```python
    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_service_and_outputs(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG
        from lakerunner_maestro_service import create_maestro_template

        template_dict = json.loads(create_maestro_template().to_json())
        resources = template_dict["Resources"]

        assert "MaestroService" in resources
        svc = resources["MaestroService"]["Properties"]
        assert svc["LaunchType"] == "FARGATE"
        assert svc["DesiredCount"] == 1
        assert svc["LoadBalancers"][0]["ContainerName"] == "Maestro"
        assert svc["LoadBalancers"][0]["ContainerPort"] == 4200
        assert resources["MaestroService"]["DependsOn"] == ["MaestroListener"]

        outputs = template_dict["Outputs"]
        for name in ["MaestroAlbDNS", "MaestroAlbArn", "MaestroServiceArn",
                     "MaestroUrl", "MaestroDbSecretArn"]:
            assert name in outputs
```

- [ ] **Step 2: Run tests to verify failure**

Command: `pytest tests/test_maestro_service_simple.py -v`
Expected: new test FAILs — `MaestroService` absent.

- [ ] **Step 3: Add the ECS Service and outputs**

Merge these imports:

```python
    from troposphere import Tags
    from troposphere.ecs import (
        AwsvpcConfiguration,
        LoadBalancer as EcsLoadBalancer,
        NetworkConfiguration,
        Service,
    )
```

Before `return t`, insert:

```python
    service = t.add_resource(Service(
        "MaestroService",
        ServiceName=Sub("${AWS::StackName}-maestro"),
        Cluster=ClusterArnValue,
        TaskDefinition=Ref(task_def),
        LaunchType="FARGATE",
        DesiredCount=1,
        NetworkConfiguration=NetworkConfiguration(
            AwsvpcConfiguration=AwsvpcConfiguration(
                Subnets=PrivateSubnetsValue,
                SecurityGroups=[TaskSecurityGroupIdValue],
                AssignPublicIp="DISABLED",
            ),
        ),
        LoadBalancers=[EcsLoadBalancer(
            ContainerName="Maestro",
            ContainerPort=maestro_port,
            TargetGroupArn=Ref(tg),
        )],
        DependsOn=["MaestroListener"],
        EnableExecuteCommand=True,
        EnableECSManagedTags=True,
        PropagateTags="SERVICE",
        Tags=Tags(
            Name=Sub("${AWS::StackName}-maestro"),
            ManagedBy="Lakerunner",
            Environment=Ref("AWS::StackName"),
            Component="Service",
        ),
    ))

    # -----------------------
    # Outputs
    # -----------------------
    t.add_output(Output(
        "MaestroAlbDNS",
        Value=GetAtt(alb, "DNSName"),
        Export=Export(name=Sub("${AWS::StackName}-MaestroAlbDNS")),
    ))
    t.add_output(Output(
        "MaestroAlbArn",
        Value=Ref(alb),
        Export=Export(name=Sub("${AWS::StackName}-MaestroAlbArn")),
    ))
    t.add_output(Output(
        "MaestroServiceArn",
        Value=Ref(service),
        Export=Export(name=Sub("${AWS::StackName}-MaestroServiceArn")),
    ))
    t.add_output(Output(
        "MaestroDbSecretArn",
        Value=Ref(maestro_db_secret),
        Export=Export(name=Sub("${AWS::StackName}-MaestroDbSecretArn")),
    ))
    t.add_output(Output(
        "MaestroUrl",
        Description="URL to access the Maestro UI/API",
        Value=Sub("http://${Dns}", Dns=GetAtt(alb, "DNSName")),
    ))
```

- [ ] **Step 4: Run tests**

Command: `pytest tests/test_maestro_service_simple.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lakerunner_maestro_service.py tests/test_maestro_service_simple.py
git commit -m "Add Maestro ECS service, load balancer wiring, and outputs"
```

---

### Task 7: Wire into build.sh and run cfn-lint

**Files:**

- Modify: `build.sh`

- [ ] **Step 1: Add the generation step**

Edit `build.sh`. Between the existing `echo "06. ..."` block and `echo "98. ..."` block, insert:

```sh
echo "07. Generating Lakerunner Maestro Service..."
python3 src/lakerunner_maestro_service.py > generated-templates/lakerunner-07-maestro-service.yaml
cfn-lint generated-templates/lakerunner-07-maestro-service.yaml
```

Keep the ordering numeric: 06 (otel) → 07 (maestro) → 98 (bedrock-setup) → 99 (debug-utility).

- [ ] **Step 2: Run the full build**

Command: `./build.sh`
Expected: runs to completion. `cfn-lint` prints no errors for `lakerunner-07-maestro-service.yaml`. Warnings (W1020 / W1030) are acceptable per repo convention.

If cfn-lint surfaces an error, fix the generator (not the YAML — it's a build artifact) and re-run.

- [ ] **Step 3: Spot-check the rendered YAML**

Command: `grep -E '^Description|MaestroService|OidcIssuerUrl' generated-templates/lakerunner-07-maestro-service.yaml | head`
Expected: sees `Description:` starting with "Lakerunner Maestro ...", `MaestroService` resource, `OidcIssuerUrl` parameter.

- [ ] **Step 4: Run the full test suite**

Command: `make test`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add build.sh
git commit -m "Generate Maestro stack template in build.sh"
```

---

## Phase B — Grafana cleanup

### Task 8: Update Grafana defaults YAML

**Files:**

- Modify: `lakerunner-grafana-defaults.yaml`

- [ ] **Step 1: Strip AI sections from the YAML**

Open `lakerunner-grafana-defaults.yaml`. Delete these top-level sections entirely: `mcp_gateway`, `conductor_server`, `maestro_server`. Delete the `mcp_gateway`, `conductor_server`, and `maestro_server` entries from `images`. Keep `grafana`, `grafana_init`, `grafana`, `task`, `database`, `api_keys`.

Final `images` block should look like:

```yaml
images:
  grafana: "grafana/grafana:latest"
  grafana_init: "ghcr.io/cardinalhq/initcontainer-grafana:latest"
```

Leave the rest of the file untouched.

- [ ] **Step 2: Confirm YAML still parses**

Command: `python3 -c "import yaml; yaml.safe_load(open('lakerunner-grafana-defaults.yaml'))"`
Expected: no output (successful parse).

- [ ] **Step 3: Commit**

```bash
git add lakerunner-grafana-defaults.yaml
git commit -m "Remove MCP Gateway, Conductor, and Maestro sections from Grafana defaults"
```

---

### Task 9: Strip AI containers from the Grafana generator

**Files:**

- Modify: `src/lakerunner_grafana_service.py`

- [ ] **Step 1: Remove AI-related parameters and metadata**

In `src/lakerunner_grafana_service.py`:

1. Remove the `BedrockModel` parameter block (lines currently ~99–110 — the full `BedrockModel = t.add_parameter(...)` block).
1. Keep the `LakerunnerApiKey` parameter. Its default still comes from `api_keys[0]['keys'][0]` via `load_grafana_config`.
1. In the `t.set_metadata({...})` call, remove `"BedrockModel"` from `ParameterGroups` and from `ParameterLabels`. Rename the "AI Configuration" group to "Datasource", keeping `LakerunnerApiKey` as its only parameter.
1. Update the template description:

   ```python
   t.set_description(
       "Lakerunner Grafana: Grafana with pre-configured plugins, PostgreSQL"
       " storage, and ALB. The Cardinal datasource is wired to the Query API."
   )
   ```

- [ ] **Step 2: Remove AI log groups and secrets**

1. Delete the `mcp_gw_log_group`, `conductor_log_group`, `maestro_log_group` blocks.
1. Delete the `ai_internal_secret = t.add_resource(Secret("AiInternalSecret", ...))` block.
1. Delete the `lakerunner_api_key_secret = t.add_resource(Secret("LakerunnerApiKeySecret", ...))` block. The datasource will inline `LakerunnerApiKey` via `Sub` instead (see step 4).

- [ ] **Step 3: Remove AI container definitions**

Delete these container construction blocks entirely:

1. `mcp_gateway_container = ContainerDefinition(...)` and the surrounding `mcp_gw_env`/`mcp_gw_secrets` setup, plus the `container_definitions.append(mcp_gateway_container)` line.
1. `conductor_container = ContainerDefinition(...)` and the surrounding `conductor_env`/`conductor_secrets` setup, plus the `container_definitions.append(conductor_container)` line.
1. `maestro_container = ContainerDefinition(...)` and the surrounding `maestro_env`/`maestro_secrets` setup, plus the `container_definitions.append(maestro_container)` line.
1. Remove the `maestro_config = config.get('maestro_server', {})`, `mcp_gw_config = config.get('mcp_gateway', {})`, and `conductor_config = config.get('conductor_server', {})` loader lines at the top of the function.
1. Remove the `mcp_gateway_image`, `conductor_server_image`, `maestro_server_image` lines.

- [ ] **Step 4: Put the API key straight into the datasource YAML**

Find the `grafana_datasource_config` dict. Change:

```python
"secureJsonData": {
    "apiKey": default_api_key
}
```

to a templated form, and update the `SSMParameter` body to `Sub` both the Query API URL and the API key in. The `default_api_key` lookup can stay (it's still the YAML default), but the emitted value must reference the `LakerunnerApiKey` parameter so operators can override it at stack creation:

```python
grafana_datasource_param = t.add_resource(SSMParameter(
    "GrafanaDatasourceConfig",
    Name=Sub("${AWS::StackName}-grafana-datasource-config"),
    Type="String",
    Value=Sub(
        yaml.dump(grafana_datasource_config),
        QUERY_API_URL=Ref(QueryApiUrl),
        LAKERUNNER_API_KEY=Ref(LakerunnerApiKey),
    ),
    Description="Grafana datasource configuration for Cardinal plugin",
))
```

And the dict changes to:

```python
grafana_datasource_config = {
    "apiVersion": 1,
    "datasources": [
        {
            "name": "Cardinal",
            "type": "cardinalhq-lakerunner-datasource",
            "access": "proxy",
            "isDefault": True,
            "editable": True,
            "jsonData": {"customPath": "${QUERY_API_URL}"},
            "secureJsonData": {"apiKey": "${LAKERUNNER_API_KEY}"},
        }
    ],
}
```

- [ ] **Step 5: Remove Bedrock policy from Grafana task role**

Inside `TaskRole = t.add_resource(Role("GrafanaTaskRole", ...))`, delete the `Policy(PolicyName="BedrockAccess", ...)` entry from the `Policies` list. Leave `LogAccess` alone.

- [ ] **Step 6: Build and lint**

Command: `./build.sh`
Expected: step 05 regenerates `lakerunner-05-grafana-service.yaml` with no cfn-lint errors. Warnings OK.

- [ ] **Step 7: Commit**

```bash
git add src/lakerunner_grafana_service.py
git commit -m "Strip MCP Gateway, Conductor, and Maestro sidecars from Grafana stack"
```

---

### Task 10: Update the Grafana tests

**Files:**

- Modify: `tests/test_grafana_service_simple.py`

- [ ] **Step 1: Trim MOCK_CONFIG**

In `tests/test_grafana_service_simple.py`, delete the `mcp_gateway`, `conductor_server`, and `maestro_server` keys from `MOCK_CONFIG`. Delete the `mcp_gateway`, `conductor_server`, and `maestro_server` entries from `MOCK_CONFIG["images"]`. Keep `grafana` + `grafana_init`. Keep `api_keys`.

- [ ] **Step 2: Rewrite assertions**

Replace the existing assertion bodies as follows. Unchanged tests stay.

Replace `test_template_description_correct`:

```python
    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_template_description_correct(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        assert "Grafana" in template_dict["Description"]
        assert "MCP Gateway" not in template_dict["Description"]
        assert "Conductor" not in template_dict["Description"]
        assert "Maestro" not in template_dict["Description"]
```

Replace `test_required_parameters_exist`:

```python
    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_required_parameters_exist(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        parameters = template_dict["Parameters"]

        assert "CommonInfraStackName" in parameters
        assert "QueryApiUrl" in parameters
        assert "AlbScheme" in parameters
        assert "LakerunnerApiKey" in parameters
        assert "GrafanaResetToken" in parameters

        # Removed — these should be gone
        assert "BedrockModel" not in parameters
        assert "GrafanaImage" not in parameters
        assert "GrafanaInitImage" not in parameters
        assert "McpGatewayImage" not in parameters
        assert "ConductorServerImage" not in parameters
```

Replace `test_grafana_resources_exist`:

```python
    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_grafana_resources_exist(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        resources = json.loads(template.to_json())["Resources"]

        # Kept
        assert "GrafanaService" in resources
        assert "GrafanaTaskDef" in resources
        assert "GrafanaAlb" in resources
        assert "GrafanaTg" in resources
        assert "GrafanaSecret" in resources
        assert "GrafanaLogGroup" in resources

        # Removed
        for name in ["McpGatewayLogGroup", "ConductorServerLogGroup",
                     "MaestroServerLogGroup", "AiInternalSecret",
                     "LakerunnerApiKeySecret"]:
            assert name not in resources, f"{name} should have been removed"
```

Replace `test_task_definition_has_all_containers`:

```python
    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_task_definition_has_only_init_and_grafana(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        containers = template_dict["Resources"]["GrafanaTaskDef"][
            "Properties"]["ContainerDefinitions"]
        names = [c["Name"] for c in containers]

        assert "GrafanaInit" in names
        assert "GrafanaContainer" in names
        assert "McpGateway" not in names
        assert "ConductorServer" not in names
        assert "MaestroServer" not in names
        assert len(containers) == 2
```

Replace `test_task_definition_uses_literal_images`:

```python
    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_task_definition_uses_literal_images(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        template_dict = json.loads(template.to_json())

        containers = template_dict["Resources"]["GrafanaTaskDef"][
            "Properties"]["ContainerDefinitions"]
        images_by_name = {c["Name"]: c["Image"] for c in containers}
        for name in ["GrafanaInit", "GrafanaContainer"]:
            image = images_by_name[name]
            assert isinstance(image, str) and image
            assert "Ref" not in image and "Fn::" not in image
            assert ":" in image
```

Delete these tests outright (they assert behavior that no longer exists):

1. `test_conductor_depends_on_mcp_gateway`
1. `test_bedrock_permissions_in_task_role`
1. `test_ai_containers_run_as_nonroot`
1. `test_bedrock_model_parameter_has_allowed_values`

Replace `test_grafana_outputs_exist` — unchanged, but re-verify it runs (outputs are untouched by the cleanup):

```python
    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_grafana_outputs_exist(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        outputs = json.loads(template.to_json())["Outputs"]

        assert "GrafanaAlbDNS" in outputs
        assert "GrafanaServiceArn" in outputs
        assert "GrafanaAdminSecretArn" in outputs
        assert "GrafanaUrl" in outputs
```

Add a new test to guard the datasource substitution path:

```python
    @patch('lakerunner_grafana_service.load_grafana_config')
    def test_datasource_ssm_param_uses_parameter_refs(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG

        from lakerunner_grafana_service import create_grafana_template

        template = create_grafana_template()
        resources = json.loads(template.to_json())["Resources"]

        ds = resources["GrafanaDatasourceConfig"]["Properties"]
        value = ds["Value"]
        # Value should be an Fn::Sub with variables for QUERY_API_URL and LAKERUNNER_API_KEY
        assert "Fn::Sub" in value
        sub_args = value["Fn::Sub"]
        # Fn::Sub with vars is a two-element list [template, {var: ref}]
        assert isinstance(sub_args, list) and len(sub_args) == 2
        template_str, var_map = sub_args
        assert "${QUERY_API_URL}" in template_str
        assert "${LAKERUNNER_API_KEY}" in template_str
        assert "QUERY_API_URL" in var_map
        assert "LAKERUNNER_API_KEY" in var_map
```

- [ ] **Step 3: Run the Grafana-specific tests**

Command: `pytest tests/test_grafana_service_simple.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_grafana_service_simple.py
git commit -m "Update Grafana tests for removed AI sidecars"
```

---

## Phase C — Final verification

### Task 11: Run full build + test + lint

**Files:** none.

- [ ] **Step 1: Clean artifacts**

Command: `make clean`
Expected: removes generated templates and caches.

- [ ] **Step 2: Build + lint**

Command: `./build.sh`
Expected: generates all templates including `generated-templates/lakerunner-07-maestro-service.yaml`. cfn-lint reports no errors. Warnings (W1020 "unnecessary Fn::Sub", W1030 "empty PublicSubnets") are OK per the repo convention.

- [ ] **Step 3: Run all tests**

Command: `make test`
Expected: every test in the default suite passes.

- [ ] **Step 4: Parameter / condition cross-template sanity**

Command: `pytest tests/test_parameter_validation.py tests/test_condition_validation.py -v`
Expected: PASS. These tests iterate known generators; if either adds hard-coded enumerations of templates, add the Maestro generator there (import path `lakerunner_maestro_service`). If enumeration is implicit, no change is needed.

If a failure surfaces a required hard-coded entry, fix the test file by adding the Maestro generator to the iteration list, not by loosening the assertion.

- [ ] **Step 5: Commit any test-iteration fixes (if applicable)**

```bash
git add -A
git status   # confirm only test files changed
git commit -m "Include Maestro generator in cross-template validation tests"
```

(Skip this commit if steps 3–4 were already green.)

- [ ] **Step 6: Final smoke test of generated YAML**

Command: `grep -c 'MaestroService\|MaestroAlb\|MaestroDbSecret' generated-templates/lakerunner-07-maestro-service.yaml`
Expected: a number greater than 3 (the template references these resource logical IDs several times across resources, outputs, and exports).

---

## Self-review

**Spec coverage:**

- Architecture (spec §Architecture) — Tasks 3–6 produce all listed resources.
- Parameters (spec §Parameters) — Task 2 defines all listed parameters with the specified types, defaults, and `AllowedValues`.
- DbInit container (spec §Container details) — Task 5.
- McpGateway container (spec §Container details) — Task 5.
- Maestro container including OIDC env wiring (spec §Container details, §Shared `MAESTRO_DB_*` env block) — Task 5.
- Secrets & IAM (spec §Secrets & IAM) — Task 3.
- Security Groups (spec §Security Groups) — Task 4.
- ALB + Listener + TG (spec §ALB) — Task 4.
- ECS Service (spec §ECS Service) — Task 6.
- Outputs (spec §Outputs) — Task 6.
- Build & Test integration (spec §Build & Test integration) — Tasks 1, 7, 11.
- Grafana cleanup (spec §Grafana stack cleanup) — Tasks 8–10.

**Placeholder scan:** none found. Every code step has a full code block. No "TODO"/"TBD"/"implement later".

**Type consistency:** container `DependsOn` uses the plain dict form `{"ContainerName": ..., "Condition": ...}` consistently with existing stacks (Grafana, Services). `ReadonlyRootFilesystem` is the correct troposphere property spelling (matches CFN `ReadonlyRootFilesystem`). Parameter names match between Task 2 (definition) and Task 5 (`Ref(OidcIssuerUrl)` etc.).

**Known sensitivity:** Task 7 enumerates existing cfn-lint warnings (W1020, W1030). If a new template emits a different warning, read the finding and decide if it's a genuine issue; the repo's convention is to fix errors and tolerate cosmetic warnings.

---

## Open alternatives the implementer may encounter

- **Helm chart vs. ECS entrypoint contract:** The chart runs MCP Gateway with `/app/entrypoint.sh mcp-gateway`. If the published image doesn't have that entrypoint script at that path, the `mcp-gateway` arg may need to be passed via `Command=["mcp-gateway"]` directly. Verify by running `docker run --rm --entrypoint= public.ecr.aws/cardinalhq.io/maestro:v0.23.0 ls /app/entrypoint.sh`. Update the `Command` field if needed and note the change in the commit message.
- **MCP `/healthz` path:** The chart assumes `/healthz` on the MCP port. If the image's health endpoint has moved (e.g., `/health`), update the `HealthCheck` command in Task 5 when the service fails to reach steady state on first deploy. This is a runtime-only check — the template itself lints cleanly either way.
- **If `tests/test_parameter_validation.py` or `tests/test_condition_validation.py` iterate a hard-coded template list:** Add `"lakerunner_maestro_service"` to that list. The tests exist to catch drift; don't suppress them.


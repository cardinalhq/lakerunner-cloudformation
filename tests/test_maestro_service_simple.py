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
        "dex": "ghcr.io/dexidp/dex:test",
        "dex_init": "public.ecr.aws/docker/library/busybox:test",
    },
    "task": {"cpu": 1024, "memory_mib": 2048},
    "ports": {
        "maestro": 4200,
        "mcp_gateway": 8080,
        "mcp_gateway_debug": 9090,
        "alb_listener": 80,
        "dex": 5556,
    },
    "dex": {
        "path_prefix": "/dex",
        "client_id": "maestro-ui",
    },
}


def _resolved_containers(containers, with_dex=False):
    """Project the ContainerDefinitions list as if HasDex==with_dex."""
    out = []
    for entry in containers:
        if isinstance(entry, dict) and "Fn::If" in entry:
            _cond, then_v, else_v = entry["Fn::If"]
            picked = then_v if with_dex else else_v
            if isinstance(picked, dict) and "Ref" in picked and \
                    picked["Ref"] == "AWS::NoValue":
                continue
            out.append(picked)
        else:
            out.append(entry)
    return out


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
        assert "Conditions" in template_dict
        assert "Metadata" in template_dict
        assert "Maestro" in template_dict["Description"]

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
            "DexEnabled", "DexAdminEmail", "DexAdminPasswordHash",
            "DexClientId", "DexPathPrefix", "DexImage", "DexInitImage",
        ]:
            assert name in parameters, f"missing parameter {name}"

        assert parameters["DexEnabled"]["AllowedValues"] == ["Yes", "No"]
        assert parameters["DexEnabled"]["Default"] == "No"
        assert parameters["DexAdminPasswordHash"]["NoEcho"] is True
        assert parameters["DexClientId"]["Default"] == "maestro-ui"
        assert parameters["DexPathPrefix"]["Default"] == "/dex"
        assert parameters["DexImage"]["Default"] == "ghcr.io/dexidp/dex:test"

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
        assert "HasDex" in conditions
        assert "MaestroBaseUrlBlank" in conditions

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
        assert "BedrockAccess" not in task_policies

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

    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_container_definitions_exist_on_task(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG
        from lakerunner_maestro_service import create_maestro_template

        resources = json.loads(create_maestro_template().to_json())["Resources"]
        assert "MaestroTaskDef" in resources
        containers = resources["MaestroTaskDef"]["Properties"]["ContainerDefinitions"]
        # With DEX disabled, the Fn::If wrappers resolve to AWS::NoValue and
        # the task holds only the always-on containers.
        resolved = _resolved_containers(containers, with_dex=False)
        names = [c["Name"] for c in resolved]
        assert names == ["DbInit", "McpGateway", "Maestro"]

        by_name = {c["Name"]: c for c in resolved}

        db_init = by_name["DbInit"]
        assert db_init["Essential"] is False
        db_init_envs = {e["Name"]: e["Value"] for e in db_init["Environment"]}
        assert db_init_envs["GRAFANA_DB_NAME"] == "maestro"
        assert db_init_envs["GRAFANA_DB_USER"] == "maestro"

        mcp = by_name["McpGateway"]
        assert mcp["Command"] == ["/app/entrypoint.sh", "mcp-gateway"]
        mcp_ports = [p["ContainerPort"] for p in mcp["PortMappings"]]
        assert 8080 in mcp_ports
        assert mcp["User"] == "65532"
        assert mcp["ReadonlyRootFilesystem"] is True
        mcp_deps = {d["ContainerName"]: d["Condition"] for d in mcp["DependsOn"]}
        assert mcp_deps["DbInit"] == "SUCCESS"

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

        maestro_env_names = set(m_envs.keys())
        for name in ["OIDC_ISSUER_URL", "OIDC_AUDIENCE", "OIDC_SUPERADMIN_GROUP",
                     "OIDC_JWKS_URL", "OIDC_SUPERADMIN_EMAILS",
                     "OIDC_TRUST_UNVERIFIED_EMAILS", "MAESTRO_BASE_URL"]:
            assert name in maestro_env_names, f"missing Maestro env {name}"

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

    # -----------------------
    # DEX-specific coverage
    # -----------------------

    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_dex_resources_gated_on_has_dex_condition(self, mock_load_config):
        """All DEX-specific resources must carry Condition: HasDex."""
        mock_load_config.return_value = MOCK_CONFIG
        from lakerunner_maestro_service import create_maestro_template

        resources = json.loads(create_maestro_template().to_json())["Resources"]

        for name in ["MaestroDexTg", "MaestroDexListenerRule",
                     "MaestroDexTaskFromAlbIngress",
                     "MaestroDexInitLogGroup", "MaestroDexLogGroup"]:
            assert name in resources, f"missing DEX resource {name}"
            assert resources[name].get("Condition") == "HasDex", \
                f"{name} missing Condition: HasDex"

    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_dex_target_group_routing(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG
        from lakerunner_maestro_service import create_maestro_template

        resources = json.loads(create_maestro_template().to_json())["Resources"]

        tg = resources["MaestroDexTg"]["Properties"]
        assert tg["Port"] == 5556
        assert tg["Protocol"] == "HTTP"
        assert tg["TargetType"] == "ip"
        # Health check path is a Sub of "${P}/healthz" — check the template.
        hc = tg["HealthCheckPath"]["Fn::Sub"]
        assert hc[0] == "${P}/healthz"
        assert hc[1] == {"P": {"Ref": "DexPathPrefix"}}

        rule = resources["MaestroDexListenerRule"]["Properties"]
        assert rule["Priority"] == 10
        assert rule["Actions"][0]["Type"] == "forward"
        assert rule["Actions"][0]["TargetGroupArn"] == {"Ref": "MaestroDexTg"}
        cond = rule["Conditions"][0]
        assert cond["Field"] == "path-pattern"
        # Two Subs: prefix itself and prefix/*.
        assert len(cond["Values"]) == 2

    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_dex_containers_and_volume_gated_by_has_dex(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG
        from lakerunner_maestro_service import create_maestro_template

        resources = json.loads(create_maestro_template().to_json())["Resources"]
        task_def = resources["MaestroTaskDef"]["Properties"]

        containers = task_def["ContainerDefinitions"]
        # With DEX on, the Fn::If branches resolve to the DEX container specs.
        on = _resolved_containers(containers, with_dex=True)
        on_names = [c["Name"] for c in on]
        assert "DexInit" in on_names
        assert "Dex" in on_names
        # With DEX off, they disappear.
        off = _resolved_containers(containers, with_dex=False)
        off_names = [c["Name"] for c in off]
        assert "DexInit" not in off_names
        assert "Dex" not in off_names

        by_name = {c["Name"]: c for c in on}
        dex_init = by_name["DexInit"]
        assert dex_init["Essential"] is False
        assert dex_init["EntryPoint"] == ["/bin/sh", "-c"]
        # Init must write to the shared config volume.
        init_mounts = {m["SourceVolume"]: m for m in dex_init["MountPoints"]}
        assert "dex-config" in init_mounts
        assert init_mounts["dex-config"]["ContainerPath"] == "/etc/dex"
        init_envs = {e["Name"] for e in dex_init["Environment"]}
        for required in ["DEX_ISSUER_URL", "DEX_REDIRECT_URI", "DEX_CLIENT_ID",
                         "DEX_PORT", "DEX_ADMIN_EMAIL", "DEX_ADMIN_HASH"]:
            assert required in init_envs, f"DexInit missing env {required}"

        dex = by_name["Dex"]
        assert dex["Essential"] is True
        assert dex["Command"] == ["dex", "serve", "/etc/dex/config.yaml"]
        dex_ports = [p["ContainerPort"] for p in dex["PortMappings"]]
        assert 5556 in dex_ports
        dex_mounts = {m["SourceVolume"]: m for m in dex["MountPoints"]}
        assert dex_mounts["dex-config"]["ReadOnly"] is True
        assert dex_mounts["tmp"]["ReadOnly"] is False
        dex_deps = {d["ContainerName"]: d["Condition"] for d in dex["DependsOn"]}
        assert dex_deps["DexInit"] == "SUCCESS"

        # Volumes list must include dex-config only when DEX is on.
        vols = task_def["Volumes"]
        # tmp always present
        assert {"Name": "tmp"} in vols
        # dex-config wrapped in Fn::If
        dex_vol_if = [v for v in vols if isinstance(v, dict) and "Fn::If" in v]
        assert len(dex_vol_if) == 1
        cond, then_v, else_v = dex_vol_if[0]["Fn::If"]
        assert cond == "HasDex"
        assert then_v == {"Name": "dex-config"}
        assert else_v == {"Ref": "AWS::NoValue"}

    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_maestro_env_switches_to_dex_values_under_has_dex(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG
        from lakerunner_maestro_service import create_maestro_template

        resources = json.loads(create_maestro_template().to_json())["Resources"]
        containers = resources["MaestroTaskDef"]["Properties"]["ContainerDefinitions"]
        maestro = next(c for c in _resolved_containers(containers)
                       if c.get("Name") == "Maestro")
        envs = {e["Name"]: e["Value"] for e in maestro["Environment"]}

        # OIDC_ISSUER_URL: Fn::If(HasDex, <dex issuer Sub>, Ref(OidcIssuerUrl))
        issuer = envs["OIDC_ISSUER_URL"]
        assert "Fn::If" in issuer
        cond, then_v, else_v = issuer["Fn::If"]
        assert cond == "HasDex"
        assert "Fn::Sub" in then_v
        assert else_v == {"Ref": "OidcIssuerUrl"}

        # OIDC_AUDIENCE: Fn::If(HasDex, Ref(DexClientId), Ref(OidcAudience))
        aud = envs["OIDC_AUDIENCE"]
        assert aud["Fn::If"][0] == "HasDex"
        assert aud["Fn::If"][1] == {"Ref": "DexClientId"}
        assert aud["Fn::If"][2] == {"Ref": "OidcAudience"}

        # OIDC_JWKS_URL: Fn::If(HasDex, <localhost Sub>, Ref(OidcJwksUrl))
        jwks = envs["OIDC_JWKS_URL"]
        assert jwks["Fn::If"][0] == "HasDex"
        assert "Fn::Sub" in jwks["Fn::If"][1]
        assert jwks["Fn::If"][2] == {"Ref": "OidcJwksUrl"}

        # MAESTRO_BASE_URL: Fn::If(HasDex, Fn::If(MaestroBaseUrlBlank, alb-sub,
        #                   Ref(MaestroBaseUrl)), Ref(MaestroBaseUrl))
        base = envs["MAESTRO_BASE_URL"]
        assert base["Fn::If"][0] == "HasDex"
        then_v = base["Fn::If"][1]
        assert "Fn::If" in then_v
        assert then_v["Fn::If"][0] == "MaestroBaseUrlBlank"
        # then-branch (blank) must Sub the ALB DNS
        assert "Fn::Sub" in then_v["Fn::If"][1]

    @patch('lakerunner_maestro_service.load_maestro_config')
    def test_service_registers_dex_target_group_conditionally(self, mock_load_config):
        mock_load_config.return_value = MOCK_CONFIG
        from lakerunner_maestro_service import create_maestro_template

        resources = json.loads(create_maestro_template().to_json())["Resources"]
        lbs = resources["MaestroService"]["Properties"]["LoadBalancers"]

        # First entry is always Maestro's TG.
        assert lbs[0]["ContainerName"] == "Maestro"
        assert lbs[0]["ContainerPort"] == 4200
        # Second entry is a Fn::If wrapping the DEX LB mapping.
        assert "Fn::If" in lbs[1]
        cond, then_v, else_v = lbs[1]["Fn::If"]
        assert cond == "HasDex"
        assert then_v["ContainerName"] == "Dex"
        assert then_v["ContainerPort"] == 5556
        assert then_v["TargetGroupArn"] == {"Ref": "MaestroDexTg"}
        assert else_v == {"Ref": "AWS::NoValue"}


if __name__ == '__main__':
    unittest.main()

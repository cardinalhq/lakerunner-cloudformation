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


if __name__ == '__main__':
    unittest.main()

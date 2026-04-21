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


if __name__ == '__main__':
    unittest.main()

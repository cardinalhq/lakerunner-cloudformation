"""The remote collector config must carry role_arn on every awss3 exporter."""

import yaml

from cardinal_cfn.defaults import load_remote_otel_default_config


def test_loads_nonempty_string():
    cfg = load_remote_otel_default_config()
    assert isinstance(cfg, str) and cfg.strip()


def test_every_awss3_exporter_has_role_arn():
    cfg = yaml.safe_load(load_remote_otel_default_config())
    exporters = cfg["exporters"]
    awss3 = {k: v for k, v in exporters.items() if k.startswith("awss3")}
    assert awss3, "expected at least one awss3 exporter"
    for name, ex in awss3.items():
        assert ex.get("role_arn") == "${env:LRDB_S3_ROLE_ARN}", (
            f"{name} missing role_arn assume-role hook"
        )


def test_keeps_health_check_extension():
    cfg = yaml.safe_load(load_remote_otel_default_config())
    assert "health_check" in cfg["extensions"]
    assert cfg["extensions"]["health_check"]["endpoint"] == "0.0.0.0:13133"

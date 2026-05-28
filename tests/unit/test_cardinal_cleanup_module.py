"""Generator-module sanity tests for cardinal_cleanup."""

from pathlib import Path

import pytest

from cardinal_cfn import cardinal_cleanup
from cardinal_cfn.cleanup_script import SCRIPT


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GENERATED = REPO_ROOT / "generated-templates" / "cardinal-cleanup.yaml"


def test_template_builds():
    """The generator must produce a valid troposphere Template."""
    t = cardinal_cleanup.build()
    yaml_text = t.to_yaml()
    assert "AWS::ECS::TaskDefinition" in yaml_text
    assert "AWS::Logs::LogGroup" in yaml_text
    # The full shell body is embedded literally; check distinctive markers
    # from each phase survive YAML serialization.
    assert "drain_services" in yaml_text
    assert "delete_lakerunner_stack" in yaml_text
    assert "empty_ingest_bucket" in yaml_text
    assert "delete_infra_stack" in yaml_text
    assert "delete_secrets" in yaml_text
    assert "self_delete" in yaml_text


def test_template_under_size_limit():
    """CFN's CreateStack template-body limit is 1 MiB; warn at 800 KiB."""
    yaml_size = len(cardinal_cleanup.build().to_yaml().encode("utf-8"))
    assert yaml_size < 800_000, (
        f"template is {yaml_size} bytes; close to CFN 1 MiB limit"
    )


def test_no_iam_resources_in_template():
    t = cardinal_cleanup.build()
    for name, resource in t.resources.items():
        assert not resource.resource_type.startswith("AWS::IAM::"), (
            f"resource {name} of type {resource.resource_type} "
            f"violates 'no IAM' rule"
        )


def test_generated_file_matches_module():
    """`make build` is the source of truth; if the file drifts, regenerate."""
    if not GENERATED.exists():
        pytest.skip("run `make build` first")
    rebuilt = cardinal_cleanup.build().to_yaml()
    on_disk = GENERATED.read_text()
    assert rebuilt == on_disk, (
        "generated-templates/cardinal-cleanup.yaml is stale; "
        "run `make build` to regenerate."
    )


def test_script_is_referenced():
    """The generator must embed SCRIPT, not a placeholder."""
    yaml_text = cardinal_cleanup.build().to_yaml()
    first_line_of_body = SCRIPT.strip().splitlines()[0]
    assert first_line_of_body in yaml_text

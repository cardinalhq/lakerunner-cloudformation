"""Tests for the standalone upgrade-lakerunner.sh script.

The script is shell + jq + AWS CLI. To keep its parameter-resolution logic
testable without AWS or shellcheck-style guesswork, the script exposes two
internal-only flags used by these tests:

- --internal-resolve-params <new-template-params.json> <current-stack-params.json>
- --internal-classify-changeset-status <status> <status-reason>

Both run pure data transforms with no AWS calls and no side effects.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "upgrade-lakerunner.sh"


def _write(path: Path, payload):
    path.write_text(json.dumps(payload))


def _run_resolve(tmp_path, new_template_params, current_stack_params, *extra_args):
    new_path = tmp_path / "new.json"
    cur_path = tmp_path / "cur.json"
    _write(new_path, new_template_params)
    _write(cur_path, current_stack_params)
    result = subprocess.run(
        [
            "sh",
            str(SCRIPT),
            "--internal-resolve-params",
            str(new_path),
            str(cur_path),
            *extra_args,
        ],
        capture_output=True,
        text=True,
    )
    return result


def _by_key(parameters):
    return {p["ParameterKey"]: p for p in parameters}


def _require_jq():
    if shutil.which("jq") is None:
        pytest.skip("jq not installed on this runner")


@pytest.fixture(autouse=True)
def _script_exists():
    if not SCRIPT.exists():
        pytest.fail(
            f"Script missing: {SCRIPT}. Implement it before running these tests."
        )


def test_image_param_takes_new_template_default_when_refresh_on(tmp_path):
    _require_jq()
    new_template = [
        {
            "ParameterKey": "LakerunnerImage",
            "DefaultValue": "public.ecr.aws/cardinalhq/lakerunner:1.20.0",
            "ParameterType": "String",
        }
    ]
    current = [
        {
            "ParameterKey": "LakerunnerImage",
            "ParameterValue": "public.ecr.aws/cardinalhq/lakerunner:1.19.2",
        }
    ]
    result = _run_resolve(tmp_path, new_template, current)
    assert result.returncode == 0, result.stderr
    out = _by_key(json.loads(result.stdout))
    assert out["LakerunnerImage"] == {
        "ParameterKey": "LakerunnerImage",
        "ParameterValue": "public.ecr.aws/cardinalhq/lakerunner:1.20.0",
    }


def test_image_param_carries_forward_when_refresh_off(tmp_path):
    _require_jq()
    new_template = [
        {
            "ParameterKey": "LakerunnerImage",
            "DefaultValue": "public.ecr.aws/cardinalhq/lakerunner:1.20.0",
            "ParameterType": "String",
        }
    ]
    current = [
        {
            "ParameterKey": "LakerunnerImage",
            "ParameterValue": "public.ecr.aws/cardinalhq/lakerunner:1.19.2",
        }
    ]
    result = _run_resolve(
        tmp_path, new_template, current, "--no-refresh-image-defaults"
    )
    assert result.returncode == 0, result.stderr
    out = _by_key(json.loads(result.stdout))
    assert out["LakerunnerImage"] == {
        "ParameterKey": "LakerunnerImage",
        "UsePreviousValue": True,
    }


def test_non_image_param_carries_forward(tmp_path):
    _require_jq()
    new_template = [
        {"ParameterKey": "VpcId", "ParameterType": "AWS::EC2::VPC::Id"},
    ]
    current = [
        {"ParameterKey": "VpcId", "ParameterValue": "vpc-0abc123"},
    ]
    result = _run_resolve(tmp_path, new_template, current)
    assert result.returncode == 0, result.stderr
    out = _by_key(json.loads(result.stdout))
    assert out["VpcId"] == {"ParameterKey": "VpcId", "UsePreviousValue": True}


def test_template_base_url_takes_explicit_override_not_carry_forward(tmp_path):
    """TemplateBaseUrl drives nested-stack TemplateURL Subs and MUST track the
    upgrade target's version path, even if the running stack has an older
    value. Carrying it forward would point the new root at the OLD nested
    templates and fail in confusing ways."""
    _require_jq()
    new_template = [
        {
            "ParameterKey": "TemplateBaseUrl",
            "DefaultValue": "https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/v9.9.9/cardinal-lakerunner/",
            "ParameterType": "String",
        }
    ]
    current = [
        {
            "ParameterKey": "TemplateBaseUrl",
            "ParameterValue": "https://cardinal-cfn.s3.us-east-2.amazonaws.com/lakerunner/v0.0.29/cardinal-lakerunner/",
        }
    ]
    explicit = "https://my-mirror.example.com/lakerunner/v9.9.9/cardinal-lakerunner/"
    result = _run_resolve(
        tmp_path, new_template, current,
        "--internal-template-base-url", explicit,
    )
    assert result.returncode == 0, result.stderr
    out = _by_key(json.loads(result.stdout))
    assert out["TemplateBaseUrl"] == {
        "ParameterKey": "TemplateBaseUrl",
        "ParameterValue": explicit,
    }


def test_template_base_url_falls_back_to_template_default_when_no_override(tmp_path):
    """If the script doesn't pass an explicit TemplateBaseUrl (e.g., a test
    invoking resolve_params directly), the carry-forward rule still applies
    when the param exists in the current stack."""
    _require_jq()
    new_template = [
        {
            "ParameterKey": "TemplateBaseUrl",
            "DefaultValue": "https://example.com/v9.9.9/",
            "ParameterType": "String",
        }
    ]
    current = [
        {"ParameterKey": "TemplateBaseUrl", "ParameterValue": "https://example.com/v0.0.29/"},
    ]
    result = _run_resolve(tmp_path, new_template, current)
    assert result.returncode == 0, result.stderr
    out = _by_key(json.loads(result.stdout))
    assert out["TemplateBaseUrl"] == {
        "ParameterKey": "TemplateBaseUrl",
        "UsePreviousValue": True,
    }


def test_new_param_takes_template_default(tmp_path):
    _require_jq()
    new_template = [
        {
            "ParameterKey": "NewKnob",
            "DefaultValue": "default-value",
            "ParameterType": "String",
        }
    ]
    current = []  # parameter didn't exist in the previous stack
    result = _run_resolve(tmp_path, new_template, current)
    assert result.returncode == 0, result.stderr
    out = _by_key(json.loads(result.stdout))
    assert out["NewKnob"] == {
        "ParameterKey": "NewKnob",
        "ParameterValue": "default-value",
    }


def test_required_new_param_with_no_default_fails_loudly(tmp_path):
    _require_jq()
    new_template = [
        {"ParameterKey": "NewRequired", "ParameterType": "String"},
    ]
    current = []
    result = _run_resolve(tmp_path, new_template, current)
    assert result.returncode == 2, (
        f"expected exit 2, got {result.returncode}: {result.stderr}"
    )
    assert "NewRequired" in result.stderr


def test_image_param_with_no_default_carries_forward(tmp_path):
    _require_jq()
    new_template = [
        {"ParameterKey": "LakerunnerImage", "ParameterType": "String"},
    ]
    current = [
        {
            "ParameterKey": "LakerunnerImage",
            "ParameterValue": "private.example.com/lakerunner:1.19.2",
        }
    ]
    result = _run_resolve(tmp_path, new_template, current)
    assert result.returncode == 0, result.stderr
    out = _by_key(json.loads(result.stdout))
    assert out["LakerunnerImage"] == {
        "ParameterKey": "LakerunnerImage",
        "UsePreviousValue": True,
    }


def test_full_template_mix(tmp_path):
    _require_jq()
    new_template = [
        {
            "ParameterKey": "LakerunnerImage",
            "DefaultValue": "public.ecr.aws/cardinalhq/lakerunner:1.20.0",
            "ParameterType": "String",
        },
        {
            "ParameterKey": "MaestroImage",
            "DefaultValue": "public.ecr.aws/cardinalhq/maestro:0.5.0",
            "ParameterType": "String",
        },
        {"ParameterKey": "VpcId", "ParameterType": "AWS::EC2::VPC::Id"},
        {
            "ParameterKey": "PrivateSubnets",
            "ParameterType": "CommaDelimitedList",
        },
        {
            "ParameterKey": "NewKnob",
            "DefaultValue": "off",
            "ParameterType": "String",
        },
    ]
    current = [
        {
            "ParameterKey": "LakerunnerImage",
            "ParameterValue": "public.ecr.aws/cardinalhq/lakerunner:1.19.2",
        },
        {
            "ParameterKey": "MaestroImage",
            "ParameterValue": "public.ecr.aws/cardinalhq/maestro:0.4.0",
        },
        {"ParameterKey": "VpcId", "ParameterValue": "vpc-0abc123"},
        {
            "ParameterKey": "PrivateSubnets",
            "ParameterValue": "subnet-1,subnet-2,subnet-3",
        },
    ]
    result = _run_resolve(tmp_path, new_template, current)
    assert result.returncode == 0, result.stderr
    out = _by_key(json.loads(result.stdout))
    assert out["LakerunnerImage"]["ParameterValue"].endswith("1.20.0")
    assert out["MaestroImage"]["ParameterValue"].endswith("0.5.0")
    assert out["VpcId"] == {"ParameterKey": "VpcId", "UsePreviousValue": True}
    assert out["PrivateSubnets"] == {
        "ParameterKey": "PrivateSubnets",
        "UsePreviousValue": True,
    }
    assert out["NewKnob"] == {"ParameterKey": "NewKnob", "ParameterValue": "off"}


def _classify(status, reason):
    return subprocess.run(
        [
            "sh",
            str(SCRIPT),
            "--internal-classify-changeset-status",
            status,
            reason,
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "status,reason",
    [
        ("FAILED", "The submitted information didn't contain changes."),
        ("FAILED", "No updates are to be performed."),
        ("FAILED", "didn't contain changes"),
    ],
)
def test_classify_noop_phrasings(status, reason):
    result = _classify(status, reason)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "noop"


def test_classify_create_complete_is_success():
    result = _classify("CREATE_COMPLETE", "")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "success"


def test_classify_real_failure_is_failure():
    result = _classify(
        "FAILED",
        "Parameters: [BogusParam] must have values",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "failure"

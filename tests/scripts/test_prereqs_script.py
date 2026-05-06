"""Tests that verify the rendered cardinal-prereqs.sh is well-formed."""

import shutil
import subprocess
from pathlib import Path

import pytest

from cardinal_cfn.prereqs.render import render_prereqs_script


def test_rendered_script_passes_shellcheck(tmp_path: Path):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    script = tmp_path / "cardinal-prereqs.sh"
    script.write_text(render_prereqs_script())
    result = subprocess.run(
        ["shellcheck", "-s", "sh", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_rendered_script_has_required_helpers():
    out = render_prereqs_script()
    for fn in [
        "ensure_role",
        "ensure_inline_policy",
        "ensure_managed_policy_attached",
        "ensure_sg",
        "ensure_ingress_self",
        "ensure_ingress_sg",
        "ensure_ingress_cidr",
    ]:
        assert f"{fn}()" in out, f"missing helper {fn}"


def test_rendered_script_uses_temporary_directory_with_cleanup():
    out = render_prereqs_script()
    assert "TMP_DIR=$(mktemp -d)" in out
    assert "trap 'rm -rf \"$TMP_DIR\"'" in out


def test_rendered_script_validates_required_args():
    out = render_prereqs_script()
    assert '--region required' in out
    assert '--vpc-id required' in out


def test_rendered_script_preflight_checks_tools():
    out = render_prereqs_script()
    assert "for tool in aws jq" in out

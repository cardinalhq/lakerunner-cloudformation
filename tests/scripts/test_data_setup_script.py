"""Tests that verify the rendered cardinal-data-setup.sh is well-formed."""

import shutil
import subprocess
from pathlib import Path

import pytest

from cardinal_cfn.data_setup.render import render_data_setup_script


def test_data_setup_script_passes_shellcheck(tmp_path: Path):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    script = tmp_path / "cardinal-data-setup.sh"
    script.write_text(render_data_setup_script())
    result = subprocess.run(
        ["shellcheck", "-s", "sh", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_script_uses_temp_directory_with_cleanup():
    out = render_data_setup_script()
    assert "TMP_DIR=$(mktemp -d)" in out
    assert "trap 'rm -rf \"$TMP_DIR\"'" in out


def test_script_preflights_required_tools():
    out = render_data_setup_script()
    assert "for tool in aws jq openssl" in out


def test_script_creates_storage_before_database():
    # Storage is fast; database is slow. If storage fails we want to know
    # before kicking off the 10+ minute RDS create. Compare call sites
    # (with arguments) rather than function-definition lines.
    out = render_data_setup_script()
    storage_idx = out.find('QUEUE_URL=$(ensure_sqs_queue "$QUEUE_NAME")')
    db_idx = out.find("ensure_db_subnet_group cardinal-db-subnet-group")
    assert 0 < storage_idx < db_idx

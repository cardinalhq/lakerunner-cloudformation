"""Offline tests for the satellite-config composition logic in deploy-lakerunner-services.sh.

Exercises the jq compose-and-validate path (synthesize central collector + merge
operator satellites) without any AWS calls.  The jq logic is exercised via a small
inline shell fragment that mirrors the driver's compose block verbatim.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "deploy-lakerunner-services.sh"

ORG = "aaaaaaaa-0000-4000-8000-000000000001"
OTHER_ORG = "bbbbbbbb-0000-4000-8000-000000000002"
BUCKET = "cardinal-raw-test"
QUEUE = "https://sqs.us-east-1.amazonaws.com/123456789012/raw"
REGION = "us-east-1"
ROLE = "arn:aws:iam::123456789012:role/satellite-access"
COLL = "lakerunner"

# The compose+validate shell logic, parameterised by env vars.  This mirrors
# the block in scripts-src/parts/deploy-lakerunner-services.sh so the test
# stays in sync with the real code.  Variables consumed:
#   ORGANIZATION_ID, CENTRAL_COLL, RAW_BUCKET, QUEUE_URL, REGION, ROLE_ARN,
#   OPERATOR_JSON
_COMPOSE_SH = r"""
set -e
other_orgs=$(printf '%s' "$OPERATOR_JSON" | jq -r --arg org "$ORGANIZATION_ID" \
    '[(.organizations // {} | keys[]) | select(. != $org)] | join(", ")' 2>&1) \
    || { echo "PARSE_ERROR: $other_orgs" >&2; exit 2; }
if [ -n "$other_orgs" ]; then
    echo "OTHER_ORG_ERROR: SATELLITE_CONFIG may only define satellites under the install org $ORGANIZATION_ID; found other org key(s): $other_orgs. This is a single-install deployment -- all satellite raw buckets feed this org." >&2
    exit 2
fi

if [ -n "$ROLE_ARN" ]; then
    central_json=$(jq -n \
        --arg org "$ORGANIZATION_ID" --arg coll "$CENTRAL_COLL" \
        --arg bucket "$RAW_BUCKET" --arg sqs "$QUEUE_URL" \
        --arg region "$REGION" --arg role "$ROLE_ARN" \
        '{organizations: {($org): {collectors: {($coll): {bucket:$bucket, sqsurl:$sqs, region:$region, mode:"normal", role:$role}}}}}')
else
    central_json=$(jq -n \
        --arg org "$ORGANIZATION_ID" --arg coll "$CENTRAL_COLL" \
        --arg bucket "$RAW_BUCKET" --arg sqs "$QUEUE_URL" \
        --arg region "$REGION" \
        '{organizations: {($org): {collectors: {($coll): {bucket:$bucket, sqsurl:$sqs, region:$region, mode:"normal"}}}}}')
fi

satellites_json=$(printf '%s' "$OPERATOR_JSON" | jq --argjson c "$central_json" --arg coll "$CENTRAL_COLL" '
    . as $op
    | ($c.organizations | keys[0]) as $org
    | if (($op.organizations[$org].collectors // {}) | has($coll)) then
        error("SATELLITE_CONFIG collector name \"\($coll)\" collides with the auto-synthesized central collector for org \($org); choose a different collector name")
      else . end
    | (($op.organizations[$org].collectors // {}) | to_entries
       | map(select((.value.mode // "normal") == "normal")) | length) as $op_normals
    | if $op_normals > 0 then
        error("operator SATELLITE_CONFIG must not declare a normal collector for the install org \($org)")
      else . end
    | reduce ($op.organizations | to_entries[]) as $entry (
        $c;
        .organizations[$entry.key].collectors = (
            (.organizations[$entry.key].collectors // {}) + $entry.value.collectors
        )
      )
' 2>&1) || { echo "COMPOSE_ERROR: $satellites_json" >&2; exit 2; }

bad=$(printf '%s' "$satellites_json" | jq -r '
    [.organizations | to_entries[] |
        {org:.key, normals: ([.value.collectors[] | select((.mode // "normal") == "normal")] | length)}
        | select(.normals != 1)
        | "\(.org):\(.normals)"] | join(", ")')
[ -n "$bad" ] && { echo "VALIDATE_ERROR: orgs without exactly one normal collector: $bad" >&2; exit 2; }

printf '%s' "$satellites_json"
"""


@pytest.fixture(autouse=True)
def _need_jq():
    if shutil.which("jq") is None:
        pytest.skip("jq not installed on this runner")
    if not SCRIPT.exists():
        pytest.skip(f"script not found: {SCRIPT}")


def _compose(org, coll, bucket, queue, region, role_arn, operator_json=None):
    """Run the compose+validate shell logic.

    Returns (ok: bool, result: str) where result is the JSON string on success
    or the error message on failure.
    """
    if operator_json is None:
        operator_json = json.dumps({"organizations": {}})

    env = {
        "ORGANIZATION_ID": org,
        "CENTRAL_COLL": coll,
        "RAW_BUCKET": bucket,
        "QUEUE_URL": queue,
        "REGION": region,
        "ROLE_ARN": role_arn,
        "OPERATOR_JSON": operator_json,
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    result = subprocess.run(
        ["sh", "-c", _COMPOSE_SH],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, result.stdout
    return False, (result.stderr or result.stdout).strip()


def _j(text):
    return json.loads(text)


# ---------------------------------------------------------------------------
# T1: central only, no operator config, no role
# ---------------------------------------------------------------------------
def test_central_only_no_role():
    ok, out = _compose(ORG, COLL, BUCKET, QUEUE, REGION, "")
    assert ok, f"expected success, got: {out}"
    data = _j(out)
    collectors = data["organizations"][ORG]["collectors"]
    assert COLL in collectors
    c = collectors[COLL]
    assert c["bucket"] == BUCKET
    assert c["sqsurl"] == QUEUE
    assert c["region"] == REGION
    assert c["mode"] == "normal"
    assert "role" not in c, "role must be absent when role_arn is empty"
    normals = [v for v in collectors.values() if v.get("mode", "normal") == "normal"]
    assert len(normals) == 1


# ---------------------------------------------------------------------------
# T2: central + one read-only satellite under the same org, with role
# ---------------------------------------------------------------------------
def test_central_plus_read_only_satellite_same_org():
    operator = {
        "organizations": {
            ORG: {
                "collectors": {
                    "satellite-eu": {
                        "mode": "read-only",
                        "bucket": "eu-bucket",
                        "sqsurl": "https://sqs.eu-west-1.amazonaws.com/222/eu",
                        "region": "eu-west-1",
                        "role": "arn:aws:iam::222:role/sat",
                    }
                }
            }
        }
    }
    ok, out = _compose(ORG, COLL, BUCKET, QUEUE, REGION, ROLE, json.dumps(operator))
    assert ok, f"expected success, got: {out}"
    data = _j(out)
    collectors = data["organizations"][ORG]["collectors"]
    assert COLL in collectors, "central lakerunner collector must be present"
    assert "satellite-eu" in collectors, "satellite-eu collector must be present"
    assert len(collectors) == 2
    normals = [v for v in collectors.values() if v.get("mode", "normal") == "normal"]
    assert len(normals) == 1
    assert collectors[COLL].get("role") == ROLE


# ---------------------------------------------------------------------------
# T3: operator declares a normal collector for the install org -> REJECT
# ---------------------------------------------------------------------------
def test_operator_normal_for_install_org_rejected():
    operator = {
        "organizations": {
            ORG: {
                "collectors": {
                    "bad-normal": {
                        "mode": "normal",
                        "bucket": "x",
                        "sqsurl": "y",
                        "region": "us-east-1",
                    }
                }
            }
        }
    }
    ok, out = _compose(ORG, COLL, BUCKET, QUEUE, REGION, "", json.dumps(operator))
    assert not ok, "expected rejection when operator declares normal for install org"
    assert (
        "must not declare a normal" in out.lower()
        or "compose_error" in out.lower()
    ), f"unexpected error message: {out}"


# ---------------------------------------------------------------------------
# T3b: operator collector keyed the same as the central collector name (but
# non-normal mode) -> rejected for collision (the `+` merge would otherwise
# silently overwrite the synthesized central, surfacing as a cryptic 0-normal).
# ---------------------------------------------------------------------------
def test_collector_name_collision_rejected():
    operator = {
        "organizations": {
            ORG: {
                "collectors": {
                    COLL: {  # same key as the central collector name
                        "mode": "read-only",
                        "bucket": "x",
                        "sqsurl": "y",
                        "region": "us-east-1",
                    }
                }
            }
        }
    }
    ok, out = _compose(ORG, COLL, BUCKET, QUEUE, REGION, "", json.dumps(operator))
    assert not ok, "expected rejection for collector-name collision with central"
    assert "collides with the auto-synthesized central" in out.lower(), (
        f"unexpected error message: {out}"
    )
    assert COLL in out, "collision message should name the colliding collector"


# ---------------------------------------------------------------------------
# T4: single-install -- any non-install org key is rejected up front
# ---------------------------------------------------------------------------
def test_non_install_org_rejected():
    operator = {
        "organizations": {
            OTHER_ORG: {
                "collectors": {
                    "central-b": {
                        "mode": "normal",
                        "bucket": "b-bucket",
                        "sqsurl": "https://sqs.us-west-2.amazonaws.com/333/b",
                        "region": "us-west-2",
                    }
                }
            }
        }
    }
    ok, out = _compose(ORG, COLL, BUCKET, QUEUE, REGION, "", json.dumps(operator))
    assert not ok, "expected rejection for a non-install org key"
    assert (
        "single-install" in out.lower()
        or "other org key" in out.lower()
        or "other_org_error" in out.lower()
    ), f"unexpected error message: {out}"
    assert OTHER_ORG in out, "rejection should name the offending org key"


# ---------------------------------------------------------------------------
# T4b: a read-only satellite under a NON-install org is also rejected up front
# (this would otherwise surface as a cryptic 0-normal validation error).
# ---------------------------------------------------------------------------
def test_non_install_org_read_only_rejected():
    operator = {
        "organizations": {
            "cccccccc-0000-4000-8000-000000000003": {
                "collectors": {
                    "ro": {
                        "mode": "read-only",
                        "bucket": "c",
                        "sqsurl": "https://sqs.us-east-1.amazonaws.com/444/c",
                        "region": "us-east-1",
                    }
                }
            }
        }
    }
    ok, out = _compose(ORG, COLL, BUCKET, QUEUE, REGION, "", json.dumps(operator))
    assert not ok, "expected rejection for a non-install org key"
    assert (
        "single-install" in out.lower() or "other_org_error" in out.lower()
    ), f"unexpected error message: {out}"


# ---------------------------------------------------------------------------
# T5: install-org-only read-only config still passes (the happy path).
# ---------------------------------------------------------------------------
def test_install_org_only_read_only_passes():
    operator = {
        "organizations": {
            ORG: {
                "collectors": {
                    "ro": {
                        "mode": "read-only",
                        "bucket": "c",
                        "sqsurl": "https://sqs.us-east-1.amazonaws.com/444/c",
                        "region": "us-east-1",
                    }
                }
            }
        }
    }
    ok, out = _compose(ORG, COLL, BUCKET, QUEUE, REGION, "", json.dumps(operator))
    assert ok, f"expected success, got: {out}"
    data = _j(out)
    collectors = data["organizations"][ORG]["collectors"]
    assert COLL in collectors and "ro" in collectors
    normals = [v for v in collectors.values() if v.get("mode", "normal") == "normal"]
    assert len(normals) == 1

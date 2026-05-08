"""Naming and tag conventions for Cardinal resources.

Two helper sets coexist:

- ``cardinal_tags(component=, role=)`` -- used by the lakerunner nested
  children. Carries an ``InstallIdShort`` Sub in the Name tag so per-install
  resources are visually distinguishable in the AWS console.
- ``cardinal_tags_v2(component=, managed_by=)`` -- used by the data-setup
  Lambda's CFN wrapper and by callers that want explicit ``managed_by``
  attribution (which layer owns the resource).

``LakerunnerComponent``, ``log_group_name``, ``name_tag``, ``secret_name``,
and ``ssm_param_name`` are constants/helpers for the bare ``cardinal-*`` /
``/cardinal/*`` naming contract documented in
``docs/superpowers/specs/2026-05-06-cardinal-cfn-prereqs-split-design.md``.
"""

from __future__ import annotations

from enum import Enum

from troposphere import Sub, Tags


# ---------------------------------------------------------------------------
# Legacy (used by src/cardinal_cfn/children/* and src/cardinal_cfn/root.py)
# ---------------------------------------------------------------------------

CARDINAL_PROJECT_TAG = "cardinal"
MANAGED_BY_TAG = "cardinal-cfn"


def cardinal_tags(*, component: str, role: str) -> Tags:
    """Legacy tag set. Carries an ``InstallIdShort`` Sub in the Name tag."""

    return Tags(
        Name=Sub(f"cardinal-{role}-${{InstallIdShort}}"),
        Project=CARDINAL_PROJECT_TAG,
        Component=component,
        ManagedBy=MANAGED_BY_TAG,
    )


# ---------------------------------------------------------------------------
# New shape (used by src/cardinal_cfn/{prereqs,data_setup}/* and the
# to-be-written src/cardinal_cfn/{app,lakerunner}/* packages)
# ---------------------------------------------------------------------------

PROJECT = "cardinal"
APPLICATION = "cardinal-lakerunner"


class LakerunnerComponent(str, Enum):
    """Service identities -- physical-name suffixes and tag values."""

    QUERY_API = "query-api"
    QUERY_WORKER = "query-worker"
    PROCESS_LOGS = "process-logs"
    PROCESS_METRICS = "process-metrics"
    PROCESS_TRACES = "process-traces"
    PUBSUB_SQS = "pubsub-sqs"
    SWEEPER = "sweeper"
    MONITORING = "monitoring"
    ADMIN_API = "admin-api"
    ALERT_EVALUATOR = "alert-evaluator"
    OTEL_COLLECTOR = "otel-collector"
    MAESTRO = "maestro"
    DEX = "dex"
    MIGRATOR = "migrator"


def cardinal_tags_v2(*, component: str, managed_by: str, install_version: str | None = None) -> Tags:
    """New tag set. ``managed_by`` is required; no InstallId."""

    if not managed_by:
        raise ValueError("managed_by is required")

    items: dict[str, str] = {
        "Application": APPLICATION,
        "Component": component,
        "ManagedBy": managed_by,
        "Name": f"cardinal-{component}",
    }
    if install_version:
        items["cardinal:install-version"] = install_version
    return Tags(**items)


def name_tag(*, role: str) -> str:
    """Plain string for resources that take a ``Name=`` arg directly."""

    return f"cardinal-{role}"


def secret_name(*, purpose: str) -> str:
    """Explicit Secrets Manager secret name. Suffix appended by AWS."""

    return f"cardinal-{purpose}"


def ssm_param_name(*, key: str) -> str:
    """Explicit SSM parameter name. Leading slash required."""

    return f"/cardinal/{key}"


def log_group_name(*, service: str) -> str:
    """Per-service CloudWatch log group name."""

    return f"/cardinal/{service}"

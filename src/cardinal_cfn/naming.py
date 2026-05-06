"""Naming and tag conventions for Cardinal resources.

This module is in transition between two shapes:

- ``cardinal_tags(component=, role=)`` -- the legacy form used by the
  current nested-children CFN templates. Kept while those templates still
  exist; will retire alongside ``children/`` in Phase 2 of the refactor.
- ``cardinal_tags_v2(component=, managed_by=)`` -- the new form, used by
  the prereqs and data-setup shell-script generators and by the
  to-be-written app/lakerunner CFN stacks. ``managed_by`` records which
  layer (script or stack) owns the resource so the customer can audit.

The new ``LakerunnerComponent`` enum, ``log_group_name``, the
``InstallId``-free ``name_tag`` / ``secret_name`` / ``ssm_param_name``
helpers, and the ``PROJECT`` / ``APPLICATION`` constants all belong to
the new shape and are unused by legacy code.
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

"""Naming and tag conventions for Cardinal resources.

One install per AWS account+region. No InstallId: physical names use
plain ``cardinal-*`` / ``cardinal/*`` prefixes. The same tag set is
applied identically by the shell-script generators and the CFN
template generators; differing only in the ``ManagedBy`` value.
"""

from __future__ import annotations

from enum import Enum

from troposphere import Tags


PROJECT = "cardinal"
APPLICATION = "cardinal-lakerunner"


class LakerunnerComponent(str, Enum):
    """Service identities -- used as physical-name suffixes and tag values."""

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


def cardinal_tags(*, component: str, managed_by: str, install_version: str | None = None) -> Tags:
    """CFN ``Tags`` value carrying the standard tag set.

    ``managed_by`` is required and identifies which layer owns the
    resource. ``install_version`` is the lakerunner template version
    that last touched the resource; when omitted callers can add it
    later via a separate Tags merge.
    """

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

"""Naming and tag conventions for Cardinal resources.

``cardinal_tags()`` is the single source of the common tag set every
Cardinal-created resource carries:

    Name         cardinal-<role or component>[-<InstallIdShort>]
    Project      cardinal
    Application  cardinal-lakerunner
    Component    <what the resource is for>
    ManagedBy    which layer owns it (cardinal-cfn-<layer>)

Nested children pass ``role=`` so their Name tag carries the install id and
per-install resources stay distinguishable in the console; root stacks pass
``component=`` alone. The deploy drivers additionally set Project /
Application / ManagedBy as *stack* tags, which CloudFormation propagates to
resource types the generators cannot tag directly (ALB listeners and listener
rules, Cloud Map, IAM server certificates).

``LakerunnerComponent``, ``log_group_name``, ``name_tag``, ``secret_name``,
and ``ssm_param_name`` are constants/helpers for the bare ``cardinal-*`` /
``/cardinal/*`` naming contract documented in
``docs/superpowers/specs/2026-05-06-cardinal-cfn-prereqs-split-design.md``.
"""

from __future__ import annotations

from enum import Enum

from troposphere import Sub, Tags


PROJECT = "cardinal"
APPLICATION = "cardinal-lakerunner"
MANAGED_BY_TAG = "cardinal-cfn"
# Back-compat alias; PROJECT is the name to use.
CARDINAL_PROJECT_TAG = PROJECT


def cardinal_tags(
    *,
    component: str,
    role: str | None = None,
    managed_by: str = MANAGED_BY_TAG,
) -> Tags:
    """The common tag set. ``role`` (children) puts InstallIdShort in Name."""

    name = Sub(f"cardinal-{role}-${{InstallIdShort}}") if role else f"cardinal-{component}"
    return Tags(
        Name=name,
        Project=PROJECT,
        Application=APPLICATION,
        Component=component,
        ManagedBy=managed_by,
    )


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

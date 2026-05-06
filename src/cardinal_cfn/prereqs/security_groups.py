"""Security-group specifications -- pure data, used by the renderer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IngressRule:
    description: str
    protocol: str            # "tcp", "udp", or "-1"
    from_port: int
    to_port: int
    source_kind: str         # "self", "sg", "cidr"
    source_value: str        # "" for self, sg name for sg, cidr for cidr


@dataclass(frozen=True)
class SgSpec:
    name: str
    description: str
    ingress: tuple[IngressRule, ...] = field(default_factory=tuple)


def expected_sg_specs() -> list[SgSpec]:
    return [
        SgSpec(
            name="cardinal-task-sg",
            description="Cardinal ECS tasks; allows intra-cluster traffic and ALB ingress",
            ingress=(
                IngressRule("self all-tcp", "tcp", 0, 65535, "self", ""),
                IngressRule("from ALB", "tcp", 0, 65535, "sg", "cardinal-alb-sg"),
            ),
        ),
        SgSpec(
            name="cardinal-alb-sg",
            description="Cardinal internal ALB",
            ingress=(
                IngressRule("https", "tcp", 443, 443, "cidr", "0.0.0.0/0"),
                IngressRule("admin-https", "tcp", 9443, 9443, "cidr", "0.0.0.0/0"),
            ),
        ),
        SgSpec(
            name="cardinal-db-sg",
            description="Cardinal RDS Postgres",
            ingress=(
                IngressRule("postgres from tasks", "tcp", 5432, 5432, "sg", "cardinal-task-sg"),
            ),
        ),
    ]

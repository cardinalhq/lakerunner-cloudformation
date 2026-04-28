"""Shared CloudFormation parameter helpers."""

from troposphere import Parameter, Template


def add_install_id_parameters(t: Template) -> None:
    """Declare InstallIdShort and InstallIdLong as String parameters.

    Used by every nested child template. The root computes the values once
    and threads them in.
    """
    t.add_parameter(
        Parameter(
            "InstallIdShort",
            Type="String",
            Description="Short per-install identifier (8 hex chars). Set by the root template.",
            MinLength=8,
            MaxLength=8,
            AllowedPattern=r"^[0-9a-fA-F]{8}$",
        )
    )
    t.add_parameter(
        Parameter(
            "InstallIdLong",
            Type="String",
            Description="Long per-install identifier (12 hex chars). Set by the root template.",
            MinLength=12,
            MaxLength=12,
            AllowedPattern=r"^[0-9a-fA-F]{12}$",
        )
    )


def add_no_echo_parameter(
    t: Template, name: str, *, description: str, default: str = ""
) -> Parameter:
    """Declare a sensitive String parameter (NoEcho=true)."""
    return t.add_parameter(
        Parameter(
            name,
            Type="String",
            NoEcho=True,
            Description=description,
            Default=default,
        )
    )


def add_parameter_group_metadata(
    t: Template,
    *,
    groups: list,
    labels: dict | None = None,
) -> None:
    """Add the AWS::CloudFormation::Interface metadata for console grouping.

    groups: list of dicts with keys "label" and "parameters" (a list of param names).
    labels: optional dict of param-name -> friendly-label.
    """
    interface: dict = {}
    interface["ParameterGroups"] = [
        {"Label": {"default": g["label"]}, "Parameters": g["parameters"]}
        for g in groups
    ]
    if labels:
        interface["ParameterLabels"] = {
            k: {"default": v} for k, v in labels.items()
        }
    md = t.metadata or {}
    md["AWS::CloudFormation::Interface"] = interface
    t.set_metadata(md)

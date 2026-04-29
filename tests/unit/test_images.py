"""Tests for image-override parameter machinery."""

import json

from troposphere import Template

from cardinal_cfn.images import add_image_override


def _to_dict(t):
    return json.loads(t.to_json())


def test_add_image_override_declares_parameter_with_default():
    t = Template()
    add_image_override(
        t,
        name="LakerunnerImage",
        default="public.ecr.aws/cardinalhq.io/lakerunner:v1.20.0",
        description="Lakerunner container image (override for air-gapped).",
    )
    rendered = _to_dict(t)
    p = rendered["Parameters"]["LakerunnerImage"]
    assert p["Type"] == "String"
    assert p["Default"] == "public.ecr.aws/cardinalhq.io/lakerunner:v1.20.0"
    assert "air-gapped" in p["Description"]


def test_add_image_override_returns_ref():
    t = Template()
    ref = add_image_override(
        t,
        name="MaestroImage",
        default="public.ecr.aws/cardinalhq.io/maestro:v0.1.0",
        description="Maestro image",
    )
    assert ref.data == {"Ref": "MaestroImage"}

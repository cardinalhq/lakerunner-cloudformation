"""Every tagged resource, in every template, carries the common tag set.

The generators are the only thing that tags most resources, so a resource that
grows a hand-rolled Tags list silently drops out of cost allocation and out of
`resourcegroupstaggingapi` queries. This walks every template we emit and
fails on any Tags that is not the full set from naming.cardinal_tags().

Resource types CloudFormation tags from the *stack* tags instead (listeners,
listener rules, Cloud Map, IAM server certificates) carry no Tags property
here by design -- see STACK_TAGS in scripts-src/parts/base.sh.
"""

import importlib

import pytest

COMMON_TAG_KEYS = {"Name", "Project", "Application", "Component", "ManagedBy"}

GENERATORS = [
    "cardinal_cfn.lakerunner_infra_base",
    "cardinal_cfn.lakerunner_infra_rds",
    "cardinal_cfn.lakerunner_services",
    "cardinal_cfn.satellite_infra_base",
    "cardinal_cfn.satellite_services",
    "cardinal_cfn.satellite_cwmetrics",
    "cardinal_cfn.children.alb",
    "cardinal_cfn.children.cert",
    "cardinal_cfn.children.maestro",
    "cardinal_cfn.children.migration",
    "cardinal_cfn.children.services_control",
    "cardinal_cfn.children.services_process",
    "cardinal_cfn.children.services_query",
]


def _tagged_resources(module_name):
    """Yield (logical_id, tag_keys) for resources carrying a Tags list."""
    template = importlib.import_module(module_name).build().to_dict()
    for logical_id, resource in template.get("Resources", {}).items():
        tags = resource.get("Properties", {}).get("Tags")
        if isinstance(tags, list) and tags and isinstance(tags[0], dict) and "Key" in tags[0]:
            yield logical_id, {tag["Key"] for tag in tags}


@pytest.mark.parametrize("module_name", GENERATORS)
def test_every_tagged_resource_carries_the_common_set(module_name):
    offenders = {
        logical_id: sorted(COMMON_TAG_KEYS - keys)
        for logical_id, keys in _tagged_resources(module_name)
        if not COMMON_TAG_KEYS <= keys
    }
    assert not offenders, f"{module_name} resources missing common tags: {offenders}"


# The services root is pure composition -- nested stacks plus the Cloud Map
# namespace, none of which the generator tags; the stack tags cover them.
COMPOSITION_ONLY = {"cardinal_cfn.lakerunner_services"}


@pytest.mark.parametrize("module_name", sorted(set(GENERATORS) - COMPOSITION_ONLY))
def test_templates_actually_tag_something(module_name):
    """Guards the walker itself: a bad shape would make the check vacuous."""
    assert list(_tagged_resources(module_name)), f"{module_name} tagged nothing"

"""Integration test: root parameter passes match child parameter declarations."""

import json

import pytest

from cardinal_cfn import root
from cardinal_cfn.children import (
    cluster, database, storage, alb, config, migration,
    services_query, services_process, services_control, otel, maestro,
)


CHILDREN = {
    "ClusterStack": cluster,
    "DatabaseStack": database,
    "StorageStack": storage,
    "AlbStack": alb,
    "ConfigStack": config,
    "MigrationStack": migration,
    "ServicesQueryStack": services_query,
    "ServicesProcessStack": services_process,
    "ServicesControlStack": services_control,
    "OtelStack": otel,
    "MaestroStack": maestro,
}


@pytest.mark.parametrize("logical_id,module", list(CHILDREN.items()))
def test_root_passes_match_child_params(logical_id, module):
    root_td = json.loads(root.build().to_json())
    child_td = json.loads(module.build().to_json())

    nested = root_td["Resources"][logical_id]["Properties"]["Parameters"]
    declared = set(child_td["Parameters"].keys())
    passed = set(nested.keys())

    missing = declared - passed
    extra = passed - declared
    assert not missing, f"{logical_id}: root does not pass required params {missing}"
    assert not extra, f"{logical_id}: root passes unknown params {extra}"

"""Static check: no module under cardinal_cfn.children may import install_id helpers.

The spec is explicit: install-id derivation runs in the root only. Children
must consume InstallIdShort / InstallIdLong as parameters via
`cardinal_cfn.parameters.add_install_id_parameters`. Importing
`cardinal_cfn.install_id` from a child would silently produce a wrong value
(Ref(AWS::StackId) in a nested stack returns the *child* stack id, not the
root's).
"""

import ast
import pathlib

import pytest


_CHILDREN_DIR = pathlib.Path(__file__).resolve().parents[2] / "src" / "cardinal_cfn" / "children"
_FORBIDDEN_MODULE = "cardinal_cfn.install_id"


def _child_modules():
    return sorted(p for p in _CHILDREN_DIR.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("module_path", _child_modules(), ids=lambda p: p.name)
def test_child_module_does_not_import_install_id_helpers(module_path):
    tree = ast.parse(module_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "") == _FORBIDDEN_MODULE:
                pytest.fail(
                    f"{module_path.name} must not import from {_FORBIDDEN_MODULE}: "
                    f"{ast.unparse(node)}"
                )
        elif isinstance(node, ast.Import):
            for name in node.names:
                if name.name == _FORBIDDEN_MODULE:
                    pytest.fail(
                        f"{module_path.name} must not import {_FORBIDDEN_MODULE}: "
                        f"{ast.unparse(node)}"
                    )

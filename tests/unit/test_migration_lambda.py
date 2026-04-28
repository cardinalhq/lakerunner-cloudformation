"""Tests for the migration custom-resource Lambda code."""

import ast

from cardinal_cfn.children import migration_lambda


def test_lambda_source_is_valid_python():
    ast.parse(migration_lambda.SOURCE)


def test_lambda_source_defines_handler():
    tree = ast.parse(migration_lambda.SOURCE)
    func_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    assert "lambda_handler" in func_names


def test_lambda_source_uses_stable_physical_id():
    """PhysicalResourceId must be cardinal-migration-<install-id-long>, never random."""
    src = migration_lambda.SOURCE
    assert "cardinal-migration-" in src
    assert "InstallIdLong" in src or "install_id_long" in src

"""Tests for the required-roles doc generator."""

import re

from cardinal_cfn.required_roles_doc import render_doc


def test_doc_starts_with_top_level_heading():
    doc = render_doc()
    assert doc.startswith("# Cardinal lakerunner -- required IAM roles\n")


def test_doc_lists_all_required_roles():
    doc = render_doc()
    for role in [
        "cardinal-task-role",
        "cardinal-execution-role",
        "cardinal-migration-lambda-role",
        "cardinal-data-setup-lambda-role",
    ]:
        assert f"## `{role}`" in doc, f"missing role section for {role}"


def test_doc_includes_naming_contract_arns():
    doc = render_doc()
    assert "cardinal-ingest-${AccountId}-${Region}" in doc
    assert "arn:aws:sqs:${Region}:${AccountId}:cardinal-ingest" in doc
    assert "task-definition/cardinal-migrator" in doc
    assert "log-group:/cardinal/" in doc


def test_doc_documents_single_role_shortcut():
    doc = render_doc()
    assert "Single-role shortcut" in doc
    assert "ecs-tasks.amazonaws.com" in doc
    assert "lambda.amazonaws.com" in doc


def test_doc_emits_well_formed_json_blocks():
    doc = render_doc()
    json_blocks = re.findall(r"```json\n(.*?)\n```", doc, re.DOTALL)
    import json
    assert len(json_blocks) >= 4 * 2  # at least trust + inline per role
    for block in json_blocks:
        parsed = json.loads(block)
        assert parsed["Version"] == "2012-10-17"


def test_doc_explains_data_setup_lambda_has_broad_scope():
    doc = render_doc()
    assert "create+update+delete" in doc.lower() or "create + update + delete" in doc

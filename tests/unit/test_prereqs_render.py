"""Tests for the prereqs script generator."""

import json

from cardinal_cfn.prereqs.render import render_prereqs_script
from cardinal_cfn.prereqs.roles import expected_role_specs
from cardinal_cfn.prereqs.security_groups import expected_sg_specs


def test_role_specs_cover_required_roles():
    names = {
        spec.name
        for spec in expected_role_specs(
            account_id="123",
            region="us-east-2",
            cluster_arn="arn:aws:ecs:us-east-2:123:cluster/cardinal",
        )
    }
    assert names == {
        "cardinal-task-role",
        "cardinal-execution-role",
        "cardinal-migration-lambda-role",
    }


def test_task_role_inline_policy_is_valid_json():
    [task_role] = [
        r
        for r in expected_role_specs(
            account_id="123",
            region="us-east-2",
            cluster_arn="arn:aws:ecs:us-east-2:123:cluster/cardinal",
        )
        if r.name == "cardinal-task-role"
    ]
    parsed = json.loads(task_role.inline_policy_json)
    assert parsed["Version"] == "2012-10-17"
    assert any("s3:GetObject" in stmt.get("Action", []) for stmt in parsed["Statement"])


def test_execution_role_attaches_managed_policy():
    [execution_role] = [
        r
        for r in expected_role_specs(
            account_id="123",
            region="us-east-2",
            cluster_arn="arn:aws:ecs:us-east-2:123:cluster/cardinal",
        )
        if r.name == "cardinal-execution-role"
    ]
    assert (
        "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
        in execution_role.managed_policy_arns
    )


def test_sg_specs_cover_required_sgs():
    names = {spec.name for spec in expected_sg_specs()}
    assert names == {"cardinal-task-sg", "cardinal-alb-sg", "cardinal-db-sg"}


def test_sg_ingress_includes_self_rule_on_task_sg():
    [task_sg] = [s for s in expected_sg_specs() if s.name == "cardinal-task-sg"]
    self_rules = [r for r in task_sg.ingress if r.source_kind == "self"]
    assert len(self_rules) == 1


def test_db_sg_only_accepts_from_task_sg():
    [db_sg] = [s for s in expected_sg_specs() if s.name == "cardinal-db-sg"]
    assert all(r.source_kind == "sg" and r.source_value == "cardinal-task-sg" for r in db_sg.ingress)


def test_render_emits_posix_shell():
    out = render_prereqs_script()
    assert out.startswith("#!/bin/sh\n")
    assert "set -eu" in out


def test_render_emits_create_role_calls_for_each_role():
    out = render_prereqs_script()
    for role in [
        "cardinal-task-role",
        "cardinal-execution-role",
        "cardinal-migration-lambda-role",
    ]:
        assert f"ensure_role {role}" in out


def test_render_emits_sg_calls_for_each_sg():
    out = render_prereqs_script()
    for sg in ["cardinal-task-sg", "cardinal-alb-sg", "cardinal-db-sg"]:
        assert f"ensure_sg {sg}" in out


def test_render_emits_no_install_id_references():
    out = render_prereqs_script()
    assert "InstallId" not in out


def test_render_emits_output_json_writer():
    out = render_prereqs_script()
    assert "--output-file" in out
    for key in ["TaskRoleArn", "ExecutionRoleArn", "MigrationLambdaRoleArn",
                "TaskSgId", "AlbSgId", "DbSgId"]:
        assert key in out


def test_render_uses_cardinal_tags_on_creates():
    out = render_prereqs_script()
    assert "Application" in out
    assert "ManagedBy" in out
    assert "cardinal-prereqs-script" in out


def test_render_orders_sg_creation_before_ingress():
    out = render_prereqs_script()
    sg_create = out.find("TASK_SG_ID=$(ensure_sg cardinal-task-sg")
    # The call site (not the function definition) must come after SG creation.
    first_ingress_call = out.find('ensure_ingress_self "$TASK_SG_ID"')
    assert sg_create != -1 and first_ingress_call != -1
    assert sg_create < first_ingress_call


def test_render_orders_alb_sg_before_task_self_ingress_dependency():
    # Task SG references ALB SG via ensure_ingress_sg -- ALB SG must exist first.
    out = render_prereqs_script()
    alb_create = out.find("ALB_SG_ID=$(ensure_sg cardinal-alb-sg")
    task_alb_ingress = out.find('ensure_ingress_sg   "$TASK_SG_ID" "$ALB_SG_ID"')
    assert 0 <= alb_create < task_alb_ingress


def test_render_uses_deterministic_resource_names():
    out = render_prereqs_script()
    assert "cardinal-ingest-${ACCOUNT_ID}-${REGION}" in out
    assert "task-definition/cardinal-migrator:*" in out

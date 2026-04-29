"""Tests for the listener-rule priority registry."""

import pytest

from cardinal_cfn.listener_priorities import LISTENER_PRIORITIES, priority_for


def test_known_services_have_unique_priorities():
    values = list(LISTENER_PRIORITIES.values())
    assert len(values) == len(set(values)), "duplicate priorities"


def test_priority_for_known_service_returns_int():
    assert priority_for("query-api") == 100
    assert priority_for("admin-api") == 110
    assert priority_for("maestro-https") == 49999
    assert priority_for("maestro-dex") == 210
    assert priority_for("otel-grpc") == 300


def test_priority_for_unknown_raises():
    with pytest.raises(KeyError):
        priority_for("totally-new-service")

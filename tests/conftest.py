"""Shared test setup. Network access is deliberately made impossible."""

from __future__ import annotations

import socket

import pytest
from support import Project


def _is_loopback(address) -> bool:
    if isinstance(address, tuple) and address:
        return address[0] in {"127.0.0.1", "::1", "localhost"}
    return False


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection

    def blocked_connect(sock, address, *args, **kwargs):
        if _is_loopback(address):
            return real_connect(sock, address, *args, **kwargs)
        raise AssertionError("external network access is disabled in tests")

    def blocked_connect_ex(sock, address, *args, **kwargs):
        if _is_loopback(address):
            return real_connect_ex(sock, address, *args, **kwargs)
        raise AssertionError("external network access is disabled in tests")

    def blocked_create(address, *args, **kwargs):
        if _is_loopback(address):
            return real_create(address, *args, **kwargs)
        raise AssertionError("external network access is disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_connect_ex)
    monkeypatch.setattr(socket, "create_connection", blocked_create)


@pytest.fixture
def project_factory(tmp_path):
    counter = {"value": 0}

    def make(name: str = "case", frozen: str = "2024-01-01T00:00:00Z", **config):
        counter["value"] += 1
        root = tmp_path / f"{name}-{counter['value']}"
        return Project(root, frozen=frozen, config=config)

    return make


@pytest.fixture
def demo_project(project_factory):
    project = project_factory("demo", frozen="2024-01-01T00:00:00Z")
    project.sql(
        "01_schema.sql",
        """
        CREATE TABLE raw.orders (id INT PRIMARY KEY, amount DECIMAL(10,2) NOT NULL, note VARCHAR(20));
        CREATE TABLE mart.daily (day DATE NOT NULL, total DECIMAL(12,2) NOT NULL);
        """,
    )
    project.sql(
        "02_build.sql",
        """
        INSERT INTO mart.daily (day, total)
        SELECT CAST('2024-01-01' AS DATE), SUM(amount) FROM raw.orders;
        """,
    )
    project.contract(
        "daily.yaml",
        """
        version: 1
        contracts:
          - target: mart.daily.total
            required_upstream: [raw.orders.amount]
            expected_type: numeric
            assertions: [not_null]
        """,
    )
    return project

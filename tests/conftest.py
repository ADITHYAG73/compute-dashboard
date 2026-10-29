"""Fail closed if a test accidentally attempts a real network call."""
import socket
import pytest


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network is forbidden in unit tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

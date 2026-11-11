import pytest
import requests

@pytest.fixture(autouse=True)
def block_network(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        return
    def blocked(*args, **kwargs):
        raise AssertionError("Unexpected live HTTP in an offline test")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)

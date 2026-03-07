from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_mcp_endpoint_rejects_no_token():
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1})
    assert resp.status_code == 401

def test_mcp_endpoint_rejects_invalid_token():
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1},
        headers={"Authorization": "Bearer invalidtoken123"},
    )
    assert resp.status_code == 401

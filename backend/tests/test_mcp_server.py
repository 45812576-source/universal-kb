from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import override_get_db
from app.database import get_db
from app.routers import mcp_server

app = FastAPI(title="MCP Server Test API")
app.include_router(mcp_server.router)
app.dependency_overrides[get_db] = override_get_db
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

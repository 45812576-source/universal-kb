"""MCP Client: connects to external MCP servers or REST adapters to browse/import Skills."""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MCP_JSONRPC = "2.0"
MCP_TOOLS_LIST = "tools/list"
MCP_TOOLS_CALL = "tools/call"


class McpClientError(Exception):
    pass


def _mcp_request(url: str, method: str, params: dict, token: str | None = None) -> Any:
    """Send a JSON-RPC 2.0 request to an MCP server."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "jsonrpc": MCP_JSONRPC,
        "id": 1,
        "method": method,
        "params": params,
    }
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise McpClientError(f"MCP error: {data['error']}")
        return data.get("result", {})
    except httpx.HTTPError as e:
        raise McpClientError(f"HTTP error: {e}") from e


def _rest_list_skills(base_url: str, token: str | None, query: str = "", page: int = 1) -> list[dict]:
    """Adapter for REST APIs that don't support MCP protocol."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {"q": query, "page": page, "limit": 20}
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/skills", params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = data if isinstance(data, list) else data.get("items", data.get("results", []))
        return [_normalize_skill(item) for item in items]
    except httpx.HTTPError as e:
        raise McpClientError(f"REST error: {e}") from e


def _normalize_skill(raw: dict) -> dict:
    """Normalize external skill representation to our internal format."""
    return {
        "upstream_id": str(raw.get("id", raw.get("slug", ""))),
        "name": raw.get("name", raw.get("title", "")),
        "description": raw.get("description", ""),
        "system_prompt": raw.get("system_prompt", raw.get("prompt", "")),
        "upstream_version": str(raw.get("version", raw.get("updated_at", "1"))),
        "author": raw.get("author", raw.get("created_by", "")),
        "tags": raw.get("tags", []),
    }


def list_remote_skills(source, query: str = "", page: int = 1) -> list[dict]:
    """List skills from a McpSource (MCP or REST adapter)."""
    if source.adapter_type == "mcp":
        result = _mcp_request(
            source.url,
            MCP_TOOLS_LIST,
            {"query": query, "page": page},
            source.auth_token,
        )
        tools = result.get("tools", [])
        return [_normalize_skill(t) for t in tools]
    else:
        return _rest_list_skills(source.url, source.auth_token, query, page)


def fetch_remote_skill(source, upstream_id: str) -> dict:
    """Fetch a single skill's full definition from an external source."""
    if source.adapter_type == "mcp":
        result = _mcp_request(
            source.url,
            MCP_TOOLS_CALL,
            {"name": "get_skill", "arguments": {"id": upstream_id}},
            source.auth_token,
        )
        return _normalize_skill(result.get("content", result))
    else:
        headers = {"Authorization": f"Bearer {source.auth_token}"} if source.auth_token else {}
        try:
            resp = httpx.get(
                f"{source.url.rstrip('/')}/skills/{upstream_id}",
                headers=headers, timeout=15,
            )
            resp.raise_for_status()
            return _normalize_skill(resp.json())
        except httpx.HTTPError as e:
            raise McpClientError(f"Fetch error: {e}") from e


def check_upstream_version(source, skill) -> dict:
    """Check if upstream has a newer version. Returns {'has_diff': bool, 'new_version': str}."""
    try:
        remote = fetch_remote_skill(source, skill.upstream_id)
        new_ver = remote.get("upstream_version", "")
        has_diff = new_ver != skill.upstream_version
        return {
            "has_diff": has_diff,
            "new_version": new_ver,
            "remote": remote,
        }
    except McpClientError as e:
        logger.warning(f"Upstream check failed for skill {skill.id}: {e}")
        return {"has_diff": False, "new_version": None, "remote": None, "error": str(e)}

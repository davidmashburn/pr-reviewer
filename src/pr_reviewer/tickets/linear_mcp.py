"""Minimal MCP client for Linear's hosted server (mcp.linear.app).

Used when Linear is connected via browser auth: Dynamic Client Registration
tokens are audienced to the MCP resource, so ticket fetches speak MCP
(JSON-RPC over streamable HTTP) rather than the GraphQL API.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

MCP_URL = "https://mcp.linear.app/mcp"
_PROTOCOL = "2025-06-18"


def _parse_body(resp: httpx.Response) -> dict[str, Any] | None:
    """Responses may be plain JSON or a one-shot SSE stream."""
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
        return None
    try:
        return json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        return None


class LinearMCP:
    def __init__(self, token: str) -> None:
        self.token = token
        self.session_id = ""
        self._rpc_id = 0

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _PROTOCOL,
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    async def _post(self, client: httpx.AsyncClient, payload: dict[str, Any]) -> dict[str, Any] | None:
        r = await client.post(MCP_URL, json=payload, headers=self._headers())
        if sid := r.headers.get("mcp-session-id"):
            self.session_id = sid
        r.raise_for_status()
        return _parse_body(r)

    async def _rpc(self, client: httpx.AsyncClient, method: str, params: dict[str, Any]) -> Any:
        self._rpc_id += 1
        body = await self._post(client, {"jsonrpc": "2.0", "id": self._rpc_id,
                                         "method": method, "params": params})
        if body is None:
            raise RuntimeError(f"empty MCP response for {method}")
        if body.get("error"):
            raise RuntimeError(f"MCP {method}: {body['error'].get('message', body['error'])}")
        return body.get("result")

    async def _init(self, client: httpx.AsyncClient) -> None:
        await self._rpc(client, "initialize", {
            "protocolVersion": _PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "pr-reviewer", "version": "0.1.0"},
        })
        # initialized notification (no id, no response expected)
        try:
            await self._post(client, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        except httpx.HTTPStatusError:
            pass  # some servers 202/204 or reject; not fatal

    @staticmethod
    def _pick_issue_tool(tools: list[dict[str, Any]]) -> tuple[str, str] | None:
        """→ (tool_name, argument_key) for fetching one issue by identifier."""
        for t in tools:
            name = (t.get("name") or "").lower()
            if "issue" in name and ("get" in name or "fetch" in name):
                props = ((t.get("inputSchema") or {}).get("properties") or {})
                for key in ("id", "issueId", "identifier", "issue_id", "query"):
                    if key in props:
                        return t["name"], key
        return None

    @staticmethod
    def _texts(result: dict[str, Any]) -> str:
        return "\n".join(c.get("text", "") for c in (result.get("content") or [])
                         if c.get("type") == "text")

    async def list_tools(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=30) as client:
            await self._init(client)
            result = await self._rpc(client, "tools/list", {})
            return result.get("tools") or []

    async def fetch_issue(self, key: str) -> dict[str, Any] | None:
        """→ {title, body, url, identifier} or None. Tool discovered at runtime."""
        async with httpx.AsyncClient(timeout=30) as client:
            await self._init(client)
            tools = (await self._rpc(client, "tools/list", {})).get("tools") or []
            picked = self._pick_issue_tool(tools)
            if picked is None:
                raise RuntimeError("Linear MCP exposes no get-issue tool")
            name, arg = picked
            result = await self._rpc(client, "tools/call",
                                     {"name": name, "arguments": {arg: key}})
            if result.get("isError"):
                return None
            text = self._texts(result)
            # prefer structured JSON when the tool returns it
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                return {
                    "identifier": data.get("identifier") or data.get("id") or key,
                    "title": data.get("title") or "",
                    "body": data.get("description") or data.get("body") or "",
                    "url": data.get("url") or "",
                }
            if text.strip():
                # plain-text tool output: keep it verbatim as the source body
                return {"identifier": key, "title": "", "body": text, "url": ""}
            return None

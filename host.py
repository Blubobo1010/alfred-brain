"""The two Hermes services the brain needs from outside: the configured model and the MCP servers.

Behind a tiny interface so the tests inject fakes and the rest of the plugin never imports Hermes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_ID = "brain"
AUX_TASK = "alfred_brain"


class Host:
    def llm_json(self, instructions: str, text: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError

    def mcp(self, server: str, tool: str, args: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        raise NotImplementedError

    def mcp_params(self, server: str, tool: str) -> list[str]:
        """Parameter names of an MCP tool's input schema ([] when unknown)."""
        return []


class HermesHost(Host):
    """The real thing: ``PluginLlm`` on the user's model (or the ``alfred_brain`` auxiliary slot) and
    the gated ``call_mcp`` (``plugins.entries.brain.mcp_allowlist``)."""

    def __init__(self) -> None:
        self._ctx: Any = None

    def _context(self) -> Any:
        if self._ctx is None:
            from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
            self._ctx = PluginContext(PluginManifest(name=PLUGIN_ID, key=PLUGIN_ID), get_plugin_manager())
        return self._ctx

    def llm_json(self, instructions: str, text: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        from agent.plugin_llm import PluginLlmTextInput
        llm = self._context().llm
        kwargs = dict(instructions=instructions, input=[PluginLlmTextInput(text=text)], json_schema=schema,
                      schema_name="estrazione", temperature=0, timeout=180, purpose="alfred-brain consolidation")
        try:
            result = llm.complete_structured(task=AUX_TASK, **kwargs)
        except Exception as exc:  # aux slot not registered in this process: the user's main model
            logger.debug("brain: auxiliary task unavailable (%s), using the main model", exc)
            result = llm.complete_structured(**kwargs)
        if isinstance(result.parsed, dict):
            return result.parsed
        try:
            parsed = json.loads(_strip_fence(result.text))
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def mcp(self, server: str, tool: str, args: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        _connect(server)
        envelope = self._context().call_mcp(server, tool, args, timeout=timeout)
        if envelope.get("truncated"):  # long transcripts exceed call_mcp's 64 KB cap: same client, no cap
            from tools.mcp_tool_handlers import _make_tool_handler
            raw = _make_tool_handler(server, tool, timeout)(dict(args))
            envelope = _envelope(raw)
        return envelope

    def mcp_params(self, server: str, tool: str) -> list[str]:
        try:
            from tools.registry import registry
            entry = registry.get_entry(f"mcp__{server}__{tool}")
        except Exception:
            return []
        schema = getattr(entry, "schema", None) or {}
        params = schema.get("parameters") or schema.get("inputSchema") or {}
        required = list(params.get("required") or [])
        return required + [p for p in (params.get("properties") or {}) if p not in required]


def _connect(server: str) -> None:
    """Outside the gateway (``alfred brain sync`` from a shell) MCP servers are not connected yet."""
    try:
        from tools.mcp_tool_discovery import discover_mcp_tools
        discover_mcp_tools([server])
    except Exception as exc:
        logger.debug("brain: MCP discovery for %s failed: %s", server, exc)


def _envelope(raw: Any) -> dict[str, Any]:
    """``call_mcp``'s envelope shape for a raw handler result, without its size cap."""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and "error" in parsed:
        return {"ok": False, "error": parsed["error"]}
    if isinstance(parsed, dict) and "result" in parsed:
        out = {"ok": True, "result": parsed["result"]}
        if "structuredContent" in parsed:
            out["structuredContent"] = parsed["structuredContent"]
        return out
    return {"ok": True, "result": parsed if parsed is not None else raw}


def _strip_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text

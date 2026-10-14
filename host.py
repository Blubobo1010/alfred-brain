"""The Hermes services the brain needs from outside: the configured model, the MCP servers, speech-to-text and
the updates cron job.

Behind a tiny interface so the tests inject fakes and the rest of the plugin never imports Hermes.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_ID = "brain"
AUX_TASK = "alfred_brain"
STT_SOURCE = "alfred-brain"  # the caller label Hermes passes to pre_transcription hooks
INBOX_JOB = "brain: aggiornamenti dalla cartella"  # the cron job that reports folder updates and voice memos


class TranscriptionError(RuntimeError):
    """Alfred's speech-to-text could not transcribe a recording."""


class Host:
    def llm_json(self, instructions: str, text: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError

    def mcp(self, server: str, tool: str, args: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        raise NotImplementedError

    def mcp_params(self, server: str, tool: str) -> list[str]:
        """Parameter names of an MCP tool's input schema ([] when unknown)."""
        return []

    def transcribe(self, path: Path) -> str:
        """The recording's text ("" when there is no speech); raises TranscriptionError."""
        raise NotImplementedError

    def local_path(self, path: str) -> Path:
        """A file path as the agent saw it (an attachment, maybe inside a sandbox) on this machine."""
        return Path(path).expanduser()

    def wake_inbox(self) -> None:
        """A recording nobody is waiting for is filed: run the updates report soon instead of at the next half hour."""


class HermesHost(Host):
    """The real thing: ``PluginLlm`` on the user's model (or the ``alfred_brain`` auxiliary slot) and
    the gated ``call_mcp`` (``plugins.entries.brain.mcp_allowlist``)."""

    def __init__(self) -> None:
        self._ctx: Any = None
        self.wake = True  # the CLI prints its own reports: there the gateway's cron must not race it

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
        return json_object(result.text)

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

    def transcribe(self, path: Path) -> str:
        """Hermes' own speech-to-text (``stt.*`` in config.yaml: local faster-whisper, a command or a cloud provider)."""
        try:
            from tools.transcription_tools import transcribe_audio
        except ImportError as exc:
            raise TranscriptionError(f"Alfred's speech-to-text is not available: {exc}") from exc
        result = transcribe_audio(str(path), source=STT_SOURCE)
        if result.get("success"):
            return str(result.get("transcript") or "").strip()
        if result.get("no_speech"):
            return ""
        raise TranscriptionError(str(result.get("error") or "transcription failed")[:300])

    def local_path(self, path: str) -> Path:
        """With a Docker terminal backend the agent sees attachments under /root/.hermes: map them back."""
        try:
            from tools.credential_files import from_agent_visible_cache_path
            path = from_agent_visible_cache_path(path)
        except Exception as exc:  # noqa: BLE001 - outside the gateway the path is already local
            logger.debug("brain: cache path mapping unavailable (%s)", exc)
        return Path(path).expanduser()

    def wake_inbox(self) -> None:
        """Move the updates job to the next cron tick (about a minute). Only an enabled job: a paused one stays
        paused (``trigger_job`` would re-enable it); without the job the report waits for the CEO to ask."""
        if not self.wake:
            return
        try:
            from cron.jobs import resolve_job_ref, trigger_job
            job = resolve_job_ref(INBOX_JOB)
            if job and job.get("enabled", True):
                trigger_job(job["id"])
        except Exception as exc:  # noqa: BLE001 - the half-hourly run still comes
            logger.debug("brain: could not wake %s: %s", INBOX_JOB, exc)


def spawn_thread(target, *, name: str, args: tuple = ()) -> threading.Thread:
    """A daemon thread that keeps the caller's Hermes context (profile-scoped home); a plain one outside Hermes."""
    try:
        from agent.memory_provider import spawn_context_thread
    except ImportError:
        return threading.Thread(target=target, name=name, args=args, daemon=True)
    return spawn_context_thread(target, name=name, args=args)


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


def json_object(text: str | None) -> dict[str, Any] | None:
    """The JSON object in a model's answer: bare, in a code fence, or after a local model's reasoning
    (``<think>…</think>{…}``) or a sentence of prose. None when there is none."""
    text = _strip_fence((text or "").rsplit("</think>", 1)[-1])
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1]):
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text

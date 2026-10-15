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
DEFAULT_OWNER = "il CEO"
RELAXED_SCHEMA: dict[str, Any] = {"type": "object"}  # the retry after an answer outside the strict schema
AUX_TASK_INFO: dict[str, Any] = {  # one place for the gateway's register() and the CLI
    "display_name": "Secondo cervello (consolidamento)",
    "description": "Model that files conversations, recordings and voice memos into client, person and project notes.",
    "inherit_from": "compression"}


class TranscriptionError(RuntimeError):
    """Alfred's speech-to-text could not transcribe a recording."""


class Host:
    wake = True  # move the updates job up when a recording nobody waits for is filed (the CLI prints its own)
    background = True  # link new items in a background pass (the CLI runs the pass itself before exiting)
    answered_by = ""  # "provider/model" of the last answer (``brain eval`` reports what it really measured)

    def llm_json(self, instructions: str, text: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError

    def owner(self) -> str:
        """The CEO's name (``plugins.entries.brain.settings.owner_name``): "io" in his memos and chat is him."""
        return DEFAULT_OWNER

    def setting(self, key: str, default: Any = None) -> Any:
        """``plugins.entries.brain.settings.<key>``."""
        return default

    def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Another plugin's tool by name (Outlook, Odoo): its JSON answer, None when there is no answer at all."""
        return None

    def model_busy(self) -> bool | None:
        """Is the local model answering someone right now (background work waits)? None: no local model to share."""
        return None

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
        self.background = True

    def _context(self) -> Any:
        if self._ctx is None:
            from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
            self._ctx = PluginContext(PluginManifest(name=PLUGIN_ID, key=PLUGIN_ID), get_plugin_manager())
        return self._ctx

    def llm_json(self, instructions: str, text: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        """The strict schema guides the model (on a local server it becomes the decoding grammar: lists and required
        fields are always respected). A provider that does not enforce it may answer outside it, and Hermes then
        throws the whole answer away: once more with a bare object schema, and the plugin normalises the result."""
        from agent.plugin_llm import PluginLlmTextInput
        llm = self._context().llm
        kwargs = dict(instructions=instructions, input=[PluginLlmTextInput(text=text)], schema_name="estrazione",
                      temperature=0, timeout=180, purpose="alfred-brain consolidation")

        def complete(json_schema: dict[str, Any]) -> Any:
            try:
                return llm.complete_structured(task=AUX_TASK, json_schema=json_schema, **kwargs)
            except PermissionError as exc:  # PluginLlmTrustError: the aux slot is not registered here (CLI)
                logger.debug("brain: auxiliary task unavailable (%s), using the main model", exc)
                return llm.complete_structured(json_schema=json_schema, **kwargs)

        try:
            result = complete(schema)
        except ValueError as exc:
            if "schema" not in str(exc):
                raise
            logger.info("brain: answer outside the schema (%s), asking again without constraints", exc)
            # The relaxed schema is what Hermes shows the model, so the shape we want goes in the instructions.
            kwargs["instructions"] = (f"{instructions}\n\nRispondi con un oggetto JSON di questa forma (le liste "
                                      f"sono liste, anche di un solo elemento): {json.dumps(schema, ensure_ascii=False)}")
            result = complete(RELAXED_SCHEMA)
        self.answered_by = "/".join(str(v) for v in (getattr(result, "provider", ""), getattr(result, "model", "")) if v)
        if isinstance(result.parsed, dict):
            return result.parsed
        return json_object(result.text)

    def register_aux_task(self) -> bool:
        """A CLI command runs without ``register()``: register the ``alfred_brain`` slot here too, so the command uses
        the model the gateway uses instead of falling back to the main one."""
        try:
            self._context().register_auxiliary_task(AUX_TASK, **AUX_TASK_INFO)
            return True
        except Exception as exc:  # noqa: BLE001 - the main model still answers
            logger.debug("brain: auxiliary task not registered in this process (%s)", exc)
            return False

    def owner(self) -> str:
        return " ".join(str(self.setting("owner_name", "") or "").split()) or DEFAULT_OWNER

    def setting(self, key: str, default: Any = None) -> Any:
        try:
            value = self._context().get_config(key, default)
        except Exception as exc:  # noqa: BLE001 - outside Hermes or a broken config: the default
            logger.debug("brain: setting %s unavailable (%s)", key, exc)
            return default
        return default if value is None else value

    def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """``PluginContext.dispatch_tool``: the tool's handler directly, as the meeting brief needs it (no approval
        hooks: only read tools are called this way). An unknown tool comes back as ``{"error": "Unknown tool…"}``."""
        try:
            raw = self._context().dispatch_tool(name, args)
        except Exception as exc:  # noqa: BLE001 - a broken plugin leaves its section out
            logger.debug("brain: tool %s failed (%s)", name, exc)
            return None
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def model_busy(self) -> bool | None:
        """Hermes' own check for its background reviews: a processing slot on the llama-server it manages. None when
        Hermes manages no local model (a cloud model has nothing to share) or the check is unavailable."""
        try:
            from agent.auxiliary_client import _managed_local_netloc
            if not _managed_local_netloc():
                return None
            from agent.review_idle_queue import _managed_server_idle
            return not _managed_server_idle()
        except Exception as exc:  # noqa: BLE001
            logger.debug("brain: local model state unavailable (%s)", exc)
            return None

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


def as_list(value: Any) -> list:
    """A field the model should have given as a list: a lone string or object becomes a one-item list (iterating a
    string would file one item per character), anything else empty."""
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value] if isinstance(value, (str, dict)) and value else []


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

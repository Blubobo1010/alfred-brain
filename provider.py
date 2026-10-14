"""The memory provider: journal every turn, recall from the vault before each turn, consolidate at
session end, and seven small tools (search, read, note, Plaud sync, folder updates, voice memos, consolidate)."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from . import consolidate, journal
from .host import HermesHost, Host
from .inbox import Inbox, voice_path
from .index import Index, recall_block
from .plaud import Plaud, PlaudError
from .vault import ENTITY_DIRS, Vault
from .voice import VoiceMemos

try:
    from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt, spawn_context_thread
except ImportError:  # outside Hermes (unit tests): the same surface, minimal
    import threading
    from abc import ABC
    from dataclasses import dataclass

    class MemoryProvider(ABC):  # type: ignore[no-redef]  # noqa: B024
        pass

    @dataclass(frozen=True)
    class RecallStatus:  # type: ignore[no-redef]
        provider_label: str
        count: int
        glyph: str = "🧠"

    def is_trivial_prompt(text):  # type: ignore[no-redef]
        stripped = (text or "").strip()
        return not stripped or stripped.startswith("/") or stripped.lower().rstrip("!.?") in {"ok", "si", "sì", "grazie", "ciao", "no"}

    def spawn_context_thread(target, *, name, daemon=True, args=(), kwargs=None):  # type: ignore[no-redef]
        return threading.Thread(target=target, name=name, daemon=daemon, args=args, kwargs=kwargs)

logger = logging.getLogger(__name__)

_PROP = {"type": "string"}
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"name": "brain_search",
     "description": "Search the CEO's second brain (Obsidian vault: clients, people, projects, daily journal, Plaud "
                    "recordings). Use it before answering about a person, a company, a meeting or a past conversation.",
     "parameters": {"type": "object", "properties": {"query": _PROP, "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
                    "required": ["query"]}},
    {"name": "brain_read",
     "description": "Read one vault note in full by its path (as returned by brain_search, e.g. 'Clienti/Kimera.md').",
     "parameters": {"type": "object", "properties": {"path": _PROP}, "required": ["path"]}},
    {"name": "brain_note",
     "description": "Save a durable fact about a client, person or project in the vault (when the user asks you to "
                    "remember something, or states a decision). One short self-contained sentence in Italian.",
     "parameters": {"type": "object", "properties": {"tipo": {"type": "string", "enum": list(ENTITY_DIRS)}, "nome": _PROP,
                                                     "fatto": _PROP}, "required": ["tipo", "nome", "fatto"]}},
    {"name": "brain_sync",
     "description": "Import new Plaud recordings into the vault (runs on a schedule; call it when asked for the latest recordings).",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "brain_inbox",
     "description": "Import new written updates from colleagues (shared folder: production, design, other topics) and "
                    "the CEO's voice memos from his own folder into the vault and return a short report for the CEO in "
                    "the field 'report' (empty when nothing is new; recordings are reported once transcribed).",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "brain_transcribe",
     "description": "Transcribe an audio file the user sent (an iPhone voice memo or any audio attachment: pass the local "
                    "path given in the attachment note), file it in the vault as a categorised voice note (Note vocali/) "
                    "and return the summary for the user in the field 'report'. Status 'in_progress' means a long "
                    "recording is still being transcribed: call again later with the same path.",
     "parameters": {"type": "object", "properties": {"path": _PROP}, "required": ["path"]}},
    {"name": "brain_consolidate",
     "description": "File what was said in recent conversations and recordings into the client, person and project notes.",
     "parameters": {"type": "object", "properties": {}}},
]


class BrainProvider(MemoryProvider):
    def __init__(self, host: Host | None = None, vault: Vault | None = None):
        self._host = host
        self._vault = vault
        self._index: Index | None = None
        self._context = "primary"
        self._platform = ""
        self._last_recall = 0
        self._author = ""  # who wrote the current turn (a voice memo's author)
        self._turn_notes: list[str] = []  # voice notes filed during the current turn

    # ---- identity -------------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "brain"

    @property
    def vault(self) -> Vault:
        if self._vault is None:
            self._vault = Vault()
        return self._vault

    @property
    def host(self) -> Host:
        if self._host is None:
            self._host = HermesHost()
        return self._host

    def is_available(self) -> bool:
        return self.vault.exists()

    def unavailable_reason(self) -> str:
        return f"no vault at {self.vault.root}: run `alfred brain init`"

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._context = str(kwargs.get("agent_context") or "primary")
        self._platform = str(kwargs.get("platform") or "")
        self._index = Index(self.vault)

    @property
    def writes(self) -> bool:
        return self._context == "primary"

    def system_prompt_block(self) -> str:
        return ("# Secondo cervello\nHai un vault Obsidian con diario delle conversazioni, note su clienti, persone e "
                "progetti, registrazioni Plaud, aggiornamenti scritti dai collaboratori (Aggiornamenti/) e note "
                "vocali (Note vocali/). Prima di rispondere su persone, aziende, incontri, stato di produzione o "
                "conversazioni passate usa brain_search; cita le note come [[percorso]]. Salva con brain_note ciò che "
                "l'utente chiede di ricordare. Quando l'utente invia un file audio (una nota vocale), chiama "
                "brain_transcribe con il suo percorso e riferisci il campo report."
                + (" Se un audio viene scartato perché troppo grande per la chat, non chiedere un file più piccolo: "
                   "digli di salvarlo nella sua cartella di Alfred su OneDrive (Condividi → Salva in File), il "
                   "riassunto arriva entro mezz'ora." if voice_path() else ""))

    # ---- per turn -------------------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if is_trivial_prompt(query) or self._index is None:
            self._last_recall = 0
            return ""
        try:
            block, self._last_recall = recall_block(self._index, query)
            return block
        except Exception as exc:  # noqa: BLE001 - recall must never break a turn
            logger.debug("brain: prefetch failed: %s", exc)
            self._last_recall = 0
            return ""

    def recall_status(self) -> RecallStatus | None:
        return RecallStatus("Secondo cervello", self._last_recall) if self._last_recall else None

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        self._author = str(kwargs.get("author_name") or "")
        self._turn_notes = []

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "",
                  messages: list[dict[str, Any]] | None = None, turn_author: dict[str, Any] | None = None) -> None:
        notes, self._turn_notes = self._turn_notes, []
        if not self.writes or is_trivial_prompt(user_content) or _is_compaction(user_content):
            return
        if notes:  # the memo's facts are filed from its note: the journal keeps the link, not the summary again
            assistant_content = "\n".join(f"Nota vocale archiviata: [[{rel[:-3]}]]" for rel in notes)
        author = str((turn_author or {}).get("name") or "")
        journal.append_turn(self.vault, user_content, assistant_content, author=author, platform=self._platform)

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        if not self.writes:
            return
        # Off the caller's thread: a gateway eviction or CLI exit must not wait for the model.
        # If the process dies first, the nightly brain_consolidate catches up (offsets in the vault state).
        spawn_context_thread(self._consolidate_quietly, name="brain-consolidate").start()

    def _consolidate_quietly(self) -> None:
        try:
            consolidate.run(self.host, self.vault)
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain: consolidation failed: %s", exc)

    # ---- tools ----------------------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return TOOL_SCHEMAS

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        try:
            handler = getattr(self, "_tool_" + tool_name.removeprefix("brain_"), None)
            if not tool_name.startswith("brain_") or handler is None:
                return _err(f"unknown tool {tool_name}")
            return json.dumps(handler(args or {}), ensure_ascii=False)
        except (KeyError, ValueError) as exc:
            return _err(str(exc))
        except PlaudError as exc:
            return _err(f"Plaud: {exc}", hint="Check `alfred mcp list` (server plaud) and "
                        "plugins.entries.brain.mcp_allowlist; re-login with `alfred mcp login plaud` if expired.")
        except PermissionError as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001 - tools always answer JSON
            return _err(f"{exc.__class__.__name__}: {str(exc)[:300]}")

    def _tool_search(self, args: dict[str, Any]) -> dict[str, Any]:
        index = self._index or Index(self.vault)
        limit = max(1, min(int(args.get("limit") or 5), 10))
        hits = index.search(str(args["query"]), limit=limit)
        return {"ok": True, "results": [{"path": d.rel, "title": d.title, "kind": d.kind,
                                         "excerpt": index.excerpt(d, str(args["query"]), 400)} for _s, d in hits]}

    def _tool_read(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self.vault.resolve(str(args["path"]))
        if path.suffix != ".md" or not path.is_file():
            raise ValueError(f"no note at {args['path']!r}")
        text = path.read_text(encoding="utf-8")
        return {"ok": True, "path": self.vault.rel(path), "text": text[:20000], "truncated": len(text) > 20000}

    def _tool_note(self, args: dict[str, Any]) -> dict[str, Any]:
        today = date.today()
        source = f"[[Diario/{today.isoformat()}]]"
        path, added = self.vault.add_facts(str(args["tipo"]), str(args["nome"]),
                                           [(str(args["fatto"]), today.isoformat(), source)], today=today)
        self.vault.write_index()
        self.vault.commit(f"Nota: {self.vault.rel(path)}")
        return {"ok": True, "path": self.vault.rel(path), "added": bool(added)}

    def _tool_sync(self, args: dict[str, Any]) -> dict[str, Any]:
        report = Plaud(self.host, self.vault).sync()
        if report["new"]:
            report["consolidation"] = consolidate.run(self.host, self.vault)
        return {"ok": True, **report}

    def _tool_inbox(self, args: dict[str, Any]) -> dict[str, Any]:
        report = Inbox(self.host, self.vault).sync()
        if report["new"]:
            report["consolidation"] = consolidate.run(self.host, self.vault)
        return {"ok": True, **report}

    def _tool_transcribe(self, args: dict[str, Any]) -> dict[str, Any]:
        result = VoiceMemos(self.host, self.vault).run(self.host.local_path(str(args["path"])),
                                                       fonte=self._platform or "chat", autore=self._author)
        if result.get("note") and result.get("status") in ("done", "duplicate"):
            self._turn_notes.append(result["note"])
        return result

    def _tool_consolidate(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, **consolidate.run(self.host, self.vault)}

    def shutdown(self) -> None:
        self._index = None


def _is_compaction(text: str) -> bool:
    """Compaction handoff summaries arrive as user turns: never journal the compactor's own output."""
    try:
        from agent.context_compressor import is_compaction_summary_message
        return bool(is_compaction_summary_message({"role": "user", "content": text}))
    except Exception:  # noqa: BLE001 - outside Hermes, or the helper moved
        return (text or "").lstrip().startswith(("[CONTEXT COMPACTION", "[Context summary"))


def _err(message: str, hint: str = "") -> str:
    return json.dumps({"ok": False, "error": message, **({"hint": hint} if hint else {})}, ensure_ascii=False)


"""Plaud recordings → ``Plaud/<date> <title>.md`` through Plaud's MCP server (no model involved).

The server's result shapes are only documented by third parties, so parsing is deliberately
tolerant: records are found by key names (id / file_id, title / name, date / start_time …) at any
nesting level, and a payload that cannot be understood is saved raw instead of being lost.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from .host import Host
from .vault import PLAUD_DIR, Vault, _lock, atomic_write, safe_filename

logger = logging.getLogger(__name__)

SERVER = "plaud"
FIRST_SYNC_DAYS = 30
ID_KEYS = ("file_id", "fileId", "id", "recording_id", "uuid")
TITLE_KEYS = ("title", "name", "file_name", "filename", "subject")
DATE_KEYS = ("start_time", "startTime", "created_at", "createdAt", "date", "recorded_at", "start_at", "time")
TEXT_KEYS = ("transcript", "text", "content", "note", "summary", "markdown", "body", "result")


class PlaudError(RuntimeError):
    pass


def payload(envelope: dict[str, Any]) -> Any:
    """The useful part of a ``call_mcp`` envelope: structured content, else the (JSON-decoded) text."""
    if not envelope.get("ok", True):
        err = envelope.get("error")
        raise PlaudError(str(err.get("message") if isinstance(err, dict) else err)[:300])
    data = envelope.get("structuredContent")
    if data is None:
        data = envelope.get("result")
    if isinstance(data, str):
        try:
            return json.loads(data)
        except ValueError:
            return data
    return data


def records(data: Any) -> list[dict[str, Any]]:
    """Every dict carrying an id-like key, depth first (lists of files, {"files": [...]}, {"data": {...}})."""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if any(k in node for k in ID_KEYS):
                found.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def _first(rec: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((rec[k] for k in keys if rec.get(k) not in (None, "")), None)


def record_date(rec: dict[str, Any]) -> date | None:
    value = _first(rec, DATE_KEYS)
    if isinstance(value, int | float):  # epoch seconds or milliseconds
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value).date()
    if isinstance(value, str):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", value)
        if m:
            return date.fromisoformat(m.group(1))
    return None


def as_text(data: Any) -> str:
    """Readable markdown from a note/transcript payload: known text fields, speaker segments, else JSON."""
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, list):
        return "\n".join(_segment(x) if isinstance(x, dict) else str(x) for x in data).strip()
    if isinstance(data, dict):
        for key in ("segments", "utterances", "transcript", "paragraphs"):
            if isinstance(data.get(key), list):
                return as_text(data[key])
        parts = [str(data[k]).strip() for k in TEXT_KEYS if isinstance(data.get(k), str) and data[k].strip()]
        for key in ("action_items", "actionItems", "todos", "tasks"):
            if isinstance(data.get(key), list) and data[key]:
                parts.append("### Azioni\n" + "\n".join(f"- {as_text(x)}" for x in data[key]))
        if parts:
            return "\n\n".join(parts)
        return "```json\n" + json.dumps(data, ensure_ascii=False, indent=2)[:20000] + "\n```"
    return "" if data is None else str(data)


def _segment(seg: dict[str, Any]) -> str:
    speaker = seg.get("speaker") or seg.get("speaker_name") or seg.get("speakerName")
    stamp = seg.get("start") or seg.get("start_time") or seg.get("timestamp")
    text = seg.get("text") or seg.get("content") or ""
    if isinstance(stamp, int | float):
        stamp = f"{int(stamp // 3600):02d}:{int(stamp % 3600 // 60):02d}:{int(stamp % 60):02d}"
    prefix = " ".join(p for p in (f"[{stamp}]" if stamp else "", f"**{speaker}:**" if speaker else "") if p)
    return f"{prefix} {text}".strip() if text else json.dumps(seg, ensure_ascii=False)


class Plaud:
    def __init__(self, host: Host, vault: Vault):
        self.host, self.vault = host, vault

    def _call(self, tool: str, args: dict[str, Any]) -> Any:
        return payload(self.host.mcp(SERVER, tool, args, timeout=120))

    def _by_id(self, tool: str, file_id: str) -> Any:
        params = self.host.mcp_params(SERVER, tool)
        names = [p for p in params if p in ID_KEYS] or [p for p in params if "id" in p.lower()] or list(ID_KEYS[:2])
        last: Exception | None = None
        for name in names[:3]:
            try:
                return self._call(tool, {name: file_id})
            except PlaudError as exc:  # wrong argument name: try the next candidate
                last = exc
        raise last or PlaudError(f"{tool} failed")

    def list_new(self, since: date) -> list[dict[str, Any]]:
        data = self._call("list_files", {"date_from": since.isoformat(), "date_to": date.today().isoformat()})
        return records(data)

    def sync(self, today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        state = self.vault.state()
        seen = set(state.get("plaud_seen", []))
        cursor = state.get("plaud_cursor")
        since = date.fromisoformat(cursor) - timedelta(days=1) if cursor else today - timedelta(days=FIRST_SYNC_DAYS)
        report: dict[str, Any] = {"new": 0, "skipped": 0, "errors": []}
        newest = date.fromisoformat(cursor) if cursor else None
        for rec in self.list_new(since):
            file_id = str(_first(rec, ID_KEYS))
            if file_id in seen:
                report["skipped"] += 1
                continue
            try:
                path = self._write(rec, file_id, today)
            except PlaudError as exc:
                report["errors"].append(f"{file_id}: {exc}")
                continue
            when = record_date(rec)
            newest = max(filter(None, (newest, when))) if when or newest else None
            with _lock:
                state = self.vault.state()
                state.setdefault("plaud_seen", []).append(file_id)
                state.setdefault("plaud_pending", []).append(self.vault.rel(path))
                if newest:
                    state["plaud_cursor"] = newest.isoformat()
                self.vault.save_state(state)
            seen.add(file_id)
            report["new"] += 1
        with _lock:
            state = self.vault.state()
            state["plaud_last_sync"] = datetime.now().isoformat(timespec="seconds")
            self.vault.save_state(state)
        if report["new"]:
            self.vault.commit(f"Plaud: {report['new']} registrazioni")
        return report

    def _write(self, rec: dict[str, Any], file_id: str, today: date) -> Any:
        title = str(_first(rec, TITLE_KEYS) or f"Registrazione {file_id[:8]}")
        when = record_date(rec) or today
        note = as_text(self._by_id("get_note", file_id))
        try:
            transcript = as_text(self._by_id("get_transcript", file_id))
        except PlaudError as exc:  # a recording still being transcribed: keep the summary, note it
            transcript = f"_Trascrizione non disponibile: {exc}_"
        path = self.vault.root / PLAUD_DIR / f"{when.isoformat()} {safe_filename(title)}.md"
        if path.exists():
            path = path.with_name(f"{path.stem} ({file_id[:6]}).md")
        meta = (f"---\ntipo: registrazione\nfonte: plaud\nplaud_id: {file_id}\ndata: {when.isoformat()}\n"
                f"importato: {today.isoformat()}\n---\n")
        body = (f"# {title}\n\n## Riassunto Plaud\n\n{note or '_Nessun riassunto._'}\n\n"
                f"## Trascrizione\n\n{transcript or '_Vuota._'}\n")
        atomic_write(path, meta + body)
        return path

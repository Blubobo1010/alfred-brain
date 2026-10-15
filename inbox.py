"""Written updates dropped in a shared folder → ``Aggiornamenti/<date> <title>.md`` + a short report.

The folder is typically a SharePoint/OneDrive folder synced on the Mac mini where colleagues put a
weekly status update (plain text, Markdown or Word). Each file version is imported once (content
hash), kept verbatim in the vault, queued for consolidation, and summarised for the CEO.

Audio files in the folder (a spoken update) become voice memos (``voice.py``): transcribed in the
background, never waited for here, and reported on the next run together with any chat memo that
finished after its caller stopped waiting. The CEO's own memo folder (``ALFRED_BRAIN_VOICE``, e.g. a
OneDrive folder only he sees, for recordings too big for the chat) is scanned the same way, audio only:
his memos never go through the folder colleagues can open.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from . import voice
from .host import Host, as_list
from .vault import UPDATES_DIR, Vault, _lock, atomic_write, safe_filename

logger = logging.getLogger(__name__)

ENV = "ALFRED_BRAIN_INBOX"
VOICE_ENV = "ALFRED_BRAIN_VOICE"
SUFFIXES = (".txt", ".md", ".docx")
SETTLE_SECONDS = 120  # a file still being written or synced is picked up on the next run
MAX_CHARS = 40000
FLAG_RE = re.compile(r"da vedere|da decidere|da approvare|da firmare|decisione|con luca", re.I)
AUTHOR_RE = re.compile(r"^\s*(?:autore|da|from|author)\s*:\s*(.+)$", re.I | re.M)

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "novita": {"type": "array", "items": {"type": "string"}},
        "rischi": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["novita", "rischi"],
}

REPORT_INSTRUCTIONS = """Sei l'assistente di un amministratore delegato. Ricevi un aggiornamento scritto dai suoi
collaboratori su produzione, progettazione e altri temi. Prepara un riepilogo brevissimo per lui.

- novita: al massimo 5 novità più importanti (avanzamenti, consegne, contratti, preventivi arrivati).
- rischi: al massimo 5 ritardi, rischi alti, blocchi, date a rischio.
Una frase breve per punto, in italiano, che inizia con il progetto o la vettura come scritti nel testo
(es. "Z20 V03: …"). Copia sigle, codici e date esattamente. Non inventare nulla e non ripetere lo
stesso punto in due liste. Liste vuote se non c'è niente.
"""


def inbox_path() -> Path | None:
    value = os.environ.get(ENV, "").strip()
    return Path(value).expanduser() if value else None


def voice_path() -> Path | None:
    value = os.environ.get(VOICE_ENV, "").strip()
    return Path(value).expanduser() if value else None


def read_text(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        return _docx_text(path)
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):  # Windows Notepad / Word "testo" exports are often cp1252
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _docx_text(path: Path) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as zf:
        root = ElementTree.fromstring(zf.read("word/document.xml"))
    lines = []
    for para in root.iter(ns + "p"):
        text = "".join(t.text or "" for t in para.iter(ns + "t"))
        bullet = para.find(f"{ns}pPr/{ns}numPr") is not None
        lines.append(("- " if bullet and text.strip() else "") + text)
    return "\n".join(lines)


def candidates(folder: Path, now: float | None = None, suffixes: tuple[str, ...] = SUFFIXES) -> list[Path]:
    now = now or time.time()
    out = []
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).parts
        if any(p.startswith((".", "~$")) for p in rel) or path.suffix.lower() not in suffixes or not path.is_file():
            continue
        try:
            if now - path.stat().st_mtime < SETTLE_SECONDS:
                continue
        except OSError:
            continue
        out.append(path)
    return out


def author_of(text: str, path: Path, folder: Path) -> str:
    m = AUTHOR_RE.search(text[:600])
    if m:
        return m.group(1).strip()[:60]
    parent = path.parent.relative_to(folder).parts
    return parent[0] if parent else ""


def flagged(text: str) -> list[str]:
    """Lines the authors marked for the CEO ("da vedere con Luca", decisions): found by keyword, not by
    the model, so they are never dropped. Prefixed with their section heading (a line ending with ':' or
    a line without a bullet followed by bullets)."""
    out, section = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        bullet = line[:1] in "-*•"
        if not bullet:
            section = line.rstrip(":").strip("# *")
            continue
        item = line.lstrip("-*• ").strip()
        if FLAG_RE.search(item):
            prefix = f"{section}: " if section and not item.lower().startswith(section.lower()) else ""
            out.append(prefix + item)
    return out


def title_of(text: str, path: Path) -> str:
    first = next((ln.strip(" :#*") for ln in text.splitlines() if ln.strip()), "")
    return first if 0 < len(first) <= 80 else path.stem


class Inbox:
    def __init__(self, host: Host, vault: Vault, folder: Path | None = None, voice_folder: Path | None = None):
        self.host, self.vault = host, vault
        self.folder = folder or inbox_path()
        self.voice_folder = voice_folder or voice_path()

    def sync(self, today: date | None = None, now: float | None = None) -> dict[str, Any]:
        today = today or date.today()
        folders = [f for f in (self.folder, self.voice_folder) if f is not None]
        if not folders:
            raise ValueError(f"no folder configured: run `alfred brain inbox --path <folder>` ({ENV}) "
                             f"or `--voice-path <folder>` ({VOICE_ENV})")
        missing = [f for f in folders if not f.is_dir()]
        if len(missing) == len(folders):
            raise ValueError(f"folder not found: {missing[0]} (is it synced on this Mac?)")
        report: dict[str, Any] = {"new": 0, "skipped": 0, "errors": [], "notes": [], "report": ""}
        if missing:  # e.g. one OneDrive folder not synced yet: the other one still runs
            report["missing"] = [str(f) for f in missing]
            logger.warning("brain: folder not found: %s", ", ".join(report["missing"]))
        imported = self._import_texts(today, now, report) if self.folder and self.folder not in missing else []
        with _lock:
            state = self.vault.state()
            state["inbox_last_sync"] = datetime.now().isoformat(timespec="seconds")
            self.vault.save_state(state)
        # The CEO's own folder first: if it sits inside the updates folder, its recordings stay his.
        roots = [(f, fonte) for f, fonte in ((self.voice_folder, voice.PERSONAL), (self.folder, voice.FOLDER))
                 if f is not None and f not in missing]
        pending = self._start_audio(now, roots)
        if imported:
            report["report"] = "\n\n".join(self._summary(note, text) for note, text in imported)
            self.vault.commit(f"Aggiornamenti: {report['new']} file")
        memos = voice.VoiceMemos(self.host, self.vault).pop_reports()  # audio done since the last run, chat memos too
        if memos:
            report["report"] = "\n\n".join(r for r in (report["report"], *memos) if r)
        report["voice"] = {"pending": pending, "running": voice.busy(), "reported": len(memos)}
        return report

    def _import_texts(self, today: date, now: float | None, report: dict[str, Any]) -> list[tuple[Path, str]]:
        """Colleagues' written updates: each file version once, kept verbatim, queued for consolidation."""
        seen = set(self.vault.state().get("inbox_seen", []))
        imported: list[tuple[Path, str]] = []
        for path in candidates(self.folder, now):
            if self.voice_folder and path.is_relative_to(self.voice_folder):
                continue  # the CEO's own folder inside this one: audio only, never a colleague's update
            try:
                text = read_text(path)
            except (OSError, zipfile.BadZipFile, ElementTree.ParseError, KeyError) as exc:
                report["errors"].append(f"{path.name}: {exc}")  # e.g. an online-only file not downloaded yet
                continue
            digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]
            if digest in seen or not text.strip():
                report["skipped"] += 1
                continue
            note = self._write(path, text, digest, today)
            with _lock:
                state = self.vault.state()
                state.setdefault("inbox_seen", []).append(digest)
                state.setdefault("inbox_pending", []).append(self.vault.rel(note))
                self.vault.save_state(state)
            seen.add(digest)
            imported.append((note, text))
            report["new"] += 1
            report["notes"].append(self.vault.rel(note))
        return imported

    def _start_audio(self, now: float | None, roots: list[tuple[Path, str]]) -> int:
        """Hand new audio files to one background pass (never waited for: a long recording takes minutes).
        Known files are skipped by size and date, without reading them. In the updates folder the author is
        the colleague's subfolder; in the CEO's own folder every recording is his."""
        known = self.vault.state().get("voice_files", {})
        items, keys = [], set()
        for folder, fonte in roots:
            for path in candidates(folder, now, voice.AUDIO_SUFFIXES):
                try:
                    key = voice.stat_key(path)
                except OSError:
                    continue
                if key not in known and key not in keys:  # one folder inside the other: listed once
                    keys.add(key)
                    items.append((path, author_of("", path, folder) if fonte == voice.FOLDER else "", key, fonte))
        voice.VoiceMemos(self.host, self.vault).drain(items)
        return len(items)

    def _write(self, path: Path, text: str, digest: str, today: date) -> Path:
        author = author_of(text, path, self.folder)
        title = title_of(text, path)
        name = f"{today.isoformat()} {safe_filename(title)}" + (f" - {safe_filename(author, 40)}" if author else "")
        note = self.vault.root / UPDATES_DIR / f"{name}.md"
        if note.exists():
            note = note.with_name(f"{note.stem} ({digest[:6]}).md")
        meta = (f"---\ntipo: aggiornamento\nfonte: cartella\nfile: {path.name}\n"
                + (f"autore: {author}\n" if author else "")
                + f"data: {today.isoformat()}\nhash: {digest}\n---\n")
        body = f"# {title}\n\n{text.strip()[:MAX_CHARS]}\n"
        atomic_write(note, meta + body)
        return note

    def _summary(self, note: Path, text: str) -> str:
        """The CEO's micro-report; if the model fails, a plain pointer to the note (never lost)."""
        link = f"[[{self.vault.rel(note)[:-3]}]]"
        header = f"Aggiornamento {note.stem}"
        try:
            parsed = self.host.llm_json(REPORT_INSTRUCTIONS, text[:MAX_CHARS], REPORT_SCHEMA) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain: report for %s failed: %s", note.name, exc)
            parsed = {}
        sections = []
        lists = {"da_vedere": flagged(text), **{k: [str(x).strip() for x in as_list(parsed.get(k)) if str(x).strip()]
                                                for k in ("rischi", "novita")}}
        for key, label in (("da_vedere", "Da vedere"), ("rischi", "Rischi e ritardi"), ("novita", "Novità")):
            if lists[key]:
                sections.append(f"{label}:\n" + "\n".join(f"- {x}" for x in lists[key]))
        if not sections:
            return f"{header}: archiviato in {link} (riepilogo non disponibile)."
        return f"{header}\n\n" + "\n\n".join(sections) + f"\n\nTesto completo: {link}"

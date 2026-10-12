"""The daily journal: every conversation turn appended verbatim to ``Diario/<date>.md`` (no model).

Consolidation reads the journal from the byte offset it last processed (kept in the vault state),
so a crash or a skipped session end is caught up on the next run.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .vault import JOURNAL_DIR, Vault, _lock, atomic_write

MAX_CHARS = 6000  # per side of a turn: the journal stays readable, the model sees the gist
ENTRY_PREFIX = "### "


def journal_file(vault: Vault, when: datetime) -> Path:
    return vault.root / JOURNAL_DIR / f"{when.date().isoformat()}.md"


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS].rstrip() + " […]"


def append_turn(vault: Vault, user_text: str, assistant_text: str, *, author: str = "", platform: str = "",
                when: datetime | None = None) -> Path:
    when = when or datetime.now()
    path = journal_file(vault, when)
    who = author or "Utente"
    header = " · ".join(p for p in (when.strftime("%H:%M"), platform, who) if p)
    entry = f"{ENTRY_PREFIX}{header}\n\n**{who}:** {_clip(user_text)}\n\n**Alfred:** {_clip(assistant_text)}\n\n"
    with _lock:
        if not path.exists():
            atomic_write(path, f"# Diario {when.date().isoformat()}\n\n")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
    return path


def pending_chunks(vault: Vault, max_chars: int = 12000) -> list[tuple[Path, int, str]]:
    """Unconsolidated journal text as ``(file, end_offset, text)`` chunks, oldest first, split on
    entry boundaries so a chunk never cuts a turn in half."""
    offsets = vault.state().get("journal_offsets", {})
    out: list[tuple[Path, int, str]] = []
    for path in sorted((vault.root / JOURNAL_DIR).glob("*.md")):
        raw = path.read_bytes()
        start = int(offsets.get(vault.rel(path), 0))
        if start >= len(raw):
            continue
        text = raw[start:].decode("utf-8", errors="replace")
        pieces = text.split("\n" + ENTRY_PREFIX)
        chunk, consumed = "", start
        for i, piece in enumerate(pieces):
            part = piece if i == 0 else "\n" + ENTRY_PREFIX + piece
            if chunk and len(chunk) + len(part) > max_chars:
                out.append((path, consumed, chunk))
                chunk = ""
            chunk += part
            consumed += len(part.encode("utf-8"))
        if chunk.strip():
            out.append((path, consumed, chunk))
    return out


def mark_done(vault: Vault, path: Path, offset: int) -> None:
    with _lock:
        state = vault.state()
        state.setdefault("journal_offsets", {})[vault.rel(path)] = offset
        vault.save_state(state)

"""From raw text (journal turns, Plaud notes, folder updates, voice memos) to entity notes: the model extracts
durable facts about clients, people and projects; the merge into the vault is deterministic and keeps the source link."""

from __future__ import annotations

import logging
import re
import threading
from datetime import date
from pathlib import Path

from . import journal
from .host import Host
from .vault import ENTITY_DIRS, Vault, _lock

logger = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "entita": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": list(ENTITY_DIRS)},
                    "nome": {"type": "string"},
                    "alias": {"type": "array", "items": {"type": "string"}},
                    "fatti": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"testo": {"type": "string"}, "data": {"type": "string"}},
                        "required": ["testo"],
                    }},
                },
                "required": ["tipo", "nome", "fatti"],
            },
        },
    },
    "required": ["entita"],
}

INSTRUCTIONS = """Sei l'archivista di un amministratore delegato. Dal testo che segue estrai SOLO i fatti
durevoli e utili in futuro su clienti (aziende), persone e progetti: chi sono, ruoli, decisioni,
accordi, cifre, scadenze, preferenze, problemi aperti, prossimi passi.

Regole:
- Ignora saluti, chiacchiere, domande senza risposta e tutto ciò che riguarda Alfred stesso.
- Un fatto = una frase breve in italiano, autosufficiente (nomina il soggetto), senza opinioni tue.
- Date assolute (AAAA-MM-GG) quando il testo le dà o si ricavano da "oggi"; altrimenti lascia vuoto.
- tipo: "cliente" per aziende e organizzazioni (anche fornitori, assicurazioni, enti), "persona" solo per
  individui con un nome proprio, "progetto" per iniziative e prodotti.
- Per i progetti usa il nome breve del progetto o prodotto, senza reparto o fase ("Z20", non
  "Z20 Produzione" né "Z20 Progettazione").
- Usa il nome più completo che compare; varianti e abbreviazioni vanno in alias.
- Non inventare nulla. Se non c'è niente da salvare restituisci {"entita": []}.
"""

TRANSCRIPT_HEADING = "## Trascrizione"
MAX_SOURCE_CHARS = 12000
_run_lock = threading.Lock()  # one consolidation at a time (session end thread vs cron tool)

KNOWN_HINT = "Nomi già presenti nel vault (riusali se indicano la stessa cosa): "


class ConsolidationError(RuntimeError):
    pass


def extract(host: Host, vault: Vault, text: str, today: date) -> list[dict]:
    known = sorted({n.title for n in vault.entities()})[:200]
    prompt = f"Oggi è {today.isoformat()}.\n" + (KNOWN_HINT + "; ".join(known) + "\n\n" if known else "\n") + text
    parsed = host.llm_json(INSTRUCTIONS, prompt, SCHEMA)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("entita"), list):
        raise ConsolidationError("the model did not return the expected JSON")
    return [e for e in parsed["entita"] if isinstance(e, dict)]


def merge(vault: Vault, entities: list[dict], source: str, today: date) -> int:
    added = 0
    for ent in entities:
        tipo, nome = str(ent.get("tipo") or "").strip().lower(), str(ent.get("nome") or "").strip()
        if tipo not in ENTITY_DIRS or not nome:
            continue
        facts = [(str(f.get("testo") or ""), str(f.get("data") or "")[:10], source)
                 for f in ent.get("fatti") or [] if isinstance(f, dict)]
        alias = [str(a) for a in ent.get("alias") or [] if isinstance(a, str)]
        _path, n = vault.add_facts(tipo, nome, facts, alias=alias, today=today)
        added += n
    return added


def consolidate_text(host: Host, vault: Vault, text: str, source: str, today: date | None = None) -> int:
    today = today or date.today()
    return merge(vault, extract(host, vault, text, today), source, today)


def source_link(vault: Vault, path: Path) -> str:
    return f"[[{vault.rel(path)[:-3]}]]"


def run(host: Host, vault: Vault, today: date | None = None) -> dict:
    """Consolidate every pending journal chunk, Plaud note, folder update and voice memo. A chunk whose extraction
    fails stays pending (retried next run); the rest go on."""
    if not _run_lock.acquire(blocking=False):
        return {"chunks": 0, "facts": 0, "failed": 0, "busy": True}
    try:
        return _run(host, vault, today or date.today())
    finally:
        _run_lock.release()


def plaud_source_text(text: str) -> str:
    """A recording's Plaud summary and action items; the transcript only when there is no summary."""
    head, sep, transcript = text.partition("\n" + TRANSCRIPT_HEADING)
    has_summary = len(head.split("\n## ", 1)[-1].strip()) > 80
    return (head if has_summary or not sep else head + sep + transcript)[:MAX_SOURCE_CHARS]


def voice_source_text(text: str) -> str:
    """A voice memo: title, date and transcript when they fit (the facts as spoken), else its summary and actions
    like a Plaud recording. Never both: the same fact would be filed twice in different words."""
    head, sep, transcript = text.partition("\n" + TRANSCRIPT_HEADING)
    title = next((ln for ln in head.splitlines() if ln.startswith("# ")), "")
    recorded = re.search(r"^data: (\S+)", head, re.M)
    lead = title + (f"\nRegistrata il {recorded.group(1)}" if recorded else "")
    if sep and len(lead) + len(transcript) < MAX_SOURCE_CHARS:
        return lead + "\n\n" + transcript.strip()
    return head[:MAX_SOURCE_CHARS]


def _run(host: Host, vault: Vault, today: date) -> dict:
    report = {"chunks": 0, "facts": 0, "failed": 0}
    for path, offset, text in journal.pending_chunks(vault):
        try:
            report["facts"] += consolidate_text(host, vault, text, source_link(vault, path), today)
        except Exception as exc:  # noqa: BLE001 - one bad chunk must not block the others
            logger.warning("brain: consolidation of %s failed: %s", vault.rel(path), exc)
            report["failed"] += 1
            break  # offsets are sequential per file: stop here, retry from this chunk next time
        journal.mark_done(vault, path, offset)
        report["chunks"] += 1
    for key, to_text in (("plaud_pending", plaud_source_text), ("inbox_pending", lambda t: t[:MAX_SOURCE_CHARS]),
                         ("voice_pending", voice_source_text)):
        _run_pending(host, vault, today, key, to_text, report)
    if report["chunks"]:
        vault.write_index()
        with _lock:
            state = vault.state()
            state["last_consolidation"] = today.isoformat()
            vault.save_state(state)
        vault.commit(f"Consolidamento: {report['facts']} fatti da {report['chunks']} blocchi")
    return report


def _run_pending(host: Host, vault: Vault, today: date, key: str, to_text, report: dict) -> None:
    """Whole-note sources (Plaud recordings, folder updates, voice memos) queued under ``key`` in the vault state."""
    with _lock:
        pending = list(vault.state().get(key, []))
    for rel in pending:
        path = vault.root / rel
        if not path.exists():  # deleted by hand (or redone under a new name): nothing left to file
            logger.info("brain: %s no longer exists, dropped from %s", rel, key)
            with _lock:
                state = vault.state()
                state[key] = [p for p in state.get(key, []) if p != rel]
                vault.save_state(state)
            continue
        try:
            text = to_text(path.read_text(encoding="utf-8"))
            report["facts"] += consolidate_text(host, vault, text, source_link(vault, path), today)
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain: consolidation of %s failed: %s", rel, exc)
            report["failed"] += 1
            continue
        with _lock:
            state = vault.state()
            state[key] = [p for p in state.get(key, []) if p != rel]
            vault.save_state(state)
        report["chunks"] += 1

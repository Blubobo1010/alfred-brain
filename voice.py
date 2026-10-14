"""Voice memos → ``Note vocali/<date> <title>.md``: transcribed, classified and filed like a Plaud recording.

Recordings come from the chat (an iPhone voice memo shared with Alfred: ``brain_transcribe``), from the updates
folder (colleagues' audio, ``inbox.py``), from the CEO's own memo folder (a OneDrive folder only he sees, for
recordings too big for the chat) or from ``alfred brain transcribe``. Hermes' own speech-to-text
(``Host.transcribe``) turns them into text; the model gives a title, a type, an area, the topics, a summary and the
actions; the note keeps the full transcript and the audio never enters the vault.

Transcription runs on the CPU and a long memo takes minutes, more than a tool call may last (420 s by default).
So every recording is transcribed in a background thread, one at a time: a chat caller waits up to ``TOOL_WAIT``
and otherwise gets ``in_progress`` (asking again with the same file joins the job or returns its result). Every
finished memo is queued in ``voice_report`` until someone reports it: the caller that got the result, the next
call with the same file, or the updates report, which a memo nobody is waiting for moves to the next cron tick
(``Host.wake_inbox``) so it reaches the CEO in about a minute, not at the next half hour.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .consolidate import KNOWN_HINT, TRANSCRIPT_HEADING
from .host import Host, spawn_thread
from .vault import VOICE_DIR, Vault, _lock, atomic_write, meta_value, safe_filename

logger = logging.getLogger(__name__)

AUDIO_SUFFIXES = (".m4a", ".mp3", ".wav", ".aac", ".caf", ".ogg", ".oga", ".opus", ".flac", ".mpga")
CHAT_SUFFIXES = (*AUDIO_SUFFIXES, ".mp4", ".mpeg", ".webm")  # Hermes' STT formats; video containers only on request
TIPI = ("idea", "da fare", "incontro", "decisione", "promemoria", "aggiornamento", "altro")
AREE = ("commerciale", "produzione", "progettazione", "fornitori", "amministrazione", "personale", "altro")
FOLDER = "cartella"  # ``fonte`` of the colleagues' updates folder: the author and "da vedere" lines in the report
PERSONAL = "cartella personale"  # ``fonte`` of the CEO's own memo folder: his memos, reported like a chat memo
FOLDERS = (FOLDER, PERSONAL)  # never waited for: retried on the next runs, given up after MAX_ATTEMPTS
TOOL_WAIT = 240.0  # under Hermes' 420 s tool deadline, with room for the model call after the transcription
MAX_ATTEMPTS = 3  # a folder recording that keeps failing is reported once, then left alone
MAX_CHARS = 40000
MAX_REPORTS = 50
STT_HINT = ("Check `alfred brain status` (stt): faster-whisper installed (`alfred pm install --extra stt-whisper`), "
            "stt.provider and stt.language it.")
LATER_HINT = ("Long recording, still being transcribed in the background: tell the user it needs a few more minutes. "
              "When it is done the summary is sent with the updates report, if that is scheduled; if they ask before, "
              "call brain_transcribe again with the same path.")

SCHEMA = {
    "type": "object",
    "properties": {
        "titolo": {"type": "string"},
        "categoria": {"type": "string", "enum": list(TIPI)},
        "area": {"type": "string", "enum": list(AREE)},
        "argomenti": {"type": "array", "items": {"type": "string"}},
        "riassunto": {"type": "array", "items": {"type": "string"}},
        "azioni": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["titolo", "categoria", "area", "argomenti", "riassunto", "azioni"],
}

INSTRUCTIONS = """Sei l'assistente di un amministratore delegato. Ricevi la trascrizione automatica di una nota
vocale registrata al telefono, da lui o da un collaboratore. Classificala e riassumila per l'archivio.

- titolo: 3-8 parole che dicono di cosa parla (es. "Preventivo Beta per i pannelli Z20"), senza data.
- categoria: idea (proposta o spunto), da fare (compiti), incontro (resoconto di una riunione o di una
  telefonata), decisione (una scelta presa), promemoria (cosa ricordare), aggiornamento (stato di lavori o
  progetti), altro.
- area: commerciale (clienti, vendite, offerte), produzione, progettazione, fornitori (acquisti),
  amministrazione (finanza, budget, contratti), personale (dipendenti, assunzioni, formazione), altro.
- argomenti: clienti, aziende, fornitori, progetti e persone nominati, con il nome più completo (al massimo 5).
- riassunto: al massimo 5 frasi brevi con i fatti, le cifre e le date dette.
- azioni: le cose da fare dette, con chi e quando se detto (al massimo 5). Lista vuota se non ce ne sono.

Scrivi in italiano. Copia nomi, sigle, cifre e date come sono nella trascrizione e non inventare nulla: la
trascrizione automatica può contenere errori, non correggerli a fantasia.
"""


@dataclass
class Job:
    digest: str
    path: Path
    fonte: str
    autore: str = ""
    key: str = ""  # folder stat key, remembered once the recording is settled
    orphaned: bool = False  # the chat caller stopped waiting: the result goes out with the updates report
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] = field(default_factory=dict)


_jobs: dict[str, Job] = {}  # digest → running job, so a second request joins it instead of starting another
_jobs_lock = threading.Lock()  # never held together with vault._lock
_cpu = threading.Lock()  # one transcription at a time: the speech model is CPU-bound
_drain: threading.Thread | None = None  # the folder pass, at most one


def file_digest(path: Path) -> str:
    with path.open("rb") as fh:  # streamed: a long recording is tens of MB
        return hashlib.file_digest(fh, "sha256").hexdigest()[:16]


def stat_key(path: Path) -> str:
    st = path.stat()
    return f"{path}|{st.st_size}|{st.st_mtime_ns}"


def busy() -> bool:
    """A folder pass is still transcribing."""
    return _drain is not None and _drain.is_alive()


def wait_idle(timeout: float | None = None) -> bool:
    """Wait for the folder pass and every running job (the CLI before exiting, tests). True when idle."""
    deadline = None if timeout is None else time.monotonic() + timeout

    def left() -> float | None:
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    drain = _drain
    if drain is not None:
        drain.join(left())
    with _jobs_lock:
        jobs = list(_jobs.values())
    for job in jobs:
        job.done.wait(left())
    with _jobs_lock:
        return not busy() and not _jobs


def spoken_flags(text: str, limit: int = 5) -> list[str]:
    """Sentences with the update keywords ("da vedere con Luca", decisions): a spoken update has no bullets."""
    from .inbox import FLAG_RE
    out: list[str] = []
    for sentence in _sentences(text):
        if FLAG_RE.search(sentence) and sentence not in out:
            out.append(sentence)
    return out[:limit]


def paragraphs(text: str, per: int = 5) -> str:
    """A transcript arrives as one long line: five sentences per paragraph keep it readable in Obsidian."""
    sentences = _sentences(text)
    return "\n\n".join(" ".join(sentences[i:i + per]) for i in range(0, len(sentences), per))


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", " ".join((text or "").split())) if s.strip()]


def _register(job: Job) -> tuple[Job, bool]:
    with _jobs_lock:
        running = _jobs.get(job.digest)
        if running is not None:
            return running, False
        _jobs[job.digest] = job
        return job, True


class VoiceMemos:
    def __init__(self, host: Host, vault: Vault):
        self.host, self.vault = host, vault

    # ---- entry points ---------------------------------------------------------------------

    def run(self, path: Path, *, fonte: str, autore: str = "", wait: float | None = TOOL_WAIT) -> dict[str, Any]:
        """Transcribe and file one recording someone asked for (chat tool, CLI). ``wait=None`` waits for the end."""
        path = Path(path)
        if path.suffix.lower() not in CHAT_SUFFIXES:
            raise ValueError(f"not an audio file: {path.name} (accepted: {' '.join(CHAT_SUFFIXES)})")
        if not path.is_file():
            raise ValueError(f"file not found: {path} (chat attachments are kept for 24 hours)")
        digest = file_digest(path)
        known = self._known(digest)
        if known is not None:
            return known
        job, new = _register(Job(digest, path, fonte, autore))
        if new:
            spawn_thread(self._execute, name="brain-voice", args=(job,)).start()
        if not job.done.wait(wait):
            job.orphaned = True
            return {"ok": True, "status": "in_progress", "file": path.name, "hint": LATER_HINT}
        self.pop_reports(digest)  # this caller tells the user: not again in the updates report
        return job.result

    def drain(self, items: list[tuple[Path, str, str, str]]) -> bool:
        """Start one background pass over new folder recordings ``(path, author, stat key, fonte)``. False when a
        pass is already running: the files are still new on the next run."""
        global _drain
        if not items:
            return False
        with _jobs_lock:
            if busy():
                return False
            _drain = spawn_thread(self._drain_items, name="brain-voice-drain", args=(list(items),))
            _drain.start()
        return True

    def pop_reports(self, digest: str | None = None) -> list[str]:
        """Take the queued reports (all, or one recording's) out of the queue: whoever takes them tells the user."""
        with _lock:
            state = self.vault.state()
            queue = state.get("voice_report", [])
            taken = [e for e in queue if digest is None or e.get("hash") == digest]
            if taken:
                state["voice_report"] = [e for e in queue if e not in taken]
                self.vault.save_state(state)
        return [str(e.get("testo") or "") for e in taken]

    # ---- jobs -----------------------------------------------------------------------------

    def _known(self, digest: str) -> dict[str, Any] | None:
        """The answer for a recording already filed, or None when it must be done. A request redoes one without
        speech or whose note was deleted (the folder leaves those alone: ``_settled``)."""
        filed = self._filed(digest)
        if filed is None:
            return None
        queued = self.pop_reports(digest)
        if queued:  # finished after its caller stopped waiting: this is the first time the user hears of it
            return {**filed, "status": "done", "report": queued[-1]}
        return filed

    def _drain_items(self, items: list[tuple[Path, str, str, str]]) -> None:
        for path, autore, key, fonte in items:
            try:
                digest = file_digest(path)
            except OSError as exc:  # e.g. an online-only OneDrive file not downloaded yet: next run
                logger.info("brain: %s not readable yet: %s", path.name, exc)
                continue
            if self._settled(digest, key):
                continue
            job, new = _register(Job(digest, path, fonte, autore, key=key))
            if new:
                self._execute(job)
            else:
                job.done.wait()  # a chat request is already transcribing the same recording

    def _settled(self, digest: str, key: str) -> bool:
        """A folder file already filed, without speech, or given up on: remember its stat key and skip it."""
        with _lock:
            state = self.vault.state()
            failed = state.get("voice_failed", {}).get(digest) or {}
            if digest not in state.get("voice_seen", {}) and int(failed.get("tentativi", 0)) < MAX_ATTEMPTS:
                return False
            state.setdefault("voice_files", {})[key] = digest
            self.vault.save_state(state)
            return True

    def _execute(self, job: Job) -> None:
        try:
            result = self._process(job)
        except Exception as exc:  # noqa: BLE001 - every job ends with an answer, never with a silent thread
            error = f"{exc.__class__.__name__}: {str(exc)[:300]}"
            logger.warning("brain: voice memo %s failed: %s", job.path.name, error)
            result = {"ok": False, "error": error, "file": job.path.name, "hint": STT_HINT}
            self._failed(job, error)
        with _jobs_lock:
            _jobs.pop(job.digest, None)
        job.result = result
        job.done.set()
        if job.fonte in FOLDERS or job.orphaned:  # nobody is waiting: the updates report carries it now
            self.host.wake_inbox()

    def _failed(self, job: Job, error: str) -> None:
        """Chat and CLI: the failure is reported (the user may have stopped waiting). Folders: retried on the next
        runs, reported once when it gives up."""
        if job.fonte not in FOLDERS:
            self._queue_report(job.digest, "", f"Nota vocale {job.path.name}: trascrizione non riuscita ({error}).")
            return
        with _lock:
            state = self.vault.state()
            entry = state.setdefault("voice_failed", {}).setdefault(job.digest, {"file": job.path.name, "tentativi": 0})
            entry.update(errore=error, tentativi=int(entry.get("tentativi", 0)) + 1)
            gave_up = entry["tentativi"] >= MAX_ATTEMPTS
            if gave_up and job.key:
                state.setdefault("voice_files", {})[job.key] = job.digest
            self.vault.save_state(state)
        if gave_up:
            self._queue_report(job.digest, "", f"Nota vocale {job.path.name}: trascrizione non riuscita dopo "
                                               f"{MAX_ATTEMPTS} tentativi ({error}). Il file resta nella cartella.")

    def _process(self, job: Job) -> dict[str, Any]:
        filed = self._filed(job.digest)
        if filed:  # finished by another job between the caller's check and this one's start
            return filed
        with _cpu:
            transcript = self.host.transcribe(job.path)
        if not transcript.strip():
            report = f"Nota vocale {job.path.name}: nessun parlato riconosciuto, non archiviata."
            self._file(job, "", report)
            return {"ok": True, "status": "no_speech", "file": job.path.name, "report": report}
        info = self._classify(transcript)  # the model call stays outside the vault lock
        with _lock:
            filed = self._filed(job.digest)
            if filed:  # filed meanwhile by another process (CLI vs gateway)
                return filed
            note = self._write(job, transcript, info)
            rel = self.vault.rel(note)
            report = self._report_text(job, rel, info, transcript)
            self._file(job, rel, report)
        self.vault.commit(f"Nota vocale: {note.stem}")
        return {"ok": True, "status": "done", "note": rel, "categoria": info["categoria"], "area": info["area"],
                "report": report}

    def _filed(self, digest: str) -> dict[str, Any] | None:
        rel = self.vault.state().get("voice_seen", {}).get(digest)
        if not rel or not (self.vault.root / rel).exists():
            return None
        return {"ok": True, "status": "duplicate", "note": rel, "report": f"Nota vocale già archiviata: [[{rel[:-3]}]]"}

    def _file(self, job: Job, rel: str, report: str) -> None:
        """Mark the recording as filed (``rel`` "" = no speech), queue it for consolidation and for the report."""
        with _lock:
            state = self.vault.state()
            state.setdefault("voice_seen", {})[job.digest] = rel
            if rel:
                state.setdefault("voice_pending", []).append(rel)
            queue = [*state.get("voice_report", []), {"hash": job.digest, "nota": rel, "testo": report}]
            state["voice_report"] = queue[-MAX_REPORTS:]
            state.get("voice_failed", {}).pop(job.digest, None)
            if job.key:
                state.setdefault("voice_files", {})[job.key] = job.digest
            self.vault.save_state(state)

    def _queue_report(self, digest: str, rel: str, text: str) -> None:
        with _lock:
            state = self.vault.state()
            queue = [*state.get("voice_report", []), {"hash": digest, "nota": rel, "testo": text}]
            state["voice_report"] = queue[-MAX_REPORTS:]
            self.vault.save_state(state)

    # ---- the note -------------------------------------------------------------------------

    def _classify(self, transcript: str) -> dict[str, Any]:
        """Title, type, area, topics, summary and actions; if the model fails the memo is still filed."""
        known = sorted({n.title for n in self.vault.entities()})[:200]
        prompt = (KNOWN_HINT + "; ".join(known) + "\n\n" if known else "") + transcript[:MAX_CHARS]
        try:
            parsed = self.host.llm_json(INSTRUCTIONS, prompt, SCHEMA) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain: classifying a voice memo failed: %s", exc)
            parsed = {}

        def choice(value: Any, allowed: tuple[str, ...]) -> str:
            value = str(value or "").strip().lower()
            return value if value in allowed else "altro"

        def items(value: Any, limit: int = 5) -> list[str]:
            values = value if isinstance(value, list) else []
            return [" ".join(str(v).split()) for v in values if str(v).strip()][:limit]

        title = " ".join(str(parsed.get("titolo") or "").replace("#", " ").split())[:80]
        if not title:
            words = transcript.split()
            title = " ".join(words[:8]).rstrip(".,;:!?") + ("…" if len(words) > 8 else "")
        return {"titolo": title, "categoria": choice(parsed.get("categoria"), TIPI), "area": choice(parsed.get("area"), AREE),
                "argomenti": items(parsed.get("argomenti")), "riassunto": items(parsed.get("riassunto")),
                "azioni": items(parsed.get("azioni"))}

    def _write(self, job: Job, transcript: str, info: dict[str, Any]) -> Path:
        today = date.today()
        try:
            when = min(date.fromtimestamp(job.path.stat().st_mtime), today)
        except OSError:
            when = today
        note = self.vault.root / VOICE_DIR / f"{when.isoformat()} {safe_filename(info['titolo'])}.md"
        if note.exists():
            note = note.with_name(f"{note.stem} ({job.digest[:6]}).md")
        topics = [t for t in (meta_value(a) for a in info["argomenti"]) if t]
        tags = ["nota-vocale", *(v.replace(" ", "-") for v in (info["categoria"], info["area"]))]
        meta = ["---", "tipo: nota vocale", f"fonte: {meta_value(job.fonte)}", f"file: {meta_value(job.path.name, 120)}",
                *([f"autore: {meta_value(job.autore)}"] if job.autore else []),
                f"data: {when.isoformat()}", f"importato: {today.isoformat()}", f"categoria: {info['categoria']}",
                f"area: {info['area']}", f"argomenti: [{', '.join(topics)}]", f"tags: [{', '.join(tags)}]",
                f"hash: {job.digest}", "---", ""]
        line = " · ".join([f"Categoria: {info['categoria']}", f"Area: {info['area']}",
                           *([f"Argomenti: {', '.join(info['argomenti'])}"] if info["argomenti"] else []),
                           *([f"Autore: {job.autore}"] if job.autore else [])])
        summary = "\n".join(f"- {x}" for x in info["riassunto"]) or "_Riassunto non disponibile._"
        actions = "\n".join(f"- {x}" for x in info["azioni"]) or "_Niente da fare._"
        body = (f"# {info['titolo']}\n\n{line}\n\n## Riassunto\n\n{summary}\n\n## Da fare\n\n{actions}\n\n"
                f"{TRANSCRIPT_HEADING}\n\n{paragraphs(transcript)}\n")
        atomic_write(note, "\n".join(meta) + body)
        return note

    def _report_text(self, job: Job, rel: str, info: dict[str, Any], transcript: str) -> str:
        """What the CEO reads: the chat reply material, or a block of the updates report."""
        who = f" di {job.autore}" if job.autore and job.fonte == FOLDER else ""  # chat, own folder: the CEO's memo
        parts = [f"Nota vocale{who} «{info['titolo']}» ({info['categoria']} · {info['area']})"]
        parts.append("\n".join(f"- {x}" for x in info["riassunto"]) or "(riassunto non disponibile)")
        if info["azioni"]:
            parts.append("Da fare:\n" + "\n".join(f"- {x}" for x in info["azioni"]))
        flags = spoken_flags(transcript) if job.fonte == FOLDER else []
        if flags:
            parts.append("Da vedere:\n" + "\n".join(f"- {x}" for x in flags))
        parts.append(f"Testo completo: [[{rel[:-3]}]]")
        return "\n\n".join(parts)

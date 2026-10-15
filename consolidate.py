"""From raw text (journal turns, Plaud notes, folder updates, voice memos) to entity notes: the model extracts
durable facts about clients, people and projects; the merge into the vault is deterministic and keeps the source link.

New items are linked by a background pass (``kick``): one worker per vault that waits until the local model has been
idle for a while before each call, so it never slows the CEO's chat. Tools, the nightly job and session ends only
kick it; where there is no background (the CLI) ``link`` runs the pass right away. One pass at a time, also across
processes (CLI and gateway): an ``flock`` on ``.alfred/consolidate.lock``.

Failures: when the model fails twice in a row the pass stops (it is down, or answers garbage to everything). A
timeout or a connection error is never counted against a source. A source whose answers stay unusable is skipped
after ``MAX_ATTEMPTS``, but only once the model has worked on another source since it first failed: a model that
answers garbage to everything must not wipe the backlog. Sources that failed before are tried last, so a bad one
never holds back the others; ``brain status`` lists what was skipped.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any

from . import journal
from .host import Host, as_list, spawn_thread
from .vault import STATE_DIR, Vault, _lock, normalize

try:
    import fcntl
except ImportError:  # Windows: one pass at a time within the process only
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Strict on purpose: on a local server the schema is the decoding grammar, so lists and required fields guide a small
# model. An answer outside it (a provider that does not enforce it) is asked again without constraints
# (``HermesHost.llm_json``), and ``entity_type``/``merge`` normalise what comes back: synonyms, bare strings.
SCHEMA = {
    "type": "object",
    "properties": {
        "entita": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tipo": {"type": "string", "enum": ["cliente", "persona", "progetto"]},
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

TIPI = {"cliente": "cliente", "azienda": "cliente", "societa": "cliente", "organizzazione": "cliente",
        "fornitore": "cliente", "ente": "cliente", "impresa": "cliente", "persona": "persona", "individuo": "persona",
        "contatto": "persona", "progetto": "progetto", "prodotto": "progetto", "iniziativa": "progetto",
        "vettura": "progetto", "veicolo": "progetto"}

_TEMPLATE = """Sei l'archivista di un amministratore delegato. Dal testo che segue estrai SOLO i fatti
durevoli e utili in futuro su clienti (aziende), persone e progetti: {what}.

Regole:
{first}- Ignora saluti, chiacchiere, domande senza risposta e tutto ciò che riguarda Alfred stesso.
- Un fatto = una frase breve in italiano, autosufficiente (nomina il soggetto), senza opinioni tue.
- Date assolute (AAAA-MM-GG) quando il testo le dà o si ricavano da "oggi"; altrimenti lascia vuoto.
- tipo: "cliente" per aziende e organizzazioni (anche fornitori, assicurazioni, enti), "persona" solo per
  individui con un nome proprio, "progetto" per iniziative e prodotti.
- Per i progetti usa il nome breve del progetto o prodotto, senza reparto o fase ("Z20", non
  "Z20 Produzione" né "Z20 Progettazione").
- Usa il nome più completo che compare; varianti e abbreviazioni vanno in alias.
- Non inventare nulla. Se non c'è niente da salvare restituisci {{"entita": []}}.
"""
INSTRUCTIONS = _TEMPLATE.format(
    what="chi sono, ruoli, decisioni, accordi, cifre, scadenze, preferenze, problemi aperti, prossimi passi", first="")

# Voice memos: their actions are commitments (``impegni``), tracked and closed apart. As facts they would live in a
# third place, and the copy in a person's note would never be closed. The list must not name tasks or deadlines at
# all: a small model followed the list and ignored a later rule against it.
VOICE_INSTRUCTIONS = _TEMPLATE.format(
    what="chi sono, ruoli, decisioni prese, accordi, cifre, preferenze, problemi", first=(
        "- NON salvare le cose da fare (chi deve fare cosa, chi manda o chiama chi, entro quando): sono già\n"
        "  registrate a parte come impegni. Se il testo contiene solo cose da fare restituisci {\"entita\": []}.\n"))

TRANSCRIPT_HEADING = "## Trascrizione"
MAX_SOURCE_CHARS = 12000
KNOWN_HINT = "Nomi già presenti nel vault (riusali se indicano la stessa cosa): "

MAX_ATTEMPTS = 3  # unusable answers for the same source before it is skipped
MAX_MODEL_ERRORS = 2  # failures in a row that end a pass
MAX_SKIPPED = 50  # skipped sources remembered for ``brain status``
QUIET_SECONDS = 15.0  # the local model must have been idle this long before a background call
POLL_SECONDS = 5.0
MAX_QUIET_WAIT = 1800.0  # after this a background call goes ahead anyway
BUSY_RETRY = 60.0  # another process is linking the same vault: try again after this
LOCK_FILE = "consolidate.lock"


class ConsolidationError(RuntimeError):
    """The model answered, but not with usable JSON: counted against the source."""


ANSWER_ERRORS = (ConsolidationError, ValueError)  # ValueError: Hermes' schema check, a malformed structure


def counts_against_source(exc: BaseException) -> bool:
    """Would this failure repeat for this text? An unusable answer, or a request the server refuses for it (HTTP
    400/413/422, context too long). Timeouts, connections and server errors are the model's, never the source's."""
    if isinstance(exc, ANSWER_ERRORS):
        return True
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    name = exc.__class__.__name__.lower()
    return status in (400, 413, 422) or any(w in name for w in ("badrequest", "contextlength", "contextwindow"))


def entity_type(value: Any) -> str | None:
    return TIPI.get(normalize(str(value or "")))


def extract(host: Host, vault: Vault, text: str, today: date, *, tasks: bool = True) -> list[dict]:
    known = sorted({n.title for n in vault.entities()})[:200]
    prompt = f"Oggi è {today.isoformat()}.\n" + (KNOWN_HINT + "; ".join(known) + "\n\n" if known else "\n") + text
    parsed = host.llm_json(INSTRUCTIONS if tasks else VOICE_INSTRUCTIONS, prompt, SCHEMA)
    if not isinstance(parsed, dict) or "entita" not in parsed or not isinstance(parsed["entita"], (list, dict)):
        raise ConsolidationError("the model did not return the expected JSON")
    return [e for e in as_list(parsed["entita"]) if isinstance(e, dict)]


def merge(vault: Vault, entities: list[dict], source: str, today: date) -> int:
    added = 0
    for ent in entities:
        tipo, nome = entity_type(ent.get("tipo")), str(ent.get("nome") or "").strip()
        if tipo is None or not nome:
            continue
        facts = [(f.get("testo") or "", str(f.get("data") or "")[:10], source) if isinstance(f, dict) else (f, "", source)
                 for f in as_list(ent.get("fatti")) if isinstance(f, (dict, str))]
        alias = [a for a in as_list(ent.get("alias")) if isinstance(a, str)]
        _path, n = vault.add_facts(tipo, nome, [(str(t), d, s) for t, d, s in facts], alias=alias, today=today)
        added += n
    return added


def consolidate_text(host: Host, vault: Vault, text: str, source: str, today: date | None = None, *,
                     tasks: bool = True) -> int:
    today = today or date.today()
    return merge(vault, extract(host, vault, text, today, tasks=tasks), source, today)


def source_link(vault: Vault, path: Path) -> str:
    return f"[[{vault.rel(path)[:-3]}]]"


def plaud_source_text(text: str) -> str:
    """A recording's Plaud summary and action items; the transcript only when there is no summary."""
    head, sep, transcript = text.partition("\n" + TRANSCRIPT_HEADING)
    has_summary = len(head.split("\n## ", 1)[-1].strip()) > 80
    return (head if has_summary or not sep else head + sep + transcript)[:MAX_SOURCE_CHARS]


def voice_source_text(text: str) -> str:
    """A voice memo: title, date, author and transcript when they fit (the facts as spoken), else its summary like a
    Plaud recording. Never both: the same fact would be filed twice in different words."""
    head, sep, transcript = text.partition("\n" + TRANSCRIPT_HEADING)
    title = next((ln for ln in head.splitlines() if ln.startswith("# ")), "")
    lead = title + "".join(f"\n{label} {value}" for label, value in
                           (("Registrata il", _meta(head, "data")), ("Autore:", _meta(head, "autore"))) if value)
    if sep and len(lead) + len(transcript) < MAX_SOURCE_CHARS:
        return lead + "\n\n" + transcript.strip()
    return head[:MAX_SOURCE_CHARS]


def update_source_text(text: str) -> str:
    """A colleague's written update, with its author in front so that "io" is attributed."""
    author = _meta(text, "autore")
    return ((f"Autore: {author}\n" if author else "") + text)[:MAX_SOURCE_CHARS]


def _meta(text: str, key: str) -> str:
    if not text.startswith("---\n"):
        return ""
    for line in text[4:].split("\n---\n", 1)[0].splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip() == key:
            return value.strip()
    return ""


QUEUES = (("plaud_pending", plaud_source_text, True), ("inbox_pending", update_source_text, True),
          ("voice_pending", voice_source_text, False))


# ---- one pass -----------------------------------------------------------------------------


class _PassLock:
    """One pass per vault at a time: a thread lock in this process, an ``flock`` across processes."""

    _threads: dict[str, threading.Lock] = {}
    _guard = threading.Lock()

    def __init__(self, vault: Vault):
        self.vault = vault
        self._fh = None
        with _PassLock._guard:
            self._thread = _PassLock._threads.setdefault(str(vault.root), threading.Lock())

    def acquire(self, wait: float = 0) -> bool:
        deadline = time.monotonic() + wait
        if not (self._thread.acquire(timeout=wait) if wait > 0 else self._thread.acquire(blocking=False)):
            return False
        try:
            if fcntl is None or self._lock_file(deadline):
                return True
        except BaseException:  # whatever happens here, the thread lock must not stay taken: no pass would ever run
            self._thread.release()
            raise
        self._thread.release()
        return False

    def _lock_file(self, deadline: float) -> bool:
        try:  # cosmetic (keeps the lock file out of the vault's history): never a reason not to link
            self.vault.ignore_in_git(f"{STATE_DIR}/*.lock")
        except Exception as exc:  # noqa: BLE001
            logger.debug("brain: could not update the vault's .gitignore (%s)", exc)
        try:
            self._fh = open(self.vault.root / STATE_DIR / LOCK_FILE, "a")  # noqa: SIM115 - held until release()
        except OSError as exc:  # no lock file possible: run without the cross-process guard
            logger.debug("brain: no consolidation lock file (%s)", exc)
            return True
        while True:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    return False
                time.sleep(0.5)

    def release(self) -> None:
        try:
            if self._fh is not None:
                try:
                    fcntl.flock(self._fh, fcntl.LOCK_UN)
                finally:
                    self._fh.close()
                    self._fh = None
        finally:
            self._thread.release()


class _Guard:
    """Model failures in a row (the pass stops at ``MAX_MODEL_ERRORS``) and, for the background pass, the wait for an
    idle local model before each call."""

    def __init__(self, host: Host, quiet: bool):
        self.host, self.quiet, self.errors = host, quiet, 0

    @property
    def stopped(self) -> bool:
        return self.errors >= MAX_MODEL_ERRORS

    def wait_quiet(self) -> None:
        if not self.quiet:
            return
        give_up = time.monotonic() + MAX_QUIET_WAIT
        idle_since: float | None = None
        while time.monotonic() < give_up:
            busy = self.host.model_busy()
            if busy is None:  # no local model managed by Hermes: nothing to share
                return
            now = time.monotonic()
            if busy:
                idle_since = None
            else:
                idle_since = now if idle_since is None else idle_since
                if now - idle_since >= QUIET_SECONDS:
                    return
            time.sleep(POLL_SECONDS)


def run(host: Host, vault: Vault, today: date | None = None, *, wait: float = 0, quiet: bool = False) -> dict:
    """Consolidate every pending journal chunk, Plaud note, folder update and voice memo. ``wait``: seconds to wait for
    a pass already running (the CLI); ``quiet``: before each model call, wait for the local model to be idle (the
    background pass)."""
    lock = _PassLock(vault)
    if not lock.acquire(wait):
        return {"chunks": 0, "facts": 0, "failed": 0, "busy": True}
    try:
        return _run(host, vault, today or date.today(), _Guard(host, quiet))
    finally:
        lock.release()


def _run(host: Host, vault: Vault, today: date, guard: _Guard) -> dict:
    report: dict[str, Any] = {"chunks": 0, "facts": 0, "failed": 0}
    for unit in _units(vault):
        if guard.stopped:
            break
        unit(host, vault, today, guard, report)
    if report["chunks"] or report.get("skipped"):
        vault.write_index()
        with _lock:
            state = vault.state()
            state["last_consolidation"] = today.isoformat()
            vault.save_state(state)
        vault.commit(f"Consolidamento: {report['facts']} fatti da {report['chunks']} blocchi")
    if guard.stopped:
        report["stopped"] = True
    return report


def _tries(entry: Any) -> int:
    return int(entry.get("tentativi", 0)) if isinstance(entry, dict) else 0


def _units(vault: Vault) -> list:
    """Everything pending, as callables: one per journal file (its chunks in order) and one per queued note. Sources
    that failed before go last (stable sort), so a bad one never holds back fresh ones."""
    state = vault.state()
    attempts = state.get("consolidation_attempts", {})
    offsets = state.get("journal_offsets", {})
    files: dict[Path, list[tuple[int, int, str]]] = {}
    for path, end, text in journal.pending_chunks(vault):
        chunks = files.setdefault(path, [])
        start = chunks[-1][1] if chunks else int(offsets.get(vault.rel(path), 0))
        chunks.append((start, end, text))
    units = [(_tries(attempts.get(f"journal:{vault.rel(p)}@{c[0][0]}")), partial(_journal_unit, p, c)) for p, c in files.items()]
    for queue, to_text, tasks in QUEUES:
        units += [(_tries(attempts.get(f"{queue}:{rel}")), partial(_note_unit, queue, rel, to_text, tasks))
                  for rel in state.get(queue, [])]
    units.sort(key=lambda u: u[0])
    return [unit for _tries_, unit in units]


def _journal_unit(path: Path, chunks: list[tuple[int, int, str]], host: Host, vault: Vault, today: date,
                  guard: _Guard, report: dict) -> None:
    for start, end, text in chunks:
        if guard.stopped:
            return
        done = _attempt(guard, vault, f"journal:{vault.rel(path)}@{start}", f"{vault.rel(path)} (da byte {start})", report,
                        lambda text=text: consolidate_text(host, vault, text, source_link(vault, path), today))
        if done is None:  # offsets are sequential per file: the rest of it waits for this chunk
            return
        journal.mark_done(vault, path, end)


def _note_unit(queue: str, rel: str, to_text, tasks: bool, host: Host, vault: Vault, today: date, guard: _Guard,
               report: dict) -> None:
    """A whole-note source (Plaud recording, folder update, voice memo) queued under ``queue`` in the vault state."""
    path = vault.root / rel
    if path.exists():
        done = _attempt(guard, vault, f"{queue}:{rel}", rel, report,
                        lambda: consolidate_text(host, vault, to_text(path.read_text(encoding="utf-8")),
                                                 source_link(vault, path), today, tasks=tasks))
        if done is None:
            return
    else:  # deleted by hand (or redone under a new name): nothing left to file
        logger.info("brain: %s no longer exists, dropped from %s", rel, queue)
    with _lock:
        state = vault.state()
        state[queue] = [p for p in state.get(queue, []) if p != rel]
        vault.save_state(state)


def _attempt(guard: _Guard, vault: Vault, key: str, label: str, report: dict, work) -> bool | None:
    """Run one extraction. True: filed; False: skipped for good; None: failed, retried on a later pass."""
    guard.wait_quiet()
    try:
        report["facts"] += work()
    except Exception as exc:  # noqa: BLE001 - one bad source must not block the others
        guard.errors += 1
        report["failed"] += 1
        logger.warning("brain: consolidation of %s failed: %s", label, exc)
        if not counts_against_source(exc):  # timeout, connection: the model's fault, not the source's
            return None
        return _count_failure(vault, key, label, exc, report)
    guard.errors = 0
    report["chunks"] += 1
    with _lock:
        state = vault.state()
        state["last_extraction_ok"] = datetime.now().isoformat()
        state.get("consolidation_attempts", {}).pop(key, None)
        vault.save_state(state)
    return True


def _count_failure(vault: Vault, key: str, label: str, exc: Exception, report: dict) -> bool | None:
    now = datetime.now().isoformat()
    with _lock:
        state = vault.state()
        attempts = state.setdefault("consolidation_attempts", {})
        entry = attempts.get(key) if isinstance(attempts.get(key), dict) else {"tentativi": 0, "dal": now}
        entry["tentativi"] = int(entry.get("tentativi", 0)) + 1
        model_works = str(state.get("last_extraction_ok") or "") > str(entry.get("dal") or "")
        if entry["tentativi"] < MAX_ATTEMPTS or not model_works:
            attempts[key] = entry
            vault.save_state(state)
            return None
        attempts.pop(key, None)
        skipped = [*state.get("consolidation_skipped", []), {"fonte": label, "data": date.today().isoformat(),
                                                             "errore": f"{exc.__class__.__name__}: {str(exc)[:200]}"}]
        state["consolidation_skipped"] = skipped[-MAX_SKIPPED:]
        vault.save_state(state)
    report["skipped"] = report.get("skipped", 0) + 1
    return False


# ---- the background worker ----------------------------------------------------------------

_workers_lock = threading.Lock()
_queued: dict[str, tuple[Host, Vault]] = {}
_workers: dict[str, threading.Thread] = {}


def kick(host: Host, vault: Vault) -> bool:
    """Ask for a background pass on this vault; a no-op where the host has no background (the CLI). A kick while a
    pass runs gives one more pass, so nothing filed meanwhile waits for the night. One worker per vault, started
    with the kicker's Hermes context (profile scope)."""
    if not getattr(host, "background", True):
        return False
    root = str(vault.root)
    with _workers_lock:
        _queued[root] = (host, vault)
        worker = _workers.get(root)
        if worker is None or not worker.is_alive():
            worker = spawn_thread(_work, name="brain-link", args=(root,))
            _workers[root] = worker
            worker.start()
    return True


def link(host: Host, vault: Vault, wait: float = 600) -> dict:
    """For the tools: a background pass in the gateway, the pass itself right away where there is no background."""
    if kick(host, vault):
        return {"background": True}
    return run(host, vault, wait=wait)


def _work(root: str) -> None:
    while True:
        with _workers_lock:
            item = _queued.pop(root, None)
            if item is None:
                _workers.pop(root, None)
                return
        host, vault = item
        try:
            report = run(host, vault, quiet=True)
        except Exception as exc:  # noqa: BLE001 - the worker never dies with work queued
            logger.warning("brain: background linking failed: %s", exc)
            continue
        if report.get("busy"):  # another process holds the vault: try again shortly
            time.sleep(BUSY_RETRY)
            with _workers_lock:
                _queued.setdefault(root, item)


def wait_idle(timeout: float | None = None) -> bool:
    """Wait for the background passes (tests, a process about to exit). True when none is left."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        with _workers_lock:
            workers = list(_workers.values())
        if not workers:
            return True
        for worker in workers:
            worker.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        if deadline is not None and time.monotonic() >= deadline:
            with _workers_lock:
                return not _workers

"""The Obsidian vault on disk: layout, entity notes with an Alfred-managed block, state, git history.

Alfred only ever rewrites the text between ``MANAGED_START`` and ``MANAGED_END`` of an entity note;
everything outside it belongs to the humans and is preserved byte for byte.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_PATH = "~/Alfred-Vault"
JOURNAL_DIR = "Diario"
PLAUD_DIR = "Plaud"
UPDATES_DIR = "Aggiornamenti"
ENTITY_DIRS = {"cliente": "Clienti", "persona": "Persone", "progetto": "Progetti"}
INDEX_NOTE = "Indice.md"
STATE_DIR = ".alfred"
MANAGED_START = "<!-- alfred:inizio -->"
MANAGED_END = "<!-- alfred:fine -->"
MANAGED_HEADING = "## Cosa sa Alfred"

README = """# Secondo cervello

Questo vault lo tiene aggiornato Alfred.

- **Diario/**: una nota al giorno con ogni conversazione, scritta così com'è.
- **Plaud/**: una nota per registrazione, con riassunto, azioni e trascrizione.
- **Aggiornamenti/**: gli aggiornamenti scritti dai collaboratori (cartella condivisa), così come arrivano.
- **Clienti/**, **Persone/**, **Progetti/**: cosa Alfred ha imparato, con la fonte di ogni informazione.

Nelle note di Clienti, Persone e Progetti, Alfred scrive solo nel riquadro tra
`<!-- alfred:inizio -->` e `<!-- alfred:fine -->`: tutto il resto è vostro e non viene mai toccato.
Ogni modifica di Alfred è registrata nella cronologia git del vault.
"""

_lock = threading.RLock()  # one writer per process: journal worker, consolidation thread and tools


def vault_path() -> Path:
    """``OBSIDIAN_VAULT_PATH`` (shared with the bundled obsidian skill), else ``~/Alfred-Vault``."""
    return Path(os.environ.get("OBSIDIAN_VAULT_PATH") or DEFAULT_PATH).expanduser()


def normalize(text: str) -> str:
    """Lowercase, no accents, single spaces: the key used to match names and deduplicate facts."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def safe_filename(name: str, limit: int = 80) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|#^\[\]\n\r\t]+', " ", name or "").strip(" .")
    return re.sub(r"\s+", " ", cleaned)[:limit].strip() or "Senza titolo"


@dataclass
class Note:
    path: Path
    meta: dict[str, Any] = field(default_factory=dict)
    title: str = ""
    body: str = ""  # everything after the frontmatter


def parse_note(path: Path) -> Note:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                key, sep, value = line.partition(":")
                if sep:
                    meta[key.strip()] = _parse_value(value.strip())
            body = text[end + 5:]
    heading = re.search(r"^# (.+)$", body, re.M)
    return Note(path, meta, heading.group(1).strip() if heading else path.stem, body)


def _parse_value(value: str) -> Any:
    if value.startswith("[") and value.endswith("]"):
        return [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
    return value.strip("\"'")


def _format_meta(meta: dict[str, Any]) -> str:
    lines = []
    for key, value in meta.items():
        if isinstance(value, list):
            value = "[" + ", ".join(str(v).replace(",", " ") for v in value) + "]"
        lines.append(f"{key}: {value}")
    return "---\n" + "\n".join(lines) + "\n---\n"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


class Vault:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).expanduser() if root else vault_path()

    # ---- layout -------------------------------------------------------------------------

    def exists(self) -> bool:
        return (self.root / STATE_DIR).is_dir()

    def init(self) -> bool:
        """Create the layout (idempotent). Returns True when the vault was new."""
        with _lock:
            new = not self.exists()
            for sub in (JOURNAL_DIR, PLAUD_DIR, UPDATES_DIR, *ENTITY_DIRS.values(), STATE_DIR):
                (self.root / sub).mkdir(parents=True, exist_ok=True)
            if not (self.root / "LEGGIMI.md").exists():
                atomic_write(self.root / "LEGGIMI.md", README)
            gitignore = self.root / ".gitignore"
            if not gitignore.exists():
                atomic_write(gitignore, ".obsidian/workspace*\n.trash/\n.DS_Store\n")
            if shutil.which("git") and not (self.root / ".git").exists():
                subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=False)
            self.write_index()
            self.commit("Vault creato")
            return new

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def resolve(self, rel_path: str) -> Path:
        """A vault-relative path, refusing anything that escapes the vault or touches its internals."""
        path = (self.root / rel_path).resolve()
        root = self.root.resolve()
        if root not in path.parents and path != root:
            raise ValueError(f"{rel_path!r} is outside the vault")
        if path.relative_to(root).parts[:1] in ((STATE_DIR,), (".git",)):
            raise ValueError(f"{rel_path!r} is internal to the vault")
        return path

    def notes(self) -> list[Path]:
        skip = {STATE_DIR, ".git", ".obsidian", ".trash"}
        return sorted(p for p in self.root.rglob("*.md") if not skip & set(p.relative_to(self.root).parts))

    # ---- state ----------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        path = self.root / STATE_DIR / "state.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_state(self, state: dict[str, Any]) -> None:
        atomic_write(self.root / STATE_DIR / "state.json", json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))

    # ---- entity notes ---------------------------------------------------------------------

    def entities(self) -> list[Note]:
        out = []
        for folder in ENTITY_DIRS.values():
            out.extend(parse_note(p) for p in sorted((self.root / folder).glob("*.md")))
        return out

    def find_entity(self, name: str, tipo: str | None = None) -> Note | None:
        key = normalize(name)
        if not key:
            return None
        for note in self.entities():
            if tipo and note.meta.get("tipo") != tipo:
                continue
            names = [note.title, note.path.stem, *(note.meta.get("alias") or [])]
            if key in {normalize(n) for n in names if n}:
                return note
        return None

    def add_facts(self, tipo: str, nome: str, facts: list[tuple[str, str, str]], alias: list[str] | None = None,
                  today: date | None = None) -> tuple[Path, int]:
        """Add ``(text, date, source_link)`` facts to the entity's managed block, creating the note if
        needed. Facts already present (same normalised text) are skipped. Returns (path, added)."""
        if tipo not in ENTITY_DIRS:
            raise ValueError(f"tipo must be one of {', '.join(ENTITY_DIRS)}")
        today = today or date.today()
        with _lock:
            note = self.find_entity(nome, tipo) or self.find_entity(nome)
            if note is None:
                path = self.root / ENTITY_DIRS[tipo] / f"{safe_filename(nome)}.md"
                meta = {"tipo": tipo, "alias": [], "aggiornato": today.isoformat()}
                body = f"# {nome.strip()}\n\n{MANAGED_START}\n{MANAGED_HEADING}\n{MANAGED_END}\n"
                note = Note(path, meta, nome.strip(), body)
            before, managed, after = _split_managed(note.body)
            lines = [ln for ln in managed.splitlines() if ln.strip() and ln.strip() != MANAGED_HEADING]
            known = {normalize(_fact_text(ln)) for ln in lines}
            added = 0
            for text, when, source in facts:
                text = " ".join((text or "").split())
                if not text or normalize(text) in known:
                    continue
                known.add(normalize(text))
                lines.append(f"- {when or today.isoformat()} — {text}" + (f" ({source})" if source else ""))
                added += 1
            aliases = list(note.meta.get("alias") or [])
            for a in alias or []:
                if a and normalize(a) not in {normalize(x) for x in [note.title, *aliases]}:
                    aliases.append(a.strip())
            if not added and aliases == list(note.meta.get("alias") or []) and note.path.exists():
                return note.path, 0
            note.meta.update({"tipo": note.meta.get("tipo", tipo), "alias": aliases, "aggiornato": today.isoformat()})
            block = "\n".join([MANAGED_START, MANAGED_HEADING, *lines, MANAGED_END])
            atomic_write(note.path, _format_meta(note.meta) + before + block + after)
            return note.path, added

    def write_index(self) -> None:
        sections = ["# Indice", "", "Mappa delle note che Alfred tiene aggiornate.", ""]
        for folder in ENTITY_DIRS.values():
            names = sorted((p.stem for p in (self.root / folder).glob("*.md")), key=str.lower)
            sections += [f"## {folder}", *([f"- [[{folder}/{n}|{n}]]" for n in names] or ["- (nessuna)"]), ""]
        atomic_write(self.root / INDEX_NOTE, "\n".join(sections))

    # ---- history --------------------------------------------------------------------------

    def commit(self, message: str) -> bool:
        if not (self.root / ".git").exists() or not shutil.which("git"):
            return False
        with _lock:
            env = {**os.environ, "GIT_AUTHOR_NAME": "Alfred", "GIT_AUTHOR_EMAIL": "alfred@localhost",
                   "GIT_COMMITTER_NAME": "Alfred", "GIT_COMMITTER_EMAIL": "alfred@localhost"}
            subprocess.run(["git", "add", "-A"], cwd=self.root, check=False, capture_output=True, env=env)
            done = subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.root, check=False,
                                  capture_output=True, env=env)
            return done.returncode == 0


def _split_managed(body: str) -> tuple[str, str, str]:
    start, end = body.find(MANAGED_START), body.find(MANAGED_END)
    if start == -1 or end == -1 or end < start:  # no block yet (or a broken one): append a fresh block
        sep = "" if body.endswith("\n\n") else ("\n" if body.endswith("\n") else "\n\n")
        return body + sep, "", "\n"
    return body[:start], body[start + len(MANAGED_START):end], body[end + len(MANAGED_END):]


def _fact_text(line: str) -> str:
    text = line.lstrip("- ").split(" — ", 1)[-1]
    return re.sub(r"\s*\((\[\[[^)]*\]\])\)\s*$", "", text)

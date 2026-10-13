"""Model-free recall over the vault: names and aliases first, then a small BM25 over note text.

The index is rebuilt incrementally from file modification times, so a note edited by hand in
Obsidian is searchable on the next turn.
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .vault import ENTITY_DIRS, MANAGED_END, MANAGED_START, Vault, normalize, parse_note

STOPWORDS = frozenset("""
a ad al alla alle allo agli ai anche che chi ci con cosa come da dal dalla dei del della delle dello di e ed
gli ha hai hanno ho il in io la le lei li lo lui ma mi ne nel nella noi non o per perche piu poi qual quale
quando questo questa quello quella se si sia sono su sua suo sul sulla ti tra tu tutto un una uno vi voi
the and of to is in for on with what who how
""".split())
FIELD_BOOST = 3.0  # a query word in the title or an alias weighs like three body hits


@dataclass
class Doc:
    rel: str
    title: str
    names: list[str]  # normalised title + aliases
    kind: str  # cliente | persona | progetto | diario | plaud | aggiornamento | nota
    terms: Counter
    length: int
    mtime: float


def tokens(text: str) -> list[str]:
    return [t for t in normalize(text).split() if len(t) > 2 and t not in STOPWORDS]


class Index:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.docs: dict[str, Doc] = {}
        self._lock = threading.Lock()

    def refresh(self) -> None:
        with self._lock:
            seen = set()
            for path in self.vault.notes():
                rel = self.vault.rel(path)
                seen.add(rel)
                mtime = path.stat().st_mtime
                if rel in self.docs and self.docs[rel].mtime == mtime:
                    continue
                self.docs[rel] = self._doc(path, rel, mtime)
            for rel in set(self.docs) - seen:
                del self.docs[rel]

    def _doc(self, path: Path, rel: str, mtime: float) -> Doc:
        note = parse_note(path)
        top = rel.split("/", 1)[0]
        kind = next((t for t, folder in ENTITY_DIRS.items() if folder == top), None)
        kind = kind or {"Diario": "diario", "Plaud": "plaud", "Aggiornamenti": "aggiornamento"}.get(top, "nota")
        names = [normalize(n) for n in [note.title, *(note.meta.get("alias") or [])] if normalize(n)]
        terms = Counter(tokens(note.title + " " + " ".join(note.meta.get("alias") or []) + " " + note.body))
        return Doc(rel, note.title, names, kind, terms, sum(terms.values()) or 1, mtime)

    def search(self, query: str, limit: int = 5) -> list[tuple[float, Doc]]:
        self.refresh()
        q_norm = f" {normalize(query)} "
        q_terms = set(tokens(query))
        if not q_terms and not q_norm.strip():
            return []
        docs = list(self.docs.values())
        n = len(docs) or 1
        avg = sum(d.length for d in docs) / n
        df = Counter(t for d in docs for t in q_terms if t in d.terms)
        scored = []
        for d in docs:
            score = 0.0
            if any(f" {name} " in q_norm for name in d.names):  # the query names this note
                score += 10.0
            name_terms = {t for name in d.names for t in name.split()}
            for t in q_terms:
                tf = d.terms.get(t, 0) + (FIELD_BOOST if t in name_terms else 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * d.length / avg))
            if score > 0:
                if d.kind in ENTITY_DIRS:
                    score *= 1.3  # curated knowledge before raw journal text
                scored.append((score, d))
        scored.sort(key=lambda s: (-s[0], s[1].rel))
        return scored[:limit]

    def excerpt(self, doc: Doc, query: str, max_chars: int = 600) -> str:
        path = self.vault.root / doc.rel
        try:
            note = parse_note(path)
        except OSError:
            return ""
        body = note.body
        if MANAGED_START in body and MANAGED_END in body:  # entity notes: the latest facts
            block = body.split(MANAGED_START, 1)[1].split(MANAGED_END, 1)[0]
            facts = [ln for ln in block.splitlines() if ln.startswith("- ")]
            human = re.sub(r"\s+", " ", body.split(MANAGED_START, 1)[0].replace(f"# {note.title}", "")).strip()
            text = "\n".join(([human[:200]] if human else []) + facts[-8:])
            return text[-max_chars:]
        q = tokens(query)
        flat = re.sub(r"\s+", " ", body)
        low = normalize(flat)
        hit = min((low.find(t) for t in q if low.find(t) >= 0), default=0)
        start = max(0, hit - max_chars // 3)
        return ("…" if start else "") + flat[start:start + max_chars].strip()


def recall_block(index: Index, query: str, *, limit: int = 3, max_chars: int = 1500) -> tuple[str, int]:
    """The ``## Dal secondo cervello`` block injected before a turn, and how many notes it cites."""
    hits = [(s, d) for s, d in index.search(query, limit=limit + 2) if s >= 1.0][:limit]
    if not hits:
        return "", 0
    parts, used = ["## Dal secondo cervello (vault Obsidian)"], 0
    for _score, doc in hits:
        text = index.excerpt(doc, query, max_chars=max(200, (max_chars - used) // max(1, limit)))
        if not text:
            continue
        piece = f"**{doc.rel}**\n{text}"
        if used + len(piece) > max_chars:
            break
        parts.append(piece)
        used += len(piece)
    return ("\n\n".join(parts), len(parts) - 1) if len(parts) > 1 else ("", 0)


"""Commitments ("impegni"): who must do what, by when.

They are born in the model call that already classifies a voice memo (``voice.classify``) and in the chat
(``brain_impegni``). One source of truth, ``.alfred/impegni.json``, with progressive ids (#12) used everywhere:
prompt, report, tool and view. ``Impegni.md`` is a view rewritten after every change (like ``Indice.md``) and never
read back: changes go through Alfred.

A memo only adds commitments or matches open ones; nothing here closes a commitment by itself. Closing and
corrections come from the CEO in the chat, and every correction is counted: the correction rate and the share of
quotes found in the transcript are the quality signals of the model in use (``brain status``, ``brain eval``).
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from .host import DEFAULT_OWNER, as_list
from .index import STOPWORDS, tokens
from .vault import STATE_DIR, Vault, _lock, atomic_write, normalize

FILE = "impegni.json"
VIEW = "Impegni.md"
STATI = ("aperto", "fatto", "annullato")
KEEP_CLOSED_DAYS = 30
PROMPT_LIMIT = 40
PREFETCH_LIMIT = 5
EVIDENCE_RATIO = 0.6  # share of the quote's words found in the transcript
MATCH_RATIO = 0.5  # eval: share of the expected action's words in a predicted one
GIORNI = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")
GIORNI_BREVI = ("lun", "mar", "mer", "gio", "ven", "sab", "dom")
SPEAKER = frozenset({"io", "me", "mi", "noi"})  # the memo's author
OWNER = frozenset({"ceo", "il ceo", "l amministratore delegato", "amministratore delegato"})
ALFRED = frozenset({"alfred", "l assistente", "assistente"})
NOTICE = ("Questa nota la scrive Alfred e la riscrive a ogni modifica: per cambiare un impegno chiedilo in chat "
          "(«il 12 è fatto», «il 13 è di Marco, entro lunedì»).")
_TASK_MARKS = re.compile(r"[\U0001F4C5✅❌\U0001F501⏳\U0001F6EB➕\U0001F53A⏫\U0001F53C\U0001F53D⏬^#\[\]|]+")


class CommitmentError(ValueError):
    """A request the store cannot apply (unknown id, a date that is not a date)."""


# ---- small helpers ------------------------------------------------------------------------


def clean(value: Any, limit: int = 200) -> str:
    """Model or user text as one line, without the Obsidian Tasks fields, block ids and link brackets it would break."""
    text = _TASK_MARKS.sub(" ", str(value or ""))
    return " ".join(text.split())[:limit].strip(" .;,-")


def parse_due(value: Any) -> str:
    try:
        return date.fromisoformat(str(value or "").strip()[:10]).isoformat()
    except ValueError:
        return ""


def parse_ref(value: Any) -> int | None:
    match = re.search(r"\d+", str(value if value is not None else ""))
    return int(match.group()) if match and int(match.group()) > 0 else None


def day_label(iso: str) -> str:
    when = date.fromisoformat(iso)
    return f"{GIORNI_BREVI[when.weekday()]} {when.day:02d}/{when.month:02d}"


def calendar(when: date, days: int = 7) -> str:
    """'oggi (giovedì 15/10) = 2026-10-15; venerdì 16/10 = …; giovedì 22/10 = …; fine mese = 2026-10-31': today, then
    the next seven days, so every weekday name has exactly one date and it is never today (with two Mondays a small
    model picked the second one; a Monday memo saying "lunedì prossimo" means the next one)."""
    out = [f"oggi ({GIORNI[when.weekday()]} {when.day:02d}/{when.month:02d}) = {when.isoformat()}"]
    for i in range(1, days + 1):
        d = when + timedelta(days=i)
        out.append(f"{GIORNI[d.weekday()]} {d.day:02d}/{d.month:02d} = {d.isoformat()}")
    month_end = (when.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return "; ".join([*out, f"fine mese = {month_end.isoformat()}"])


def evidence_found(evidence: str, text: str) -> bool | None:
    """Is the model's quote in the transcript? None when it gave none."""
    quoted = set(tokens(evidence))
    if not quoted:
        return None
    return len(quoted & set(tokens(text))) / len(quoted) >= EVIDENCE_RATIO


def owner_keys(owner: str) -> set[str]:
    key = normalize(owner)
    keys = {key} if key else set()
    first = key.split()[0] if key else ""
    if len(first) > 2 and key not in OWNER:
        keys.add(first)
    return keys


def same_person(name: str, owner: str) -> bool:
    """Is this chat sender the CEO? His Telegram name can be written any way ("Luca B.", "Betti Luca", "Dott. Luca
    Betti | Kimera"): any word of his name is enough, and with no name configured, or in doubt, it is him. Only a name
    with nothing in common with his ("Andrea Lorini", another user the bot allows) is someone else."""
    if not name or owner == DEFAULT_OWNER:
        return True
    words = {w for w in normalize(name).split() if len(w) > 1 and w not in TITLES}
    return not words or bool(words & {w for w in normalize(owner).split() if len(w) > 1})


def person(vault: Vault, name: Any, *, owner: str, author: str) -> str | None:
    """Who must do it: "io" or nobody → the memo's author; "il CEO", his name or first name → the CEO; a known person or
    company → its note's title; Alfred itself → None (not a commitment)."""
    raw = clean(name, 80)
    key = normalize(raw)
    if not key or key in SPEAKER:
        return author
    if key in OWNER or key in owner_keys(owner):
        return owner
    if key in ALFRED:
        return None
    note = vault.find_entity(raw, "persona") or vault.find_entity(raw)
    return note.title if note else raw


def plain_action(action: dict[str, Any]) -> str:
    """An action as the model wrote it, for a note whose commitments could not be stored."""
    who, what = clean(action.get("chi"), 80), clean(action.get("cosa"))
    due = parse_due(action.get("entro"))
    return (f"{who}: " if who else "") + what + (f" (entro {due})" if due else "")


def actions(value: Any, limit: int = 8) -> list[dict[str, Any]]:
    """The model's ``azioni``: objects as asked, bare strings tolerated (a model that ignores the format)."""
    out = []
    for item in as_list(value):
        if isinstance(item, str):
            item = {"cosa": item}
        if isinstance(item, dict) and clean(item.get("cosa")):
            out.append({k: item.get(k) for k in ("chi", "cosa", "entro", "stesso_di", "evidenza")})
    return out[:limit]


def report_line(item: dict[str, Any], owner: str, today: date | None = None) -> str:
    """What the CEO reads: '#12 Andrea Lorini — mandare i disegni — entro ven 16/10' ('Tu' for his own)."""
    today = today or date.today()
    who = "Tu" if item["chi"] == owner else item["chi"]
    seen = "" if item.get("nuovo", True) else " (già aperto)"
    due = ""
    if item.get("entro"):
        due = f" — entro {day_label(item['entro'])}" + (" (scaduto)" if item["entro"] < today.isoformat() else "")
    if item.get("entro_detto"):  # a memo about an open one gave another date: the CEO decides, nothing moved
        due += f" (la nota dice {day_label(item['entro_detto'])})"
    return f"#{item['id']}{seen} {who} — {item['cosa']}{due}"


TITLES = frozenset("dott dottor dottore dottoressa dr ing ingegner ingegnere avv avvocato sig signor signore signora "
                   "arch architetto geom prof professor professore professoressa".split())


def same_owner(a: str, b: str) -> bool:
    """"Andrea" is "Andrea Lorini", "la dottoressa Ferri" is "Ferri": one name's words within the other's, titles aside."""
    names = [{w for w in normalize(n).split() if w not in TITLES and w not in {"la", "il", "lo"}} for n in (a, b)]
    return bool(names[0] and names[1]) and (names[0] <= names[1] or names[1] <= names[0])


def same_task(item: dict[str, Any], chi: str, cosa: str) -> bool:
    """Can a memo's action be this open commitment? Same owner and words in common: a model's ``stesso_di`` alone is
    not enough (a small one tied "send the supplier comparison" to the same person's "send the CRM tasks")."""
    if not same_owner(item["chi"], chi):
        return False
    old, new = set(tokens(item["cosa"])), set(tokens(cosa))
    return bool(old and new) and len(old & new) / len(old | new) >= MATCH_RATIO  # one shared verb is not a match


def action_text(cosa: str, evidence: str) -> str:
    """The action as stored: the model's words, or its quote when it gave a single word ("mandare")."""
    if len(tokens(cosa)) < 2 and len(tokens(evidence)) >= 3:
        return clean(evidence, 160)
    return cosa


def note_line(item: dict[str, Any]) -> str:
    return f"#{item['id']} {item['chi']}: {item['cosa']}" + (f" 📅 {item['entro']}" if item.get("entro") else "")


def _task_line(item: dict[str, Any]) -> str:
    """Obsidian Tasks order: description first, the emoji fields last."""
    box = {"aperto": " ", "fatto": "x", "annullato": "-"}[item["stato"]]
    line = f"- [{box}] #{item['id']} {item['chi']}: {item['cosa']}" + (f" · fonte {item['fonte']}" if item.get("fonte") else "")
    if item.get("entro"):
        line += f" 📅 {item['entro']}"
    if item["stato"] == "fatto" and item.get("chiuso"):
        line += f" ✅ {item['chiuso']}"
    if item["stato"] == "annullato" and item.get("chiuso"):
        line += f" ❌ {item['chiuso']}"
    return line


def _sort_key(today: str):
    def key(item: dict[str, Any]) -> tuple:
        due = item.get("entro") or ""
        return (not (due and due < today), due or "9999", item["id"])
    return key


def _names(item: dict[str, Any]) -> set[str]:
    """The owner's full name and its meaningful words ("il responsabile della produzione": not "il", not "della")."""
    key = normalize(item["chi"])
    return {key, *(t for t in key.split() if len(t) > 2 and t not in STOPWORDS and t not in TITLES)} if key else set()


# ---- the store ----------------------------------------------------------------------------


class Impegni:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.path = vault.root / STATE_DIR / FILE

    def load(self) -> dict[str, Any]:
        """The store; an unreadable file is an error, never silently replaced by an empty one."""
        damaged = f"{self.vault.rel(self.path)} is damaged: restore it from the vault's git history"
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = ""
        except (OSError, UnicodeDecodeError) as exc:
            raise CommitmentError(f"{damaged} ({exc})") from exc
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise CommitmentError(f"{damaged} (not valid JSON: {exc})") from exc
        if not isinstance(data, dict):
            raise CommitmentError(f"{damaged} (not a commitments file)")
        data.setdefault("prossimo", 1)
        data.setdefault("impegni", [])
        data.setdefault("correzioni", {"modificati": 0, "annullati": 0, "riaperti": 0})
        data.setdefault("evidenza", {"date": 0, "trovate": 0})
        for item in data["impegni"]:  # every reader relies on these: a hand-edited entry without them is damage
            if not (isinstance(item, dict) and isinstance(item.get("id"), int) and isinstance(item.get("chi"), str)
                    and isinstance(item.get("cosa"), str) and item.get("stato") in STATI):
                raise CommitmentError(f"{damaged} (entry {str(item)[:80]})")
            item.setdefault("entro", "")
            item.setdefault("fonte", "")
            item.setdefault("chiuso", "")
        return data

    def open_items(self) -> list[dict[str, Any]]:
        return [i for i in self.load()["impegni"] if i.get("stato") == "aperto"]

    # ---- writing --------------------------------------------------------------------------

    def file_memo(self, acts: list[dict[str, Any]], *, author: str, owner: str, fonte: str, when: date,
                  transcript: str, offered: set[int], today: date | None = None) -> list[dict[str, Any]]:
        """Store a memo's actions. An action is an open commitment when the model says so (``stesso_di`` one the
        prompt offered) and it passes ``same_task``, or when owner and words are the same; the rest are new. A memo
        never moves a date it did not set: a missing one is filled in, a different one is only reported
        (``entro_detto``). Returns what the report shows, each with ``nuovo``."""
        today = today or date.today()
        out: list[dict[str, Any]] = []
        with _lock:
            data = self.load()
            changed = False
            for act in acts:
                chi = person(self.vault, act.get("chi"), owner=owner, author=author)
                evidenza = clean(act.get("evidenza"), 300)
                cosa = action_text(clean(act.get("cosa")), evidenza)
                if chi is None or not cosa:
                    continue
                entro = parse_due(act.get("entro"))
                ref = parse_ref(act.get("stesso_di"))
                match = self._open(data, ref) if ref in offered else None
                if match is not None and not same_task(match, chi, cosa):
                    match = None
                match = match or self._duplicate(data, chi, cosa)
                if match is not None:
                    said = ""
                    if entro and not match.get("entro"):
                        match["entro"] = entro
                        changed = True
                    elif entro and entro != match["entro"]:
                        said = entro
                    if match["id"] not in {o["id"] for o in out}:
                        out.append({**match, "nuovo": False, "entro_detto": said})
                    continue
                found = evidence_found(evidenza, transcript)
                data["evidenza"]["date"] += found is not None
                data["evidenza"]["trovate"] += bool(found)
                item = {"id": data["prossimo"], "chi": chi, "cosa": cosa, "entro": entro, "stato": "aperto",
                        "fonte": fonte, "creato": when.isoformat(), "chiuso": "", "evidenza": evidenza, "riscontro": found}
                data["prossimo"] += 1
                data["impegni"].append(item)
                out.append({**item, "nuovo": True})
                changed = True
            if changed:
                self._save(data, today)
        return out

    def add(self, chi: Any, cosa: Any, entro: Any = "", *, owner: str, author: str, fonte: str,
            today: date | None = None) -> dict[str, Any]:
        """One commitment the CEO states in the chat; the same open one is returned instead of a copy."""
        today = today or date.today()
        who = person(self.vault, chi, owner=owner, author=author)
        what = clean(cosa)
        if not who or not what:
            raise CommitmentError("a commitment needs who (chi) and what (cosa)")
        due = self._due(entro)
        with _lock:
            data = self.load()
            match = self._duplicate(data, who, what)
            if match is not None:
                if due and due != match.get("entro"):
                    match["entro"] = due
                    self._save(data, today)
                return {**match, "nuovo": False}
            item = {"id": data["prossimo"], "chi": who, "cosa": what, "entro": due, "stato": "aperto", "fonte": fonte,
                    "creato": today.isoformat(), "chiuso": "", "evidenza": "", "riscontro": None}
            data["prossimo"] += 1
            data["impegni"].append(item)
            self._save(data, today)
        return {**item, "nuovo": True}

    def modify(self, ident: Any, *, chi: Any = None, cosa: Any = None, entro: Any = None, stato: Any = None,
               owner: str, author: str, today: date | None = None) -> dict[str, Any]:
        """Close, reopen or correct one commitment. A wrong owner, text or date, a cancellation and a reopening are
        corrections and are counted; "fatto" is not."""
        today = today or date.today()
        ref = parse_ref(ident)
        with _lock:
            data = self.load()
            item = next((i for i in data["impegni"] if i["id"] == ref), None)
            if item is None:
                raise CommitmentError(f"no commitment #{ident}")
            fixed = False
            if chi not in (None, ""):
                who = person(self.vault, chi, owner=owner, author=author)
                if who and who != item["chi"]:
                    item["chi"], fixed = who, True
            if cosa not in (None, "") and clean(cosa) and clean(cosa) != item["cosa"]:
                item["cosa"], fixed = clean(cosa), True
            if entro not in (None, ""):  # empty means "not changed" like the other fields; "nessuna" removes the date
                due = self._due(entro)
                if due != item.get("entro"):
                    item["entro"], fixed = due, True
            if stato not in (None, ""):
                new = normalize(str(stato))
                new = {"chiuso": "fatto", "completato": "fatto", "riaperto": "aperto", "cancellato": "annullato"}.get(new, new)
                if new not in STATI:
                    raise CommitmentError(f"stato must be one of {', '.join(STATI)}")
                if new != item["stato"]:
                    if new == "aperto":
                        data["correzioni"]["riaperti"] += 1
                    elif new == "annullato":
                        data["correzioni"]["annullati"] += 1
                    item["stato"], item["chiuso"] = new, ("" if new == "aperto" else today.isoformat())
            if fixed:
                data["correzioni"]["modificati"] += 1
            self._save(data, today)
        return dict(item)

    @staticmethod
    def _due(value: Any) -> str:
        if value in (None, "") or normalize(str(value)) in {"nessuna", "nessuno", "no"}:
            return ""
        due = parse_due(value)
        if not due:
            raise CommitmentError(f"entro must be a date YYYY-MM-DD, not {value!r}")
        return due

    @staticmethod
    def _open(data: dict[str, Any], ref: int | None) -> dict[str, Any] | None:
        return next((i for i in data["impegni"] if i["id"] == ref and i["stato"] == "aperto"), None)

    @staticmethod
    def _duplicate(data: dict[str, Any], chi: str, cosa: str) -> dict[str, Any] | None:
        """The same words for the same person ("Andrea" and "Andrea Lorini" are one), still open."""
        what = normalize(cosa)
        return next((i for i in data["impegni"] if i["stato"] == "aperto" and normalize(i["cosa"]) == what
                     and same_owner(i["chi"], chi)), None)

    def _save(self, data: dict[str, Any], today: date) -> None:
        atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2))
        self._render(data, today)

    def _render(self, data: dict[str, Any], today: date) -> None:
        iso = today.isoformat()
        since = (today - timedelta(days=KEEP_CLOSED_DAYS)).isoformat()
        open_items = sorted((i for i in data["impegni"] if i["stato"] == "aperto"), key=_sort_key(iso))
        closed = sorted((i for i in data["impegni"] if i["stato"] != "aperto" and (i.get("chiuso") or "") >= since),
                        key=lambda i: (i.get("chiuso") or "", i["id"]), reverse=True)
        lines = ["---", "tipo: impegni", f"aggiornato: {iso}", "---", "# Impegni", "", NOTICE, "", "## Aperti", "",
                 *([_task_line(i) for i in open_items] or ["_Nessun impegno aperto._"]), "",
                 f"## Chiusi negli ultimi {KEEP_CLOSED_DAYS} giorni", "",
                 *([_task_line(i) for i in closed] or ["_Nessuno._"]), ""]
        atomic_write(self.vault.root / VIEW, "\n".join(lines))

    # ---- reading --------------------------------------------------------------------------

    def listing(self, *, chi: Any = None, scaduti: bool = False, owner: str, author: str,
                today: date | None = None) -> list[dict[str, Any]]:
        today = today or date.today()
        items = self.open_items()
        if chi not in (None, ""):
            who = person(self.vault, chi, owner=owner, author=author) or ""
            items = [i for i in items if same_owner(i["chi"], who)]
        if scaduti:
            items = [i for i in items if i.get("entro") and i["entro"] < today.isoformat()]
        return sorted(items, key=_sort_key(today.isoformat()))

    def prompt_block(self, transcript: str, *, owner: str, author: str) -> tuple[str, set[int]]:
        """Open commitments the model may recognise in a memo (``stesso_di``): the people the transcript names, plus the
        CEO's and the author's own; newest first, at most ``PROMPT_LIMIT``."""
        text = f" {normalize(transcript)} "
        chosen = [i for i in sorted(self.open_items(), key=lambda i: -i["id"])
                  if i["chi"] in (owner, author) or any(f" {n} " in text for n in _names(i))][:PROMPT_LIMIT]
        lines = [f"#{i['id']} {i['chi']} — {i['cosa']}" + (f" (entro {i['entro']})" if i.get("entro") else "") for i in chosen]
        return "\n".join(lines), {i["id"] for i in chosen}

    def prefetch_block(self, query: str, entity_names: set[str], today: date | None = None) -> str:
        """Open commitments of the people (or about the clients and projects) the CEO's message names."""
        today = today or date.today()
        words = set(normalize(query).split())
        hits = [i for i in self.open_items()
                if _names(i) & words or f" {normalize(i['chi'])} " in f" {normalize(query)} "
                or any(f" {n} " in f" {normalize(i['cosa'])} " or n == normalize(i["chi"]) for n in entity_names)]
        hits = sorted(hits, key=_sort_key(today.isoformat()))[:PREFETCH_LIMIT]
        if not hits:
            return ""
        return "## Impegni aperti (tenuti da Alfred)\n" + "\n".join(report_line({**i, "nuovo": True}, "", today) for i in hits)

    def stats(self, today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        data = self.load()
        items = data["impegni"]
        since = (today - timedelta(days=KEEP_CLOSED_DAYS)).isoformat()
        open_items = [i for i in items if i["stato"] == "aperto"]
        corrections = data["correzioni"]
        return {"aperti": len(open_items),
                "scaduti": sum(1 for i in open_items if i.get("entro") and i["entro"] < today.isoformat()),
                "chiusi_30_giorni": sum(1 for i in items if i["stato"] != "aperto" and (i.get("chiuso") or "") >= since),
                "totale": len(items), "correzioni": corrections,
                "tasso_correzione": round(sum(corrections.values()) / len(items), 2) if items else 0.0,
                "citazioni": data["evidenza"]}


# ---- quality: labelled memos --------------------------------------------------------------


def _overlap(expected: str, predicted: str) -> float:
    want = set(tokens(expected))
    return len(want & set(tokens(predicted))) / len(want) if want else 0.0


def score(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Precision, recall, owner and date accuracy over ``[{"attesi": [...], "previsti": [...], "trascrizione": str}]``,
    every commitment already as ``{"chi", "cosa", "entro", "evidenza"}`` with canonical names. A predicted action
    matches the expected one whose words it covers best (at least ``MATCH_RATIO``)."""
    expected = predicted = pairs = owner_ok = dated = date_ok = invented_dates = quotes = quotes_found = 0
    details = []
    for case in cases:
        free = list(case["previsti"])
        expected += len(case["attesi"])
        predicted += len(free)
        missed, wrong = [], []
        for want in case["attesi"]:
            best = max(free, key=lambda p: _overlap(want["cosa"], p["cosa"]), default=None)
            if best is None or _overlap(want["cosa"], best["cosa"]) < MATCH_RATIO:
                missed.append(want["cosa"])
                continue
            free.remove(best)
            pairs += 1
            if same_owner(want["chi"], best["chi"]):
                owner_ok += 1
            else:
                wrong.append(f"{want['cosa']}: chi {best['chi']!r}, atteso {want['chi']!r}")
            if want.get("entro"):
                dated += 1
                if want["entro"] == best.get("entro"):
                    date_ok += 1
                else:
                    wrong.append(f"{want['cosa']}: entro {best.get('entro') or '—'}, atteso {want['entro']}")
            elif best.get("entro"):
                invented_dates += 1
                wrong.append(f"{want['cosa']}: entro {best['entro']} inventato")
        for p in case["previsti"]:
            found = evidence_found(p.get("evidenza") or "", case["trascrizione"])
            quotes += found is not None
            quotes_found += bool(found)
        if missed or free or wrong:
            details.append({"trascrizione": case["trascrizione"][:80], "mancati": missed,
                            "in_piu": [p["cosa"] for p in free], "sbagliati": wrong})

    def ratio(a: int, b: int) -> float | None:
        return round(a / b, 2) if b else None

    return {"memo": len(cases), "attesi": expected, "previsti": predicted, "precisione": ratio(pairs, predicted),
            "richiamo": ratio(pairs, expected), "responsabile_giusto": ratio(owner_ok, pairs),
            "data_giusta": ratio(date_ok, dated), "date_inventate": invented_dates,
            "citazioni_trovate": ratio(quotes_found, quotes), "dettagli": details}

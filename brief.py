"""Meeting briefs: shortly before a meeting, what Odoo, the mail and the vault say about the people in it.

Alfred's cron runs ``alfred brain brief --due`` every 5 minutes as a script (``no_agent``: no model, no tokens). The
first meeting starting within the next ``brief_minutes`` (plus one tick) that has not been briefed gets one brief,
printed on stdout: the cron delivers it to the CEO's Telegram and mirrors it into his chat, so Alfred knows what it
said when he answers. Nothing printed is a silent run.

The brief is assembled from data, without the model: an invented "to clarify" is worse than none, and the cron's
process must not start or share the local model. Other plugins' tools are called by name (``Host.tool``): Outlook for
the calendar and the mail, Odoo for the deals. A missing or failing one leaves its section out.

Every brief sent leaves a note in ``Riunioni/``, created once and never rewritten, for memos and recordings to link
to later. Text written by others (mail subjects, the invitation) goes to the CEO but never into the vault: notes are
recalled into prompts as Alfred's own memory.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .host import DEFAULT_OWNER, Host
from .impegni import GIORNI_BREVI, CommitmentError, Impegni, _sort_key, report_line
from .vault import MEETINGS_DIR, STATE_DIR, Note, Vault, _format_meta, atomic_write, create_exclusive, entity_facts, meta_value, normalize, safe_filename

try:
    import fcntl
except ImportError:  # Windows: no guard across processes
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

JOB = "brain: brief riunioni"
SCRIPT = "alfred-brain-brief.py"
STATE_FILE = "brief.json"
LOCK_FILE = "brief.lock"
DEFAULT_MINUTES = 30
TICK_MINUTES = 5  # the cron's period: the window reaches one tick further, so a tick lost to a restart still briefs
MIN_LEAD_MINUTES = 5  # closer to the start than this, a brief is noise
KEEP_DAYS = 7
MAIL_DAYS = 60
MAX_SEARCHES = 3  # Odoo searches per meeting (one per outside company)
MAX_MAIL_PEOPLE = 5
MAX_MAILS = 5
MAX_LEADS = 3
MAX_COMMITMENTS = 5
MAX_FACTS = 2
MAX_CHARS = 3000
DRY_RUN_MEETINGS = 8
PRIVATE = frozenset({"private", "personal", "confidential"})
# Mail providers: an address there says nothing about a company, so Odoo is searched for the address itself. By
# brand, whatever the country (yahoo.com, yahoo.co.uk, yahoo.fr), plus certified-mail (PEC) domains.
PROVIDERS = frozenset("""
gmail googlemail yahoo ymail rocketmail hotmail outlook live msn aol icloud me mac gmx web mail email proton protonmail
pm tutanota zoho yandex libero virgilio alice tim tin tiscali fastwebnet inwind iol pec legalmail postecert arubapec
bluewin hispeed orange free laposte sfr wanadoo neuf btinternet virginmedia t-online freenet arcor seznam wp o2 onet
interia qq 163 126 naver daum comcast verizon att sbcglobal bellsouth cox charter earthlink shaw rogers sympatico
bigpond optusnet telenet skynet ziggo kpnmail hetnet terra uol bol rediffmail
""".split())
PUBLIC_DOMAINS = frozenset({"pec.it", "pec.aruba.it", "cert.legalmail.it"})
LEGAL_FORMS = frozenset("srl srls spa sapa sas snc ss scarl scrl soc ltd limited llc inc corp gmbh ag sa sarl bv nv".split())

SCRIPT_TEXT = '''"""Alfred's meeting brief, run by Alfred's cron every 5 minutes. Written by `alfred brain setup`.

Only the brief reaches stdout, which the cron delivers to the CEO: whatever Alfred prints while loading goes to stderr.
"""
import sys

sys.argv = ["alfred", "brain", "brief", "--due"]
sys.stdout = sys.stderr
from hermes_cli.main import main  # noqa: E402

main()
'''

_ADDRESS = re.compile(r"^(.*?)\s*<\s*([^<>\s]+@[^<>\s]+)\s*>$")
_EMAIL = re.compile(r"\S+@\S+")
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# Whatever Telegram would turn into a link: a URL, or a bare domain with a common or two-letter ending ("bit.ly/x").
_LINK = re.compile(r"(?:https?://|www\.)\S+|\b(?:[a-z0-9-]+\.)+(?:[a-z]{2}|com|net|org|info|biz|app|dev|io|ai|xyz|online|"
                   r"site|top|shop|link|click|live)\b(?:/\S*)?", re.IGNORECASE)
_AFTER_NAME = re.compile(r"\s*[(\[|].*$")  # "Mario Rossi (Beta)", "Mario Rossi | Beta": the name only


# ---- reading Outlook's answer ---------------------------------------------------------------


def plain(value: Any, limit: int = 200) -> str:
    """Text for the brief: one line, without links, web or mail addresses, or angle brackets. One '<' turns a Telegram
    message into HTML, which Telegram then refuses; a bare domain or a Markdown link becomes a link in Alfred's message,
    whoever wrote it."""
    text = _LINK.sub(" ", _MARKDOWN_LINK.sub(r"\1", _EMAIL.sub(" ", str(value or ""))))
    text = " ".join(text.replace("<", " ").replace(">", " ").replace("«", '"').replace("»", '"').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def parse_person(value: Any) -> tuple[str, str]:
    """'Mario Rossi <m.rossi@beta.it>' → ('Mario Rossi', 'm.rossi@beta.it'); 'Rossi, Mario' → 'Mario Rossi'; a bare
    address has no name (none is made up from it: 'info@' is nobody)."""
    text = " ".join(str(value or "").split())
    found = _ADDRESS.match(text)
    if found:
        name, address = found.group(1), found.group(2)
    elif "@" in text and " " not in text:
        name, address = "", text
    else:
        name, address = text, ""
    name = _AFTER_NAME.sub("", name.strip(" \"'"))
    if name.count(",") == 1:
        last, first = (part.strip() for part in name.split(","))
        name = f"{first} {last}" if first and last else name.replace(",", " ")
    if "@" in name:  # Outlook repeats the address when it knows no name
        name = ""
    return " ".join(name.split()), address.strip().lower()


def domain_of(address: str) -> str:
    return address.rpartition("@")[2].strip().lower() if "@" in address else ""


def _under(domain: str, parents: set[str] | frozenset[str]) -> bool:
    return bool(domain) and any(domain == p or domain.endswith(f".{p}") for p in parents)


def _instant(value: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()


def _where(event: dict[str, Any]) -> str:
    location = plain(event.get("location"), 60)
    online = str(event.get("online") or "").lower()
    if location:
        return location
    if "zoom" in online:
        return "Zoom"
    return ("Teams" if "teams" in online else "online") if online else ""


@dataclass
class Person:
    name: str  # "" when Outlook knows only the address
    address: str
    internal: bool
    note: Note | None = None  # the person's note in Persone/

    @property
    def domain(self) -> str:
        return domain_of(self.address)


@dataclass
class Meeting:
    id: str
    subject: str
    start: datetime
    end: datetime | None
    where: str
    organizer: str  # the organiser's name, when it is not the CEO
    people: list[Person]
    agenda: str  # the invitation's own text (alfred-m365 drops the Teams/Zoom join block): third-party text

    @property
    def key(self) -> str:
        """A meeting moved to another time is another brief."""
        return f"{self.id}|{self.start.isoformat()}"

    @property
    def outsiders(self) -> list[Person]:
        return [p for p in self.people if not p.internal]


def meetings(result: dict[str, Any], *, owner: str, internal: set[str]) -> list[Meeting]:
    """The calendar events a brief can be about, by start: not cancelled, all-day, declined or private, and with someone
    besides the CEO (rooms are not people: alfred-m365 lists them apart). People from the CEO's own domain, or from
    ``internal``, are colleagues."""
    mailbox = str(result.get("mailbox") or "").lower()
    ours = {d.strip().lower().lstrip("@") for d in internal if d and d.strip()} | ({domain_of(mailbox)} if mailbox else set())
    me = {normalize(n) for n in _orders(owner)} - {""} if owner != DEFAULT_OWNER else set()

    def is_ceo(name: str, address: str) -> bool:
        """The CEO, also under another address (an alias, the mailbox configured as a sign-in name): the mailbox, his
        full name, or the mailbox's user name on one of the company's domains."""
        user = mailbox.partition("@")[0]
        return bool((address and address == mailbox) or (name and normalize(name) in me)
                    or (address and user and address.partition("@")[0] == user and _under(domain_of(address), ours)))

    out = []
    for event in result.get("events") or []:
        if not isinstance(event, dict) or event.get("cancelled") or event.get("all_day") or not event.get("id"):
            continue
        if str(event.get("my_response") or "").lower() == "declined" or str(event.get("sensitivity") or "").lower() in PRIVATE:
            continue
        start = _instant(event.get("start"))
        if start is None:
            continue
        organizer = None if event.get("organised_by_ceo") else event.get("organizer")
        people, seen = [], set()
        for raw in [organizer, *(event.get("attendees") or [])]:
            name, address = parse_person(raw)
            key = address or normalize(name)
            if not key or key in seen or is_ceo(name, address):
                continue
            seen.add(key)
            people.append(Person(name, address, internal=not address or _under(domain_of(address), ours)))
        if not people:
            continue
        organizer_name, organizer_address = parse_person(organizer)
        out.append(Meeting(id=str(event["id"]), subject=plain(event.get("subject"), 120) or "Riunione senza titolo",
                           start=start, end=_instant(event.get("end")), where=_where(event),
                           organizer="" if is_ceo(organizer_name, organizer_address) else organizer_name, people=people,
                           agenda=plain(event.get("agenda"), 300)))
    return sorted(out, key=lambda m: m.start)


def due(items: list[Meeting], now: datetime, minutes: int, done: set[str]) -> list[Meeting]:
    """Meetings starting between ``MIN_LEAD_MINUTES`` and ``minutes`` + one tick from now, not handled yet."""
    low, high = now + timedelta(minutes=MIN_LEAD_MINUTES), now + timedelta(minutes=minutes + TICK_MINUTES)
    return [m for m in items if low <= m.start <= high and m.key not in done]


# ---- gathering --------------------------------------------------------------------------------


@dataclass
class Brief:
    meeting: Meeting
    mailbox: str
    leads: list[dict[str, Any]] = field(default_factory=list)
    lead_totals: list[tuple[str, str, int]] = field(default_factory=list)  # (company domain, or "" and the person, total)
    mails: list[dict[str, Any]] = field(default_factory=list)
    companies: dict[str, str] = field(default_factory=dict)  # outside domain -> company name
    company_notes: list[Note] = field(default_factory=list)
    known: list[tuple[str, list[str]]] = field(default_factory=list)  # (who, latest facts in the vault)
    commitments: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # sections left out and why (dry run, status)

    def worth_sending(self) -> bool:
        """Among colleagues only what is still owed is news; with outside people, anything Alfred found. The invitation
        alone is not: Outlook already shows it."""
        if not self.meeting.outsiders:
            return bool(self.commitments)
        return bool(self.leads or self.mails or self.known or self.commitments)


def gather(host: Host, vault: Vault, meeting: Meeting, *, owner: str, mailbox: str, now: datetime,
           names: dict[str, list[Note]] | None = None) -> Brief:
    brief = Brief(meeting, mailbox)
    names = names if names is not None else entity_names(vault)
    for p in meeting.people:  # a full name only: a first name alone is not one person
        p.note = _find(names, p.name, "persona") if len(p.name.split()) >= 2 else None
        if p.note is not None:  # the vault's spelling ("Rossi Mario" in Outlook is "Mario Rossi" there)
            if meeting.organizer == p.name:
                meeting.organizer = p.note.title
            p.name = p.note.title
    _deals(host, brief)
    _companies(brief, names)
    _mail(host, brief, since=now.date() - timedelta(days=MAIL_DAYS))
    for who, note in [(p.name, p.note) for p in meeting.outsiders if p.note] + [(n.title, n) for n in brief.company_notes]:
        facts = entity_facts(note)[-MAX_FACTS:]
        if facts:
            brief.known.append((who, facts))
    _commitments(vault, brief, owner, now.date())
    return brief


def entity_names(vault: Vault) -> dict[str, list[Note]]:
    """Every client, person and project note by normalised title, file name and alias: read once per run."""
    out: dict[str, list[Note]] = {}
    for note in vault.entities():
        for name in [note.title, note.path.stem, *(note.meta.get("alias") or [])]:
            key = normalize(name)
            if key and all(n.path != note.path for n in out.get(key, [])):
                out.setdefault(key, []).append(note)
    return out


def _orders(name: str) -> list[str]:
    """Exchange shows 'Mario Rossi' or 'Rossi Mario': both, for two-word names."""
    words = name.split()
    return [name, f"{words[1]} {words[0]}"] if len(words) == 2 else [name]


def _find(names: dict[str, list[Note]], name: str, tipo: str, *, reorder: bool = True) -> Note | None:
    for candidate in _orders(name) if reorder else [name]:
        for note in names.get(normalize(candidate), []):
            if note.meta.get("tipo") == tipo:
                return note
    return None


def _ok(result: Any) -> bool:
    return isinstance(result, dict) and result.get("ok") is True


def _why(result: Any) -> str:
    """A tool's failure in a few words (``brain status``, dry run): 'area_not_enabled', 'not_configured', …"""
    if not isinstance(result, dict):
        return "strumento non disponibile"
    error = result.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or error.get("message") or "errore")[:80]
    text = str(error or "risposta senza ok")
    return "strumento non installato" if text.lower().startswith("unknown tool") else text[:80]


def _deals(host: Host, brief: Brief) -> None:
    """Open deals in Odoo for each outside company (``@domain``), or for the address itself on a mail provider's domain.
    alfred-odoo keeps its data areas in its own client: without ``crm`` the answer is an error and the section goes."""
    searches: list[tuple[str, str, str]] = []  # (text, label, company domain or "")
    for p in brief.meeting.outsiders:
        if not p.address:
            continue
        public = is_public(p.domain)
        text = p.address if public else f"@{p.domain}"
        if text not in [s[0] for s in searches]:
            searches.append((text, (p.name or "questo contatto") if public else "", "" if public else p.domain))
    seen: set[Any] = set()
    for text, label, domain in searches[:MAX_SEARCHES]:
        result = host.tool("odoo_search_leads", {"text": text, "type": "any", "limit": MAX_LEADS})
        if not _ok(result):
            brief.missing.append(f"Odoo: {_why(result)}")
            return  # the same answer for the other companies (no crm area, no plugin, Odoo down)
        items = [i for i in result.get("items") or [] if isinstance(i, dict) and i.get("id") not in seen]
        shown = items[:max(0, MAX_LEADS - len(brief.leads))]
        seen.update(i.get("id") for i in items)
        brief.leads.extend(shown)
        total = int(result.get("total") or 0)
        if total > len(shown):
            brief.lead_totals.append((domain, label, total))
        partner = next((str((i.get("partner") or {}).get("name") or "") for i in items if i.get("partner")), "")
        if domain and partner:
            brief.companies.setdefault(domain, partner)


def _companies(brief: Brief, names: dict[str, list[Note]]) -> None:
    """Each outside company's note in Clienti/: by the Odoo customer's name, else by the domain ('beta-group.it' is
    'Beta Group'). Exact names only."""
    for domain in dict.fromkeys(p.domain for p in brief.meeting.outsiders if p.domain and not is_public(p.domain)):
        candidates = [brief.companies.get(domain, ""), _company_key(brief.companies.get(domain, "")), _domain_name(domain)]
        note = next((n for c in candidates if c for n in [_find(names, c, "cliente", reorder=False)] if n), None)
        if note is not None:
            brief.companies[domain] = note.title
            if all(n.path != note.path for n in brief.company_notes):
                brief.company_notes.append(note)


def _brand(domain: str) -> str:
    """'mail.beta-group.it' → 'beta-group', 'yahoo.co.uk' → 'yahoo'."""
    labels = domain.split(".")
    if len(labels) < 2:
        return ""
    return labels[-3] if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "net", "gov", "ac", "edu"} else labels[-2]


def _domain_name(domain: str) -> str:
    return _brand(domain).replace("-", " ")


def is_public(domain: str) -> bool:
    """A mail provider's domain (anyone can have an address there), in any country."""
    return _under(domain, PUBLIC_DOMAINS) or _brand(domain) in PROVIDERS


def company_label(brief: Brief, domain: str) -> str:
    """What the CEO reads for an outside domain: the company's name, never the domain (Telegram would make it a link)."""
    return brief.companies.get(domain) or _domain_name(domain).title()


def _company_key(name: str) -> str:
    """'Beta S.r.l.' → 'beta': the name a memo or a commitment would use."""
    words = normalize(name).split()
    while words and (words[-1] in LEGAL_FORMS or len(words[-1]) == 1):
        words.pop()
    return " ".join(words)


def _mail(host: Host, brief: Brief, *, since: date) -> None:
    """The latest mail with the outside people, both ways (``participants:`` also finds what the CEO sent)."""
    found: dict[str, dict[str, Any]] = {}
    for p in [p for p in brief.meeting.outsiders if p.address][:MAX_MAIL_PEOPLE]:
        result = host.tool("outlook_search_mail", {"query": f"participants:{p.address}", "since": since.isoformat(),
                                                   "limit": MAX_MAILS})
        if not _ok(result):
            brief.missing.append(f"posta: {_why(result)}")
            return
        for item in result.get("items") or []:
            if isinstance(item, dict) and item.get("id") and not item.get("draft"):
                found.setdefault(str(item["id"]), item)
    brief.mails = sorted(found.values(), key=lambda m: str(m.get("received") or ""), reverse=True)[:MAX_MAILS]


def _commitments(vault: Vault, brief: Brief, owner: str, today: date) -> None:
    """Open commitments of the people and companies at the meeting, and the CEO's own that name them. Full names
    only: "Mario" alone would bring every Mario's."""
    try:
        items = Impegni(vault).open_items()
    except CommitmentError as exc:
        brief.missing.append(f"impegni: {exc}")
        return
    keys: set[str] = set()
    for p in brief.meeting.people:
        names = [*_orders(p.name), *([p.note.title, *(p.note.meta.get("alias") or [])] if p.note else [])]
        keys.update(k for k in map(normalize, names) if len(k.split()) >= 2)
    for note in brief.company_notes:
        keys.update(k for k in map(normalize, [note.title, *(note.meta.get("alias") or [])]) if len(k) >= 3)
    keys.update(k for k in map(_company_key, brief.companies.values()) if len(k) >= 3)
    me = normalize(owner)
    hits = [i for i in items if normalize(i["chi"]) in keys
            or (normalize(i["chi"]) == me and any(f" {k} " in f" {normalize(i['cosa'])} " for k in keys))]
    brief.commitments = sorted(hits, key=_sort_key(today.isoformat()))[:MAX_COMMITMENTS]


# ---- writing ----------------------------------------------------------------------------------


def _ddmm(value: Any) -> str:
    try:
        when = datetime.fromisoformat(str(value or "")[:10])
    except ValueError:
        return ""
    return f"{when:%d/%m}"


def _money(value: dict[str, Any]) -> str:
    try:
        amount = f"{float(value.get('amount') or 0):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return ""
    currency = str(value.get("currency") or "")
    return f"{amount} {({'EUR': '€', 'USD': '$', 'GBP': '£'}).get(currency.upper(), currency)}".strip()


def _lead_lines(lead: dict[str, Any]) -> list[str]:
    parts = [plain(lead.get("name"), 80) or "Opportunità"]
    if lead.get("stage"):
        parts.append(plain(lead["stage"], 40))
    value = lead.get("expected_revenue") or {}
    if value:
        parts.append("valore da verificare" if value.get("suspect") else _money(value))
    activity = lead.get("next_activity") or {}
    if activity:
        due_on = _ddmm(activity.get("due"))
        parts.append("prossima: " + (plain(activity.get("summary"), 60) or "attività") + (f" entro {due_on}" if due_on else ""))
    line = "• " + " — ".join(p for p in parts if p) + (f" ({plain(lead['salesperson'], 40)})" if lead.get("salesperson") else "")
    custom = "; ".join(f"{plain(k, 40)}: {plain(v, 60)}" for k, v in (lead.get("custom_fields") or {}).items()
                       if v not in (None, "", False, []))
    return [line, *([f"  {custom}"] if custom else [])]


def _mail_line(mail: dict[str, Any], brief: Brief) -> str:
    sender, address = parse_person(mail.get("from"))
    subject = plain(mail.get("subject"), 80) or "senza oggetto"
    if address and address == brief.mailbox:
        recipients = [parse_person(r) for r in mail.get("to") or []]
        ours = {p.address for p in brief.meeting.people}
        name, to = next(((n, a) for n, a in recipients if a in ours), recipients[0] if recipients else ("", ""))
        who = f"Tu a {name or company_label(brief, domain_of(to))}" if name or to else "Tu"
    else:
        who = sender or company_label(brief, domain_of(address)) or "?"
    return f"• {_ddmm(mail.get('received'))} {plain(who, 60)}: «{subject}»"


def _people_line(brief: Brief) -> str:
    groups: dict[str, list[str]] = {}
    nameless: dict[str, int] = {}
    for p in sorted(brief.meeting.people, key=lambda p: p.internal):  # outside people first
        key = "" if p.internal or is_public(p.domain) else p.domain
        groups.setdefault(key, [])
        if p.name:
            groups[key].append(p.name)
        else:
            nameless[key] = nameless.get(key, 0) + 1
    parts = []
    for key, names in groups.items():
        extra = nameless.get(key, 0)
        if extra:
            names = [*names, f"{extra} altr{'o' if extra == 1 else 'i'}" if names else f"{extra} person{'a' if extra == 1 else 'e'}"]
        parts.append(", ".join(names) + (f" ({company_label(brief, key)})" if key else ""))
    line = "Con: " + "; ".join(parts)
    if brief.meeting.organizer:
        line += f" · organizza {brief.meeting.organizer}"
    return plain(line, 400)


def _when(meeting: Meeting, now: datetime) -> str:
    """In the meeting's own time zone (Outlook's, with the right offset on both sides of a clock change)."""
    start = meeting.start
    if start.date() == now.astimezone(start.tzinfo).date():
        return f"Alle {start:%H:%M} (tra {max(0, round((start - now).total_seconds() / 60))} min)"
    return f"{GIORNI_BREVI[start.weekday()].capitalize()} {start:%d/%m} alle {start:%H:%M}"


def render(brief: Brief, now: datetime, owner: str) -> str:
    """The brief as the CEO reads it on Telegram: plain text, newest and most useful first."""
    m = brief.meeting
    lines = [f"📅 {_when(m, now)}: {m.subject}" + (f" — {m.where}" if m.where else ""), _people_line(brief)]
    if brief.leads:
        lines += ["", "Odoo", *[ln for lead in brief.leads for ln in _lead_lines(lead)]]
        lines += [f"  (in tutto {total} con {plain(company_label(brief, domain) if domain else label, 60)})"
                  for domain, label, total in brief.lead_totals]
    if brief.mails:
        lines += ["", "Ultime mail", *[_mail_line(mail, brief) for mail in brief.mails]]
    if brief.known:
        lines += ["", "Cosa so di loro", *[f"• {plain(who, 60)}: {'; '.join(plain(f, 140).rstrip(' .') for f in facts)}."
                                           for who, facts in brief.known]]
    if brief.commitments:
        lines += ["", "Impegni aperti", *[f"• {plain(report_line({**i, 'nuovo': True}, owner, now.date()), 200)}"
                                          for i in brief.commitments]]
    if m.agenda and m.outsiders:  # among colleagues the brief is only what is owed
        lines += ["", f"Dall'invito: «{m.agenda}»"]
    text = "\n".join(lines)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS].rsplit("\n", 1)[0] + "\n…"


def note_text(brief: Brief, now: datetime, owner: str, vault: Vault) -> str:
    """The meeting's note: who was there and what Odoo and the commitments said at brief time. Nothing written by
    others (mail, invitation): the note becomes part of what Alfred remembers."""
    m = brief.meeting

    def link(note: Note | None, label: str) -> str:
        return f"[[{vault.rel(note.path)[:-3]}|{label}]]" if note else label

    meta: dict[str, Any] = {"tipo": "riunione", "data": m.start.date().isoformat(), "ora": f'"{m.start:%H:%M}"'}
    if m.end:
        meta["fine"] = f'"{m.end:%H:%M}"'
    meta.update({"titolo": meta_value(m.subject, 120), "evento": re.sub(r"[^A-Za-z0-9=_+/-]", "", m.id),
                 "partecipanti": [meta_value(p.name) for p in m.people if p.name], "brief": f'"{now:%Y-%m-%d %H:%M}"'})
    people = [link(p.note, p.name) for p in m.people if p.name]
    nameless = sum(1 for p in m.people if not p.name)
    lines = [f"# {m.subject} — {GIORNI_BREVI[m.start.weekday()]} {m.start:%d/%m} {m.start:%H:%M}", "",
             "Partecipanti: " + ", ".join(people + ([f"+{nameless}"] if nameless else []))]
    if brief.company_notes:
        lines.append("Aziende: " + ", ".join(link(n, n.title) for n in brief.company_notes))
    if m.organizer:
        lines.append(f"Organizza: {m.organizer}")
    lines += ["", f"## Al momento del brief ({now:%d/%m %H:%M})"]
    lines += [f"- Odoo: {ln[2:]}" for lead in brief.leads for ln in _lead_lines(lead)[:1]]
    if brief.mails:
        lines.append(f"- Posta: {len(brief.mails)} mail recenti con loro, l'ultima il {_ddmm(brief.mails[0].get('received'))}.")
    # the owner's name, not "Tu": the note is read by Alfred and by everyone with the vault
    lines += [f"- Impegno: {plain(report_line({**i, 'nuovo': True}, '', now.date()), 200)}" for i in brief.commitments]
    if lines[-1].startswith("## "):
        lines.append("- Niente da Odoo, dalla posta o dagli impegni.")
    lines += ["", "_Alfred ha creato questa nota prima della riunione e non la riscrive: quello che aggiungete resta._", ""]
    return _format_meta(meta) + "\n".join(lines)


# ---- one run ----------------------------------------------------------------------------------


@contextlib.contextmanager
def _exclusive(vault: Vault) -> Iterator[bool]:
    """One run at a time across processes (the cron and a hand-made ``--due``): False when another one holds it."""
    if fcntl is None:
        yield True
        return
    try:
        handle = open(vault.root / STATE_DIR / LOCK_FILE, "a")  # noqa: SIM115 - held for the whole run
    except OSError:
        yield True
        return
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def _load(vault: Vault) -> dict[str, Any]:
    try:
        data = json.loads((vault.root / STATE_DIR / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(vault: Vault, state: dict[str, Any], now: datetime, outcome: dict[str, Any]) -> None:
    since = now - timedelta(days=KEEP_DAYS)
    state["inviati"] = {k: v for k, v in (state.get("inviati") or {}).items()
                        if (_instant(v.get("quando")) or now) >= since}
    state["ultimo_giro"] = {"quando": now.isoformat(timespec="seconds"), "esito": outcome.get("esito"),
                            **({"riunione": outcome["riunione"]} if outcome.get("riunione") else {})}
    atomic_write(vault.root / STATE_DIR / STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))


def _int(value: Any, default: int) -> int:
    try:
        return max(MIN_LEAD_MINUTES, int(value))
    except (TypeError, ValueError):
        return default


def _domains(value: Any) -> set[str]:
    items = value if isinstance(value, (list, tuple)) else str(value or "").replace(";", ",").split(",")
    return {str(d).strip().lower() for d in items if str(d).strip()}


def run(host: Host, vault: Vault, *, now: datetime | None = None, send: bool = True) -> dict[str, Any]:
    """One cron tick (``send``): the first due meeting worth a brief gets its text (``testo``), its note and its mark in
    the state, which is saved before the caller prints anything. Without ``send``, a dry run: the next meeting worth a
    brief today or tomorrow, nothing saved."""
    now = now or datetime.now().astimezone()
    with _exclusive(vault) as mine:
        if not mine:
            return {"esito": "un altro giro è in corso"}
        state = _load(vault)
        try:
            outcome = _run(host, vault, state, now, send)
        except Exception as exc:
            if send:
                _save(vault, state, now, {"esito": f"errore: {exc.__class__.__name__}: {str(exc)[:200]}"})
            raise
        if send:
            _save(vault, state, now, outcome)
        return outcome


def _run(host: Host, vault: Vault, state: dict[str, Any], now: datetime, send: bool) -> dict[str, Any]:
    owner = host.owner()
    today = now.date()
    result = host.tool("outlook_list_events", {"start": today.isoformat(), "end": (today + timedelta(days=1)).isoformat()})
    if not _ok(result):
        return {"esito": f"calendario non disponibile: {_why(result)}"}
    items = meetings(result, owner=owner, internal=_domains(host.setting("internal_domains", [])))
    sent = state.setdefault("inviati", {})
    if send:
        candidates = due(items, now, _int(host.setting("brief_minutes", DEFAULT_MINUTES), DEFAULT_MINUTES), set(sent))
    else:
        candidates = [m for m in items if m.start > now][:DRY_RUN_MEETINGS]
    if not candidates:
        return {"esito": "nessuna riunione in arrivo"}
    names = entity_names(vault)
    mailbox = str(result.get("mailbox") or "").lower()
    skipped = []
    for meeting in candidates:
        brief = gather(host, vault, meeting, owner=owner, mailbox=mailbox, now=now, names=names)
        if not brief.worth_sending():
            skipped.append(meeting.subject)
            if send:  # nothing now: not again at every tick until it starts
                sent[meeting.key] = {"quando": now.isoformat(timespec="seconds"), "esito": "niente da dire"}
            continue
        outcome = {"esito": "inviato" if send else "prova", "riunione": meeting.subject,
                   "inizio": meeting.start.isoformat(timespec="minutes"), "testo": render(brief, now, owner),
                   "mancano": brief.missing}
        if send:
            sent[meeting.key] = {"quando": now.isoformat(timespec="seconds"), "esito": "inviato"}
            outcome["nota"] = sent[meeting.key]["nota"] = _write_note(vault, brief, now, owner)
        return outcome
    return {"esito": "niente da dire", "saltate": skipped}


def _write_note(vault: Vault, brief: Brief, now: datetime, owner: str) -> str | None:
    """The brief goes out even when its note cannot be written."""
    m = brief.meeting
    path = vault.root / MEETINGS_DIR / f"{m.start:%Y-%m-%d %H.%M} {safe_filename(m.subject, 60)}.md"
    try:
        if create_exclusive(path, note_text(brief, now, owner, vault)):
            vault.commit(f"Riunione: {safe_filename(m.subject, 60)}", paths=[path])
        return vault.rel(path)
    except Exception as exc:  # noqa: BLE001 - the brief is already marked sent: it must still be printed
        logger.warning("brain: meeting note not written: %s", exc)
        return None


def status(vault: Vault) -> dict[str, Any]:
    state = _load(vault)
    sent = [v for v in (state.get("inviati") or {}).values() if v.get("esito") == "inviato"]
    return {"job": _job_state(), "ultimo_giro": state.get("ultimo_giro"), f"inviati_ultimi_{KEEP_DAYS}_giorni": len(sent)}


def _job_state() -> str:
    try:
        from cron.jobs import resolve_job_ref
        job = resolve_job_ref(JOB)
    except Exception as exc:  # noqa: BLE001 - outside Alfred, or a broken jobs file
        logger.debug("brain: cron job state unavailable (%s)", exc)
        return "sconosciuto"
    if not job:
        return "assente: alfred brain setup"
    return "attivo" if job.get("enabled", True) else f'in pausa: alfred cron resume "{JOB}"'


def parse_now(value: str) -> datetime:
    """``--at "2026-10-16 14:30"``: a local time to pretend it is (dry runs and tests)."""
    when = datetime.fromisoformat(value.strip().replace(" ", "T", 1))
    return when if when.tzinfo else when.astimezone()

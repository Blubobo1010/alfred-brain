import json
import subprocess
import sys
import types
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from alfred_brain import brief, cli
from alfred_brain.impegni import Impegni
from alfred_brain.vault import STATE_DIR, parse_note
from conftest import FakeHost

ROME = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 16, 14, 30, tzinfo=ROME)  # a Friday
OWNER = "Giorgio Conti"
MAILBOX = "giorgio.conti@azienda.example"
MARIO = "Mario Rossi <mario.rossi@beta.example>"
PAOLO = "Paolo Neri <paolo.neri@azienda.example>"


def event(minutes, subject="Revisione Z20", *, organizer=MARIO, attendees=(), **extra):
    start = NOW + timedelta(minutes=minutes)
    return {"id": f"EVT{minutes:04d}{len(subject)}", "subject": subject, "start": start.isoformat(timespec="minutes"),
            "end": (start + timedelta(minutes=45)).isoformat(timespec="minutes"), "all_day": False, "location": "",
            "organizer": organizer, "organised_by_ceo": organizer.endswith(f"<{MAILBOX}>"), "attendees": list(attendees),
            "rooms": [], "online": "Teams", "status": "busy", "my_response": "accepted", "sensitivity": "normal",
            "recurring": False, "cancelled": False, "agenda": "", **extra}


def calendar(*events):
    return {"ok": True, "mailbox": MAILBOX, "events": list(events)}


LEAD = {"id": 7, "name": "Z20 per Beta", "partner": {"id": 3, "name": "Beta S.r.l."}, "stage": "Proposta",
        "salesperson": "Anna Neri", "expected_revenue": {"amount": 450000.0, "currency": "EUR"},
        "next_activity": {"summary": "Chiamata di follow-up", "due": "2026-10-20"},
        "custom_fields": {"Modello auto": "Z20", "Note": None}}


def leads(args):
    return {"ok": True, "total": 2 if args["text"] == "@beta.example" else 0,
            "items": [LEAD] if args["text"] == "@beta.example" else []}


def mail(n, sender, to, subject, day):
    return {"id": f"M{n}", "date": "x", "received": f"2026-10-{day:02d}T10:{n:02d}+02:00", "from": sender, "to": [to],
            "subject": subject, "preview": "", "unread": False, "draft": False, "attachments": False}


def mails(args):
    if args["query"] != "participants:mario.rossi@beta.example":
        return {"ok": True, "items": []}
    return {"ok": True, "items": [
        mail(1, MARIO, f"Giorgio <{MAILBOX}>", "Disegni cofano: vedi https://evil.example/x o scrivi a x@evil.example", 14),
        mail(2, f"Giorgio Conti <{MAILBOX}>", MARIO, "Offerta Z20 rev. 2", 10)]}


def host(*events, **tools):
    table = {"outlook_list_events": calendar(*events), "outlook_search_mail": mails, "odoo_search_leads": leads, **tools}
    return FakeHost(owner=OWNER, tools={k: v for k, v in table.items() if v is not None})


def known_people(vault):
    vault.add_facts("persona", "Mario Rossi", [("Responsabile acquisti di Beta.", "2026-10-01", ""),
                                               ("Preferisce i report il lunedì.", "2026-10-05", "")])
    vault.add_facts("cliente", "Beta", [("Cliente dal 2024.", "2026-09-01", "")], alias=["Beta S.r.l."])
    store = Impegni(vault)
    for chi, cosa, entro in (("Mario Rossi", "mandare il disegno del cofano", "2026-10-13"),
                             (OWNER, "mandare a Beta il listino 2027", ""), ("Mario", "chiamare il fornitore", ""),
                             (OWNER, "rinnovare il dominio", "")):
        store.add(chi, cosa, entro, owner=OWNER, author=OWNER, fonte="[[Diario/2026-10-12]]", today=date(2026, 10, 12))


# ---- which meetings ----------------------------------------------------------------------------


def test_window_from_five_minutes_to_one_tick_past_the_brief_time():
    items = brief.meetings(calendar(*(event(m, f"R{m}") for m in (4, 5, 35, 36))), owner=OWNER, internal=set())
    assert [m.subject for m in brief.due(items, NOW, 30, set())] == ["R5", "R35"]
    assert [m.subject for m in brief.due(items, NOW, 30, {items[1].key})] == ["R35"]


def test_cancelled_all_day_declined_and_private_meetings_are_left_alone():
    found = brief.meetings(calendar(event(30, "a", cancelled=True), event(30, "b", all_day=True),
                                    event(30, "c", my_response="declined"), event(30, "d", sensitivity="private"),
                                    event(30, "e", sensitivity="Confidential"), event(30, "ok", my_response="tentativeAccepted")),
                           owner=OWNER, internal=set())
    assert [m.subject for m in found] == ["ok"]


def test_who_is_at_the_meeting():
    found = brief.meetings(calendar(
        event(30, "fuori", attendees=[]),  # an outside organiser, nobody else invited
        event(31, "solo io", organizer=f"Giorgio Conti <{MAILBOX}>", attendees=[f"Giorgio Conti <{MAILBOX}>"],
              rooms=["Sala Riunioni <sala@azienda.example>"]),
        event(32, "interna", organizer=f"Giorgio Conti <{MAILBOX}>", attendees=[PAOLO, "Neri, Anna <anna.neri@azienda-group.example>"]),
    ), owner=OWNER, internal={"azienda-group.example"})
    assert [m.subject for m in found] == ["fuori", "interna"]
    outside, inside = found
    assert [(p.name, p.address, p.internal) for p in outside.people] == [("Mario Rossi", "mario.rossi@beta.example", False)]
    assert outside.organizer == "Mario Rossi" and inside.organizer == "" and not inside.outsiders
    assert [p.name for p in inside.people] == ["Paolo Neri", "Anna Neri"]


def test_the_ceo_is_never_a_participant_whatever_address_the_invitation_uses():
    found = brief.meetings(calendar(
        event(30, "alias", organizer="Giorgio Conti <giorgio@alias.example>", attendees=["Conti, Giorgio <gc@altro.example>"]),
        event(31, "utente", organizer=MARIO, attendees=["G. C. <giorgio.conti@azienda-group.example>"]),
        event(32, "sua", organizer="Segreteria <segreteria@azienda.example>", organised_by_ceo=True, attendees=[MARIO]),
    ), owner=OWNER, internal={"azienda-group.example"})
    assert [m.subject for m in found] == ["utente", "sua"]
    assert [p.name for m in found for p in m.people] == ["Mario Rossi", "Mario Rossi"] and found[1].organizer == ""


def test_mail_providers_are_known_by_brand_in_any_country():
    assert all(map(brief.is_public, ["gmail.com", "yahoo.co.uk", "hotmail.fr", "aol.com", "gmx.net", "outlook.de",
                                     "bluewin.ch", "mail.com", "pec.aruba.it", "cert.legalmail.it", "libero.it"]))
    assert not any(map(brief.is_public, ["beta.example", "azienda-group.example", "studio-rossi.it"]))


def test_no_link_survives_into_the_brief():
    text = brief.plain("Paga su pay-acme.com/login o [Apri](bit.ly/x), www.x.it, https://y.z/a, m@x.it. S.r.l. ore 9.30")
    assert text == "Paga su o Apri, S.r.l. ore 9.30"


def test_names_and_addresses_as_outlook_writes_them():
    assert brief.parse_person("Mario Rossi <Mario.Rossi@Beta.example>") == ("Mario Rossi", "mario.rossi@beta.example")
    assert brief.parse_person("Rossi, Mario <m@b.it>") == ("Mario Rossi", "m@b.it")
    assert brief.parse_person("Mario Rossi (Beta) <m@b.it>") == ("Mario Rossi", "m@b.it")
    assert brief.parse_person("info@b.it") == ("", "info@b.it")  # no name made up from the address
    assert brief.parse_person("m@b.it <m@b.it>") == ("", "m@b.it")


# ---- what goes in --------------------------------------------------------------------------------


def test_a_meeting_with_outside_people_gets_the_whole_brief(vault):
    known_people(vault)
    h = host(event(32, attendees=[MARIO, "Paolo Neri <paolo.neri@beta.example>"], agenda="Punti: consegne. Vedi www.x.it"))
    out = brief.run(h, vault, now=NOW)
    text = out["testo"]
    assert out["esito"] == "inviato" and text.startswith("📅 Alle 15:02 (tra 32 min): Revisione Z20 — Teams")
    assert "Con: Mario Rossi, Paolo Neri (Beta) · organizza Mario Rossi" in text
    assert ("• Z20 per Beta — Proposta — 450.000 € — prossima: Chiamata di follow-up entro 20/10 (Anna Neri)\n"
            "  Modello auto: Z20\n  (in tutto 2 con Beta)") in text
    assert "• 14/10 Mario Rossi: «Disegni cofano: vedi o scrivi a»" in text and "• 10/10 Tu a Mario Rossi: «Offerta Z20 rev. 2»" in text
    assert "• Mario Rossi: Responsabile acquisti di Beta; Preferisce i report il lunedì." in text
    assert "• Beta: Cliente dal 2024." in text
    assert "#1 Mario Rossi — mandare il disegno del cofano — entro mar 13/10 (scaduto)" in text
    assert "#2 Tu — mandare a Beta il listino 2027" in text
    assert "chiamare il fornitore" not in text and "rinnovare il dominio" not in text  # "Mario" alone is not him
    assert "Dall'invito: «Punti: consegne. Vedi»" in text
    assert not any(c in text for c in "<>@") and "http" not in text and "evil" not in text
    calls = dict(h.tool_calls)
    assert calls["odoo_search_leads"] == {"text": "@beta.example", "type": "any", "limit": 3}
    assert calls["outlook_list_events"] == {"start": "2026-10-16", "end": "2026-10-17"}
    assert [a["query"] for n, a in h.tool_calls if n == "outlook_search_mail"] == [
        "participants:mario.rossi@beta.example", "participants:paolo.neri@beta.example"]


def test_the_meeting_note_holds_no_text_written_by_others(vault):
    known_people(vault)
    out = brief.run(host(event(32, attendees=[MARIO], agenda="Punti: consegne")), vault, now=NOW)
    assert out["nota"] == "Riunioni/2026-10-16 15.02 Revisione Z20.md"
    note = parse_note(vault.root / out["nota"])
    assert note.meta["tipo"] == "riunione" and note.meta["ora"] == "15:02" and note.meta["partecipanti"] == ["Mario Rossi"]
    assert "[[Persone/Mario Rossi|Mario Rossi]]" in note.body and "[[Clienti/Beta|Beta]]" in note.body
    assert "- Odoo: Z20 per Beta — Proposta" in note.body and "l'ultima il 14/10" in note.body
    assert f"- Impegno: #2 {OWNER} — mandare a Beta il listino 2027" in note.body and "Tu " not in note.body
    assert "Disegni cofano" not in note.body and "consegne" not in note.body and "@" not in note.body
    log = subprocess.run(["git", "log", "-1", "--name-only", "--format=%s"], cwd=vault.root, capture_output=True,
                         text=True).stdout.split("\n")
    assert log[0] == "Riunione: Revisione Z20" and [ln for ln in log[1:] if ln] == [out["nota"]]


def test_a_reversed_name_is_found_in_the_vault(vault):
    known_people(vault)
    out = brief.run(host(event(32, organizer="Rossi Mario <mario.rossi@beta.example>")), vault, now=NOW)
    assert "• Mario Rossi: Responsabile acquisti di Beta; Preferisce i report il lunedì." in out["testo"]
    assert "Con: Mario Rossi (Beta) · organizza Mario Rossi" in out["testo"]


def test_a_mail_provider_address_is_searched_as_it_is(vault):
    h = host(event(32, organizer="Gianni Verdi <gianni.verdi@gmail.com>", agenda="Ciao"))
    brief.run(h, vault, now=NOW)
    assert dict(h.tool_calls)["odoo_search_leads"]["text"] == "gianni.verdi@gmail.com"


def test_a_mail_domain_is_not_a_name_and_the_invitation_alone_is_not_a_brief(vault):
    Impegni(vault).add(OWNER, "rispondere alla mail di Ugo", "", owner=OWNER, author=OWNER, fonte="x")
    h = host(event(32, organizer="Anna Blu <anna@mail.com>", agenda="Ordine del giorno: presentazione"),
             outlook_search_mail={"ok": True, "items": []}, odoo_search_leads={"ok": True, "total": 0, "items": []})
    out = brief.run(h, vault, now=NOW)
    assert out == {"esito": "niente da dire", "saltate": ["Revisione Z20"]}


def test_suspect_values_and_custom_fields():
    lead = {**LEAD, "expected_revenue": {"amount": 880.0, "currency": "EUR", "suspect": True},
            "custom_fields": {"Allestimento": "Carbonio", "Vuoto": ""}}
    line, custom = brief._lead_lines(lead)
    assert "valore da verificare" in line and "880" not in line and custom == "  Allestimento: Carbonio"


def test_a_missing_or_refusing_tool_leaves_its_section_out(vault):
    known_people(vault)
    refused = {"ok": False, "error": {"code": "area_not_enabled", "message": "crm off"}}
    h = host(event(32, attendees=["Ugo Bianchi <ugo@gamma.example>"]), odoo_search_leads=refused, outlook_search_mail=None)
    out = brief.run(h, vault, now=NOW)
    assert out["mancano"] == ["Odoo: area_not_enabled", "posta: strumento non installato"]
    assert [n for n, _ in h.tool_calls].count("odoo_search_leads") == 1  # the same answer for every company
    assert "Odoo" not in out["testo"] and "Ultime mail" not in out["testo"] and "#1 Mario Rossi" in out["testo"]


def test_mail_from_several_people_merged_newest_first_at_most_five(vault):
    def many(args):
        who = args["query"].split(":", 1)[1]
        return {"ok": True, "items": [{**mail(n, f"X <{who}>", f"G <{MAILBOX}>", f"numero {n}", 1 + n), "id": f"{who}{n}"}
                                      for n in range(3)] + [mail(9, MARIO, f"G <{MAILBOX}>", "in copia a tutti", 20)]}
    people = [f"P{n} Q <p{n}@beta.example>" for n in range(3)]
    out = brief.run(host(event(32, organizer=people[0], attendees=people[1:]), outlook_search_mail=many), vault, now=NOW)
    lines = [ln for ln in out["testo"].splitlines() if ln.startswith("• ") and "«" in ln]
    assert len(lines) == 5 and lines[0].startswith("• 20/10 Mario Rossi") and sum("in copia" in ln for ln in lines) == 1


# ---- when it is sent -----------------------------------------------------------------------------


def test_colleagues_only_get_a_brief_when_something_is_owed(vault):
    internal = event(32, "Punto produzione", organizer=f"Giorgio Conti <{MAILBOX}>", attendees=[PAOLO])
    assert brief.run(host(internal), vault, now=NOW)["esito"] == "niente da dire"
    assert brief.run(host(internal), vault, now=NOW)["esito"] == "nessuna riunione in arrivo"  # not asked again
    Impegni(vault).add("Paolo Neri", "preventivo vernici", "2026-10-10", owner=OWNER, author=OWNER, fonte="x",
                       today=date(2026, 10, 9))
    moved = {**internal, "start": (NOW + timedelta(minutes=33)).isoformat(timespec="minutes"), "agenda": "Turni"}
    h = host(moved)
    out = brief.run(h, vault, now=NOW)
    assert out["esito"] == "inviato" and "#1 Paolo Neri — preventivo vernici" in out["testo"]
    assert "Odoo" not in out["testo"] and "Ultime mail" not in out["testo"] and "Turni" not in out["testo"]
    assert [n for n, _ in h.tool_calls] == ["outlook_list_events"]  # nobody outside: no Odoo, no mail


def test_once_per_meeting_again_when_it_moves(vault):
    h = host(event(32, agenda="Punti"))
    assert brief.run(h, vault, now=NOW)["esito"] == "inviato"
    assert brief.run(h, vault, now=NOW + timedelta(minutes=5))["esito"] == "nessuna riunione in arrivo"
    moved = host({**event(32, agenda="Punti"), "start": (NOW + timedelta(minutes=40)).isoformat(timespec="minutes")})
    again = brief.run(moved, vault, now=NOW + timedelta(minutes=10))
    assert again["esito"] == "inviato" and again["nota"] != "Riunioni/2026-10-16 15.02 Revisione Z20.md"
    state = json.loads((vault.root / STATE_DIR / brief.STATE_FILE).read_text())
    assert len(state["inviati"]) == 2 and state["ultimo_giro"]["esito"] == "inviato"
    assert brief.status(vault)[f"inviati_ultimi_{brief.KEEP_DAYS}_giorni"] == 2


def test_one_run_at_a_time(vault):
    import fcntl
    with open(vault.root / STATE_DIR / brief.LOCK_FILE, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert brief.run(host(event(32, agenda="Punti")), vault, now=NOW) == {"esito": "un altro giro è in corso"}
    assert brief.run(host(event(32, agenda="Punti")), vault, now=NOW)["esito"] == "inviato"


def test_a_note_someone_wrote_in_is_never_overwritten_and_a_failed_note_does_not_stop_the_brief(vault, monkeypatch):
    path = vault.root / "Riunioni" / "2026-10-16 15.02 Revisione Z20.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("Appunti miei\n", encoding="utf-8")
    assert brief.run(host(event(32, agenda="Punti")), vault, now=NOW)["esito"] == "inviato"
    assert path.read_text(encoding="utf-8") == "Appunti miei\n"
    for n, failure in enumerate((OSError("disk full"), UnicodeEncodeError("utf-8", "x", 0, 1, "bad"))):
        monkeypatch.setattr(brief, "create_exclusive", lambda *a, failure=failure: (_ for _ in ()).throw(failure))
        out = brief.run(host(event(33 + n, f"Altra {n}", agenda="Punti")), vault, now=NOW)
        assert out["esito"] == "inviato" and out["nota"] is None and out["testo"]


def test_calendar_unavailable_is_a_quiet_outcome(vault):
    out = brief.run(FakeHost(owner=OWNER), vault, now=NOW)
    assert out == {"esito": "calendario non disponibile: strumento non installato"}
    assert brief.status(vault)["ultimo_giro"]["esito"].startswith("calendario non disponibile")


def test_a_dry_run_shows_the_next_meeting_worth_a_brief_and_saves_nothing(vault):
    h = host(event(90, "Interna", organizer=f"Giorgio Conti <{MAILBOX}>", attendees=[PAOLO]), event(120, agenda="Punti"))
    out = brief.run(h, vault, now=NOW, send=False)
    assert out["esito"] == "prova" and out["riunione"] == "Revisione Z20" and "(tra 120 min)" in out["testo"]
    assert not (vault.root / STATE_DIR / brief.STATE_FILE).exists() and not list((vault.root / "Riunioni").glob("*.md"))
    tomorrow = brief.run(host(event(60 * 20, agenda="Punti")), vault, now=NOW, send=False)
    assert tomorrow["testo"].startswith("📅 Sab 17/10 alle 10:30: Revisione Z20")
    saturday = datetime(2026, 10, 24, 20, 0, tzinfo=ROME)  # summer time ends that night
    sunday = {**event(0), "start": "2026-10-25T10:00+01:00", "end": "2026-10-25T11:00+01:00"}
    after = brief.run(host(sunday), vault, now=saturday.astimezone(ROME).replace(tzinfo=None).astimezone(), send=False)
    assert after["testo"].startswith("📅 Dom 25/10 alle 10:00: Revisione Z20")


# ---- the command and the cron job ----------------------------------------------------------------


def test_due_prints_only_the_brief_and_saves_the_state_first(vault, monkeypatch, capsys):
    state = vault.root / STATE_DIR / brief.STATE_FILE

    class Stdout:  # the real stdout, which the cron delivers: the state must already say "sent"
        text = ""

        def write(self, chunk):
            assert json.loads(state.read_text())["inviati"]
            self.text += chunk

        def flush(self):
            pass

    out = Stdout()
    monkeypatch.setattr(sys, "__stdout__", out)
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault.root))
    monkeypatch.setattr("alfred_brain.host.HermesHost", lambda: host(event(32, agenda="Punti")))
    args = types.SimpleNamespace(at="2026-10-16 14:30", due=True)
    assert cli._brief(args) == 0
    assert out.text.startswith("📅 Alle 15:02 (tra 32 min): Revisione Z20") and out.text.endswith("«Punti»\n")
    captured = capsys.readouterr()
    assert captured.out == "" and '"esito": "inviato"' in captured.err
    out.text = ""
    assert cli._brief(args) == 0 and out.text == ""  # already briefed: a silent run


def test_setup_writes_the_script_then_creates_the_paused_job(tmp_path, monkeypatch):
    jobs, created, saved = [], [], []

    def create_job(prompt, schedule, **kw):
        if kw["name"] == brief.JOB:  # the cron reads the script at every run: it is there first
            assert (tmp_path / "scripts" / brief.SCRIPT).exists()
        created.append((prompt, schedule, kw))
        jobs.append({"name": kw["name"]})

    monkeypatch.setitem(sys.modules, "cron", types.ModuleType("cron"))
    monkeypatch.setitem(sys.modules, "cron.jobs", types.SimpleNamespace(create_job=create_job, list_jobs=lambda **kw: list(jobs)))
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: tmp_path))
    monkeypatch.setattr(cli, "_save_config", saved.append)
    monkeypatch.setattr(cli, "_plugin_entry", lambda: {"mcp_allowlist": ["plaud"]})
    monkeypatch.setattr(cli, "_mcp_servers", lambda: {"plaud": {}})
    assert cli._setup(types.SimpleNamespace()) == 0
    prompt, schedule, kw = next(c for c in created if c[2]["name"] == brief.JOB)
    assert (prompt, schedule) == ("", "every 5m")
    assert kw == {"name": brief.JOB, "deliver": "telegram", "no_agent": True, "script": brief.SCRIPT,
                  "attach_to_session": True, "failure_deliver": "local", "paused": True}
    assert {"cron": {"wrap_response": False}} in saved
    script = (tmp_path / "scripts" / brief.SCRIPT).read_text()
    assert '"brief", "--due"' in script and "sys.stdout = sys.stderr" in script
    compile(script, brief.SCRIPT, "exec")
    created.clear()
    assert cli._setup(types.SimpleNamespace()) == 0 and created == []  # idempotent


@pytest.mark.parametrize("value,expected", [("2026-10-16 14:30", (14, 30)), ("2026-10-16T09:05", (9, 5))])
def test_at_is_a_local_time(value, expected):
    when = brief.parse_now(value)
    assert (when.hour, when.minute) == expected and when.tzinfo is not None

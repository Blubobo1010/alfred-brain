import json
from datetime import date

import pytest
from alfred_brain import impegni
from alfred_brain.cli import evaluate
from alfred_brain.impegni import CommitmentError, Impegni
from conftest import FIXTURES, FakeHost

TODAY = date(2026, 10, 15)  # a Thursday
OWNER = "Luca Betti"


def store(vault):
    return Impegni(vault)


def memo(vault, acts, *, author=OWNER, offered=frozenset(), transcript="", fonte="[[Note vocali/m]]"):
    return store(vault).file_memo(acts, author=author, owner=OWNER, fonte=fonte, when=TODAY, transcript=transcript,
                                  offered=set(offered), today=TODAY)


def test_who_is_resolved_like_the_ceo_would_mean_it(vault):
    vault.add_facts("persona", "Andrea Lorini", [("Consulente.", "", "")], alias=["Andrea"])

    def who(name, author=OWNER):
        return impegni.person(vault, name, owner=OWNER, author=author)

    assert who("io") == who("") == who("Noi") == OWNER
    assert who("io", author="Fabrizio") == "Fabrizio"  # a colleague's memo: "io" is the colleague
    assert who("Luca") == who("luca betti") == who("il CEO") == OWNER
    assert who("Andrea") == who("andrea lorini") == "Andrea Lorini"
    assert who("Alfred") is None
    assert who("Marco [[Bianchi]] 📅") == "Marco Bianchi"
    assert impegni.person(vault, "Luca", owner="il CEO", author="il CEO") == "Luca"  # no name configured: just a name


def test_a_memo_files_numbered_commitments_and_matches_open_ones(vault):
    first = memo(vault, [{"chi": "Andrea Lorini", "cosa": "mandare i 3 compiti", "entro": "2026-10-16",
                          "evidenza": "Andrea mi manda i tre compiti entro venerdì"},
                         {"chi": "io", "cosa": "chiamare il fornitore dei pannelli", "entro": "venerdì"}],
                 transcript="Andrea mi manda i tre compiti entro venerdì. Io chiamo il fornitore.")
    assert [(c["id"], c["chi"], c["entro"], c["nuovo"]) for c in first] == [
        (1, "Andrea Lorini", "2026-10-16", True), (2, OWNER, "", True)]
    again = memo(vault, [{"chi": "Andrea", "cosa": "mandare i compiti di digitalizzazione", "stesso_di": "#1", "entro": "2026-10-17"},
                         {"chi": "io", "cosa": "Chiamare il fornitore dei pannelli.", "entro": "2026-10-19"},
                         {"chi": "Marco", "cosa": "mandare la bozza", "stesso_di": 2}], offered={1})
    assert [(c["id"], c["nuovo"]) for c in again] == [(1, False), (2, False), (3, True)]  # #2 was not offered: a new one
    assert again[0]["entro_detto"] == "2026-10-17" and again[1]["entro_detto"] == ""
    data = store(vault).load()
    assert data["prossimo"] == 4 and data["evidenza"] == {"date": 1, "trovate": 1}
    assert data["impegni"][0]["entro"] == "2026-10-16"  # a memo never moves a date: it only reports the other one
    assert data["impegni"][1]["entro"] == "2026-10-19"  # ...but fills in a missing one
    line = impegni.report_line(again[0], OWNER, TODAY)
    assert line == "#1 (già aperto) Andrea Lorini — mandare i 3 compiti — entro ven 16/10 (la nota dice sab 17/10)"


def test_the_model_saying_same_is_not_enough(vault):
    memo(vault, [{"chi": "Andrea Lorini", "cosa": "inviare i compiti per il CRM", "entro": "2026-10-16"}])
    out = memo(vault, [{"chi": "Andrea", "cosa": "mandare il confronto con l'altro fornitore", "stesso_di": 1, "entro": "2026-10-26"}],
               offered={1})
    assert (out[0]["id"], out[0]["nuovo"]) == (2, True)
    assert [i["entro"] for i in store(vault).load()["impegni"]] == ["2026-10-16", "2026-10-26"]
    wrong_owner = memo(vault, [{"chi": "Marco", "cosa": "inviare i compiti per il CRM", "stesso_di": 1}], offered={1})
    assert wrong_owner[0]["nuovo"] is True
    lazy = memo(vault, [{"chi": "Andrea", "cosa": "inviare", "stesso_di": 1,  # one word: its quote is the action
                         "evidenza": "Andrea mi deve mandare entro lunedì il confronto con l'altro fornitore."}], offered={1})
    assert lazy[0]["nuovo"] is True and lazy[0]["cosa"] == "Andrea mi deve mandare entro lunedì il confronto con l'altro fornitore"


def test_the_view_is_rewritten_in_obsidian_tasks_order(vault):
    memo(vault, [{"chi": "Andrea Lorini", "cosa": "mandare i compiti", "entro": "2026-10-16"}], fonte="[[Note vocali/2026-10-15 Riunione]]")
    view = (vault.root / "Impegni.md").read_text(encoding="utf-8")
    assert "- [ ] #1 Andrea Lorini: mandare i compiti · fonte [[Note vocali/2026-10-15 Riunione]] 📅 2026-10-16" in view
    assert "chiedilo in chat" in view
    store(vault).modify(1, stato="fatto", owner=OWNER, author=OWNER, today=TODAY)
    view = (vault.root / "Impegni.md").read_text(encoding="utf-8")
    assert "_Nessun impegno aperto._" in view and view.rstrip().endswith("📅 2026-10-16 ✅ 2026-10-15")
    store(vault).modify(1, stato="annullato", owner=OWNER, author=OWNER, today=date(2026, 12, 1))
    view = (vault.root / "Impegni.md").read_text(encoding="utf-8")
    assert "- [-] #1 Andrea Lorini: mandare i compiti" in view and view.rstrip().endswith("📅 2026-10-16 ❌ 2026-12-01")


def test_closed_ones_leave_the_view_after_30_days_but_stay_in_the_store(vault):
    memo(vault, [{"chi": "io", "cosa": "firmare il contratto"}])
    store(vault).modify(1, stato="fatto", owner=OWNER, author=OWNER, today=TODAY)
    store(vault)._render(store(vault).load(), date(2026, 11, 20))
    assert "#1" not in (vault.root / "Impegni.md").read_text(encoding="utf-8")
    assert store(vault).load()["impegni"][0]["stato"] == "fatto"


def test_corrections_are_counted_but_closing_is_not(vault):
    memo(vault, [{"chi": "Andrea", "cosa": "mandare i compiti"}, {"chi": "io", "cosa": "chiamare Beta"}])
    s = store(vault)
    s.modify(1, chi="Marco Bianchi", entro="2026-10-19", owner=OWNER, author=OWNER, today=TODAY)
    s.modify(2, stato="fatto", owner=OWNER, author=OWNER, today=TODAY)
    s.modify(2, stato="aperto", owner=OWNER, author=OWNER, today=TODAY)
    s.modify(2, stato="annullato", owner=OWNER, author=OWNER, today=TODAY)
    assert s.load()["correzioni"] == {"modificati": 1, "annullati": 1, "riaperti": 1}
    stats = s.stats(TODAY)
    assert stats["aperti"] == 1 and stats["tasso_correzione"] == 1.5
    with pytest.raises(CommitmentError, match="no commitment"):
        s.modify(99, stato="fatto", owner=OWNER, author=OWNER)
    with pytest.raises(CommitmentError, match="YYYY-MM-DD"):
        s.modify(1, entro="venerdì", owner=OWNER, author=OWNER)
    with pytest.raises(CommitmentError, match="stato"):
        s.modify(1, stato="boh", owner=OWNER, author=OWNER)


def test_chat_additions_are_deduplicated(vault):
    s = store(vault)
    one = s.add("Marco", "mandare il disegno del cofano", "2026-10-16", owner=OWNER, author=OWNER, fonte="[[Diario/2026-10-15]]", today=TODAY)
    two = s.add("marco", "Mandare il disegno del cofano", "", owner=OWNER, author=OWNER, fonte="[[Diario/2026-10-15]]", today=TODAY)
    assert (one["id"], one["nuovo"], two["id"], two["nuovo"]) == (1, True, 1, False)
    with pytest.raises(CommitmentError, match="who"):
        s.add("Alfred", "ricordarmelo", owner=OWNER, author=OWNER, fonte="")


def test_listing_filters_and_puts_overdue_first(vault):
    memo(vault, [{"chi": "Andrea Lorini", "cosa": "a", "entro": "2026-10-20"}, {"chi": "Andrea Lorini", "cosa": "b", "entro": "2026-10-10"},
                 {"chi": "io", "cosa": "c"}, {"chi": "Andrea Lorini", "cosa": "d"}])
    s = store(vault)
    assert [i["cosa"] for i in s.listing(chi="Andrea", owner=OWNER, author=OWNER, today=TODAY)] == ["b", "a", "d"]
    assert [i["cosa"] for i in s.listing(chi="io", owner=OWNER, author=OWNER, today=TODAY)] == ["c"]
    assert [i["cosa"] for i in s.listing(scaduti=True, owner=OWNER, author=OWNER, today=TODAY)] == ["b"]
    line = impegni.report_line({**s.listing(scaduti=True, owner=OWNER, author=OWNER, today=TODAY)[0], "nuovo": True}, OWNER, TODAY)
    assert line == "#2 Andrea Lorini — b — entro sab 10/10 (scaduto)"
    assert impegni.report_line({**s.listing(chi="io", owner=OWNER, author=OWNER)[0], "nuovo": True}, OWNER) == "#3 Tu — c"


def test_prompt_offers_the_people_named_and_the_ceo_s_own(vault):
    memo(vault, [{"chi": "Andrea Lorini", "cosa": "mandare i compiti"}, {"chi": "Marco Bianchi", "cosa": "mandare la bozza"},
                 {"chi": "io", "cosa": "firmare"}])
    block, offered = store(vault).prompt_block("Ho sentito Andrea per i compiti.", owner=OWNER, author=OWNER)
    assert offered == {1, 3} and "#1 Andrea Lorini — mandare i compiti" in block and "Marco" not in block


def test_prefetch_names_people_and_projects_but_not_common_words(vault):
    memo(vault, [{"chi": "Andrea Lorini", "cosa": "mandare i compiti"}, {"chi": "io", "cosa": "chiudere il preventivo Z20"}])
    s = store(vault)
    assert "#1 Andrea Lorini — mandare i compiti" in s.prefetch_block("Cosa deve fare Andrea?", set(), TODAY)
    assert "#2" in s.prefetch_block("A che punto è la Z20?", {"z20"}, TODAY)
    assert s.prefetch_block("Il tempo è bello", set(), TODAY) == ""
    ceo = Impegni(vault)
    ceo.add("il CEO", "pagare", owner="il CEO", author="il CEO", fonte="")
    assert "pagare" not in ceo.prefetch_block("Il preventivo è arrivato", set(), TODAY)  # "il" is not a name


def test_a_damaged_file_is_an_error_never_an_empty_store(vault):
    path = vault.root / ".alfred" / impegni.FILE
    path.write_text("{non json", encoding="utf-8")
    with pytest.raises(CommitmentError, match="git history"):
        store(vault).load()
    with pytest.raises(CommitmentError):
        store(vault).add("io", "x", owner=OWNER, author=OWNER, fonte="")
    assert path.read_text(encoding="utf-8") == "{non json"


def test_small_helpers():
    assert impegni.calendar(TODAY, days=1) == "oggi (giovedì 15/10) = 2026-10-15; venerdì 16/10 = 2026-10-16; fine mese = 2026-10-31"
    week = impegni.calendar(TODAY)
    assert week.count("lunedì") == 1 and "mercoledì 21/10 = 2026-10-21" in week and "giovedì 22/10 = 2026-10-22" in week
    monday = impegni.calendar(date(2026, 10, 19))  # a Monday memo: "lunedì" is the next one, never today
    assert "oggi (lunedì 19/10)" in monday and "lunedì 26/10 = 2026-10-26" in monday and monday.count("lunedì") == 2
    assert impegni.calendar(date(2026, 12, 30), days=0).endswith("fine mese = 2026-12-31")
    assert impegni.calendar(date(2028, 2, 3), days=0).endswith("fine mese = 2028-02-29")
    assert impegni.parse_due("2026-10-16T10:00") == "2026-10-16" and impegni.parse_due("venerdì") == ""
    assert impegni.parse_ref("#12") == 12 and impegni.parse_ref(0) is None and impegni.parse_ref("") is None
    assert impegni.evidence_found("mi manda i tre compiti entro venerdì", "Andrea mi manda i tre compiti entro venerdì") is True
    assert impegni.evidence_found("firmare con Gamma", "Andrea mi manda i compiti") is False
    assert impegni.evidence_found("", "x") is None
    assert impegni.actions(["chiamare Beta", {"chi": "Marco"}, 7, {"cosa": "x", "altro": 1}]) == [
        {"chi": None, "cosa": "chiamare Beta", "entro": None, "stesso_di": None, "evidenza": None},
        {"chi": None, "cosa": "x", "entro": None, "stesso_di": None, "evidenza": None}]


def test_eval_scores_the_configured_model_on_labelled_memos(vault):
    lines = (FIXTURES / "eval-sintetico.jsonl").read_text(encoding="utf-8").splitlines()
    labels = [json.loads(line) for line in lines]

    def model(text):  # a fair model: right on most, one wrong owner, one wrong date, one invented action
        record = next(r for r in labels if r["trascrizione"][:40] in text)
        acts = [{**a, "evidenza": a["cosa"]} for a in record["impegni"]]
        if "Gamma Logistica" in record["trascrizione"]:
            acts[1] = {**acts[1], "chi": "Andrea Lorini"}
        if "piano ferie" in record["trascrizione"]:
            acts[0] = {**acts[0], "entro": "2026-10-21"}
        if "fibra di lino" in record["trascrizione"]:
            acts = [{"chi": "io", "cosa": "provare i pannelli in fibra di lino"}]
        return {"titolo": "x", "azioni": acts}

    result = evaluate(FakeHost(llm=model, owner=OWNER), vault, lines)
    assert (result["memo"], result["attesi"], result["previsti"]) == (5, 7, 8)
    assert result["risposto_da"] == ["fake/modello"]
    assert (result["precisione"], result["richiamo"]) == (0.88, 1.0)
    assert result["responsabile_giusto"] == 0.86 and result["data_giusta"] == 0.8
    assert result["dettagli"] == [{"trascrizione": labels[2]["trascrizione"][:80], "mancati": [],
                                   "in_piu": ["provare i pannelli in fibra di lino"], "sbagliati": []},
                                  {"trascrizione": labels[3]["trascrizione"][:80], "mancati": [], "in_piu": [],
                                   "sbagliati": ["mandare la bozza aggiornata del contratto: chi 'Andrea Lorini', atteso 'Marco Bianchi'"]},
                                  {"trascrizione": labels[4]["trascrizione"][:80], "mancati": [], "in_piu": [],
                                   "sbagliati": ["preparare il piano ferie: entro 2026-10-21, atteso 2026-10-20"]}]


def test_names_compare_without_titles_and_by_their_words():
    assert impegni.same_owner("Marco", "Marco Bianchi") and impegni.same_owner("dottoressa Ferri", "Ferri")
    assert impegni.same_owner("la dott.ssa Ferri", "Ferri")  # "ssa" is left over, Ferri is still within
    assert not impegni.same_owner("Marco Rossi", "Marco Bianchi") and not impegni.same_owner("", "Marco")


def test_the_ceo_is_recognised_however_telegram_writes_his_name():
    for name in ("Luca", "Luca Betti", "Luca B.", "Betti Luca", "Dott. Luca Betti", "Luca Betti | Kimera", ""):
        assert impegni.same_person(name, OWNER), name
    assert not impegni.same_person("Andrea Lorini", OWNER)
    assert impegni.same_person("Andrea Lorini", "il CEO")  # no name configured: whoever writes is the CEO


def test_a_short_name_and_the_full_one_are_the_same_owner_everywhere(vault):
    memo(vault, [{"chi": "Andrea", "cosa": "mandare i compiti"}])
    vault.add_facts("persona", "Andrea Lorini", [("Consulente.", "", "")], alias=["Andrea"])
    s = store(vault)
    assert [i["id"] for i in s.listing(chi="Andrea Lorini", owner=OWNER, author=OWNER)] == [1]
    assert [i["id"] for i in s.listing(chi="Andrea", owner=OWNER, author=OWNER)] == [1]
    again = s.add("Andrea Lorini", "mandare i compiti", owner=OWNER, author=OWNER, fonte="")
    assert again["id"] == 1 and again["nuovo"] is False


def test_an_empty_date_in_a_correction_changes_nothing(vault):
    memo(vault, [{"chi": "Andrea", "cosa": "mandare i compiti", "entro": "2026-10-16"}])
    s = store(vault)
    s.modify(1, stato="fatto", entro="", owner=OWNER, author=OWNER, today=TODAY)
    data = s.load()
    assert data["impegni"][0]["entro"] == "2026-10-16" and data["correzioni"]["modificati"] == 0
    s.modify(1, entro="nessuna", owner=OWNER, author=OWNER, today=TODAY)
    assert s.load()["impegni"][0]["entro"] == "" and s.load()["correzioni"]["modificati"] == 1


def test_common_words_in_an_owner_never_match_a_message(vault):
    memo(vault, [{"chi": "il responsabile della produzione", "cosa": "ordinare i cerchi"}])
    assert store(vault).prefetch_block("Novità della settimana?", set(), TODAY) == ""
    assert "#1" in store(vault).prefetch_block("Cosa deve fare il responsabile?", set(), TODAY)


def test_entries_without_their_fields_are_damage_not_a_crash(vault):
    path = vault.root / ".alfred" / impegni.FILE
    path.write_text(json.dumps({"prossimo": 2, "impegni": [{"id": 1, "chi": "Marco"}]}), encoding="utf-8")
    with pytest.raises(CommitmentError, match="entry"):
        store(vault).load()
    path.write_bytes(b"\xff\xfe{")
    with pytest.raises(CommitmentError, match="damaged"):
        store(vault).open_items()


def test_lists_given_as_one_string_are_one_item(vault):
    assert impegni.actions("chiamare Beta") == [{"chi": None, "cosa": "chiamare Beta", "entro": None, "stesso_di": None,
                                                 "evidenza": None}]
    assert impegni.actions({"chi": "Marco", "cosa": "x"})[0]["chi"] == "Marco"

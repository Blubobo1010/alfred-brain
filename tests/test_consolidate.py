from datetime import date, datetime

from alfred_brain import consolidate, journal
from alfred_brain.vault import parse_note
from conftest import FakeHost

EXTRACT = {"entita": [
    {"tipo": "cliente", "nome": "Kimera Automobili", "alias": ["Kimera"], "fatti": [{"testo": "Ha firmato il contratto K39.", "data": "2026-10-12"}]},
    {"tipo": "persona", "nome": "Marco Bianchi", "fatti": [{"testo": "Conferma la consegna a marzo."}]},
    {"tipo": "evento", "nome": "ignored", "fatti": [{"testo": "bad tipo"}]},
]}


def test_journal_consolidation_writes_entities_with_source(vault):
    journal.append_turn(vault, "Kimera ha firmato?", "Sì, la K39.", author="Luca", when=datetime(2026, 10, 12, 10, 0))
    host = FakeHost(llm=EXTRACT)
    report = consolidate.run(host, vault, today=date(2026, 10, 12))
    assert report == {"chunks": 1, "facts": 2, "failed": 0}
    kimera = parse_note(vault.root / "Clienti" / "Kimera Automobili.md")
    assert "Ha firmato il contratto K39. ([[Diario/2026-10-12]])" in kimera.body
    assert "Kimera Automobili" in (vault.root / "Indice.md").read_text(encoding="utf-8")
    assert journal.pending_chunks(vault) == []
    assert consolidate.run(host, vault)["chunks"] == 0  # nothing new: no model call
    assert len(host.llm_calls) == 1


def test_known_entities_are_offered_to_the_model(vault):
    vault.add_facts("cliente", "Kimera Automobili", [("x", "", "")])
    journal.append_turn(vault, "Novità su Kimera", "Nessuna", when=datetime(2026, 10, 12, 10, 0))
    host = FakeHost()
    consolidate.run(host, vault, today=date(2026, 10, 12))
    assert "Kimera Automobili" in host.llm_calls[0] and "Oggi è 2026-10-12" in host.llm_calls[0]


def test_malformed_model_output_keeps_the_chunk_pending(vault):
    journal.append_turn(vault, "Kimera?", "Sì", when=datetime(2026, 10, 12, 10, 0))
    report = consolidate.run(FakeHost(llm={"wrong": 1}), vault)
    assert report["failed"] == 1 and len(journal.pending_chunks(vault)) == 1
    assert consolidate.run(FakeHost(llm=EXTRACT), vault)["chunks"] == 1


def test_voice_memos_are_consolidated_with_their_note_as_source(vault, tmp_path):
    from alfred_brain.voice import VoiceMemos
    audio = tmp_path / "memo.m4a"
    audio.write_bytes(b"x")
    info = {"titolo": "Preventivo Beta", "categoria": "decisione", "area": "fornitori", "riassunto": [], "azioni": []}
    out = VoiceMemos(FakeHost(llm=[info], transcripts={"memo.m4a": "Firmiamo con Beta Compositi."}), vault).run(audio, fonte="telegram")
    host = FakeHost(llm={"entita": [{"tipo": "cliente", "nome": "Beta Compositi", "fatti": [{"testo": "Kimera firma con Beta."}]}]})
    report = consolidate.run(host, vault, today=date(2026, 10, 14))
    assert report["chunks"] == 1 and vault.state()["voice_pending"] == []
    assert "Firmiamo con Beta Compositi." in host.llm_calls[0]
    assert f"([[{out['note'][:-3]}]])" in (vault.root / "Clienti" / "Beta Compositi.md").read_text(encoding="utf-8")


def test_a_queued_note_deleted_by_hand_leaves_the_queue(vault):
    vault.save_state({"voice_pending": ["Note vocali/sparita.md"], "plaud_pending": ["Plaud/sparita.md"]})
    report = consolidate.run(FakeHost(), vault)
    assert report["failed"] == 0 and vault.state()["voice_pending"] == [] and vault.state()["plaud_pending"] == []


def test_voice_source_is_the_transcript_when_it_fits_else_the_summary():
    note = ("---\ntipo: nota vocale\ndata: 2026-10-14\nautore: Fabrizio\n---\n# Idea pannelli\n\nCategoria: idea · Area: fornitori\n\n"
            "## Riassunto\n\n- Sintesi del modello\n\n## Da fare\n\n_Niente da fare._\n\n## Trascrizione\n\nDetto a voce.\n")
    assert consolidate.voice_source_text(note) == "# Idea pannelli\nRegistrata il 2026-10-14\nAutore: Fabrizio\n\nDetto a voce."
    long = consolidate.voice_source_text(note.replace("Detto a voce.", "parola " * 3000))
    assert "Sintesi del modello" in long and "parola" not in long


def test_updates_carry_their_author_and_voice_memos_leave_tasks_to_the_commitments(vault):
    (vault.root / "Aggiornamenti" / "a.md").write_text("---\ntipo: aggiornamento\nautore: Marco\n---\n# A\n\nIo chiudo V03.", encoding="utf-8")
    (vault.root / "Note vocali" / "v.md").write_text("---\ndata: 2026-10-14\n---\n# V\n\n## Trascrizione\n\nMarco manda i disegni.", encoding="utf-8")
    vault.save_state({"inbox_pending": ["Aggiornamenti/a.md"], "voice_pending": ["Note vocali/v.md"]})
    host = FakeHost()
    assert consolidate.run(host, vault)["chunks"] == 2
    update = host.llm_calls[host.instructions.index(consolidate.INSTRUCTIONS)]
    assert update.split("\n\n", 1)[1].startswith("Autore: Marco\n---")
    assert consolidate.VOICE_INSTRUCTIONS in host.instructions  # the memo: its actions are commitments


def test_an_unconstrained_answer_is_normalised_instead_of_dropped(vault):
    """What a retry without the schema may bring back: synonyms of the types, facts as bare strings."""
    journal.append_turn(vault, "Beta?", "Ok", when=datetime(2026, 10, 12, 10, 0))
    host = FakeHost(llm={"entita": [{"tipo": "Azienda", "nome": "Beta Compositi", "fatti": ["Fornisce i pannelli."]},
                                    {"tipo": "fornitore", "nome": "Gamma", "fatti": [{"testo": "Consegna a marzo."}]}]})
    assert consolidate.run(host, vault, today=date(2026, 10, 12))["facts"] == 2
    assert "Fornisce i pannelli." in (vault.root / "Clienti" / "Beta Compositi.md").read_text(encoding="utf-8")
    assert (vault.root / "Clienti" / "Gamma.md").exists()


def _turns(vault, *names):
    for i, name in enumerate(names):
        journal.append_turn(vault, f"Novità su {name}?", "Nessuna.", when=datetime(2026, 10, 10 + i, 10, 0))


def test_a_source_the_model_cannot_answer_is_skipped_once_the_model_works_elsewhere(vault):
    _turns(vault, "Alfa", "Beta")

    def answer(text):
        return {"wrong": 1} if "Alfa" in text else {"entita": []}

    host = FakeHost(llm=answer)
    first = consolidate.run(host, vault)
    assert (first["chunks"], first["failed"]) == (1, 1)  # Alfa's day waits, Beta's goes on
    assert len(journal.pending_chunks(vault)) == 1
    consolidate.run(host, vault)
    third = consolidate.run(host, vault)
    assert third["skipped"] == 1 and journal.pending_chunks(vault) == []
    state = vault.state()
    assert state["consolidation_attempts"] == {} and "Diario/2026-10-10.md" in state["consolidation_skipped"][0]["fonte"]


def test_a_model_that_answers_garbage_to_everything_skips_nothing(vault):
    _turns(vault, "Alfa", "Beta", "Gamma")
    host = FakeHost(llm=lambda _t: {"wrong": 1})
    for _ in range(5):
        report = consolidate.run(host, vault)
        assert report["stopped"] and report["failed"] == 2  # two failures in a row end the pass
    assert len(journal.pending_chunks(vault)) == 3 and "consolidation_skipped" not in vault.state()


def test_timeouts_are_not_counted_against_the_source(vault):
    _turns(vault, "Alfa")
    host = FakeHost(llm=lambda _t: TimeoutError("model timed out"))
    for _ in range(4):
        assert consolidate.run(host, vault)["failed"] == 1
    assert vault.state().get("consolidation_attempts", {}) == {} and len(journal.pending_chunks(vault)) == 1


def test_sources_that_failed_before_go_last(vault):
    _turns(vault, "Alfa", "Beta", "Gamma")
    attempts = {"journal:Diario/2026-10-10.md@0": {"tentativi": 1, "dal": "2026-10-10T10:00:00"}}
    vault.save_state({**vault.state(), "consolidation_attempts": attempts})
    host = FakeHost()
    consolidate.run(host, vault)
    assert ["Alfa" in t for t in host.llm_calls] == [False, False, True]


def test_plaud_source_prefers_the_summary():
    note = "# T\n\n## Riassunto Plaud\n\n" + "Riassunto lungo " * 10 + "\n\n## Trascrizione\n\nSEGRETO"
    assert "SEGRETO" not in consolidate.plaud_source_text(note)
    empty = "# T\n\n## Riassunto Plaud\n\n_Nessun riassunto._\n\n## Trascrizione\n\nTesto parlato"
    assert "Testo parlato" in consolidate.plaud_source_text(empty)


def test_a_lone_string_of_facts_is_one_fact_not_one_per_letter(vault):
    journal.append_turn(vault, "Marco?", "Ok", when=datetime(2026, 10, 12, 10, 0))
    host = FakeHost(llm={"entita": {"tipo": "persona", "nome": "Marco Bianchi", "fatti": "Marco è il responsabile acquisti."}})
    assert consolidate.run(host, vault)["facts"] == 1
    assert "- " in (vault.root / "Persone" / "Marco Bianchi.md").read_text(encoding="utf-8").split("alfred:inizio")[1]


class _HttpError(RuntimeError):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


def test_a_request_the_server_always_refuses_counts_against_the_source(vault):
    assert consolidate.counts_against_source(_HttpError(400)) and consolidate.counts_against_source(_HttpError(413))
    assert not consolidate.counts_against_source(_HttpError(503)) and not consolidate.counts_against_source(TimeoutError())

    class ContextLengthExceededError(Exception):
        pass

    assert consolidate.counts_against_source(ContextLengthExceededError())

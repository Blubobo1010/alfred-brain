from datetime import date, datetime

from alfred_brain import consolidate, journal
from alfred_brain.vault import parse_note
from conftest import FakeHost

EXTRACT = {"entita": [
    {"tipo": "cliente", "nome": "Kimera Automobili", "alias": ["Kimera"], "fatti": [{"testo": "Ha firmato il contratto K39.", "data": "2026-10-12"}]},
    {"tipo": "persona", "nome": "Marco Bianchi", "fatti": [{"testo": "Conferma la consegna a marzo."}]},
    {"tipo": "azienda", "nome": "ignored", "fatti": [{"testo": "bad tipo"}]},
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
    note = ("---\ntipo: nota vocale\ndata: 2026-10-14\n---\n# Idea pannelli\n\nCategoria: idea · Area: fornitori\n\n"
            "## Riassunto\n\n- Sintesi del modello\n\n## Da fare\n\n_Niente da fare._\n\n## Trascrizione\n\nDetto a voce.\n")
    assert consolidate.voice_source_text(note) == "# Idea pannelli\nRegistrata il 2026-10-14\n\nDetto a voce."
    long = consolidate.voice_source_text(note.replace("Detto a voce.", "parola " * 3000))
    assert "Sintesi del modello" in long and "parola" not in long


def test_plaud_source_prefers_the_summary():
    note = "# T\n\n## Riassunto Plaud\n\n" + "Riassunto lungo " * 10 + "\n\n## Trascrizione\n\nSEGRETO"
    assert "SEGRETO" not in consolidate.plaud_source_text(note)
    empty = "# T\n\n## Riassunto Plaud\n\n_Nessun riassunto._\n\n## Trascrizione\n\nTesto parlato"
    assert "Testo parlato" in consolidate.plaud_source_text(empty)

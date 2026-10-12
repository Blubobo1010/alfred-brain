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


def test_plaud_source_prefers_the_summary():
    note = "# T\n\n## Riassunto Plaud\n\n" + "Riassunto lungo " * 10 + "\n\n## Trascrizione\n\nSEGRETO"
    assert "SEGRETO" not in consolidate.plaud_source_text(note)
    empty = "# T\n\n## Riassunto Plaud\n\n_Nessun riassunto._\n\n## Trascrizione\n\nTesto parlato"
    assert "Testo parlato" in consolidate.plaud_source_text(empty)

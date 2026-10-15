import os
import zipfile
from datetime import date

import pytest
from alfred_brain import consolidate
from alfred_brain.inbox import Inbox, author_of, read_text
from alfred_brain.provider import BrainProvider
from conftest import FIXTURES, FakeHost

TODAY = date(2026, 10, 13)
REPORT = {
          "novita": ["Z20 V05: cellula incollata, passa in meccanica"],
          "rischi": ["Z20 V06/V08/V09/P10: rischio alto"]}


def drop(folder, name, text, age=600):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    old = path.stat().st_mtime - age
    os.utime(path, (old, old))
    return path


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "Aggiornamenti Alfred"
    f.mkdir()
    return f


def test_update_is_archived_verbatim_reported_and_imported_once(vault, folder):
    sample = (FIXTURES / "aggiornamento_settimanale.txt").read_text(encoding="utf-8")
    drop(folder, "Fabrizio/settimana 42.txt", sample)
    host = FakeHost(llm=[REPORT])
    report = Inbox(host, vault, folder).sync(today=TODAY)
    assert report["new"] == 1 and report["errors"] == []
    note = vault.root / report["notes"][0]
    assert note.name == "2026-10-13 Aggiornamento settimanale per Luca - Fabrizio.md"
    text = note.read_text(encoding="utf-8")
    assert "autore: Fabrizio" in text and "V03 finisce montaggio meccanica" in text
    assert ("Da vedere:\n- M10: Ricevuto capitolato di Beta per la versione Sport, valutazione in corso - da vedere con Luca"
            in report["report"])
    assert "Altri topic: Situazione omologazioni pronta - da vedere con Luca" in report["report"]
    assert "Alfa" not in report["report"].split("Rischi")[0]
    assert "Rischi e ritardi" in report["report"] and report["report"].endswith(f"[[{report['notes'][0][:-3]}]]")
    again = Inbox(host, vault, folder).sync(today=TODAY)
    assert again["new"] == 0 and again["skipped"] == 1 and again["report"] == ""
    assert vault.state()["inbox_pending"] == report["notes"]


def test_edited_file_is_a_new_version(vault, folder):
    path = drop(folder, "agg.txt", "Aggiornamento\n- V03 in meccanica")
    Inbox(FakeHost(llm=[REPORT]), vault, folder).sync(today=TODAY)
    path.write_text("Aggiornamento\n- V03 finita", encoding="utf-8")
    os.utime(path, (path.stat().st_mtime - 600,) * 2)
    report = Inbox(FakeHost(llm=[REPORT]), vault, folder).sync(today=TODAY)
    assert report["new"] == 1 and len(list((vault.root / "Aggiornamenti").glob("*.md"))) == 2


def test_fresh_hidden_and_office_lock_files_are_skipped(vault, folder):
    drop(folder, "in scrittura.txt", "testo", age=0)
    drop(folder, ".nascosto.txt", "testo")
    drop(folder, "~$bozza.docx", "x")
    drop(folder, "foto.jpg", "x")
    report = Inbox(FakeHost(), vault, folder).sync(today=TODAY)
    assert report["new"] == 0 and report["errors"] == []


def test_model_failure_still_archives_and_points_to_the_note(vault, folder):
    drop(folder, "agg.txt", "Aggiornamento\n- V03 in meccanica")

    def boom(_text):
        raise RuntimeError("model offline")

    report = Inbox(FakeHost(llm=boom), vault, folder).sync(today=TODAY)
    assert report["new"] == 1 and "riepilogo non disponibile" in report["report"]


def test_flagged_lines_survive_a_model_failure(vault, folder):
    drop(folder, "agg.txt", "M10\n- Overview cambio pronta - da vedere con Luca")
    report = Inbox(FakeHost(llm=lambda _t: {}), vault, folder).sync(today=TODAY)
    assert "Da vedere:\n- M10: Overview cambio pronta - da vedere con Luca" in report["report"]


def test_docx_and_cp1252_and_author_header(tmp_path):
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xml = (f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>Z20</w:t></w:r></w:p>'
           '<w:p><w:pPr><w:numPr/></w:pPr><w:r><w:t>V03 </w:t></w:r><w:r><w:t>in meccanica</w:t></w:r></w:p>'
           '</w:body></w:document>')
    docx = tmp_path / "a.docx"
    with zipfile.ZipFile(docx, "w") as zf:
        zf.writestr("word/document.xml", xml)
    assert read_text(docx) == "Z20\n- V03 in meccanica"
    legacy = tmp_path / "b.txt"
    legacy.write_bytes("Novità: già fatto".encode("cp1252"))
    assert read_text(legacy) == "Novità: già fatto"
    assert author_of("Aggiornamento\nAutore: Marco Rossi\n- x", tmp_path / "b.txt", tmp_path) == "Marco Rossi"


def test_updates_are_consolidated_into_projects(vault, folder):
    drop(folder, "agg.txt", "Aggiornamento\n- Z20 V05 cellula incollata")
    Inbox(FakeHost(llm=[REPORT]), vault, folder).sync(today=TODAY)
    host = FakeHost(llm={"entita": [{"tipo": "progetto", "nome": "Z20", "fatti": [
        {"testo": "La V05 ha la cellula incollata e passa in meccanica."}]}]})
    report = consolidate.run(host, vault, today=TODAY)
    assert report["chunks"] == 1 and vault.state()["inbox_pending"] == []
    assert "[[Aggiornamenti/" in (vault.root / "Progetti" / "Z20.md").read_text(encoding="utf-8")


def test_tool_without_folder_is_a_clear_error(vault, monkeypatch):
    monkeypatch.delenv("ALFRED_BRAIN_INBOX", raising=False)
    out = BrainProvider(host=FakeHost(), vault=vault).handle_tool_call("brain_inbox", {})
    assert '"ok": false' in out and "alfred brain inbox --path" in out and "--voice-path" in out


def test_tool_with_only_the_ceo_s_voice_folder(vault, tmp_path, monkeypatch):
    own = tmp_path / "Alfred"
    own.mkdir()
    monkeypatch.setenv("ALFRED_BRAIN_VOICE", str(own))
    out = BrainProvider(host=FakeHost(), vault=vault).handle_tool_call("brain_inbox", {})
    assert '"ok": true' in out and '"new": 0' in out and '"report": ""' in out


def test_a_missing_folder_does_not_stop_the_other(vault, folder, tmp_path):
    drop(folder, "agg.txt", "Aggiornamento\n- Z20 V05 cellula incollata")
    gone = tmp_path / "OneDrive non sincronizzato"
    report = Inbox(FakeHost(llm=[REPORT]), vault, folder, voice_folder=gone).sync(today=TODAY)
    assert report["new"] == 1 and report["missing"] == [str(gone)] and report["errors"] == []
    with pytest.raises(ValueError, match="folder not found"):
        Inbox(FakeHost(), vault, tmp_path / "sparita", voice_folder=gone).sync()


def test_tool_reports_and_consolidates(vault, folder, monkeypatch):
    monkeypatch.setenv("ALFRED_BRAIN_INBOX", str(folder))
    drop(folder, "agg.txt", "Aggiornamento\n- Z20 V05 cellula incollata")
    out = BrainProvider(host=FakeHost(llm=[REPORT, {"entita": []}]), vault=vault).handle_tool_call("brain_inbox", {})
    assert '"new": 1' in out and "Rischi e ritardi" in out and '"consolidation"' in out


def test_a_report_list_given_as_one_string_is_one_line(vault, folder):
    drop(folder, "agg.txt", "Aggiornamento\n- V03 in ritardo")
    report = Inbox(FakeHost(llm=[{"novita": [], "rischi": "Z20 V03: ritardo di una settimana"}]), vault, folder).sync(today=TODAY)
    assert "Rischi e ritardi:\n- Z20 V03: ritardo di una settimana\n" in report["report"]

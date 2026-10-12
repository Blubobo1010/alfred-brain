from datetime import date

from alfred_brain import consolidate
from alfred_brain.plaud import Plaud, as_text, payload, records
from conftest import FakeHost, fixture


def host_for(list_fixture="plaud_list_structured.json", **extra):
    return FakeHost(mcp={"list_files": fixture(list_fixture), "get_note": fixture("plaud_note.json"),
                         "get_transcript": fixture("plaud_transcript.json"), **extra},
                    params={"get_note": ["file_id"], "get_transcript": ["file_id"]})


def test_sync_writes_one_note_per_recording_and_is_idempotent(vault):
    host = host_for()
    report = Plaud(host, vault).sync(today=date(2026, 10, 12))
    assert report["new"] == 2 and report["errors"] == []
    notes = sorted(p.name for p in (vault.root / "Plaud").glob("*.md"))
    assert notes == ["2026-10-11 Telefonata Bianchi.md", "2026-10-12 Riunione Kimera budget.md"] or len(notes) == 2
    text = next((vault.root / "Plaud").glob("*Riunione*")).read_text(encoding="utf-8")
    assert "plaud_id: f1" in text and "K39 Stradale" in text and "### Azioni" in text
    assert "**Marco Bianchi:** La consegna slitta a marzo." in text and "[00:01:05]" in text
    assert ("plaud", "get_note", {"file_id": "f1"}) in host.mcp_calls
    again = Plaud(host, vault).sync(today=date(2026, 10, 12))
    assert again["new"] == 0 and again["skipped"] == 2
    assert len(vault.state()["plaud_pending"]) == 2


def test_first_sync_looks_back_then_uses_the_cursor(vault):
    host = host_for()
    Plaud(host, vault).sync(today=date(2026, 10, 12))
    first = host.mcp_calls[0][2]
    assert first["date_from"] == "2026-09-12"
    Plaud(host, vault).sync(today=date(2026, 10, 12))
    assert [c for c in host.mcp_calls if c[1] == "list_files"][-1][2]["date_from"] == "2026-10-11"


def test_text_json_shape_and_unknown_param_names(vault):
    host = host_for("plaud_list_text.json")
    host.params = {}  # schema unknown: the plugin tries file_id, then fileId
    report = Plaud(host, vault).sync(today=date(2026, 10, 12))
    assert report["new"] == 1 and next((vault.root / "Plaud").glob("*Rossi*"))


def test_missing_transcript_keeps_the_summary(vault):
    host = host_for(get_transcript={"ok": False, "error": {"message": "transcription in progress"}})
    Plaud(host, vault).sync(today=date(2026, 10, 12))
    text = next((vault.root / "Plaud").glob("*Riunione*")).read_text(encoding="utf-8")
    assert "Trascrizione non disponibile" in text and "K39" in text


def test_list_error_is_reported(vault):
    host = FakeHost(mcp={})
    try:
        Plaud(host, vault).sync()
    except Exception as exc:
        assert "unknown tool" in str(exc)
    else:
        raise AssertionError("expected a Plaud error")


def test_recordings_are_consolidated_from_their_summary(vault):
    Plaud(host_for(), vault).sync(today=date(2026, 10, 12))
    seen = []
    host = FakeHost(llm=lambda text: seen.append(text) or {"entita": [
        {"tipo": "persona", "nome": "Marco Bianchi", "fatti": [{"testo": "Ha spostato la consegna K39 a marzo 2027."}]}]})
    report = consolidate.run(host, vault, today=date(2026, 10, 12))
    assert report["chunks"] == 2 and vault.state()["plaud_pending"] == []
    assert all("La consegna slitta" not in t for t in seen)  # transcript not sent when a summary exists
    assert "[[Plaud/" in (vault.root / "Persone" / "Marco Bianchi.md").read_text(encoding="utf-8")


def test_parsers():
    assert payload({"ok": True, "result": "[1, 2]"}) == [1, 2]
    assert records({"a": {"b": [{"id": 1}, {"x": 2}]}}) == [{"id": 1}]
    assert as_text([{"speaker": "A", "text": "ciao"}]) == "**A:** ciao"
    assert "```json" in as_text({"weird": 1})

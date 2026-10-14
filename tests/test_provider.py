import json
from datetime import date

from alfred_brain.provider import BrainProvider
from conftest import FakeHost, fixture


def provider(vault, **kw):
    p = BrainProvider(host=FakeHost(**kw), vault=vault)
    p.initialize("s1", platform="telegram", agent_context="primary")
    return p


def call(p, tool, **args):
    return json.loads(p.handle_tool_call(tool, args))


def test_turns_are_journaled_but_trivial_and_non_primary_are_not(vault):
    p = provider(vault)
    p.sync_turn("Novità da Kimera?", "Nessuna.", turn_author={"name": "Luca"})
    p.sync_turn("ok", "👍")
    cron = BrainProvider(host=FakeHost(), vault=vault)
    cron.initialize("s2", agent_context="cron")
    cron.sync_turn("Chiama brain_sync", "fatto")
    text = (vault.root / "Diario" / f"{date.today().isoformat()}.md").read_text(encoding="utf-8")
    assert "**Luca:** Novità da Kimera?" in text and "👍" not in text and "brain_sync" not in text


def test_prefetch_and_recall_status(vault):
    vault.add_facts("cliente", "Kimera Automobili", [("Contratto K39 firmato.", "2026-10-12", "")], alias=["Kimera"])
    p = provider(vault)
    block = p.prefetch("Come va con Kimera?")
    assert "Contratto K39 firmato." in block and p.recall_status().count == 1
    assert p.prefetch("ok") == "" and p.recall_status() is None


def test_tools(vault):
    p = provider(vault)
    saved = call(p, "brain_note", tipo="persona", nome="Luca Betti", fatto="Preferisce i report il lunedì.")
    assert saved == {"ok": True, "path": "Persone/Luca Betti.md", "added": True}
    found = call(p, "brain_search", query="Luca report")
    assert found["results"][0]["path"] == "Persone/Luca Betti.md"
    note = call(p, "brain_read", path="Persone/Luca Betti.md")
    assert "Preferisce i report" in note["text"]
    assert call(p, "brain_read", path="../secret.md")["ok"] is False
    assert call(p, "brain_note", tipo="azienda", nome="X", fatto="y")["ok"] is False
    assert call(p, "brain_nope")["ok"] is False


def test_sync_tool_imports_and_consolidates(vault):
    p = provider(vault, mcp={"list_files": fixture("plaud_list_structured.json"), "get_note": fixture("plaud_note.json"),
                             "get_transcript": fixture("plaud_transcript.json")}, params={"get_note": ["file_id"], "get_transcript": ["file_id"]})
    out = call(p, "brain_sync")
    assert out["ok"] and out["new"] == 2 and out["consolidation"]["chunks"] == 2


def test_plaud_errors_come_back_with_a_hint(vault):
    out = call(provider(vault, mcp={}), "brain_sync")
    assert out["ok"] is False and "mcp_allowlist" in out["hint"]


def test_session_end_consolidates_in_background(vault):
    p = provider(vault, llm={"entita": [{"tipo": "cliente", "nome": "Kimera", "fatti": [{"testo": "Ok sul budget."}]}]})
    p.sync_turn("Kimera ha approvato il budget?", "Sì.")
    p.on_session_end([])
    import threading
    for t in threading.enumerate():
        if t.name == "brain-consolidate":
            t.join(5)
    assert (vault.root / "Clienti" / "Kimera.md").exists()


def test_transcribe_tool_reports_and_journals_only_the_link(vault, tmp_path):
    from alfred_brain.vault import parse_note
    audio = tmp_path / "audio_1a2b.m4a"
    audio.write_bytes(b"x")
    info = {"titolo": "Idea pannelli", "categoria": "idea", "area": "progettazione", "riassunto": ["Pannelli più leggeri."],
            "azioni": []}
    p = provider(vault, llm=[info], transcripts={"audio_1a2b.m4a": "Proviamo pannelli più leggeri per la Z20."})
    assert "brain_transcribe" in {s["name"] for s in p.get_tool_schemas()} and "brain_transcribe" in p.system_prompt_block()
    p.on_turn_start(1, "nota vocale", author_name="Luca")
    out = call(p, "brain_transcribe", path=str(audio))
    assert out["ok"] and out["status"] == "done" and out["report"].startswith("Nota vocale «Idea pannelli» (idea · progettazione)")
    assert parse_note(vault.root / out["note"]).meta["autore"] == "Luca"
    p.sync_turn(f"[The user sent an audio file attachment: 'memo'. It is saved at: {audio}.]", out["report"],
                turn_author={"name": "Luca"})
    text = (vault.root / "Diario" / f"{date.today().isoformat()}.md").read_text(encoding="utf-8")
    assert f"**Alfred:** Nota vocale archiviata: [[{out['note'][:-3]}]]" in text and "Pannelli più leggeri." not in text
    p.on_turn_start(2, "altro")
    p.sync_turn("E il budget di Kimera?", "Approvato.")
    assert "**Alfred:** Approvato." in (vault.root / "Diario" / f"{date.today().isoformat()}.md").read_text(encoding="utf-8")
    assert call(p, "brain_transcribe", path=str(tmp_path / "sparito.m4a"))["ok"] is False
    assert "not an audio file" in call(p, "brain_transcribe", path=str(tmp_path / "foto.jpg"))["error"]


def test_oversized_audio_points_to_the_ceo_s_folder_only_when_there_is_one(vault, tmp_path, monkeypatch):
    p = provider(vault)
    assert "OneDrive" not in p.system_prompt_block()
    monkeypatch.setenv("ALFRED_BRAIN_VOICE", str(tmp_path))
    assert "troppo grande per la chat" in p.system_prompt_block() and "OneDrive" in p.system_prompt_block()


def test_unavailable_without_vault(tmp_path):
    from alfred_brain.vault import Vault
    p = BrainProvider(host=FakeHost(), vault=Vault(tmp_path / "none"))
    assert p.is_available() is False and "alfred brain init" in p.unavailable_reason()

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
    from alfred_brain import consolidate
    p = provider(vault, llm={"entita": [{"tipo": "cliente", "nome": "Kimera", "fatti": [{"testo": "Ok sul budget."}]}]},
                 background=True)
    p.sync_turn("Kimera ha approvato il budget?", "Sì.")
    p.on_session_end([])
    assert consolidate.wait_idle(5)
    assert (vault.root / "Clienti" / "Kimera.md").exists()


def test_tools_link_in_the_background_when_there_is_one(vault):
    from alfred_brain import consolidate
    p = provider(vault, mcp={"list_files": fixture("plaud_list_structured.json"), "get_note": fixture("plaud_note.json"),
                             "get_transcript": fixture("plaud_transcript.json")}, params={"get_note": ["file_id"], "get_transcript": ["file_id"]},
                 background=True)
    out = call(p, "brain_sync")
    assert out["new"] == 2 and out["consolidation"] == {"background": True}
    assert call(p, "brain_consolidate") == {"ok": True, "background": True}
    assert consolidate.wait_idle(5) and vault.state()["plaud_pending"] == []


def test_commitments_tool(vault):
    p = provider(vault, owner="Luca Betti")
    p.on_turn_start(1, "x", author_name="Luca")
    added = call(p, "brain_impegni", azione="aggiungi", chi="Marco", cosa="mandare il disegno del cofano", entro="2026-10-16")
    assert added["ok"] and added["impegno"]["id"] == 1 and added["testo"].startswith("#1 Marco — mandare il disegno del cofano")
    mine = call(p, "brain_impegni", azione="aggiungi", cosa="chiamare Beta")
    assert mine["impegno"]["chi"] == "Luca Betti" and mine["testo"] == "#2 Tu — chiamare Beta"
    listed = call(p, "brain_impegni", azione="elenca", chi="io")
    assert listed["count"] == 1 and listed["testo"] == "#2 Tu — chiamare Beta"
    fixed = call(p, "brain_impegni", azione="modifica", id=1, chi="Marco Bianchi", entro="2026-10-19")
    assert fixed["impegno"]["chi"] == "Marco Bianchi" and fixed["impegno"]["entro"] == "2026-10-19"
    done = call(p, "brain_impegni", azione="modifica", id=2, stato="fatto")
    assert done["impegno"]["stato"] == "fatto" and call(p, "brain_impegni", azione="elenca")["count"] == 1
    assert call(p, "brain_impegni", azione="modifica", stato="fatto")["ok"] is False
    assert "YYYY-MM-DD" in call(p, "brain_impegni", azione="aggiungi", cosa="x", entro="domani")["error"]
    assert call(p, "brain_impegni", azione="boh")["ok"] is False
    assert (vault.root / "Impegni.md").exists() and "brain_impegni" in p.system_prompt_block()


def test_prefetch_brings_the_open_commitments_of_who_is_named(vault):
    vault.add_facts("persona", "Marco Bianchi", [("Responsabile acquisti di Beta.", "2026-10-12", "")], alias=["Marco"])
    p = provider(vault)
    call(p, "brain_impegni", azione="aggiungi", chi="Marco", cosa="mandare il disegno del cofano")
    block = p.prefetch("Marco ha mandato qualcosa?")
    assert "Responsabile acquisti di Beta." in block and "#1 Marco Bianchi — mandare il disegno del cofano" in block
    assert p.recall_status().count == 2
    assert "Impegni aperti" not in p.prefetch("Che tempo fa a Modena?")


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


def test_commitment_changes_from_the_chat_are_in_the_vault_history(vault):
    import subprocess
    p = provider(vault)
    call(p, "brain_impegni", azione="aggiungi", chi="Marco", cosa="mandare il disegno")
    call(p, "brain_impegni", azione="modifica", id=1, stato="fatto")
    log = subprocess.run(["git", "log", "--format=%s"], cwd=vault.root, capture_output=True, text=True).stdout
    assert "Impegno #1: aggiunto" in log and "Impegno #1: fatto" in log

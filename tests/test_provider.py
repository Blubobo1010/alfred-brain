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


def test_unavailable_without_vault(tmp_path):
    from alfred_brain.vault import Vault
    p = BrainProvider(host=FakeHost(), vault=Vault(tmp_path / "none"))
    assert p.is_available() is False and "alfred brain init" in p.unavailable_reason()

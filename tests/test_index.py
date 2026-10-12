from datetime import datetime

from alfred_brain import journal
from alfred_brain.index import Index, recall_block


def seed(vault):
    vault.add_facts("cliente", "Kimera Automobili", [("Consegna K39 Stradale a marzo 2027.", "2026-10-12", "[[Diario/2026-10-12]]")], alias=["Kimera"])
    vault.add_facts("persona", "Marco Bianchi", [("Responsabile acquisti di Kimera.", "2026-10-12", "")])
    journal.append_turn(vault, "Parliamo del menu del pranzo", "Va bene", when=datetime(2026, 10, 12, 12, 0))


def test_names_and_aliases_rank_first(vault):
    seed(vault)
    hits = Index(vault).search("cosa sappiamo di Kimera?")
    assert hits[0][1].rel == "Clienti/Kimera Automobili.md"


def test_recall_block_is_capped_and_cites_paths(vault):
    seed(vault)
    block, count = recall_block(Index(vault), "Marco Bianchi di Kimera", max_chars=500)
    assert block.startswith("## Dal secondo cervello") and count >= 1 and len(block) < 700
    assert "**Persone/Marco Bianchi.md**" in block or "**Clienti/Kimera Automobili.md**" in block


def test_no_match_no_block(vault):
    seed(vault)
    assert recall_block(Index(vault), "quantistica")[0] == ""


def test_hand_edits_are_seen_on_next_search(vault):
    seed(vault)
    index = Index(vault)
    index.search("x")
    (vault.root / "Progetti" / "Fiera Ginevra.md").write_text("# Fiera Ginevra\n\nStand prenotato.\n", encoding="utf-8")
    assert index.search("Ginevra")[0][1].rel == "Progetti/Fiera Ginevra.md"

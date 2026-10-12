from datetime import datetime

from alfred_brain import journal


def test_append_and_pending_chunks_respect_offsets(vault):
    t = datetime(2026, 10, 12, 14, 32)
    journal.append_turn(vault, "Com'è andata con Kimera?", "Bene, contratto firmato.", author="Luca", platform="telegram", when=t)
    journal.append_turn(vault, "E Bianchi?", "Ha confermato marzo.", author="Luca", platform="telegram", when=t)
    text = (vault.root / "Diario" / "2026-10-12.md").read_text(encoding="utf-8")
    assert text.startswith("# Diario 2026-10-12") and "### 14:32 · telegram · Luca" in text and "**Alfred:** Bene" in text
    chunks = journal.pending_chunks(vault)
    assert len(chunks) == 1 and "Bianchi" in chunks[0][2]
    journal.mark_done(vault, chunks[0][0], chunks[0][1])
    assert journal.pending_chunks(vault) == []
    journal.append_turn(vault, "Nuova domanda su Rossi", "Risposta", when=t)
    pending = journal.pending_chunks(vault)
    assert len(pending) == 1 and "Rossi" in pending[0][2] and "Bianchi" not in pending[0][2]


def test_chunks_split_on_entry_boundaries(vault):
    t = datetime(2026, 10, 12, 9, 0)
    for i in range(6):
        journal.append_turn(vault, f"domanda {i} " + "x" * 300, "risposta", when=t)
    chunks = journal.pending_chunks(vault, max_chars=800)
    assert len(chunks) > 1 and all(c[2].lstrip().startswith(("# Diario", "### ")) for c in chunks)
    assert chunks[-1][1] == (vault.root / "Diario" / "2026-10-12.md").stat().st_size


def test_long_turns_are_clipped(vault):
    path = journal.append_turn(vault, "a" * 10000, "b", when=datetime(2026, 1, 1))
    assert "[…]" in path.read_text(encoding="utf-8")

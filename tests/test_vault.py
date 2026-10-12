from datetime import date

import pytest
from alfred_brain.vault import MANAGED_END, MANAGED_START, Vault, normalize, parse_note


def test_init_is_idempotent_and_versioned(tmp_path):
    v = Vault(tmp_path / "v")
    assert v.init() is True and v.init() is False
    for d in ("Diario", "Plaud", "Clienti", "Persone", "Progetti", ".alfred"):
        assert (v.root / d).is_dir()
    assert (v.root / "Indice.md").exists() and (v.root / "LEGGIMI.md").exists()


def test_add_facts_creates_note_with_source_and_dedups(vault):
    path, n = vault.add_facts("cliente", "Kimera Automobili", [("Ha 396 opportunità nel CRM.", "2026-10-12", "[[Diario/2026-10-12]]")],
                              alias=["Kimera"], today=date(2026, 10, 12))
    assert n == 1 and path.name == "Kimera Automobili.md"
    _, again = vault.add_facts("cliente", "kimera", [("ha 396 opportunita nel crm", "", "")], today=date(2026, 10, 13))
    assert again == 0  # alias match + normalised duplicate
    note = parse_note(path)
    assert note.meta["tipo"] == "cliente" and note.meta["alias"] == ["Kimera"]
    assert "- 2026-10-12 — Ha 396 opportunità nel CRM. ([[Diario/2026-10-12]])" in note.body


def test_human_text_outside_the_managed_block_is_preserved(vault):
    path, _ = vault.add_facts("persona", "Luca Betti", [("È il CEO di Kimera.", "2026-10-12", "")])
    text = path.read_text(encoding="utf-8")
    human = "Appunti miei: preferisce le call al mattino.\n"
    text = text.replace(MANAGED_START, human + "\n" + MANAGED_START) + "\n## Note a mano\nNon toccare.\n"
    path.write_text(text, encoding="utf-8")
    vault.add_facts("persona", "Luca Betti", [("Vuole i report il lunedì.", "2026-10-13", "")])
    after = path.read_text(encoding="utf-8")
    assert human in after and after.endswith("## Note a mano\nNon toccare.\n")
    block = after.split(MANAGED_START)[1].split(MANAGED_END)[0]
    assert "È il CEO" in block and "report il lunedì" in block


def test_note_without_block_gets_one_appended(vault):
    path = vault.root / "Clienti" / "Studio Rossi.md"
    path.write_text("---\ntipo: cliente\n---\n# Studio Rossi\n\nScritta a mano.\n", encoding="utf-8")
    vault.add_facts("cliente", "Studio Rossi", [("Pratica aperta a ottobre.", "", "")])
    text = path.read_text(encoding="utf-8")
    assert "Scritta a mano." in text and MANAGED_START in text


def test_resolve_refuses_escapes_and_internals(vault):
    with pytest.raises(ValueError):
        vault.resolve("../etc/passwd")
    with pytest.raises(ValueError):
        vault.resolve(".alfred/state.json")
    assert vault.resolve("Clienti/x.md").name == "x.md"


def test_bad_tipo_refused(vault):
    with pytest.raises(ValueError):
        vault.add_facts("azienda", "X", [("y", "", "")])


def test_normalize():
    assert normalize("  Società  ÀBC-srl ") == "societa abc srl"

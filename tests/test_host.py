from alfred_brain.host import json_object


def test_json_object_finds_the_answer_in_model_noise():
    answer = {"titolo": "Prova", "categoria": "promemoria"}
    bare = '{"titolo": "Prova", "categoria": "promemoria"}'
    assert json_object(bare) == answer
    assert json_object(f"```json\n{bare}\n```") == answer
    assert json_object(f"<think>\nServe un oggetto {{titolo, categoria}}.\n</think>\n\n{bare}") == answer
    assert json_object(f"Ecco il JSON richiesto: {bare} Spero vada bene.") == answer
    assert json_object("nessun JSON qui") is None and json_object("") is None and json_object(None) is None
    assert json_object("[1, 2]") is None

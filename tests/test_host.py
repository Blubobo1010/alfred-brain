import sys
import types
from dataclasses import dataclass

import pytest
from alfred_brain.host import AUX_TASK, RELAXED_SCHEMA, HermesHost, json_object


def test_json_object_finds_the_answer_in_model_noise():
    answer = {"titolo": "Prova", "categoria": "promemoria"}
    bare = '{"titolo": "Prova", "categoria": "promemoria"}'
    assert json_object(bare) == answer
    assert json_object(f"```json\n{bare}\n```") == answer
    assert json_object(f"<think>\nServe un oggetto {{titolo, categoria}}.\n</think>\n\n{bare}") == answer
    assert json_object(f"Ecco il JSON richiesto: {bare} Spero vada bene.") == answer
    assert json_object("nessun JSON qui") is None and json_object("") is None and json_object(None) is None
    assert json_object("[1, 2]") is None


@dataclass
class Result:
    parsed: object = None
    text: str = ""


class FakeLlm:
    """``complete_structured`` answers from a queue of results or exceptions and records (task, schema)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def complete_structured(self, *, task=None, json_schema=None, **kwargs):
        self.calls.append((task, json_schema))
        self.instructions = kwargs.get("instructions")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    plugin_llm = types.ModuleType("agent.plugin_llm")
    plugin_llm.PluginLlmTextInput = lambda text: text
    monkeypatch.setitem(sys.modules, "agent.plugin_llm", plugin_llm)
    h = HermesHost()
    h._ctx = types.SimpleNamespace(llm=None, get_config=lambda key, default=None: {"owner_name": "  Luca   Betti "}.get(key, default))
    return h


STRICT = {"type": "object", "required": ["x"]}


def test_an_answer_outside_the_schema_is_asked_again_without_constraints(host):
    host._ctx.llm = FakeLlm(ValueError("Plugin LLM structured output did not match schema: 'urgente' is not one of [...]"),
                            Result(parsed={"categoria": "urgente"}))
    assert host.llm_json("i", "t", STRICT) == {"categoria": "urgente"}
    assert host._ctx.llm.calls == [(AUX_TASK, STRICT), (AUX_TASK, RELAXED_SCHEMA)]
    assert host._ctx.llm.instructions.startswith("i\n\n") and '"required": ["x"]' in host._ctx.llm.instructions


def test_who_answered_is_recorded(host):
    host._ctx.llm = FakeLlm(types.SimpleNamespace(parsed={"a": 1}, text="", provider="custom", model="qwen3.6:35b"))
    host.llm_json("i", "t", STRICT)
    assert host.answered_by == "custom/qwen3.6:35b"


def test_lone_strings_and_objects_become_one_item_lists():
    from alfred_brain.host import as_list
    assert as_list("Ritardo V03") == ["Ritardo V03"] and as_list({"a": 1}) == [{"a": 1}]
    assert as_list(None) == [] and as_list("") == [] and as_list(3) == [] and as_list(("x",)) == ["x"]


def test_the_main_model_only_when_the_auxiliary_slot_is_not_registered(host):
    class PluginLlmTrustError(PermissionError):
        pass

    host._ctx.llm = FakeLlm(PluginLlmTrustError("cannot route through auxiliary task"), Result(text='{"a": 1}'))
    assert host.llm_json("i", "t", STRICT) == {"a": 1}
    assert host._ctx.llm.calls == [(AUX_TASK, STRICT), (None, STRICT)]
    host._ctx.llm = FakeLlm(TimeoutError("model timed out"))
    with pytest.raises(TimeoutError):  # one call, not two: a timeout is not a missing slot
        host.llm_json("i", "t", STRICT)
    assert len(host._ctx.llm.calls) == 1
    host._ctx.llm = FakeLlm(ValueError("Unknown input block shape"))
    with pytest.raises(ValueError, match="input block"):
        host.llm_json("i", "t", STRICT)


def test_owner_name_from_the_plugin_settings(host):
    assert host.owner() == "Luca Betti"
    host._ctx = types.SimpleNamespace(get_config=lambda key, default=None: "")
    assert host.owner() == "il CEO"


def test_model_busy_is_unknown_outside_hermes():
    assert HermesHost().model_busy() is None

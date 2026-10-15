"""Loads the plugin (repo root) as the package ``alfred_brain`` and provides fakes for the model and MCP."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def _load_package() -> Any:
    if "alfred_brain" in sys.modules:
        return sys.modules["alfred_brain"]
    spec = importlib.util.spec_from_file_location("alfred_brain", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["alfred_brain"] = module
    spec.loader.exec_module(module)
    return module


_load_package()

from alfred_brain.host import Host  # noqa: E402
from alfred_brain.vault import Vault  # noqa: E402


class FakeHost(Host):
    """``llm_json`` answers from a queue (or a callable); ``mcp`` answers from a {tool: response} table;
    ``transcribe`` from a {file name: text or exception} table (or a callable), held by ``gate`` while it is closed;
    ``wake_inbox`` counts in ``wakes``; ``owner`` is the CEO's configured name; ``busy`` answers ``model_busy`` (a
    callable, a list consumed one per check, or a fixed value); ``tools`` answers other plugins' tools by name (a dict
    or a callable of the arguments; unknown names get Hermes' "Unknown tool" error) and ``settings`` the plugin's
    settings. No background passes unless ``background=True``: a test reads the model's answers in order."""

    def __init__(self, llm: Any = None, mcp: dict[str, Any] | None = None, params: dict[str, list[str]] | None = None,
                 transcripts: Any = None, gate: threading.Event | None = None, owner: str = "il CEO",
                 busy: Any = None, background: bool = False, tools: dict[str, Any] | None = None,
                 settings: dict[str, Any] | None = None):
        self.llm = llm
        self.mcp_table = mcp or {}
        self.params = params or {}
        self.transcripts = transcripts
        self.gate = gate
        self.owner_name = owner
        self.busy = busy
        self.background = background
        self.llm_calls: list[str] = []
        self.instructions: list[str] = []
        self.mcp_calls: list[tuple[str, str, dict]] = []
        self.transcribe_calls: list[str] = []
        self.busy_checks = 0
        self.wakes = 0
        self.tools = tools or {}
        self.settings = settings or {}
        self.tool_calls: list[tuple[str, dict]] = []

    def llm_json(self, instructions, text, schema):
        self.llm_calls.append(text)
        self.instructions.append(instructions)
        self.answered_by = "fake/modello"
        if callable(self.llm):
            answer = self.llm(text)
        elif isinstance(self.llm, list):
            answer = self.llm.pop(0) if self.llm else {"entita": []}
        else:
            answer = self.llm if self.llm is not None else {"entita": []}
        if isinstance(answer, Exception):  # a queued failure: timeouts, schema errors
            raise answer
        return answer

    def owner(self):
        return self.owner_name

    def model_busy(self):
        self.busy_checks += 1
        if callable(self.busy):
            return self.busy()
        if isinstance(self.busy, list):
            return self.busy.pop(0) if self.busy else False
        return self.busy

    def setting(self, key, default=None):
        return self.settings.get(key, default)

    def tool(self, name, args):
        self.tool_calls.append((name, dict(args)))
        if name not in self.tools:
            return {"error": f"Unknown tool: {name}"}
        answer = self.tools[name]
        return answer(args) if callable(answer) else answer

    def mcp(self, server, tool, args, timeout=60):
        self.mcp_calls.append((server, tool, dict(args)))
        answer = self.mcp_table.get(tool)
        if callable(answer):
            answer = answer(args)
        if answer is None:
            return {"ok": False, "error": {"message": f"unknown tool {tool}"}}
        return answer

    def mcp_params(self, server, tool):
        return self.params.get(tool, [])

    def transcribe(self, path):
        self.transcribe_calls.append(Path(path).name)
        if self.gate is not None:
            self.gate.wait(5)
        answer = self.transcripts(path) if callable(self.transcripts) else (self.transcripts or {}).get(Path(path).name, "")
        if isinstance(answer, Exception):
            raise answer
        return answer

    def wake_inbox(self):
        self.wakes += 1


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_folders_from_the_environment(monkeypatch):
    """Tests pass their folders explicitly: a real ALFRED_BRAIN_* on the developer's machine must not leak in."""
    for name in ("ALFRED_BRAIN_INBOX", "ALFRED_BRAIN_VOICE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_background_left():
    """A background pass started by a test ends with it, and never waits for real on a local model."""
    from alfred_brain import consolidate
    yield
    assert consolidate.wait_idle(10), "a background linking pass is still running"


@pytest.fixture
def vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "vault")
    v.init()
    return v

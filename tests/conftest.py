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
    ``wake_inbox`` counts in ``wakes``."""

    def __init__(self, llm: Any = None, mcp: dict[str, Any] | None = None, params: dict[str, list[str]] | None = None,
                 transcripts: Any = None, gate: threading.Event | None = None):
        self.llm = llm
        self.mcp_table = mcp or {}
        self.params = params or {}
        self.transcripts = transcripts
        self.gate = gate
        self.llm_calls: list[str] = []
        self.mcp_calls: list[tuple[str, str, dict]] = []
        self.transcribe_calls: list[str] = []
        self.wakes = 0

    def llm_json(self, instructions, text, schema):
        self.llm_calls.append(text)
        if callable(self.llm):
            return self.llm(text)
        if isinstance(self.llm, list):
            return self.llm.pop(0) if self.llm else {"entita": []}
        return self.llm if self.llm is not None else {"entita": []}

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


@pytest.fixture
def vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "vault")
    v.init()
    return v

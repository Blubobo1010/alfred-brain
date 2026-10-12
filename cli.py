"""``alfred brain …``: init, setup, status, sync, consolidate, reindex.

Hermes only wires this file for the ACTIVE memory provider, so the order on a new machine is:
``alfred plugins install Blubobo1010/alfred-brain --enable`` (sets ``memory.provider: brain``),
then ``alfred brain init`` and ``alfred brain setup``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .vault import Vault, vault_path

SYNC_JOB = "brain: Plaud ogni 30 minuti"
NIGHT_JOB = "brain: consolidamento notturno"
SYNC_PROMPT = "Chiama il tool brain_sync una volta e rispondi solo con il suo risultato in una riga."
NIGHT_PROMPT = "Chiama il tool brain_consolidate una volta e rispondi solo con il suo risultato in una riga."


def register_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="brain_command")
    init = sub.add_parser("init", help="Create the Obsidian vault and point Alfred at it")
    init.add_argument("--path", default="", help=f"Vault folder (default {vault_path()})")
    sub.add_parser("setup", help="Schedule Plaud import (every 30 min) and nightly consolidation; check Plaud access")
    sub.add_parser("status", help="Vault counts, last Plaud sync and consolidation")
    sub.add_parser("sync", help="Import new Plaud recordings now")
    sub.add_parser("consolidate", help="File pending journal and recordings into entity notes now")
    sub.add_parser("reindex", help="Rebuild the search index (normally automatic)")


def brain_command(args: argparse.Namespace) -> int:
    command = getattr(args, "brain_command", None) or "status"
    try:
        return {"init": _init, "setup": _setup, "status": _status, "sync": _sync,
                "consolidate": _consolidate, "reindex": _reindex}[command](args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - a CLI error is a message, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _init(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser() if getattr(args, "path", "") else vault_path()
    vault = Vault(root)
    new = vault.init()
    _save_env("OBSIDIAN_VAULT_PATH", str(vault.root))
    _save_config({"memory": {"provider": "brain"}})
    print(f"{'Created' if new else 'Already set up'}: {vault.root}")
    print("OBSIDIAN_VAULT_PATH saved; memory.provider = brain. Next: `alfred brain setup`, then restart the gateway.")
    return 0


def _setup(args: argparse.Namespace) -> int:
    from cron.jobs import create_job, list_jobs
    names = {j.get("name") for j in list_jobs(include_disabled=True)}
    for name, schedule, prompt in ((SYNC_JOB, "every 30m", SYNC_PROMPT), (NIGHT_JOB, "0 3 * * *", NIGHT_PROMPT)):
        if name in names:
            print(f"cron job already present: {name}")
            continue
        create_job(prompt, schedule, name=name, deliver="local", enabled_toolsets=["memory"])
        print(f"cron job created: {name} ({schedule})")
    allow = _plugin_entry().get("mcp_allowlist") or []
    if "plaud" not in allow:
        _save_config({"plugins": {"entries": {"brain": {"mcp_allowlist": [*allow, "plaud"]}}}})
        print("plugins.entries.brain.mcp_allowlist now includes plaud")
    if "plaud" not in _mcp_servers():
        print("Plaud is not configured yet: `alfred mcp add plaud --url https://mcp.plaud.ai/mcp --auth oauth` "
              "and log in with the CEO's Plaud account.")
    print("Restart the gateway so the schedule runs: `alfred gateway restart`.")
    return 0


def _status(args: argparse.Namespace) -> int:
    vault = Vault()
    if not vault.exists():
        print(f"No vault at {vault.root}: run `alfred brain init`.")
        return 1
    state = vault.state()
    counts = {folder: len(list((vault.root / folder).glob("*.md")))
              for folder in ("Diario", "Plaud", "Clienti", "Persone", "Progetti")}
    from . import journal
    print(json.dumps({
        "vault": str(vault.root), "notes": counts,
        "plaud_configured": "plaud" in _mcp_servers(),
        "plaud_allowed": "plaud" in (_plugin_entry().get("mcp_allowlist") or []),
        "plaud_last_sync": state.get("plaud_last_sync"), "plaud_pending": len(state.get("plaud_pending", [])),
        "journal_pending_chunks": len(journal.pending_chunks(vault)),
        "last_consolidation": state.get("last_consolidation"),
    }, ensure_ascii=False, indent=2))
    return 0


def _sync(args: argparse.Namespace) -> int:
    from .provider import BrainProvider
    print(BrainProvider().handle_tool_call("brain_sync", {}))
    return 0


def _consolidate(args: argparse.Namespace) -> int:
    from .provider import BrainProvider
    print(BrainProvider().handle_tool_call("brain_consolidate", {}))
    return 0


def _reindex(args: argparse.Namespace) -> int:
    from .index import Index
    index = Index(Vault())
    index.refresh()
    print(f"{len(index.docs)} notes indexed")
    return 0


def _save_env(name: str, value: str) -> None:
    from hermes_cli.config import save_env_value
    save_env_value(name, value)


def _save_config(values: dict) -> None:
    from hermes_cli.config import save_config
    save_config(values, merge_existing=True)


def _plugin_entry() -> dict:
    from hermes_cli.config import load_config
    entry = (((load_config() or {}).get("plugins") or {}).get("entries") or {}).get("brain") or {}
    return entry if isinstance(entry, dict) else {}


def _mcp_servers() -> dict:
    from hermes_cli.config import load_config
    servers = (load_config() or {}).get("mcp_servers") or {}
    return servers if isinstance(servers, dict) else {}

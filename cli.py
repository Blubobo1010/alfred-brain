"""``alfred brain …``: init, setup, inbox, transcribe, brief, status, sync, consolidate, reindex, eval.

Hermes only wires this file for the ACTIVE memory provider, so the order on a new machine is:
``alfred plugins install Blubobo1010/alfred-brain --enable`` (sets ``memory.provider: brain``),
then ``alfred brain init`` and ``alfred brain setup``.

A CLI command is its own short-lived process: it never leaves linking to a background thread (``host.background``
off) and runs the pass itself before exiting.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .host import INBOX_JOB
from .vault import Vault, vault_path

SYNC_JOB = "brain: Plaud ogni 30 minuti"
NIGHT_JOB = "brain: consolidamento notturno"
SYNC_PROMPT = "Chiama il tool brain_sync una volta e rispondi solo con il suo risultato in una riga."
INBOX_PROMPT = ("Chiama il tool brain_inbox una volta. Se il campo report è vuoto o c'è un errore rispondi solo [SILENT]. "
                "Altrimenti rispondi con il testo del campo report così com'è, senza aggiungere altro.")
NIGHT_PROMPT = "Chiama il tool brain_consolidate una volta e rispondi solo con il suo risultato in una riga."


def register_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="brain_command")
    init = sub.add_parser("init", help="Create the Obsidian vault and point Alfred at it")
    init.add_argument("--path", default="", help=f"Vault folder (default {vault_path()})")
    sub.add_parser("setup", help="Schedule Plaud import (every 30 min), nightly consolidation and the meeting brief "
                   "(paused until resumed); check Plaud access")
    inbox = sub.add_parser("inbox", help="Import colleagues' updates and the CEO's voice memos from their folders now; "
                           "with --path/--voice-path, configure the folders and schedule them every 30 min with a "
                           "report to the CEO")
    inbox.add_argument("--path", default="", help="Synced folder (e.g. a SharePoint folder in ~/Library/CloudStorage)")
    inbox.add_argument("--voice-path", default="", help="The CEO's own synced folder for voice memos too big for the "
                       "chat (e.g. a OneDrive folder only he can see): audio only, filed as his memos")
    inbox.add_argument("--deliver", default="telegram", help="Where the report goes (cron deliver target, default telegram)")
    transcribe = sub.add_parser("transcribe", help="Transcribe audio files (e.g. iPhone voice memos) into Note vocali/ now")
    transcribe.add_argument("files", nargs="+", help="Audio files (.m4a, .mp3, .wav, …)")
    brief = sub.add_parser("brief", help="Show the brief of the next meeting worth one (nothing sent or saved); "
                           "--due is what the cron runs every 5 minutes")
    brief.add_argument("--at", default="", help='Pretend it is this local time, e.g. "2026-10-16 14:30"')
    brief.add_argument("--due", action="store_true", help="Brief the meeting starting in about half an hour: print the "
                       "brief for the cron to deliver, save its note in Riunioni/")
    sub.add_parser("status", help="Vault counts, commitments and their correction rate, meeting briefs, last Plaud sync "
                   "and consolidation, speech-to-text settings")
    sub.add_parser("sync", help="Import new Plaud recordings now")
    sub.add_parser("consolidate", help="File pending journal and recordings into entity notes now")
    sub.add_parser("reindex", help="Rebuild the search index (normally automatic)")
    evaluate = sub.add_parser("eval", help="Measure how well the configured model finds commitments in labelled voice "
                              "memos (JSONL: trascrizione, data, autore, impegni) — rerun it after every model change")
    evaluate.add_argument("file", help="JSONL file, one labelled memo per line")
    evaluate.add_argument("--dettagli", action="store_true", help="Also list the missed and the extra commitments")


def brain_command(args: argparse.Namespace) -> int:
    command = getattr(args, "brain_command", None) or "status"
    try:
        return {"init": _init, "setup": _setup, "inbox": _inbox, "transcribe": _transcribe, "brief": _brief,
                "status": _status, "sync": _sync, "consolidate": _consolidate, "reindex": _reindex,
                "eval": _eval}[command](args)
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
    _setup_brief(names)
    allow = _plugin_entry().get("mcp_allowlist") or []
    if "plaud" not in allow:
        _save_config({"plugins": {"entries": {"brain": {"mcp_allowlist": [*allow, "plaud"]}}}})
        print("plugins.entries.brain.mcp_allowlist now includes plaud")
    if "plaud" not in _mcp_servers():
        print("Plaud is not configured yet: `alfred mcp add plaud --url https://mcp.plaud.ai/mcp --auth oauth` "
              "and log in with the CEO's Plaud account.")
    print("Restart the gateway so the schedule runs: `alfred gateway restart`.")
    return 0


def _setup_brief(names: set) -> None:
    """The meeting brief: a script in Alfred's scripts folder (written first: the cron reads it at every run), a job
    every 5 minutes that runs it without the model and delivers what it prints to the CEO on Telegram, mirrored into
    his chat. Created paused: it is resumed once Outlook is connected and ``alfred brain brief`` looks right."""
    from cron.jobs import create_job
    from hermes_constants import get_hermes_home

    from . import brief
    script = Path(get_hermes_home()) / "scripts" / brief.SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(brief.SCRIPT_TEXT, encoding="utf-8")
    if brief.JOB in names:
        print(f"cron job already present: {brief.JOB}")
    else:
        create_job("", "every 5m", name=brief.JOB, deliver="telegram", no_agent=True, script=brief.SCRIPT,
                   attach_to_session=True, failure_deliver="local", paused=True)
        print(f"cron job created, paused: {brief.JOB} (every 5 min, no model). Once Outlook is connected and "
              f"`alfred brain brief` looks right: alfred cron resume \"{brief.JOB}\"")
    _save_config({"cron": {"wrap_response": False}})
    print("cron.wrap_response = false: cron messages reach the CEO without the job's English header")


def _brief(args: argparse.Namespace) -> int:
    """``--due`` writes the brief on the real stdout (the cron's script sends everything else to stderr) and nothing
    when there is nothing to send; without it, a dry run that explains itself."""
    from . import brief
    from .host import HermesHost
    vault = Vault()
    if not vault.exists():
        print(f"No vault at {vault.root}: run `alfred brain init`.", file=sys.stderr)
        return 1
    now = brief.parse_now(args.at) if getattr(args, "at", "") else None
    result = brief.run(HermesHost(), vault, now=now, send=bool(getattr(args, "due", False)))
    if getattr(args, "due", False):
        if result.get("testo"):
            sys.__stdout__.write(result["testo"] + "\n")
            sys.__stdout__.flush()
        print(f"brief: {json.dumps({k: v for k, v in result.items() if k != 'testo'}, ensure_ascii=False)}", file=sys.stderr)
        return 0
    if result.get("testo"):
        print(result["testo"])
        print(f"\n(prova: niente inviato né salvato; inizio {result['inizio']}"
              + (f"; senza {', '.join(result['mancano'])}" if result.get("mancano") else "") + ")")
    else:
        print(f"Nessun brief: {result.get('esito')}" + (f" ({', '.join(result['saltate'])})" if result.get("saltate") else ""))
    return 0


def _inbox(args: argparse.Namespace) -> int:
    from .inbox import ENV, VOICE_ENV
    given = [(name, Path(value).expanduser().resolve()) for name, value in
             ((ENV, getattr(args, "path", "")), (VOICE_ENV, getattr(args, "voice_path", ""))) if value]
    if not given:
        from . import voice
        provider = _provider()
        print(provider.handle_tool_call("brain_inbox", {}))
        if voice.busy():  # audio from the folders: this process would exit and drop the transcription
            print("Transcribing audio from the folders…", file=sys.stderr)
            voice.wait_idle()
            for text in voice.VoiceMemos(provider.host, provider.vault).pop_reports():
                print(f"\n{text}")
            _link(provider)
        return 0
    for _name, folder in given:
        if not folder.is_dir():
            print(f"error: {folder} is not a folder (is it synced on this Mac?)", file=sys.stderr)
            return 1
    from cron.jobs import create_job, list_jobs
    for name, folder in given:
        _save_env(name, str(folder))
        print(f"{name} = {folder}")
    if INBOX_JOB in {j.get("name") for j in list_jobs(include_disabled=True)}:
        print(f"cron job already present: {INBOX_JOB}")
    else:
        create_job(INBOX_PROMPT, "every 30m", name=INBOX_JOB, deliver=args.deliver, enabled_toolsets=["memory"])
        print(f"cron job created: {INBOX_JOB} (every 30m, report to {args.deliver})")
    if args.deliver == "telegram":
        print("The report goes to the Telegram home channel: the CEO sends /set-home to the bot once.")
    print("Restart the gateway so the schedule runs: `alfred gateway restart`.")
    return 0


def _transcribe(args: argparse.Namespace) -> int:
    from .voice import VoiceMemos
    provider = _provider()
    memos = VoiceMemos(provider.host, provider.vault)
    failed = 0
    for name in args.files:
        try:
            result = memos.run(Path(name).expanduser(), fonte="cli", wait=None)
        except ValueError as exc:
            result = {"ok": False, "error": str(exc)}
        failed += not result.get("ok")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    _link(provider)
    return 1 if failed else 0


def _provider():
    """A provider for a short-lived command: it prints the reports itself, links before exiting and uses the model
    the gateway uses (the ``alfred_brain`` slot)."""
    from .provider import BrainProvider
    provider = BrainProvider()
    provider.host.wake = False
    provider.host.background = False
    provider.host.register_aux_task()
    return provider


def _link(provider) -> None:
    from . import consolidate
    report = consolidate.run(provider.host, provider.vault, wait=600)
    print(f"collegamento: {json.dumps(report, ensure_ascii=False)}", file=sys.stderr)


def _status(args: argparse.Namespace) -> int:
    vault = Vault()
    if not vault.exists():
        print(f"No vault at {vault.root}: run `alfred brain init`.")
        return 1
    state = vault.state()
    counts = {folder: len(list((vault.root / folder).glob("*.md")))
              for folder in ("Diario", "Plaud", "Aggiornamenti", "Note vocali", "Riunioni", "Clienti", "Persone",
                             "Progetti")}
    from . import brief, journal
    from .host import HermesHost
    from .impegni import CommitmentError, Impegni
    from .inbox import inbox_path, voice_path

    def folder(path: Path | None) -> str:
        return "" if path is None else f"{path}{'' if path.is_dir() else ' (NOT FOUND)'}"

    try:
        commitments = Impegni(vault).stats()
    except CommitmentError as exc:
        commitments = {"errore": str(exc)}
    print(json.dumps({
        "vault": str(vault.root), "notes": counts, "owner_name": HermesHost().owner(), "impegni": commitments,
        "plaud_configured": "plaud" in _mcp_servers(),
        "plaud_allowed": "plaud" in (_plugin_entry().get("mcp_allowlist") or []),
        "plaud_last_sync": state.get("plaud_last_sync"), "plaud_pending": len(state.get("plaud_pending", [])),
        "inbox_folder": folder(inbox_path()), "voice_folder": folder(voice_path()),
        "inbox_last_sync": state.get("inbox_last_sync"), "inbox_pending": len(state.get("inbox_pending", [])),
        "voice_pending": len(state.get("voice_pending", [])),
        "voice_unreported": len(state.get("voice_report", [])),
        "voice_failed": [f"{v.get('file')}: {v.get('errore')} ({v.get('tentativi')}x)"
                         for v in state.get("voice_failed", {}).values()],
        "journal_pending_chunks": len(journal.pending_chunks(vault)),
        "last_consolidation": state.get("last_consolidation"),
        "consolidation_retrying": len(state.get("consolidation_attempts", {})),
        "consolidation_skipped": state.get("consolidation_skipped", [])[-5:],
        "brief": brief.status(vault),
        "stt": _stt_status(),
    }, ensure_ascii=False, indent=2))
    return 0


def _stt_status() -> dict:
    """The speech-to-text settings voice memos depend on (Hermes' ``stt`` section; Alfred defaults to English)."""
    import importlib.util

    from hermes_cli.config import load_config
    stt = (load_config() or {}).get("stt") or {}
    provider = str(stt.get("provider") or "auto")
    section = stt.get(provider) if isinstance(stt.get(provider), dict) else {}
    local = stt.get("local") if isinstance(stt.get("local"), dict) else {}
    return {"enabled": stt.get("enabled", True), "provider": provider, "local_model": local.get("model") or "base",
            "language": section.get("language") or stt.get("language") or "auto",
            "faster_whisper": importlib.util.find_spec("faster_whisper") is not None}


def _sync(args: argparse.Namespace) -> int:
    print(_provider().handle_tool_call("brain_sync", {}))
    return 0


def _consolidate(args: argparse.Namespace) -> int:
    print(_provider().handle_tool_call("brain_consolidate", {}))
    return 0


def _eval(args: argparse.Namespace) -> int:
    """Run the voice-memo classification of the configured model on labelled memos and score its commitments. The
    labelled file never goes in a repository: real memos are client data."""
    import tempfile

    from .host import HermesHost
    host = HermesHost()
    host.register_aux_task()  # measure the model the gateway uses, not the main one by accident
    vault = Vault() if Vault().exists() else Vault(Path(tempfile.mkdtemp(prefix="brain-eval-")))
    lines = Path(args.file).expanduser().read_text(encoding="utf-8").splitlines()
    result = evaluate(host, vault, lines, progress=lambda message: print(message, file=sys.stderr))
    if not args.dettagli:
        result.pop("dettagli")
    print(json.dumps({"owner_name": host.owner(), **result}, ensure_ascii=False, indent=2))
    return 0


def evaluate(host, vault: Vault, lines: list[str], progress=None) -> dict:
    """Score the commitments ``voice.classify`` finds in labelled memos, one JSON object per line: ``trascrizione``,
    ``data`` (the recording day), ``autore`` (omitted or "ceo" for the CEO, else the colleague's name) and ``impegni``
    (``chi`` — "io" for the author —, ``cosa``, ``entro``). Names are compared after the same canonicalisation the
    store applies."""
    from . import impegni
    from .voice import classify
    owner = host.owner()
    cases, models = [], set()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        record = json.loads(line)
        transcript = str(record["trascrizione"])
        when = date.fromisoformat(str(record.get("data") or date.today().isoformat())[:10])
        writer = str(record.get("autore") or "")
        author = owner if not writer or writer.lower() == "ceo" or impegni.same_person(writer, owner) else writer
        info = classify(host, vault, transcript, when=when, author=author, owner=owner)
        models.add(getattr(host, "answered_by", ""))

        def canonical(items: list, author: str = author) -> list[dict]:
            out = []
            for item in impegni.actions(items, limit=50):
                who = impegni.person(vault, item.get("chi"), owner=owner, author=author)
                quote = impegni.clean(item.get("evidenza"), 300)
                if who:  # the same repairs the store applies, so the score is about what would be filed
                    out.append({"chi": who, "cosa": impegni.action_text(impegni.clean(item.get("cosa")), quote),
                                "entro": impegni.parse_due(item.get("entro")), "evidenza": quote})
            return out

        cases.append({"trascrizione": transcript, "attesi": canonical(record.get("impegni") or []),
                      "previsti": canonical(info["azioni"])})
        if progress:
            progress(f"memo {number}: {len(cases[-1]['previsti'])} trovati, {len(cases[-1]['attesi'])} attesi")
    return {"risposto_da": sorted(models - {""}), **impegni.score(cases)}


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

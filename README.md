# alfred-brain

The second brain of [Alfred](https://github.com/Blubobo1010/Alfred-Ai-Audit) / Hermes Agent: an
**Obsidian vault** that Alfred keeps up to date by itself. It is a Hermes **memory provider**
(`memory.provider: brain`).

- **Diario/** — every conversation turn, appended verbatim to one note per day (no model involved).
- **Clienti/, Persone/, Progetti/** — what Alfred learned, one fact per line with its source link
  (`[[Diario/2026-10-12]]`, `[[Plaud/…]]`). Alfred writes only inside the
  `<!-- alfred:inizio -->` … `<!-- alfred:fine -->` block: everything else in a note is yours.
- **Plaud/** — one note per Plaud recording (Plaud's summary, action items, full transcript),
  imported every 30 minutes through Plaud's MCP server.
- **Aggiornamenti/** — written updates colleagues drop in a shared folder (SharePoint/OneDrive synced
  on the Mac, `.txt`, `.md` or `.docx`), kept verbatim. Every 30 minutes new files are imported, filed
  into the project notes and summarised for the CEO on Telegram: lines marked "da vedere con …" are
  listed as they are (by keyword, never dropped by the model), then risks and news. Audio files in the
  folder (a spoken update) become voice notes, reported on the next run.
- **Note vocali/** — one note per voice memo: an iPhone memo the CEO shares with Alfred on Telegram or
  saves in his own memo folder (for recordings too big for the chat), a colleague's audio in the updates
  folder, or `alfred brain transcribe FILE`. Alfred transcribes it with
  its own speech-to-text, gives it a title, a type (idea, da fare, incontro, decisione, promemoria,
  aggiornamento) and an area (commerciale, produzione, progettazione, fornitori, amministrazione,
  personale), and keeps summary, actions and the full transcript. The audio itself is never copied.
- Recall before every turn (names and aliases first, then keyword search, model-free) and seven
  tools: `brain_search`, `brain_read`, `brain_note`, `brain_sync`, `brain_inbox`, `brain_transcribe`,
  `brain_consolidate`.
- The vault is a git repository: every change Alfred makes is a commit you can inspect or revert.

Consolidation (journal and recordings → entity notes) runs at the end of each session and nightly
at 03:00 on the model Alfred uses (cloud or local), or on the auxiliary slot `alfred_brain`
(`auxiliary.alfred_brain` in config.yaml) if you want a different one.

## Install

```bash
alfred plugins install Blubobo1010/alfred-brain --enable
alfred brain init
alfred brain setup
alfred mcp add plaud --url https://mcp.plaud.ai/mcp --auth oauth
alfred gateway restart
alfred brain status
```

`install` sets `memory.provider: brain`; `init` creates `~/Alfred-Vault` (or `--path …`) and sets
`OBSIDIAN_VAULT_PATH`; `setup` schedules Plaud every 30 minutes and consolidation at 03:00; after `mcp add`, log in
with the Plaud account. Optional folders, then `alfred gateway restart` again:

```bash
alfred brain inbox --path "$HOME/Library/CloudStorage/OneDrive-Kimera/Aggiornamenti Alfred"
alfred brain inbox --voice-path "$HOME/Library/CloudStorage/OneDrive-Kimera/Alfred"
```

`--path` is the colleagues' shared updates folder, `--voice-path` the CEO's own memo folder (take the exact
OneDrive folder name from `ls ~/Library/CloudStorage/`).

`alfred brain` commands exist only while `brain` is the active memory provider (Hermes wires the
CLI of the active provider only). Other commands: `sync`, `inbox` (import now), `transcribe FILE…`,
`consolidate`, `reindex`.

## Voice memos

Voice notes use Alfred's own speech-to-text (`stt.*` in config.yaml). Local faster-whisper keeps the
audio on the machine, but it is not part of the standard install and Alfred transcribes in English by
default:

```bash
alfred pm install --extra stt-whisper
alfred config set stt.provider local
alfred config set stt.local.model turbo
alfred config set stt.language it
alfred gateway restart
alfred brain transcribe ~/test.m4a
alfred brain status
```

`stt.provider local` keeps the audio on the machine (the "not a recognized config key" warning is expected: Hermes
leaves the key unset on purpose, and reads it). `turbo` is large-v3-turbo, good in Italian; the default `base` is not.
The first transcription downloads the model (about 1.6 GB); `brain status` should show `faster_whisper: true` and
`language: it`. These settings also apply to the voice messages recorded in Telegram.

Speed on the CPU (int8), measured on an Apple M4 with 4 performance cores: `turbo` transcribes Italian at
about 4× real time (a 10-minute memo in about 2.5 minutes), `small` at about 6.5× but with more mistakes.
Macs with more performance cores are faster.

- In the chat, `brain_transcribe` waits up to 4 minutes; when a recording takes longer it answers
  "in progress" and goes on in the background. When it is done the updates job (if scheduled) runs at
  the next cron tick and sends the summary; the CEO can also just ask ("com'è venuta la nota vocale?").
- One transcription at a time. Telegram bots receive files up to 20 MB: in the iPhone settings set
  Voice Memos → Audio Quality → Compressed (about 20–40 minutes fit).
- **The CEO's own folder** (`--voice-path`, `ALFRED_BRAIN_VOICE`) takes recordings of any size: a folder
  only he can see, e.g. `Alfred` in his OneDrive, synced on the Mac (iPhone: Share → Save to Files →
  OneDrive → Alfred). Audio only; every recording is his memo. It is checked every 30 minutes and the
  summary goes out as soon as the transcription is done. Never use the colleagues' updates folder for his
  memos: they can open it.
- `stt.local.initial_prompt` with recurring names and codes ("Kimera, EVO38, K39") helps the
  transcription of acronyms.

## What this plugin does on your machine

- Reads and writes Markdown files under the vault folder only (and `.alfred/` state inside it);
  reads (never modifies) the updates folder and the CEO's memo folder if configured (`ALFRED_BRAIN_INBOX`,
  `ALFRED_BRAIN_VOICE`).
- Reads the audio files it is given (chat attachments, the two folders, the CLI) and hands them to
  Alfred's speech-to-text: with `stt.provider local` nothing leaves the machine, with a cloud
  provider the audio goes to that provider. Audio is never copied into the vault.
- Calls the configured model for consolidation, through Hermes' plugin LLM facade.
- Calls the MCP server `plaud` only if you add it to `plugins.entries.brain.mcp_allowlist`
  (`alfred brain setup` does it); no other network access.
- Runs `git` in the vault folder when git is installed.
- Moves its own updates cron job to the next tick when a recording nobody is waiting for is filed.

## Development

```bash
uv run --no-project --with pytest pytest -q tests
uvx ruff check .
```
The tests use a fake vault, model and MCP server: no network.
Hermes contracts: from an Alfred checkout, `python -m hermes_cli.main plugins doctor <repo> --ci`
and `plugins validate <repo>`.

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
  listed as they are (by keyword, never dropped by the model), then risks and news.
- Recall before every turn (names and aliases first, then keyword search, model-free) and six
  tools: `brain_search`, `brain_read`, `brain_note`, `brain_sync`, `brain_inbox`, `brain_consolidate`.
- The vault is a git repository: every change Alfred makes is a commit you can inspect or revert.

Consolidation (journal and recordings → entity notes) runs at the end of each session and nightly
at 03:00 on the model Alfred uses (cloud or local), or on the auxiliary slot `alfred_brain`
(`auxiliary.alfred_brain` in config.yaml) if you want a different one.

## Install

```bash
alfred plugins install Blubobo1010/alfred-brain --enable   # sets memory.provider: brain
alfred brain init                  # creates ~/Alfred-Vault (or --path …), sets OBSIDIAN_VAULT_PATH
alfred brain setup                 # cron jobs: Plaud every 30 min, consolidation at 03:00
alfred mcp add plaud --url https://mcp.plaud.ai/mcp --auth oauth   # then log in with the Plaud account
alfred brain inbox --path "~/Library/CloudStorage/OneDrive-…/Aggiornamenti Alfred"   # optional
alfred gateway restart
alfred brain status
```

`alfred brain` commands exist only while `brain` is the active memory provider (Hermes wires the
CLI of the active provider only). Other commands: `sync`, `inbox` (import now), `consolidate`, `reindex`.

## What this plugin does on your machine

- Reads and writes Markdown files under the vault folder only (and `.alfred/` state inside it);
  reads (never modifies) the updates folder if configured (`ALFRED_BRAIN_INBOX`).
- Calls the configured model for consolidation, through Hermes' plugin LLM facade.
- Calls the MCP server `plaud` only if you add it to `plugins.entries.brain.mcp_allowlist`
  (`alfred brain setup` does it); no other network access.
- Runs `git` in the vault folder when git is installed.

## Development

```bash
uv run --no-project --with pytest pytest -q tests   # fake vault, model and MCP; no network
uvx ruff check .
```
Hermes contracts: from an Alfred checkout, `python -m hermes_cli.main plugins doctor <repo> --ci`
and `plugins validate <repo>`.

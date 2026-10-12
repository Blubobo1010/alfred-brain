# alfred-brain — istruzioni per Claude

Memory provider Hermes `brain` (plugin `exclusive`): vault Obsidian come memoria di Alfred. Repo =
pacchetto plugin (`plugin.yaml` + `__init__.py`), stdlib only, niente `pyproject.toml` (come alfred-odoo).

## Comandi
- `uv run --no-project --with pytest pytest -q tests` (fake in `tests/conftest.py`: `FakeHost` per modello e MCP).
- `uvx ruff check .`
- Contratti Hermes dall'albero Alfred (`~/dev/Alfred/work/tree`): `../alfred-venv/bin/python -m hermes_cli.main
  plugins doctor ~/dev/alfred-brain --ci` e `plugins validate`.
- Prova vera: `HERMES_HOME` usa-e-getta, `rsync` del repo in `$HERMES_HOME/plugins/brain/` (install da path
  locale non supportato), `memory.provider: brain` in config.yaml, poi `… -m hermes_cli.main brain init --path …`,
  `-z "…"` per i turni, `brain consolidate`, `cron run <id>`. Con Ollama serve `model.ollama_num_ctx: 65536`
  (Alfred rifiuta < 64K); qwen2.5:7b consolida bene ma non regge l'agente completo.

## Invarianti
- Alfred scrive nelle note entità SOLO tra `<!-- alfred:inizio -->` e `<!-- alfred:fine -->` (`vault.add_facts`);
  il resto è degli umani. Ogni fatto porta il link alla fonte.
- Contesti non `primary` (cron, subagent, flush) non scrivono il diario (`provider.writes`).
- Diario senza modello; consolidamento per offset in `.alfred/state.json` (`journal_offsets`, `plaud_pending`):
  un blocco fallito resta in coda. Un solo consolidamento alla volta (`consolidate._run_lock`).
- `on_session_end` consolida in un thread: CLI/one-shot che escono subito lo perdono, lo recupera il cron delle 03:00.
- La CLI Hermes carica `cli.py` solo per il provider attivo e senza `register()`: lì il task ausiliario
  `alfred_brain` non esiste e `HermesHost.llm_json` ripiega sul modello principale.
- Plaud: forme dei risultati MCP non documentate ufficialmente → parsing tollerante in `plaud.py`; nomi
  dei parametri letti dallo schema del tool (`HermesHost.mcp_params`), fallback `file_id`/`fileId`.
  `call_mcp` tronca a 64 KB: per le trascrizioni lunghe `HermesHost.mcp` rifà la chiamata senza tetto.
- La casella Gmail personale del CEO non va MAI scritta nel vault (decisione utente 2026-10-12).

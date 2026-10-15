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
  (Alfred rifiuta < 64K) e `model.base_url` con `/v1`: senza, l'agente va ma le chiamate del plugin (facade
  `PluginLlm`) prendono 404 e le note vocali finiscono in `altro · altro` senza riassunto. qwen2.5:7b consolida
  bene ma non regge l'agente completo. Le chiamate del plugin non passano `ollama_num_ctx`: Ollama apre un secondo
  runner dello stesso modello a 8192. Con qwen2.5:14b (64K ≈ 22 GB, più il runner a 8K e Whisper) un Mac da 24 GB si
  è riavviato durante un turno `-z` (2026-10-14, causa probabile la RAM): nei turni di prova usare qwen2.5:7b.
- Note vocali in prova: audio italiano sintetico con `say -v Alice -o x.m4a --file-format=m4af --data-format=aac`
  (mai audio vero nel repo), `stt.provider: local`, `stt.language: it`, `stt.local.model: turbo`; `brain transcribe`
  dalla CLI e il turno `-z` con il testo della nota-allegato del gateway (`gateway/run_inbound.py`,
  `_prepend_inbound_media_file_notes`) girano in-process come il gateway. Su Ollama con `base_url …/v1` il prompt
  dell'agente viene troncato a 4K (`num_ctx` ignorato): un turno `-z` lì non prova la scelta del tool.

## Invarianti
- Alfred scrive nelle note entità SOLO tra `<!-- alfred:inizio -->` e `<!-- alfred:fine -->` (`vault.add_facts`);
  il resto è degli umani. Ogni fatto porta il link alla fonte.
- Contesti non `primary` (cron, subagent, flush) non scrivono il diario (`provider.writes`).
- Diario senza modello; consolidamento per offset in `.alfred/state.json` (`journal_offsets`, `plaud_pending`,
  `inbox_pending`, `voice_pending`): una nota cancellata a mano esce dalla coda.
- Collegamento in background (`consolidate.kick`): un worker per vault, avviato con il contesto Hermes di chi lo
  chiama; lo attivano nota vocale archiviata, `brain_inbox`/`brain_sync` (MAI consolidare dentro il tool: con
  l'arretrato si superano i 420 s e il report delle cartelle va perso), `brain_consolidate` notturno,
  `on_session_end`. Prima di ogni chiamata aspetta che il llama-server gestito da Hermes sia fermo da 15 s
  (`Host.model_busy` → `agent.review_idle_queue._managed_server_idle`; `None` = nessun modello locale, niente
  attesa), dopo 30 min parte comunque. Un kick durante un giro dà un giro in più. Un giro alla volta per vault anche
  tra processi: lock di thread + `flock` su `.alfred/consolidate.lock` (escluso da git con `vault.ignore_in_git`, in
  byte e mai bloccante). Il lock di thread si rilascia SEMPRE (anche se `flock(LOCK_UN)` fallisce): se restasse preso,
  ogni giro sarebbe `busy` per sempre.
  La CLI è un processo breve: `host.background = False`, e `consolidate.link`/`cli._link` fanno il giro in primo piano
  prima di uscire. Mai chiamare `kick`/`run` tenendo `_jobs_lock` o `vault._lock`.
- Tentativi: una risposta inutilizzabile (`ConsolidationError`, `ValueError`) o una richiesta che il server rifiuta
  per quel testo (HTTP 400/413/422, contesto troppo lungo: `counts_against_source`) conta contro la fonte; timeout,
  connessione e 5xx mai. Dopo 3 la fonte si salta (`consolidation_skipped`, in `brain status`) solo se il modello
  ha funzionato su un'altra fonte dopo il suo primo fallimento (`last_extraction_ok`): un modello che risponde
  spazzatura a tutto non deve cancellare l'arretrato. Le fonti già fallite vanno per ultime; due errori di fila
  chiudono il giro.
- Schemi RIGIDI (`enum`, `required`): su un server locale sono la grammatica di decodifica. Prova del 2026-10-15
  con qwen2.5:7b: senza `required` saltava titolo e riassunto, senza `enum` metteva un'area nella categoria. Se un
  provider che non vincola risponde fuori schema, Hermes solleva `ValueError … schema` e `HermesHost.llm_json` rifà
  la chiamata con `RELAXED_SCHEMA` mettendo lo schema rigido nelle istruzioni (Hermes al modello mostra solo lo schema
  passato). Ogni lista che arriva dal modello passa da `host.as_list`: una stringa iterata diventava un fatto, o un
  punto del report Telegram, per ogni lettera. `entity_type` (sinonimi), `merge`, `choice` e `impegni.actions`
  normalizzano.
- La CLI Hermes carica `cli.py` solo per il provider attivo e senza `register()`: lì il task ausiliario
  `alfred_brain` non esiste: `cli._provider` e `brain eval` lo registrano (`HermesHost.register_aux_task`) per usare
  il modello del gateway; altrimenti `HermesHost.llm_json` ripiega sul modello principale, ma solo per
  `PluginLlmTrustError` (`PermissionError`): un timeout non deve costare due chiamate. `answered_by` dice chi ha
  risposto davvero (`brain eval` lo riporta in `risposto_da`).
- Impegni (`impegni.py`): unica fonte `.alfred/impegni.json` con id progressivi (#12) usati ovunque; `Impegni.md` è
  una vista riscritta a ogni modifica, mai riletta ed esclusa dal richiamo (`index.NOT_RECALLED`). Nascono nella
  stessa chiamata che classifica la nota vocale (niente seconda estrazione); in chat con `brain_impegni`
  (elenca/aggiungi/modifica). Un memo non chiude nulla e non sposta una data esistente (la riempie se manca,
  altrimenti `entro_detto` nel report). `stesso_di` del modello vale solo se `same_task`: stesso responsabile
  (`same_owner`, parole del nome senza titoli) e Jaccard delle parole ≥ 0,5 (un 7b agganciava compiti diversi della
  stessa persona). Azione di una parola sola → la citazione `evidenza` diventa il testo (`action_text`). Correzioni
  contate (modificati, annullati, riaperti): con le citazioni trovate sono la qualità del modello in `brain status`.
  "io" = autore della nota: CEO per chat, CLI e cartella personale, il collega per la cartella condivisa; nome del
  CEO da `plugins.entries.brain.settings.owner_name` (`Host.owner()`). Il mittente Telegram è il CEO se ha anche solo
  una parola del suo nome ("Luca B.", "Dott. Luca Betti | Kimera"): nel dubbio è lui (`same_person`). Elenco,
  duplicati e agganci confrontano i responsabili con `same_owner` ("Andrea" = "Andrea Lorini"); i nomi senza parole
  comuni (`_names`: "della" non aggancia nulla). In `modifica` un `entro` vuoto non cambia niente ("nessuna" toglie la
  data): altrimenti si perde la scadenza e si gonfia il tasso di correzione. Una voce di `impegni.json` senza id,
  chi, cosa o stato = file danneggiato (errore chiaro, mai sovrascritto); le modifiche dalla chat fanno commit.
- `alfred brain eval FILE.jsonl`: stessa `voice.classify` e stesse riparazioni dell'archivio; i memo veri
  etichettati restano sul Mac del cliente, MAI nel repo (qui solo `tests/fixtures/eval-sintetico.jsonl`, inventato).
- Plaud: forme dei risultati MCP non documentate ufficialmente → parsing tollerante in `plaud.py`; nomi
  dei parametri letti dallo schema del tool (`HermesHost.mcp_params`), fallback `file_id`/`fileId`.
  `call_mcp` tronca a 64 KB: per le trascrizioni lunghe `HermesHost.mcp` rifà la chiamata senza tetto.
- Cartella aggiornamenti (`inbox.py`, env `ALFRED_BRAIN_INBOX`, cron con deliver telegram): una versione di file =
  un hash di contenuto; file freschi (< 2 min), nascosti e `~$` saltati. I punti "da vedere" del report sono
  per parola chiave (`FLAG_RE`), non dal modello: con un modello debole non devono sparire. La cartella non si scrive mai.
- Note vocali (`voice.py`, un solo flusso per chat, cartella e CLI): l'audio non entra MAI nel vault, solo la nota
  `Note vocali/<data> <titolo>.md` con la trascrizione. STT = quello di Hermes (`tools.transcription_tools`,
  sezione `stt.*`): default inglese e faster-whisper fuori da `[all]`, quindi `stt.language it` e
  `alfred pm install --extra stt-whisper` sono parte del setup.
  - Una trascrizione per volta (`voice._cpu`, CPU-bound); un job per hash di contenuto (`_jobs`): una seconda
    richiesta si aggancia. Mai `_jobs_lock` e `vault._lock` insieme.
  - `brain_transcribe` aspetta al massimo `TOOL_WAIT` (240 s, sotto i 420 s della deadline dei tool Hermes), poi
    `in_progress`; il job continua nel thread e il risultato resta in `voice_report` finché qualcuno lo riferisce
    (stesso tool richiamato, CLI, report della cartella). Il provider non può scrivere all'utente di sua iniziativa:
    quando finisce un memo che nessuno aspetta (cartelle, chat andata in `in_progress`) `Host.wake_inbox` sposta il
    job cron `INBOX_JOB` al tick successivo (`cron.jobs.trigger_job`, solo se abilitato: su un job in pausa lo
    riattiverebbe). La CLI mette `host.wake = False`: stampa lei i report.
  - Cartella personale del CEO (`ALFRED_BRAIN_VOICE`, `alfred brain inbox --voice-path`): solo audio, fonte
    `cartella personale`, niente autore né righe "da vedere"; scansionata per prima, così se sta dentro la cartella
    aggiornamenti i suoi file restano suoi (e i suoi testi non diventano aggiornamenti dei colleghi). I memo del CEO
    non vanno MAI nella cartella condivisa: i colleghi la vedono. Una cartella mancante non ferma l'altra
    (`report["missing"]`); errore solo se mancano tutte.
  - Le cartelle non aspettano mai: un drain in background (`voice.drain`), report al giro dopo (anticipato da
    `wake_inbox`).
    Funziona perché su macOS i cron girano nel processo del gateway e il plugin, di default
    (`plugins.isolation: in_process`), gira dentro quel processo: i thread sopravvivono al turno. Con
    `plugins.isolation: host` gira in un processo figlio longevo (un thread per richiesta, nessun timeout RPC).
  - Errori delle cartelle in `voice_failed` (3 tentativi, poi una riga "non riuscita"), MAI in `errors`:
    con `errors` il prompt del cron risponderebbe [SILENT].
  - Nel diario il turno di una nota vocale tiene solo il link (`Nota vocale archiviata: [[…]]`): i fatti si
    consolidano dalla nota (`voice_pending`), non due volte.
  - I fatti di una nota vocale escludono i compiti (`consolidate.VOICE_INSTRUCTIONS`: l'elenco non nomina scadenze
    né prossimi passi e il divieto è la prima regola; una regola aggiunta in fondo il 7b la ignorava): i compiti sono
    impegni. Il prompt di classificazione ha autore, data e un calendario "oggi" + i 7 giorni DOPO + "fine mese": ogni
    nome di giorno ha una sola data e mai oggi (con due lunedì il 7b prendeva il secondo; un memo di lunedì che dice
    "lunedì" intende il prossimo).
- La casella Gmail personale del CEO non va MAI scritta nel vault (decisione utente 2026-10-12).

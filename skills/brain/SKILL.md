---
name: brain
description: Use the CEO's second brain (Obsidian vault kept by Alfred) — recall people, companies, projects, past conversations and Plaud recordings, and save what the CEO asks to remember.
---

# Secondo cervello

The vault holds a daily journal of every conversation (`Diario/`), one note per Plaud recording
(`Plaud/`), colleagues' written updates (`Aggiornamenti/`), and notes on clients, people and projects (`Clienti/`, `Persone/`, `Progetti/`) whose
"Cosa sa Alfred" block lists facts with their source.

## How to work

1. Before answering about a person, a company, a meeting or "what we said", call `brain_search`
   with the names involved; open the best hit with `brain_read` when the excerpt is not enough.
2. Answer from the notes and cite them as `[[path]]` (e.g. `[[Clienti/Kimera]]`, `[[Plaud/2026-10-12 Riunione]]`).
   If the vault has nothing, say so; never invent a past conversation.
3. When the CEO says "ricordati che…", states a decision or corrects a fact, save it with `brain_note`
   (one short self-contained sentence, the right `tipo`: cliente, persona, progetto).
4. "Le ultime registrazioni": call `brain_sync` first, then search `Plaud/`.
   Production, design and other operational status ("a che punto è P03?") come from colleagues'
   updates in `Aggiornamenti/` and the project notes: call `brain_inbox` first if asked for the latest.
5. Never edit text outside the `<!-- alfred:inizio -->` … `<!-- alfred:fine -->` block of a note:
   that part is written by humans.
6. The personal mailbox is never copied into the vault.

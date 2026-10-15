---
name: brain
description: Use the CEO's second brain (Obsidian vault kept by Alfred) — recall people, companies, projects, past conversations, Plaud recordings and voice memos, file the audio files the CEO sends, keep track of commitments (who must do what, by when), and save what the CEO asks to remember.
---

# Secondo cervello

The vault holds a daily journal of every conversation (`Diario/`), one note per Plaud recording
(`Plaud/`), colleagues' written updates (`Aggiornamenti/`), one note per voice memo (`Note vocali/`), notes on
clients, people and projects (`Clienti/`, `Persone/`, `Progetti/`) whose "Cosa sa Alfred" block lists facts with
their source, and the commitments Alfred tracks (`Impegni.md`, each with a number like #12).

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
5. When the CEO sends an audio file (an iPhone voice memo shared with you), call `brain_transcribe` with the
   path given in the attachment note and reply with its `report` as it is: title, category, summary, the
   numbered commitments and the link to the note. Status `in_progress` means a long recording: tell him it will
   be ready in a few minutes, and when he asks again call `brain_transcribe` with the same path. A failure comes
   with a `hint`: pass it on. Recordings too big for the chat go in his own memo folder (e.g. `Alfred` on his
   OneDrive) when one is configured: they are filed and summarised without him asking.
6. Commitments (`brain_impegni`): voice memos record them by themselves; never ask him to file anything.
   - He states one ("Marco mi manda il disegno entro giovedì", "devo chiamare Beta") → `azione: aggiungi`
     with `chi` (a name, or "io" for him), `cosa` (short, verb first) and `entro` as YYYY-MM-DD when he gives a day.
   - "Il 12 è fatto" / "Marco l'ha mandato" → `azione: modifica`, `stato: fatto` (find the number with `elenca`
     first if he gives none). A wrong one → `stato: annullato`; "riaprilo" → `stato: aperto`.
   - "Il 13 è di Marco, entro lunedì" → `azione: modifica` with `chi` and/or `entro`.
   - "Cosa deve fare Andrea?", "cosa ho in sospeso?", "cosa è scaduto?" → `azione: elenca` with `chi`
     ("io" for his own) or `scaduti: true`, and reply with the `testo` lines.
7. Never edit text outside the `<!-- alfred:inizio -->` … `<!-- alfred:fine -->` block of a note:
   that part is written by humans. `Impegni.md` is rewritten from Alfred's store: change commitments only with
   `brain_impegni`.
8. The personal mailbox is never copied into the vault.

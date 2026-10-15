import os
import sys
import threading
import types

import pytest
from alfred_brain import voice
from alfred_brain.host import HermesHost, TranscriptionError
from alfred_brain.inbox import Inbox
from alfred_brain.vault import parse_note
from alfred_brain.voice import VoiceMemos
from conftest import FakeHost

SPOKEN = ("Ho sentito Marco Bianchi della Beta Compositi. Il preventivo per i pannelli della Z20 arriva venerdì. "
          "Ho deciso di firmare se resta sotto i quarantacinquemila euro. Questo è da vedere con Luca prima della firma.")
INFO = {"titolo": "Preventivo Beta per i pannelli Z20", "categoria": "decisione", "area": "fornitori",
        "argomenti": ["Beta Compositi", "Z20", "Marco Bianchi: acquisti"],
        "riassunto": ["Il preventivo Beta per i pannelli della Z20 arriva venerdì."],
        "azioni": ["Mandare a Bianchi i disegni del cofano"]}


def memo(folder, name, data=b"audio", age=600):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    old = path.stat().st_mtime - age
    os.utime(path, (old, old))
    return path


@pytest.fixture(autouse=True)
def idle():
    yield
    assert voice.wait_idle(5) and not voice._jobs


def test_memo_becomes_a_categorised_note(vault, tmp_path):
    path = memo(tmp_path, "Nuova registrazione 12.m4a")
    host = FakeHost(llm=[INFO], transcripts={path.name: SPOKEN})
    out = VoiceMemos(host, vault).run(path, fonte="telegram", autore="Luca")
    assert out["ok"] and out["status"] == "done" and (out["categoria"], out["area"]) == ("decisione", "fornitori")
    assert out["note"].startswith("Note vocali/") and out["note"].endswith(" Preventivo Beta per i pannelli Z20.md")
    note = parse_note(vault.root / out["note"])
    assert note.title == "Preventivo Beta per i pannelli Z20"
    assert (note.meta["tipo"], note.meta["fonte"], note.meta["autore"], note.meta["file"]) == (
        "nota vocale", "telegram", "Luca", "Nuova registrazione 12.m4a")
    assert note.meta["argomenti"] == ["Beta Compositi", "Z20", "Marco Bianchi acquisti"]
    assert note.meta["tags"] == ["nota-vocale", "decisione", "fornitori"]
    assert "Categoria: decisione · Area: fornitori · Argomenti: Beta Compositi" in note.body
    assert "## Riassunto" in note.body and "## Da fare\n\n- #1 il CEO: Mandare a Bianchi i disegni del cofano\n" in note.body
    assert "## Trascrizione\n\nHo sentito Marco Bianchi" in note.body
    state = vault.state()
    assert state["voice_seen"] == {voice.file_digest(path): out["note"]} and state["voice_pending"] == [out["note"]]
    assert state["voice_report"] == []  # this caller tells the user: not again in the updates report
    assert out["report"].startswith("Nota vocale «Preventivo Beta per i pannelli Z20» (decisione · fornitori)")
    assert ("Impegni:\n#1 Tu — Mandare a Bianchi i disegni del cofano\n"
            "Se uno non è giusto dimmelo (es. «il 1 è di Marco, entro lunedì»).") in out["report"]
    assert "Da vedere" not in out["report"] and out["impegni"] == [1]
    assert out["report"].endswith(f"Testo completo: [[{out['note'][:-3]}]]")
    prompt = host.llm_calls[0]
    assert "Marco Bianchi" in prompt and "Autore della nota: il CEO." in prompt and "Calendario: " in prompt
    assert host.wakes == 0  # the caller got it: no updates report needed


MEETING = ("Riunione con Andrea Lorini sulla digitalizzazione. Andrea mi manda i tre compiti entro venerdì. "
           "Io chiamo Marco Bianchi per il preventivo.")


def test_actions_become_numbered_commitments_with_who_and_when(vault, tmp_path):
    from datetime import date

    from alfred_brain.impegni import Impegni
    path = memo(tmp_path, "riunione.m4a")
    stamp = os.path.getmtime(path)
    when = date.fromtimestamp(stamp)
    friday = next(when.fromordinal(when.toordinal() + i) for i in range(7) if when.fromordinal(when.toordinal() + i).weekday() == 4)
    acts = [{"chi": "Andrea Lorini", "cosa": "mandare i tre compiti", "entro": friday.isoformat(), "stesso_di": 0,
             "evidenza": "Andrea mi manda i tre compiti entro venerdì"},
            {"chi": "io", "cosa": "chiamare Marco Bianchi per il preventivo", "entro": "", "evidenza": "Io chiamo Marco Bianchi"},
            {"chi": "Alfred", "cosa": "ricordarmelo"}]
    host = FakeHost(llm=[{**INFO, "azioni": acts}], transcripts={"riunione.m4a": MEETING}, owner="Luca Betti")
    out = VoiceMemos(host, vault).run(path, fonte="telegram", autore="Luca")
    assert out["impegni"] == [1, 2]
    assert f"#1 Andrea Lorini — mandare i tre compiti — entro ven {friday.day:02d}/{friday.month:02d}" in out["report"]
    assert "#2 Tu — chiamare Marco Bianchi per il preventivo" in out["report"] and "ricordarmelo" not in out["report"]
    note = (vault.root / out["note"]).read_text(encoding="utf-8")
    assert f"- #1 Andrea Lorini: mandare i tre compiti 📅 {friday.isoformat()}" in note and "- #2 Luca Betti: chiamare" in note
    stored = Impegni(vault).load()["impegni"]
    assert [i["fonte"] for i in stored] == [f"[[{out['note'][:-3]}]]"] * 2 and stored[0]["riscontro"] is True
    assert "Autore della nota: Luca Betti (il CEO)." in host.llm_calls[0] and when.isoformat() in host.llm_calls[0]

    later = memo(tmp_path, "seguito.m4a", b"seguito")
    host.llm = [{**INFO, "titolo": "Seguito", "azioni": [{"chi": "Andrea", "cosa": "mandare i compiti", "stesso_di": 1}]}]
    host.transcripts = {"seguito.m4a": "Sentito Andrea, i compiti arrivano."}
    again = VoiceMemos(host, vault).run(later, fonte="telegram", autore="Luca")
    assert "Impegni già aperti (numero, chi, cosa):\n#2 Luca Betti — chiamare" in host.llm_calls[1]
    assert "#1 Andrea Lorini — mandare i tre compiti" in host.llm_calls[1]
    assert "#1 (già aperto) Andrea Lorini — mandare i tre compiti" in again["report"] and again["impegni"] == [1]
    assert "(es. «il 1 è di Marco" in again["report"]


def test_who_io_is_depends_on_who_recorded(vault, tmp_path):
    other = memo(tmp_path, "andrea.m4a", b"andrea")
    host = FakeHost(llm=[{**INFO, "azioni": [{"chi": "io", "cosa": "mandare il piano"}]}],
                    transcripts={"andrea.m4a": "Io mando il piano."}, owner="Luca Betti")
    out = VoiceMemos(host, vault).run(other, fonte="telegram", autore="Andrea Lorini")  # another user the bot allows
    assert "#1 Andrea Lorini — mandare il piano" in out["report"]
    assert "Autore della nota: Andrea Lorini (collaboratore)." in host.llm_calls[0]


def test_a_damaged_commitments_file_still_files_the_memo(vault, tmp_path):
    (vault.root / ".alfred" / "impegni.json").write_text("{rotto", encoding="utf-8")
    path = memo(tmp_path, "f.m4a")
    out = VoiceMemos(FakeHost(llm=[INFO], transcripts={"f.m4a": SPOKEN}), vault).run(path, fonte="telegram")
    assert out["status"] == "done" and out["impegni"] == []
    assert "Da fare:\n- Mandare a Bianchi i disegni del cofano" in out["report"]
    assert "## Da fare\n\n- Mandare a Bianchi" in (vault.root / out["note"]).read_text(encoding="utf-8")


def test_a_filed_memo_is_linked_in_the_background(vault, tmp_path):
    from alfred_brain import consolidate
    path = memo(tmp_path, "g.m4a")
    facts = {"entita": [{"tipo": "cliente", "nome": "Beta Compositi", "fatti": [{"testo": "Preventivo sotto 45.000 euro."}]}]}
    host = FakeHost(llm=[INFO, facts], transcripts={"g.m4a": SPOKEN}, background=True)
    out = VoiceMemos(host, vault).run(path, fonte="telegram")
    assert out["status"] == "done" and consolidate.wait_idle(5)
    assert vault.state()["voice_pending"] == []
    assert f"([[{out['note'][:-3]}]])" in (vault.root / "Clienti" / "Beta Compositi.md").read_text(encoding="utf-8")


def test_same_recording_twice_is_filed_once_unless_its_note_was_deleted(vault, tmp_path):
    path = memo(tmp_path, "a.m4a")
    host = FakeHost(llm=[INFO, INFO], transcripts={"a.m4a": SPOKEN})
    memos = VoiceMemos(host, vault)
    first = memos.run(path, fonte="telegram")
    again = memos.run(path, fonte="telegram")
    assert again["status"] == "duplicate" and again["note"] == first["note"] and "già archiviata" in again["report"]
    assert host.transcribe_calls == ["a.m4a"]
    (vault.root / first["note"]).unlink()
    assert memos.run(path, fonte="telegram")["status"] == "done" and len(host.transcribe_calls) == 2


def test_model_failure_still_files_the_memo(vault, tmp_path):
    path = memo(tmp_path, "b.m4a")

    def boom(_text):
        raise RuntimeError("model offline")

    out = VoiceMemos(FakeHost(llm=boom, transcripts={"b.m4a": SPOKEN}), vault).run(path, fonte="telegram")
    note = parse_note(vault.root / out["note"])
    assert (note.meta["categoria"], note.meta["area"]) == ("altro", "altro")
    assert note.title.startswith("Ho sentito Marco Bianchi della Beta Compositi")
    assert "_Riassunto non disponibile._" in note.body and "(riassunto non disponibile)" in out["report"]


def test_an_unusable_model_answer_is_logged(vault, tmp_path, caplog):
    path = memo(tmp_path, "e.m4a")
    out = VoiceMemos(FakeHost(llm=lambda _text: None, transcripts={"e.m4a": SPOKEN}), vault).run(path, fonte="telegram")
    assert out["categoria"] == "altro" and "classifying a voice memo failed: the model gave no usable JSON" in caplog.text


def test_categories_outside_the_lists_become_altro(vault, tmp_path):
    path = memo(tmp_path, "c.m4a")
    host = FakeHost(llm=[{**INFO, "categoria": "urgente", "area": "marketing"}], transcripts={"c.m4a": SPOKEN})
    out = VoiceMemos(host, vault).run(path, fonte="telegram")
    assert (out["categoria"], out["area"]) == ("altro", "altro")


def test_no_speech_is_not_filed_and_a_request_retries_it(vault, tmp_path):
    path = memo(tmp_path, "silenzio.m4a")
    host = FakeHost(transcripts={"silenzio.m4a": ""})
    memos = VoiceMemos(host, vault)
    out = memos.run(path, fonte="telegram")
    assert out["status"] == "no_speech" and "nessun parlato" in out["report"]
    assert vault.state()["voice_seen"] == {voice.file_digest(path): ""}
    assert not list((vault.root / "Note vocali").glob("*.md"))
    memos.run(path, fonte="telegram")
    assert host.transcribe_calls == ["silenzio.m4a"] * 2


def test_transcription_error_is_returned_and_retried(vault, tmp_path):
    path = memo(tmp_path, "d.m4a")
    host = FakeHost(transcripts={"d.m4a": TranscriptionError("No STT provider available")})
    out = VoiceMemos(host, vault).run(path, fonte="telegram")
    assert out["ok"] is False and "No STT provider available" in out["error"] and "stt" in out["hint"]
    assert "voice_seen" not in vault.state() and vault.state()["voice_report"] == []
    host.transcripts, host.llm = {"d.m4a": SPOKEN}, [INFO]
    assert VoiceMemos(host, vault).run(path, fonte="telegram")["status"] == "done"


def test_bad_files_are_refused(vault, tmp_path):
    memos = VoiceMemos(FakeHost(), vault)
    with pytest.raises(ValueError, match="not an audio file"):
        memos.run(memo(tmp_path, "foto.jpg"), fonte="telegram")
    with pytest.raises(ValueError, match="file not found"):
        memos.run(tmp_path / "sparito.m4a", fonte="telegram")


def test_slow_memo_answers_in_progress_then_the_result_once(vault, tmp_path):
    path = memo(tmp_path, "lunga.m4a")
    gate = threading.Event()
    host = FakeHost(llm=[INFO], transcripts={"lunga.m4a": SPOKEN}, gate=gate)
    memos = VoiceMemos(host, vault)
    first = memos.run(path, fonte="telegram", wait=0.01)
    second = memos.run(path, fonte="telegram", wait=0.01)  # joins the running job
    assert first["status"] == second["status"] == "in_progress" and "same path" in first["hint"]
    gate.set()
    assert voice.wait_idle(5) and host.transcribe_calls == ["lunga.m4a"]
    assert host.wakes == 1  # nobody waits any more: the updates report is moved up
    done = memos.run(path, fonte="telegram")
    assert done["status"] == "done" and done["report"].startswith("Nota vocale «Preventivo Beta")
    assert memos.run(path, fonte="telegram")["status"] == "duplicate"


def test_a_late_memo_comes_with_the_next_updates_report_once(vault, tmp_path):
    folder = tmp_path / "Aggiornamenti Alfred"
    folder.mkdir()
    path = memo(tmp_path, "lunga.m4a")
    gate = threading.Event()
    host = FakeHost(llm=[INFO], transcripts={"lunga.m4a": SPOKEN}, gate=gate)
    assert VoiceMemos(host, vault).run(path, fonte="telegram", wait=0.01)["status"] == "in_progress"
    gate.set()
    assert voice.wait_idle(5)
    first = Inbox(host, vault, folder).sync()
    assert first["voice"]["reported"] == 1 and "Preventivo Beta" in first["report"]
    assert Inbox(host, vault, folder).sync()["report"] == ""
    assert VoiceMemos(host, vault).run(path, fonte="telegram")["status"] == "duplicate"


def test_folder_audio_is_transcribed_in_background_and_reported_next_run(vault, tmp_path):
    folder = tmp_path / "Aggiornamenti Alfred"
    memo(folder, "Fabrizio/aggiornamento.m4a")
    gate = threading.Event()
    host = FakeHost(llm=[{**INFO, "categoria": "aggiornamento", "area": "produzione"}],
                    transcripts={"aggiornamento.m4a": SPOKEN}, gate=gate)
    first = Inbox(host, vault, folder).sync()
    assert first["voice"] == {"pending": 1, "running": True, "reported": 0} and first["report"] == ""
    gate.set()
    assert voice.wait_idle(5) and host.wakes == 1
    second = Inbox(host, vault, folder).sync()
    assert second["voice"] == {"pending": 0, "running": False, "reported": 1} and second["errors"] == []
    assert second["report"].startswith("Nota vocale di Fabrizio «Preventivo Beta per i pannelli Z20» (aggiornamento · produzione)")
    assert "Da vedere:\n- Questo è da vedere con Luca prima della firma." in second["report"]
    rel = next(iter(vault.state()["voice_seen"].values()))
    note = parse_note(vault.root / rel)
    assert (note.meta["fonte"], note.meta["autore"]) == ("cartella", "Fabrizio")
    third = Inbox(host, vault, folder).sync()
    assert third["report"] == "" and third["voice"]["pending"] == 0 and host.transcribe_calls == ["aggiornamento.m4a"]


def test_the_ceo_s_own_folder_files_his_memos_without_author_or_flags(vault, tmp_path):
    own = tmp_path / "OneDrive" / "Alfred"
    memo(own, "Riunioni/Nuova registrazione 7.m4a")
    gate = threading.Event()
    host = FakeHost(llm=[INFO], transcripts={"Nuova registrazione 7.m4a": SPOKEN}, gate=gate)
    first = Inbox(host, vault, voice_folder=own).sync()
    assert first["new"] == 0 and first["voice"] == {"pending": 1, "running": True, "reported": 0}
    gate.set()
    assert voice.wait_idle(5)
    report = Inbox(host, vault, voice_folder=own).sync()["report"]
    assert report.startswith("Nota vocale «Preventivo Beta per i pannelli Z20» (decisione · fornitori)")
    assert "Da vedere" not in report  # "da vedere con Luca" in his own memo is not a point for him
    note = parse_note(vault.root / next(iter(vault.state()["voice_seen"].values())))
    assert note.meta["fonte"] == "cartella personale" and "autore" not in note.meta
    assert Inbox(host, vault, voice_folder=own).sync()["report"] == ""
    assert host.transcribe_calls == ["Nuova registrazione 7.m4a"]


def test_the_ceo_s_folder_inside_the_updates_folder_stays_his(vault, tmp_path):
    folder = tmp_path / "Aggiornamenti Alfred"
    memo(folder, "Luca/memo.m4a")
    memo(folder, "Luca/appunti.txt", "Appunti privati: il prezzo minimo è 40.000 euro.".encode())
    gate = threading.Event()
    host = FakeHost(llm=[INFO], transcripts={"memo.m4a": SPOKEN}, gate=gate)
    first = Inbox(host, vault, folder, voice_folder=folder / "Luca").sync()
    assert first["new"] == 0 and first["voice"]["pending"] == 1  # his text is not a colleague's update
    gate.set()
    assert voice.wait_idle(5)
    assert Inbox(host, vault, folder, voice_folder=folder / "Luca").sync()["report"].startswith("Nota vocale «")


def test_folder_skips_fresh_hidden_office_and_video_files(vault, tmp_path):
    folder = tmp_path / "Agg"
    memo(folder, "in arrivo.m4a", age=0)
    memo(folder, ".nascosto.m4a")
    memo(folder, "~$x.m4a")
    memo(folder, "video.mp4")
    assert Inbox(FakeHost(), vault, folder).sync()["voice"]["pending"] == 0


def test_folder_no_speech_is_left_alone(vault, tmp_path):
    folder = tmp_path / "Agg"
    memo(folder, "silenzio.m4a")
    host = FakeHost(transcripts={"silenzio.m4a": ""})
    Inbox(host, vault, folder).sync()
    assert voice.wait_idle(5)
    assert "nessun parlato" in Inbox(host, vault, folder).sync()["report"]
    Inbox(host, vault, folder).sync()
    assert host.transcribe_calls == ["silenzio.m4a"]


@pytest.mark.parametrize("own", [False, True], ids=["updates folder", "ceo folder"])
def test_folder_failures_give_up_after_three_runs_and_are_reported_once(vault, tmp_path, own):
    folder = tmp_path / "Agg"
    memo(folder, "rotto.m4a")
    host = FakeHost(transcripts={"rotto.m4a": TranscriptionError("decode error")})

    def inbox():
        return Inbox(host, vault, voice_folder=folder) if own else Inbox(host, vault, folder)

    reports = []
    for _ in range(5):
        out = inbox().sync()
        assert voice.wait_idle(5) and out["errors"] == []
        reports.append(out["report"])
    reports.append(inbox().sync()["report"])
    assert host.transcribe_calls == ["rotto.m4a"] * 3
    assert "\n".join(reports).count("non riuscita dopo 3 tentativi (TranscriptionError: decode error)") == 1


def test_one_folder_pass_at_a_time_and_a_chat_request_joins_it(vault, tmp_path):
    folder = tmp_path / "Agg"
    memo(folder, "due.m4a", b"due")
    memo(folder, "uno.m4a", b"uno")
    gate = threading.Event()
    host = FakeHost(llm=[INFO, INFO], transcripts={"due.m4a": SPOKEN, "uno.m4a": SPOKEN}, gate=gate)
    Inbox(host, vault, folder).sync()
    again = Inbox(host, vault, folder).sync()
    assert again["voice"]["running"] is True
    assert sum(t.name == "brain-voice-drain" for t in threading.enumerate()) == 1
    assert VoiceMemos(host, vault).run(folder / "due.m4a", fonte="telegram", wait=0.01)["status"] == "in_progress"
    gate.set()
    assert voice.wait_idle(5)
    assert sorted(host.transcribe_calls) == ["due.m4a", "uno.m4a"]
    assert len(list((vault.root / "Note vocali").glob("*.md"))) == 2


def test_hermes_host_wakes_only_an_enabled_updates_job(monkeypatch):
    jobs, triggered = {}, []
    fake = types.ModuleType("cron.jobs")
    fake.resolve_job_ref = lambda ref: jobs.get(ref)
    fake.trigger_job = lambda job_id: triggered.append(job_id)
    monkeypatch.setitem(sys.modules, "cron", types.ModuleType("cron"))
    monkeypatch.setitem(sys.modules, "cron.jobs", fake)
    host = HermesHost()
    host.wake_inbox()  # no job: nothing to move
    jobs["brain: aggiornamenti dalla cartella"] = {"id": "j1", "enabled": False}
    host.wake_inbox()  # paused by the user: stays paused
    jobs["brain: aggiornamenti dalla cartella"]["enabled"] = True
    host.wake_inbox()
    host.wake = False
    host.wake_inbox()  # the CLI prints its own reports
    assert triggered == ["j1"]
    host.wake = True
    fake.trigger_job = lambda job_id: 1 / 0
    host.wake_inbox()  # a failure never reaches the transcription


def test_spoken_flags_and_paragraphs():
    text = "Primo punto. Il budget è da approvare entro lunedì! Terzo? Quarto. Quinto. Sesto."
    assert voice.spoken_flags(text) == ["Il budget è da approvare entro lunedì!"]
    assert voice.paragraphs(text) == "Primo punto. Il budget è da approvare entro lunedì! Terzo? Quarto. Quinto.\n\nSesto."


def test_hermes_host_maps_the_stt_result(monkeypatch, tmp_path):
    calls = []
    results = iter([{"success": True, "transcript": " ciao "}, {"success": False, "transcript": "", "no_speech": True},
                    {"success": False, "transcript": "", "error": "No STT provider available"}])

    def transcribe_audio(file_path, model=None, source=None):
        calls.append((file_path, source))
        return next(results)

    fake = types.ModuleType("tools.transcription_tools")
    fake.transcribe_audio = transcribe_audio
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", fake)
    host, path = HermesHost(), tmp_path / "x.m4a"
    assert host.transcribe(path) == "ciao" and host.transcribe(path) == ""
    with pytest.raises(TranscriptionError, match="No STT provider available"):
        host.transcribe(path)
    assert calls[0] == (str(path), "alfred-brain")
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", None)
    with pytest.raises(TranscriptionError, match="not available"):
        host.transcribe(path)
    assert host.local_path("~/memo.m4a") == path.home() / "memo.m4a"


def test_an_unreadable_commitments_file_still_files_the_memo(vault, tmp_path):
    (vault.root / ".alfred" / "impegni.json").write_bytes(b"\xff\xfe{")
    path = memo(tmp_path, "h.m4a")
    out = VoiceMemos(FakeHost(llm=[INFO], transcripts={"h.m4a": SPOKEN}), vault).run(path, fonte="telegram")
    assert out["status"] == "done" and "Da fare:\n- Mandare a Bianchi" in out["report"]

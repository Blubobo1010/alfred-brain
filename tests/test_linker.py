import subprocess
import sys
import threading
import time
from datetime import datetime

import pytest
from alfred_brain import consolidate, journal
from conftest import FakeHost

KIMERA = {"entita": [{"tipo": "cliente", "nome": "Kimera", "fatti": [{"testo": "Budget approvato."}]}]}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(consolidate, "QUIET_SECONDS", 0.05)
    monkeypatch.setattr(consolidate, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(consolidate, "BUSY_RETRY", 0.05)


def turn(vault, text, day=12):
    journal.append_turn(vault, text, "Ok.", when=datetime(2026, 10, day, 10, 0))


def test_kick_links_in_the_background(vault):
    turn(vault, "Kimera ha approvato il budget?")
    host = FakeHost(background=True, llm=KIMERA)
    assert consolidate.kick(host, vault) is True
    assert consolidate.wait_idle(5)
    assert (vault.root / "Clienti" / "Kimera.md").exists() and journal.pending_chunks(vault) == []


def test_without_background_nothing_is_spawned_and_link_runs_now(vault):
    turn(vault, "Kimera?")
    host = FakeHost(llm=KIMERA)
    assert consolidate.kick(host, vault) is False
    assert not any(t.name == "brain-link" for t in threading.enumerate())
    assert consolidate.link(host, vault) == {"chunks": 1, "facts": 1, "failed": 0}
    assert consolidate.link(FakeHost(background=True), vault) == {"background": True}


def test_kicks_coalesce_and_a_kick_during_a_pass_gives_one_more(vault, monkeypatch):
    gate, started, calls, passes = threading.Event(), threading.Event(), [], []
    real_run = consolidate._run
    monkeypatch.setattr(consolidate, "_run", lambda *a: passes.append(1) or real_run(*a))

    def answer(text):
        calls.append(text)
        started.set()
        gate.wait(5)
        return {"entita": []}

    turn(vault, "primo giorno", day=12)
    host = FakeHost(background=True, llm=answer)
    for _ in range(5):
        consolidate.kick(host, vault)
    assert started.wait(5)
    for _ in range(5):  # several kicks while the pass runs: one more pass, not five
        consolidate.kick(host, vault)
    turn(vault, "secondo giorno", day=13)
    gate.set()
    assert consolidate.wait_idle(5)
    assert len(passes) <= 3 and len(calls) == 2 and journal.pending_chunks(vault) == []
    assert sum(t.name == "brain-link" for t in threading.enumerate()) == 0


def test_the_background_pass_waits_for_the_local_model_to_be_idle(vault):
    turn(vault, "Kimera?")
    free, seen = threading.Event(), []
    host = FakeHost(background=True, busy=lambda: not free.is_set(),
                    llm=lambda _t: seen.append(free.is_set()) or {"entita": []})
    consolidate.kick(host, vault)
    time.sleep(0.2)
    assert seen == []  # the CEO's chat has the model
    free.set()
    assert consolidate.wait_idle(5) and seen == [True]


def test_after_the_maximum_wait_the_call_goes_ahead(vault, monkeypatch):
    monkeypatch.setattr(consolidate, "MAX_QUIET_WAIT", 0.1)
    turn(vault, "Kimera?")
    host = FakeHost(background=True, busy=True)
    consolidate.kick(host, vault)
    assert consolidate.wait_idle(5) and len(host.llm_calls) == 1


def test_a_cloud_model_is_not_waited_for(vault):
    turn(vault, "Kimera?")
    host = FakeHost(background=True, busy=None)
    consolidate.kick(host, vault)
    assert consolidate.wait_idle(5) and host.busy_checks == 1 and len(host.llm_calls) == 1


def test_one_pass_at_a_time_in_this_process_and_across_processes(vault):
    gate, started = threading.Event(), threading.Event()

    def slow(_text):
        started.set()
        gate.wait(5)
        return {"entita": []}

    turn(vault, "Kimera?")
    first = threading.Thread(target=consolidate.run, args=(FakeHost(llm=slow), vault))
    first.start()
    assert started.wait(5)
    assert consolidate.run(FakeHost(), vault)["busy"] is True
    lock = vault.root / ".alfred" / consolidate.LOCK_FILE
    other = subprocess.run([sys.executable, "-c", "import fcntl, sys; f = open(sys.argv[1], 'a'); "
                            "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)", str(lock)], capture_output=True)
    assert other.returncode != 0 and b"BlockingIOError" in other.stderr
    gate.set()
    first.join(5)
    assert "busy" not in consolidate.run(FakeHost(), vault, wait=1)
    assert ".alfred/*.lock" in (vault.root / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_the_worker_retries_when_another_process_holds_the_vault(vault):
    turn(vault, "Kimera?")
    lock = vault.root / ".alfred" / consolidate.LOCK_FILE
    other = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time; f = open(sys.argv[1], 'a'); "
                              "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(0.6)", str(lock)],
                             stdout=subprocess.PIPE, text=True)
    assert other.stdout.readline().strip() == "held"
    host = FakeHost(background=True, llm=KIMERA)
    consolidate.kick(host, vault)
    time.sleep(0.2)
    assert host.llm_calls == []  # the other process (e.g. `alfred brain transcribe`) is linking
    other.wait(5)
    assert consolidate.wait_idle(5) and len(host.llm_calls) == 1


def test_the_pass_lock_is_released_even_when_unlocking_fails(vault, monkeypatch):
    held = consolidate._PassLock(vault)
    assert held.acquire()
    real_flock = consolidate.fcntl.flock

    def broken(fh, op):
        if op == consolidate.fcntl.LOCK_UN:
            raise OSError("unlock failed")
        return real_flock(fh, op)

    monkeypatch.setattr(consolidate.fcntl, "flock", broken)
    with pytest.raises(OSError):
        held.release()
    monkeypatch.setattr(consolidate.fcntl, "flock", real_flock)
    again = consolidate._PassLock(vault)
    assert again.acquire()  # the file was closed (its lock with it) and the thread lock freed
    again.release()
    monkeypatch.setattr(vault, "ignore_in_git", lambda pattern: b"\xff".decode("utf-8"))
    assert consolidate.run(FakeHost(), vault)["chunks"] == 0  # a broken .gitignore never stops linking


def test_an_old_vault_gets_the_lock_ignored(vault):
    (vault.root / ".gitignore").write_text(".DS_Store", encoding="utf-8")
    vault.ignore_in_git(".alfred/*.lock")
    vault.ignore_in_git(".alfred/*.lock")
    assert (vault.root / ".gitignore").read_text(encoding="utf-8") == ".DS_Store\n.alfred/*.lock\n"

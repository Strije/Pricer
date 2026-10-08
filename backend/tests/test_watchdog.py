"""Сторож сервера (deploy/watchdog.py): запуск остановленной базы, сообщения о нехватке памяти."""
import importlib.util
import os
import subprocess

import pytest

PATH = os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "watchdog.py")


@pytest.fixture
def watchdog():
    spec = importlib.util.spec_from_file_location("watchdog", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_run(answers, calls):
    def run(args, timeout=60):
        calls.append(args)
        for prefix, out in answers:
            if args[:len(prefix)] == prefix:
                value = out(calls) if callable(out) else out
                return subprocess.CompletedProcess(args, value[0], value[1], value[2] if len(value) > 2 else "")
        return None
    return run


def test_db_down_is_started_and_reason_reported(watchdog, monkeypatch, tmp_path):
    log = tmp_path / "pg.log"
    log.write_text("2026-10-08 LOG:  checkpoint\n2026-10-08 FATAL:  could not write: No space left on device\n"
                   "2026-10-08 ERROR:  password authentication failed for user secret\n", encoding="utf-8")
    calls = []
    started = lambda c: (0, "18 main 5432 online postgres /d " + str(log)) if ["pg_ctlcluster", "18", "main", "start"] in c \
        else (0, "18 main 5432 down postgres /d " + str(log))  # noqa: E731
    monkeypatch.setattr(watchdog, "_run", fake_run([(["pg_lsclusters"], started), (["pg_ctlcluster"], (0, ""))], calls))
    events = []
    assert watchdog.check_db(events) is None
    assert ["pg_ctlcluster", "18", "main", "start"] in calls
    assert "запустил её снова" in events[0] and "No space left" in events[0] and "password" not in events[0]


def test_db_does_not_start(watchdog, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(watchdog, "_run", fake_run([
        (["pg_lsclusters"], (0, f"18 main 5432 down postgres /d {tmp_path}/none.log")),
        (["pg_ctlcluster"], (1, "", "Error: could not start server"))], calls))
    events = []
    message = watchdog.check_db(events)
    assert "не запускается" in message and "could not start" in message and not events


def test_db_online_and_no_postgres(watchdog, monkeypatch):
    calls = []
    monkeypatch.setattr(watchdog, "_run", fake_run([(["pg_lsclusters"], (0, "18 main 5432 online postgres /d /l"))], calls))
    assert watchdog.check_db([]) is None and not any(c[0] == "pg_ctlcluster" for c in calls)
    monkeypatch.setattr(watchdog, "_run", lambda *a, **k: None)  # PostgreSQL не установлен
    assert watchdog.check_db([]) is None


def test_oom_event(watchdog, monkeypatch):
    calls = []
    monkeypatch.setattr(watchdog, "_run", fake_run([(["journalctl"], (0, "kernel: Out of memory: Killed process 812 (postgres) "
                                                                          "total-vm:300000kB\n"))], calls))
    state, events = {}, []
    watchdog.check_oom(state, events)
    assert "812 (postgres)" in events[0] and state["oom_since"]
    monkeypatch.setattr(watchdog, "_run", fake_run([(["journalctl"], (0, ""))], calls))
    since, events = state["oom_since"], []
    watchdog.check_oom(state, events)
    assert events == [] and calls[-1][-1] == f"@{since}"  # следующая проверка — с момента прошлой

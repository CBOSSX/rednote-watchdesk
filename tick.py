#!/usr/bin/env python3
"""Dispatch due checks and record health without browser dependencies."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import sys
from paths import RUNTIME, PROJECT, HEALTH as PRIVATE_HEALTH
from watch_config import normalize, needs_reconcile
from retry_policy import query_due, follow_due

ROOT = RUNTIME
CODE_ROOT = PROJECT
DATA = ROOT / "data"
HEALTH = PRIVATE_HEALTH


def mirror(name, value):
    HEALTH.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = HEALTH / name
    tmp = p.with_name(p.name + ".tmp." + str(os.getpid()))
    tmp.write_text(json.dumps(value))
    tmp.chmod(0o600)
    tmp.replace(p)


def heartbeat(status, **extra):
    p = DATA / "heartbeat.json"
    try:
        previous = json.loads(p.read_text())
    except (ValueError, OSError):
        previous = {}
    fault = previous.get("worker_fault")
    if status == "fatal":
        fault = extra.get("reason", "unknown")
    elif status in ("ok", "handled_error"):
        fault = None
    tmp = p.with_suffix(".tmp")
    value = {"status": status, "updated_at": time.time(), "worker_fault": fault, **extra}
    tmp.write_text(json.dumps(value))
    tmp.chmod(0o600)
    tmp.replace(p)
    mirror("heartbeat.json", value)


def main():
    os.umask(0o077)
    DATA.mkdir(exist_ok=True, mode=0o700)
    with (DATA / "tick.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        started = time.time()
        heartbeat("starting", started_at=started)
        try:
            config = json.loads((ROOT / "config.json").read_text())
            state_path = DATA / "state.json"
            state = json.loads(state_path.read_text())
            mirror("state.json", {k: state[k] for k in ("paused", "completed", "query_complete", "follow_pending", "configured_targets", "last_attempt_epoch", "last_status", "last_checked_at", "resumed_at") if k in state})
            plan = normalize(config)
            changed = needs_reconcile(config, state)
            now = time.time()
            interval = config.get("interval_seconds", 1800)
            follow_work = False
            if state.get("targets"):
                names = [t for p in plan['people'] for t in p['candidates']]
                entries = [state["targets"].get(t, {}) for t in names]
                due = any(query_due(e, now, interval) for e in entries)
                follow_work = any(follow_due(state['targets'].get(t, {}), now, state.get('auto_follow_enabled', config.get('auto_follow_enabled')), state.get('follow_paused'))
                                  for t in state.get('auto_follow_targets', config.get('auto_follow_targets', [])) if t in names)
            else:
                due = query_due(state, now, interval)
            if not changed and not state.get("pending") and (state.get("paused") or state.get("completed") or (state.get("query_complete") and not follow_work) or not (due or follow_work)):
                heartbeat("idle")
                return
            timeout = 480 if any(p['auto_follow'] for p in plan['people']) else 240
            heartbeat("checking", started_at=started, timeout_seconds=timeout)
            child = subprocess.Popen([sys.executable, str(CODE_ROOT / "watch.py"), "check"], start_new_session=True)
            try:
                code = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                heartbeat("fatal", reason="worker_timeout")
                return
            if code == 0:
                state = json.loads(state_path.read_text())
                mirror("state.json", {k: state[k] for k in ("paused", "completed", "query_complete", "follow_pending", "configured_targets", "last_attempt_epoch", "last_status", "last_checked_at", "resumed_at") if k in state})
                heartbeat("ok")
                return
            state = json.loads(state_path.read_text())
            mirror("state.json", {k: state[k] for k in ("paused", "completed", "query_complete", "follow_pending", "configured_targets", "last_attempt_epoch", "last_status", "last_checked_at", "resumed_at") if k in state})
            checked = datetime.datetime.fromisoformat(state.get("last_checked_at", "1970-01-01T00:00:00+00:00")).timestamp()
            handled = code == 2 and checked >= started - 2 and state.get("last_status") in ("error", "partial_error", "paused")
            heartbeat("handled_error" if handled else "fatal", reason="worker_exit_" + str(code))
        except Exception as error:
            heartbeat("fatal", reason="scheduler_" + type(error).__name__)


if __name__ == "__main__":
    main()

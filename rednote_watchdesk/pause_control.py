"""Accept pause requests without contending with browser work or losing results."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import time
import uuid


def read_request(data):
    try:
        return json.loads((Path(data)/'pause-request.json').read_text())
    except FileNotFoundError:
        return {}


def token(data):
    return read_request(data).get('id', '')


def apply(data, state):
    if read_request(data):
        state['paused'] = True
    return state


@contextmanager
def control_lock(data):
    with (Path(data)/'pause-control.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write(path, value):
    tmp = path.with_name(path.name + '.tmp.' + uuid.uuid4().hex)
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    tmp.replace(path)


def request_pause(data):
    data = Path(data)
    request = {'id': uuid.uuid4().hex, 'requested_at': time.time()}
    with control_lock(data):
        _write(data/'pause-request.json', request)
    # If idle, persist immediately. An active worker merges this request on save.
    with (data/'watch.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return request
        with control_lock(data):
            if read_request(data):
                path = data/'state.json'
                state = json.loads(path.read_text()) if path.exists() else {}
                _write(path, apply(data, state))
    return request


def clear(data):
    """Caller holds the worker lock and control_lock while resuming."""
    (Path(data)/'pause-request.json').unlink(missing_ok=True)

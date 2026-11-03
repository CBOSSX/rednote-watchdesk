"""Shared scheduling rules; no browser or third-party dependencies."""


def retry_delay(errors):
    return min(1800, 300 * 2 ** min(max(errors - 1, 0), 3))


def query_due(entry, current, interval):
    if entry.get('paused') or entry.get('completed') or entry.get('found') or entry.get('suppressed_by'):
        return False
    deadline = entry.get('next_retry_epoch')
    if deadline is None:
        deadline = entry.get('last_attempt_epoch', 0) + interval
    return current >= deadline


def follow_due(entry, current, enabled, paused=False):
    job = entry.get('follow', {})
    return bool(enabled and not paused and entry.get('found')
                and job.get('status') in ('pending', 'running', 'retry')
                and current >= job.get('next_attempt_epoch', 0))

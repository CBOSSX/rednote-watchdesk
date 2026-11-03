"""Per-target, per-actor follow state, including non-destructive legacy migration."""
from copy import deepcopy
from rednote_watchdesk.accounts import LEGACY_FOLLOW
from rednote_watchdesk.retry_policy import follow_due


def selected_accounts(state, target):
    entry = state['targets'][target]
    person = state.get('people', {}).get(entry.get('person_id'), {})
    return person.get('follow_account_ids', []) if not entry.get('suppressed_by') else []


def sync_multi_follows(state):
    if not state.get('multi_follow_initialized'):
        if state.get('follow_paused'):
            state.setdefault('follow_paused_accounts', {})[LEGACY_FOLLOW] = state.get('follow_pause_reason', 'login_required')
        state['multi_follow_initialized'] = True
    paused = state.setdefault('follow_paused_accounts', {})
    pending = False
    # Removed targets can still have queued legacy notifications to acknowledge.
    for entry in state['targets'].values():
        if 'follow' in entry:
            entry.setdefault('follows', {}).setdefault(LEGACY_FOLLOW, deepcopy(entry['follow']))
    for target in state['configured_targets']:
        entry = state['targets'][target]
        jobs = entry.setdefault('follows', {})
        if not entry.get('found'):
            continue
        if 'discovery_notified' not in entry:
            entry['discovery_notified'] = bool(entry.get('completed'))
        selected = selected_accounts(state, target)
        for aid in selected:
            jobs.setdefault(aid, {'status': 'pending'})
            if aid not in paused and jobs[aid].get('status') in ('pending', 'running', 'retry'):
                pending = True
        entry['completed'] = bool(entry.get('discovery_notified') and all(
            jobs[aid].get('status') in ('succeeded', 'failed', 'blocked') and jobs[aid].get('result_notified') for aid in selected))
    state['follow_pending'] = pending
    state['follow_paused'] = bool(paused)


def multi_follow_due(state, current):
    paused = state.get('follow_paused_accounts', {})
    return any(follow_due({'found': entry.get('found'), 'follow': entry.get('follows', {}).get(aid, {})}, current, True, aid in paused)
               for target in state.get('auto_follow_targets', [])
               for entry in [state['targets'].get(target, {})]
               for aid in selected_accounts(state, target))


def resume_account(state, account_id):
    state.setdefault('follow_paused_accounts', {}).pop(account_id, None)
    for entry in state['targets'].values():
        job = entry.get('follows', {}).get(account_id, {})
        if job.get('status') in ('failed', 'blocked'):
            job.update(status='pending', attempts=0, result_notified=False)
            job.pop('next_attempt_epoch', None)
            job.pop('failure_notified', None)
            # Keep click_started: recovery after an uncertain write only verifies.
    sync_multi_follows(state)

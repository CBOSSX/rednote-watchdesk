"""Execute independent follow jobs without sharing retry or identity state."""
import time
from rednote_watchdesk.accounts import require_account
from rednote_watchdesk.follow import FollowProblem
from rednote_watchdesk.follow_state import selected_accounts
from rednote_watchdesk.retry_policy import follow_due, retry_delay


def run(state, config, *, data, enqueue, save, gate, site_paused, driver, cancelled=lambda: False):
    attempted = 0
    paused = state.setdefault('follow_paused_accounts', {})
    for target in state.get('auto_follow_targets', []):
        entry = state['targets'][target]
        if not entry.get('found'):
            continue
        for aid in selected_accounts(state, target):
            if cancelled():
                return
            job = entry['follows'][aid]
            if attempted >= 2:
                return  # Remaining actors continue on the next scheduler tick.
            if not follow_due({'found': entry.get('found'), 'follow': job}, time.time(), True, aid in paused):
                continue
            attempted += 1
            account = None
            job.update(status='running', attempts=job.get('attempts', 0) + 1)
            save(state)

            def checkpoint():
                job['click_started'] = True
                save(state)

            def event(kind, reason=None):
                return {'kind': kind, 'target': target, 'account_id': aid, 'result': entry['found'],
                        'actor_name': (account or {}).get('name', aid), 'reason': reason,
                        'follow_status': job['status'], 'click_started': job.get('click_started', False),
                        'text': ('自动关注成功：' if kind == 'follow_success' else '自动关注未成功：') + target}

            try:
                account = require_account(data, aid)
                if not account.get('name') or not account.get('profile_url'):
                    raise FollowProblem('follow_account_not_ready', blocked=True)
                if job.get('actor_profile_url', account['profile_url']) != account['profile_url']:
                    raise FollowProblem('follow_account_mismatch', blocked=True)
                job['actor_profile_url'] = account['profile_url']
                save(state)
                result = driver(data, account, entry['found'], target, state['auto_follow_targets'],
                                checkpoint, gate, verify_only=job.get('click_started', False), account_id=aid)
            except Exception as error:
                reason = str(error) if isinstance(error, (FollowProblem, site_paused)) else type(error).__name__
                blocked = isinstance(error, site_paused) or getattr(error, 'blocked', False)
                retryable = not isinstance(error, (FollowProblem, site_paused)) or getattr(error, 'retryable', False)
                retry = not blocked and retryable and job['attempts'] < 3
                previous_failure = job.get('failure_notified', False)
                job.update(status='blocked' if blocked else 'retry' if retry else 'failed', reason=reason, result_notified=False)
                if retry:
                    job['next_attempt_epoch'] = time.time() + retry_delay(job['attempts'])
                if blocked:
                    paused[aid] = reason
                if not previous_failure or not retry:
                    job['failure_notified'] = True
                    enqueue(state, event('follow_failure', reason))
            else:
                job.update(status='succeeded', result=result, result_notified=False)
                job.pop('reason', None)
                notice = event('follow_success')
                notice['already_followed'] = result['status'] == 'already_followed'
                enqueue(state, notice)
            save(state)

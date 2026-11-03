"""Durable follow tasks; notification retries never repeat a successful click."""
import json
import logging
import time
from rednote_watchdesk.follow import FollowProblem, follow_once
from rednote_watchdesk.retry_policy import follow_due, retry_delay
LOG = logging.getLogger('xhs-watch')


def run_follow_jobs(state, config, *, data, enqueue, save, gate, site_paused):
    if state.get('multi_account'):
        from rednote_watchdesk.multi_follow import run
        return run(state, config, data=data, enqueue=enqueue, save=save, gate=gate, site_paused=site_paused, driver=follow_once)
    names = [t for t in state.get('auto_follow_targets', []) if follow_due(
        state['targets'][t], time.time(), state.get('auto_follow_enabled'), state.get('follow_paused'))]
    if not names:
        return
    try:
        account = json.loads((data/'follow-account.json').read_text())
        if not isinstance(account, dict) or not account.get('profile_url') or not account.get('name'):
            raise ValueError('missing identity')
    except (OSError, ValueError):
        account = None

    def event(target, kind, reason=None):
        entry = state['targets'][target]
        return {'kind': kind, 'target': target, 'result': entry['found'],
                'actor_name': account['name'] if account else '已登录的大号',
                'reason': reason, 'follow_status': entry['follow']['status'],
                'click_started': entry['follow'].get('click_started', False),
                'text': ('自动关注成功：' if kind == 'follow_success' else '自动关注未成功：') + target}

    for target in names:
        entry = state['targets'][target]
        job = entry['follow']
        if state.get('follow_paused'):
            job.update(status='blocked', reason=state['follow_pause_reason'], result_notified=False)
            enqueue(state, event(target, 'follow_failure', job['reason']))
            save(state)
            continue
        job.update(status='running', attempts=job.get('attempts', 0) + 1)
        LOG.info('follow_attempt target=%s attempt=%s verify_only=%s', target, job['attempts'], job.get('click_started', False))
        save(state)

        def checkpoint():
            job['click_started'] = True
            save(state)

        try:
            if account is None:
                raise FollowProblem('follow_account_not_ready', blocked=True)
            result = follow_once(data, account, entry['found'], target, state['auto_follow_targets'],
                                 checkpoint, gate, verify_only=job.get('click_started', False))
        except Exception as error:
            reason = str(error) if isinstance(error, (FollowProblem, site_paused)) else type(error).__name__
            blocked = isinstance(error, site_paused) or getattr(error, 'blocked', False)
            retryable = not isinstance(error, (FollowProblem, site_paused)) or getattr(error, 'retryable', False)
            retry = not blocked and retryable and job['attempts'] < 3
            status = 'blocked' if blocked else 'retry' if retry else 'failed'
            previous_failure = job.get('failure_notified', False)
            job.update(status=status, reason=reason, result_notified=False)
            LOG.warning('follow_error target=%s status=%s reason=%s', target, status, reason)
            if retry:
                job['next_attempt_epoch'] = time.time() + retry_delay(job['attempts'])
            if blocked:
                state.update(follow_paused=True, follow_pause_reason=reason)
            if not previous_failure or not retry:
                job['failure_notified'] = True
                enqueue(state, event(target, 'follow_failure', reason))
        else:
            job.update(status='succeeded', result=result, result_notified=False)
            LOG.info('follow_success target=%s status=%s', target, result['status'])
            job.pop('reason', None)
            notice = event(target, 'follow_success')
            notice['already_followed'] = result['status'] == 'already_followed'
            enqueue(state, notice)
        save(state)

"""Persist one winning candidate per person while retaining per-account history."""
from rednote_watchdesk.watch_config import normalize, signature

def apply_people(state, config):
    plan = normalize(config)
    state['multi_account'] = 'search_account_ids' in plan
    search_ids = plan.get('search_account_ids')
    if search_ids != state.get('search', {}).get('account_ids'):
        state['search'] = {'account_ids': search_ids}
    targets = state.setdefault('targets', {})
    old_people = state.get('people', {})
    people = {}
    for person in plan['people']:
        pid = person['id']
        candidates = person['candidates']
        for target in candidates:
            if target not in targets:
                previous = next((e for t,e in targets.items() if t.casefold()==target.casefold()), None)
                targets[target] = dict(previous) if previous is not None else {}
            targets[target]['person_id'] = pid
            targets[target].pop('suppressed_by', None)
        old = old_people.get(pid, {})
        winner = old.get('matched_target')
        if winner:
            winner = next((t for t in candidates if t.casefold()==winner.casefold()), None)
            if winner is None:
                raise ValueError(person['name']+' 已命中 '+old['matched_target']+'；请保留该候选，或新建监控任务')
        if not winner:
            winner = next((t for t in candidates if targets[t].get('found')), None)
        people[pid] = dict(person, matched_target=winner)
    state.update(people=people, configured_people=list(people), config_signature=signature(config),
                 configured_targets=[t for p in plan['people'] for t in p['candidates']], interval_seconds=plan['interval_seconds'])
    sync_people(state)

def sync_people(state):
    people = state.get('people')
    if people is None:
        return
    allowed = []
    for person in people.values():
        winner = person.get('matched_target')
        if not winner:
            winner = next((t for t in person['candidates'] if state['targets'][t].get('found')), None)
            person['matched_target'] = winner
        for target in person['candidates']:
            entry = state['targets'][target]
            if winner and target != winner:
                entry['suppressed_by'] = winner
            else:
                entry.pop('suppressed_by', None)
                if person['auto_follow']:
                    allowed.append(target)
        person['query_complete'] = bool(winner)
    state['auto_follow_enabled'] = any(p['auto_follow'] for p in people.values())
    state['auto_follow_targets'] = allowed

def complete_people(state):
    for person in state['people'].values():
        winner = person.get('matched_target')
        person['completed'] = bool(winner and state['targets'][winner].get('completed'))
    state['query_complete'] = all(p['query_complete'] for p in state['people'].values())
    state['completed'] = all(p['completed'] for p in state['people'].values())

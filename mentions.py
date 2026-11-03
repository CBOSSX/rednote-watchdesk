"""Render configured Feishu mentions at send time, including queued retries."""
from html import escape
import re


def mention_tags(config, card=False):
    users = config.get('mention_users', [])
    if not isinstance(users, list):
        raise ValueError('mention_users must be a list')
    tags, seen = [], set()
    for user in users:
        user_id, name = user.get('user_id', ''), user.get('name', '')
        if not isinstance(user_id, str) or user_id == 'all' or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', user_id):
            raise ValueError('Invalid mention user ID')
        if not isinstance(name, str) or not name.strip():
            raise ValueError('Missing mention name')
        if user_id not in seen:
            tags.append(('<at id=' + user_id + '></at>') if card else
                        '<at user_id="' + escape(user_id, quote=True) + '">' + escape(name) + '</at>')
            seen.add(user_id)
    return ' '.join(tags)


def mention_text(text, config):
    tags = mention_tags(config)
    return tags + '\n' + text if tags else text

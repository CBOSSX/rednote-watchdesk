"""Real Chrome checks against a temporary local instance; no monitor or notifications.

Run from the repository root: .venv/bin/python -m tests.ui_smoke
"""
from pathlib import Path
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from rednote_watchdesk import config_server as server, accounts


def exercise(page, root, reduced):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('response', lambda response: errors.append(response.url) if response.status >= 400 and not response.url.endswith('/favicon.ico') else None)
    page.add_init_script('''{
        const animate = Element.prototype.animate;
        window.cardAnimations = 0;
        Element.prototype.animate = function (...args) {
            if (this.matches('.person-card')) window.cardAnimations++;
            return animate.apply(this, args);
        };
    }''')
    initial_requests = []
    page.route('**/api/state', lambda route: initial_requests.append(route), times=1)
    with page.expect_request('**/api/state'):
        page.goto(root)
    stats = page.locator('.stats')
    expect(stats.locator('#people-count')).to_be_hidden()
    stats_height = stats.bounding_box()['height']
    initial_requests[0].continue_()
    expect(page.locator('.person-card')).to_have_count(3)
    expect(page.locator('.person-card:visible')).to_have_count(2)
    expect(stats.locator('#people-count')).to_be_visible()
    expect(stats.locator('#people-count')).to_have_text('3')
    assert stats.bounding_box()['height'] == stats_height
    assert page.evaluate('window.cardAnimations') == 0
    unfinished = page.locator('[data-filter="unfinished"]')
    expect(page.locator('.filter-button')).to_have_count(3)
    expect(unfinished).to_have_attribute('aria-pressed', 'true')
    expect(unfinished.locator('.filter-count')).to_have_text('2')
    page.locator('.name').first.fill('编辑中的目标')
    page.evaluate('window.firstCard = document.querySelector(".person-card")')

    # Rapid navigation must settle on the last selection, even mid-animation.
    page.evaluate('''() => {
      for (let i = 0; i < 12; i++) {
        document.querySelector('[data-tab="settings"]').click();
        document.querySelector('[data-tab="people"]').click();
      }
      document.querySelector('[data-tab="accounts"]').click();
    }''')
    expect(page.locator('main > section:visible')).to_have_count(1)
    expect(page.locator('#accounts-tab')).to_be_visible()
    if reduced:
        assert page.evaluate('document.getAnimations().length') == 0
    else:
        assert page.locator('#accounts-tab').evaluate('node => node.getAnimations().length') > 0
    expect(page.locator('#accounts-jobs')).to_be_empty()
    expect(page.locator('#settings-jobs')).to_be_empty()
    expect(page.locator('.account-card')).to_have_count(2)
    page.locator('#account-label').fill('备用账号')
    page.locator('#add-account').click()
    expect(page.locator('.account-card')).to_have_count(3)
    expect(page.locator('#account-label')).to_have_value('')
    actor_ids = [item['id'] for item in page.request.get(root + '/api/state').json()['accounts']]
    first_account = page.locator('.account-card').first
    first_account.locator('.rename-account').click()
    expect(page.locator('#rename-account-label')).to_have_value('日常账号')
    page.locator('#rename-account-label').fill('取消的备注')
    page.locator('#cancel-rename-account').click()
    expect(first_account.locator('.account-label-text')).to_have_text('日常账号')
    first_account.locator('.rename-account').click()
    page.locator('#rename-account-label').fill('我的搜索与关注账号')
    page.locator('#save-account-label').click()
    expect(page.locator('#rename-account-dialog')).not_to_be_visible()
    expect(first_account.locator('.account-label-text')).to_have_text('我的搜索与关注账号')
    expect(page.locator('#search-account-select option').nth(1)).to_contain_text('我的搜索与关注账号')
    snapshot = page.request.get(root + '/api/state').json()
    assert snapshot['accounts'][0]['id'] == actor_ids[0]
    assert snapshot['config']['people'][0]['name'] == 'Alpha'  # Rename cannot save target drafts.
    page.locator('#search-account-select').select_option(actor_ids[1])
    page.locator('#save-search-account').click()
    expect(page.locator('#save-hint')).to_have_text('已保存')
    assert page.request.get(root + '/api/state').json()['config']['search_account_id'] == actor_ids[1]
    page.locator('[data-tab="people"]').click()
    expect(page.locator('.name').first).to_have_value('编辑中的目标')

    page.locator('[data-filter="completed"]').click()
    expect(page.locator('.person-card:visible')).to_have_count(1)
    unfinished.click()
    expect(page.locator('.person-card:visible')).to_have_count(2)
    page.locator('#target-search').fill('no-such-target')
    expect(page.locator('#filter-empty')).to_be_visible()
    page.locator('#clear-filters').click()
    expect(page.locator('.person-card:visible')).to_have_count(3)

    # New cards must not replace existing editors; candidates retain keyboard flow.
    unfinished.click()
    page.locator('#add-person').click()
    expect(page.locator('.person-card')).to_have_count(4)
    expect(page.locator('.person-card:visible')).to_have_count(3)
    assert page.evaluate('window.firstCard === document.querySelector(".person-card")')
    card = page.locator('.person-card').last
    expect(card.locator('.name')).to_be_focused()
    card.locator('.name').fill('新目标')
    card.locator('.candidate-input').fill('example_delta')
    card.locator('.candidate-input').press('Enter')
    expect(card.locator('.candidate-input')).to_have_count(2)
    expect(card.locator('.candidate-input').last).to_be_focused()
    card.locator('.candidate-input').last.fill('example_echo')
    card.locator('.remove-candidate').last.click()
    expect(card.locator('.candidate-input')).to_have_count(1)
    heights = page.locator('.person-card').evaluate_all('nodes => nodes.map(n => n.getBoundingClientRect().height)')
    card.locator('.follow-picker').click()
    picker = page.locator('#follow-dialog')
    expect(picker).to_be_visible()
    expect(picker.locator('.choice-label').first).to_contain_text('我的搜索与关注账号')
    picker.locator('input').nth(0).check()
    page.keyboard.press('Escape')
    expect(picker).not_to_be_visible()
    expect(card.locator('.selected-follow-count')).to_have_text('仅通知')
    expect(card.locator('.follow-picker')).to_be_focused()
    card.locator('.follow-picker').click()
    picker.locator('input').nth(0).check()
    picker.locator('input').nth(1).check()
    assert page.locator('.person-card').evaluate_all('nodes => nodes.map(n => n.getBoundingClientRect().height)') == heights
    page.locator('#apply-follow-dialog').click()
    expect(picker).not_to_be_visible()
    expect(card.locator('.selected-follow-count')).to_have_text('2 个账号')
    card.locator('.follow-picker').click()
    expect(picker.locator('input:checked')).to_have_count(2)
    picker.locator('input').nth(0).uncheck()
    page.locator('#cancel-follow-dialog').click()
    expect(card.locator('.selected-follow-count')).to_have_text('2 个账号')

    # Saving a filtered list must include hidden drafts.
    page.locator('[data-filter="completed"]').click()
    page.locator('#save-people').click()
    expect(page.locator('#save-hint')).to_have_text('已保存')
    expect(page.locator('#toast.show')).to_have_text('配置已保存，将在下一轮调度时生效')
    expect(page.locator('#banner')).to_have_count(0)
    saved = page.request.get(root + '/api/state').json()['config']['people']
    assert len(saved) == 4 and saved[0]['name'] == '编辑中的目标'
    assert saved[-1]['candidates'] == ['example_delta'] and saved[-1]['auto_follow']
    assert saved[-1]['follow_account_ids'] == actor_ids[:2]
    page.locator('[data-filter="all"]').click()
    page.once('dialog', lambda dialog: dialog.accept())
    card.locator('.remove').click()
    expect(page.locator('.person-card')).to_have_count(3)

    page.locator('[data-tab="settings"]').click()
    page.locator('#test-notifications').click()
    expect(page.locator('#settings-jobs')).to_contain_text('测试飞书通知')
    expect(page.locator('#accounts-jobs')).to_be_empty()
    for job in server.JOBS.values():
        if job['status'] == 'running':
            job.update(status='succeeded', message='操作完成', finished_at=time.time())
    expect(page.locator('#settings-jobs')).to_contain_text('操作完成', timeout=7000)
    expect(page.locator('#settings-jobs')).to_be_empty(timeout=10000)
    expect(page.locator('#accounts-jobs')).to_be_empty()
    expect(page.locator('#toast')).to_be_hidden()
    expect(page.locator('#webhook-editor')).to_be_hidden()
    page.locator('#replace-webhook').click()
    expect(page.locator('#webhook')).to_be_focused()
    page.locator('#webhook').fill('unsaved draft')
    page.locator('#replace-webhook').click()
    expect(page.locator('#webhook-editor')).to_be_hidden()
    page.locator('#replace-webhook').click()
    expect(page.locator('#webhook')).to_have_value('')
    # Switching reduced motion on during animation cancels active effects.
    page.emulate_media(reduced_motion='reduce')
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    assert page.evaluate('document.getAnimations().length') == 0
    page.set_viewport_size({'width': 390, 'height': 844})
    for tab in ['people', 'accounts', 'settings']:
        page.locator(f'[data-tab="{tab}"]').click()
        expect(page.locator(f'#{tab}-tab')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), tab
    page.locator('[data-tab="people"]').click()
    mobile_card = page.locator('.person-card').first
    mobile_height = mobile_card.bounding_box()['height']
    mobile_card.locator('.follow-picker').click()
    expect(page.locator('#follow-dialog')).to_be_visible()
    assert mobile_card.bounding_box()['height'] == mobile_height
    box = page.locator('#follow-dialog').bounding_box()
    assert 0 <= box['x'] and box['x'] + box['width'] <= 390
    assert 0 <= box['y'] and box['y'] + box['height'] <= 844
    page.locator('#cancel-follow-dialog').click()
    assert not errors, errors


def fake_start(command, **kwargs):
    jid = uuid.uuid4().hex
    server.JOBS[jid] = {'id': jid, 'scope': kwargs.get('scope', 'people'), 'title': kwargs.get('title', command), 'status': 'running', 'started_at': time.time()}
    return jid


def main():
    with tempfile.TemporaryDirectory(prefix='watchdesk-ui-') as directory, \
            patch.object(server, 'RUNTIME', Path(directory)), patch.object(server, 'wake'), patch.object(server, 'start_job', side_effect=fake_start):
        server.ensure_home()
        config = {'interval_seconds': 1800, 'people': [
            {'id': name, 'name': name.title(), 'candidates': ['example_' + name], 'auto_follow': False}
            for name in ['alpha', 'beta', 'gamma']
        ]}
        server.write(server.RUNTIME / 'secrets.json', {'webhook_url': 'https://open.feishu.cn/open-apis/bot/v2/hook/TEST_ONLY'})
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='chrome', headless=True)
                try:
                    for reduced in [False, True]:
                        server.write(server.RUNTIME / 'data/accounts.json', {'accounts': []})
                        server.JOBS.clear()
                        server.JOBS['historical'] = {'id': 'historical', 'scope': 'settings', 'title': '测试飞书通知', 'status': 'succeeded', 'message': '操作完成', 'finished_at': time.time() - 3600}
                        actor_ids = []
                        for name, digit in [('日常账号', '1'), ('工作账号', '2')]:
                            aid = accounts.create_account(server.RUNTIME/'data', name)['id']
                            actor_ids.append(aid)
                            accounts.write(accounts.account_paths(server.RUNTIME/'data', aid)[0], {'name': name, 'red_id': 'demo_' + digit, 'profile_url': 'https://www.xiaohongshu.com/user/profile/' + digit*24})
                        config['search_account_id'] = actor_ids[0]
                        for person in config['people']:
                            person['follow_account_ids'] = []
                        server.write(server.RUNTIME / 'config.json', config)
                        server.write(server.RUNTIME / 'data/state.json', {
                            'people': {'beta': {'matched_target': 'example_beta', 'completed': True}},
                            'targets': {'example_beta': {'completed': True}}
                        })
                        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce' if reduced else 'no-preference')
                        try:
                            exercise(context.new_page(), f'http://127.0.0.1:{http.server_port}', reduced)
                        finally:
                            context.close()
                        print('UI checks passed:', 'reduced motion' if reduced else 'normal motion')
                finally:
                    browser.close()
        finally:
            http.shutdown()
            http.server_close()
            thread.join()


if __name__ == '__main__':
    main()

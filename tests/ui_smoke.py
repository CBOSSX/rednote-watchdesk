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
from rednote_watchdesk import watch
from rednote_watchdesk.query_diagnostics import public_error


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
    progress_panel = page.locator('#query-progress')
    expect(progress_panel).to_be_visible()
    expect(page.locator('#query-progress-title')).to_have_text('查询进度 · 等待首次查询')
    initial_progress_height = progress_panel.bounding_box()['height']
    expect(stats.locator('#people-count')).to_be_hidden()
    stats_height = stats.bounding_box()['height']
    initial_requests[0].continue_()
    expect(page.locator('.person-card')).to_have_count(3)
    expect(page.locator('.person-card:visible')).to_have_count(2)
    expect(stats.locator('#people-count')).to_be_visible()
    expect(stats.locator('#people-count')).to_have_text('3')
    expect(page.locator('.brand-icon')).to_have_attribute('src', '/favicon.svg')
    assert page.locator('.brand-icon').evaluate('img => img.complete && img.naturalWidth > 0')
    assert page.locator('.brand-icon').get_attribute('src') == page.locator('link[rel="icon"]').get_attribute('href')
    assert stats.bounding_box()['height'] == stats_height
    assert progress_panel.bounding_box()['height'] == initial_progress_height
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
    expect(page.locator('main > section:visible').filter(has_not=page.locator('#query-progress-title'))).to_have_count(1)
    expect(page.locator('#accounts-tab')).to_be_visible()
    if reduced:
        assert page.evaluate('document.getAnimations().length') == 0
    else:
        assert page.locator('#accounts-tab').evaluate('node => node.getAnimations().length') > 0
    banners = page.locator('.jobs, .job-feedback, #banner')
    expect(banners).to_have_count(0)
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
    search_order = page.locator('#search-account-order')
    expect(search_order.locator('strong')).to_have_text(['1 · 我的搜索与关注账号'])
    snapshot = page.request.get(root + '/api/state').json()
    assert snapshot['accounts'][0]['id'] == actor_ids[0]
    assert snapshot['config']['people'][0]['name'] == 'Alpha'  # Rename cannot save target drafts.
    add_search = page.locator('#add-search-account')
    search_picker = page.locator('#search-account-dialog')
    confirm_search = search_picker.get_by_role('button', name='确认')
    work_search = search_picker.get_by_role('checkbox', name='工作账号', exact=False)
    backup_search = search_picker.get_by_role('checkbox', name='备用账号', exact=False)
    expect(page.locator('#accounts-tab select')).to_have_count(0)
    add_search.click()
    expect(confirm_search).to_be_disabled()
    work_search.check()
    expect(confirm_search).to_be_enabled()
    expect(search_order.locator('li')).to_have_count(1)
    page.keyboard.press('Escape')
    expect(search_picker).not_to_be_visible()
    expect(add_search).to_be_focused()
    expect(search_order.locator('li')).to_have_count(1)
    add_search.click()
    expect(work_search).not_to_be_checked()
    expect(search_picker.get_by_role('checkbox')).to_have_count(2)
    backup_search.check()
    work_search.press('Space')
    expect(backup_search).to_be_checked()
    expect(work_search).to_be_checked()
    expect(search_order.locator('li')).to_have_count(1)
    confirm_search.press('Enter')
    expect(search_picker).not_to_be_visible()
    expect(search_order.locator('strong')).to_have_text(['1 · 我的搜索与关注账号', '2 · 工作账号', '3 · 备用账号'])
    search_order.get_by_role('button', name='上移 工作账号').click()
    add_search.click()
    expect(search_picker).to_contain_text('所有账号都已加入搜索顺序')
    expect(confirm_search).to_be_disabled()
    page.locator('#cancel-search-account-dialog').click()
    expect(search_order.locator('strong')).to_have_text(['1 · 工作账号', '2 · 我的搜索与关注账号', '3 · 备用账号'])
    page.locator('#save-search-account').click()
    expect(page.locator('#save-hint')).to_have_text('已保存')
    saved_search_order = [actor_ids[1], actor_ids[0], actor_ids[2]]
    assert page.request.get(root + '/api/state').json()['config']['search_account_ids'] == saved_search_order
    state_path = server.RUNTIME / 'data/state.json'
    state = server.read(state_path)
    state['search'] = {'account_ids': saved_search_order, 'active_account_id': actor_ids[0],
                       'errors': {actor_ids[1]: 'login_required'}}
    server.write(state_path, state)
    expect(search_order.locator('small')).to_have_text(['登录已失效', '当前使用', '待登录'], timeout=7000)
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
    expect(banners).to_have_count(0)
    saved = page.request.get(root + '/api/state').json()['config']['people']
    assert len(saved) == 4 and saved[0]['name'] == '编辑中的目标'
    assert saved[-1]['candidates'] == ['example_delta'] and saved[-1]['auto_follow']
    assert saved[-1]['follow_account_ids'] == actor_ids[:2]
    page.locator('[data-filter="all"]').click()
    # Deletion stays inside the page; dismissing it must preserve the draft.
    native_dialogs = []
    def dismiss_native_dialog(dialog):
        native_dialogs.append(dialog.type)
        dialog.dismiss()
    page.on('dialog', dismiss_native_dialog)
    delete_dialog = page.get_by_role('dialog', name='删除监控目标')
    card.locator('.remove').click()
    expect(delete_dialog).to_be_visible()
    expect(delete_dialog).to_contain_text('新目标')
    expect(delete_dialog).to_contain_text('现有关注关系将保留')
    expect(delete_dialog.get_by_role('button', name='取消')).to_be_focused()
    page.keyboard.press('Escape')
    expect(delete_dialog).not_to_be_visible()
    expect(card.locator('.remove')).to_be_focused()
    expect(page.locator('.person-card')).to_have_count(4)
    expect(page.locator('#save-hint')).to_have_text('已保存')
    card.locator('.remove').click()
    delete_dialog.get_by_role('button', name='取消').click()
    expect(page.locator('.person-card')).to_have_count(4)
    card.locator('.remove').click()
    delete_dialog.get_by_role('button', name='关闭确认弹窗').click()
    expect(page.locator('.person-card')).to_have_count(4)
    card.locator('.remove').click()
    delete_dialog.get_by_role('button', name='删除目标', exact=True).click()
    expect(delete_dialog).not_to_be_visible()
    expect(page.locator('.person-card')).to_have_count(3)
    expect(page.locator('#people-count')).to_have_text('3')
    expect(page.locator('.person-card').last.locator('.name')).to_be_focused()
    expect(page.locator('#save-hint')).to_have_text('有未保存的修改')
    assert len(page.request.get(root + '/api/state').json()['config']['people']) == 4
    page.locator('#save-people').click()
    expect(page.locator('#save-hint')).to_have_text('已保存')
    assert len(page.request.get(root + '/api/state').json()['config']['people']) == 3
    assert not native_dialogs, native_dialogs

    page.locator('[data-tab="settings"]').click()
    settings_panel = page.locator('#settings-tab .panel').first
    top_in_section = 'panel => panel.getBoundingClientRect().top - panel.parentElement.getBoundingClientRect().top'
    panel_top = settings_panel.evaluate(top_in_section)
    page.locator('#test-notifications').click()
    expect(page.locator('#toast.show')).to_have_text('操作已提交')
    expect(banners).to_have_count(0)
    assert settings_panel.evaluate(top_in_section) == panel_top
    for job in server.JOBS.values():
        if job['status'] == 'running':
            job.update(status='succeeded', message='操作完成', finished_at=time.time())
    expect(page.locator('#toast.show')).to_have_text('测试飞书通知 · 操作完成', timeout=7000)
    expect(banners).to_have_count(0)
    expect(page.locator('#toast')).to_be_hidden(timeout=8000)
    expect(page.locator('#webhook-editor')).to_be_hidden()
    page.locator('#replace-webhook').click()
    expect(page.locator('#webhook')).to_be_focused()
    page.locator('#webhook').fill('unsaved draft')
    page.locator('#replace-webhook').click()
    expect(page.locator('#webhook-editor')).to_be_hidden()
    page.locator('#replace-webhook').click()
    expect(page.locator('#webhook')).to_have_value('')

    # All clearing variants share the page dialog and only submit on confirmation.
    clearing_requests = []
    page.on('request', lambda request: clearing_requests.append(request.post_data_json)
            if request.url.endswith('/api/notifications/clear') else None)
    confirmation = page.locator('#confirmation-dialog')
    page.locator('#clear-webhook').click()
    expect(confirmation).to_have_accessible_name('清除 Webhook 地址')
    expect(confirmation).to_contain_text('待发送消息会保留')
    expect(confirmation.get_by_role('button', name='取消')).to_be_focused()
    expect(page.locator('#confirmation-hint')).to_be_hidden()
    page.keyboard.press('Escape')
    expect(page.locator('#clear-webhook')).to_be_focused()
    expect(page.locator('#webhook-status')).to_have_text('已配置')
    assert not clearing_requests
    for button, title, fields in [
        ('#clear-webhook', '清除 Webhook 地址', ['webhook_url']),
        ('#clear-signing', '清除签名密钥', ['signing_secret']),
    ]:
        page.locator(button).click()
        expect(confirmation).to_have_accessible_name(title)
        with page.expect_response('**/api/notifications/clear') as response:
            confirmation.get_by_role('button', name='清除配置').click()
        assert response.value.ok
        expect(page.locator('#toast.show')).to_have_text('已清除所选配置')
        assert clearing_requests[-1] == {'fields': fields}
    snapshot = page.request.get(root + '/api/state').json()['notifications']
    assert not snapshot['configured'] and not snapshot['signing_configured']

    page.locator('#webhook').fill('https://open.feishu.cn/open-apis/bot/v2/hook/TEST_ONLY')
    page.locator('#signing').fill('TEST_SIGNING_ONLY')
    page.locator('#mentions').fill('测试提醒,ou_test')
    page.locator('#save-notifications').click()
    expect(page.locator('#webhook-status')).to_have_text('已配置')
    expect(page.locator('#signing-status')).to_have_text('已配置')
    page.locator('#clear-notifications').click()
    expect(confirmation).to_have_accessible_name('清除通知配置')
    confirmation.get_by_role('button', name='取消').click()
    assert len(clearing_requests) == 2
    page.locator('#clear-notifications').click()
    confirmation.get_by_role('button', name='清除配置').click()
    expect(page.locator('#toast.show')).to_have_text('通知配置已清除')
    assert clearing_requests[-1] == {'fields': ['webhook_url', 'signing_secret', 'mention_users']}
    snapshot = page.request.get(root + '/api/state').json()['notifications']
    assert not snapshot['configured'] and not snapshot['signing_configured'] and not snapshot['mention_users']

    page.locator('[data-tab="accounts"]').click()
    login_requests = []
    page.on('request', lambda request: login_requests.append(request.post_data_json)
            if request.url.endswith('/api/login') else None)
    first_account.locator('.fresh').click()
    expect(confirmation).to_have_accessible_name('重新扫码登录')
    expect(confirmation).to_contain_text('请登录同一个身份')
    expect(confirmation.get_by_role('button', name='取消')).to_be_focused()
    page.keyboard.press('Escape')
    expect(first_account.locator('.fresh')).to_be_focused()
    assert not login_requests
    first_account.locator('.fresh').click()
    with page.expect_response('**/api/login') as response:
        confirmation.get_by_role('button', name='重新扫码', exact=True).click()
    assert response.value.ok
    expect(confirmation).not_to_be_visible()
    assert login_requests == [{'account_id': actor_ids[0], 'fresh': True}]
    # Switching reduced motion on during animation cancels active effects.
    page.emulate_media(reduced_motion='reduce')
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    assert page.evaluate('document.getAnimations().length') == 0
    page.set_viewport_size({'width': 390, 'height': 844})
    for tab in ['people', 'accounts', 'settings']:
        page.locator(f'[data-tab="{tab}"]').click()
        expect(page.locator(f'#{tab}-tab')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), tab
    page.locator('[data-tab="accounts"]').click()
    search_order.get_by_role('button', name='移除 备用账号').click()
    add_search.click()
    expect(search_picker).to_be_visible()
    expect(backup_search).to_be_visible()
    search_box = search_picker.bounding_box()
    assert 0 <= search_box['x'] and search_box['x'] + search_box['width'] <= 390
    page.keyboard.press('Escape')
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
    mobile_card.locator('.remove').click()
    expect(delete_dialog).to_be_visible()
    box = delete_dialog.bounding_box()
    assert 0 <= box['x'] and box['x'] + box['width'] <= 390
    assert 0 <= box['y'] and box['y'] + box['height'] <= 844
    page.keyboard.press('Escape')
    expect(mobile_card.locator('.remove')).to_be_focused()
    # Query errors stay visible while paused and disappear after recovery.
    snapshot = server.payload()
    person = snapshot['state']['people'][0]
    person.update(matched_target=None, completed=False, follow_status=None, follow_jobs=[])
    diagnostic = public_error({'stage': 'homepage_access', 'type': 'TimeoutError'})
    person['query_errors'] = [dict(diagnostic, target='example_alpha', consecutive_errors=3,
                                   next_retry_epoch=time.time() + 1200)]
    snapshot['state'].update(paused=False, search_account_errors={actor_ids[0]: 'TimeoutError'},
                             search_account_error_details={actor_ids[0]: diagnostic})
    # The interaction checks above intentionally leave unsaved drafts open.
    page = page.context.new_page()
    page.set_viewport_size({'width': 390, 'height': 844})
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', dismiss_native_dialog)
    page.route('**/api/state', lambda route: route.fulfill(json=snapshot))
    page.goto(root)
    search_order = page.locator('#search-account-order')
    error_card = page.locator('.person-card').filter(has=page.locator('.query-error'))
    expect(error_card.locator('.person-status')).to_have_text('查询异常')
    expect(error_card).to_contain_text('读取首页正文超时')
    expect(error_card).to_contain_text('连续失败 3 次')
    expect(error_card).to_contain_text('下次重试')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('[data-tab="accounts"]').click()
    expect(search_order).to_contain_text('读取首页正文超时')
    snapshot['state']['paused'] = True
    page.reload()
    expect(error_card.locator('.person-status')).to_have_text('已暂停')
    expect(error_card).to_contain_text('监测已暂停，恢复后再重试')
    snapshot['state']['paused'] = False
    person['query_errors'] = []
    snapshot['state']['search_account_errors'] = {}
    snapshot['state']['search_account_error_details'] = {}
    page.reload()
    expect(page.locator('.query-error')).to_have_count(0)
    # One manual request runs while paused and never toggles automatic polling.
    snapshot['jobs'] = []
    snapshot['state']['paused'] = False
    page.reload()
    manual_requests = []
    def manual_control(route):
        manual_requests.append(route.request.post_data_json)
        if route.request.post_data_json['action'] == 'pause':
            snapshot['state']['paused'] = True
            route.fulfill(json={'ok': True, 'message': '已暂停后续调度，正在执行的步骤将安全收尾'})
            return
        snapshot['jobs'] = [{'id': 'manual-once', 'title': '立即查询一次', 'scope': 'people', 'status': 'running', 'started_at': time.time()}]
        route.fulfill(status=202, json={'ok': True, 'job_id': 'manual-once'})
    page.route('**/api/control', manual_control)
    manual_button = page.locator('#check-now')
    expect(manual_button).to_be_enabled()
    page.locator('.person-card .name').first.fill('Unsaved draft')
    manual_button.click()
    expect(page.locator('#toast.show')).to_have_text('请先保存配置，再立即查询')
    assert not manual_requests
    # A fresh tab keeps the original unsaved-draft protection intact.
    manual_page = page.context.new_page()
    manual_page.set_viewport_size({'width': 390, 'height': 844})
    manual_page.route('**/api/state', lambda route: route.fulfill(json=snapshot))
    manual_page.route('**/api/control', manual_control)
    manual_page.goto(root)
    expect(manual_page.locator('#query-progress')).to_be_visible()
    expect(manual_page.locator('#query-progress-title')).to_have_text('查询进度 · 等待首次查询')
    progress_height = manual_page.locator('#query-progress').bounding_box()['height']
    stats_top = manual_page.locator('.stats').bounding_box()['y']
    manual_button = manual_page.locator('#check-now')
    manual_button.dblclick()
    expect(manual_button).to_have_text('正在查询…')
    expect(manual_button).to_be_disabled()
    assert manual_requests == [{'action': 'check-now'}]
    expect(manual_page.locator('#query-progress-title')).to_have_text('手动查询 · 等待调度器启动')
    assert manual_page.locator('#query-progress').bounding_box()['height'] == progress_height
    assert manual_page.locator('.stats').bounding_box()['y'] == stats_top
    snapshot['state']['query_progress'] = {'job_id': 'manual-once', 'manual': True, 'status': 'running',
        'started_at': time.time()-70, 'stage_started_at': time.time()-60, 'stage_label': '读取首页正文',
        'stage_budget_seconds': 15, 'target': 'example_alpha', 'total': 1, 'completed': 0}
    expect(manual_page.locator('#query-progress-title')).to_contain_text('读取首页正文', timeout=10000)
    expect(manual_page.locator('#query-progress-title')).to_have_text('手动查询 · 读取首页正文')
    assert manual_page.locator('#query-progress').bounding_box()['height'] == progress_height
    assert manual_page.locator('.stats').bounding_box()['y'] == stats_top
    expect(manual_page.locator('#query-progress-elapsed')).to_contain_text('已用 1 分')
    expect(manual_page.locator('#query-progress-hint')).to_contain_text('正在等待浏览器响应')
    manual_page.locator('#pause').click()
    expect(manual_page.locator('#resume')).to_be_visible()
    expect(manual_page.locator('#query-progress-hint')).to_contain_text('当前步骤正在安全收尾')
    assert manual_requests[-1]['action'] == 'pause'
    snapshot['jobs'][0].update(status='succeeded', message='本轮完成：查询成功 1 个，失败 0 个，发现 0 个')
    snapshot['state']['query_progress'].update(status='done', stage_label='本轮已完成', completed=1,
        successful=1, finished_at=time.time())
    expect(manual_button).to_have_text('立即查询一次', timeout=10000)
    expect(manual_button).to_be_enabled()
    expect(manual_page.locator('#toast.show')).to_contain_text('查询成功 1 个')
    expect(manual_page.locator('#resume')).to_be_visible()
    expect(manual_page.locator('#pause')).to_be_hidden()
    assert manual_page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    snapshot['state']['query_progress'].update(manual=False, job_id=None, stage_label='打开搜索页面', status='running',
        stage_started_at=time.time(), updated_at=time.time())
    expect(manual_page.locator('#query-progress-title')).to_have_text('自动查询 · 打开搜索页面', timeout=10000)
    assert manual_page.locator('#query-progress').bounding_box()['height'] == progress_height
    assert manual_page.locator('.stats').bounding_box()['y'] == stats_top
    assert not native_dialogs, native_dialogs
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
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='chrome', headless=True)
                try:
                    context = browser.new_context()
                    try:
                        page = context.new_page()
                        page.set_content('<html><body>ready after delay</body></html>')
                        page.evaluate('''() => {
                            const body = document.body;
                            body.remove();
                            setTimeout(() => document.documentElement.append(body), 5500);
                        }''')
                        assert watch.visible_body(page) == 'ready after delay'
                        assert page._xhs_body_attempts == 2
                        page.evaluate("document.body.textContent = 'IP存在风险'")
                        try:
                            watch.check_gate(page)
                        except watch.SitePaused as error:
                            assert str(error) == 'ip_risk'
                        else:
                            raise AssertionError('risk gate was ignored')
                        print('Delayed body recovered; platform risk still blocks queries.')
                    finally:
                        context.close()
                    for reduced in [False, True]:
                        server.write(server.RUNTIME / 'secrets.json', {
                            'webhook_url': 'https://open.feishu.cn/open-apis/bot/v2/hook/TEST_ONLY',
                            'signing_secret': 'TEST_SIGNING_ONLY',
                        })
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

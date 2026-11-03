'use strict';
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const csrf = $('meta[name="csrf-token"]').content;
let latest = null, dirty = false, version = '', toastTimer, selectedFilter = 'all';
const time = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '尚未运行';

function toast(message, error = false) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.toggle('error', error);
  node.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('show'), 5000);
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || '请求失败');
  return data;
}
function switchTab(tab) {
  for (const name of ['people', 'accounts', 'settings']) $('#' + name + '-tab').classList.toggle('hidden', name !== tab);
  $$('.nav').forEach(node => node.classList.toggle('active', node.dataset.tab === tab));
  $('#page-title').textContent = {people: '监控候选管理', accounts: '登录账号', settings: '轮询与通知'}[tab];
  $('#page-subtitle').textContent = {people: '管理候选账号、查询状态与自动关注。', accounts: '独立登录，分别保存。', settings: '让雷达按你的节奏运行。'}[tab];
}
function readEditors() {
  // Hidden cards remain in the draft and must be included when saving a filtered list.
  return $$('.person-card').map(card => ({
    id: card.dataset.id, name: card.querySelector('.name').value.trim(),
    candidates: [...card.querySelectorAll('.candidate-input')].map(input => input.value.trim()).filter(Boolean),
    auto_follow: card.querySelector('.auto-follow').checked
  }));
}
function changed() {
  dirty = true;
  $('#save-hint').textContent = '有未保存的修改';
  updateStats();
  applyFilters();
}
function cardState(card) {
  const state = latest?.state.people.find(item => item.id === card.dataset.id);
  if (!state) return {key: 'waiting', label: '未保存', state: null};
  if (['failed', 'blocked', 'retry'].includes(state.follow_status)) {
    return {key: 'attention', label: {failed: '关注未成功', blocked: '关注已暂停', retry: '等待重试'}[state.follow_status], state};
  }
  if (state.completed) return {key: 'completed', label: '已完成', state};
  if (state.matched_target) return {key: 'matched', label: '已命中', state};
  if (latest.state.paused) return {key: 'paused', label: '已暂停', state};
  return {key: 'waiting', label: '等待命中', state};
}
function updateStats() {
  const people = readEditors();
  $('#people-count').textContent = people.length;
  $('#candidate-count').textContent = people.reduce((count, person) => count + person.candidates.length, 0);
  $('#matched-count').textContent = $$('.person-card').filter(card => cardState(card).state?.matched_target).length;
  for (const card of $$('.person-card')) {
    const count = [...card.querySelectorAll('.candidate-input')].filter(input => input.value.trim()).length;
    card.querySelector('.candidate-total').textContent = count + ' 个候选';
  }
}
function applyFilters() {
  const query = $('#target-search').value.trim().toLocaleLowerCase();
  const cards = $$('.person-card');
  const counts = {all: cards.length, waiting: 0, matched: 0, completed: 0, attention: 0, paused: 0};
  let visible = 0;
  for (const card of cards) {
    const status = cardState(card);
    counts[status.key]++;
    const terms = [card.querySelector('.name').value, ...[...card.querySelectorAll('.candidate-input')].map(input => input.value), status.state?.result?.name || ''].join(' ').toLocaleLowerCase();
    const matches = (!query || terms.includes(query)) && (selectedFilter === 'all' || status.key === selectedFilter);
    const editing = card.contains(document.activeElement);
    card.classList.toggle('hidden', !matches && !editing);
    if (matches || editing) visible++;
  }
  for (const button of $$('.filter-button')) {
    const active = button.dataset.filter === selectedFilter;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
    button.querySelector('.filter-count').textContent = counts[button.dataset.filter];
  }
  $('#results-count').textContent = '显示 ' + visible + ' / ' + cards.length + ' 组';
  $('#empty').classList.toggle('hidden', cards.length > 0);
  $('#filter-empty').classList.toggle('hidden', cards.length === 0 || visible > 0);
  $('#status-filters').classList.toggle('hidden', cards.length === 0);
  $('#people-list').classList.toggle('hidden', cards.length === 0);
}
function resetFilters() {
  $('#target-search').value = '';
  selectedFilter = 'all';
  applyFilters();
}
function reindexCandidates(card) {
  const rows = [...card.querySelectorAll('.candidate-row')];
  rows.forEach((row, index) => {
    row.querySelector('.candidate-index').textContent = String(index + 1).padStart(2, '0');
    row.querySelector('.candidate-input').setAttribute('aria-label', '候选小红书号 ' + (index + 1));
    row.querySelector('.remove-candidate').setAttribute('aria-label', '移除候选 ' + (index + 1));
  });
  card.querySelector('.add-candidate').disabled = rows.length >= 10;
}
function addCandidate(card, value = '', focus = false, after = null) {
  const list = card.querySelector('.candidate-list');
  if (list.children.length >= 10) { toast('每组最多添加 10 个候选', true); return; }
  const row = document.createElement('div');
  row.className = 'candidate-row';
  const number = document.createElement('span'); number.className = 'candidate-index'; number.setAttribute('aria-hidden', 'true');
  const input = document.createElement('input'); input.className = 'candidate-input'; input.type = 'text'; input.value = value;
  input.placeholder = '输入小红书号'; input.autocomplete = 'off'; input.spellcheck = false;
  const remove = document.createElement('button'); remove.className = 'remove-candidate'; remove.type = 'button'; remove.textContent = '×'; remove.title = '移除此候选';
  row.append(number, input, remove);
  if (after) after.after(row); else list.append(row);
  input.addEventListener('input', changed);
  input.addEventListener('blur', () => setTimeout(applyFilters, 0));
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); addCandidate(card, '', true, row); changed(); }
  });
  input.addEventListener('paste', event => {
    if (input.readOnly) return;
    const values = event.clipboardData.getData('text').trim().split(/[\s,，;；]+/).filter(Boolean);
    if (values.length < 2) return;
    const count = list.children.length - 1 + values.length;
    if (count > 10) { event.preventDefault(); toast('每组最多添加 10 个候选', true); return; }
    event.preventDefault(); input.value = values[0];
    let previous = row;
    for (const value of values.slice(1)) { addCandidate(card, value, false, previous); previous = previous.nextElementSibling; }
    changed();
  });
  remove.onclick = () => {
    if (list.children.length === 1) input.value = ''; else row.remove();
    reindexCandidates(card); changed();
  };
  reindexCandidates(card);
  if (focus) input.focus();
}
function renderPeople(people) {
  const list = $('#people-list'); list.replaceChildren();
  for (const person of people) {
    const card = $('#person-template').content.firstElementChild.cloneNode(true);
    card.dataset.id = person.id;
    card.querySelector('.name').value = person.name;
    card.querySelector('.auto-follow').checked = person.auto_follow;
    card.querySelector('.person-avatar').textContent = (person.name || '?').slice(0, 1).toUpperCase();
    card.querySelector('.auto-follow').setAttribute('aria-label', (person.name || '当前目标') + '自动关注');
    for (const candidate of person.candidates.length ? person.candidates : ['']) addCandidate(card, candidate);
    card.querySelector('.name').addEventListener('input', () => {
      card.querySelector('.person-avatar').textContent = (card.querySelector('.name').value || '?').slice(0, 1).toUpperCase(); changed();
    });
    card.querySelector('.name').addEventListener('blur', () => setTimeout(applyFilters, 0));
    card.querySelector('.auto-follow').addEventListener('input', changed);
    card.querySelector('.add-candidate').onclick = () => { addCandidate(card, '', true); changed(); };
    card.querySelector('.remove').onclick = () => {
      if (confirm('删除“' + (card.querySelector('.name').value || '未命名目标') + '”的监控配置？现有关注关系将保留。')) {
        card.remove(); changed();
      }
    };
    list.append(card);
  }
  updateStats(); renderStatuses();
}
function renderStatuses() {
  if (!latest) return;
  for (const card of $$('.person-card')) {
    const {key, label, state} = cardState(card);
    const badge = card.querySelector('.person-status'), info = card.querySelector('.match-info');
    badge.className = 'person-status pill ' + ({completed: 'green', matched: 'green', attention: 'orange', paused: 'orange'}[key] || '');
    badge.textContent = label; info.replaceChildren();
    for (const row of card.querySelectorAll('.candidate-row')) {
      const input = row.querySelector('.candidate-input');
      const matched = !!state?.matched_target && input.value.toLowerCase() === state.matched_target.toLowerCase();
      input.readOnly = matched; row.classList.toggle('matched-candidate', matched);
      row.querySelector('.remove-candidate').disabled = matched;
      row.querySelector('.remove-candidate').title = matched ? '已命中的候选将保留在配置中' : '移除此候选';
    }
    if (state?.matched_target) {
      const line = document.createElement('div'); line.className = 'match-summary'; line.textContent = '已命中 ' + state.matched_target; info.append(line);
      const detail = document.createElement('div'); detail.textContent = '同组其余候选已停止查询'; info.append(detail);
      if (state.result?.url && /^https:\/\/www\.xiaohongshu\.com\/user\/profile\/[a-f0-9]{24}$/i.test(state.result.url)) {
        const link = document.createElement('a'); link.href = state.result.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = '查看主页 ↗'; info.append(link);
      }
    } else if (state?.last_checked_at) { info.textContent = '上次查询 ' + time(state.last_checked_at); }
    info.classList.toggle('hidden', !info.childNodes.length);
  }
  applyFilters();
}
function renderStatus() {
  const state = latest.state, badge = $('#global-status'); badge.className = 'pill';
  badge.textContent = latest.config.people.length === 0 ? '等待配置' : latest.pending_apply ? '配置待应用' : state.paused ? '监测已暂停' : state.completed ? '全部已完成' : '监测已开启';
  badge.classList.toggle('green', !state.paused && !latest.pending_apply);
  $('#pause').classList.toggle('hidden', state.paused); $('#resume').classList.toggle('hidden', !state.paused);
  $('#follow-resume').classList.toggle('hidden', !state.follow_paused);
  $('#last-run').textContent = state.heartbeat_at ? new Date(state.heartbeat_at * 1000).toLocaleTimeString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '尚未运行';
  const messages = [];
  if (latest.pending_apply) messages.push('配置已保存，后台将在当前轮次结束后应用。');
  if (state.worker_fault) messages.push('后台运行异常，请检查本机网络和日志。');
  if (state.follow_paused) messages.push('大号自动关注已暂停，请前往“登录账号”处理后恢复。');
  if (state.pending_notifications) messages.push('有通知等待发送；请检查飞书配置和网络。');
  $('#banner').classList.toggle('hidden', messages.length === 0); $('#banner').textContent = messages.join(' ');
  for (const role of ['search', 'follow']) {
    const account = latest.accounts[role], node = $('#' + role + '-account'); node.replaceChildren();
    if (account.name) {
      const name = document.createElement('strong'); name.textContent = account.name; node.append(name);
      const id = document.createElement('div'); id.textContent = '小红书号：' + account.red_id; node.append(id);
      const caption = document.createElement('small'); caption.textContent = '登录态已保存 · ' + time(account.enrolled_at); node.append(caption);
    } else node.textContent = '尚未保存登录信息';
  }
  const running = latest.jobs.find(job => job.status === 'running'); $$('.login,.fresh').forEach(button => button.disabled = !!running);
  $('#jobs').replaceChildren();
  for (const job of latest.jobs.slice(-4).reverse()) {
    const node = document.createElement('div'); node.textContent = job.title + ' · ' + (job.status === 'running' ? '操作进行中，请查看 Chrome 窗口' : job.message); $('#jobs').append(node);
  }
  renderStatuses(); updateStats();
}
async function load(initial = false) {
  try {
    latest = await api('/api/state');
    if (initial) {
      version = latest.version; $('#interval').value = latest.config.interval_seconds / 60;
      renderPeople(latest.config.people);
      $('#webhook-status').textContent = latest.notifications.configured ? '已设置，留空不修改' : '尚未设置';
      $('#mentions').value = latest.notifications.mention_users.map(user => user.name + ',' + user.user_id).join('\n');
    }
    renderStatus();
  } catch (error) { toast(error.message, true); }
}
async function savePeople() {
  try {
    const config = {interval_seconds: Number($('#interval').value) * 60, people: readEditors()};
    await api('/api/config', {version, config}); dirty = false;
    const result = await api('/api/state'); version = result.version; latest = result;
    $('#save-hint').textContent = '已保存'; toast('配置已保存'); renderStatus();
  } catch (error) { toast(error.message, true); }
}
function addPerson() {
  const people = readEditors();
  people.push({id: 'p_' + crypto.randomUUID().replaceAll('-', '').slice(0, 16), name: '', candidates: [], auto_follow: false});
  $('#target-search').value = ''; selectedFilter = 'all'; renderPeople(people); changed();
  const card = $$('.person-card').at(-1); card.scrollIntoView({block: 'nearest', behavior: 'smooth'}); card.querySelector('.name').focus();
}
async function control(action) {
  try { await api('/api/control', {action}); toast('操作已提交'); setTimeout(() => load(), 600); }
  catch (error) { toast(error.message, true); }
}
$$('.nav').forEach(node => node.onclick = () => switchTab(node.dataset.tab));
$('#add-person').onclick = addPerson; $('#add-empty').onclick = addPerson;
$('#save-people').onclick = savePeople; $('#save-interval').onclick = savePeople;
$('#pause').onclick = () => control('pause'); $('#resume').onclick = () => control('resume');
$('#follow-resume').onclick = () => control('follow-resume'); $('#test-notifications').onclick = () => control('test-webhook');
$('#target-search').addEventListener('input', applyFilters);
$$('.filter-button').forEach(button => button.onclick = () => { selectedFilter = button.dataset.filter; applyFilters(); });
$('#clear-filters').onclick = resetFilters;
for (const button of $$('.login,.fresh')) button.onclick = async () => {
  const fresh = button.classList.contains('fresh');
  if (fresh && !confirm('将清除这个专用浏览器的 Cookie 并重新打开登录窗口。现有任务与关注关系不变，是否继续？')) return;
  try { await api('/api/login', {role: button.dataset.role, fresh}); toast('正在打开独立 Chrome 登录窗口，请扫码登录'); switchTab('accounts'); setTimeout(() => load(), 500); }
  catch (error) { toast(error.message, true); }
};
$('#save-notifications').onclick = async () => {
  try {
    const users = $('#mentions').value.split('\n').filter(line => line.trim()).map(line => {
      const parts = line.split(/[,，]/); if (parts.length !== 2) throw Error('@ 对象请按“显示名,Open ID”填写');
      return {name: parts[0].trim(), user_id: parts[1].trim()};
    });
    await api('/api/notifications', {webhook_url: $('#webhook').value, signing_secret: $('#signing').value, mention_users: users});
    $('#webhook').value = ''; $('#signing').value = ''; toast('通知设置已保存'); await load();
    $('#webhook-status').textContent = latest.notifications.configured ? '已设置，留空不修改' : '尚未设置';
  } catch (error) { toast(error.message, true); }
};
window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
load(true); setInterval(() => load(), 5000);

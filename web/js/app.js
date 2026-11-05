import {layoutChange, reveal, scrollToNode, setVisible, toggleDisclosure} from './motion.js';
import {accountName, editAccountLabel, renderAccounts, renderFollowerOptions} from './accounts.js';
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const csrf = $('meta[name="csrf-token"]').content;
let latest = null, dirty = false, version = '', toastTimer, selectedFilter = 'unfinished';
let currentTab = 'people', searchAccountId = null;
let jobsInitialized = false;
let activeNotices = new Set();
const seenJobResults = new Set(), recentJobResults = new Map();
const credentialEditing = {webhook: false, signing: false};
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
  if (currentTab === tab) return;
  currentTab = tab;
  for (const name of ['people', 'accounts', 'settings']) setVisible($('#' + name + '-tab'), name === tab);
  $$('.nav').forEach(node => {
    node.classList.toggle('active', node.dataset.tab === tab);
    if (node.dataset.tab === tab) node.setAttribute('aria-current', 'page');
    else node.removeAttribute('aria-current');
  });
  reveal($('#page-title').parentElement, 5);
  $('#page-title').textContent = {people: '监控目标', accounts: '登录账号', settings: '轮询与通知'}[tab];
  $('#page-subtitle').textContent = {people: '管理候选账号、查询状态与自动关注', accounts: '独立登录，分别保存', settings: '让雷达按你的节奏运行'}[tab];
}
function readEditors() {
  // Hidden cards remain in the draft and must be included when saving a filtered list.
  return $$('.person-card').map(card => ({
    id: card.dataset.id, name: card.querySelector('.name').value.trim(),
    candidates: [...card.querySelectorAll('.candidate-input')].map(input => input.value.trim()).filter(Boolean),
    auto_follow: card.querySelectorAll('.follow-account:checked').length > 0,
    follow_account_ids: [...card.querySelectorAll('.follow-account:checked')].map(input => input.value)
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
  layoutChange($('#people-list'), updateFilters);
}
function updateFilters() {
  const query = $('#target-search').value.trim().toLocaleLowerCase();
  const cards = $$('.person-card');
  const counts = {all: cards.length, unfinished: 0, completed: 0};
  let visible = 0;
  for (const card of cards) {
    const status = cardState(card);
    const group = status.key === 'completed' ? 'completed' : 'unfinished';
    counts[group]++;
    const terms = [card.querySelector('.name').value, ...[...card.querySelectorAll('.candidate-input')].map(input => input.value), status.state?.result?.name || ''].join(' ').toLocaleLowerCase();
    const matchesStatus = selectedFilter === 'all' || selectedFilter === group;
    const matches = (!query || terms.includes(query)) && matchesStatus;
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
  setVisible($('#empty'), cards.length === 0);
  setVisible($('#filter-empty'), cards.length > 0 && visible === 0);
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
    const next = row.nextElementSibling || row.previousElementSibling;
    layoutChange(list, () => {
      if (list.children.length === 1) input.value = ''; else row.remove();
      reindexCandidates(card);
    });
    (next?.querySelector('.candidate-input') || input).focus({preventScroll: true});
    changed();
  };
  reindexCandidates(card);
  if (focus) { reveal(row, 6); input.focus({preventScroll: true}); }
}
function renderPeople(people) {
  const list = $('#people-list'); list.replaceChildren();
  for (const person of people) list.append(createPerson(person));
}
function createPerson(person) {
  const card = $('#person-template').content.firstElementChild.cloneNode(true);
  card.dataset.id = person.id;
  card.querySelector('.name').value = person.name;
  renderFollowerOptions(card, latest?.accounts || [], person.follow_account_ids || [], changed);
  card.querySelector('.person-avatar').textContent = (person.name || '?').slice(0, 1).toUpperCase();
  for (const candidate of person.candidates.length ? person.candidates : ['']) addCandidate(card, candidate);
  card.querySelector('.name').addEventListener('input', () => {
    card.querySelector('.person-avatar').textContent = (card.querySelector('.name').value || '?').slice(0, 1).toUpperCase(); changed();
  });
  card.querySelector('.name').addEventListener('blur', () => setTimeout(applyFilters, 0));
  card.querySelector('.add-candidate').onclick = () => { addCandidate(card, '', true); changed(); };
  card.querySelector('.remove').onclick = () => {
    if (confirm('删除“' + (card.querySelector('.name').value || '未命名目标') + '”的监控配置？现有关注关系将保留')) {
      const next = card.nextElementSibling || card.previousElementSibling;
      layoutChange($('#people-list'), () => { card.remove(); dirty = true; updateStats(); updateFilters(); });
      $('#save-hint').textContent = '有未保存的修改';
      (next && !next.classList.contains('hidden') ? next.querySelector('.name') : $('#add-person')).focus({preventScroll: true});
    }
  };
  return card;
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
    for (const job of state?.follow_jobs || []) {
      if (job.status === 'waiting_match') continue;
      const line = document.createElement('div'); line.className = 'follow-task-status';
      const account = latest.accounts.find(a => a.id === job.account_id);
      line.textContent = accountName(account) + ' · ' + ({pending: '等待关注', running: '关注中', retry: '等待重试', succeeded: '已关注', failed: '关注未成功', blocked: '需重新登录'}[job.status] || job.status);
      info.append(line);
    }
    info.classList.toggle('hidden', !info.childNodes.length);
  }
}
function renderNotificationFields() {
  const {configured, signing_configured: signed, mention_users: users} = latest.notifications;
  for (const [slot, saved, label] of [['webhook', configured, 'Webhook 地址'], ['signing', signed, '签名密钥']]) {
    if (!saved) credentialEditing[slot] = false;
    const editing = credentialEditing[slot];
    $('#' + slot + '-status').textContent = saved ? '已配置' : '未配置';
    $('#' + slot + '-saved').classList.toggle('hidden', !saved);
    toggleDisclosure($('#' + slot + '-editor'), !saved || editing);
    $('#replace-' + slot).setAttribute('aria-expanded', String(editing));
    $('#replace-' + slot).setAttribute('aria-controls', slot + '-editor');
    $('#replace-' + slot).textContent = editing ? '取消更换' : '更换';
    $('#' + slot).placeholder = saved ? '请输入新的' + label : slot === 'webhook' ? '请输入飞书或 Lark 自定义机器人的 Webhook 地址' : '机器人开启签名校验时，请输入签名密钥';
    $('#' + slot + '-hint').textContent = saved ? (editing ? '填写新值后，点击下方“保存通知设置”生效' : '已安全保存。可点击“更换”或“清除”，内容不会显示') : slot === 'webhook' ? '首次使用请填写你自己的 Webhook 地址' : '未开启签名校验时无需填写';
  }
  $('#clear-notifications').classList.toggle('hidden', !configured && !signed && !users.length);
  $('#test-notifications').disabled = !configured;
  $('#test-notifications').title = configured ? '' : '请先配置 Webhook';
}
async function clearNotificationFields(fields) {
  const all = fields.length === 3;
  const prompt = all ? '清除全部飞书通知配置（Webhook、签名密钥和 @ 对象）？重新配置 Webhook 前将无法发送飞书通知，待发送消息会保留' : fields[0] === 'webhook_url' ? '清除已保存的 Webhook 地址？重新配置前将无法发送飞书通知，待发送消息会保留' : '清除已保存的签名密钥？如果机器人仍启用签名校验，消息可能发送失败';
  if (!confirm(prompt)) return;
  try {
    await api('/api/notifications/clear', {fields});
    if (fields.includes('webhook_url')) { $('#webhook').value = ''; credentialEditing.webhook = false; }
    if (fields.includes('signing_secret')) { $('#signing').value = ''; credentialEditing.signing = false; }
    if (fields.includes('mention_users')) $('#mentions').value = '';
    await load(); toast(all ? '通知配置已清除' : '已清除所选配置');
  } catch (error) { toast(error.message, true); }
}

function renderStatus({initial = false, saved = false} = {}) {
  renderNotificationFields();
  const state = latest.state, badge = $('#global-status'); badge.className = 'pill';
  badge.textContent = latest.config.people.length === 0 ? '等待配置' : latest.pending_apply ? '配置待应用' : state.paused ? '监测已暂停' : state.completed ? '全部已完成' : '监测已开启';
  badge.classList.toggle('green', !state.paused && !latest.pending_apply);
  $('#pause').classList.toggle('hidden', state.paused); $('#resume').classList.toggle('hidden', !state.paused);
  $('#last-run').textContent = state.heartbeat_at ? new Date(state.heartbeat_at * 1000).toLocaleTimeString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '尚未运行';
  const pendingApply = '配置已保存，将在下一轮调度时生效';
  const notices = new Set();
  if (latest.pending_apply) notices.add(pendingApply);
  if (state.worker_fault) notices.add('后台运行异常，请检查本机网络和日志');
  if (state.follow_paused) notices.add('部分账号的自动关注已暂停，请前往“登录账号”处理后恢复');
  if (state.pending_notifications) notices.add('有通知等待发送，请检查飞书配置和网络');
  const messages = [...notices].filter(message => !activeNotices.has(message) || (saved && message === pendingApply));
  activeNotices = notices;
  if (saved && !latest.pending_apply) messages.unshift('配置已保存');
  if (messages.length) toast(messages.join('。'));
  const running = latest.jobs.some(job => job.status === 'running');
  renderAccounts(latest.accounts, searchAccountId, running, {login: loginAccount, resume: aid => control('follow-resume', aid), rename: account => editAccountLabel(account, renameAccount)});
  for (const card of $$('.person-card')) {
    const selected = [...card.querySelectorAll('.follow-account:checked')].map(input => input.value);
    renderFollowerOptions(card, latest.accounts, selected, changed);
  }
  renderJobs();
  renderStatuses(); updateStats();
  if (initial) {
    updateFilters();
    $('.stats').removeAttribute('aria-busy');
  } else {
    applyFilters();
  }
}
async function load() {
  try {
    const snapshot = await api('/api/state');
    const initial = !latest;
    latest = snapshot;
    if (initial) {
      searchAccountId = latest.config.search_account_id;
      version = latest.version; $('#interval').value = latest.config.interval_seconds / 60;
      renderPeople(latest.config.people);
      $('#mentions').value = latest.notifications.mention_users.map(user => user.name + ',' + user.user_id).join('\n');
    }
    renderStatus({initial});
  } catch (error) { toast(error.message, true); }
}
async function savePeople() {
  try {
    const config = {interval_seconds: Number($('#interval').value) * 60, search_account_id: searchAccountId, people: readEditors()};
    await api('/api/config', {version, config}); dirty = false;
    const result = await api('/api/state'); version = result.version; latest = result;
    $('#save-hint').textContent = '已保存'; renderStatus({saved: true});
  } catch (error) { toast(error.message, true); }
}
function addPerson() {
  const card = createPerson({id: 'p_' + crypto.randomUUID().replaceAll('-', '').slice(0, 16), name: '', candidates: [], auto_follow: false});
  $('#target-search').value = '';
  if (selectedFilter !== 'all') selectedFilter = 'unfinished';
  layoutChange($('#people-list'), () => {
    $('#people-list').append(card);
    dirty = true; $('#save-hint').textContent = '有未保存的修改';
    updateStats(); renderStatuses(); updateFilters();
  });
  card.querySelector('.name').focus({preventScroll: true});
  scrollToNode(card);
}
async function control(action, accountId = null) {
  try { await api('/api/control', {action, account_id: accountId}); toast('操作已提交'); setTimeout(() => load(), 600); }
  catch (error) { toast(error.message, true); }
}
$$('.nav').forEach(node => node.onclick = () => switchTab(node.dataset.tab));
$('#add-person').onclick = addPerson; $('#add-empty').onclick = addPerson;
$('#save-people').onclick = savePeople; $('#save-interval').onclick = savePeople;
$('#pause').onclick = () => control('pause'); $('#resume').onclick = () => control('resume');
$('#test-notifications').onclick = () => control('test-webhook');
$('#target-search').addEventListener('input', applyFilters);
$$('.filter-button').forEach(button => button.onclick = () => { selectedFilter = button.dataset.filter; applyFilters(); });
$('#clear-filters').onclick = resetFilters;
async function renameAccount(accountId, label, previousLabel) {
  await api('/api/accounts/rename', {account_id: accountId, label, previous_label: previousLabel});
  await load();
  toast('账号备注已保存');
}
async function loginAccount(accountId, fresh) {
  if (fresh && !confirm('将清除此账号浏览器的 Cookie 并重新扫码，请登录同一个身份。其他身份请添加新账号')) return;
  try {
    await api('/api/login', {account_id: accountId, fresh});
    toast('正在打开独立 Chrome 登录窗口，请扫码登录');
    await load();
  } catch (error) { toast(error.message, true); }
}
$('#add-account').onclick = async () => {
  const button = $('#add-account'); button.disabled = true;
  try {
    await api('/api/accounts', {label: $('#account-label').value.trim()});
    $('#account-label').value = ''; await load(); toast('账号已添加，点击“登录 / 校验”扫码');
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
};
$('#search-account-select').onchange = () => { searchAccountId = $('#search-account-select').value || null; changed(); };
$('#save-search-account').onclick = savePeople;

// Show active work on its own page; completed results expire instead of becoming a log.
function renderJobs() {
  for (const job of latest.jobs) {
    if (job.status === 'running' || seenJobResults.has(job.id)) continue;
    seenJobResults.add(job.id);
    if (jobsInitialized) {
      recentJobResults.set(job.scope || 'people', {job, expires: Date.now() + 8000});
      setTimeout(renderJobs, 8100);
    }
  }
  jobsInitialized = true;
  for (const scope of ['people', 'accounts', 'settings']) {
    const node = $('#' + scope + '-jobs'); node.replaceChildren();
    const running = latest.jobs.find(job => job.status === 'running' && job.scope === scope);
    const recent = recentJobResults.get(scope);
    const job = running || (recent?.expires > Date.now() ? recent.job : null);
    if (!job) continue;
    const line = document.createElement('div'); line.className = 'job-feedback';
    line.textContent = job.title + ' · ' + (job.status === 'running' ? (scope === 'accounts' ? '进行中，请查看 Chrome 窗口' : '进行中') : job.message);
    node.append(line);
  }
}
for (const slot of ['webhook', 'signing']) {
  $('#replace-' + slot).onclick = () => {
    credentialEditing[slot] = !credentialEditing[slot];
    if (!credentialEditing[slot]) $('#' + slot).value = '';
    renderNotificationFields();
    if (credentialEditing[slot]) $('#' + slot).focus();
  };
}
$('#clear-webhook').onclick = () => clearNotificationFields(['webhook_url']);
$('#clear-signing').onclick = () => clearNotificationFields(['signing_secret']);
$('#clear-notifications').onclick = () => clearNotificationFields(['webhook_url', 'signing_secret', 'mention_users']);
$('#save-notifications').onclick = async () => {
  try {
    const users = $('#mentions').value.split('\n').filter(line => line.trim()).map(line => {
      const parts = line.split(/[,，]/); if (parts.length !== 2) throw Error('@ 对象请按“显示名,Open ID”填写');
      return {name: parts[0].trim(), user_id: parts[1].trim()};
    });
    const values = {mention_users: users};
    for (const [slot, key] of [['webhook', 'webhook_url'], ['signing', 'signing_secret']]) {
      const saved = slot === 'webhook' ? latest.notifications.configured : latest.notifications.signing_configured;
      const value = $('#' + slot).value.trim();
      if (saved && credentialEditing[slot] && !value) throw Error('请填写新的' + (slot === 'webhook' ? ' Webhook 地址' : '签名密钥') + '，或取消更换');
      if (!saved || credentialEditing[slot]) values[key] = value;
    }
    await api('/api/notifications', values);
    $('#webhook').value = ''; $('#signing').value = '';
    credentialEditing.webhook = false; credentialEditing.signing = false;
    toast('通知设置已保存'); await load();
  } catch (error) { toast(error.message, true); }
};
window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
load(); setInterval(() => load(), 5000);

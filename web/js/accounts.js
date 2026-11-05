import {layoutChange, reveal, setVisible} from './motion.js';
const $ = selector => document.querySelector(selector);
export const accountName = account => account?.name || account?.label || '未登录账号';
const initials = new Intl.Segmenter('zh-CN', {granularity: 'grapheme'});

function renderAccountIcon(icon, account) {
  icon.textContent = [...initials.segment((account.label || '').trim())][0]?.segment || '?';
  // Use the stable identity so renaming, sorting and reloading keep the same color.
  let hash = 2166136261;
  for (const character of account.id) hash = Math.imul(hash ^ character.codePointAt(0), 16777619) >>> 0;
  icon.dataset.tone = hash % 6;
}

function accountChoiceLabel(account) {
  const text = document.createElement('span'); text.className = 'choice-label';
  const name = document.createElement('strong'); name.textContent = account.label;
  const detail = document.createElement('small');
  detail.textContent = account.ready ? accountName(account) + (account.red_id ? ' · ' + account.red_id : '') : '尚未登录';
  text.append(name, detail);
  return text;
}

function updateFollowerSummary(card) {
  const count = card.querySelectorAll('.follow-account:checked').length;
  card.querySelector('.selected-follow-count').textContent = count ? count + ' 个账号' : '仅通知';
  const name = card.querySelector('.name').value || '当前目标';
  card.querySelector('.follow-picker').setAttribute('aria-label', name + '：选择自动关注账号，' + (count ? '已选 ' + count + ' 个' : '仅通知'));
}

export function renderFollowerOptions(card, accounts, selected, onChange) {
  // Draft selection lives in hidden inputs; opening the modal never changes grid height.
  const list = card.querySelector('.follow-account-options');
  const signature = JSON.stringify(accounts.map(a => [a.id, a.label, a.name, a.ready]));
  if (list.dataset.accounts !== signature) {
    list.dataset.accounts = signature;
    list.replaceChildren();
    for (const account of accounts) {
      const input = document.createElement('input'); input.type = 'checkbox';
      input.className = 'follow-account'; input.value = account.id; input.checked = selected.includes(account.id);
      list.append(input);
    }
  }
  updateFollowerSummary(card);
  card.querySelector('.follow-picker').onclick = () => {
    const dialog = $('#follow-dialog'), options = $('#follow-dialog-options');
    options.replaceChildren();
    $('#follow-dialog-description').textContent = '监控目标：' + (card.querySelector('.name').value || '未命名目标');
    if (!accounts.length) {
      const empty = document.createElement('p'); empty.textContent = '还没有可选账号，请先到“登录账号”添加'; options.append(empty);
    }
    for (const account of accounts) {
      const row = document.createElement('label'); row.className = 'account-choice';
      const input = document.createElement('input'); input.type = 'checkbox'; input.value = account.id;
      input.checked = [...list.querySelectorAll('input:checked')].some(node => node.value === account.id);
      row.append(input, accountChoiceLabel(account)); options.append(row);
    }
    $('#close-follow-dialog').onclick = $('#cancel-follow-dialog').onclick = () => dialog.close();
    $('#apply-follow-dialog').onclick = () => {
      const ids = new Set([...options.querySelectorAll('input:checked')].map(input => input.value));
      let changed = false;
      list.querySelectorAll('input').forEach(input => {
        if (input.checked !== ids.has(input.value)) changed = true;
        input.checked = ids.has(input.value);
      });
      updateFollowerSummary(card);
      dialog.close();
      if (changed) onChange();
    };
    dialog.showModal();
    reveal(dialog, 8);
  };
}

export function editAccountLabel(account, save) {
  const dialog = $('#rename-account-dialog'), input = $('#rename-account-label');
  const submit = $('#save-account-label'), cancel = $('#cancel-rename-account');
  let saving = false;
  input.value = account.label;
  $('#rename-account-error').textContent = '';
  cancel.onclick = () => dialog.close();
  dialog.oncancel = event => { if (saving) event.preventDefault(); };
  $('#rename-account-form').onsubmit = async event => {
    event.preventDefault();
    if (saving) return;
    const label = input.value.trim();
    if (!label) { $('#rename-account-error').textContent = '请填写 1–40 字的账号备注'; input.focus(); return; }
    if (label === account.label) { dialog.close(); return; }
    saving = true; submit.disabled = true; cancel.disabled = true; input.disabled = true;
    $('#rename-account-error').textContent = '';
    try {
      await save(account.id, label, account.label);
      dialog.close();
    } catch (error) { $('#rename-account-error').textContent = error.message; }
    finally { saving = false; submit.disabled = false; cancel.disabled = false; input.disabled = false; }
  };
  dialog.showModal(); input.select(); reveal(dialog, 8);
}

const searchProblems = {
  login_required: '登录已失效',
  search_account_not_ready: '尚未登录',
  search_account_mismatch: '登录身份不一致',
  ip_risk: '网络存在风险',
  risk_control: '平台要求安全验证',
  http_403: '平台拒绝访问',
  http_429: '平台限制访问频率',
};

export function renderSearchAccounts(accounts, selected, state, onChange) {
  const list = $('#search-account-order');
  const add = $('#add-search-account');
  const active = state.active_search_account_id, errors = state.search_account_errors || {};
  const byId = new Map(accounts.map(account => [account.id, account]));
  const stopped = state.paused || state.completed;
  const signature = JSON.stringify([accounts.map(a => [a.id, a.label, a.name, a.ready, a.red_id]), selected, active, errors, stopped]);
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  add.onclick = () => {
    const dialog = $('#search-account-dialog'), options = $('#search-account-dialog-options');
    const confirm = $('#confirm-search-account-dialog');
    const available = accounts.filter(account => !selected.includes(account.id));
    options.replaceChildren();
    if (!available.length) {
      const empty = document.createElement('p');
      empty.textContent = accounts.length ? '所有账号都已加入搜索顺序' : '请先在上方添加账号';
      options.append(empty);
    }
    for (const account of available) {
      const row = document.createElement('label'); row.className = 'account-choice';
      const input = document.createElement('input');
      input.type = 'checkbox'; input.value = account.id;
      row.append(input, accountChoiceLabel(account));
      options.append(row);
    }
    confirm.disabled = true;
    options.onchange = () => { confirm.disabled = !options.querySelector('input:checked'); };
    confirm.onclick = () => {
      const choices = [...options.querySelectorAll('input:checked')].map(input => input.value);
      if (!choices.length) return;
      dialog.close();
      onChange([...selected, ...choices]);
    };
    $('#close-search-account-dialog').onclick = $('#cancel-search-account-dialog').onclick = () => dialog.close();
    dialog.showModal();
    reveal(dialog, 8);
  };

  list.replaceChildren();
  selected.forEach((id, index) => {
    const account = byId.get(id);
    const row = document.createElement('li'); row.className = 'search-account-item';
    const label = document.createElement('div'); label.className = 'choice-label';
    const name = document.createElement('strong');
    const accountLabel = account?.label || '未知账号';
    name.textContent = (index + 1) + ' · ' + accountLabel;
    const status = document.createElement('small');
    const problem = errors[id];
    status.textContent = id === active ? (stopped ? '上次使用' : '当前使用') : problem ? searchProblems[problem] || '上次搜索异常' :
      !account?.ready ? '待登录' : index === 0 ? '首选' : '备用';
    label.append(name, status);
    const controls = document.createElement('div'); controls.className = 'row';
    for (const [buttonIndex, [action, offset]] of [['上移', -1], ['下移', 1], ['移除', 0]].entries()) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'button quiet'; button.textContent = action;
      button.setAttribute('aria-label', action + ' ' + accountLabel);
      const position = index + offset;
      button.disabled = position < 0 || position >= selected.length;
      button.onclick = () => {
        const order = selected.slice();
        order.splice(index, 1);
        if (offset) order.splice(position, 0, id);
        onChange(order);
        const next = offset && list.children[position].querySelectorAll('button')[buttonIndex];
        (next && !next.disabled ? next : $('#save-search-account')).focus();
      };
      controls.append(button);
    }
    row.append(label, controls); list.append(row);
  });
}

export function renderAccounts(accounts, busy, {login, resume, rename}) {
  const list = $('#accounts-list');
  layoutChange(list, () => {
    for (const account of accounts) {
      let card = [...list.children].find(node => node.dataset.id === account.id);
      if (!card) {
        card = $('#account-template').content.firstElementChild.cloneNode(true);
        card.dataset.id = account.id;
        card.querySelector('.login').onclick = () => login(account.id, false);
        card.querySelector('.fresh').onclick = () => login(account.id, true);
        card.querySelector('.account-resume').onclick = () => resume(account.id);
        list.append(card);
      }
      card.querySelector('.account-label-text').textContent = account.label;
      renderAccountIcon(card.querySelector('.account-icon'), account);
      card.querySelector('.rename-account').onclick = () => rename(account);
      card.querySelector('.rename-account').setAttribute('aria-label', '修改 ' + account.label + ' 的备注');
      const role = card.querySelector('.account-role');
      role.textContent = account.follow_paused ? '关注已暂停' : account.follow_needs_resume ? '关注需处理' : account.ready ? '已登录' : '待登录';
      role.className = 'account-role pill ' + (account.follow_needs_resume ? 'orange' : account.ready ? 'green' : '');
      const info = card.querySelector('.account-info');
      info.replaceChildren();
      if (account.ready) {
        const name = document.createElement('strong'); name.textContent = account.name;
        const id = document.createElement('div'); id.textContent = '小红书号：' + (account.red_id || '—');
        const caption = document.createElement('small'); caption.textContent = '登录态已保存';
        info.append(name, id, caption);
      } else info.textContent = '尚未登录，点击下方按钮打开独立窗口扫码';
      setVisible(card.querySelector('.account-resume'), account.follow_needs_resume);
      card.querySelectorAll('.login, .fresh, .account-resume').forEach(button => button.disabled = busy);
    }
  });
  setVisible($('#accounts-empty'), accounts.length === 0);
}

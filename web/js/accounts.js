import {layoutChange, reveal, setVisible} from './motion.js';
const $ = selector => document.querySelector(selector);
export const accountName = account => account?.name || account?.label || '未登录账号';

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
      const row = document.createElement('label'); row.className = 'follow-account-choice';
      const input = document.createElement('input'); input.type = 'checkbox'; input.value = account.id;
      input.checked = [...list.querySelectorAll('input:checked')].some(node => node.value === account.id);
      const text = document.createElement('span'); text.className = 'choice-label';
      const name = document.createElement('strong'); name.textContent = account.label;
      const detail = document.createElement('small'); detail.textContent = account.ready ? accountName(account) + (account.red_id ? ' · ' + account.red_id : '') : '尚未登录';
      text.append(name, detail); row.append(input, text); options.append(row);
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

export function renderAccounts(accounts, selectedSearch, busy, {login, resume, rename}) {
  const select = $('#search-account-select');
  const signature = JSON.stringify(accounts.map(a => [a.id, a.label, a.name, a.ready]));
  if (select.dataset.accounts !== signature) {
    select.dataset.accounts = signature;
    select.replaceChildren(new Option('请选择搜索账号', ''));
    for (const account of accounts) {
      select.append(new Option(accountName(account) + ' · ' + account.label + (account.ready ? '' : '（待登录）'), account.id));
    }
  }
  select.value = selectedSearch || '';
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
      card.querySelector('.rename-account').onclick = () => rename(account);
      card.querySelector('.rename-account').setAttribute('aria-label', '修改 ' + account.label + ' 的备注');
      const role = card.querySelector('.account-role');
      role.textContent = account.follow_paused ? '关注已暂停' : account.follow_needs_resume ? '关注需处理' : selectedSearch === account.id ? '用于搜索' : account.ready ? '已登录' : '待登录';
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

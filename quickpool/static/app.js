'use strict';
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {admin: sessionStorage.getItem('quickpool-admin') || '', config: null, accounts: [], page: 'overview',
  code: 'python', keyVisible: false, refreshing: false, authAccountId: null, authorizationUrl: '',
  authGeneration: 0, authTimer: null, authStatus: 'none', authStarting: false, quotaRefreshing: false,
  selected: new Set(), accountStatus: '', accountSort: '', keys: []};
const baseURL = location.origin + '/v1';
let toastTimer, refreshTimer, logQueryTimer, logRequest = 0, logOffset = 0;
let logFilters = new URLSearchParams();
const logLimit = 50;

function text(selector, value) { $(selector).textContent = String(value); }
function escapeHTML(value) { return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch])); }
function toast(message) {
  const dialog = $('dialog#key-result[open], dialog#key-dialog[open], dialog#detail-dialog[open]');
  const box = $('#toast');
  (dialog || document.body).append(box);
  box.textContent = message; box.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { box.hidden = true; }, 4000);
}
async function copy(value) { try { await navigator.clipboard.writeText(value); toast('已复制'); } catch { toast('剪贴板不可用，请手动复制'); } }
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'Authorization': 'Bearer ' + state.admin,
    ...(options.body ? {'Content-Type': 'application/json'} : {}), ...options.headers}});
  if (response.status === 204) return null;
  const result = await response.json();
  if (response.status === 401 && result.detail === '管理密钥无效') { sessionStorage.removeItem('quickpool-admin'); state.admin = ''; stopAuthPolling(); if ($('#login-dialog').open) $('#login-dialog').close(); showAuth(); throw Error('管理密钥无效，请重新登录'); }
  if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : result.error?.message || '操作失败 (' + response.status + ')');
  return result;
}
function showAuth() { if (!$('#auth-dialog').open) $('#auth-dialog').showModal(); }
function changePage(page) {
  const labels = {overview: ['运行总览', '让每一次请求，都有着落。', '管理 Amazon Quick 会话，查看账号状态与代理请求。'],
    accounts: ['账号池', '你的账号，各就各位。', '添加、授权并管理独立会话，让请求有序流转。'],
    connect: ['API 接入', '一个入口，连接你的工具。', '使用 OpenAI 兼容接口接入客户端，或在这里直接测试。'],
    logs: ['请求日志', '每次转发，都有记录。', '查看真实调用结果、响应耗时与账号状态。'],
    keys: ['API 密钥', '为每个客户端分配密钥。', '按账号分组和用量管理 API 访问。']};
  if (!labels[page]) return;
  state.page = page;
  $$('[data-section]').forEach(el => { el.hidden = el.dataset.section !== page; });
  $$('.nav-item').forEach(el => el.classList.toggle('selected', el.dataset.page === page));
  text('#breadcrumb', labels[page][0]); text('#page-title', labels[page][1]); text('#page-description', labels[page][2]);
  if (page === 'overview') refresh();
  if (page === 'accounts') loadSettings().catch(error => toast(error.message));
  if (['keys', 'connect', 'logs'].includes(page)) loadKeys().catch(error => toast(error.message));
  if (page === 'logs') loadLogs().catch(error => toast(error.message));
}
const accountLabels = {ready: '可用', unconfigured: '待授权', unverified: '待测试', expired: '授权失效',
  cooldown: '冷却中', disabled: '已停用', exhausted: '额度已满'};
function accountStatus(a) {
  if (!a.enabled) return 'disabled';
  return a.state === 'ready' && a.quota_exhausted ? 'exhausted' : a.state;
}
function statusBadge(account) {
  const status = accountStatus(account);
  const title = status === 'exhausted' ? '会话额度已用尽，调度会自动跳过此账号' : account.last_error;
  return `<span class="status ${escapeHTML(status)}" title="${escapeHTML(title)}">${status === 'ready' ? '<i class="status-dot green"></i>' : ''}${escapeHTML(accountLabels[status] ?? '—')}</span>`;
}
function formatTime(epoch) {
  return epoch ? new Date(epoch * 1000).toLocaleString('zh-CN') : '—';
}
function quotaPeriod(account, period, detail) {
  const q = account.quota?.[period];
  const session = period === 'session';
  const label = session ? '会话' : '月度';
  const full = session && (account.quota_exhausted || q?.usedPercentage >= 95);
  const percent = q?.usedPercentage;
  const used = percent == null ? '' : `已用 ${percent}%`;
  const exhausted = full ? '<strong class="quota-full">会话额度已满</strong>' : '';
  const progress = percent != null || full
    ? `<progress class="${full ? 'exhausted' : ''}" max="100" value="${escapeHTML(full && percent == null ? 100 : percent)}" aria-label="${escapeHTML(label)}额度已用"></progress>` : '';
  const minutes = !session && (q?.availableUnits != null || q?.provisionedUnits != null)
    ? `剩余 ${q.availableUnits?.toLocaleString() ?? '—'} / ${q.provisionedUnits?.toLocaleString() ?? '—'} 分钟` : '';
  const resume = session && q?.resumeInMinutes != null && (full || detail) ? `约 ${q.resumeInMinutes} 分钟后恢复` : '';
  const reset = q?.resetsAt != null ? `重置：${q.resetsAt}` : '';
  const content = [(session || detail || !minutes) && used, minutes, resume, detail && reset].filter(Boolean).map(s => `<span>${escapeHTML(s)}</span>`).join('');
  if (!content && !progress && !detail) return '';
  const updated = account.quota?.updated_at ? '更新：' + formatTime(account.quota.updated_at) : '';
  return `<div class="quota-period" title="${escapeHTML([updated, reset].filter(Boolean).join('；'))}"><b>${escapeHTML(label)}</b>${exhausted}${progress}${content || (progress ? '' : '<span>—</span>')}</div>`;
}
function quotaSummary(account) {
  return quotaPeriod(account, 'session', false) + quotaPeriod(account, 'monthly', false)
    || `<span class="muted" title="${escapeHTML(account.quota?.reason)}">暂不可用</span>`;
}
function accountTable(accounts, summary = false) {
  if (!accounts.length) return `<div class="empty"><span class="empty-symbol">▤</span><h3>${state.accounts.length ? '没有匹配的账号' : '账号池，等待你的第一个会话'}</h3><p>${state.accounts.length ? '试试其他搜索词或状态。' : '添加一个 Amazon Quick 账号，完成授权后，<br>即可通过统一 API 发起对话。'}</p>${state.accounts.length ? '' : '<button class="button primary" data-action="add">＋ 添加第一个账号</button>'}</div>`;
  const checkbox = summary ? '' : '<th class="select-cell"><input type="checkbox" id="select-all-accounts" aria-label="选择当前筛选结果"></th>';
  return `<div class="table-wrap"><table><thead><tr>${checkbox}<th>账号</th><th>状态</th><th>剩余额度</th><th>并发 / 权重</th><th>成功 / 失败</th><th>操作</th></tr></thead><tbody>${(summary ? accounts.slice(0, 5) : accounts).map(a => {
    let detail = a.aws_account_id || 'AWS Account';
    if (accountStatus(a) === 'cooldown' && a.cooldown_until != null) {
      const remaining = Math.max(0, Math.ceil(a.cooldown_until - Date.now()/1000));
      detail += ' · ' + remaining + 's 后重试';
    }
    const id = escapeHTML(a.id);
    const select = summary ? '' : `<td class="select-cell"><input type="checkbox" data-select-account="${id}" aria-label="选择${escapeHTML(a.name)}"></td>`;
    return `<tr>${select}<td class="name-cell"><div class="account-identity">
      <span class="account-avatar">${escapeHTML(a.name?.slice(0,1).toUpperCase())}</span>
      <div><button class="account-name" data-action="detail" data-id="${id}">${escapeHTML(a.name ?? '—')}</button><span class="secondary mono">${escapeHTML(detail)}</span></div>
      </div></td><td>${statusBadge(a)}</td><td>${quotaSummary(a)}</td>
      <td class="mono">${escapeHTML(a.inflight)} / ${escapeHTML(a.max_concurrency)}<span class="secondary">权重 ${escapeHTML(a.weight)}</span></td>
      <td class="mono">${escapeHTML(a.successes)} <span class="muted">/ ${escapeHTML(a.failures)}</span></td>
      <td><div class="account-actions"><button data-action="detail" data-id="${id}">详情</button><button data-action="login" data-id="${id}">${a.configured ? '重新授权' : '授权'}</button><button data-action="test" data-id="${id}">测试</button></div></td></tr>`;
  }).join('')}</tbody></table></div>`;
}
function renderSelection() {
  const boxes = $$('[data-select-account]');
  boxes.forEach(box => { box.checked = state.selected.has(box.dataset.selectAccount); });
  const all = $('#select-all-accounts');
  if (all) {
    const checked = boxes.filter(box => box.checked).length;
    all.checked = checked === boxes.length;
    all.indeterminate = checked > 0 && !all.checked;
  }
  $('#batch-bar').hidden = !state.selected.size;
  text('#batch-count', '已选 ' + state.selected.size + ' 个');
}
function renderAccountGroups(container, accounts) {
  $$(container + ' .account-name').forEach((name, index) => {
    const {group_name, notes} = accounts[index];
    if (group_name) {
      const group = document.createElement('span');
      group.className = 'pill account-group-tag'; group.textContent = group_name;
      name.after(group);
    }
    if (notes) {
      const note = document.createElement('span');
      note.className = 'muted account-note'; note.textContent = notes;
      name.parentElement.append(note);
    }
  });
}
function accountGroups() {
  return [...new Set(state.accounts.map(a => a.group_name || '').sort())];
}
function renderAccounts() {
  $('#overview-accounts').innerHTML = accountTable(state.accounts, true);
  renderAccountGroups('#overview-accounts', state.accounts);
  const groupSelect = $('#account-group');
  const selectedGroup = groupSelect.selectedIndex > 0 ? groupSelect.value : null;
  const groups = accountGroups();
  groupSelect.replaceChildren(new Option('全部分组', ''), ...groups.map(group => new Option(group || '未分组', group)));
  groupSelect.selectedIndex = groups.indexOf(selectedGroup) + 1;
  const query = $('#account-search').value.trim().toLowerCase();
  const filtered = state.accounts.filter(a => {
    const status = accountStatus(a);
    const statusFilter = ['unconfigured', 'expired'].includes(status) ? 'auth' : status;
    return (!state.accountStatus || state.accountStatus === statusFilter)
      && (groupSelect.selectedIndex === 0 || (a.group_name || '') === groupSelect.value)
      && [a.name, a.aws_account_id].join(' ').toLowerCase().includes(query);
  });
  const sortValue = {
    session: a => a.quota?.session?.usedPercentage == null ? null : 100 - a.quota.session.usedPercentage,
    monthly: a => a.quota?.monthly?.availableUnits,
    success: a => a.successes + a.failures > 0 ? a.successes / (a.successes + a.failures) : 0,
    recent: a => a.last_used,
  }[state.accountSort];
  if (sortValue) filtered.sort((a, b) => (sortValue(b) ?? -Infinity) - (sortValue(a) ?? -Infinity));
  $('#all-accounts').innerHTML = accountTable(filtered);
  renderAccountGroups('#all-accounts', filtered);
  text('#account-summary', filtered.length + ' / ' + state.accounts.length + ' 个账号');
  const cooldown = state.accounts.some(a => accountStatus(a) === 'cooldown');
  $('[data-batch="cooldown"]').classList.toggle('primary', cooldown);
  $('[data-batch="cooldown"]').disabled = !cooldown;
  $('[data-batch="all"]').disabled = !state.accounts.length;
  renderSelection();
}
function renderLogAccounts() {
  const select = $('#log-account');
  const value = select.value;
  select.replaceChildren(new Option('全部账号', ''), ...state.accounts.map(a => new Option(a.name, a.id)));
  if (state.accounts.some(a => String(a.id) === value)) select.value = value;
  else if (logFilters.has('account_id')) {
    logFilters.delete('account_id');
    logOffset = 0;
  }
}
async function batch(action, ids) {
  const result = await api('/api/accounts/batch', {method: 'POST', body: JSON.stringify({action, account_ids: ids})});
  if (action === 'test') {
    const success = result.results.filter(item => item.ok).length;
    toast('测试完成：成功 ' + success + ' / 失败 ' + (result.results.length - success));
  } else toast('批量操作完成 · ' + result.affected + ' 个账号');
  await refresh();
}
async function refresh() {
  if (!state.admin || state.refreshing) return;
  state.refreshing = true; $('#refresh-icon').classList.add('spin');
  try {
    const result = await api('/api/overview');
    state.accounts = result.accounts;
    const ids = new Set(state.accounts.map(a => a.id));
    for (const id of state.selected) if (!ids.has(id)) state.selected.delete(id);
    const ready = state.accounts.filter(a => accountStatus(a) === 'ready').length;
    text('#stat-ready', ready); text('#stat-total', result.accounts.length); text('#nav-count', result.accounts.length);
    text('#pool-count', result.accounts.length + ' 个账号'); text('#route-ready', ready + ' 个可用会话');
    text('#stat-account-note', result.accounts.length ? ready + ' 个就绪 · ' + (result.accounts.length - ready) + ' 个未就绪' : '尚未添加账号');
    text('#stat-requests', result.stats.total.toLocaleString());
    text('#stat-rate', result.stats.total ? (result.stats.success/result.stats.total*100).toFixed(1) : '—');
    text('#rate-unit', result.stats.total ? '%' : '');
    text('#stat-success-note', result.stats.total ? result.stats.success + ' 条成功 / ' + result.stats.total + ' 条尝试' : '等待第一条请求');
    text('#stat-latency', result.stats.success ? (result.stats.avg_ms/1000).toFixed(2) : '—');
    const cache = result.cache_stats;
    text('#stat-cache-rate', (100 * (cache?.hit_rate ?? 0)).toFixed(1).replace(/\.0$/, ''));
    text('#stat-cache-note', '命中 ' + (cache?.hits ?? 0) + ' 次 · 活跃会话 ' + (cache?.active_sessions ?? 0) + ' 个');
    text('#version', 'v' + result.version); text('#connection-label', '服务已连接');
    $('#connection-dot').classList.add('green'); $('#connection-dot').style.removeProperty('background');
    renderAccounts();
    renderLogAccounts();
    if (!state.quotaRefreshing && state.accounts.some(a => a.configured && (!a.quota?.updated_at || Date.now()/1000-a.quota.updated_at>300))) {
      refreshQuotaCache();
    }
    if (state.page === 'overview') await Promise.all([loadAnalytics(), loadSessions()].map(load => load.catch(error => toast(error.message))));
    if (state.page === 'logs') await loadLogs().catch(error => toast(error.message));
  } catch (error) { text('#connection-label', '连接中断'); if (state.admin) toast(error.message); }
  finally { state.refreshing = false; $('#refresh-icon').classList.remove('spin'); }
}
async function refreshQuotaCache() {
  state.quotaRefreshing = true;
  const pending = state.accounts.filter(a => a.configured && (!a.quota?.updated_at || Date.now()/1000-a.quota.updated_at>300));
  async function worker() {
    while (pending.length && state.admin) {
      const account = pending.shift();
      try { account.quota = await api(`/api/accounts/${encodeURIComponent(account.id)}/quota/refresh`,{method:'POST'}); }
      catch { account.quota = {status:'unavailable',reason:'额度读取失败',updated_at:Date.now()/1000}; }
    }
  }
  try { await Promise.all([worker(),worker()]); renderAccounts(); }
  finally { state.quotaRefreshing = false; }
}
function tableRow(...values) {
  const row = document.createElement('tr');
  for (const value of values) {
    const cell = row.insertCell();
    if (value instanceof Node) cell.append(value);
    else cell.textContent = value;
  }
  return row;
}
function rowButton(label, action) {
  const button = document.createElement('button');
  button.className = 'button compact';
  button.textContent = label;
  button.addEventListener('click', () => action().catch(error => toast(error.message)));
  return button;
}
function svgNode(tag, attributes, value) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [name, attr] of Object.entries(attributes)) node.setAttribute(name, attr);
  if (value !== undefined) node.textContent = value;
  return node;
}
function renderAnalytics({hourly, models}) {
  if (hourly.length) {
    const width = 384, height = 112, step = width / hourly.length;
    const max = Math.max(1, ...hourly.map(item => item.success + item.failed));
    const chart = svgNode('svg', {viewBox: '0 0 384 134', role: 'group', 'aria-label': '每小时请求数'});
    chart.append(svgNode('line', {x1: 0, x2: width, y1: height, y2: height, stroke: 'var(--border)'}));
    hourly.forEach(({hour_ts, success, failed, cache_hits}, i) => {
      const label = String(new Date(hour_ts * 1000).getHours()).padStart(2, '0') + ':00';
      const tooltip = label + ' · 成功 ' + success + ' · 失败 ' + failed + ' · 命中缓存 ' + cache_hits;
      const group = svgNode('g', {role: 'img', 'aria-label': tooltip, tabindex: 0});
      const good = height * success / max, bad = height * failed / max;
      group.append(
        svgNode('title', {}, tooltip),
        svgNode('rect', {x: step * i, y: 0, width: step, height, fill: 'transparent'}),
        svgNode('rect', {x: step * (i + .15), y: height - good, width: step * .7, height: good, fill: 'var(--green)'}),
        svgNode('rect', {x: step * (i + .15), y: height - good - bad, width: step * .7, height: bad, fill: 'var(--red)'}));
      chart.append(group);
      if (i % Math.ceil(hourly.length / 6) === 0) {
        chart.append(svgNode('text', {x: Math.max(17, (i + .5) * step), y: 130, 'text-anchor': 'middle', 'font-size': 11, fill: 'var(--muted)'}, label));
      }
    });
    $('#hourly-chart').replaceChildren(chart);
  } else text('#hourly-chart', '暂无数据');

  if (!models.length) { text('#model-chart', '暂无数据'); return; }
  const max = Math.max(1, ...models.map(item => item.total));
  $('#model-chart').replaceChildren(...models.map(({model, total, success, avg_ms}) => {
    const item = document.createElement('div');
    item.className = 'model-distribution';
    const label = document.createElement('strong');
    label.textContent = model;
    const stats = document.createElement('span');
    stats.className = 'muted';
    const rate = total ? (100 * success / total).toFixed(1) : '0';
    stats.textContent = total + ' 次 · 成功率 ' + rate + '% · 平均 ' + avg_ms + ' ms';
    const bar = svgNode('svg', {viewBox: '0 0 100 5', preserveAspectRatio: 'none', 'aria-hidden': 'true'});
    bar.append(svgNode('rect', {width: 100, height: 5, fill: 'var(--surface-2)'}),
      svgNode('rect', {width: total / max * 100, height: 5, fill: 'var(--orange)'}));
    item.append(label, stats, bar);
    return item;
  }));
}
async function loadAnalytics() { renderAnalytics(await api('/api/analytics?hours=24')); }
async function loadSessions() {
  const {sessions} = await api('/api/sessions');
  $('#sessions-body').replaceChildren(...sessions.map(({thread_id, account_name, turns, remaining_sec}) => {
    const clean = rowButton('清理', async () => {
      await api('/api/sessions/' + encodeURIComponent(thread_id), {method: 'DELETE'});
      await loadSessions();
    });
    const remaining = Math.floor(remaining_sec / 60) + ':' + String(remaining_sec % 60).padStart(2, '0');
    return tableRow(thread_id.slice(0, 8), account_name, turns, remaining, clean);
  }));
  $('#sessions-table').hidden = !sessions.length;
  $('#sessions-empty').hidden = !!sessions.length;
  $('#clear-sessions').disabled = !sessions.length;
}
function renderSettings({scheduling_strategy, session_cache_ttl, max_attempts, max_images = 50, max_messages = 2000, max_body_mb = 20, smart_default_effort = 'high'}) {
  $('#scheduling-strategy').value = scheduling_strategy;
  $('#session-cache-ttl').value = session_cache_ttl;
  $('#max-attempts').value = max_attempts;
  $('#max-images').value = max_images;
  $('#max-messages').value = max_messages;
  $('#max-body-mb').value = max_body_mb;
  $('#smart-default-effort').value = smart_default_effort;
  $('#settings-form button').disabled = false;
}
async function loadSettings() { renderSettings(await api('/api/settings')); }

function renderKeys() {
  $('#keys-body').replaceChildren(...state.keys.map(key => {
    const name = document.createElement('div');
    name.textContent = key.name;
    if (key.notes) {
      const note = document.createElement('span');
      note.className = 'secondary key-note'; note.textContent = key.notes;
      name.append(note);
    }
    const badge = document.createElement('span');
    const expired = key.expires_at !== null && key.expires_at <= Date.now() / 1000;
    badge.className = 'status ' + (key.enabled ? (expired ? 'expired' : 'ready') : 'disabled');
    badge.textContent = key.enabled ? (expired ? '已过期' : '已启用') : '已停用';
    const path = '/api/keys/' + encodeURIComponent(key.id);
    const actions = document.createElement('div');
    actions.className = 'key-actions';
    actions.append(
      rowButton(key.enabled ? '停用' : '启用', async () => {
        const updated = await api(path, {method: 'PATCH', body: JSON.stringify({enabled: !key.enabled})});
        state.keys = state.keys.map(item => item.id === key.id ? updated : item);
        renderKeys();
      }),
      rowButton('编辑', async () => openKeyEditor(key)),
      rowButton('删除', async () => {
        if (!confirm('删除「' + key.name + '」？此密钥将无法再发起请求。')) return;
        await api(path, {method: 'DELETE'});
        state.keys = state.keys.filter(item => item.id !== key.id);
        renderKeys();
      }));
    return tableRow(name, key.key_prefix, key.group_name || '全部账号', key.requests_today + ' · ' + key.requests_total,
      key.daily_limit === 0 ? '不限' : key.daily_limit, key.expires_at === null ? '永不' : formatTime(key.expires_at), badge, actions);
  }));
  $('#keys-empty').hidden = !!state.keys.length;
  for (const [id, main] of [['#example-key', '主 Key'], ['#log-key', '全部 API Key']]) {
    const select = $(id);
    const value = select.value;
    select.replaceChildren(new Option(main, ''), ...state.keys.map(key => new Option(key.name + ' · ' + key.key_prefix, key.id)));
    if (state.keys.some(key => String(key.id) === value)) select.value = value;
  }
  if (logFilters.has('api_key_id') && !state.keys.some(key => String(key.id) === logFilters.get('api_key_id'))) {
    logFilters.delete('api_key_id');
    logOffset = 0;
    if (state.page === 'logs') loadLogs().catch(error => toast(error.message));
  }
  renderCode();
}
async function loadKeys() {
  const {keys} = await api('/api/keys');
  state.keys = keys;
  renderKeys();
}
function openKeyEditor(key) {
  const dialog = $('#key-dialog');
  $('#key-form').reset();
  const groups = accountGroups().filter(Boolean);
  if (key?.group_name && !groups.includes(key.group_name)) groups.push(key.group_name);
  $('#key-group').replaceChildren(new Option('全部账号', ''), ...groups.map(group => new Option(group)));
  dialog.dataset.id = key ? key.id : '';
  text('#key-dialog-title', key ? '编辑 API Key' : '创建 API Key');
  text('#save-key', key ? '保存修改' : '创建密钥');
  $('#key-quick-expiry').hidden = !!key;
  $('#key-exact-expiry').hidden = !key;
  if (key) {
    $('#key-name').value = key.name;
    $('#key-notes').value = key.notes;
    $('#key-group').value = key.group_name;
    $('#key-limit').value = key.daily_limit;
    if (key.expires_at !== null) {
      const date = new Date(key.expires_at * 1000);
      $('#key-expiry-at').value = new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
    }
  }
  dialog.showModal();
}

async function loadConfig() {
  state.config = await api('/api/config');
  const options = state.config.models || ['amazon-fast'];
  for (const id of ['#playground-model', '#example-model']) $(id).replaceChildren(...options.map(model => new Option(model)));
  renderCode();
}
async function apiKey() { return (await api('/api/config/reveal-key', {method:'POST'})).api_key; }
function shellQuote(value) { return "'" + value.replace(/'/g, "'\\''") + "'"; }
function renderCode() {
  text('#endpoint-label', baseURL); text('#config-url', baseURL);
  if (!state.keyVisible) text('#config-key', state.config?.api_key_masked || '••••••••••••••••••••••••');
  const selected = $('#example-key').value;
  const apiKeyMeta = state.keys.find(key => String(key.id) === selected);
  const key = apiKeyMeta ? '<粘贴 ' + apiKeyMeta.name + ' 的密钥 ' + apiKeyMeta.key_prefix + '>' : $('#config-key').textContent;
  const model = $('#example-model').value;
  const env = {ANTHROPIC_BASE_URL: location.origin, ANTHROPIC_AUTH_TOKEN: key, ANTHROPIC_MODEL: model};
  const claudeExports = Object.entries(env).map(([name, value]) => 'export ' + name + '=' + shellQuote(value)).join('\n');
  const examples = {
    claude: claudeExports + '\n\n# ~/.claude/settings.json\n' + JSON.stringify({env}, null, 2),
    codex: ['# ~/.codex/config.toml', 'model = ' + JSON.stringify(model), 'model_provider = "quickpool"', '',
      '[model_providers.quickpool]', 'name = "QuickPool"', 'base_url = ' + JSON.stringify(baseURL),
      'wire_api = "responses"', 'env_key = "QUICKPOOL_API_KEY"', '', '# 在终端执行',
      'export QUICKPOOL_API_KEY=' + shellQuote(key)].join('\n'),
    python: `from openai import OpenAI\n\nclient = OpenAI(\n    base_url="${baseURL}",\n    api_key=${JSON.stringify(key)},\n)\n\nstream = client.chat.completions.create(\n    model=${JSON.stringify(model)},\n    messages=[\n        {"role": "user", "content": "你好"}\n    ],\n    stream=True,\n)\nfor chunk in stream:\n    print(chunk.choices[0].delta.content or "", end="")`,
    curl: `curl ${baseURL}/chat/completions \\\n  -H ${shellQuote('Authorization: Bearer ' + key)} \\\n  -H "Content-Type: application/json" \\\n  -d ${shellQuote(JSON.stringify({model, messages: [{role: 'user', content: '你好'}], stream: true}, null, 2))}`};
  text('#code-example', examples[state.code]);
}
function logRows(requests, showAccount) {
  return requests.map(log => {
    const id = document.createElement('span');
    id.textContent = (log.id?.slice(0,18) ?? '') + (log.id?.length > 18 ? '…' : '');
    const time = document.createElement('span');
    time.className = 'secondary'; time.textContent = formatTime(log.created_at);
    const request = document.createElement('span');
    request.append(id, time);
    const model = document.createElement('span');
    model.textContent = log.model ?? '—';
    if (log.session_hit) {
      const hit = document.createElement('span');
      hit.className = 'status ready'; hit.textContent = '复用';
      model.append(' ', hit);
    }
    const status = document.createElement('span');
    status.className = 'status ' + (log.status === 200 ? 'ready' : 'expired');
    status.textContent = log.status ?? '—';
    const cells = [request];
    if (showAccount) cells.push(log.account_name ?? '已删除账号');
    cells.push(model, status, log.duration_ms == null ? '—' : (log.duration_ms / 1000).toFixed(2) + 's', log.output_chars ?? '—', log.error || '完成');
    const row = tableRow(...cells);
    row.cells[0].setAttribute('title', log.id ?? '');
    for (const index of [0, cells.length - 3, cells.length - 2]) row.cells[index].className = 'mono';
    return row;
  });
}
async function loadLogs() {
  const requestId = ++logRequest;
  const params = new URLSearchParams(logFilters);
  params.set('limit', logLimit);
  params.set('offset', logOffset);
  const result = await api('/api/logs?' + params);
  if (requestId !== logRequest) return;
  const legacy = Array.isArray(result);
  const requests = legacy ? result : result.requests;
  text('#log-count', (legacy ? requests.length : result.total) + ' 条');
  $('#logs-empty').hidden = !!requests.length;
  $('#logs-body').replaceChildren(...logRows(requests, true));
  $('#log-pager').hidden = legacy;
  if (legacy) return;
  const {total, limit, offset} = result;
  logOffset = offset;
  text('#log-page', '第 ' + (Math.floor(offset / limit) + 1) + ' / ' + Math.max(1, Math.ceil(total / limit)) + ' 页 · 共 ' + total + ' 条');
  const prev = $('#log-prev'), next = $('#log-next');
  prev.dataset.offset = Math.max(0, offset - limit); prev.disabled = offset === 0;
  next.dataset.offset = offset + limit; next.disabled = offset + limit >= total;
}
function submitLogQuery() {
  clearTimeout(logQueryTimer);
  logQueryTimer = setTimeout(() => {
    logFilters = new URLSearchParams();
    for (const [name, selector] of Object.entries({account_id: '#log-account', status: '#log-status', model: '#log-model', api_key_id: '#log-key', q: '#log-query'})) {
      const value = $(selector).value.trim();
      if (value) logFilters.set(name, value);
    }
    logOffset = 0;
    loadLogs().catch(error => toast(error.message));
  }, 250);
}

async function openDetail(id, action) {
  const {account, stats, recent_logs, auth_events} = await api(`/api/accounts/${encodeURIComponent(id)}/detail`);
  const dialog = $('#detail-dialog');
  if (action !== 'detail' && (!dialog.open || dialog.dataset.id !== id)) return;
  dialog.dataset.id = id;
  text('#detail-title', account.name ?? '账号详情');
  const fields = [
    ['Provider', account.auth_provider], ['AWS Account ID', account.aws_account_id],
    ['Token 到期', formatTime(account.token_expires_at)], ['Refresh Token', account.has_refresh_token == null ? null : account.has_refresh_token ? '已保存' : '无'],
    ['最近使用', formatTime(account.last_used)],
  ];
  $('#detail-meta').innerHTML = '<div><dt>状态</dt><dd>' + statusBadge(account) + '</dd></div>' + fields.map(([label, value]) =>
    `<div><dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value ?? '—')}</dd></div>`).join('');
  text('#detail-last-error', account.last_error || '—');
  if (action === 'detail' || action === 'save-detail') {
    $('#detail-name').value = account.name ?? '';
    $('#detail-group-name').value = account.group_name ?? '';
    $('#detail-notes').value = account.notes ?? '';
    $('#detail-weight').value = account.weight ?? '';
    $('#detail-concurrency').value = account.max_concurrency ?? '';
  }
  $('#detail-quota').innerHTML = quotaPeriod(account, 'monthly', true) + quotaPeriod(account, 'session', true);
  $('#detail-dialog [data-action="quota"]').disabled = !account.configured;
  $('#detail-dialog [data-action="auth-refresh"]').disabled = !account.configured;
  text('#detail-dialog [data-action="toggle"]', account.enabled ? '停用' : '启用');
  const metrics = [['请求', stats?.total], ['成功', stats?.success],
    ['平均耗时', stats?.avg_ms == null ? null : (stats.avg_ms / 1000).toFixed(2) + 's'], ['会话复用', stats?.session_hits]];
  $('#detail-stats').innerHTML = metrics.map(([label, value]) =>
    `<div><dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value ?? '—')}</dd></div>`).join('');
  if (recent_logs?.length) $('#detail-logs').replaceChildren(...logRows(recent_logs, false));
  else {
    const empty = tableRow('暂无请求');
    empty.cells[0].colSpan = 6; empty.cells[0].className = 'muted';
    $('#detail-logs').replaceChildren(empty);
  }
  $('#detail-events').innerHTML = auth_events?.length ? auth_events.map(event => `<li><time>${escapeHTML(formatTime(event.created_at))}</time>
    <strong>${escapeHTML(event.event)}</strong><pre>${escapeHTML(event.details && typeof event.details === 'object' ? JSON.stringify(event.details, null, 2) : event.details)}</pre></li>`).join('')
    : '<li class="muted">暂无授权事件</li>';
  if (!dialog.open) dialog.showModal();
}

function stopAuthPolling() {
  clearTimeout(state.authTimer); state.authTimer = null; state.authGeneration += 1;
  state.authStarting = false; $('#start-authorization').disabled = false;
  $('#auth-provider').disabled = false; $('#auth-region').disabled = false;
}
function currentAuth(id, generation) {
  return state.admin && $('#login-dialog').open && state.authAccountId === id && state.authGeneration === generation;
}
function setAuthorizationUrl(value) {
  state.authorizationUrl = ''; $('#authorization-link').removeAttribute('href');
  $('#authorization-link').textContent = '正在准备授权链接…'; $('#authorization-link-box').hidden = true;
  $('#copy-authorization-url').disabled = true;
  if (!value) return;
  let url;
  try { url = new URL(value); } catch { throw Error('服务返回了无效的授权链接'); }
  if (url.protocol !== 'https:' || url.username || url.password) throw Error('授权链接必须是 HTTPS 地址');
  state.authorizationUrl = url.href; $('#authorization-link').href = url.href;
  $('#authorization-link').textContent = url.href; $('#authorization-link-box').hidden = false;
  $('#copy-authorization-url').disabled = false;
}
function renderAuthStatus(result) {
  const labels = {none:'尚未授权', awaiting_discovery:'正在发现 Quick 授权服务…',
    awaiting_authorization:'等待你在浏览器中完成授权…', exchanging:'正在交换并保存授权令牌…',
    authorized:'授权已保存。请点击「实际测试」验证完整转发。', failed:'授权未完成，请重新生成链接。',
    expired:'授权已失效，请重新发起授权。'};
  state.authStatus = result.status || 'none';
  text('#login-status', labels[state.authStatus] || '正在确认授权状态…');
  text('#login-error', result.error || '');
  let expiry = '';
  if (result.expires_at) {
    const numeric = Number(result.expires_at);
    const date = new Date(Number.isFinite(numeric) ? numeric * 1000 : result.expires_at);
    if (!Number.isNaN(date.getTime())) expiry = (state.authStatus.startsWith('awaiting_') ? '授权链接到期：' : '访问令牌到期（自动刷新）：') + date.toLocaleString('zh-CN');
  }
  text('#login-expiry', expiry);
  const pending = ['awaiting_discovery', 'awaiting_authorization', 'exchanging'].includes(state.authStatus);
  $('#cancel-authorization').hidden = !pending;
  $('#authorization-test').hidden = state.authStatus !== 'authorized';
  $('#start-authorization').textContent = pending ? '重新生成链接' : '生成授权链接 ↗';
  if (['authorized', 'failed', 'expired', 'none'].includes(state.authStatus)) {
    $('#authorization-link-box').hidden = true;
    $('#copy-authorization-url').disabled = true;
  }
}
async function pollAuthStatus(id, generation) {
  if (!currentAuth(id, generation)) return;
  try {
    const result = await api(`/api/accounts/${encodeURIComponent(id)}/auth/status`);
    if (!currentAuth(id, generation)) return;
    if (result.authorization_url) setAuthorizationUrl(result.authorization_url);
    renderAuthStatus(result);
    if (['authorized', 'failed', 'expired', 'none'].includes(result.status)) {
      stopAuthPolling();
      if (result.status === 'authorized') toast('授权已保存，请点击「实际测试」');
      await refresh(); return;
    }
  } catch (error) {
    if (!currentAuth(id, generation)) return;
    text('#login-error', error.message + '；3 秒后重试状态查询。');
  }
  if (currentAuth(id, generation)) state.authTimer = setTimeout(() => pollAuthStatus(id, generation), 3000);
}
function openAuthorization(account) {
  stopAuthPolling(); state.authAccountId = account.id; state.authStarting = false;
  text('#login-account-name', account.name); text('#login-error', ''); text('#login-expiry', '');
  $('#auth-provider').value = ['enterprise', 'gateway'].includes(account.auth_provider) ? 'enterprise' : 'social';
  $('#auth-region').value = account.region || 'us-east-1';
  $('#start-authorization').disabled = false; $('#cancel-authorization').disabled = false;
  $('#auth-provider').disabled = false; $('#auth-region').disabled = false;
  setAuthorizationUrl(''); renderAuthStatus({status:'none'}); $('#login-dialog').showModal();
  // Resume a pending authorization when this account's dialog is reopened.
  const generation = state.authGeneration;
  pollAuthStatus(account.id, generation);
}
async function accountAction(action, id, button) {
  if (action === 'add') { $('#account-dialog').showModal(); $('#account-name').focus(); return; }
  const dialog = $('#detail-dialog');
  button.disabled = true;
  try {
    const scope = button.dataset.batch;
    if (scope) {
      const ids = scope === 'selected' ? [...state.selected]
        : state.accounts.filter(a => scope === 'all' || accountStatus(a) === 'cooldown').map(a => a.id);
      await batch(action, ids); return;
    }
    const account = state.accounts.find(a => a.id === id);
    if (!account) return;
    if (action === 'detail') {
      await openDetail(id, action); return;
    } else if (action === 'save-detail') {
      await api(`/api/accounts/${encodeURIComponent(id)}`, {method: 'PATCH', body: JSON.stringify({name: $('#detail-name').value.trim(),
        group_name: $('#detail-group-name').value.trim(), notes: $('#detail-notes').value.trim(),
        weight: Number($('#detail-weight').value), max_concurrency: Number($('#detail-concurrency').value)})});
      toast('账号设置已保存');
    } else if (action === 'login') {
      openAuthorization(account); return;
    } else if (action === 'auth-refresh') {
      await api(`/api/accounts/${encodeURIComponent(id)}/auth/refresh`, {method: 'POST'});
      toast('授权令牌已刷新，请点击「测试」验证转发');
    } else if (action === 'quota') {
      const quota = await api(`/api/accounts/${encodeURIComponent(id)}/quota/refresh`, {method:'POST'});
      toast(quota.status === 'available' ? '额度已更新（服务额度分钟，非 token）' : quota.reason);
    } else if (action === 'toggle') {
      await api(`/api/accounts/${encodeURIComponent(id)}`, {method: 'PATCH', body: JSON.stringify({enabled: !account.enabled})});
      toast(account.enabled ? '账号已停用' : '账号已启用');
    } else if (action === 'delete') {
      if (!confirm(`删除「${account.name}」？将删除该账号保存在本地的授权令牌。`)) return;
      await api(`/api/accounts/${encodeURIComponent(id)}`, {method: 'DELETE'});
      if (dialog.open && dialog.dataset.id === id) dialog.close();
      toast('账号已删除');
    } else if (action === 'test') {
      toast('正在向 Quick 发送实际测试请求…');
      const result = await api(`/api/accounts/${encodeURIComponent(id)}/test`, {method: 'POST'});
      toast('测试通过 · ' + (result.duration_ms/1000).toFixed(2) + 's');
    }
    await refresh();
    if (dialog.open && dialog.dataset.id === id) await openDetail(id, action);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}

document.addEventListener('click', event => {
  const action = event.target.closest('[data-action]');
  if (action) { accountAction(action.dataset.action, action.dataset.id ?? action.closest('#detail-dialog')?.dataset.id, action); return; }
  const close = event.target.closest('[data-close]');
  if (close) { $('#' + close.dataset.close).close(); return; }
  const page = event.target.closest('[data-page], [data-goto]');
  if (page) changePage(page.dataset.page || page.dataset.goto);
});
$('#auth-dialog').addEventListener('cancel', event => event.preventDefault());
$('#auth-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  state.admin = $('#admin-key').value.trim();
  try {
    await loadConfig(); sessionStorage.setItem('quickpool-admin', state.admin); $('#auth-dialog').close();
    $('#admin-key').value = ''; text('#auth-error', ''); await refresh();
  } catch (error) { text('#auth-error', error.message); }
  finally { button.disabled = false; }
});
$('#logout').addEventListener('click', () => { stopAuthPolling(); if ($('#login-dialog').open) $('#login-dialog').close(); sessionStorage.removeItem('quickpool-admin'); state.admin = ''; state.config = null; state.keyVisible = false; state.accounts = []; state.selected.clear(); renderAccounts(); renderLogAccounts(); renderCode(); showAuth(); });
$('#refresh').addEventListener('click', refresh);
$('#add-account').addEventListener('click', () => { $('#account-dialog').showModal(); $('#account-name').focus(); });
$('#account-search').addEventListener('input', renderAccounts);
$('#account-status').addEventListener('change', event => { state.accountStatus = event.target.value; renderAccounts(); });
$('#account-group').addEventListener('change', renderAccounts);
$('#account-sort').addEventListener('change', event => { state.accountSort = event.target.value; renderAccounts(); });
$('#all-accounts').addEventListener('change', event => {
  const boxes = event.target.id === 'select-all-accounts' ? $$('[data-select-account]') : [event.target];
  for (const box of boxes) {
    if (event.target.checked) state.selected.add(box.dataset.selectAccount);
    else state.selected.delete(box.dataset.selectAccount);
  }
  renderSelection();
});
$('#clear-selection').addEventListener('click', () => { state.selected.clear(); renderSelection(); });
$('#detail-form').addEventListener('submit', event => {
  event.preventDefault();
  accountAction('save-detail', $('#detail-dialog').dataset.id, event.submitter);
});
$('#clear-sessions').addEventListener('click', async () => {
  if (!confirm('清空全部粘性会话缓存？')) return;
  try {
    const {cleared} = await api('/api/sessions', {method: 'DELETE'});
    toast('已清理 ' + cleared + ' 个会话');
    await loadSessions();
  } catch (error) { toast(error.message); }
});
$('#settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter;
  const settings = {scheduling_strategy: $('#scheduling-strategy').value, session_cache_ttl: Number($('#session-cache-ttl').value),
    max_attempts: Number($('#max-attempts').value), max_images: Number($('#max-images').value),
    max_messages: Number($('#max-messages').value), max_body_mb: Number($('#max-body-mb').value),
    smart_default_effort: $('#smart-default-effort').value};
  button.disabled = true;
  try {
    renderSettings(await api('/api/settings', {method: 'PATCH', body: JSON.stringify(settings)}));
    toast('全局调度设置已保存');
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
$('#create-key').addEventListener('click', () => openKeyEditor());
$('#key-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter;
  const id = $('#key-dialog').dataset.id;
  const days = Number($('#key-expiry-days').value);
  const exact = $('#key-expiry-at').value;
  const expires_at = id ? (exact ? new Date(exact).getTime() / 1000 : null) : (days ? Math.floor(Date.now() / 1000) + days * 86400 : null);
  const body = {name: $('#key-name').value.trim(), notes: $('#key-notes').value.trim(), group_name: $('#key-group').value,
    daily_limit: Number($('#key-limit').value), expires_at};
  button.disabled = true;
  try {
    if (id) {
      const key = await api('/api/keys/' + encodeURIComponent(id), {method: 'PATCH', body: JSON.stringify(body)});
      state.keys = state.keys.map(item => String(item.id) === id ? key : item);
      $('#key-dialog').close();
      toast('密钥设置已保存');
    } else {
      const {plaintext_key, ...key} = await api('/api/keys', {method: 'POST', body: JSON.stringify(body)});
      state.keys.push(key);
      $('#key-dialog').close();
      text('#plaintext-key', plaintext_key);
      $('#key-result').showModal();
    }
    renderKeys();
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
$('#copy-created-key').addEventListener('click', () => copy($('#plaintext-key').textContent));
$('#key-result').addEventListener('close', () => text('#plaintext-key', ''));
$('#log-filters').addEventListener('submit', event => { event.preventDefault(); submitLogQuery(); });
for (const id of ['#log-account', '#log-status', '#log-model', '#log-key']) $(id).addEventListener('change', submitLogQuery);
for (const id of ['#log-prev', '#log-next']) $(id).addEventListener('click', event => {
  logOffset = Number(event.currentTarget.dataset.offset);
  loadLogs().catch(error => toast(error.message));
});
$('#export-logs').addEventListener('click', async () => {
  try {
    const response = await fetch('/api/logs/export?' + logFilters, {headers: {Authorization: 'Bearer ' + state.admin}});
    if (!response.ok) {
      const {detail} = await response.json();
      throw Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url; link.download = 'quickpool-logs.csv'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { toast(error.message); }
});
$('#account-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true; text('#account-error', '');
  try {
    await api('/api/accounts', {method: 'POST', body: JSON.stringify({name: $('#account-name').value.trim(),
      group_name: $('#account-group-name').value.trim(), notes: $('#account-notes').value.trim(),
      login_url: $('#login-url').value.trim(), weight: Number($('#account-weight').value), max_concurrency: Number($('#account-concurrency').value)})});
    $('#account-dialog').close();
    for (const field of ['#account-name', '#account-group-name', '#account-notes']) $(field).value = '';
    toast('账号已创建，点击「授权」连接 Quick');
    await refresh(); changePage('accounts');
  } catch (error) { text('#account-error', error.message); }
  finally { button.disabled = false; }
});
$('#authorization-form').addEventListener('submit', async event => {
  event.preventDefault(); if (state.authStarting || !state.authAccountId) return;
  stopAuthPolling(); const id = state.authAccountId; const generation = state.authGeneration;
  state.authStarting = true; const button = $('#start-authorization'); button.disabled = true;
  $('#auth-provider').disabled = true; $('#auth-region').disabled = true;
  text('#login-error', ''); setAuthorizationUrl(''); text('#login-status', '正在准备原生 PKCE 授权…');
  try {
    const result = await api(`/api/accounts/${encodeURIComponent(id)}/login`, {method: 'POST',
      body: JSON.stringify({provider: $('#auth-provider').value, region: $('#auth-region').value.trim() || 'us-east-1'})});
    if (!currentAuth(id, generation)) return;
    setAuthorizationUrl(result.authorization_url); renderAuthStatus(result);
    if (['awaiting_discovery', 'awaiting_authorization', 'exchanging'].includes(result.status)) {
      state.authTimer = setTimeout(() => pollAuthStatus(id, generation), 3000);
    }
  } catch (error) { if (currentAuth(id, generation)) { renderAuthStatus({status:'failed'}); text('#login-error', error.message); } }
  finally {
    if (state.authAccountId === id && state.authGeneration === generation) {
      state.authStarting = false; button.disabled = false;
      $('#auth-provider').disabled = false; $('#auth-region').disabled = false;
    }
  }
});
$('#cancel-authorization').addEventListener('click', async event => {
  const id = state.authAccountId; if (!id) return;
  stopAuthPolling(); const generation = state.authGeneration; const button = event.currentTarget; button.disabled = true;
  try {
    await api(`/api/accounts/${encodeURIComponent(id)}/auth/cancel`, {method:'POST'});
    if (currentAuth(id, generation)) { setAuthorizationUrl(''); renderAuthStatus({status:'none'}); text('#login-status', '已取消本次授权，可重新生成链接。'); }
  } catch (error) { if (currentAuth(id, generation)) text('#login-error', error.message); }
  finally { button.disabled = false; }
});
$('#login-dialog').addEventListener('close', stopAuthPolling);
$('#copy-authorization-url').addEventListener('click', () => state.authorizationUrl && copy(state.authorizationUrl));
$('#authorization-test').addEventListener('click', event => accountAction('test', state.authAccountId, event.currentTarget));
$('#copy-endpoint').addEventListener('click', () => copy(baseURL));
$('#copy-config-url').addEventListener('click', () => copy(baseURL));
$('#copy-key').addEventListener('click', async () => { try { await copy(await apiKey()); } catch(error) { toast(error.message); } });
$('#reveal-key').addEventListener('click', async () => {
  try {
    state.keyVisible = !state.keyVisible;
    if (state.keyVisible) text('#config-key', await apiKey());
    $('#reveal-key').setAttribute('aria-label', state.keyVisible ? '隐藏 API Key' : '显示 API Key'); renderCode();
  } catch(error) { state.keyVisible = false; renderCode(); toast(error.message); }
});
for (const id of ['#example-key', '#example-model']) $(id).addEventListener('change', renderCode);
$('#copy-example').addEventListener('click', () => copy($('#code-example').textContent));
$$('[data-code]').forEach(button => button.addEventListener('click', () => { state.code = button.dataset.code; $$('[data-code]').forEach(b => b.classList.toggle('active', b === button)); renderCode(); }));
$('#playground-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true; $('#playground-output').hidden = false;
  text('#playground-output', ''); text('#playground-status', '正在等待 Quick 回复…'); const start = performance.now();
  try {
    if (!state.config) throw Error('请先登录管理界面');
    const response = await fetch('/v1/chat/completions', {method: 'POST', headers: {'Content-Type':'application/json',
      'Authorization': 'Bearer ' + await apiKey()}, body: JSON.stringify({model:$('#playground-model').value, stream:true,
      messages:[{role:'user',content:$('#playground-input').value}]})});
    if (!response.ok) { const error = await response.json(); throw Error(error.error?.message || error.detail || '请求失败'); }
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''; let done = false;
    while (true) {
      const part = await reader.read(); buffer += decoder.decode(part.value || new Uint8Array(), {stream:!part.done});
      let boundary;
      while ((boundary = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0,boundary); buffer = buffer.slice(boundary+2);
        for (const line of frame.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6);
          if (data === '[DONE]') { done = true; continue; }
          const item = JSON.parse(data); if (item.error) throw Error(item.error.message);
          $('#playground-output').textContent += item.choices?.[0]?.delta?.content || '';
        }
      }
      if (part.done) break;
    }
    if (!done) throw Error('响应流提前结束，请检查请求日志');
    text('#playground-status', '完成 · ' + ((performance.now()-start)/1000).toFixed(2) + 's');
  } catch (error) { text('#playground-status', '请求失败'); $('#playground-output').textContent += '\n' + error.message; }
  finally { button.disabled = false; await refresh(); }
});
['export','import'].forEach(action => $('#'+action+'-accounts').addEventListener('click', () => {
  $('#transfer-form').reset(); $('#transfer-action').value = action;
  text('#transfer-title', action === 'export' ? '加密导出账号' : '导入账号');
  $('#transfer-file-row').hidden = action === 'export'; $('#transfer-file').required = action === 'import';
  text('#transfer-error',''); $('#transfer-dialog').showModal();
}));
$('#transfer-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  const action = $('#transfer-action').value; const passphrase = $('#transfer-passphrase').value;
  try {
    const body = {passphrase};
    if (action === 'import') {
      const file = $('#transfer-file').files[0];
      if (!file || file.size > 4*1024*1024) throw Error('请选择不超过 4 MiB 的加密迁移文件');
      body.bundle = JSON.parse(await file.text());
    }
    const result = await api('/api/accounts/'+action,{method:'POST',body:JSON.stringify(body)});
    if (action === 'export') {
      const url = URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'}));
      const a = document.createElement('a'); a.href = url; a.download = 'accounts.quickpool.json'; a.click();
      setTimeout(() => URL.revokeObjectURL(url),1000); toast('已导出加密账号文件');
    } else { toast('已导入 '+result.imported.length+' 个账号，请逐个实际测试'); await refresh(); }
    $('#transfer-form').reset(); $('#transfer-dialog').close();
  } catch(error) { text('#transfer-error',error.message); }
  finally { $('#transfer-passphrase').value = ''; button.disabled = false; }
});
renderCode(); renderAccounts();
if (state.admin) { loadConfig().then(refresh).catch(() => showAuth()); } else showAuth();
function setAutoRefresh() {
  clearInterval(refreshTimer);
  const seconds = Number($('#auto-refresh').value);
  if (seconds) refreshTimer = setInterval(() => {
    if (!document.hidden && !$('dialog[open]')) refresh();
  }, seconds * 1000);
}
$('#auto-refresh').addEventListener('change', setAutoRefresh);
setAutoRefresh();

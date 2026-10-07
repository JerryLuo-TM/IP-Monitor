const $ = (id) => document.getElementById(id);
const form = $('settings');
let token = '', initialized = false, latest = null, connected = false;
let eventFilter = 'all', toastTimer, dirty = false;
const titles = {
  overview: ['监控总览', '每一次出口变化，尽在掌握。', '电信与联通双线观察，分别记录公网出口与网段状态。'],
  events: ['事件记录', '变化有迹可循。', '从出口切换到邮件送达，查看每一条重要动态。'],
  settings: ['监控设置', '按你的节奏，保持关注。', '调整采样策略与邮件通知，保存后即刻应用。']
};
const fmt = (ts) => ts ? new Date(ts * 1000).toLocaleString('zh-CN', {hour12: false}) : '—';
function notify(message, error = false) {
  clearTimeout(toastTimer); $('feedback').textContent = message;
  $('feedback').className = 'toast' + (error ? ' error' : ''); $('feedback').hidden = false;
  toastTimer = setTimeout(() => { $('feedback').hidden = true; }, 5000);
}
function showPage(name) {
  if (!Object.hasOwn(titles, name)) name = 'overview';
  for (const page of Object.keys(titles)) $('page-' + page).hidden = page !== name;
  document.querySelectorAll('[data-page]').forEach(button => {
    const active = button.dataset.page === name; button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  [$('breadcrumb').textContent, $('page-title').textContent, $('page-subtitle').textContent] = titles[name];
}
document.querySelectorAll('[data-page], [data-go]').forEach(button => {
  button.onclick = () => { location.hash = button.dataset.page || button.dataset.go; };
});
addEventListener('hashchange', () => showPage(location.hash.slice(1)));
showPage(location.hash.slice(1));
function markDirty(value) {
  dirty = value; $('save-status').textContent = value ? '有未保存的更改' : '设置已同步';
  $('dirty-dot').classList.toggle('dirty', value);
}
form.addEventListener('input', () => markDirty(true));
form.addEventListener('change', () => markDirty(true));
addEventListener('beforeunload', (event) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
async function api(path, body) {
  const options = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token}, body: JSON.stringify(body)};
  const response = await fetch(path, {...options, signal: AbortSignal.timeout(10000)});
  if (response.status === 401) throw new Error('登录已失效，请刷新页面');
  const data = await response.json(); if (!response.ok) throw new Error(data.error || '请求失败'); return data;
}
function icon(name, className) {
  const span = document.createElement('span'); span.className = className;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + name); svg.append(use); span.append(svg); return span;
}
function category(row) {
  if (/失败|异常|故障/.test(row.kind)) return 'error';
  if (/邮件/.test(row.kind)) return 'mail'; if (/IP/.test(row.kind)) return 'ip'; return 'other';
}
function renderRows(target, rows, filtered = false) {
  target.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement('div'); empty.className = 'empty-state'; empty.append(icon('activity', ''));
    const title = document.createElement('b'); title.textContent = filtered ? '没有匹配的记录' : '等待第一条动态';
    const help = document.createElement('p'); help.textContent = filtered ? '换一个关键词，或查看全部事件。' : '出口变化和通知结果将在这里按时间呈现。';
    empty.append(title, help); target.append(empty); return;
  }
  for (const row of rows) {
    const kind = category(row); const entry = document.createElement('div'); entry.className = 'event-row';
    entry.append(icon(kind === 'mail' ? 'mail' : kind === 'error' ? 'shield' : 'activity', 'event-icon ' + kind));
    for (const [tag, value] of [['b', row.kind], ['p', row.message], ['time', fmt(row.time)]]) {
      const child = document.createElement(tag); child.textContent = value; entry.append(child);
    }
    target.append(entry);
  }
}
function renderEvents() {
  if (!latest) return;
  const search = $('event-search').value.trim().toLowerCase();
  const rows = latest.status.history.filter(row => (eventFilter === 'all' || category(row) === eventFilter)
    && `${row.kind} ${row.message}`.toLowerCase().includes(search));
  renderRows($('history'), rows, eventFilter !== 'all' || !!search); $('events-total').textContent = rows.length + ' 条记录';
}
document.querySelectorAll('[data-filter]').forEach(button => {
  button.onclick = () => {
    eventFilter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(item => {
      item.classList.toggle('active', item === button); item.setAttribute('aria-pressed', String(item === button));
    }); renderEvents();
  };
});
$('event-search').addEventListener('input', renderEvents);
function countdown() {
  if (!connected || !latest) { $('next').textContent = '—'; $('next-label').textContent = '等待同步'; return; }
  const {status: s, config: c} = latest;
  const seconds = s.next_check ? Math.max(0, Math.ceil(s.next_check - Date.now()/1000)) : 0;
  $('next').textContent = s.busy ? '···' : String(seconds);
  $('next-label').textContent = s.busy ? '正在检测 / 发送' : seconds ? '秒后再次检测' : '即将执行';
  $('ring-progress').setAttribute('stroke-dashoffset', String(320.44 * (1 - Math.min(1, seconds / c.CHECK_INTERVAL))));
}
function render(data) {
  latest = data; const {status: s, config: c, csrf} = data; token = csrf;
  $('demo-banner').hidden = !s.history.some(row => row.kind.includes('演示'));
  $('connection-text').textContent = '面板已连接'; $('connection').className = 'connection';
  $('ip').textContent = s.ip || '—'; $('copy-ip').disabled = !s.ip;
  $('line-name').textContent = c.MONITOR_NAME; $('last').textContent = '最近采样 ' + fmt(s.last_success);
  $('ip-location').textContent = s.location ? s.location + (s.error ? '（上次成功采样）' : '') : s.ip ? '查询服务未提供归属地' : '等待获取归属地';
  $('risk').textContent = s.error ? '检测异常 / 待检测' : s.risk === null ? '等待检测' : s.risk ? '命中风险网段' : '出口未命中风险网段';
  $('risk').className = 'status-chip ' + (s.error || s.risk === null ? 'neutral' : s.risk ? 'risk' : 'ok');
  $('error').textContent = s.error || (s.risk ? '当前出口命中关注网段，请留意上行表现。' : '持续观察公网出口，出现风险时按策略提醒。');
  const u = s.unicom || {error:'请同时更新 web.py 与 monitor.py', ready:false};
  $('unicom-ip').textContent = u.ip || '—'; $('copy-unicom').disabled = !u.ip;
  $('unicom-name').textContent = c.UNICOM_NAME || '联通宽带';
  $('unicom-last').textContent = '最近采样 ' + fmt(u.last_success);
  $('unicom-location').textContent = u.location ? u.location + (u.error || !u.ready ? '（上次成功采样）' : '') : u.ip ? '查询服务未提供归属地' : '等待获取归属地';
  $('unicom-risk').textContent = !u.ready ? '等待配置分流' : u.error ? '检测异常 / 待检测' : u.risk === null ? '等待检测' : u.risk ? '偏离预期网段' : '符合预期公网网段';
  $('unicom-risk').className = 'status-chip ' + (!u.ready || u.error || u.risk === null ? 'neutral' : u.risk ? 'risk' : 'ok');
  $('unicom-error').textContent = u.error || (u.risk ? '当前地址不在 119.4.0.0/16，请核对线路与运营商分配。' : '预期网段 119.4.0.0/16 · 分别记录联通出口变化');
  $('dual-state').textContent = c.DUAL_ROUTING_READY ? '双线采样已启用 · 检查配置 ↗' : '待配置双线分流 ↗';
  $('interval').textContent = c.CHECK_INTERVAL; $('confirm-count').textContent = c.CONFIRM_COUNT; $('smtp-port').textContent = c.SMTP_PORT;
  $('reminder').textContent = c.REMIND_INTERVAL === 0 ? '已关闭' : c.REMIND_INTERVAL % 3600 === 0 ? `每 ${c.REMIND_INTERVAL / 3600} 小时` : `每 ${c.REMIND_INTERVAL} 秒`;
  $('change-count').textContent = s.history.filter(row => /IP.*变化/.test(row.kind)).length;
  $('mail-enabled').textContent = c.MAIL_ENABLED ? '已开启' : '未开启'; $('mail').textContent = s.mail_status;
  $('check').disabled = s.busy; $('test').disabled = s.busy;
  document.querySelectorAll('.rule-state').forEach(el => { el.textContent = s.error ? '待确认' : s.risk === null ? '待采样' : '监测中'; el.classList.toggle('unknown', !!s.error || s.risk === null); });
  if (!initialized) {
    for (const [key, value] of Object.entries(c)) {
      const input = form.elements.namedItem(key); if (!input) continue;
      if (input.type === 'checkbox') input.checked = value; else input.value = value;
    } initialized = true;
  }
  $('secret').textContent = c.PASSWORD_SET ? '已保存授权码；留空保留，填入新值替换' : '尚未设置授权码';
  $('nav-count').textContent = s.history.length; $('recent-count').textContent = s.history.length;
  renderRows($('recent-history'), s.history.slice(0, 4)); renderEvents(); countdown();
}
async function refresh() {
  try { const data = await api('/api/status'); connected = true; render(data); }
  catch {
    connected = false; $('connection-text').textContent = '连接中断'; $('connection').className = 'connection offline';
    $('risk').textContent = '当前状态未知'; $('risk').className = 'status-chip neutral';
    $('error').textContent = '无法连接监控服务，当前显示的是旧采样。';
    $('unicom-risk').textContent = '当前状态未知'; $('unicom-risk').className = 'status-chip neutral';
    $('unicom-error').textContent = '无法连接监控服务，当前显示的是旧采样。';
    if (latest) {
      $('ip-location').textContent = latest.status.location ? latest.status.location + '（上次成功采样）' : '归属地待同步';
      $('unicom-location').textContent = latest.status.unicom?.location ? latest.status.unicom.location + '（上次成功采样）' : '归属地待同步';
    }
    $('check').disabled = true; $('test').disabled = true;
    document.querySelectorAll('.rule-state').forEach(el => { el.textContent = '待同步'; el.classList.add('unknown'); }); countdown();
  }
}
async function action(path, body, message, button) {
  if (button) button.disabled = true;
  try { await api(path, body); notify(message); await refresh(); return true; }
  catch (error) { notify(error.message, true); return false; }
  finally { if (button && button.id === 'save-button') button.disabled = false; else if (button) button.disabled = !connected || !!latest?.status.busy; }
}
$('check').onclick = () => action('/api/check', {}, '检测任务已提交，结果将自动更新', $('check'));
$('test').onclick = () => action('/api/test', {}, '测试邮件已排队，请查看事件记录和收件箱', $('test'));
async function copyIp(value, element) {
  if (!value) return;
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
    else {
      const selection = window.getSelection(); const range = document.createRange(); range.selectNodeContents(element);
      selection.removeAllRanges(); selection.addRange(range);
      if (!document.execCommand('copy')) throw new Error('copy'); selection.removeAllRanges();
    } notify('出口 IP 已复制');
  } catch { notify('请选中出口 IP 后手动复制', true); }
}
$('copy-ip').onclick = () => copyIp(latest?.status.ip, $('ip'));
$('copy-unicom').onclick = () => copyIp(latest?.status.unicom?.ip, $('unicom-ip'));
form.onsubmit = async (event) => {
  event.preventDefault(); const body = {};
  for (const input of form.elements) {
    if (!input.name) continue;
    body[input.name] = input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value;
  }
  if (await action('/api/config', body, '设置已保存，新策略将在下一轮应用', $('save-button'))) {
    form.elements.SMTP_PASSWORD.value = ''; markDirty(false);
  }
};
setInterval(countdown, 1000);
async function poll() { await refresh(); setTimeout(poll, 3000); }
poll();

const DEFAULT_PROXY_MAX_LATENCY = 1500;

const state = {
  status: null,
  tab: 'dashboard',
  accounts: [],
  config: null,
  challenges: [],
  challengeStats: {},
  challengesUpdatedAt: null,
  files: [],
  flags: [],
  logs: [],
  logsTimer: null,
  logsInFlight: false,
  logsPage: { page: 1, pageSize: 50, total: 0 },
  seenLogToasts: new Set(),
  loginAccountIds: [],
  batchAccountIds: [],
  batchAccountSelectionDirty: false,
  proxyTestResults: [],
  proxyTesting: false,
  proxyMaxLatency: DEFAULT_PROXY_MAX_LATENCY,
  configDraft: null,
  submit: { batchChal: '', batchFlag: '', batchMd5: '' },
  modalChallenge: null,
  operationPanel: null,
  expandedUserLists: {},
  importText: '',
  filters: { account: '', source: '', category: '', status: 'all', q: '' },
  flagFilters: { category: '', chal: '', q: '' },
  fileFilters: { account: '', source: '', category: '', chal: '', q: '' },
  busy: {},
  progressJobs: {},
  progressTimers: {},
  progressRemoveTimers: {},
  progressEventIds: {},
  progressDoneHandlers: {},
  realtimeTimers: {}
};

function h(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}

const challengeSources = [
  { value: 'challenge', label: '练武' },
  { value: 'arena', label: '擂台' },
  { value: 'measure', label: '实战' }
];

function challengeSourceValue(item) {
  return String(item?.source || 'challenge');
}

function challengeSourceLabel(source) {
  return challengeSources.find(item => item.value === String(source || 'challenge'))?.label || '练武';
}

function challengeDisplayId(item) {
  const source = challengeSourceValue(item);
  const id = String(item?.id ?? item?.challenge_id ?? '');
  if (!id || id.includes(':')) return id;
  return `${source}:${id}`;
}

function challengeLookup() {
  return new Map(state.challenges.map(item => [String(item.id), item]));
}

function challengeById(id) {
  return state.challenges.find(item => String(item.id) === String(id));
}

function sourceSelectOptions(selected) {
  return `<option value="">全部赛道</option>${challengeSources.map(item => `<option value="${h(item.value)}" ${String(selected) === item.value ? 'selected' : ''}>${h(item.label)}</option>`).join('')}`;
}

function toast(message, type='error') {
  const root = document.getElementById('toast-root');
  const dock = document.querySelector('.progress-dock');
  const bottom = dock ? dock.getBoundingClientRect().bottom + 12 : 88;
  root.style.top = `${Math.max(88, Math.round(bottom))}px`;
  const node = document.createElement('div');
  node.className = `toast ${type}`;
  node.textContent = message;
  root.appendChild(node);
  setTimeout(() => node.remove(), 4600);
}

async function api(path, options={}) {
  const init = {
    method: options.method || 'GET',
    credentials: 'same-origin',
    headers: { 'Accept': 'application/json' }
  };
  if (options.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, init);
  const data = await res.json().catch(() => null);
  if (!res.ok || !data || data.ok === false) {
    const message = data?.error || `请求失败：HTTP ${res.status}`;
    if (res.status === 401 && state.status && !path.includes('/api/status')) {
      state.status.authenticated = false;
      renderAuth();
    }
    throw new Error(message);
  }
  return data.data;
}

async function boot() {
  try {
    state.status = await api('/api/status');
    if (state.status.authenticated) {
      await loadData();
      await restoreProgressJobs();
      renderApp();
    } else {
      renderAuth();
    }
  } catch (err) {
    document.getElementById('app').innerHTML = `<div class="auth-wrap"><div class="auth-card"><h2>服务异常</h2><p class="muted">${h(err.message)}</p></div></div>`;
  }
}

function renderAuth() {
  const setup = state.status?.setup_required;
  document.getElementById('app').innerHTML = `
    <div class="auth-wrap">
      <div class="auth-card">
        <div class="brand"><div class="logo">IS</div><div><h1>ISCC 管理台</h1><p>${setup ? '首次初始化本站登录' : '登录本站管理界面'}</p></div></div>
        <div class="field"><label>本站用户名</label><input id="authUser" autocomplete="username" value="${setup ? 'admin' : ''}" placeholder="admin"></div>
        <div class="field"><label>本站密码</label><input id="authPass" type="password" autocomplete="current-password" placeholder="请输入本站密码"></div>
        <button style="width:100%;margin-top:10px" onclick="${setup ? 'setupLocal()' : 'loginLocal()'}">${setup ? '完成初始化' : '登录'}</button>
      </div>
    </div>`;
}

async function setupLocal() {
  try {
    const username = document.getElementById('authUser').value.trim();
    const password = document.getElementById('authPass').value;
    state.status = await api('/api/setup', { method: 'POST', body: { username, password } });
    toast('初始化完成', 'success');
    await loadData();
    renderApp();
  } catch (err) { toast(err.message); }
}

async function loginLocal() {
  try {
    const username = document.getElementById('authUser').value.trim();
    const password = document.getElementById('authPass').value;
    await api('/api/login', { method: 'POST', body: { username, password } });
    state.status = await api('/api/status');
    toast('登录成功', 'success');
    await loadData();
    renderApp();
  } catch (err) { toast(err.message); }
}

async function logoutLocal() {
  try { await api('/api/logout', { method: 'POST' }); } catch (err) {}
  state.status.authenticated = false;
  renderAuth();
}

async function loadData() {
  const [accounts, config, challengesData, files, flags, logs] = await Promise.all([
    api('/api/accounts'),
    api('/api/config'),
    api('/api/challenges'),
    api('/api/files'),
    api('/api/flags'),
    fetchLogsPage()
  ]);
  state.accounts = accounts;
  pruneLoginSelection();
  pruneBatchSelection();
  state.config = config;
  state.challenges = challengesData.items || [];
  state.challengeStats = challengesData.stats || {};
  state.challengesUpdatedAt = challengesData.updated_at;
  state.files = files;
  state.flags = flags || [];
  applyLogsResponse(logs);
}

async function refreshKinds(kinds, render=true) {
  const uniqueKinds = [...new Set(kinds)];
  await Promise.all(uniqueKinds.map(kind => refreshLocalKind(kind, false)));
  if (render) renderCurrentViewForKinds(uniqueKinds) || renderMainOrApp();
}

async function reloadAndRender() {
  await loadData();
  renderCurrentViewForKinds(['accounts', 'config', 'challenges', 'files', 'flags', 'logs']) || renderMainOrApp();
}

function renderCurrentViewForKinds(kinds=[]) {
  const changed = new Set(kinds);
  if (state.operationPanel) {
    updateProgressDock();
    return true;
  }
  if (state.modalChallenge) {
    renderModalRoots();
    updateProgressDock();
    return true;
  }
  if (state.tab === 'challenges' && (changed.has('challenges') || changed.has('accounts'))) {
    if (changed.has('challenges')) renderMainOrApp();
    else if (!renderChallengesTable()) renderMainOrApp();
    updateProgressDock();
    return true;
  }
  if (state.tab === 'files' && (changed.has('files') || changed.has('challenges') || changed.has('accounts') || changed.has('config'))) {
    if (changed.has('challenges') || changed.has('config')) renderMainOrApp();
    else if (!renderFilesTable()) renderMainOrApp();
    updateProgressDock();
    return true;
  }
  if (state.tab === 'submit' && (changed.has('flags') || changed.has('challenges') || changed.has('accounts') || changed.has('files'))) {
    if (changed.size === 1 && changed.has('flags') && renderFlagManagementTable()) updateProgressDock();
    else renderMainOrApp();
    return true;
  }
  if (state.tab === 'logs' && changed.has('logs')) {
    renderMainOrApp();
    updateProgressDock();
    return true;
  }
  if (state.tab === 'accounts' && changed.has('accounts')) {
    renderMainOrApp();
    updateProgressDock();
    return true;
  }
  if (state.tab === 'dashboard' && ['accounts', 'challenges', 'files'].some(kind => changed.has(kind))) {
    renderMainOrApp();
    updateProgressDock();
    return true;
  }
  updateProgressDock();
  return false;
}

async function refreshLocalKind(kind, render=true) {
  if (kind === 'accounts') {
    state.accounts = await api('/api/accounts');
    pruneLoginSelection();
    pruneBatchSelection();
  } else if (kind === 'config') {
    state.config = await api('/api/config');
  } else if (kind === 'challenges') {
    const data = await api('/api/challenges');
    state.challenges = data.items || [];
    state.challengeStats = data.stats || {};
    state.challengesUpdatedAt = data.updated_at;
  } else if (kind === 'files') {
    state.files = await api('/api/files');
  } else if (kind === 'flags') {
    state.flags = await api('/api/flags');
  } else if (kind === 'logs') {
    await loadLogs(false, true);
  }
  if (!render) return;
  renderCurrentViewForKinds([kind]) || renderMainOrApp();
}

async function refreshRealtimeKinds(kinds) {
  const uniqueKinds = [...new Set(kinds)];
  const nextKinds = new Set(uniqueKinds);
  await Promise.all(uniqueKinds.map(kind => refreshLocalKind(kind, false)));
  if (uniqueKinds.includes('logs') && state.logs.some(log => log.event === 'proxy_removed')) {
    nextKinds.add('config');
    await refreshLocalKind('config', false);
  }
  renderCurrentViewForKinds([...nextKinds]);
}

function startRealtimeRefresh(kinds, interval=1200) {
  const activeKinds = [...new Set(kinds)];
  let changed = false;
  activeKinds.forEach(kind => {
    const entry = state.realtimeTimers[kind];
    if (entry) {
      entry.refs += 1;
      return;
    }
    refreshLocalKind(kind, false).catch(() => {});
    const timerEntry = { refs: 1, timer: null, inFlight: false };
    timerEntry.timer = setInterval(async () => {
      if (timerEntry.inFlight) return;
      timerEntry.inFlight = true;
      try {
        await refreshRealtimeKinds([kind]);
      } catch (err) {
      } finally {
        timerEntry.inFlight = false;
      }
    }, interval);
    state.realtimeTimers[kind] = timerEntry;
    changed = true;
  });
  if (changed) updateProgressDock();
  return activeKinds;
}

function stopRealtimeRefresh(kinds) {
  [...new Set(kinds || [])].forEach(kind => {
    const entry = state.realtimeTimers[kind];
    if (!entry) return;
    entry.refs -= 1;
    if (entry.refs > 0) return;
    clearInterval(entry.timer);
    delete state.realtimeTimers[kind];
  });
}

function newProgressId() {
  return `${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

function progressPercent(p) {
  if (!p || !p.total) return 0;
  return Math.max(0, Math.min(100, Math.round((Number(p.current || 0) / Number(p.total || 1)) * 100)));
}

function progressDockHtml() {
  const jobs = Object.values(state.progressJobs);
  if (!jobs.length) return '';
  return `<div class="progress-dock">${jobs.map(p => {
    const percent = progressPercent(p);
    const statusClass = p.done ? (p.ok === false ? 'bad' : 'good') : 'warn';
    const statusText = p.done ? (p.ok === false ? '失败' : '完成') : '进行中';
    return `<div class="progress-card">
      <div class="progress-card-head"><strong>${h(p.title || '处理中')}</strong><span class="pill ${statusClass}">${statusText}</span></div>
      <p class="muted">${h(p.message || '正在处理，请稍候')}</p>
      <div class="progress-line"><b style="width:${percent}%"></b></div>
      <div class="progress-meta"><span>${h(p.username || '-')}</span><strong>${h(p.current || 0)}/${h(p.total || 0)} ${percent}%</strong></div>
      ${p.done ? '' : `<div class="row-actions" style="margin-top:10px">${p.paused ? `<button class="good" onclick="resumeProgress('${h(p.id)}')">继续</button>` : `<button class="ghost" onclick="pauseProgress('${h(p.id)}')">暂停</button>`}<button class="ghost" onclick="skipProgress('${h(p.id)}')">跳过</button><button class="danger" onclick="cancelProgress('${h(p.id)}')">停止</button></div>`}
    </div>`;
  }).join('')}</div>`;
}

function renderProgressDock() {
  return `<div id="progress-dock-root">${progressDockHtml()}</div>`;
}

function updateProgressDock() {
  const target = document.getElementById('progress-dock-root');
  if (!target) return false;
  target.innerHTML = progressDockHtml();
  return true;
}

function renderForProgressChange() {
  renderProgressOrApp();
}

function showProgressEvents(progressId, events=[]) {
  const seen = state.progressEventIds[progressId] || new Set();
  state.progressEventIds[progressId] = seen;
  events.forEach(event => {
    if (seen.has(event.id)) return;
    seen.add(event.id);
    const message = event.message || (event.ok ? '成功' : '失败');
    const isImportantSuccess = event.ok && /完成|删除|保存|登录完成|提交成功/.test(message);
    if (event.ok && !isImportantSuccess) return;
    const username = event.username ? `${event.username}：` : '';
    toast(`${username}${message}`, event.ok ? 'success' : 'warn');
  });
}

function progressSnapshot(progress) {
  return JSON.stringify({
    current: progress?.current || 0,
    total: progress?.total || 0,
    username: progress?.username || '',
    message: progress?.message || '',
    done: Boolean(progress?.done),
    ok: progress?.ok,
    events: (progress?.events || []).length
  });
}

function watchProgress(progress_id, onDone=null) {
  if (!progress_id) return;
  if (onDone) state.progressDoneHandlers[progress_id] = onDone;
  if (state.progressTimers[progress_id]) return;
  let inFlight = false;
  state.progressTimers[progress_id] = setInterval(async () => {
    if (inFlight) return;
    inFlight = true;
    try {
      const data = await api(`/api/progress/${progress_id}`);
      showProgressEvents(progress_id, data.events || []);
      if ((data.events || []).some(event => event.kind === 'files')) {
        await refreshLocalKind('files', false);
        if (state.tab === 'files') renderFilesTable() || renderMainOrApp();
      }
      const nextProgress = { id: progress_id, ...data };
      if (progressSnapshot(state.progressJobs[progress_id]) !== progressSnapshot(nextProgress)) {
        state.progressJobs[progress_id] = nextProgress;
        renderForProgressChange();
      }
      if (data.done) {
        stopProgress(progress_id, false);
        const doneHandler = state.progressDoneHandlers[progress_id];
        delete state.progressDoneHandlers[progress_id];
        if (doneHandler) await doneHandler(data);
        finishProgressSoon(progress_id);
      }
    } catch (err) {
    } finally {
      inFlight = false;
    }
  }, 500);
}

function startProgress(title, total=0) {
  const progress_id = newProgressId();
  state.progressEventIds[progress_id] = new Set();
  state.progressJobs[progress_id] = { id: progress_id, title, total, current: 0, message: '准备开始', done: false, events: [] };
  renderForProgressChange();
  watchProgress(progress_id);
  return progress_id;
}

async function restoreProgressJobs() {
  const data = await api('/api/progress').catch(() => ({ jobs: [] }));
  (data.jobs || []).filter(job => !job.done).forEach(job => {
    state.progressEventIds[job.id] = new Set((job.events || []).map(event => event.id));
    state.progressJobs[job.id] = { id: job.id, ...job };
    watchProgress(job.id, async () => {
      await refreshKinds(['accounts', 'challenges', 'files', 'flags', 'logs'], true);
    });
  });
}

function stopProgress(progress_id, clear=true) {
  if (!progress_id) return;
  if (state.progressTimers[progress_id]) clearInterval(state.progressTimers[progress_id]);
  delete state.progressTimers[progress_id];
  const job = state.progressJobs[progress_id];
  if (clear && job?.events) showProgressEvents(progress_id, job.events);
  if (clear) {
    if (state.progressRemoveTimers[progress_id]) clearTimeout(state.progressRemoveTimers[progress_id]);
    delete state.progressRemoveTimers[progress_id];
    delete state.progressJobs[progress_id];
    delete state.progressEventIds[progress_id];
    delete state.progressDoneHandlers[progress_id];
    renderForProgressChange();
  }
}

function finishProgressSoon(progress_id) {
  if (!progress_id || state.progressRemoveTimers[progress_id]) return;
  state.progressRemoveTimers[progress_id] = setTimeout(() => stopProgress(progress_id, true), 1300);
}

async function pauseProgress(progress_id) {
  try {
    state.progressJobs[progress_id] = { ...state.progressJobs[progress_id], ...(await api(`/api/progress/${progress_id}/pause`, { method: 'POST' })) };
    renderForProgressChange();
  } catch (err) { toast(err.message); }
}

async function resumeProgress(progress_id) {
  try {
    state.progressJobs[progress_id] = { ...state.progressJobs[progress_id], ...(await api(`/api/progress/${progress_id}/resume`, { method: 'POST' })) };
    renderForProgressChange();
  } catch (err) { toast(err.message); }
}

async function skipProgress(progress_id) {
  try {
    state.progressJobs[progress_id] = { ...state.progressJobs[progress_id], ...(await api(`/api/progress/${progress_id}/skip`, { method: 'POST' })) };
    renderForProgressChange();
  } catch (err) { toast(err.message); }
}

async function cancelProgress(progress_id) {
  try {
    state.progressJobs[progress_id] = { ...state.progressJobs[progress_id], ...(await api(`/api/progress/${progress_id}/cancel`, { method: 'POST' })) };
    renderForProgressChange();
  } catch (err) { toast(err.message); }
}

async function finishOperationRefresh(progress_id, message, type='success') {
  if (message) toast(message, type);
  finishProgressSoon(progress_id);
  await refreshKinds(['accounts', 'challenges', 'files', 'flags'], true);
}

function ensureNotCancelled(data) {
  if (data?.cancelled) throw new Error(data.message || '已停止');
  return data;
}

const pages = {
  dashboard: { label: '总览', render: () => renderDashboardPage() },
  accounts: { label: '账号', render: () => renderAccountsPage() },
  challenges: { label: '题目', render: () => renderChallengesPage() },
  files: { label: '附件', render: () => renderFilesPage() },
  submit: { label: '提交', render: () => renderSubmitPage() },
  logs: { label: '日志', render: () => renderLogsPage() },
  config: { label: '配置', render: () => renderConfigPage() }
};

function setTab(tab) {
  state.tab = pages[tab] ? tab : 'dashboard';
  updateLogsPolling();
  if (!renderActiveNav()) renderApp();
  else renderMainOrApp();
  updateProgressDock();
}

function renderApp() {
  const tabEntries = Object.entries(pages).filter(([id]) => id !== 'dashboard');
  document.getElementById('app').innerHTML = `
    <div class="shell">
      <aside class="sidebar">
        <div class="brand" onclick="setTab('dashboard')" style="cursor:pointer"><div class="logo">IS</div><div><h1 style="font-size:22px">ISCC 管理台</h1></div></div>
        <div class="nav">${tabEntries.map(([id, page]) => `<button data-tab="${id}" class="${state.tab === id ? 'active' : ''}" onclick="setTab('${id}')">${page.label}</button>`).join('')}<button onclick="logoutLocal()">退出登录</button></div>
        <div class="card" style="margin-top:20px"><div class="muted">当前用户</div><strong>${h(state.config?.local_auth?.username || 'admin')}</strong><p class="muted">提交会直接发送到 ISCC，请确认 flag 后再操作。</p></div>
      </aside>
      <main id="main-root" class="main">${renderTab()}</main>
      <div id="modal-root">${renderModal()}</div>
      <div id="operation-modal-root">${renderOperationPanel()}</div>
      ${renderProgressDock()}
    </div>`;
}

function replaceHtml(id, html) {
  const target = document.getElementById(id);
  if (!target) return false;
  target.innerHTML = html;
  return true;
}

function renderMain() {
  return replaceHtml('main-root', renderTab());
}

function renderMainOrApp() {
  if (renderMain()) return true;
  renderApp();
  return false;
}

function renderProgressOrApp() {
  if (updateProgressDock()) return true;
  renderApp();
  return false;
}

function renderActiveNav() {
  const buttons = document.querySelectorAll('.nav button[data-tab]');
  if (!buttons.length) return false;
  buttons.forEach(button => button.classList.toggle('active', button.dataset.tab === state.tab));
  return true;
}

function renderModalRoots() {
  const modalOk = replaceHtml('modal-root', renderModal());
  const operationOk = replaceHtml('operation-modal-root', renderOperationPanel());
  return modalOk && operationOk;
}

function pageTitle(title, desc, actions='') {
  return `<div class="page-title"><div><h2>${h(title)}</h2><p>${h(desc)}</p></div><div class="toolbar">${actions}</div></div>`;
}

function renderTab() {
  const page = pages[state.tab] || pages.dashboard;
  return page.render();
}

function categoryColor(category, index) {
  const palette = ['#2563eb', '#059669', '#d97706', '#7c3aed', '#e11d48', '#0891b2', '#65a30d', '#db2777', '#4f46e5', '#0f766e'];
  return palette[index % palette.length];
}

function renderDashboardPage() {
  const passwordReady = state.accounts.filter(a => a.password_set).length;
  const solved = state.challengeStats.solved_any || 0;
  const total = state.challengeStats.total || state.challenges.length;
  const unsolved = Math.max(total - solved, 0);
  const filesWithHash = state.files.filter(f => f.md5).length;
  const categories = [...new Set(state.challenges.map(c => c.category || '未分类'))].sort();
  const categoryMeta = categories.map((category, index) => ({ category, color: categoryColor(category, index) }));
  const solveMap = new Map();
  state.challenges.forEach(c => (c.solved_by || []).forEach(u => {
    const row = solveMap.get(u.account_id) || { count: 0, categories: new Map() };
    const category = c.category || '未分类';
    row.count += 1;
    row.categories.set(category, (row.categories.get(category) || 0) + 1);
    solveMap.set(u.account_id, row);
  }));
  const accountRows = state.accounts.map(a => {
    const row = solveMap.get(a.id) || { count: 0, categories: new Map() };
    return {
      username: a.username,
      count: row.count,
      percent: total ? Math.round(row.count * 100 / total) : 0,
      segments: categoryMeta.map(meta => ({ ...meta, count: row.categories.get(meta.category) || 0 }))
    };
  }).sort((a, b) => b.percent - a.percent || a.username.localeCompare(b.username));
  const legend = categoryMeta.map(meta => `<span class="legend-chip"><i style="background:${meta.color}"></i>${h(meta.category)}</span>`).join('');
  return `
    ${pageTitle('总览', '账号、题目、解题与附件状态集中查看。')}
    <div class="grid cols-4">
      <div class="card metric"><span>账号总数</span><strong>${state.accounts.length}</strong></div>
      <div class="card metric"><span>已存密码</span><strong>${passwordReady}</strong></div>
      <div class="card metric"><span>题目缓存</span><strong>${total}</strong></div>
      <div class="card metric"><span>已解 / 未解</span><strong>${solved}/${unsolved}</strong></div>
    </div>
    <div class="dashboard-hero" style="margin-top:16px">
      <div class="card">
        <h3>账号解题进度</h3>
        <div class="legend-row">${legend || '<span class="muted">暂无方向数据</span>'}</div>
        <div class="mini-bars">${accountRows.map(row => `<div class="mini-bar"><span><em>${h(row.username)}</em><b class="progress-value">${row.count}/${total || 0} ${row.percent}%</b></span><div class="progress-track">${row.segments.map(seg => seg.count ? `<b class="progress-segment" title="${h(seg.category)}：${seg.count}" style="width:${total ? (seg.count * 100 / total) : 0}%;background:${seg.color}"></b>` : '').join('')}</div></div>`).join('') || '<p class="muted">暂无账号数据。</p>'}</div>
      </div>
      <div class="card"><h3>最近同步</h3><p class="muted">题目：${h(state.challengesUpdatedAt || '尚未同步')}</p><p class="muted">附件记录：${state.files.length} 个</p><p class="muted">附件哈希：${filesWithHash} 个</p><p class="muted">代理：${state.config?.iscc?.proxy?.enabled ? `已启用（${state.config?.iscc?.proxy?.list?.length || 0} 个）` : '未启用'}</p></div>
    </div>
    <div class="card" style="margin-top:16px"><h3>使用顺序</h3><p class="muted">1. 在「账号」导入 ISCC 账号；2. 在「题目」同步并查看已解/未解；3. 在「附件」更新文件和 MD5；4. 在「提交」提交 flag。</p></div>`;
}

function isAccountBusy() {
  return Boolean(state.busy.importAccounts || state.busy.loginAccounts);
}

function pruneLoginSelection() {
  const existing = new Set(state.accounts.map(account => account.id));
  state.loginAccountIds = state.loginAccountIds.filter(id => existing.has(id));
}

function isLoginAccountSelected(id) {
  return state.loginAccountIds.includes(id);
}

function syncLoginSelectionFromDom() {
  state.loginAccountIds = selectedValues('.loginAccount:checked');
}

function renderAccountsPage() {
  const accountBusy = isAccountBusy();
  return `
    ${pageTitle('账号', '导入和管理 ISCC 账号，本站不会在接口中回显密码。', `<button class="ghost" onclick="setLoginAccounts(true)" ${accountBusy ? 'disabled' : ''}>全选账号</button><button class="ghost" onclick="setLoginAccounts(false)" ${accountBusy ? 'disabled' : ''}>清空选择</button><button class="good" onclick="loginAllAccounts()" ${accountBusy ? 'disabled' : ''}>${accountBusy ? '账号任务进行中' : '登录所选账号'}</button>`) }
    <div class="grid cols-2">
      <div class="card">
        <h3>批量导入</h3>
        <p class="muted">每行一个账号：<span class="kbd">Username Password</span>，也支持 CSV：<span class="kbd">账号,密码</span>。</p>
        <textarea id="importText" oninput="state.importText=this.value" ${accountBusy ? 'disabled' : ''} placeholder="Username1 Password1&#10;Username2 Password2">${h(state.importText)}</textarea>
        <div class="toolbar" style="margin-top:12px"><button onclick="importAccounts()" ${accountBusy ? 'disabled' : ''}>${accountBusy ? '账号任务进行中' : '导入/更新'}</button></div>
      </div>
      <div class="card"><h3>安全说明</h3><p class="muted">ISCC 密码仅保存在本机 <span class="kbd">/app/data/accounts.json</span>，API 不会回显明文。由于自动登录必须使用可逆凭据，建议只在可信环境运行。</p></div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="table-wrap"><table><thead><tr><th>选择</th><th>账号</th><th>密码</th><th>登录状态 / 最近登录</th><th>错误</th><th>操作</th></tr></thead><tbody>
        ${state.accounts.map(a => `<tr><td><input class="loginAccount" type="checkbox" value="${h(a.id)}" ${isLoginAccountSelected(a.id) ? 'checked' : ''} ${accountBusy ? 'disabled' : ''} onchange="syncLoginSelectionFromDom()"></td><td><strong>${h(a.username)}</strong></td><td>${a.password_set ? '<span class="pill">已存密码</span>' : '<span class="pill bad">无密码</span>'}</td><td>${a.last_login_ok === true ? '<span class="pill good">成功</span>' : a.last_login_ok === false ? '<span class="pill bad">失败</span>' : '<span class="pill">未登录</span>'}<br><span class="muted">${h(a.last_login_at || '-')}</span></td><td class="muted">${h(a.last_error || '-')}</td><td><div class="row-actions"><button onclick="loginAccount('${h(a.id)}')" ${accountBusy ? 'disabled' : ''}>登录</button><button class="danger" onclick="deleteAccount('${h(a.id)}')" ${accountBusy ? 'disabled' : ''}>删除</button></div></td></tr>`).join('') || '<tr><td colspan="6" class="muted">暂无账号</td></tr>'}
      </tbody></table></div>
    </div>`;
}

function challengesTableHtml() {
  const filtered = filteredChallenges();
  return `<div class="table-wrap"><table><thead><tr><th>ID</th><th>题目</th><th>分类</th><th>分值</th><th>已解用户</th><th>未解用户</th><th>访问状态</th><th>附件</th><th>操作</th></tr></thead><tbody>
    ${filtered.map(c => `<tr><td>${h(challengeDisplayId(c))}</td><td><strong>${h(c.name || '未命名')}</strong> <span class="pill">${h(c.source_label || challengeSourceLabel(challengeSourceValue(c)))}</span> <button class="ghost rename-btn" onclick="renameChallenge('${h(c.id)}')">重命名</button><br><span class="muted">解出次数：${h(c.solves ?? '-')}</span></td><td><span class="pill">${h(c.category || '-')}</span></td><td>${h(c.value ?? '-')}</td><td>${renderUserChips(c.solved_by || [], 'good', `solved-${c.id}`)}</td><td>${renderUserChips(c.unsolved_by || [], 'bad', `unsolved-${c.id}`)}</td><td><span class="pill ${Number(c.visit_count || 0) >= Number(c.account_count || state.accounts.length || 0) ? 'good' : 'warn'}">${h(c.visit_count || 0)}/${h(c.account_count || state.accounts.length || 0)}</span><br>${renderUserChips(c.unvisited_by || [], 'warn', `unvisited-${c.id}`)}</td><td>${(c.files || []).length} 个</td><td><button onclick="openChallenge('${h(c.id)}')">详情</button></td></tr>`).join('') || '<tr><td colspan="9" class="muted">暂无题目，先同步题目。</td></tr>'}
  </tbody></table></div>`;
}

function renderChallengesTable() {
  return replaceHtml('challenges-table', challengesTableHtml());
}

function renderChallengesPage() {
  const cats = [...new Set(state.challenges.map(c => c.category).filter(Boolean))].sort();
  return `
    <div class="page-title"><div><div class="toolbar"><h2 style="margin:0">题目</h2><button class="ghost" onclick="manualRefreshChallenges()">刷新题目缓存</button></div><p>查看每个账号的已解与未解题目，支持分类和关键词筛选。</p></div><div class="toolbar"><button class="ghost" onclick="setTab('files')">查看/更新附件</button><button class="good" onclick="openOperationPanel('sync')">选择范围同步题目</button></div></div>
    <div class="card">
      <div class="filters">
        <div><label>账号视图</label>${accountSelectWithValue('filterAccount', true, state.filters.account, 'updateFilterFromDom()')}</div>
        <div><label>赛道</label><select id="filterSource" onchange="updateFilterFromDom()">${sourceSelectOptions(state.filters.source)}</select></div>
        <div><label>分类</label><select id="filterCategory" onchange="updateFilterFromDom()"><option value="">全部分类</option>${cats.map(c => `<option ${state.filters.category === c ? 'selected' : ''} value="${h(c)}">${h(c)}</option>`).join('')}</select></div>
        <div><label>状态</label><select id="filterStatus" onchange="updateFilterFromDom()"><option value="all" ${state.filters.status === 'all' ? 'selected' : ''}>全部</option><option value="solved" ${state.filters.status === 'solved' ? 'selected' : ''}>已解</option><option value="unsolved" ${state.filters.status === 'unsolved' ? 'selected' : ''}>未解</option></select></div>
        <div><label>搜索</label><input id="filterQ" value="${h(state.filters.q)}" oninput="state.filters.q=this.value; renderChallengesTable()" placeholder="题目名 / ID / 分类"></div>
      </div>
      <div id="challenges-table">${challengesTableHtml()}</div>
    </div>`;
}

function accountSelectWithValue(id, allowAll, value, onchange) {
  return `<select id="${id}" onchange="${onchange || ''}">${allowAll ? '<option value="">全部/聚合</option>' : ''}${state.accounts.map(a => `<option value="${h(a.id)}" ${value === a.id ? 'selected' : ''}>${h(a.username)}</option>`).join('')}</select>`;
}

function updateFilterFromDom(render=true) {
  state.filters.account = document.getElementById('filterAccount')?.value || '';
  state.filters.source = document.getElementById('filterSource')?.value || '';
  state.filters.category = document.getElementById('filterCategory')?.value || '';
  state.filters.status = document.getElementById('filterStatus')?.value || 'all';
  state.filters.q = document.getElementById('filterQ')?.value || '';
  if (render && !renderChallengesTable()) renderMainOrApp();
}

function renderUserChips(users, type, key='') {
  if (!users || !users.length) return '<span class="muted">无</span>';
  const expanded = key && state.expandedUserLists[key];
  const shown = expanded ? users : users.slice(0, 6);
  const visible = shown.map(u => `<span class="pill ${type}">${h(u.username || u.account_id)}</span>`).join('');
  const more = !expanded && users.length > 6 ? `<button class="pill ghost more-users" onclick="toggleUserList('${h(key)}')">+${users.length - 6}</button>` : '';
  const less = expanded && users.length > 6 ? `<button class="pill ghost more-users" onclick="toggleUserList('${h(key)}')">收起</button>` : '';
  return `<div class="user-list ${expanded ? 'expanded' : ''}">${visible}${more}${less}</div>`;
}

function toggleUserList(key) {
  if (!key) return;
  state.expandedUserLists[key] = !state.expandedUserLists[key];
  if (state.tab === 'challenges' && renderChallengesTable()) return;
  renderMainOrApp();
}

function challengeSolved(c) {
  if (state.filters.account) return (c.solved_by_ids || []).includes(state.filters.account);
  return (c.solved_by_ids || []).length > 0;
}

function selectedChallengeId(kind) {
  const saved = state.submit[kind] || '';
  if (saved && state.challenges.some(c => String(c.id) === String(saved))) return String(saved);
  const fallback = state.challenges[0] ? String(state.challenges[0].id) : '';
  if (fallback) state.submit[kind] = fallback;
  return fallback;
}

function submittedAccountIds(chalId) {
  const ids = new Set();
  const c = challengeById(chalId);
  (c?.solved_by_ids || []).forEach(id => ids.add(id));
  state.flags.filter(item => String(item.chal_id) === String(chalId)).forEach(item => item.account_id && ids.add(item.account_id));
  return ids;
}

function challengeFlags(chalId) {
  const seen = new Set();
  return state.flags.filter(item => {
    if (chalId && String(item.chal_id) !== String(chalId)) return false;
    const key = item.dedupe_key || `${item.flag_md5 || ''}:${item.attachment_md5 || ''}:${item.flag || ''}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function pruneBatchSelection() {
  const existing = new Set(state.accounts.map(account => account.id));
  state.batchAccountIds = state.batchAccountIds.filter(id => existing.has(id));
}

function syncBatchSelectionFromDom() {
  state.batchAccountSelectionDirty = true;
  state.batchAccountIds = selectedValues('.batchAccount:checked:not(:disabled)');
}

function batchAccountChecks(chalId) {
  const blocked = submittedAccountIds(chalId);
  return state.accounts.map(a => {
    const disabled = blocked.has(a.id);
    const checked = !disabled && (!state.batchAccountSelectionDirty || state.batchAccountIds.includes(a.id));
    const suffix = blocked.has(a.id) ? '已解/已提交' : '';
    return `<label class="check account-check ${disabled ? 'disabled' : ''}"><input class="batchAccount" type="checkbox" value="${h(a.id)}" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''} onchange="syncBatchSelectionFromDom()"> <span>${h(a.username)}</span>${suffix ? `<small>${suffix}</small>` : ''}</label>`;
  }).join('') || '<span class="muted">暂无账号</span>';
}

function updateSubmitState(kind, value) {
  captureSubmitInputs();
  state.submit[kind] = value;
  renderMainOrApp();
}

function captureSubmitInputs() {
  state.submit.batchFlag = document.getElementById('batchFlag')?.value || state.submit.batchFlag || '';
  state.submit.batchMd5 = document.getElementById('batchMd5')?.value || state.submit.batchMd5 || '';
}

function filteredChallenges() {
  const q = state.filters.q.trim().toLowerCase();
  return state.challenges.filter(c => {
    if (state.filters.source && challengeSourceValue(c) !== state.filters.source) return false;
    if (state.filters.category && c.category !== state.filters.category) return false;
    const solved = challengeSolved(c);
    if (state.filters.status === 'solved' && !solved) return false;
    if (state.filters.status === 'unsolved' && solved) return false;
    if (q) {
      const hay = `${c.id} ${challengeDisplayId(c)} ${c.name || ''} ${c.category || ''} ${c.source_label || challengeSourceLabel(challengeSourceValue(c))}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function flagManagementTableHtml() {
  const q = state.flagFilters.q.trim().toLowerCase();
  const challengesById = challengeLookup();
  const visibleFlags = challengeFlags('').map(flag => ({ flag, challenge: challengesById.get(String(flag.chal_id)) })).filter(({ flag, challenge }) => {
    if (state.flagFilters.category && challenge?.category !== state.flagFilters.category) return false;
    if (state.flagFilters.chal && String(flag.chal_id) !== String(state.flagFilters.chal)) return false;
    if (q) {
      const hay = `${flag.chal_id} ${challenge?.name || ''} ${challenge?.category || ''} ${flag.flag || ''} ${flag.attachment_md5 || ''}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  return `<table><thead><tr><th>题目</th><th>分类</th><th>Flag</th><th>附件 MD5</th><th>时间</th></tr></thead><tbody>${visibleFlags.map(({ flag, challenge }) => `<tr><td>${h(challengeName(flag.chal_id, challenge))}</td><td><span class="pill">${h(challenge?.category || '-')}</span></td><td><span class="kbd">${h(flag.flag)}</span></td><td><span class="kbd">${h(flag.attachment_md5 || '-')}</span></td><td>${h(flag.updated_at || flag.created_at || '-')}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">当前筛选暂无已保存 flag</td></tr>'}</tbody></table>`;
}

function renderFlagManagementTable() {
  if (state.tab !== 'submit') return false;
  const target = document.getElementById('flag-management-table');
  if (!target) return false;
  target.innerHTML = flagManagementTableHtml();
  return true;
}

async function manualRefreshFlags() {
  try {
    await refreshLocalKind('flags', false);
    renderFlagManagementTable() || renderMainOrApp();
    toast('Flag 已刷新', 'success');
  } catch (err) { toast(err.message); }
}

function updateFlagFilterFromDom(render=true) {
  const previousCategory = state.flagFilters.category;
  state.flagFilters.category = document.getElementById('flagFilterCategory')?.value || '';
  state.flagFilters.chal = document.getElementById('flagFilterChal')?.value || '';
  state.flagFilters.q = document.getElementById('flagFilterQ')?.value || '';
  if (previousCategory !== state.flagFilters.category) state.flagFilters.chal = '';
  if (!render) return;
  if (previousCategory !== state.flagFilters.category) renderMainOrApp();
  else renderFlagManagementTable() || renderMainOrApp();
}

function renderSubmitPage() {
  const batchChal = selectedChallengeId('batchChal');
  const challengeOptions = selected => state.challenges.map(c => `<option value="${h(c.id)}" ${String(selected) === String(c.id) ? 'selected' : ''}>${h(c.name || ('题目 ' + challengeDisplayId(c)))}（#${h(challengeDisplayId(c))} / ${h(c.category || '-')}）</option>`).join('');
  const flagCategories = [...new Set(state.challenges.map(c => c.category).filter(Boolean))].sort();
  const flagChallengeOptions = state.challenges.filter(c => !state.flagFilters.category || c.category === state.flagFilters.category).map(c => `<option value="${h(c.id)}" ${String(state.flagFilters.chal) === String(c.id) ? 'selected' : ''}>${h(c.name || ('题目 ' + challengeDisplayId(c)))}（#${h(challengeDisplayId(c))} / ${h(c.category || '-')}）</option>`).join('');
  return `
    ${pageTitle('提交', '选择一个题目向多个账号批量提交，并管理已保存 Flag。')}
    <div class="card submit-batch-card">
      <h3>批量提交</h3>
      <div class="field"><label>题目</label><select id="batchChal" onchange="captureSubmitInputs(); updateSubmitState('batchChal', this.value)">${challengeOptions(batchChal)}</select></div>
      <div class="field"><label>附件 MD5（可选）</label><input id="batchMd5" value="${h(state.submit.batchMd5)}" oninput="state.submit.batchMd5=this.value; matchBatchAccountsByMd5(false)" placeholder="输入 MD5 后自动匹配题目和用户"></div>
      <div class="toolbar"><button class="ghost" onclick="setBatchAccounts(true)">全选用户</button><button class="ghost" onclick="setBatchAccounts(false)">清空选择</button></div>
      <label>选择账号</label><div class="checkbox-list batch-account-list">${batchAccountChecks(batchChal)}</div>
      <div class="field"><label>Flag</label><input id="batchFlag" value="${h(state.submit.batchFlag)}" oninput="state.submit.batchFlag=this.value" placeholder="ISCC{...}"></div>
      <div class="toolbar" style="margin-top:12px"><button onclick="batchSubmit()">批量提交</button></div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="toolbar"><h3 style="margin:0">Flag 管理</h3><button class="ghost" onclick="manualRefreshFlags()">刷新 Flag</button></div>
      <div class="filters">
        <div><label>分类</label><select id="flagFilterCategory" onchange="updateFlagFilterFromDom()"><option value="">全部分类</option>${flagCategories.map(c => `<option value="${h(c)}" ${state.flagFilters.category === c ? 'selected' : ''}>${h(c)}</option>`).join('')}</select></div>
        <div><label>题目</label><select id="flagFilterChal" onchange="updateFlagFilterFromDom()"><option value="">全部题目</option>${flagChallengeOptions}</select></div>
        <div style="grid-column: span 2"><label>搜索</label><input id="flagFilterQ" value="${h(state.flagFilters.q)}" oninput="state.flagFilters.q=this.value; renderFlagManagementTable()" placeholder="题目 / 分类 / Flag / 附件 MD5"></div>
      </div>
      <div id="flag-management-table" class="result-box table-wrap">${flagManagementTableHtml()}</div>
    </div>`;
}

function filesFilterHtml() {
  const fileChallenges = state.challenges.filter(challengeHasAttachmentInfo).filter(c => !state.fileFilters.source || challengeSourceValue(c) === state.fileFilters.source);
  const fileCategories = [...new Set(fileChallenges.map(c => c.category).filter(Boolean))].sort();
  const challengeOptions = fileChallenges.filter(c => !state.fileFilters.category || c.category === state.fileFilters.category).map(c => `<option value="${h(c.id)}" ${String(state.fileFilters.chal) === String(c.id) ? 'selected' : ''}>${h(fileChallengeName(c.name, challengeDisplayId(c)))}（#${h(challengeDisplayId(c))} / ${h(c.source_label || challengeSourceLabel(challengeSourceValue(c)))})</option>`).join('');
  return `<div class="filters files-filters">
    <div><label>用户</label>${accountSelectWithValue('fileFilterAccount', true, state.fileFilters.account, 'updateFileFilterFromDom()')}</div>
    <div><label>赛道</label><select id="fileFilterSource" onchange="updateFileFilterFromDom()">${sourceSelectOptions(state.fileFilters.source)}</select></div>
    <div><label>分类</label><select id="fileFilterCategory" onchange="updateFileFilterFromDom()"><option value="">全部分类</option>${fileCategories.map(c => `<option value="${h(c)}" ${state.fileFilters.category === c ? 'selected' : ''}>${h(c)}</option>`).join('')}</select></div>
    <div><label>题目名称</label><select id="fileFilterChal" onchange="updateFileFilterFromDom()"><option value="">全部题目</option>${challengeOptions}</select></div>
    <div><label>搜索</label><input id="fileFilterQ" value="${h(state.fileFilters.q)}" oninput="state.fileFilters.q=this.value; renderFilesTable()" placeholder="题目 / 赛道 / 文件名 / MD5 / 账号"></div>
  </div>`;
}

function filesTableHtml() {
  const challengesById = challengeLookup();
  const files = filteredFiles(challengesById);
  return `<div class="toolbar"><span class="muted">共 ${files.length}/${state.files.length} 个附件记录。原始下载只会打开当前筛选结果里的 ISCC 原始链接。</span></div>
  <div class="table-wrap"><table><thead><tr><th>账号</th><th>题目</th><th>原文件</th><th>保存文件</th><th>大小</th><th>MD5</th><th>更新时间</th><th>操作</th></tr></thead><tbody>
    ${files.map(f => { const challenge = challengesById.get(String(f.challenge_id)); return `<tr><td>${h(f.account_username || f.account_id)}</td><td><strong>${h(fileDisplayChallengeName(f, challenge))}</strong> <span class="pill">${h(f.source_label || challengeSourceLabel(f.source))}</span><br><span class="muted">#${h(challengeDisplayId(f) || '-')}</span></td><td>${h(f.original_name)}</td><td class="kbd">${h(f.stored_name)}</td><td>${formatSize(f.size)}</td><td><span class="kbd">${h(f.md5 || '-')}</span></td><td>${h(f.updated_at || '-')}</td><td><div class="row-actions">${f.source_url ? `<a class="pill" href="${h(f.source_url)}" target="_blank" rel="noopener noreferrer">下载原始</a>` : '<span class="pill">无链接</span>'}<button class="danger" onclick="deleteFileRecord('${h(f.file_id)}')">删除</button></div></td></tr>`; }).join('') || '<tr><td colspan="8" class="muted">暂无附件记录。</td></tr>'}
  </tbody></table></div>`;
}

function renderFilesTable() {
  return replaceHtml('files-table', filesTableHtml());
}

async function manualRefreshChallenges() {
  try {
    await refreshLocalKind('challenges', false);
    renderCurrentViewForKinds(['challenges']) || renderMainOrApp();
    toast('题目缓存已刷新', 'success');
  } catch (err) { toast(err.message); }
}

async function manualRefreshFiles() {
  try {
    await Promise.all([refreshLocalKind('files', false), refreshLocalKind('challenges', false)]);
    renderCurrentViewForKinds(['files', 'challenges']) || renderMainOrApp();
    toast('附件列表已刷新', 'success');
  } catch (err) { toast(err.message); }
}

function renderFilesPage() {
  return `
    <div class="page-title"><div><div class="toolbar"><h2 style="margin:0">附件</h2><button class="ghost" onclick="manualRefreshFiles()">刷新附件列表</button></div><p>根据当前筛选的账号和题目更新附件，并查看文件、大小和 MD5。</p></div><div class="toolbar"><button class="ghost" onclick="setTab('challenges')">查看/更新题目</button><button class="good" onclick="openOperationPanel('files')">选择范围更新附件</button><button class="ghost" onclick="downloadAllOriginalFiles()">下载筛选原始附件</button><button class="danger" onclick="deleteFilteredFiles()">删除筛选文件</button></div></div>
    <div class="card">
      <div id="files-filters">${filesFilterHtml()}</div>
      <div id="files-table">${filesTableHtml()}</div>
    </div>`;
}

function filteredFiles(challengesById=challengeLookup()) {
  const q = state.fileFilters.q.trim().toLowerCase();
  return state.files.filter(f => {
    const challenge = challengesById.get(String(f.challenge_id));
    const source = challenge ? challengeSourceValue(challenge) : String(f.source || 'challenge');
    if (state.fileFilters.account && f.account_id !== state.fileFilters.account) return false;
    if (state.fileFilters.source && source !== state.fileFilters.source) return false;
    if (state.fileFilters.category && challenge?.category !== state.fileFilters.category) return false;
    if (state.fileFilters.chal && String(f.challenge_id) !== String(state.fileFilters.chal)) return false;
    if (q) {
      const hay = `${f.account_username || ''} ${f.account_id || ''} ${challengeDisplayId(f)} ${fileDisplayChallengeName(f)} ${f.challenge_name || ''} ${challenge?.name || ''} ${challenge?.category || ''} ${f.source_label || challengeSourceLabel(source)} ${f.original_name || ''} ${f.stored_name || ''} ${f.md5 || ''}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function logsOffset() {
  return (Math.max(1, state.logsPage.page) - 1) * state.logsPage.pageSize;
}

function maxLogsPage() {
  return Math.max(1, Math.ceil((state.logsPage.total || 0) / state.logsPage.pageSize));
}

async function fetchLogsPage() {
  return api(`/api/logs?limit=${encodeURIComponent(state.logsPage.pageSize)}&offset=${encodeURIComponent(logsOffset())}`);
}

function applyLogsResponse(data, notify=false) {
  const logs = Array.isArray(data) ? data : (data?.items || []);
  if (notify) notifyImportantLogs(logs);
  else rememberImportantLogs(logs);
  if (Array.isArray(data)) {
    state.logs = logs;
    state.logsPage.total = data.length;
    return;
  }
  state.logs = logs;
  state.logsPage.total = Number(data?.total || 0);
  state.logsPage.pageSize = Number(data?.limit || state.logsPage.pageSize);
  const nextPage = Math.min(state.logsPage.page, maxLogsPage());
  state.logsPage.page = Math.max(1, nextPage);
}

function logToastKey(log) {
  return `${log.time || ''}:${log.event || ''}:${log.proxy || ''}:${log.message || ''}`;
}

function rememberImportantLogs(logs) {
  logs.filter(log => log.event === 'proxy_removed').forEach(log => {
    state.seenLogToasts.add(logToastKey(log));
  });
}

function notifyImportantLogs(logs) {
  logs.filter(log => log.event === 'proxy_removed').forEach(log => {
    const key = logToastKey(log);
    if (state.seenLogToasts.has(key)) return;
    state.seenLogToasts.add(key);
    toast(log.message || `已删除不可用代理：${log.proxy || '-'}`, 'warn');
  });
}

async function loadLogs(render=true, notify=false) {
  const requestedPage = state.logsPage.page;
  const data = await fetchLogsPage();
  applyLogsResponse(data, notify);
  if (state.logsPage.page !== requestedPage) {
    applyLogsResponse(await fetchLogsPage(), notify);
  }
  if (render) renderCurrentViewForKinds(['logs']) || renderMainOrApp();
}

async function setLogsPage(page) {
  state.logsPage.page = Math.max(1, Math.min(maxLogsPage(), Number(page) || 1));
  await loadLogs(true).catch(err => toast(err.message));
}

async function setLogsPageSize(size) {
  state.logsPage.pageSize = Number(size) || 50;
  state.logsPage.page = 1;
  await loadLogs(true).catch(err => toast(err.message));
}

function renderLogsPage() {
  const maxPage = maxLogsPage();
  const page = Math.min(state.logsPage.page, maxPage);
  const pager = `<button class="ghost" onclick="setLogsPage(1)" ${page <= 1 ? 'disabled' : ''}>首页</button><button class="ghost" onclick="setLogsPage(${page - 1})" ${page <= 1 ? 'disabled' : ''}>上一页</button><span class="pill">第 ${page} / ${maxPage} 页，共 ${state.logsPage.total || 0} 条</span><button class="ghost" onclick="setLogsPage(${page + 1})" ${page >= maxPage ? 'disabled' : ''}>下一页</button><button class="ghost" onclick="setLogsPage(${maxPage})" ${page >= maxPage ? 'disabled' : ''}>末页</button><select onchange="setLogsPageSize(this.value)"><option value="25" ${state.logsPage.pageSize === 25 ? 'selected' : ''}>25 条/页</option><option value="50" ${state.logsPage.pageSize === 50 ? 'selected' : ''}>50 条/页</option><option value="100" ${state.logsPage.pageSize === 100 ? 'selected' : ''}>100 条/页</option></select><button onclick="refreshLogs()">刷新日志</button>`;
  return `
    ${pageTitle('日志', '查看本次服务运行期的 ISCC 登录、同步和提交诊断日志。', pager)}
    <div class="card">
      <div class="table-wrap"><table><thead><tr><th>时间</th><th>级别</th><th>事件</th><th>账号</th><th>代理</th><th>题目</th><th>信息</th></tr></thead><tbody>
        ${state.logs.map(log => `<tr><td>${h(log.time)}</td><td><span class="pill ${log.level === 'error' ? 'bad' : log.level === 'warn' ? 'warn' : 'good'}">${h(log.level)}</span></td><td>${h(log.event)}</td><td>${h(log.username || log.account_id || '-')}</td><td>${h(log.proxy || '-')}</td><td>${h(log.chal_id || '-')}</td><td class="pre">${h(log.error || log.message || log.raw || '-')}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">暂无日志</td></tr>'}
      </tbody></table></div>
    </div>`;
}

function updateLogsPolling() {
  if (state.logsTimer && state.tab !== 'logs') {
    clearInterval(state.logsTimer);
    state.logsTimer = null;
  }
  if (state.tab === 'logs' && !state.logsTimer) {
    state.logsTimer = setInterval(() => refreshLogs(false), 1500);
  }
}

async function refreshLogs(showError=true) {
  if (state.logsInFlight) return;
  state.logsInFlight = true;
  try {
    await refreshLocalKind('logs', state.tab === 'logs');
  } catch (err) {
    if (showError) toast(err.message);
  } finally {
    state.logsInFlight = false;
  }
}

function updateFileFilterFromDom(render=true) {
  state.fileFilters.account = document.getElementById('fileFilterAccount')?.value || '';
  const previousSource = state.fileFilters.source;
  const previousCategory = state.fileFilters.category;
  state.fileFilters.source = document.getElementById('fileFilterSource')?.value || '';
  state.fileFilters.category = document.getElementById('fileFilterCategory')?.value || '';
  state.fileFilters.chal = document.getElementById('fileFilterChal')?.value || '';
  state.fileFilters.q = document.getElementById('fileFilterQ')?.value || state.fileFilters.q || '';
  if (previousSource !== state.fileFilters.source) state.fileFilters.category = '';
  if (previousSource !== state.fileFilters.source || previousCategory !== state.fileFilters.category) state.fileFilters.chal = '';
  if (!render) return;
  if (previousSource !== state.fileFilters.source || previousCategory !== state.fileFilters.category) renderMainOrApp();
  else renderFilesTable() || renderMainOrApp();
}

function fileChallengeName(name, chalId) {
  name = String(name || '').trim();
  return name && name !== `#${chalId}` ? name : `题目 ${chalId}`;
}

function challengeTitle(chalId, challenge=null) {
  const c = challenge || challengeById(chalId);
  return c ? fileChallengeName(c.name, challengeDisplayId(c)) : chalId;
}

function challengeName(chalId, challenge=null) {
  const c = challenge || challengeById(chalId);
  return c ? `${challengeTitle(chalId, c)}（#${challengeDisplayId(c)}）` : chalId;
}

function fileDisplayChallengeName(file, challenge=null) {
  const c = challenge || challengeById(file.challenge_id);
  return c ? fileChallengeName(c.name, challengeDisplayId(c)) : fileChallengeName(file.challenge_name, challengeDisplayId(file));
}

function setChecked(selector, checked, skipDisabled=false) {
  document.querySelectorAll(selector).forEach(item => {
    item.checked = checked && (!skipDisabled || !item.disabled);
  });
}

function selectedValues(selector) {
  return [...document.querySelectorAll(selector)].map(x => x.value);
}

function setBatchAccounts(checked) {
  captureSubmitInputs();
  const blocked = submittedAccountIds(selectedChallengeId('batchChal'));
  state.batchAccountSelectionDirty = true;
  state.batchAccountIds = checked ? state.accounts.filter(account => !blocked.has(account.id)).map(account => account.id) : [];
  renderMainOrApp();
}

function batchAccountInputs() {
  return document.querySelectorAll('.batchAccount');
}

function matchBatchAccountsByMd5(showToast=true) {
  captureSubmitInputs();
  const md5 = state.submit.batchMd5.trim().toLowerCase();
  if (!md5) {
    state.batchAccountSelectionDirty = true;
    state.batchAccountIds = [];
    setChecked('.batchAccount', false);
    return;
  }
  const matches = state.files.filter(f => String(f.md5 || '').trim().toLowerCase() === md5 && f.account_id);
  if (!matches.length) {
    state.batchAccountSelectionDirty = true;
    state.batchAccountIds = [];
    setChecked('.batchAccount', false);
    if (showToast) toast('没有匹配到该 MD5 的附件用户', 'warn');
    return;
  }
  const countByChallenge = new Map();
  matches.forEach(f => {
    const key = String(f.challenge_id);
    countByChallenge.set(key, (countByChallenge.get(key) || 0) + 1);
  });
  const chalId = [...countByChallenge.entries()].sort((a, b) => b[1] - a[1])[0][0];
  if (String(state.submit.batchChal) !== String(chalId)) {
    state.submit.batchChal = chalId;
    renderMainOrApp();
    setTimeout(() => matchBatchAccountsByMd5(showToast), 0);
    return;
  }
  const blocked = submittedAccountIds(chalId);
  const matched = new Set(matches.filter(f => String(f.challenge_id) === String(chalId)).map(f => f.account_id));
  let selected = 0;
  let blockedCount = 0;
  const selectedIds = [];
  batchAccountInputs().forEach(item => {
    if (item.disabled) {
      if (matched.has(item.value) && blocked.has(item.value)) blockedCount += 1;
      item.checked = false;
      return;
    }
    item.checked = matched.has(item.value);
    if (item.checked) {
      selected += 1;
      selectedIds.push(item.value);
    }
  });
  state.batchAccountSelectionDirty = true;
  state.batchAccountIds = selectedIds;
  if (showToast) toast(`已自动匹配题目：${challengeName(chalId)}，可提交账号 ${selected} 个${blockedCount ? `，${blockedCount} 个已提交/已解被跳过` : ''}`, selected ? 'success' : 'warn');
}

function formatSize(size) {
  size = Number(size || 0);
  if (size > 1024 * 1024) return `${(size / 1024 / 1024).toFixed(2)} MB`;
  if (size > 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${size} B`;
}

function proxyDelayClass(ms, ok, status) {
  if (status === 'pending' || status === 'testing') return 'pending';
  if (!ok) return 'bad';
  if (ms <= 600) return 'good';
  if (ms <= (state.proxyMaxLatency || DEFAULT_PROXY_MAX_LATENCY)) return 'warn';
  return 'bad';
}

function proxyDisplayName(proxy) {
  return String(proxy || '').replace(/^https?:\/\//, '');
}

function proxyTestTable() {
  if (!state.proxyTestResults.length) return '<p class="muted">点击“一键测试代理”后会先展示全部端口，再逐个刷新连通性。</p>';
  return `<div class="proxy-test-list">${state.proxyTestResults.map(r => {
    const delay = r.latency_ms == null ? '-' : `${h(r.latency_ms)} ms`;
    const cls = proxyDelayClass(Number(r.latency_ms || 0), r.ok, r.status);
    const text = r.status === 'pending' ? '等待测试' : r.status === 'testing' ? '测试中' : r.ok ? `HTTP ${h(r.status_code || '-')}` : '连接失败';
    return `<div class="proxy-test-item ${cls}"><div><strong>${h(proxyDisplayName(r.proxy || '-'))}</strong><span>${text}</span></div><b>${delay}</b></div>`;
  }).join('')}</div>`;
}

function renderProxyTestResults() {
  return replaceHtml('proxy-test-results', proxyTestTable());
}

function renderConfigPage() {
  const c = state.configDraft || state.config || {};
  return `
    ${pageTitle('配置', '调整 ISCC 请求、代理和本地服务参数。')}
    <div class="grid cols-2">
      <div class="card">
        <h3>ISCC 请求</h3>
        <div class="field"><label>ISCC 地址</label><input id="cfgBaseUrl" value="${h(c.iscc?.base_url || '')}"></div>
        <div class="field"><label>请求超时（秒）</label><input id="cfgTimeout" type="number" min="3" max="120" value="${h(c.iscc?.timeout || 15)}"></div>
        <div class="field"><label>操作间隔延时（秒）</label><input id="cfgDelay" type="number" min="0" max="60" step="0.1" value="${h(c.iscc?.operation_delay_seconds ?? 0)}"><p class="muted">用于批量同步、附件更新和批量提交时控制账号/题目处理间隔，最大 60 秒。</p></div>
        <div class="field"><label>提交参数方式</label><select id="cfgPayload"><option value="data" ${c.iscc?.submit_payload_mode === 'data' ? 'selected' : ''}>POST 表单</option><option value="params" ${c.iscc?.submit_payload_mode !== 'data' ? 'selected' : ''}>POST 地址参数</option></select></div>
        <label class="check"><input id="cfgVerify" type="checkbox" ${c.iscc?.verify_tls ? 'checked' : ''}> 校验 TLS 证书</label>
        <div class="field"><label>跳过下载分类（逗号分隔，可留空）</label><input id="cfgSkipCats" value="${h((c.iscc?.skip_file_categories || []).join(','))}" placeholder="WEB,PWN"></div>
      </div>
      <div class="card">
        <h3>代理</h3>
        <label class="check"><input id="cfgProxyEnabled" type="checkbox" ${c.iscc?.proxy?.enabled ? 'checked' : ''}> 启用代理</label>
        <div class="field"><label>代理列表（空格或换行分隔）</label><textarea id="cfgProxyList" placeholder="http://127.0.0.1:8000&#10;http://127.0.0.1:7890&#10;http://127.0.0.1:9000">${h((c.iscc?.proxy?.list || []).join('\n'))}</textarea></div>
        <div class="grid cols-2"><div class="field"><label>选择方式</label><select id="cfgProxyMode"><option value="round_robin" ${c.iscc?.proxy?.mode !== 'random' ? 'selected' : ''}>按账号固定绑定</option><option value="random" ${c.iscc?.proxy?.mode === 'random' ? 'selected' : ''}>每次请求随机代理</option></select></div><div class="field"><label>最大允许延迟（ms）</label><input id="cfgProxyMaxLatency" type="number" min="1" step="100" value="${h(state.proxyMaxLatency || DEFAULT_PROXY_MAX_LATENCY)}" placeholder="超过也删除"></div></div>
        <p class="muted">固定绑定会在每次保存配置后按账号顺序重新分配；随机代理会在每一次 ISCC 请求发出前从代理池随机选择一个代理。</p>
        <div class="toolbar"><button class="ghost" onclick="testProxyUi()" ${state.proxyTesting ? 'disabled' : ''}>${state.proxyTesting ? '正在测试代理' : '一键测试代理'}</button></div>
        <div id="proxy-test-results">${proxyTestTable()}</div>
      </div>
      <div class="card">
        <h3>本地服务</h3>
        <div class="field"><label>监听地址</label><input id="cfgHost" value="${h(c.server?.host || '127.0.0.1')}"></div>
        <div class="field"><label>监听端口</label><input id="cfgPort" type="number" value="${h(c.server?.port || 5000)}"></div>
        <p class="muted">监听地址和端口保存后需要重启服务才会生效。</p>
        <div class="toolbar"><button onclick="saveConfigUi()">保存配置</button></div>
      </div>
    </div>`;
}

function renderModal() {
  return renderChallengeModal();
}

function renderChallengeModal() {
  if (!state.modalChallenge) return '';
  const c = challengeById(state.modalChallenge);
  if (!c) return '';
  const files = state.files.filter(f => String(f.challenge_id) === String(c.id));
  return `<div class="modal-backdrop" onclick="closeModal(event)"><div class="modal" onclick="event.stopPropagation()">
    <div class="page-title"><div><h2>#${h(challengeDisplayId(c))} ${h(c.name || '')}</h2><p>${h(c.source_label || challengeSourceLabel(challengeSourceValue(c)))} · ${h(c.category || '')} · ${h(c.value ?? '-')} 分 · 解出次数 ${h(c.solves ?? '-')}</p></div><div class="toolbar"><button class="ghost" onclick="renameChallenge('${h(c.id)}')">重命名</button><button class="ghost" onclick="state.modalChallenge=null;renderModalRoots()">关闭</button></div></div>
    <h3>题目描述</h3><div class="pre">${h(c.description || '暂无描述')}</div>
    <h3>已解账号</h3>${renderUserChips(c.solved_by || [], 'good')}
    <h3>未解账号</h3>${renderUserChips(c.unsolved_by || [], 'bad')}
    <h3>题目详情访问状态</h3><p class="muted">已访问 ${h(c.visit_count || 0)}/${h(c.account_count || state.accounts.length || 0)} 个账号</p>${renderUserChips(c.unvisited_by || [], 'warn')}
    <h3>已下载文件</h3><div class="table-wrap"><table><thead><tr><th>账号</th><th>文件</th><th>MD5</th><th>大小</th></tr></thead><tbody>${files.map(f => `<tr><td>${h(f.account_username)}</td><td>${h(f.original_name)}</td><td class="kbd">${h(f.md5)}</td><td>${formatSize(f.size)}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">暂无下载记录</td></tr>'}</tbody></table></div>
  </div></div>`;
}

function renderOperationPanel() {
  const panel = state.operationPanel;
  if (!panel) return '';
  const title = panel.type === 'files' ? '选择附件更新范围' : '选择范围同步题目';
  const primaryText = panel.type === 'files' ? '开始更新附件' : '开始同步题目';
  const subtitle = panel.type === 'files' ? '附件面板只显示已有附件地址或已有下载记录的题目；默认跳过本地已存在文件。' : '同步默认使用第一个账号同步题目信息，其他账号同步题解状态。';
  const accountChecks = state.accounts.map(account => `<label class="check account-check"><input type="checkbox" class="opAccount" value="${h(account.id)}" ${panel.accountIds.includes(account.id) ? 'checked' : ''}> <span>${h(account.username)}</span></label>`).join('');
  const operationChallengeIds = panel.type === 'files' ? filteredChallengeIdsFromFileView().filter(id => panel.challengeIds.includes(id)) : panel.challengeIds;
  const challengeChecks = operationChallengeIds.map(id => {
    const challenge = challengeById(id);
    return `<label class="check"><input type="checkbox" class="opChallenge" value="${h(id)}" checked> <span>${h(challenge?.name || `#${id}`)}</span><small>#${h(id)}</small></label>`;
  }).join('') || '<p class="muted">当前筛选没有题目，将由后端按账号刷新可用题目。</p>';
  return `<div class="modal-backdrop"><div class="modal operation-modal">
    <div class="page-title"><div><h2>${title}</h2><p>${subtitle}</p></div><button class="ghost" onclick="closeOperationPanel()">关闭</button></div>
    <div class="legend-row"><span class="pill good">账号 ${panel.accountIds.length}/${state.accounts.length}</span><span class="pill warn">题目 ${operationChallengeIds.length}</span></div>
    <div class="grid cols-2 operation-grid">
      <div class="operation-section"><h3>账号</h3><div class="toolbar"><button class="ghost" onclick="setOperationAccounts(true)">全选</button><button class="ghost" onclick="setOperationAccounts(false)">清空</button></div><div class="checkbox-list batch-account-list">${accountChecks}</div></div>
      <div class="operation-section"><h3>题目</h3><div class="toolbar"><button class="ghost" onclick="setOperationChallenges(true)">全选</button><button class="ghost" onclick="setOperationChallenges(false)">清空</button></div><div class="checkbox-list operation-challenge-list">${challengeChecks}</div></div>
    </div>
    <div class="toolbar" style="margin-top:16px"><button class="good" onclick="runOperationPanel()">${primaryText}</button><button class="ghost" onclick="closeOperationPanel()">取消</button></div>
  </div></div>`;
}

function closeModal(event) {
  if (event.target.classList.contains('modal-backdrop')) {
    state.modalChallenge = null;
    renderModalRoots();
  }
}

function openChallenge(id) {
  state.modalChallenge = id;
  renderModalRoots();
}

async function renameChallenge(id) {
  const current = challengeById(id);
  const name = prompt('请输入新的题目名称', current?.name || '');
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed) {
    toast('题目名称不能为空', 'warn');
    return;
  }
  try {
    await api(`/api/challenges/${encodeURIComponent(id)}`, { method: 'PATCH', body: { name: trimmed } });
    await refreshLocalKind('challenges', false);
    renderCurrentViewForKinds(['challenges']);
    toast('题目名称已更新', 'success');
  } catch (err) { toast(err.message); }
}

function openOperationPanel(type) {
  if (type === 'files') updateFileFilterFromDom(false);
  else updateFilterFromDom(false);
  const accountIds = filteredAccountIds(type === 'files' ? state.fileFilters.account : state.filters.account);
  const challengeIds = type === 'files' ? filteredChallengeIdsFromFileView() : filteredChallengeIdsFromChallengeView();
  state.operationPanel = { type, accountIds, challengeIds, initialChallengeIds: [...challengeIds] };
  renderModalRoots();
}

function closeOperationPanel() {
  state.operationPanel = null;
  renderModalRoots();
}

function setOperationAccounts(checked) {
  setChecked('.opAccount', checked);
}

function setOperationChallenges(checked) {
  setChecked('.opChallenge', checked);
}

async function runOperationPanel() {
  const panel = state.operationPanel;
  if (!panel) return;
  const accountIds = selectedValues('.opAccount:checked');
  let challengeIds = selectedValues('.opChallenge:checked');
  if (panel.type === 'sync' && challengeIds.length === (panel.initialChallengeIds || []).length && state.tab !== 'files') challengeIds = null;
  closeOperationPanel();
  if (panel.type === 'files') await updateFiles({ accountIds, challengeIds });
  else await syncAll({ accountIds, challengeIds });
}

async function importAccounts() {
  if (isAccountBusy()) {
    toast('已有账号任务正在进行，请等待完成', 'warn');
    return;
  }
  let progress_id = null;
  state.busy.importAccounts = true;
  renderCurrentViewForKinds(['accounts']) || renderMainOrApp();
  const startedRealtime = startRealtimeRefresh(['accounts', 'challenges', 'logs']);
  try {
    const text = document.getElementById('importText').value;
    state.importText = text;
    const total = text.split(/\n/).map(x => x.trim()).filter(x => x && !x.startsWith('#')).length || 1;
    progress_id = startProgress('导入并登录账号', total);
    const data = ensureNotCancelled(await api('/api/accounts/import', { method: 'POST', body: { text, progress_id } }));
    const loginOk = (data.login_results || []).filter(x => x.ok).length;
    toast(`导入完成：新增 ${data.created}，更新 ${data.updated}，登录成功 ${loginOk}/${(data.login_results || []).length}${loginOk ? '，已自动同步' : ''}`, loginOk ? 'success' : 'warn');
    finishProgressSoon(progress_id);
    await refreshKinds(['accounts', 'challenges', 'files', 'flags', 'logs'], true);
  } catch (err) {
    stopProgress(progress_id, true);
    toast(err.message);
  } finally {
    stopRealtimeRefresh(startedRealtime);
    state.busy.importAccounts = false;
    await refreshKinds(['accounts', 'challenges', 'files', 'flags', 'logs'], true).catch(() => renderCurrentViewForKinds(['accounts']) || renderMainOrApp());
  }
}

async function loginAccount(id) {
  if (isAccountBusy()) {
    toast('已有账号任务正在进行，请等待完成', 'warn');
    return;
  }
  let progress_id = null;
  state.busy.loginAccounts = true;
  renderCurrentViewForKinds(['accounts']) || renderMainOrApp();
  const startedRealtime = startRealtimeRefresh(['accounts', 'challenges', 'logs']);
  try {
    progress_id = startProgress('登录账号', 1);
    ensureNotCancelled(await api(`/api/accounts/${id}/test-login`, { method: 'POST', body: { progress_id } }));
    finishProgressSoon(progress_id);
    await refreshKinds(['accounts', 'challenges', 'flags', 'logs'], true);
  } catch (err) {
    stopProgress(progress_id, true);
    toast(err.message);
    await refreshKinds(['accounts', 'challenges', 'flags', 'logs'], true).catch(() => {});
  } finally {
    stopRealtimeRefresh(startedRealtime);
    state.busy.loginAccounts = false;
    renderCurrentViewForKinds(['accounts']) || renderMainOrApp();
  }
}

async function loginAllAccounts() {
  if (isAccountBusy()) {
    toast('已有账号任务正在进行，请等待完成', 'warn');
    return;
  }
  let progress_id = null;
  state.busy.loginAccounts = true;
  renderCurrentViewForKinds(['accounts']) || renderMainOrApp();
  const startedRealtime = startRealtimeRefresh(['accounts', 'challenges', 'logs']);
  try {
    syncLoginSelectionFromDom();
    pruneLoginSelection();
    const account_ids = [...state.loginAccountIds];
    if (!account_ids.length) throw new Error('请选择至少一个账号');
    progress_id = startProgress('登录所选账号', account_ids.length);
    const data = ensureNotCancelled(await api('/api/accounts/test-login-all', { method: 'POST', body: { account_ids, progress_id } }));
    const ok = data.results.filter(item => item.ok).length;
    toast(`登录完成：${ok}/${data.results.length}`, ok === data.results.length ? 'success' : 'warn');
    finishProgressSoon(progress_id);
    await refreshKinds(['accounts', 'challenges', 'flags', 'logs'], true);
  } catch (err) {
    stopProgress(progress_id, true);
    toast(err.message);
    await refreshKinds(['accounts', 'challenges', 'flags', 'logs'], true).catch(() => {});
  } finally {
    stopRealtimeRefresh(startedRealtime);
    state.busy.loginAccounts = false;
    renderCurrentViewForKinds(['accounts']) || renderMainOrApp();
  }
}

function setLoginAccounts(checked) {
  state.loginAccountIds = checked ? state.accounts.map(account => account.id) : [];
  renderCurrentViewForKinds(['accounts']) || renderMainOrApp();
}

async function deleteAccount(id) {
  if (isAccountBusy()) {
    toast('已有账号任务正在进行，请等待完成', 'warn');
    return;
  }
  if (!confirm('确认删除该账号记录？已有附件文件不会自动删除。')) return;
  try {
    await api(`/api/accounts/${id}`, { method: 'DELETE' });
    state.loginAccountIds = state.loginAccountIds.filter(accountId => accountId !== id);
    state.batchAccountIds = state.batchAccountIds.filter(accountId => accountId !== id);
    toast('账号已删除，本地题解/附件记录已同步清理，Flag 记录已保留', 'success');
    await refreshKinds(['accounts', 'challenges', 'files', 'flags', 'logs'], true);
  } catch (err) { toast(err.message); }
}

function filteredAccountIds(filterAccountId) {
  if (filterAccountId) return state.accounts.some(account => account.id === filterAccountId) ? [filterAccountId] : [];
  return state.accounts.map(account => account.id);
}

function filteredChallengeIdsFromChallengeView() {
  if (state.tab === 'files') return filteredChallengesForFileView().map(challenge => String(challenge.id));
  return filteredChallenges().map(challenge => String(challenge.id));
}

function skippedFileCategories() {
  return new Set((state.config?.iscc?.skip_file_categories || []).map(item => String(item || '').toUpperCase()));
}

function challengeHasAttachmentInfo(challenge) {
  const category = String(challenge.category || '').toUpperCase();
  if (skippedFileCategories().has(category)) return false;
  return (challenge.files || []).length > 0;
}

function filteredChallengesForFileView() {
  return state.challenges.filter(challengeHasAttachmentInfo).filter(challenge => {
    if (state.fileFilters.source && challengeSourceValue(challenge) !== state.fileFilters.source) return false;
    if (state.fileFilters.category && challenge.category !== state.fileFilters.category) return false;
    if (state.fileFilters.chal && String(challenge.id) !== String(state.fileFilters.chal)) return false;
    const q = state.fileFilters.q.trim().toLowerCase();
    if (q) {
      const hay = `${challenge.id} ${challengeDisplayId(challenge)} ${challenge.name || ''} ${challenge.category || ''} ${challenge.source_label || challengeSourceLabel(challengeSourceValue(challenge))}`.toLowerCase();
      const hasFileMatch = state.files.some(file => String(file.challenge_id) === String(challenge.id) && `${file.account_username || ''} ${file.account_id || ''} ${file.original_name || ''} ${file.stored_name || ''} ${file.md5 || ''}`.toLowerCase().includes(q));
      if (!hay.includes(q) && !hasFileMatch) return false;
    }
    return true;
  });
}

function filteredChallengeIdsFromFileView() {
  return filteredChallengesForFileView().map(challenge => String(challenge.id));
}

async function syncAll(options={}) {
  let progress_id = null;
  const startedRealtime = startRealtimeRefresh(['challenges', 'accounts', 'logs']);
  try {
    updateFilterFromDom(false);
    const account_ids = options.accountIds || filteredAccountIds(state.filters.account);
    if (!account_ids.length) throw new Error('当前筛选没有可同步账号');
    const chal_ids = Object.prototype.hasOwnProperty.call(options, 'challengeIds') ? options.challengeIds : filteredChallengeIdsFromChallengeView();
    progress_id = startProgress('同步题目与题解', account_ids.length);
    watchProgress(progress_id, async data => {
      try {
        await refreshKinds(['accounts', 'challenges', 'files', 'flags', 'logs'], true);
        toast(data.ok === false ? (data.message || '同步失败') : '同步完成', data.ok === false ? 'warn' : 'success');
      } finally {
        stopRealtimeRefresh(startedRealtime);
      }
    });
    await api('/api/sync/run', { method: 'POST', body: { account_ids, chal_ids, progress_id } });
    toast('同步任务已在后台开始', 'success');
  } catch (err) {
    stopRealtimeRefresh(startedRealtime);
    stopProgress(progress_id, true);
    toast(err.message);
  }
}

async function updateFiles(options={}) {
  let progress_id = null;
  const startedRealtime = startRealtimeRefresh(['files', 'challenges', 'accounts', 'logs']);
  try {
    updateFileFilterFromDom(false);
    const account_ids = options.accountIds || filteredAccountIds(state.fileFilters.account);
    if (!account_ids.length) throw new Error('当前筛选没有可更新账号');
    const chal_ids = options.challengeIds || filteredChallengeIdsFromFileView();
    const body = { account_ids, chal_ids };
    progress_id = startProgress('按筛选更新附件', account_ids.length);
    body.progress_id = progress_id;
    watchProgress(progress_id, async data => {
      try {
        await refreshKinds(['accounts', 'challenges', 'files', 'flags', 'logs'], true);
        toast(data.ok === false ? (data.message || '附件更新失败') : '附件更新完成', data.ok === false ? 'warn' : 'success');
      } finally {
        stopRealtimeRefresh(startedRealtime);
      }
    });
    await api('/api/files/update', { method: 'POST', body });
    toast('附件更新已在后台开始', 'success');
  } catch (err) {
    stopRealtimeRefresh(startedRealtime);
    stopProgress(progress_id, true);
    toast(err.message);
  }
}

function downloadAllOriginalFiles() {
  const urls = [...new Set(filteredFiles().map(f => f.source_url).filter(Boolean))];
  if (!urls.length) {
    toast('当前筛选结果没有可下载的原始附件链接', 'warn');
    return;
  }
  urls.forEach((url, index) => {
    setTimeout(() => {
      const link = document.createElement('a');
      link.href = url;
      link.download = '';
      link.rel = 'noopener noreferrer';
      link.style.display = 'none';
      document.body.appendChild(link);
      link.click();
      link.remove();
    }, index * 250);
  });
  toast(`已触发 ${urls.length} 个当前筛选原始附件下载`, 'success');
}

async function deleteFileRecord(fileId) {
  if (!fileId || !confirm('确认删除该下载记录和未被引用的本地文件？')) return;
  try {
    await api(`/api/files/${encodeURIComponent(fileId)}`, { method: 'DELETE' });
    await refreshLocalKind('files', false);
    renderFilesTable() || renderMainOrApp();
    toast('已删除文件记录', 'success');
  } catch (err) { toast(err.message); }
}

async function deleteFilteredFiles() {
  const fileIds = filteredFiles().map(file => file.file_id).filter(Boolean);
  if (!fileIds.length) {
    toast('当前筛选结果没有可删除文件', 'warn');
    return;
  }
  if (!confirm(`确认删除当前筛选的 ${fileIds.length} 条下载记录？未被其他记录引用的本地文件也会删除。`)) return;
  try {
    const data = await api('/api/files/delete', { method: 'POST', body: { file_ids: fileIds } });
    await refreshLocalKind('files', false);
    renderFilesTable() || renderMainOrApp();
    toast(`已删除 ${data.deleted || 0} 条文件记录`, 'success');
  } catch (err) { toast(err.message); }
}

async function batchSubmit() {
  let progress_id = null;
  const startedRealtime = startRealtimeRefresh(['accounts', 'challenges', 'files', 'flags', 'logs']);
  try {
    captureSubmitInputs();
    if (!state.batchAccountSelectionDirty) state.batchAccountIds = selectedValues('.batchAccount:checked:not(:disabled)');
    else syncBatchSelectionFromDom();
    pruneBatchSelection();
    const account_ids = [...state.batchAccountIds];
    if (!account_ids.length) throw new Error('请选择至少一个可提交账号');
    const body = {
      account_ids,
      chal_id: document.getElementById('batchChal').value,
      flag: state.submit.batchFlag.trim(),
      md5: state.submit.batchMd5.trim().toLowerCase()
    };
    progress_id = startProgress('批量提交 Flag', account_ids.length);
    body.progress_id = progress_id;
    watchProgress(progress_id, async data => {
      try {
        await refreshKinds(['accounts', 'challenges', 'files', 'flags', 'logs'], true);
        toast(data.ok === false ? (data.message || '批量提交失败') : '批量提交完成', data.ok === false ? 'warn' : 'success');
      } finally {
        stopRealtimeRefresh(startedRealtime);
      }
    });
    await api('/api/submit/batch', { method: 'POST', body });
    state.submit.batchFlag = '';
    toast('批量提交已在后台开始', 'success');
  } catch (err) {
    stopRealtimeRefresh(startedRealtime);
    stopProgress(progress_id, true);
    toast(err.message);
  }
}

function configBodyFromDom() {
  const body = {
    server: {
      host: document.getElementById('cfgHost').value.trim(),
      port: Number(document.getElementById('cfgPort').value || 5000)
    },
    iscc: {
      base_url: document.getElementById('cfgBaseUrl').value.trim(),
      timeout: Number(document.getElementById('cfgTimeout').value || 15),
      operation_delay_seconds: Number(document.getElementById('cfgDelay').value || 0),
      verify_tls: document.getElementById('cfgVerify').checked,
      submit_payload_mode: document.getElementById('cfgPayload').value,
      skip_file_categories: document.getElementById('cfgSkipCats').value.split(',').map(x => x.trim().toUpperCase()).filter(Boolean),
      proxy: {
        enabled: document.getElementById('cfgProxyEnabled').checked,
        list: proxyListFromDom(),
        mode: document.getElementById('cfgProxyMode').value
      }
    }
  };
  state.configDraft = body;
  return body;
}

function proxyListFromDom() {
  return document.getElementById('cfgProxyList').value.split(/\s+/).map(x => x.trim()).filter(Boolean);
}

async function testProxyUi() {
  if (state.proxyTesting) return;
  const body = configBodyFromDom();
  const proxies = proxyListFromDom();
  const maxLatency = Math.max(1, Number(document.getElementById('cfgProxyMaxLatency')?.value || state.proxyMaxLatency || DEFAULT_PROXY_MAX_LATENCY));
  state.proxyMaxLatency = maxLatency;
  if (!proxies.length) {
    state.proxyTestResults = [];
    toast('没有代理可测试', 'warn');
    renderProxyTestResults() || renderMainOrApp();
    return;
  }
  state.proxyTesting = true;
  state.proxyTestResults = proxies.map(proxy => ({ proxy, ok: false, latency_ms: null, status_code: null, status: 'pending' }));
  renderProxyTestResults() || renderMainOrApp();
  let ok = 0;
  for (let index = 0; index < proxies.length; index += 1) {
    state.proxyTestResults[index] = { ...state.proxyTestResults[index], status: 'testing' };
    renderProxyTestResults() || renderMainOrApp();
    try {
      const testBody = JSON.parse(JSON.stringify(body));
      testBody.iscc.proxy.list = [proxies[index]];
      const data = await api('/api/config/proxy-test', { method: 'POST', body: testBody });
      const result = (data.results || [])[0] || {};
      state.proxyTestResults[index] = { ...state.proxyTestResults[index], ...result, proxy: result.proxy || proxies[index], status: 'done' };
      if (state.proxyTestResults[index].ok) ok += 1;
    } catch (err) {
      state.proxyTestResults[index] = { ...state.proxyTestResults[index], ok: false, latency_ms: null, status_code: null, status: 'done' };
    }
    renderProxyTestResults() || renderMainOrApp();
  }
  const tested = state.proxyTestResults.length;
  const aliveResults = state.proxyTestResults.filter(item => item.ok && Number(item.latency_ms || 0) <= maxLatency);
  const aliveProxies = proxies.filter((_, index) => {
    const item = state.proxyTestResults[index];
    return item?.ok && Number(item.latency_ms || 0) <= maxLatency;
  });
  state.proxyTesting = false;
  state.proxyTestResults = aliveResults;
  const input = document.getElementById('cfgProxyList');
  if (input) input.value = aliveProxies.join('\n');
  state.configDraft = configBodyFromDom();
  toast(`代理测试完成：可用 ${aliveProxies.length}/${tested}，已移除 ${tested - aliveProxies.length} 个不可用或超 ${maxLatency}ms 的代理`, aliveProxies.length ? 'success' : 'warn');
  renderProxyTestResults() || renderMainOrApp();
}

async function saveConfigUi() {
  try {
    const body = configBodyFromDom();
    state.config = await api('/api/config', { method: 'PATCH', body });
    state.configDraft = null;
    toast('配置已保存', 'success');
    renderCurrentViewForKinds(['config']) || renderMainOrApp();
  } catch (err) { toast(err.message); }
}

boot();

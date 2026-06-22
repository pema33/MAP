/* MAP — frontend app.js */

// ── State ────────────────────────────────────────────────────────
let servers = [];
let activeServer = null;
let _propsLines = [];
let statsInterval     = null;
let logsInterval      = null;
let playersInterval   = null;
let worldSizeInterval = null;
let currentView     = 'dashboard';

// ── API helpers ──────────────────────────────────────────────────
async function api(path, opts = {}) {
  const res = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('Unauthorized');
  }
  return res.json();
}

async function apiPost(path, body) {
  return api(path, { method: 'POST', body: JSON.stringify(body) });
}

async function apiDelete(path) {
  return api(path, { method: 'DELETE' });
}

// ── Toast ────────────────────────────────────────────────────────
let toastTimer;
function toast(msg, type = 'info') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast toast--${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 3200);
}

// ── Navigation ───────────────────────────────────────────────────
function showView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));

  const panel = document.getElementById('detailPanel');
  panel.classList.add('hidden');

  const viewEl = document.getElementById('view-' + name);
  if (viewEl) viewEl.classList.remove('hidden');

  document.querySelector(`[data-view="${name}"]`)?.classList.add('active');

  const titles = { dashboard: 'Dashboard', servers: 'Servers', backups: 'Backups' };
  document.getElementById('viewTitle').textContent = titles[name] || name;

  currentView = name;

  if (name === 'dashboard') loadDashboard();
  if (name === 'servers')   loadServers();
  if (name === 'backups')   loadBackupsView();
}

// ── Dashboard ────────────────────────────────────────────────────
async function loadDashboard() {
  const [srvData, sysData] = await Promise.all([
    api('/servers'),
    api('/system'),
  ]);

  servers = srvData;

  document.getElementById('statTotal').textContent   = servers.length;
  document.getElementById('statRunning').textContent = servers.filter(s => isRunning(s)).length;
  document.getElementById('statBackups').textContent = sysData.backup_count ?? '—';
  document.getElementById('statDocker').textContent  = sysData.docker_version ?? '—';

  const grid = document.getElementById('dashServerGrid');
  if (!servers.length) {
    grid.innerHTML = '<div class="empty-state">No servers yet. Click <strong>+ New Server</strong> to get started.</div>';
    return;
  }
  grid.innerHTML = servers.map(serverCard).join('');
  bindCardActions();
}

function isRunning(s) {
  return s.status && s.status.toLowerCase().includes('up');
}

function statusBadge(s) {
  const st = (s.status || '').toLowerCase();
  if (st.includes('up'))      return `<span class="sc-badge sc-badge--running"><span class="dot dot--green"></span>Running</span>`;
  if (st.includes('start') || st.includes('creat')) return `<span class="sc-badge sc-badge--starting"><span class="dot dot--yellow"></span>Starting</span>`;
  return `<span class="sc-badge sc-badge--stopped"><span class="dot dot--gray"></span>Stopped</span>`;
}

function serverCard(s) {
  const running = isRunning(s);
  return `
  <div class="server-card server-card--${running ? 'running' : 'stopped'}" data-id="${s.id}">
    <div class="sc-header">
      <div class="sc-name">${esc(s.name)}</div>
      ${statusBadge(s)}
    </div>
    <div class="sc-meta">
      <div>Type: ${s.type || '—'}  v${s.version || '?'}</div>
      <div>Port: ${s.port || '—'}  RAM: ${s.memory || '—'}</div>
    </div>
    <div class="sc-actions">
      ${running
        ? `<button class="btn btn--yellow btn--sm" data-action="restart" data-id="${s.id}">↺ Restart</button>
           <button class="btn btn--red btn--sm" data-action="stop" data-id="${s.id}">■ Stop</button>`
        : `<button class="btn btn--green btn--sm" data-action="start" data-id="${s.id}">▶ Start</button>`}
      <button class="btn btn--ghost btn--sm" data-action="open" data-id="${s.id}">Console →</button>
    </div>
  </div>`;
}

function bindCardActions() {
  document.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', async e => {
      e.stopPropagation();
      const { action, id } = btn.dataset;
      if (action === 'open') { openServer(id); return; }
      await doServerAction(action, id);
      await loadDashboard();
    });
  });
  document.querySelectorAll('.server-card').forEach(card => {
    card.addEventListener('click', e => {
      if (!e.target.closest('button')) openServer(card.dataset.id);
    });
  });
}

async function doServerAction(action, id) {
  const labels = { start: 'Starting', stop: 'Stopping', restart: 'Restarting' };
  toast(`${labels[action] || action}…`, 'info');
  const res = await apiPost(`/servers/${id}/${action}`, {});
  if (res.success) toast(`Server ${action}ed.`, 'success');
  else toast(res.error || 'Action failed', 'error');
}

// ── Servers list ─────────────────────────────────────────────────
async function loadServers() {
  const list = document.getElementById('serverList');
  list.innerHTML = '<div class="empty-state"><span class="spinner"></span></div>';

  servers = await api('/servers');
  if (!servers.length) {
    list.innerHTML = '<div class="empty-state">No servers found.</div>';
    return;
  }

  list.innerHTML = servers.map(s => `
  <div class="server-row" data-id="${s.id}">
    <div class="sr-status">
      <span class="dot ${isRunning(s) ? 'dot--green' : 'dot--gray'}"></span>
    </div>
    <div class="sr-info">
      <div class="sr-name">${esc(s.name)}</div>
      <div class="sr-sub">${s.type || 'VANILLA'} · v${s.version || 'unknown'} · ${s.memory || '?'} RAM</div>
    </div>
    <div class="sr-port">:${s.port || '—'}</div>
    <div class="sr-type">${s.status || 'unknown'}</div>
    <div class="sr-actions">
      ${isRunning(s)
        ? `<button class="btn btn--yellow btn--sm" data-action="restart" data-id="${s.id}">↺</button>
           <button class="btn btn--red btn--sm" data-action="stop" data-id="${s.id}">■</button>`
        : `<button class="btn btn--green btn--sm" data-action="start" data-id="${s.id}">▶</button>`}
      <button class="btn btn--ghost btn--sm" data-action="open" data-id="${s.id}">Open</button>
      <button class="btn btn--red btn--sm" data-action="delete" data-id="${s.id}" title="Delete server">✕</button>
    </div>
  </div>`).join('');

  list.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', async e => {
      e.stopPropagation();
      const { action, id } = btn.dataset;
      if (action === 'open')   { openServer(id); return; }
      if (action === 'delete') { await confirmDelete(id); return; }
      await doServerAction(action, id);
      await loadServers();
    });
  });

  list.querySelectorAll('.server-row').forEach(row => {
    row.addEventListener('click', e => {
      if (!e.target.closest('button')) openServer(row.dataset.id);
    });
  });
}

async function confirmDelete(id) {
  if (!confirm('Delete this server and its world data? This cannot be undone.')) return;
  const res = await apiDelete(`/servers/${id}/delete`);
  if (res.success) { toast('Server deleted', 'success'); await loadServers(); }
  else toast(res.error || 'Delete failed', 'error');
}

// ── Server detail ────────────────────────────────────────────────
function openServer(id) {
  activeServer = servers.find(s => s.id === id) || { id };

  // Show panel, hide views
  document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
  document.getElementById('detailPanel').classList.remove('hidden');

  document.getElementById('detailName').textContent = activeServer.name || id;

  // Mods/Plugins tab is only relevant for mod-loader and plugin-based server types
  const modLoaders = ['FORGE', 'FABRIC'];
  const pluginLoaders = ['SPIGOT', 'PAPER', 'PURPUR'];
  const serverType = (activeServer.type || '').toUpperCase();
  const isPluginServer = pluginLoaders.includes(serverType);
  document.getElementById('modsTabBtn').classList.toggle(
    'hidden', !modLoaders.includes(serverType) && !isPluginServer
  );
  document.getElementById('modsTabBtn').textContent = isPluginServer ? 'Plugins' : 'Mods';

  // Show info tab data
  renderServerInfo();

  // Switch to console tab
  switchTab('console');
  pollLogs();
  pollStats();
}

function closeDetail() {
  clearInterval(statsInterval);
  clearInterval(logsInterval);
  clearInterval(playersInterval);
  clearInterval(worldSizeInterval);
  statsInterval = logsInterval = playersInterval = worldSizeInterval = null;
  activeServer = null;
  document.getElementById('detailPanel').classList.add('hidden');
  showView(currentView === 'dashboard' ? 'dashboard' : 'servers');
}

// ── Tabs ─────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('hidden'));
  document.querySelector(`[data-tab="${name}"]`)?.classList.add('active');
  document.getElementById('tab-' + name)?.classList.remove('hidden');

  if (name === 'backups') loadServerBackups();
  if (name === 'mods') loadMods();
  if (name === 'properties') loadProperties();
  if (name === 'ops') {
    document.getElementById('opsResponse').textContent = '';
    fetchPlayers();
    fetchWhitelist();
  }
}

// ── Mods tab ──────────────────────────────────────────────────────
async function loadMods() {
  if (!activeServer) return;
  const list = document.getElementById('modList');
  list.innerHTML = '<div class="empty-state"><span class="spinner"></span></div>';

  const res = await api(`/servers/${activeServer.id}/mods`);
  const mods = res.mods || [];
  const label = res.addons_dir === 'plugins' ? 'plugin' : 'mod';

  document.getElementById('modsHint').innerHTML =
    `${label === 'plugin' ? 'Plugins' : 'Mods'} are loaded from <code>/${res.addons_dir || 'mods'}</code> in the instance's data volume. Restart the server after adding or removing ${label}s.`;
  document.getElementById('uploadModBtn').textContent = `Upload ${_modLabel()}`;

  if (!mods.length) {
    list.innerHTML = `<div class="empty-state">No ${label}s installed.</div>`;
    return;
  }

  list.innerHTML = mods.map(m => `
  <div class="backup-row">
    <div class="backup-name">${esc(m.filename)}</div>
    <div class="backup-meta backup-size">${formatBytes(m.size)}</div>
    <div class="backup-actions">
      <button class="btn btn--red btn--icon" data-action="del-mod" data-file="${esc(m.filename)}" title="Delete">✕</button>
    </div>
  </div>`).join('');

  list.querySelectorAll('[data-action="del-mod"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm(`Remove ${label} "${btn.dataset.file}"?`)) return;
      const res = await apiDelete(`/servers/${activeServer.id}/mods/${encodeURIComponent(btn.dataset.file)}`);
      if (res.success) { toast(`${label[0].toUpperCase()}${label.slice(1)} removed`, 'success'); loadMods(); }
      else toast(res.error || `Failed to remove ${label}`, 'error');
    });
  });
}

function _modLabel() {
  return ['SPIGOT', 'PAPER', 'PURPUR'].includes((activeServer?.type || '').toUpperCase()) ? 'Plugin' : 'Mod';
}

async function uploadMod() {
  if (!activeServer) return;
  const input = document.getElementById('modFile');
  const file = input.files[0];
  if (!file) { toast('Choose a .jar file first', 'error'); return; }

  const btn = document.getElementById('uploadModBtn');
  btn.disabled = true;
  btn.textContent = 'Uploading…';

  const form = new FormData();
  form.append('file', file);
  const res = await fetch(`/api/servers/${activeServer.id}/mods`, { method: 'POST', body: form })
    .then(r => r.json());

  btn.disabled = false;
  btn.textContent = `Upload ${_modLabel()}`;

  if (res.error) { toast(res.error, 'error'); return; }
  toast(`${_modLabel()} "${res.filename}" added — restart the server to apply`, 'success');
  input.value = '';
  loadMods();
}

async function addModUrl() {
  if (!activeServer) return;
  const input = document.getElementById('modUrl');
  const url = input.value.trim();
  if (!url) { toast('Enter a download URL', 'error'); return; }

  const btn = document.getElementById('addModUrlBtn');
  btn.disabled = true;
  btn.textContent = 'Downloading…';

  const res = await apiPost(`/servers/${activeServer.id}/mods`, { url });

  btn.disabled = false;
  btn.textContent = 'Add from URL';

  if (res.error) { toast(res.error, 'error'); return; }
  toast(`${_modLabel()} "${res.filename}" added — restart the server to apply`, 'success');
  input.value = '';
  loadMods();
}

// ── Console / Logs ───────────────────────────────────────────────
function pollLogs() {
  clearInterval(logsInterval);
  fetchLogs();
  logsInterval = setInterval(fetchLogs, 4000);
}

async function fetchLogs() {
  if (!activeServer) return;
  const data = await api(`/servers/${activeServer.id}/logs?lines=120`);
  renderLogs(data.logs || '');
}

function renderLogs(raw) {
  const el = document.getElementById('consoleOutput');
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;

  const lines = raw.split('\n').filter(l => l && !l.includes('Thread RCON Client')).slice(-120);
  el.innerHTML = lines.map(line => {
    const l = line.toLowerCase();
    let cls = '';
    if (l.includes('warn'))  cls = 'console-line--warn';
    else if (l.includes('error') || l.includes('exception') || l.includes('fatal')) cls = 'console-line--error';
    else if (l.includes('info')) cls = 'console-line--info';
    return `<div class="console-line ${cls}">${esc(line)}</div>`;
  }).join('');

  if (atBottom) el.scrollTop = el.scrollHeight;
}

async function sendCommand(cmd) {
  if (!activeServer || !cmd.trim()) return;
  const out = document.getElementById('consoleOutput');
  out.innerHTML += `<div class="console-line console-line--cmd">/ ${esc(cmd)}</div>`;
  out.scrollTop = out.scrollHeight;

  const res = await apiPost(`/servers/${activeServer.id}/command`, { command: cmd });
  if (res.output) {
    out.innerHTML += `<div class="console-line">${esc(res.output)}</div>`;
    out.scrollTop = out.scrollHeight;
  }
}

// ── Stats ────────────────────────────────────────────────────────
async function fetchStats() {
  if (!activeServer) return;
  const data = await api(`/servers/${activeServer.id}/stats`);
  if (data.error) {
    document.getElementById('dCPU').textContent = '—';
    document.getElementById('dMem').textContent = 'Stopped';
    return;
  }
  document.getElementById('dCPU').textContent = data.cpu + '%';
  document.getElementById('dMem').textContent = data.memory;
}

async function fetchWorldSize() {
  if (!activeServer) return;
  const data = await api(`/servers/${activeServer.id}/world-size`);
  document.getElementById('dWorldSize').textContent = data.size || '—';
}

async function fetchPlayers() {
  if (!activeServer) return;
  const pl = await api(`/servers/${activeServer.id}/players`);
  const match = (pl.output || '').match(/(\d+) of a max of (\d+)/);
  document.getElementById('dPlayers').textContent = match ? `${match[1]} / ${match[2]}` : (pl.players ? String(pl.players.length) : '—');

  const listEl = document.getElementById('connectedPlayersList');
  if (!listEl) return;
  if (!pl.players || pl.players.length === 0) {
    listEl.innerHTML = '<div class="players-empty">No players connected</div>';
  } else {
    listEl.innerHTML = pl.players.map(name =>
      `<div class="player-chip">${esc(name)}</div>`
    ).join('');
  }
}

async function fetchWhitelist() {
  if (!activeServer) return;
  const wl = await api(`/servers/${activeServer.id}/whitelist`);
  const listEl = document.getElementById('whitelistedPlayersList');
  if (!listEl) return;
  if (!wl.players || wl.players.length === 0) {
    listEl.innerHTML = '<div class="players-empty">No players whitelisted</div>';
    return;
  }
  listEl.innerHTML = wl.players.map(name =>
    `<div class="player-chip">${esc(name)} <span class="player-chip-remove" data-player="${esc(name)}" title="Remove from whitelist">✕</span></div>`
  ).join('');
  listEl.querySelectorAll('.player-chip-remove').forEach(el => {
    el.addEventListener('click', () => updateWhitelist(el.dataset.player, 'remove'));
  });
}

async function updateWhitelist(player, action) {
  if (!activeServer || !player) return;
  const res = await apiPost(`/servers/${activeServer.id}/whitelist`, { player, action });
  const el = document.getElementById('opsResponse');
  el.textContent = res.output || (res.success ? `✓ ${action === 'add' ? 'Added' : 'Removed'} ${player}` : '✗ ' + (res.error || 'Error'));
  fetchWhitelist();
}

function pollStats() {
  clearInterval(statsInterval);
  clearInterval(playersInterval);
  clearInterval(worldSizeInterval);
  fetchStats();
  fetchPlayers();
  fetchWorldSize();
  statsInterval     = setInterval(fetchStats,     5000);
  playersInterval   = setInterval(fetchPlayers,   30000);
  worldSizeInterval = setInterval(fetchWorldSize, 30000);
}

// ── OP panel actions ──────────────────────────────────────────────
async function opsCommand(cmd) {
  if (!activeServer) return;
  const res = await apiPost(`/servers/${activeServer.id}/command`, { command: cmd });
  const el = document.getElementById('opsResponse');
  el.textContent = res.output || (res.success ? '✓ Done' : '✗ ' + (res.error || 'Error'));
}

// ── Server Backups tab ────────────────────────────────────────────
async function loadServerBackups() {
  if (!activeServer) return;
  const list = document.getElementById('serverBackupList');
  list.innerHTML = '<div class="empty-state"><span class="spinner"></span></div>';

  const [backups] = await Promise.all([
    api(`/servers/${activeServer.id}/backups`),
    loadSchedule(),
  ]);
  renderBackupList(backups, list, true);
}

async function loadSchedule() {
  if (!activeServer) return;
  const sched = await api(`/servers/${activeServer.id}/schedule`);
  document.getElementById('scheduleEnabled').checked = !!sched.enabled;
  const sel = document.getElementById('scheduleInterval');
  if (sched.interval_hours) sel.value = String(sched.interval_hours);
  renderScheduleStatus(sched);
}

function renderScheduleStatus(sched) {
  const el = document.getElementById('scheduleStatus');
  if (!sched.enabled) {
    el.textContent = '';
    return;
  }
  const parts = [];
  if (sched.next_run_fmt) parts.push(`Next: ${sched.next_run_fmt}`);
  if (sched.last_run_fmt) parts.push(`Last: ${sched.last_run_fmt}`);
  el.textContent = parts.join('  ·  ');
}

async function saveSchedule() {
  if (!activeServer) return;
  const enabled = document.getElementById('scheduleEnabled').checked;
  const interval_hours = parseInt(document.getElementById('scheduleInterval').value);
  const btn = document.getElementById('saveScheduleBtn');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  const res = await apiPost(`/servers/${activeServer.id}/schedule`, { enabled, interval_hours });
  btn.disabled = false;
  btn.textContent = 'Save';
  if (res.error) { toast(res.error, 'error'); return; }
  toast(enabled ? `Auto-backup every ${interval_hours}h — next: ${res.next_run_fmt}` : 'Auto-backup disabled', 'success');
  renderScheduleStatus({ enabled, interval_hours, next_run_fmt: res.next_run_fmt });
}

function renderBackupList(backups, container, withRestore = false) {
  if (!backups.length) {
    container.innerHTML = '<div class="empty-state">No backups yet.</div>';
    return;
  }
  container.innerHTML = backups.map(b => `
  <div class="backup-row">
    <div class="backup-name">${esc(b.filename)}</div>
    <div class="backup-meta">${b.created}</div>
    <div class="backup-meta backup-size">${formatBytes(b.size)}</div>
    <div class="backup-actions">
      ${withRestore ? `<button class="btn btn--yellow btn--sm" data-action="restore" data-file="${esc(b.filename)}">↺ Restore</button>` : ''}
      <a href="/api/backups/${encodeURIComponent(b.filename)}/download" class="btn btn--ghost btn--sm" title="Download">↓</a>
      <button class="btn btn--red btn--icon" data-action="del-backup" data-file="${esc(b.filename)}" title="Delete">✕</button>
    </div>
  </div>`).join('');

  container.querySelectorAll('[data-action="restore"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm(`Restore "${btn.dataset.file}"? This will stop the server and overwrite the current world.`)) return;
      toast('Restoring…', 'info');
      const res = await api(`/backups/${encodeURIComponent(btn.dataset.file)}/restore/${activeServer.id}`, { method: 'POST' });
      if (res.success) toast('Restore complete, server restarted', 'success');
      else toast(res.error || 'Restore failed', 'error');
    });
  });

  container.querySelectorAll('[data-action="del-backup"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm(`Delete backup "${btn.dataset.file}"?`)) return;
      await apiDelete(`/backups/${btn.dataset.file}`);
      if (withRestore) loadServerBackups();
      else loadBackupsView();
    });
  });
}

// ── Backups view (top-level) ──────────────────────────────────────
async function loadBackupsView() {
  const sel = document.getElementById('backupServerSelect');
  servers = await api('/servers');
  sel.innerHTML = '<option value="">— Select a server —</option>' +
    servers.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
}

async function loadBackupsForServer(id) {
  const list = document.getElementById('backupList');
  if (!id) { list.innerHTML = ''; return; }
  list.innerHTML = '<div class="empty-state"><span class="spinner"></span></div>';
  const backups = await api(`/servers/${id}/backups`);
  renderBackupList(backups, list, false);
}

// ── Version loader ────────────────────────────────────────────────
const _versionCache = {};

async function loadVersions(serverType, selectId = 'f-version', currentVersion = null) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  sel.disabled = true;
  sel.innerHTML = '<option value="">Loading…</option>';

  if (!_versionCache[serverType]) {
    try {
      _versionCache[serverType] = await api(`/versions?type=${encodeURIComponent(serverType)}`);
    } catch (_) {
      _versionCache[serverType] = [];
    }
  }

  const versions = _versionCache[serverType];
  sel.innerHTML = versions.map(v =>
    `<option value="${esc(v)}" ${v === currentVersion ? 'selected' : ''}>${esc(v)}</option>`
  ).join('') || '<option value="" disabled>No versions found</option>';
  sel.disabled = false;
}

// ── Create server ─────────────────────────────────────────────────
async function createServer() {
  const body = {
    name:        document.getElementById('f-name').value.trim().replace(/[^a-zA-Z0-9_-]/g, '-'),
    version:     document.getElementById('f-version').value,
    type:        document.getElementById('f-type').value,
    memory:      (parseInt(document.getElementById('f-memory').value) || 2048) + 'M',
    port:        parseInt(document.getElementById('f-port').value) || 25565,
    difficulty:  document.getElementById('f-difficulty').value,
    max_players: parseInt(document.getElementById('f-maxplayers').value) || 20,
    motd:        document.getElementById('f-motd').value.trim(),
    whitelist:   document.getElementById('f-whitelist').value.trim(),
  };

  if (!body.name) { toast('Server name is required', 'error'); return; }
  if (parseInt(document.getElementById('f-memory').value) < 256) { toast('Minimum memory is 256 MiB', 'error'); return; }

  const btn = document.getElementById('confirmCreate');
  btn.disabled = true;
  btn.textContent = 'Launching…';

  const res = await apiPost('/servers', body);
  btn.disabled = false;
  btn.textContent = 'Launch Server';

  if (res.error) { toast(res.error, 'error'); return; }

  document.getElementById('createModal').classList.add('hidden');
  toast(`Creating "${body.name}"… pulling image, this may take a minute.`, 'info');
  pollForServer(body.name);
}

async function pollForServer(name) {
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 3000));
    const status = await api(`/servers/${encodeURIComponent(name)}/creation-status`);
    if (status.status === 'done') {
      toast(`Server "${name}" is ready!`, 'success');
      await loadDashboard();
      return;
    }
    if (status.status === 'error') {
      toast(`Failed to create "${name}": ${status.error}`, 'error');
      return;
    }
  }
  toast(`"${name}" is taking longer than expected — check the server logs.`, 'error');
}

// ── Server info tab ───────────────────────────────────────────────
function renderServerIcon() {
  const s = activeServer;
  const img = document.getElementById('serverIconImg');
  const placeholder = document.getElementById('serverIconPlaceholder');
  if (s.has_icon) {
    img.src = `/api/servers/${s.id}/icon?_=${Date.now()}`;
    img.classList.remove('hidden');
    placeholder.classList.add('hidden');
  } else {
    img.classList.add('hidden');
    img.removeAttribute('src');
    placeholder.classList.remove('hidden');
  }
}

function renderServerInfo() {
  if (!activeServer) return;
  const s = activeServer;

  renderServerIcon();

  document.getElementById('uploadIconBtn').onclick = async () => {
    const input = document.getElementById('serverIconFile');
    const file = input.files[0];
    if (!file) { toast('Choose an image file first', 'error'); return; }

    const btn = document.getElementById('uploadIconBtn');
    btn.disabled = true;
    btn.textContent = 'Uploading…';

    const form = new FormData();
    form.append('file', file);
    const res = await fetch(`/api/servers/${s.id}/icon`, { method: 'POST', body: form })
      .then(r => r.json());

    btn.disabled = false;
    btn.textContent = 'Upload Icon';

    if (res.error) { toast(res.error, 'error'); return; }
    toast('Server icon updated', 'success');
    input.value = '';
    activeServer.has_icon = true;
    renderServerIcon();
  };

  document.getElementById('removeIconBtn').onclick = async () => {
    const res = await apiDelete(`/servers/${s.id}/icon`);
    if (res.error) { toast(res.error, 'error'); return; }
    toast('Server icon removed', 'success');
    activeServer.has_icon = false;
    renderServerIcon();
  };

  const info = [
    ['Container ID', s.id],
    ['Name',         s.name],
    ['Status',       s.status || '—'],
    ['Port',         s.port ? `:${s.port}` : '—'],
    ['Type',         s.type || '—'],
    ['Created',      s.created || '—'],
  ];

  const currentMiB = memToMiB(s.memory);
  const currentMotd = s.motd || '';

  document.getElementById('serverInfo').innerHTML = info.map(([k, v]) => `
    <div class="info-item">
      <div class="stat-label">${k}</div>
      <div class="stat-value">${esc(String(v))}</div>
    </div>`).join('') + `
    <div class="info-item">
      <div class="stat-label">Version</div>
      <div class="info-memory-control">
        <select class="select" id="versionSelect"><option value="">Loading…</option></select>
        <button class="btn btn--primary btn--sm" id="applyVersionBtn">Apply</button>
      </div>
    </div>
    <div class="info-item">
      <div class="stat-label">Memory (MiB)</div>
      <div class="info-memory-control">
        <input type="number" class="input" id="memoryInput" value="${currentMiB}" min="256" step="256">
        <button class="btn btn--primary btn--sm" id="applyMemoryBtn">Apply</button>
      </div>
    </div>
    <div class="info-item">
      <div class="stat-label">MOTD</div>
      <div class="info-memory-control">
        <input type="text" class="input" id="motdInput" value="${esc(currentMotd)}" placeholder="A Minecraft Server">
        <button class="btn btn--primary btn--sm" id="applyMotdBtn">Apply</button>
      </div>
    </div>
    <div class="info-item">
      <div class="stat-label">Max Players</div>
      <div class="info-memory-control">
        <input type="number" class="input" id="maxPlayersInput" value="${parseInt(s.max_players) || 20}" min="1" step="1">
        <button class="btn btn--primary btn--sm" id="applyMaxPlayersBtn">Apply</button>
      </div>
    </div>`;

  loadVersions(s.type || 'PAPER', 'versionSelect', s.version);

  document.getElementById('applyVersionBtn').addEventListener('click', async () => {
    const version = document.getElementById('versionSelect').value;
    if (!version) { toast('Select a version first', 'error'); return; }
    const btn = document.getElementById('applyVersionBtn');
    btn.disabled = true;
    btn.textContent = 'Applying…';
    const res = await apiPost(`/servers/${s.id}/version`, { version });
    btn.disabled = false;
    btn.textContent = 'Apply';
    if (res.error) { toast(res.error, 'error'); return; }
    toast(`Version updated to ${version}, server restarting…`, 'success');
    activeServer.version = version;
    activeServer.id = res.id;
    renderServerInfo();
  });

  document.getElementById('applyMemoryBtn').addEventListener('click', async () => {
    const mib = parseInt(document.getElementById('memoryInput').value);
    if (!mib || mib < 256) { toast('Minimum memory is 256 MiB', 'error'); return; }
    const btn = document.getElementById('applyMemoryBtn');
    btn.disabled = true;
    btn.textContent = 'Applying…';
    const res = await apiPost(`/servers/${s.id}/memory`, { memory_mib: mib });
    btn.disabled = false;
    btn.textContent = 'Apply';
    if (res.error) { toast(res.error, 'error'); return; }
    toast(`Memory updated to ${mib} MiB, server restarting…`, 'success');
    activeServer.memory = `${mib}M`;
    activeServer.id = res.id;
    renderServerInfo();
  });

  document.getElementById('applyMotdBtn').addEventListener('click', async () => {
    const motd = document.getElementById('motdInput').value.trim();
    if (!motd) { toast('MOTD cannot be empty', 'error'); return; }
    const btn = document.getElementById('applyMotdBtn');
    btn.disabled = true;
    btn.textContent = 'Applying…';
    const res = await apiPost(`/servers/${s.id}/motd`, { motd });
    btn.disabled = false;
    btn.textContent = 'Apply';
    if (res.error) { toast(res.error, 'error'); return; }
    toast('MOTD updated, server restarting…', 'success');
    activeServer.motd = motd;
    activeServer.id = res.id;
    renderServerInfo();
  });

  document.getElementById('applyMaxPlayersBtn').addEventListener('click', async () => {
    const max_players = parseInt(document.getElementById('maxPlayersInput').value);
    if (!max_players || max_players < 1) { toast('Max players must be at least 1', 'error'); return; }
    const btn = document.getElementById('applyMaxPlayersBtn');
    btn.disabled = true;
    btn.textContent = 'Applying…';
    const res = await apiPost(`/servers/${s.id}/max-players`, { max_players });
    btn.disabled = false;
    btn.textContent = 'Apply';
    if (res.error) { toast(res.error, 'error'); return; }
    toast(`Max players updated to ${max_players}, server restarting…`, 'success');
    activeServer.max_players = String(max_players);
    activeServer.id = res.id;
    renderServerInfo();
  });
}

// ── Properties tab ───────────────────────────────────────────────
async function loadProperties() {
  if (!activeServer) return;
  const list = document.getElementById('propsList');
  list.innerHTML = '<div class="empty-state"><span class="spinner"></span></div>';
  document.getElementById('propsFilter').value = '';
  document.getElementById('propertiesStatus').textContent = '';

  const res = await api(`/servers/${activeServer.id}/properties`);
  if (res.error) {
    list.innerHTML = `<div class="empty-state">${esc(res.error)}</div>`;
    return;
  }
  _propsLines = res.lines || [];
  renderProps('');
}

function renderProps(filter) {
  const list = document.getElementById('propsList');
  const f = filter.toLowerCase();
  const props = _propsLines.filter(l =>
    l.type === 'property' && (!f || l.key.toLowerCase().includes(f) || l.value.toLowerCase().includes(f))
  );

  if (!props.length) {
    list.innerHTML = '<div class="empty-state">No matching properties.</div>';
    return;
  }

  list.innerHTML = props.map(p => `
    <div class="prop-row">
      <div class="prop-key" title="${esc(p.key)}">${esc(p.key)}</div>
      <input type="text" class="input prop-val" value="${esc(p.value)}" data-key="${esc(p.key)}">
    </div>`).join('');

  list.querySelectorAll('.prop-val').forEach(input => {
    input.addEventListener('input', () => {
      const entry = _propsLines.find(l => l.type === 'property' && l.key === input.dataset.key);
      if (entry) entry.value = input.value;
      document.getElementById('propertiesStatus').textContent = 'Unsaved changes';
    });
  });
}

async function saveProperties() {
  if (!activeServer) return;
  const btn = document.getElementById('savePropertiesBtn');
  btn.disabled = true;
  btn.textContent = 'Saving…';

  const res = await apiPost(`/servers/${activeServer.id}/properties`, { lines: _propsLines });
  btn.disabled = false;
  btn.textContent = 'Save Changes';

  if (res.error) { toast(res.error, 'error'); return; }
  toast('server.properties saved — restart the server to apply changes', 'success');
  document.getElementById('propertiesStatus').textContent = 'Saved';
}

// ── MOTD preview renderer ─────────────────────────────────────────
function renderMotdPreview(text) {
  const COLORS = {
    '0':'#000000','1':'#0000AA','2':'#00AA00','3':'#00AAAA',
    '4':'#AA0000','5':'#AA00AA','6':'#FFAA00','7':'#AAAAAA',
    '8':'#555555','9':'#5555FF','a':'#55FF55','b':'#55FFFF',
    'c':'#FF5555','d':'#FF55FF','e':'#FFFF55','f':'#FFFFFF',
  };
  if (!text) return '<span class="motd-placeholder">A Minecraft Server</span>';
  const normalized = text.replace(/&([0-9a-frlonmkA-FRLONMK])/g, (_, c) => '§' + c.toLowerCase());
  let html = '', color = null, bold = false, italic = false, under = false, strike = false;
  let i = 0;
  while (i < normalized.length) {
    if (normalized[i] === '§' && i + 1 < normalized.length) {
      const c = normalized[++i].toLowerCase(); i++;
      if (COLORS[c]) { color = COLORS[c]; bold = italic = under = strike = false; }
      else if (c === 'l') bold = true;
      else if (c === 'o') italic = true;
      else if (c === 'n') under = true;
      else if (c === 'm') strike = true;
      else if (c === 'r') { color = null; bold = italic = under = strike = false; }
    } else {
      let chunk = '';
      while (i < normalized.length && normalized[i] !== '§') chunk += normalized[i++];
      if (chunk) {
        let style = color ? `color:${color};` : '';
        if (bold) style += 'font-weight:700;';
        if (italic) style += 'font-style:italic;';
        const deco = [under && 'underline', strike && 'line-through'].filter(Boolean).join(' ');
        if (deco) style += `text-decoration:${deco};`;
        html += style ? `<span style="${style}">${esc(chunk)}</span>` : esc(chunk);
      }
    }
  }
  return html || '<span class="motd-placeholder">A Minecraft Server</span>';
}

// ── Helpers ───────────────────────────────────────────────────────
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function memToMiB(mem) {
  if (!mem) return 2048;
  const s = String(mem).toUpperCase();
  if (s.endsWith('G')) return parseInt(s) * 1024;
  if (s.endsWith('M')) return parseInt(s);
  return 2048;
}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
  return (bytes / 1073741824).toFixed(2) + ' GB';
}

// ── Boot ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {

  // Nav
  document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', e => {
      e.preventDefault();
      showView(item.dataset.view);
    });
  });

  // New server button
  document.getElementById('newServerBtn').addEventListener('click', () => {
    document.getElementById('createModal').classList.remove('hidden');
    loadVersions(document.getElementById('f-type').value);
  });

  document.getElementById('f-type').addEventListener('change', e => loadVersions(e.target.value));

  document.getElementById('closeModal').addEventListener('click', () => {
    document.getElementById('createModal').classList.add('hidden');
  });

  document.getElementById('cancelCreate').addEventListener('click', () => {
    document.getElementById('createModal').classList.add('hidden');
  });

  document.getElementById('confirmCreate').addEventListener('click', createServer);

  // Close modal on overlay click
  document.getElementById('createModal').addEventListener('click', e => {
    if (e.target === e.currentTarget) e.currentTarget.classList.add('hidden');
  });

  // Detail panel controls
  document.getElementById('closeDetail').addEventListener('click', closeDetail);

  document.getElementById('detailStart').addEventListener('click', async () => {
    await doServerAction('start', activeServer.id);
    pollStats();
  });
  document.getElementById('detailStop').addEventListener('click', async () => {
    await doServerAction('stop', activeServer.id);
    clearInterval(statsInterval);
  });
  document.getElementById('detailRestart').addEventListener('click', async () => {
    await doServerAction('restart', activeServer.id);
  });

  // Console send
  const cmdInput = document.getElementById('cmdInput');
  const sendBtn  = document.getElementById('sendCmd');
  sendBtn.addEventListener('click', () => {
    sendCommand(cmdInput.value);
    cmdInput.value = '';
  });
  cmdInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') { sendCommand(cmdInput.value); cmdInput.value = ''; }
  });

  // Quick commands
  document.querySelectorAll('.qbtn[data-cmd]').forEach(btn => {
    btn.addEventListener('click', () => sendCommand(btn.dataset.cmd));
  });

  // Tabs
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
  });

  // OP panel
  document.getElementById('opBtn').addEventListener('click', () => {
    const p = document.getElementById('opPlayer').value.trim();
    if (p) opsCommand(`op ${p}`);
  });
  document.getElementById('deopBtn').addEventListener('click', () => {
    const p = document.getElementById('opPlayer').value.trim();
    if (p) opsCommand(`deop ${p}`);
  });
  document.getElementById('kickBtn').addEventListener('click', () => {
    const p = document.getElementById('kickPlayer').value.trim();
    if (p) opsCommand(`kick ${p}`);
  });
  document.getElementById('banBtn').addEventListener('click', () => {
    const p = document.getElementById('kickPlayer').value.trim();
    if (p) opsCommand(`ban ${p}`);
  });
  document.getElementById('pardonBtn').addEventListener('click', () => {
    const p = document.getElementById('kickPlayer').value.trim();
    if (p) opsCommand(`pardon ${p}`);
  });
  document.getElementById('giveBtn').addEventListener('click', () => {
    const player = document.getElementById('givePlayer').value.trim() || '@p';
    const item   = document.getElementById('giveItem').value.trim();
    const amount = document.getElementById('giveAmount').value || 1;
    if (item) opsCommand(`give ${player} ${item} ${amount}`);
  });

  document.querySelectorAll('.qbtn[data-gm]').forEach(btn => {
    btn.addEventListener('click', () => {
      const player = document.getElementById('gmPlayer').value.trim() || '@a';
      opsCommand(`gamemode ${btn.dataset.gm} ${player}`);
    });
  });

  // Refresh connected players
  document.getElementById('refreshPlayersBtn').addEventListener('click', fetchPlayers);

  // Whitelist
  document.getElementById('whitelistAddBtn').addEventListener('click', () => {
    const p = document.getElementById('whitelistPlayer').value.trim();
    if (p) updateWhitelist(p, 'add');
  });
  document.getElementById('whitelistRemoveBtn').addEventListener('click', () => {
    const p = document.getElementById('whitelistPlayer').value.trim();
    if (p) updateWhitelist(p, 'remove');
  });
  document.getElementById('refreshWhitelistBtn').addEventListener('click', fetchWhitelist);

  // Mods
  document.getElementById('uploadModBtn').addEventListener('click', uploadMod);
  document.getElementById('addModUrlBtn').addEventListener('click', addModUrl);

  // Schedule
  document.getElementById('saveScheduleBtn').addEventListener('click', saveSchedule);

  // Create backup
  document.getElementById('createBackupBtn').addEventListener('click', async () => {
    if (!activeServer) return;
    const label = document.getElementById('backupLabel').value.trim();
    toast('Creating backup…', 'info');
    const res = await apiPost(`/servers/${activeServer.id}/backup`, { label });
    if (res.success) {
      toast(`Backup saved: ${res.filename}`, 'success');
      document.getElementById('backupLabel').value = '';
      loadServerBackups();
    } else {
      toast(res.error || 'Backup failed', 'error');
    }
  });

  // Properties tab
  document.getElementById('savePropertiesBtn').addEventListener('click', saveProperties);
  document.getElementById('reloadPropsBtn').addEventListener('click', loadProperties);
  document.getElementById('propsFilter').addEventListener('input', e => renderProps(e.target.value));

  // Backups view server select
  document.getElementById('backupServerSelect').addEventListener('change', e => {
    loadBackupsForServer(e.target.value);
  });

  // Logout
  document.getElementById('logoutBtn').addEventListener('click', async () => {
    await fetch('/logout', { method: 'POST' });
    window.location.href = '/login';
  });

  // Change password modal
  document.getElementById('changePasswordBtn').addEventListener('click', () => {
    document.getElementById('cp-current').value = '';
    document.getElementById('cp-new').value = '';
    document.getElementById('cp-confirm').value = '';
    document.getElementById('changePasswordModal').classList.remove('hidden');
    document.getElementById('cp-current').focus();
  });

  const closeChangePw = () => document.getElementById('changePasswordModal').classList.add('hidden');
  document.getElementById('closeChangePassword').addEventListener('click', closeChangePw);
  document.getElementById('cancelChangePassword').addEventListener('click', closeChangePw);
  document.getElementById('changePasswordModal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeChangePw();
  });

  document.getElementById('confirmChangePassword').addEventListener('click', async () => {
    const current     = document.getElementById('cp-current').value;
    const newPw       = document.getElementById('cp-new').value;
    const confirm     = document.getElementById('cp-confirm').value;
    if (newPw.length < 8)      { toast('Password must be at least 8 characters', 'error'); return; }
    if (newPw !== confirm)     { toast('Passwords do not match', 'error'); return; }
    const btn = document.getElementById('confirmChangePassword');
    btn.disabled = true;
    btn.textContent = 'Updating…';
    const res = await api('/change-password', {
      method: 'POST',
      body: JSON.stringify({ current, new_password: newPw }),
    });
    btn.disabled = false;
    btn.textContent = 'Update Password';
    if (res.error) { toast(res.error, 'error'); return; }
    toast('Password updated', 'success');
    closeChangePw();
  });

  // Initial load
  loadDashboard();
});

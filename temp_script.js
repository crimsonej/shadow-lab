const API = '';  // same origin
let currentServerId = null;
let editingServerId = null;
let currentTab = 'models';
let addMode = 'api';
let serverStatuses = {};
let newKeyValue = '';
let deployInterval = null;

function setAddMode(mode) {
  addMode = mode;
  $('tab-mode-api').classList.toggle('active', mode === 'api');
  $('tab-mode-ssh').classList.toggle('active', mode === 'ssh');
  $('fields-api').classList.toggle('hidden', mode !== 'api');
  $('fields-ssh').classList.toggle('hidden', mode !== 'ssh');
  $('btn-save-server').textContent = mode === 'api' ? 'Save Server' : 'Start Deployment';
}

function startDeployment() {
  const host = $('f-ssh-host').value.trim();
  const user = $('f-ssh-user').value.trim();
  const port = $('f-ssh-port').value.trim() || '22';
  const pass = $('f-ssh-pass').value;
  const notes = $('f-srv-notes').value.trim();
  const name = $('f-ssh-name').value.trim() || host;

  if (!host || !user) { toast('IP and Username are required', 'error'); return; }
  if (!pass) { toast('SSH Password is required', 'error'); return; }

  closeModal('modal-server');
  $('modal-deploy').classList.remove('hidden');
  $('deploy-terminal').textContent = '';
  $('deploy-spinner').classList.remove('hidden');
  $('btn-deploy-close').classList.add('hidden');
  $('deploy-modal-title').textContent = `Deploying to ${host}...`;
  $('deploy-status-text').textContent = 'Connecting...';

  apiFetch('/api/deploy/start', {
    method: 'POST',
    body: { host, port: parseInt(port), username: user, password: pass, server_name: name }
  }).then(data => {
    trackDeployment(data.deploy_id, name, notes);
  }).catch(e => {
    $('deploy-terminal').textContent += `\nERROR: ${e.message}`;
    $('deploy-spinner').classList.add('hidden');
    $('btn-deploy-close').classList.remove('hidden');
    $('deploy-status-text').textContent = 'Failed to start deployment';
  });
}

function trackDeployment(id, name, notes) {
  if (deployInterval) clearInterval(deployInterval);
  
  deployInterval = setInterval(async () => {
    try {
      const data = await apiFetch(`/api/deploy/status/${id}`);
      $('deploy-terminal').textContent = data.logs.join('\n');
      $('deploy-terminal').scrollTop = $('deploy-terminal').scrollHeight;

      if (data.status === 'success') {
        clearInterval(deployInterval);
        $('deploy-spinner').classList.add('hidden');
        $('deploy-modal-title').textContent = '✓ Deployment Complete!';
        $('deploy-status-text').innerHTML = `<span style="color:var(--success)">Agent online at ${data.agent_url}</span>`;
        $('btn-deploy-close').classList.remove('hidden');
        toast('Deployment successful!', 'success');
        // Auto-register server in dashboard
        await apiFetch('/api/servers', {
          method: 'POST',
          body: { name, host: data.agent_url, admin_token: data.token, notes }
        });
        loadOverview();
      } else if (data.status === 'failed') {
        clearInterval(deployInterval);
        $('deploy-spinner').classList.add('hidden');
        $('deploy-modal-title').textContent = '✗ Deployment Failed';
        $('deploy-status-text').innerHTML = `<span style="color:var(--danger)">Check the logs above for details</span>`;
        $('btn-deploy-close').classList.remove('hidden');
        toast('Deployment failed — see logs', 'error');
      } else {
        $('deploy-status-text').textContent = `Status: ${data.status.toUpperCase()} (${data.logs.length} steps)`;
      }
    } catch (e) {
      console.error(e);
    }
  }, 1500);
}

// ── Utilities ────────────────────────────────────────────────────────────────

const $ = id => document.getElementById(id);
const toast = (msg, type='info') => {
  const el = $('toast');
  el.textContent = msg;
  el.style.borderColor = type==='error'?'var(--danger)':type==='success'?'var(--success)':'var(--border)';
  el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'), 3200);
};

const fmt = n => n==null?'—':Number(n).toLocaleString();
const fmtBytes = b => {
  if(!b) return '—';
  if(b>1e9) return (b/1e9).toFixed(1)+'GB';
  if(b>1e6) return (b/1e6).toFixed(1)+'MB';
  return b+'B';
};
const fmtDate = s => s ? new Date(s).toLocaleDateString()+' '+new Date(s).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '—';
const truncateKey = k => k ? k.slice(0,12)+'…'+k.slice(-4) : '—';

async function apiFetch(path, opts={}) {
  const r = await fetch(API+path, {
    headers: {'Content-Type':'application/json', ...(opts.headers||{})},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(data.detail || data.error || r.statusText);
  return data;
}

// ── Views ────────────────────────────────────────────────────────────────────

function showView(view) {
  ['overview','server-detail','servers'].forEach(v=>{
    $(`view-${v}`).classList.toggle('hidden', v!==view);
  });
  ['overview','servers'].forEach(n=>{
    $(`nav-${n}`)?.classList.toggle('active', n===view);
  });
  if(view==='overview') loadOverview();
  if(view==='servers') loadServersTable();
}

function switchTab(tab) {
  currentTab = tab;
  const allTabs = ['models','keys','testing','lifecycle','uptime','logs'];
  document.querySelectorAll('#detail-tabs .tab').forEach((t,i)=>{
    t.classList.toggle('active', allTabs[i]===tab);
  });
  allTabs.forEach(t => {
    const el = $(`tab-${t}`);
    if(el) el.classList.toggle('hidden', t!==tab);
  });
  if(tab==='keys') loadKeys(currentServerId);
  if(tab==='testing') {} // loaded on-demand via buttons
  if(tab==='lifecycle') loadLifecycleStatus();
  if(tab==='uptime') loadUptime();
  if(tab==='logs') loadLogs('recent');
}

// ── Overview ─────────────────────────────────────────────────────────────────

async function loadOverview() {
  const servers = await apiFetch('/api/servers').catch(()=>[]);
  $('stat-total').textContent = servers.length;

  let online=0, models=0, keys=0;
  const cards = [];

  const statuses = await Promise.all(servers.map(s =>
    apiFetch(`/api/servers/${s.id}/status`).catch(()=>({online:false,metrics:null,models:[]}))
  ));

  statuses.forEach((st,i)=>{
    const s = servers[i];
    serverStatuses[s.id] = st;
    if(st.online) online++;
    models += (st.models||[]).length;
    cards.push(buildServerCard(s, st));
  });

  $('stat-online').textContent = online;
  $('stat-models').textContent = models;

  // Load keys count
  Promise.all(servers.map(s=>
    apiFetch(`/api/servers/${s.id}/keys`).catch(()=>[])
  )).then(allKeys=>{
    let total = 0;
    allKeys.forEach(k=>{ total+=(Array.isArray(k)?k:Object.values(k)).length; });
    $('stat-keys').textContent = total;
  });

  $('overview-server-cards').innerHTML = cards.length
    ? cards.join('')
    : `<div class="empty"><div class="empty-icon">🖥️</div><div class="empty-title">No servers yet</div>
       <p style="font-size:13px;margin-top:4px">Click "Add Server" to register your first remote agent.</p></div>`;

  buildSidebarServerList(servers, statuses);
}

function buildServerCard(s, st) {
  const badge = st.online
    ? `<span class="badge badge-online">● Online</span>`
    : `<span class="badge badge-offline">● Offline</span>`;
  const m = st.metrics || {};
  const cpu = m.cpu_percent ?? 0;
  const ram = m.ram ?? {};
  const gpu = m.gpu?.gpus?.[0];

  const modelsHtml = (st.models||[]).slice(0,4).map(mod=>
    `<span class="model-pill">${mod.name}</span>`
  ).join(' ') || '<span style="color:var(--muted);font-size:12px">No models</span>';

  return `
  <div class="card" style="margin-bottom:16px;cursor:pointer" onclick="openServerDetail(${s.id})">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px">
      <div>
        <div style="font-size:15px;font-weight:700">${s.name}</div>
        <div style="font-size:12px;color:var(--muted);margin-top:3px">${s.host||'—'}</div>
      </div>
      ${badge}
    </div>
    ${st.online ? `
    <div class="grid-4" style="gap:10px;margin-bottom:14px">
      <div>
        <div style="font-size:11px;color:var(--muted)">CPU</div>
        <div style="font-size:18px;font-weight:700">${cpu.toFixed(0)}%</div>
        <div class="progress-bar"><div class="progress-fill fill-cpu" style="width:${cpu}%"></div></div>
      </div>
      <div>
        <div style="font-size:11px;color:var(--muted)">RAM</div>
        <div style="font-size:18px;font-weight:700">${ram.percent?.toFixed(0)||0}%</div>
        <div class="progress-bar"><div class="progress-fill fill-ram" style="width:${ram.percent||0}%"></div></div>
      </div>
      ${gpu ? `<div>
        <div style="font-size:11px;color:var(--muted)">GPU</div>
        <div style="font-size:18px;font-weight:700">${gpu.utilization_percent?.toFixed(0)||0}%</div>
        <div class="progress-bar"><div class="progress-fill fill-gpu" style="width:${gpu.utilization_percent||0}%"></div></div>
      </div>` : ''}
    </div>` : ''}
    <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
      <span style="font-size:11px;color:var(--muted);margin-right:4px">Models:</span>
      ${modelsHtml}
    </div>
  </div>`;
}

function buildSidebarServerList(servers, statuses) {
  $('server-nav-list').innerHTML = servers.map((s,i)=>{
    const online = statuses[i]?.online;
    return `<div class="server-nav-item ${currentServerId===s.id?'active':''}" onclick="openServerDetail(${s.id})">
      <div class="dot ${online?'online':'offline'}"></div>
      <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.name}</div>
    </div>`;
  }).join('') || '<div style="padding:10px 14px;font-size:12px;color:var(--muted)">No servers</div>';
}

// ── Server Detail ─────────────────────────────────────────────────────────────

function openServerDetail(id) {
  currentServerId = id;
  showView('server-detail');
  loadServerDetail(id);
}

async function loadServerDetail(id) {
  const server = (await apiFetch('/api/servers').catch(()=>[])).find(s=>s.id===id);
  if(!server) return;

  $('detail-server-name').textContent = server.name;
  $('detail-server-host').textContent = server.host||'—';

  const st = await apiFetch(`/api/servers/${id}/status`).catch(()=>({online:false,state:'offline',metrics:null,models:[]}));
  serverStatuses[id] = st;

  let badgeHtml = '';
  if(st.state === 'idle') badgeHtml = '<span class="badge badge-warn">💤 Idle</span>';
  else if(st.online) badgeHtml = '<span class="badge badge-online">● Online</span>';
  else badgeHtml = '<span class="badge badge-offline">● Offline</span>';

  let activeModelName = null;
  (st.models||[]).forEach(m => { if(m.active) activeModelName = m.name; });
  if (activeModelName && st.online) {
      badgeHtml += `<span class="badge" style="background:#1a4d2e;color:#4ade80;border:1px solid #14532d;margin-left:8px">🌟 Active Model: ${activeModelName}</span>`;
  }

  $('detail-status-badge').innerHTML = badgeHtml;

  // Disable controls if offline
  const isOffline = st.state === 'offline' || !st.online;
  ['btn-activate','btn-deactivate','btn-restart-ai','btn-idle','btn-restart-ollama'].forEach(btnId => {
    const btn = $(btnId);
    if(btn) btn.style.opacity = isOffline ? '0.5' : '1';
    if(btn) btn.style.pointerEvents = isOffline ? 'none' : 'auto';
  });

  renderMetricCards(st.metrics);
  renderModelsTable(st.models || []);

  if(currentTab==='keys') loadKeys(id);
}

function renderMetricCards(m) {
  if(!m) { $('detail-metric-cards').innerHTML=''; return; }
  const cpu = m.cpu_percent??0;
  const ram = m.ram??{};
  const disk = m.disk??{};
  const gpu = m.gpu?.gpus?.[0];
  const uptime = m.agent_uptime_seconds ?? 0;
  const h = Math.floor(uptime/3600), min = Math.floor((uptime%3600)/60);

  $('detail-metric-cards').innerHTML = `
    <div class="card">
      <div class="card-title">CPU Usage</div>
      <div class="card-value">${cpu.toFixed(1)}%</div>
      <div class="progress-bar" style="margin-top:10px"><div class="progress-fill fill-cpu" style="width:${cpu}%"></div></div>
    </div>
    <div class="card">
      <div class="card-title">RAM</div>
      <div class="card-value">${ram.percent?.toFixed(1)||0}%</div>
      <div class="card-sub">${ram.used_gb?.toFixed(1)||0} / ${ram.total_gb?.toFixed(1)||0} GB</div>
      <div class="progress-bar" style="margin-top:8px"><div class="progress-fill fill-ram" style="width:${ram.percent||0}%"></div></div>
    </div>
    <div class="card">
      <div class="card-title">Disk</div>
      <div class="card-value">${disk.percent?.toFixed(1)||0}%</div>
      <div class="card-sub">${disk.used_gb?.toFixed(1)||0} / ${disk.total_gb?.toFixed(1)||0} GB</div>
      <div class="progress-bar" style="margin-top:8px"><div class="progress-fill fill-disk" style="width:${disk.percent||0}%"></div></div>
    </div>
    ${gpu ? `<div class="card">
      <div class="card-title">GPU · ${gpu.name||'GPU'}</div>
      <div class="card-value">${gpu.utilization_percent?.toFixed(0)||0}%</div>
      <div class="card-sub">${gpu.memory_used_mb?.toFixed(0)||0} / ${gpu.memory_total_mb?.toFixed(0)||0} MB VRAM · ${gpu.temperature_c||'—'}°C</div>
      <div class="progress-bar" style="margin-top:8px"><div class="progress-fill fill-gpu" style="width:${gpu.utilization_percent||0}%"></div></div>
    </div>` : `<div class="card">
      <div class="card-title">Uptime</div>
      <div class="card-value">${h}h ${min}m</div>
      <div class="card-sub">Agent running</div>
    </div>`}
  `;
}

function renderModelsTable(models) {
  if(!models.length) {
    $('models-table-body').innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:32px">
      No models installed. Pull one above ↑</td></tr>`;
    return;
  }
  
  $('models-table-body').innerHTML = models.map(m => {
    const size = fmtBytes(m.size);
    const family = m.details?.family || '—';
    const modified = m.modified_at ? fmtDate(m.modified_at) : '—';
    
    // Status indicators
    const isLoaded = m.loaded;
    const isActive = m.active;
    
    let stateHtml = '';
    if (isActive) stateHtml += `<span class="badge" style="background:#1a4d2e;color:#4ade80;border:1px solid #14532d;margin-right:4px">🌟 ACTIVE</span>`;
    if (isLoaded) stateHtml += `<span class="badge" style="background:var(--primary-dark);color:white;border:1px solid var(--primary)">🔋 LOADED</span>`;
    if (!isActive && !isLoaded) stateHtml += `<span style="color:var(--muted);font-size:12px">Zzz</span>`;

    return `<tr>
      <td><span class="mono" style="${isActive ? 'font-weight:bold;color:white;' : ''}">${m.name}</span></td>
      <td>${stateHtml}</td>
      <td>${size}</td>
      <td>${family}</td>
      <td style="color:var(--muted);font-size:12px">${modified}</td>
      <td>
        <div style="display:flex;gap:6px">
          ${!isActive ? `<button class="btn btn-secondary btn-sm" onclick="setActiveModel('${m.name}')">Set Active</button>` : ''}
          ${!isLoaded ? `<button class="btn btn-secondary btn-sm" onclick="loadModel('${m.name}')">Load</button>` : `<button class="btn btn-secondary btn-sm" onclick="unloadModel('${m.name}')">Unload</button>`}
          <button class="btn btn-danger btn-sm" onclick="deleteModel('${m.name}')" title="Delete Model">🗑️</button>
        </div>
      </td>
    </tr>`;
  }).join('');
}

// ── Model Actions ─────────────────────────────────────────────────────────────

async function setActiveModel(name) {
  toast(`Setting ${name} as active...`, 'info');
  try {
    const data = await apiFetch(`/api/servers/${currentServerId}/models/select`, {method:'POST', body:{model: name}});
    if (data.active_model) {
      toast(`${name} is now the active model`, 'success');
      loadServerDetail(currentServerId); // refresh state
    }
  } catch(e) { toast(e.message, 'error'); }
}

async function loadModel(name) {
  toast(`Loading ${name} into VRAM...`, 'info');
  try {
    const data = await apiFetch(`/api/servers/${currentServerId}/models/load`, {method:'POST', body:{model: name}});
    toast(data.message || 'Model loaded', 'success');
    setTimeout(() => loadServerDetail(currentServerId), 1500);
  } catch(e) { toast(e.message, 'error'); }
}

async function unloadModel(name) {
  toast(`Unloading ${name}...`, 'info');
  try {
    const data = await apiFetch(`/api/servers/${currentServerId}/models/unload`, {method:'POST', body:{model: name}});
    toast(data.message || 'Model unloaded', 'success');
    setTimeout(() => loadServerDetail(currentServerId), 2500); // UI needs extra second to see Ollama un-cache
  } catch(e) { toast(e.message, 'error'); }
}

async function pullModel() {
  const name = $('pull-model-input').value.trim();
  if(!name) { toast('Enter a model name', 'error'); return; }
  toast(`Pulling ${name}… this may take minutes`, 'info');
  try {
    await apiFetch(`/api/servers/${currentServerId}/models/pull`, {method:'POST', body:{name}});
    toast(`Pull started for ${name}. Refresh in a minute.`, 'success');
    $('pull-model-input').value = '';
  } catch(e) { toast(e.message, 'error'); }
}

async function deleteModel(name) {
  if(!confirm(`Delete model "${name}"?`)) return;
  try {
    await apiFetch(`/api/servers/${currentServerId}/models`, {
      method:'DELETE', body:{name}
    });
    toast(`Deleted ${name}`, 'success');
    loadServerDetail(currentServerId);
  } catch(e) { toast(e.message,'error'); }
}

// ── Keys ─────────────────────────────────────────────────────────────────────

async function loadKeys(id) {
  $('keys-table-body').innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:24px">Loading…</td></tr>';
  try {
    const keys = await apiFetch(`/api/servers/${id}/keys`);
    const arr = Array.isArray(keys) ? keys : [];
    if(!arr.length) {
      $('keys-table-body').innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:32px">No API keys yet. Create one above ↑</td></tr>';
      return;
    }
    $('keys-table-body').innerHTML = arr.map(k => `<tr>
      <td><span class="mono">${truncateKey(k.key)}</span>
        <button class="btn-icon btn-sm" style="margin-left:6px" title="Copy" onclick="navigator.clipboard.writeText('${k.key}');toast('Copied!','success')">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
          </svg>
        </button>
      </td>
      <td>${k.label||'—'}</td>
      <td>${fmt(k.requests)}</td>
      <td>${fmt(k.tokens_in)}</td>
      <td>${fmt(k.tokens_out)}</td>
      <td><span class="badge ${k.enabled?'badge-online':'badge-offline'}">${k.enabled?'Active':'Disabled'}</span></td>
      <td style="font-size:12px;color:var(--muted)">${k.last_used?fmtDate(k.last_used):'Never'}</td>
      <td style="display:flex;gap:6px">
        <button class="btn btn-secondary btn-sm" onclick="testApiKey('${k.key}', '${k.label}')">Test</button>
        ${k.enabled?`<button class="btn btn-secondary btn-sm" onclick="revokeKey('${k.key}')">Revoke</button>`:''}
        <button class="btn btn-danger btn-sm" onclick="deleteKey('${k.key}')">Delete</button>
      </td>
    </tr>`).join('');
  } catch(e) { toast(e.message,'error'); }
}

function openCreateKey() { $('f-key-label').value=''; $('f-key-rpm').value='0'; $('modal-key').classList.remove('hidden'); }

async function createKey() {
  const label = $('f-key-label').value.trim();
  const limit_rpm = parseInt($('f-key-rpm').value)||0;
  try {
    const data = await apiFetch(`/api/servers/${currentServerId}/keys`, {method:'POST',body:{label,limit_rpm}});
    closeModal('modal-key');
    newKeyValue = data.key;
    $('new-key-display').textContent = data.key;
    $('modal-show-key').classList.remove('hidden');
    toast('API key created!','success');
  } catch(e) { toast(e.message,'error'); }
}

async function revokeKey(key) {
  if(!confirm('Revoke this key? It will stop working immediately.')) return;
  try {
    await apiFetch(`/api/servers/${currentServerId}/keys/revoke`, {method:'POST',body:{key}});
    toast('Key revoked','success');
    loadKeys(currentServerId);
  } catch(e) { toast(e.message,'error'); }
}

async function deleteKey(key) {
  if(!confirm('Permanently delete this key?')) return;
  try {
    await apiFetch(`/api/servers/${currentServerId}/keys`, {method:'DELETE',body:{key}});
    toast('Key deleted','success');
    loadKeys(currentServerId);
  } catch(e) { toast(e.message,'error'); }
}

function copyKey() { navigator.clipboard.writeText(newKeyValue); toast('Key copied!','success'); }

// ── Control Plane: Model Testing ──────────────────────────────────────────────

async function runModelHealth() {
  $('btn-model-health').disabled = true;
  $('model-health-results').innerHTML = '<div class="spin" style="margin:12px auto"></div>';
  try {
    const data = await apiFetch(`/api/servers/${currentServerId}/models/health`);
    let html = `<div style="font-size:12px;color:var(--muted);margin-bottom:10px">${data.healthy}/${data.total_models} models healthy</div>`;
    (data.models||[]).forEach(m => {
      const ok = m.status === 'pass';
      html += `<div class="test-result ${ok?'test-pass':'test-fail'}">
        <span>${ok?'✓':'✗'}</span>
        <span class="mono">${m.model}</span>
        <span style="margin-left:auto;font-size:11px;color:var(--muted)">${m.latency_ms}ms</span>
        ${m.error?`<span style="font-size:11px;color:var(--danger)">${m.error}</span>`:''}
      </div>`;
    });
    $('model-health-results').innerHTML = html;
  } catch(e) { $('model-health-results').innerHTML = `<span style="color:var(--danger)">${e.message}</span>`; }
  $('btn-model-health').disabled = false;
}

async function runApiTest() {
  const model = $('api-test-model').value.trim();
  if(!model) { toast('Enter a model name','error'); return; }
  $('btn-api-test').disabled = true;
  $('api-test-results').innerHTML = '<div class="spin" style="margin:12px auto"></div>';
  try {
    const data = await apiFetch(`/api/servers/${currentServerId}/test-api`, {
      method:'POST', body:{model}
    });
    const statusColor = data.status==='PASS'?'var(--success)':'var(--danger)';
    let html = `<div style="font-size:14px;font-weight:700;color:${statusColor};margin-bottom:10px">
      ${data.status} — ${data.passed_count}/${data.passed_count+data.failed_count} tests passed (${data.total_latency_ms}ms total)
    </div>`;
    (data.tests||[]).forEach(t => {
      const ok = t.passed;
      html += `<div class="test-result ${ok?'test-pass':'test-fail'}" style="margin-bottom:4px">
        <span>${ok?'✓':'✗'}</span>
        <span style="font-weight:600">${t.name.replace(/_/g,' ')}</span>
        <span style="margin-left:auto;font-size:11px;color:var(--muted)">${t.latency_ms}ms</span>
        ${t.error?`<br><span style="font-size:11px;color:var(--danger);margin-left:20px">${t.error}</span>`:''}
      </div>`;
    });
    $('api-test-results').innerHTML = html;
  } catch(e) { $('api-test-results').innerHTML = `<span style="color:var(--danger)">${e.message}</span>`; }
  $('btn-api-test').disabled = false;
}

async function runSingleModelTest() {
  const name = $('test-model-name').value.trim();
  if(!name) { toast('Enter a model name','error'); return; }
  const prompt = $('test-model-prompt').value.trim();
  $('single-model-result').innerHTML = '<div class="spin" style="margin:8px 0"></div>';
  try {
    const data = await apiFetch(`/api/servers/${currentServerId}/test-model`, {
      method:'POST', body:{name, prompt}
    });
    const ok = data.status==='pass';
    $('single-model-result').innerHTML = `
      <div class="test-result ${ok?'test-pass':'test-fail'}">
        <span>${ok?'✓':'✗'}</span>
        <span class="mono">${data.model}</span>
        <span style="margin-left:12px;font-size:12px">${data.latency_ms}ms</span>
        ${data.tokens?`<span style="font-size:11px;color:var(--muted);margin-left:8px">${data.tokens.total} tokens</span>`:''}
      </div>
      ${data.response_text?`<div style="margin-top:8px;padding:10px;background:var(--bg3);border-radius:6px;font-size:12px;color:var(--muted)"><strong>Response:</strong> ${data.response_text.substring(0,200)}</div>`:''}
      ${data.error?`<div style="margin-top:8px;color:var(--danger);font-size:12px">${data.error}</div>`:''}
    `;
  } catch(e) { $('single-model-result').innerHTML = `<span style="color:var(--danger)">${e.message}</span>`; }
}

// ── API Key Testing ────────────────────────────────────────────────────────────

async function testApiKey(key, label) {
  toast(`Testing key: ${label || key.substring(0,8)}...`, 'info');
  try {
    const data = await apiFetch(`/api/servers/${currentServerId}/keys/test`, {method:'POST', body:{api_key: key}});
    if(data.status === 'PASS') {
      toast(`✅ PASS (${data.latency_ms}ms, using ${data.model_used})`, 'success');
      alert(`API Key Test Successful!\n\nModel routed: ${data.model_used}\nLatency: ${data.latency_ms}ms\nResponse: "${data.response}"`);
    } else {
      toast(`❌ FAIL: ${data.error || 'Unknown error'}`, 'error');
    }
  } catch(e) { toast(e.message, 'error'); }
}

// ── Control Plane: Lifecycle ──────────────────────────────────────────────────

async function lifecycleAction(action) {
  const labels = {
    'activate': 'Activating Server',
    'deactivate': 'Deactivating Server',
    'restart-ai': 'Restarting AI',
    'idle': 'Entering Idle Mode',
    'restart-ollama': 'Restarting Ollama',
  };
  const label = labels[action] || action;
  if(!confirm(`${label}?`)) return;
  toast(`${label}...`, 'info');
  try {
    const data = await apiFetch(`/api/servers/${currentServerId}/lifecycle/${action}`, {method:'POST'});
    if(data.success !== false) {
      toast(`${label}: Success (${data.method || 'agent'})`, 'success');
    } else {
      toast(`${label} Failed: ${data.details?.error || data.message || 'Unknown error'}`, 'error');
    }
    setTimeout(loadLifecycleStatus, 2000);
    setTimeout(() => loadServerDetail(currentServerId), 3000);
  } catch(e) { toast(e.message, 'error'); }
}

async function confirmSystemAction(action, msg) {
   if(confirm(msg)) {
      lifecycleAction(action);
   }
}

async function loadLifecycleStatus() {
  try {
    const data = await apiFetch(`/api/servers/${currentServerId}/lifecycle/health`, {method:'POST'});
    const stateLabel = data.ollama_running ? 'ACTIVE' : (data.online ? 'IDLE' : 'OFFLINE');
    const stateColor = data.ollama_running ? 'var(--success)' : (data.online ? 'var(--warning)' : 'var(--danger)');
    const html = `
      <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin-top:4px">
        <div><span class="badge ${data.online?'badge-online':'badge-offline'}">
          ${data.online?'● Agent Online':'● Agent Offline'}</span></div>
        <div style="font-size:13px;font-weight:600;color:${stateColor}">State: ${stateLabel}</div>
        <div style="font-size:12px;color:var(--muted)">Connection: <strong>${data.method}</strong></div>
        ${data.ollama_running!=null?`<div style="font-size:12px">
          Ollama: <span style="color:${data.ollama_running?'var(--success)':'var(--danger)'}">
          ${data.ollama_running?'Running':'Down'}</span></div>`:''}
      </div>`;
    $('lifecycle-status').innerHTML = html;

    // Disable controls if offline
    const isOffline = !data.online;
    ['btn-activate','btn-deactivate','btn-restart-ai','btn-idle','btn-restart-ollama'].forEach(btnId => {
      const btn = $(btnId);
      if(btn) { btn.style.opacity = isOffline ? '0.5' : '1'; btn.style.pointerEvents = isOffline ? 'none' : 'auto'; }
    });
  } catch(e) { $('lifecycle-status').innerHTML = `<span style="color:var(--muted);font-size:13px">Could not fetch status</span>`; }
}

// ── Control Plane: Uptime ────────────────────────────────────────────────────

async function loadUptime() {
  try {
    const uptime = await apiFetch(`/api/servers/${currentServerId}/uptime`);
    $('uptime-current-val').textContent = uptime.current_session_formatted || '—';
    $('uptime-boot').textContent = uptime.last_boot ? fmtDate(uptime.last_boot) : '—';
    $('uptime-sessions').textContent = uptime.total_sessions ?? '—';
  } catch(e) {
    $('uptime-current-val').textContent = '—';
  }

  try {
    const monthly = await apiFetch(`/api/servers/${currentServerId}/monthly-runtime`);
    $('uptime-monthly-val').textContent = monthly.current_month_formatted || '—';

    const months = monthly.all_months || {};
    const keys = Object.keys(months).sort().reverse().slice(0, 12);
    if(keys.length) {
      let html = '<div style="display:flex;flex-direction:column;gap:8px">';
      const maxSec = Math.max(...keys.map(k => months[k].total_seconds || 0), 1);
      keys.forEach(k => {
        const sec = months[k].total_seconds || 0;
        const pct = (sec / maxSec) * 100;
        const hrs = (sec / 3600).toFixed(1);
        html += `<div style="display:flex;align-items:center;gap:12px">
          <span style="width:70px;font-size:12px;color:var(--muted)">${k}</span>
          <div style="flex:1"><div class="uptime-bar"><div class="uptime-fill" style="width:${pct}%"></div></div></div>
          <span style="width:60px;text-align:right;font-size:12px;font-weight:600">${hrs}h</span>
          <span style="width:50px;text-align:right;font-size:11px;color:var(--muted)">${months[k].session_count||0} runs</span>
        </div>`;
      });
      html += '</div>';
      $('monthly-runtime-list').innerHTML = html;
    } else {
      $('monthly-runtime-list').innerHTML = '<div style="color:var(--muted);font-size:13px">No monthly data yet.</div>';
    }
  } catch(e) {
    $('uptime-monthly-val').textContent = '—';
  }
}

// ── Control Plane: Logs ──────────────────────────────────────────────────────

async function loadLogs(type) {
  $('log-entries').innerHTML = '<div style="text-align:center;padding:24px"><div class="spin" style="margin:0 auto"></div></div>';
  try {
    const data = await apiFetch(`/api/servers/${currentServerId}/logs/${type}?limit=150`);
    const entries = data.entries || [];
    if(!entries.length) {
      $('log-entries').innerHTML = '<div style="text-align:center;color:var(--muted);padding:32px">No log entries found.</div>';
      return;
    }
    $('log-entries').innerHTML = entries.map(e => {
      const lvl = (e.level||'').toLowerCase();
      const cls = lvl==='error'||lvl==='critical' ? 'error' : lvl==='warning' ? 'warning' : '';
      const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString() : '';
      return `<div class="log-entry ${cls}">
        <span style="color:var(--muted);margin-right:8px">${ts}</span>
        <span style="font-weight:600;margin-right:8px;min-width:55px;display:inline-block">${(e.level||'').toUpperCase()}</span>
        ${e.request_id?`<span style="color:var(--accent);margin-right:8px">[${e.request_id}]</span>`:''}
        ${e.message||''}
      </div>`;
    }).join('');
  } catch(e) { $('log-entries').innerHTML = `<div style="color:var(--danger);padding:16px">${e.message}</div>`; }
}

// ── Servers management ────────────────────────────────────────────────────────

async function loadServersTable() {
  const servers = await apiFetch('/api/servers').catch(()=>[]);
  if(!servers.length) {
    $('servers-table-body').innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:32px">No servers registered yet.</td></tr>';
    return;
  }
  $('servers-table-body').innerHTML = servers.map(s=>`<tr>
    <td><strong>${s.name}</strong></td>
    <td><span class="mono">${s.host}</span></td>
    <td id="srv-status-${s.id}"><span class="badge badge-warn">Checking…</span></td>
    <td style="font-size:12px;color:var(--muted)">${s.last_seen?fmtDate(s.last_seen):'Never'}</td>
    <td style="font-size:12px;color:var(--muted)">${s.notes||'—'}</td>
    <td style="display:flex;gap:6px">
      <button class="btn btn-secondary btn-sm" onclick="openServerDetail(${s.id})">Open</button>
      <button class="btn btn-secondary btn-sm" onclick="openEditServer(${s.id})">Edit</button>
      <button class="btn btn-danger btn-sm" onclick="removeServer(${s.id})">Remove</button>
    </td>
  </tr>`).join('');

  // Check status async
  servers.forEach(s=>{
    apiFetch(`/api/servers/${s.id}/status`).then(st=>{
      const el = $(`srv-status-${s.id}`);
      if(el) {
        if(st.state === 'idle') el.innerHTML = '<span class="badge badge-warn">💤 Idle</span>';
        else if(st.online) el.innerHTML = '<span class="badge badge-online">● Online</span>';
        else el.innerHTML = '<span class="badge badge-offline">● Offline</span>';
      }
    }).catch(()=>{
      const el = $(`srv-status-${s.id}`);
      if(el) el.innerHTML = `<span class="badge badge-offline">● Offline</span>`;
    });
  });
}

function openAddServer() {
  editingServerId = null;
  setAddMode('api');
  $('modal-server-title').textContent = 'Add Server';
  // Reset API fields
  ['f-srv-name','f-srv-host','f-srv-token','f-srv-notes'].forEach(id=>$(id).value='');
  // Reset SSH fields
  ['f-ssh-name','f-ssh-host','f-ssh-user','f-ssh-pass'].forEach(id=>$(id).value='');
  $('f-ssh-port').value = '22';
  $('modal-server').classList.remove('hidden');
}

async function openEditServer(id) {
  const servers = await apiFetch('/api/servers').catch(()=>[]);
  const s = servers.find(sv=>sv.id===id);
  if(!s) return;
  editingServerId = id;
  setAddMode('api');  // Editing always uses API mode
  $('modal-server-title').textContent = 'Edit Server';
  $('f-srv-name').value = s.name;
  $('f-srv-host').value = s.host;
  $('f-srv-token').value = '';  // token is hidden; user must re-enter to change
  $('f-srv-notes').value = s.notes||'';
  $('modal-server').classList.remove('hidden');
}

async function saveServer() {
  const notes = $('f-srv-notes').value.trim();

  if (addMode === 'ssh' && !editingServerId) {
    startDeployment();
    return;
  }

  const name = $('f-srv-name').value.trim();
  const host = $('f-srv-host').value.trim();
  const token = $('f-srv-token').value.trim();

  if(!name||!host||!token) { toast('Name, Host, and Admin Token are required','error'); return; }
  try {
    if(editingServerId) {
      await apiFetch(`/api/servers/${editingServerId}`, {method:'PUT',body:{name,host,admin_token:token,notes}});
      toast('Server updated','success');
    } else {
      await apiFetch('/api/servers', {method:'POST',body:{name,host,admin_token:token,notes}});
      toast('Server added','success');
    }
    closeModal('modal-server');
    loadOverview();
  } catch(e) { toast(e.message,'error'); }
}

async function removeServer(id) {
  if(!confirm('Remove this server from the dashboard? (The remote agent keeps running.)')) return;
  try {
    await apiFetch(`/api/servers/${id}`, {method:'DELETE'});
    toast('Server removed','success');
    if(currentServerId===id) showView('overview');
    else loadOverview();
  } catch(e) { toast(e.message,'error'); }
}

// ── Modals ────────────────────────────────────────────────────────────────────
function closeModal(id) { $(id).classList.add('hidden'); }

document.addEventListener('keydown', e=>{
  if(e.key==='Escape') document.querySelectorAll('.modal-overlay').forEach(m=>m.classList.add('hidden'));
});

// ── Refresh ────────────────────────────────────────────────────────────────
async function refreshAll() {
  if($('view-overview').classList.contains('hidden')===false) loadOverview();
  else if(currentServerId) loadServerDetail(currentServerId);
}

// ── Init ───────────────────────────────────────────────────────────────────
loadOverview();
// Auto-refresh every 30s
setInterval(()=>{
  if(!$('view-overview').classList.contains('hidden')) loadOverview();
  else if(currentServerId && !$('view-server-detail').classList.contains('hidden')) loadServerDetail(currentServerId);
}, 30000);

/* cronpypeline dashboard — polling frontend */

const POLL_MS = 4000;

const els = {
  configSelect: document.getElementById('config-select'),
  pipelineName: document.getElementById('pipeline-name'),
  modeBadge: document.getElementById('mode-badge'),
  connDot: document.getElementById('conn-dot'),
  connLabel: document.getElementById('conn-label'),
  toggleWrap: document.getElementById('toggle-wrap'),
  toggleBtn: document.getElementById('toggle-btn'),
  toggleLabel: document.getElementById('toggle-label'),
  errorBanner: document.getElementById('error-banner'),
  disabledBanner: document.getElementById('disabled-banner'),
  summary: document.getElementById('summary'),
  lanes: document.getElementById('lanes'),
  emptyState: document.getElementById('empty-state'),
  emptyDetail: document.getElementById('empty-detail'),
  panel: document.getElementById('detail-panel'),
  panelOverlay: document.getElementById('panel-overlay'),
  panelClose: document.getElementById('panel-close'),
  panelStageId: document.getElementById('panel-stage-id'),
  panelTitle: document.getElementById('panel-title'),
  panelTarget: document.getElementById('panel-target'),
  panelStateBadge: document.getElementById('panel-state-badge'),
  panelBody: document.getElementById('panel-body'),
  viewOptions: document.getElementById('view-options'),
  hidePending: document.getElementById('hide-pending'),
};

let currentConfig = null;
let pipelineMeta = null;      // /api/pipeline response
let lastStatus = null;        // previous /api/status response (for diffing)
let pollTimer = null;
let justCompleted = new Set(); // "target::stage" keys that flipped to complete
let hidePending = localStorage.getItem('cronpypeline.hidePending') === '1';
let lanesAnimated = false;    // entrance animation only on first render per config

// ── Utilities ────────────────────────────────────────────────────────────────

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function setConn(state, label) {
  els.connDot.className = 'conn-dot ' + state;
  els.connLabel.textContent = label;
}

function stageStateClass(st) {
  if (!st || st.stateless) return 'stateless';
  if (st.given_up) return 'given-up';
  if (st.stale) return 'stale';
  if (st.processing) return 'processing';
  if (st.complete) return 'complete';
  if (st.rejected) return 'rejected';
  return 'pending';
}

const STATE_LABELS = {
  'complete':   { text: 'Complete',   cls: 'text-emerald-300 border-emerald-500/50 bg-emerald-500/10' },
  'processing': { text: 'Processing', cls: 'text-cyan-300 border-cyan-500/50 bg-cyan-500/10' },
  'stale':      { text: 'Stale',      cls: 'text-amber-300 border-amber-500/50 bg-amber-500/10' },
  'given-up':   { text: 'Gave up',    cls: 'text-rose-300 border-rose-500/50 bg-rose-500/10' },
  'rejected':   { text: 'Rejected',   cls: 'text-orange-300 border-orange-500/50 bg-orange-500/10' },
  'pending':    { text: 'Pending',    cls: 'text-slate-400 border-slate-600 bg-slate-500/10' },
  'stateless':  { text: 'Stateless',  cls: 'text-slate-500 border-slate-700 bg-transparent' },
};

function nodeIcon(cls) {
  switch (cls) {
    case 'complete': return '✓';
    case 'processing': return '<span class="spinner"></span>';
    case 'stale': return '!';
    case 'given-up': return '✕';
    case 'rejected': return '↺';
    case 'stateless': return '·';
    default: return '○';
  }
}

// ── View options ──────────────────────────────────────────────────────────────────────

els.hidePending.checked = hidePending;
els.hidePending.addEventListener('change', () => {
  hidePending = els.hidePending.checked;
  localStorage.setItem('cronpypeline.hidePending', hidePending ? '1' : '0');
  if (lastStatus) renderLanes(lastStatus);
});

// Hide dangling connectors on the last stop of each wrapped row, and mark
// the first stop of continuation rows with a ↳ cue
function markRowEnds() {
  els.lanes.querySelectorAll('.subway').forEach(subway => {
    const stops = [...subway.querySelectorAll('.subway-stop')];
    stops.forEach((el, i) => {
      const next = stops[i + 1];
      const prev = stops[i - 1];
      el.classList.toggle('row-end', !!next && next.offsetTop !== el.offsetTop);
      el.classList.toggle('row-start', !!prev && prev.offsetTop !== el.offsetTop);
    });
  });
}

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(markRowEnds, 150);
});

// ── Config dropdown ──────────────────────────────────────────────────────────

async function loadConfigs() {
  const data = await api('/api/configs');
  els.configSelect.innerHTML = '';
  if (data.configs.length === 0) {
    const opt = document.createElement('option');
    opt.textContent = 'no configs found';
    els.configSelect.appendChild(opt);
    els.emptyState.classList.remove('hidden');
    els.emptyDetail.textContent = 'No *.json files in ' + data.configs_dir;
    return;
  }
  for (const name of data.configs) {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    els.configSelect.appendChild(opt);
  }
  const saved = localStorage.getItem('cronpypeline.config');
  currentConfig = data.configs.includes(saved) ? saved : data.configs[0];
  els.configSelect.value = currentConfig;
  await switchConfig(currentConfig);
}

els.configSelect.addEventListener('change', () => switchConfig(els.configSelect.value));

async function switchConfig(name) {
  currentConfig = name;
  localStorage.setItem('cronpypeline.config', name);
  lastStatus = null;
  lanesAnimated = false;
  justCompleted.clear();
  closePanel();
  els.lanes.innerHTML = '';
  els.summary.innerHTML = '';
  els.errorBanner.classList.add('hidden');
  if (pollTimer) clearInterval(pollTimer);

  try {
    pipelineMeta = await api('/api/pipeline?config=' + encodeURIComponent(name));
  } catch (e) {
    showError('Failed to load pipeline: ' + e.message);
    return;
  }

  els.pipelineName.textContent = pipelineMeta.name + '  ·  ' + pipelineMeta.workspace_dir;
  renderModeBadge(pipelineMeta.mode);
  renderToggle(pipelineMeta.has_toggle, pipelineMeta.enabled);

  await poll();
  pollTimer = setInterval(poll, POLL_MS);
}

// ── Mode + toggle ────────────────────────────────────────────────────────────

function renderModeBadge(mode) {
  if (mode) {
    els.modeBadge.textContent = 'mode: ' + mode;
    els.modeBadge.classList.remove('hidden');
  } else {
    els.modeBadge.classList.add('hidden');
  }
}

function renderToggle(hasToggle, enabled) {
  if (!hasToggle) {
    els.toggleWrap.classList.add('hidden');
    return;
  }
  els.toggleWrap.classList.remove('hidden');
  els.toggleBtn.classList.toggle('on', !!enabled);
  els.toggleBtn.setAttribute('aria-checked', String(!!enabled));
  els.toggleLabel.textContent = enabled ? 'enabled' : 'disabled';
  els.toggleLabel.className = 'text-xs font-semibold uppercase tracking-widest ' +
    (enabled ? 'text-emerald-400' : 'text-rose-400');
  els.disabledBanner.classList.toggle('hidden', !!enabled);
}

els.toggleBtn.addEventListener('click', async () => {
  const target = !els.toggleBtn.classList.contains('on');
  els.toggleBtn.classList.add('busy');
  try {
    const res = await fetch('/api/toggle?config=' + encodeURIComponent(currentConfig), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: target }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    renderToggle(true, target);
  } catch (e) {
    showError('Toggle failed: ' + e.message);
  } finally {
    els.toggleBtn.classList.remove('busy');
  }
});

// ── Polling + rendering ──────────────────────────────────────────────────────

async function poll() {
  let status;
  try {
    status = await api('/api/status?config=' + encodeURIComponent(currentConfig));
    setConn('live', 'live · ' + new Date().toLocaleTimeString());
  } catch (e) {
    setConn('dead', 'connection lost');
    return;
  }

  renderModeBadge(status.mode);
  if (pipelineMeta && pipelineMeta.has_toggle) renderToggle(true, status.enabled);

  if (status.error) {
    showError(status.error);
    els.lanes.innerHTML = '';
    els.summary.innerHTML = '';
    els.emptyState.classList.remove('hidden');
    els.emptyDetail.textContent = status.error;
    return;
  }
  els.errorBanner.classList.add('hidden');
  els.emptyState.classList.add('hidden');

  detectCompletions(status);
  renderSummary(status.summary);
  renderLanes(status);
  lastStatus = status;
}

function detectCompletions(status) {
  justCompleted.clear();
  if (!lastStatus) return;
  for (const [target, ts] of Object.entries(status.targets)) {
    const prev = lastStatus.targets[target];
    if (!prev) continue;
    for (const [sid, st] of Object.entries(ts.stages)) {
      const p = prev.stages[sid];
      if (st.complete && p && !p.complete) justCompleted.add(target + '::' + sid);
    }
  }
}

function showError(msg) {
  els.errorBanner.textContent = msg;
  els.errorBanner.classList.remove('hidden');
}

// Summary cards
const SUMMARY_DEFS = [
  { key: 'targets', label: 'Targets', color: 'text-slate-200' },
  { key: 'tracked_stages', label: 'Tracked stages', color: 'text-slate-200' },
  { key: 'complete', label: 'Complete', color: 'text-emerald-400' },
  { key: 'processing', label: 'Processing', color: 'text-cyan-400' },
  { key: 'stale', label: 'Stale', color: 'text-amber-400' },
  { key: 'given_up', label: 'Gave up', color: 'text-rose-400' },
];

function renderSummary(summary) {
  const prev = lastStatus ? lastStatus.summary : {};
  els.summary.innerHTML = SUMMARY_DEFS.map(d => {
    const v = summary[d.key] ?? 0;
    const changed = prev[d.key] !== undefined && prev[d.key] !== v;
    const color = v === 0 ? 'text-slate-600' : d.color;
    return `<div class="summary-card ${changed ? 'pop' : ''}">
      <div class="value ${color}">${v}</div>
      <div class="label">${d.label}</div>
    </div>`;
  }).join('');
}

// SWE plugin state badges (PR / GitHub session / issue counts)
const PR_STATE_STYLES = {
  open:               { text: 'PR OPEN',              cls: 'bg-cyan-500/15 text-cyan-300 border-cyan-500/40' },
  approved:           { text: 'PR APPROVED',          cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40' },
  changes_requested:  { text: 'PR CHANGES REQUESTED', cls: 'bg-amber-500/15 text-amber-300 border-amber-500/40' },
  merged:             { text: 'PR MERGED',            cls: 'bg-violet-500/15 text-violet-300 border-violet-500/40' },
  rejected:           { text: 'PR REJECTED',          cls: 'bg-rose-500/15 text-rose-300 border-rose-500/40' },
};

function sweBadges(swe) {
  if (!swe) return '';
  const badges = [];

  if (swe.pr && swe.pr.pr_number) {
    const style = PR_STATE_STYLES[swe.pr.pr_state] || PR_STATE_STYLES.open;
    const cycles = swe.pr.pr_review_cycles > 0
      ? ` · ${swe.pr.pr_review_cycles} review cycle${swe.pr.pr_review_cycles > 1 ? 's' : ''}` : '';
    const inner = `#${esc(swe.pr.pr_number)} · ${esc(style.text)}${cycles}`;
    const pill = `<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold border ${style.cls}">${inner}</span>`;
    badges.push(swe.pr.pr_url
      ? `<a href="${esc(swe.pr.pr_url)}" target="_blank" rel="noopener" class="hover:opacity-80" title="open PR on GitHub">${pill}</a>`
      : pill);
  }

  if (swe.session && swe.session.active) {
    const gh = swe.session.github_number ? ` #${esc(swe.session.github_number)}` : '';
    badges.push(`<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold border bg-indigo-500/15 text-indigo-300 border-indigo-500/40" title="GitHub session issue: ${esc(swe.session.issue_id)}">GH SESSION${gh}</span>`);
  }

  if (swe.issues) {
    const parts = Object.entries(swe.issues)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([status, n]) => `${n} ${esc(status)}`);
    badges.push(`<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold border bg-slate-500/10 text-slate-400 border-slate-600" title="issues in .SWE/issues/">issues: ${parts.join(' · ')}</span>`);
  }

  return badges.join('');
}

// Pipeline lanes (subway map per target)
function stageName(sid) {
  const meta = (pipelineMeta?.stages || []).find(s => s.id === sid);
  return meta ? meta.name : '';
}

function renderLanes(status) {
  const targets = Object.entries(status.targets);
  if (targets.length === 0) {
    els.lanes.innerHTML = '';
    els.viewOptions.classList.add('hidden');
    els.emptyState.classList.remove('hidden');
    els.emptyDetail.textContent = 'No targets configured';
    return;
  }
  els.viewOptions.classList.remove('hidden');

  els.lanes.innerHTML = targets.map(([target, ts], i) => {
    const stageIds = Object.keys(ts.stages);
    const tracked = stageIds.filter(sid => !ts.stages[sid].stateless);
    const done = tracked.filter(sid => ts.stages[sid].complete).length;
    const pct = tracked.length ? Math.round(100 * done / tracked.length) : 0;

    // Filter: optionally hide idle pending/stateless stages (keep next-actionable)
    const visible = stageIds.filter(sid => {
      if (!hidePending) return true;
      const cls = stageStateClass(ts.stages[sid]);
      if (cls !== 'pending' && cls !== 'stateless') return true;
      return ts.next_actionable === sid;
    });
    const hiddenCount = stageIds.length - visible.length;

    const stops = visible.map((sid, idx) => {
      const st = ts.stages[sid];
      const cls = stageStateClass(st);
      const isNext = ts.next_actionable === sid && (cls === 'pending' || cls === 'stateless');
      const glow = justCompleted.has(target + '::' + sid) ? ' just-completed' : '';

      // Connector reflects BOTH endpoints: green only if this and the next
      // visible stage are complete; half-green fading out if only this one is;
      // flowing if either endpoint is processing/stale.
      const nextSt = idx + 1 < visible.length ? ts.stages[visible[idx + 1]] : null;
      let connCls = '';
      const thisActive = st && (st.processing || st.stale);
      const nextActive = nextSt && (nextSt.processing || nextSt.stale);
      if (thisActive || nextActive) connCls = 'flowing';
      else if (st && st.complete && nextSt && nextSt.complete) connCls = 'done';
      else if (st && st.complete) connCls = 'done-half';

      let badges = '';
      if (st && st.retry_count > 0) badges += `<span class="node-badge retry" title="retries">${st.retry_count}</span>`;
      if (st && st.rejection_count > 0) badges += `<span class="node-badge reject" title="rejections">${st.rejection_count}</span>`;

      const name = stageName(sid);
      const sl = STATE_LABELS[cls];
      return `<div class="subway-stop" data-target="${esc(target)}" data-stage="${esc(sid)}" title="${esc(name)} — ${esc(sl.text)}">
        <div class="connector ${connCls}"></div>
        <div class="node ${cls}${isNext ? ' next' : ''}${glow}">${nodeIcon(cls)}${badges}</div>
        <div class="node-label">${esc(sid)}</div>
        <div class="node-name">${esc(name)}</div>
      </div>`;
    }).join('');

    const dirWarn = ts.target_dir_exists ? '' :
      '<span class="text-xs text-amber-400 font-[JetBrains_Mono]">⚠ target dir missing</span>';
    const procBadge = ts.has_processing ?
      '<span class="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">ACTIVE</span>' : '';
    const hiddenBadge = hiddenCount > 0 ?
      `<span class="text-[10px] text-slate-600 font-[JetBrains_Mono]">${hiddenCount} pending hidden</span>` : '';

    const anim = lanesAnimated ? 'animation:none' : `animation-delay:${i * 60}ms`;
    return `<div class="lane" style="${anim}">
      <div class="flex items-center gap-3 flex-wrap">
        <h3 class="font-bold font-[JetBrains_Mono] text-slate-100"><span class="text-slate-500 font-normal text-xs mr-1">target:</span>${esc(target)}</h3>
        ${procBadge}
        ${sweBadges(ts.swe)}
        ${dirWarn}
        ${hiddenBadge}
        <div class="flex-1"></div>
        <span class="text-xs text-slate-500 font-[JetBrains_Mono]">${done}/${tracked.length} stages · ${pct}%</span>
      </div>
      <div class="lane-progress-track"><div class="lane-progress-fill${ts.has_processing ? ' active' : ''}" style="width:${pct}%"></div></div>
      <div class="subway">${stops}</div>
    </div>`;
  }).join('');

  // Attach click handlers for detail panel
  els.lanes.querySelectorAll('.subway-stop').forEach(el => {
    el.addEventListener('click', () => openPanel(el.dataset.target, el.dataset.stage));
  });

  lanesAnimated = true;
  markRowEnds();
}

// ── Detail panel ─────────────────────────────────────────────────────────────

function openPanel(target, stageId) {
  const meta = (pipelineMeta?.stages || []).find(s => s.id === stageId);
  const st = lastStatus?.targets?.[target]?.stages?.[stageId];
  const cls = stageStateClass(st);
  const sl = STATE_LABELS[cls];

  els.panelStageId.textContent = stageId;
  els.panelTitle.textContent = meta ? meta.name : stageId;
  els.panelTarget.textContent = 'target: ' + target;
  els.panelStateBadge.innerHTML = `<span class="state-pill ${sl.cls}">${nodeIcon(cls)} ${sl.text}</span>`;

  const rows = [];
  if (meta) {
    rows.push(kv('Trigger', meta.trigger_type));
    rows.push(kv('Action', meta.action_type + (meta.agent ? ' → ' + meta.agent : '')));
    if (meta.callable) rows.push(kv('Callable', meta.callable.split('.').slice(-1)[0]));
    rows.push(kv('Chain', meta.chain ? 'yes' : 'no'));
    rows.push(kv('Timeout', meta.timeout_minutes + ' min'));
    rows.push(kv('Max retries', meta.max_retries));
    if (meta.max_rejections > 0) rows.push(kv('Max rejections', meta.max_rejections));
    if (meta.modes.length) rows.push(kv('Modes', meta.modes.join(', ')));
    if (meta.marker_roles.length) rows.push(kv('Markers', meta.marker_roles.join(', ')));
  }
  if (st && !st.stateless) {
    rows.push(kv('Retry count', st.retry_count));
    if (st.rejection_count > 0) rows.push(kv('Rejection count', st.rejection_count));
  }

  let html = '<div>' + rows.join('') + '</div>';
  if (st && st.processing_data) {
    html += `<div>
      <div class="text-xs uppercase tracking-widest text-slate-500 mb-2 mt-4">Processing marker</div>
      <pre class="json-blob">${esc(JSON.stringify(st.processing_data, null, 2))}</pre>
    </div>`;
  }
  if (st && st.stateless) {
    html += '<p class="text-slate-500 text-xs mt-4">This stage defines no markers — its state is managed by custom plugin logic and is not tracked here.</p>';
  }

  // PR-related stages: show the plugin-owned PR state from .SWE/pr_published.json
  const swe = lastStatus?.targets?.[target]?.swe;
  if (swe && swe.pr && ['C-pr-status', 'C-publish', 'C-pr-review', 'C-pr-title'].includes(stageId)) {
    html += `<div>
      <div class="text-xs uppercase tracking-widest text-slate-500 mb-2 mt-4">PR state (.SWE/pr_published.json)</div>
      <pre class="json-blob">${esc(JSON.stringify(swe.pr, null, 2))}</pre>
    </div>`;
  }
  if (swe && swe.session && ['C-session-terminal', 'C-pr-status', 'B1-fetch-issues'].includes(stageId)) {
    html += `<div>
      <div class="text-xs uppercase tracking-widest text-slate-500 mb-2 mt-4">GitHub session (.SWE/github_session.json)</div>
      <pre class="json-blob">${esc(JSON.stringify(swe.session, null, 2))}</pre>
    </div>`;
  }
  els.panelBody.innerHTML = html;

  els.panelOverlay.classList.remove('hidden');
  requestAnimationFrame(() => {
    els.panelOverlay.classList.remove('opacity-0');
    els.panel.classList.remove('translate-x-full');
  });
}

function kv(k, v) {
  return `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`;
}

function closePanel() {
  els.panel.classList.add('translate-x-full');
  els.panelOverlay.classList.add('opacity-0');
  setTimeout(() => els.panelOverlay.classList.add('hidden'), 300);
}

els.panelClose.addEventListener('click', closePanel);
els.panelOverlay.addEventListener('click', closePanel);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closePanel(); });

// ── Boot ─────────────────────────────────────────────────────────────────────

loadConfigs().catch(e => {
  setConn('dead', 'failed to load');
  showError('Failed to load configs: ' + e.message);
});

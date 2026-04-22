/* APX GP War Room — dashboard.js */

// ─── Local timer state (updated from API, drives 1s tick) ────────────────────
const T = {
  raceStartedAt:  null,   // ms timestamp
  stintStartedAt: null,   // ms timestamp
  raceDurationMs: 780 * 60000,
  maxStintMs:     45  * 60000,
  raceStarted:    false,
};

// ─── Utilities ────────────────────────────────────────────────────────────────

function el(id) { return document.getElementById(id); }

function setText(id, val) {
  const e = el(id);
  if (e) e.textContent = (val !== null && val !== undefined) ? val : '—';
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fmtMMSS(totalSecs) {
  if (totalSecs == null || isNaN(totalSecs) || totalSecs < 0) return '00:00';
  const m = Math.floor(totalSecs / 60);
  const s = Math.floor(totalSecs % 60);
  return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

function fmtHMMSS(totalSecs) {
  if (totalSecs == null || isNaN(totalSecs) || totalSecs < 0) return '0:00:00';
  const h = Math.floor(totalSecs / 3600);
  const m = Math.floor((totalSecs % 3600) / 60);
  const s = Math.floor(totalSecs % 60);
  return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

function fmtLap(secs) {
  if (secs == null) return '—';
  const m = Math.floor(secs / 60);
  const rem = (secs % 60).toFixed(3);
  return m > 0 ? `${m}:${rem.padStart(6,'0')}` : rem;
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

// ─── Connection indicator ─────────────────────────────────────────────────────

function setConnected(ok, mode) {
  const dot = el('conn-dot');
  const lbl = el('conn-label');
  const mod = el('mode-label');
  if (dot) dot.className = 'conn-dot ' + (ok ? 'ok' : 'error');
  if (lbl) lbl.textContent = ok ? 'LIVE' : 'ERROR';
  if (mod && mode) mod.textContent = mode;
}

// ─── 1-second timer tick ──────────────────────────────────────────────────────

function tickTimers() {
  const now = Date.now();

  // Stint timer
  if (T.stintStartedAt) {
    const elapsedSec = Math.max(0, (now - T.stintStartedAt) / 1000);
    const remainSec  = Math.max(0, T.maxStintMs / 1000 - elapsedSec);
    const pct        = clamp(elapsedSec / (T.maxStintMs / 1000) * 100, 0, 100);

    setText('stint-elapsed', fmtMMSS(elapsedSec));
    setText('stint-remaining', fmtMMSS(remainSec));

    const bar = el('stint-bar');
    if (bar) {
      bar.style.width = pct + '%';
      // colour by urgency
      if (elapsedSec >= 44 * 60)      bar.className = 'progress-fill fill-critical';
      else if (elapsedSec >= 42 * 60) bar.className = 'progress-fill fill-prepare';
      else if (elapsedSec >= 40 * 60) bar.className = 'progress-fill fill-warn';
      else                             bar.className = 'progress-fill fill-ok';
    }
  }

  // Race clock
  if (T.raceStartedAt) {
    const elapsedSec  = Math.max(0, (now - T.raceStartedAt) / 1000);
    const remainSec   = Math.max(0, T.raceDurationMs / 1000 - elapsedSec);
    const pct         = clamp(elapsedSec / (T.raceDurationMs / 1000) * 100, 0, 100);

    setText('race-remaining', fmtHMMSS(remainSec));
    setText('race-elapsed-label', 'elapsed ' + fmtHMMSS(elapsedSec));

    const bar = el('race-bar');
    if (bar) bar.style.width = pct + '%';
  }
}

setInterval(tickTimers, 1000);

// ─── Timing table ─────────────────────────────────────────────────────────────

function renderTimingTable(teams) {
  const tbody = el('timing-tbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  (teams || []).forEach(t => {
    const tr = document.createElement('tr');
    if (t.is_our_team) tr.className = 'our-team';
    const pc = t.position === 1 ? 'p1' : t.position === 2 ? 'p2' : t.position === 3 ? 'p3' : '';
    tr.innerHTML =
      `<td><span class="pos-badge ${pc}">${t.position}</span></td>` +
      `<td class="col-kart">${escHtml(t.kart_number)}</td>` +
      `<td class="col-name">${escHtml(t.name)}${t.is_our_team ? ' <span class="our-star">★</span>' : ''}</td>` +
      `<td class="col-num">${fmtLap(t.last_lap_seconds)}</td>` +
      `<td class="col-num">${fmtLap(t.best_lap_seconds)}</td>` +
      `<td class="col-num">${t.lap_count}</td>` +
      `<td class="col-num">${t.pit_count}</td>` +
      `<td class="col-num">${escHtml(t.gap || '—')}</td>`;
    tbody.appendChild(tr);
  });
}

// ─── Events log ───────────────────────────────────────────────────────────────

function renderEvents(events) {
  const log = el('events-log');
  if (!log) return;
  log.innerHTML = (events || []).map(e =>
    `<div class="event-row lvl-${e.level}">` +
    `<span class="ev-time">${escHtml(e.time)}</span>` +
    `<span class="ev-cat">${escHtml(e.category)}</span>` +
    `<span class="ev-msg">${escHtml(e.message)}</span>` +
    `</div>`
  ).join('');
}

// ─── Decision card ────────────────────────────────────────────────────────────

function renderDecision(s, manualDriver) {
  if (!s) return;
  const d = s.decision || 'HOLD';

  // Badge text + class
  const badge = el('decision-badge');
  if (badge) { badge.textContent = d; badge.className = 'decision-word state-' + d; }

  // Panel glow
  const panel = el('panel-decision');
  if (panel) panel.className = 'panel panel-decision glow-' + d;

  setText('decision-window',   s.box_window);
  setText('decision-undercut', s.undercut === 'POSSIBLE' ? '⚡ POSSIBLE' : 'NO');
  setText('decision-risk',     s.risk);
  setText('next-driver',       s.recommended_driver);

  // Kart badge
  const kr = s.kart_rating || 'GOOD';
  const kb = el('kart-badge');
  if (kb) { kb.textContent = kr; kb.className = 'kart-pill kart-' + kr; }

  // Commands
  const cmds = el('decision-commands');
  if (cmds) {
    cmds.innerHTML = (s.commands || []).map(c =>
      `<div class="cmd-item">${escHtml(c)}</div>`
    ).join('');
  }

  // Pit stats
  setText('actual-pits',    s.actual_pits ?? 0);
  setText('expected-pits',  s.expected_pits != null ? s.expected_pits.toFixed(1) : '—');
  setText('pit-plan',       s.pit_plan);
  setText('recommended-driver', s.recommended_driver);

  // Pace
  setText('avg3-val', s.avg3 != null ? fmtLap(s.avg3) : '—');
  setText('avg5-val', s.avg5 != null ? fmtLap(s.avg5) : '—');
  const drop = s.pace_drop ?? 0;
  const dropEl = el('pace-drop-val');
  if (dropEl) {
    dropEl.textContent = (drop >= 0 ? '+' : '') + drop.toFixed(3) + 's';
    dropEl.className = 'pace-val ' + (drop > 0.30 ? 'pace-bad' : drop > 0.10 ? 'pace-warn' : 'pace-ok');
  }

  // Current driver
  setText('current-driver', manualDriver || '—');
}

// ─── Command page full render ─────────────────────────────────────────────────

function renderCommand(state) {
  setConnected(true, state.session.mode);

  // Store timestamps for local 1s tick
  T.raceDurationMs = (state.session.race_duration || 780) * 60000;
  T.raceStarted    = state.session.started;
  T.raceStartedAt  = state.session.started_at ? new Date(state.session.started_at).getTime() : null;
  T.stintStartedAt = state.manual?.stint_started_at ? new Date(state.manual.stint_started_at).getTime() : null;

  // Immediate tick (don't wait 1s)
  tickTimers();

  renderDecision(state.strategy, state.manual?.current_driver);
  renderTimingTable(state.teams);
  renderEvents(state.events);
}

// ─── Manual + race API helpers ────────────────────────────────────────────────

function sendManual(data) {
  fetch('/api/manual/', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data),
  }).catch(e => console.error('Manual API:', e));
}

function raceAction(action) {
  fetch('/api/race/', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action}),
  }).catch(e => console.error('Race API:', e));
}

// ─── Polling ──────────────────────────────────────────────────────────────────

function startPolling(url, renderer, ms) {
  async function tick() {
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      renderer(await r.json());
    } catch (e) {
      setConnected(false, null);
    }
    setTimeout(tick, ms);
  }
  tick();
}

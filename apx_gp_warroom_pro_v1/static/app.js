const $ = (id) => document.getElementById(id);
const fmt = (v, d=3) => (v === null || v === undefined || v === '') ? '--' : (typeof v === 'number' ? v.toFixed(d) : v);
const clock = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600)/60), sec = s % 60;
  return h > 0 ? `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}` : `${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
};
function setText(id, value){ const el = $(id); if(el) el.textContent = value; }
function cssTrend(t){ return t === '↑' ? 'trendUp' : (t === '↓' ? 'trendDown' : 'trendStable'); }
function updateStatusClass(status){ document.body.className = `status-${status || 'HOLD'}`; }

async function loadState(){
  try{
    const res = await fetch('/api/state', {cache:'no-store'});
    const state = await res.json();
    render(state);
  }catch(e){
    setText('sourceStatus','OFFLINE');
  }
}

function render(state){
  const m = state.metrics || {};
  const d = state.decision || {};
  updateStatusClass(d.status);
  setText('teamName', state.team_name || 'APX GP');
  setText('position', m.position ? `P${m.position}` : 'P--');
  setText('driver', m.current_driver || '--');
  setText('stint', clock(m.stint_seconds));
  setText('stintRemaining', clock(m.stint_remaining_seconds));
  setText('raceElapsed', clock(m.race_elapsed_seconds));
  setText('raceRemaining', clock(m.race_remaining_seconds));
  setText('phase', m.phase || '--');
  setText('avg5', fmt(m.avg5));
  setText('avg3', fmt(m.avg3));
  setText('avg10', fmt(m.avg10));
  setText('bestLap', fmt(m.best_lap));
  setText('lastLap', fmt(m.last_lap));
  const trendEl = $('trend');
  if(trendEl){ trendEl.textContent = `${m.trend || '→'} ${m.trend_label || ''}`; trendEl.className = `value ${cssTrend(m.trend)}`; }
  setText('kartRating', m.kart_rating || 'MEDIUM');
  setText('gapFront', m.gap_front || '--');
  setText('gapBack', m.gap_back || '--');
  setText('pitPlan', `${m.pits || 0}/23 · ${m.pit_plan_status || 'ON PLAN'}`);
  setText('expectedPits', fmt(m.expected_pits, 1));
  setText('sourceStatus', m.source_status || '--');
  setText('headline', d.headline || d.status || 'HOLD');
  setText('reason', d.reason || '--');
  setText('boxWindow', d.box_window || '--');
  setText('undercut', d.undercut || '--');
  setText('risk', d.risk || '--');
  setText('recommendedDriver', d.recommended_driver || '--');
  const commands = $('commands');
  if(commands){ commands.innerHTML = (d.commands || []).map(c => `<div class="command">${escapeHtml(c)}</div>`).join(''); }
  renderTable(state.top_table || []);
  renderEvents(state.events || []);
  renderCams(state.cameras || []);
  drawLaps(state.laps || []);
}

function renderTable(rows){
  const body = $('topTable'); if(!body) return;
  body.innerHTML = rows.map(r => `<tr>
    <td><strong>${r.position ? 'P'+r.position : '--'}</strong></td>
    <td>${escapeHtml(r.name || '')}</td>
    <td>${fmt(r.last_lap)}</td>
    <td>${fmt(r.avg5)}</td>
    <td class="${cssTrend(r.trend)}">${r.trend || '→'}</td>
    <td>${fmt(r.best_lap)}</td>
    <td>${r.pits || 0}</td>
    <td>${escapeHtml(r.gap_front || '')}</td>
  </tr>`).join('');
}

function renderEvents(events){
  const el = $('events'); if(!el) return;
  el.innerHTML = events.map(e => `<div class="event"><div class="type">${escapeHtml(e.event_type || '')}</div><div class="msg">${escapeHtml(e.message || '')}</div><div class="time">${escapeHtml(e.timestamp || '')}</div></div>`).join('');
}

function renderCams(cams){
  for(let i=0;i<2;i++){
    const box = $(`cam${i+1}`); if(!box) continue;
    const c = cams[i] || {};
    const name = c.name || `CAM ${i+1}`;
    const url = c.url || '';
    box.innerHTML = `<div class="tag">${escapeHtml(name)}</div>` + (url ? `<iframe src="${escapeAttr(url)}" allow="autoplay; fullscreen"></iframe>` : `<div class="placeholder">${escapeHtml(name)}<br/>Stream URL in config.json eintragen</div>`);
  }
}

function drawLaps(laps){
  const canvas = $('lapCanvas'); if(!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width = canvas.clientWidth * window.devicePixelRatio;
  const h = canvas.height = canvas.clientHeight * window.devicePixelRatio;
  ctx.clearRect(0,0,w,h);
  ctx.strokeStyle = '#2a3038'; ctx.lineWidth = 1;
  for(let i=1;i<4;i++){ ctx.beginPath(); ctx.moveTo(0,h*i/4); ctx.lineTo(w,h*i/4); ctx.stroke(); }
  if(!laps || laps.length < 2){ ctx.fillStyle='#8b929e'; ctx.font=`${16*window.devicePixelRatio}px Arial`; ctx.fillText('No lap history yet', 20, 35*window.devicePixelRatio); return; }
  const vals = laps.map(Number).filter(v => !Number.isNaN(v));
  const min = Math.min(...vals), max = Math.max(...vals);
  const range = Math.max(0.2, max-min);
  ctx.strokeStyle = '#f6c500'; ctx.lineWidth = 3*window.devicePixelRatio; ctx.beginPath();
  vals.forEach((v, i) => {
    const x = (i/(vals.length-1))*w;
    const y = h - ((v-min)/range)*h*0.8 - h*0.1;
    if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  });
  ctx.stroke();
  ctx.fillStyle='#8b929e'; ctx.font=`${12*window.devicePixelRatio}px Arial`;
  ctx.fillText(`min ${min.toFixed(3)} · max ${max.toFixed(3)}`, 18*window.devicePixelRatio, 24*window.devicePixelRatio);
}

async function manualUpdate(){
  const payload = {
    current_driver: $('manualDriver')?.value,
    current_kart: $('manualKart')?.value,
    box_status: $('manualBox')?.value,
    reset_stint: $('resetStint')?.checked || false
  };
  await fetch('/api/manual', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  if($('resetStint')) $('resetStint').checked = false;
  loadState();
}

function escapeHtml(s){return String(s).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#039;','"':'&quot;'}[c]));}
function escapeAttr(s){return escapeHtml(s).replace(/`/g,'&#096;');}

setInterval(loadState, 2000);
window.addEventListener('resize', () => loadState());
window.addEventListener('load', loadState);

// System-load / idle-detection analytics dashboard.
// Opened from the SYSTEM panel's "📊 LOAD" shortcut. Fetches /api/system/load,
// renders current idle status, a date filter, an hourly load histogram, and a
// per-day bar chart. Self-contained: does not piggy-back on the WS render loop.

let _slStart = null;   // 'YYYY-MM-DD' or null → full history
let _slEnd   = null;
let _slPreset = 'all';

function openSysLoad(){
  show('modal-sysload');
  el('modal-sysload-body').innerHTML = '<div class="sl-empty">Loading load history…</div>';
  _slLoad();
}

function _slLoad(){
  const qs = [];
  if(_slStart) qs.push('start=' + _slStart);
  if(_slEnd)   qs.push('end=' + _slEnd);
  fetch('/api/system/load' + (qs.length ? '?' + qs.join('&') : ''))
    .then(r => r.json())
    .then(_slRender)
    .catch(() => { el('modal-sysload-body').innerHTML =
      '<div class="sl-empty">Could not load system stats.</div>'; });
}

// Map a 0-100 load score to a colour class matching the idle thresholds.
function _slClass(score, idleMax){
  if(score < idleMax) return 'g';   // idle
  if(score < 50)      return 'c';
  if(score < 75)      return 'a';
  return 'r';
}

function _slPresetClick(p){
  _slPreset = p;
  const today = new Date();
  const fmt = d => d.toISOString().slice(0,10);
  if(p === 'all'){ _slStart = null; _slEnd = null; }
  else{
    const days = p === '1d' ? 0 : p === '7d' ? 6 : 13;
    const from = new Date(today); from.setDate(today.getDate() - days);
    _slStart = fmt(from); _slEnd = fmt(today);
  }
  _slLoad();
}

function _slApplyDates(){
  _slPreset = 'custom';
  _slStart = el('sl-from').value || null;
  _slEnd   = el('sl-to').value   || null;
  _slLoad();
}

function _slRender(d){
  const c = d.current || {};
  const rng = d.range || {};
  const idleMax = d.idle_score_max || 25;
  const nowH = new Date().getHours();

  // ── Current status ──────────────────────────────────────────────
  const badge = c.is_idle
    ? '<span class="sl-badge idle">● IDLE</span>'
    : '<span class="sl-badge busy">● BUSY</span>';
  let h = sec('CURRENT STATE');
  h += '<div class="sl-status">';
  h += `<div class="sl-stat"><div class="k">STATUS</div><div class="v">${badge}</div></div>`;
  h += `<div class="sl-stat"><div class="k">LOAD SCORE</div><div class="v">${c.load_score ?? '--'}</div></div>`;
  h += `<div class="sl-stat"><div class="k">CPU</div><div class="v">${c.cpu_pct ?? '--'}%</div></div>`;
  h += `<div class="sl-stat"><div class="k">RAM</div><div class="v">${c.ram_pct ?? '--'}%</div></div>`;
  const busyTxt = c.ollama_busy ? 'yes' : 'no';
  h += `<div class="sl-stat"><div class="k">QUERIES 8m / LLM</div><div class="v">${c.recent_queries ?? 0} / ${busyTxt}</div></div>`;
  h += '</div>';

  const pi = (d.predicted_idle || []).map(x => String(x).padStart(2,'0') + ':xx').join('  ');
  if(pi) h += row('Predicted idle hours', pi);
  h += row('Data quality', d.data_quality || '--');

  // ── Date filter ─────────────────────────────────────────────────
  const b = d.bounds || {};
  h += sec('DATE FILTER');
  h += '<div class="sl-filter">';
  const presets = [['all','ALL'],['1d','TODAY'],['7d','7D'],['14d','14D']];
  for(const [k,lbl] of presets)
    h += `<button class="sl-preset${_slPreset===k?' on':''}" onclick="_slPresetClick('${k}')">${lbl}</button>`;
  h += `<label>FROM</label><input type="date" id="sl-from" value="${_slStart||rng.start||''}" min="${b.earliest||''}" max="${b.latest||''}">`;
  h += `<label>TO</label><input type="date" id="sl-to" value="${_slEnd||rng.end||''}" min="${b.earliest||''}" max="${b.latest||''}">`;
  h += '<button class="sl-preset" onclick="_slApplyDates()">APPLY</button>';
  h += '</div>';

  // ── Range summary ───────────────────────────────────────────────
  h += sec(`WINDOW · ${rng.start||'?'} → ${rng.end||'?'}`);
  h += '<div class="sl-status">';
  h += `<div class="sl-stat"><div class="k">OBSERVATIONS</div><div class="v">${(rng.total||0).toLocaleString()}</div></div>`;
  h += `<div class="sl-stat"><div class="k">AVG LOAD</div><div class="v">${rng.avg_load ?? '--'}</div></div>`;
  h += `<div class="sl-stat"><div class="k">IDLE TIME</div><div class="v">${rng.idle_pct ?? '--'}%</div></div>`;
  h += `<div class="sl-stat"><div class="k">MIN / MAX</div><div class="v">${rng.min ?? '--'} / ${rng.max ?? '--'}</div></div>`;
  h += '</div>';

  // ── Hourly histogram ────────────────────────────────────────────
  h += sec('LOAD BY HOUR  (0–23, lower = more idle)');
  if((d.hourly||[]).length){
    const byH = {}; d.hourly.forEach(o => byH[o.hour] = o);
    h += '<div class="sl-chart">';
    for(let hr=0; hr<24; hr++){
      const o = byH[hr];
      const isNow = hr === nowH ? ' now' : '';
      if(o){
        const ht = Math.max(2, Math.round(o.avg_load));   // 0-100 → % height
        const cls = _slClass(o.avg_load, idleMax);
        h += `<div class="sl-col${isNow}" title="${String(hr).padStart(2,'0')}:00 — avg ${o.avg_load}, ${o.samples} samples${o.idle?' (idle)':''}">`
           + `<div class="sl-bar ${cls}" style="height:${ht}%"></div>`
           + `<div class="sl-xlabel">${String(hr).padStart(2,'0')}</div></div>`;
      }else{
        h += `<div class="sl-col${isNow}" title="${String(hr).padStart(2,'0')}:00 — no data">`
           + '<div class="sl-bar" style="height:1px;background:#12222f"></div>'
           + `<div class="sl-xlabel">${String(hr).padStart(2,'0')}</div></div>`;
      }
    }
    h += '</div>';
  }else{
    h += '<div class="sl-empty">No observations in this window.</div>';
  }

  // ── Daily bars ──────────────────────────────────────────────────
  h += sec('AVG LOAD BY DAY');
  if((d.daily||[]).length){
    h += '<div class="sl-chart">';
    for(const day of d.daily){
      const ht = Math.max(2, Math.round(day.avg_load));
      const cls = _slClass(day.avg_load, idleMax);
      const label = day.date.slice(5);   // MM-DD
      h += `<div class="sl-col" title="${day.date} — avg ${day.avg_load}, idle ${day.idle_pct}%, ${day.samples} samples">`
         + `<div class="sl-bar ${cls}" style="height:${ht}%"></div>`
         + `<div class="sl-xlabel">${label}</div></div>`;
    }
    h += '</div>';
  }else{
    h += '<div class="sl-empty">No daily data in this window.</div>';
  }

  // Legend
  h += '<div class="sl-legend">'
     + '<span><i class="sl-bar g"></i>idle (&lt;'+idleMax+')</span>'
     + '<span><i class="sl-bar c"></i>light</span>'
     + '<span><i class="sl-bar a"></i>moderate</span>'
     + '<span><i class="sl-bar r"></i>heavy</span>'
     + '<span>◻ outlined = current hour</span></div>';

  el('modal-sysload-body').innerHTML = h;
}

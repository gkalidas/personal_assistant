'use strict';

// ── Clock ─────────────────────────────────────────────────────────────────────
function tick(){const n=new Date(),p=x=>String(x).padStart(2,'0');el('clock').textContent=`${p(n.getHours())}:${p(n.getMinutes())}:${p(n.getSeconds())}  ${p(n.getDate())}/${p(n.getMonth()+1)}/${n.getFullYear()}`}
setInterval(tick,1000);tick();

let last={};
let _openModal=null;               // full-screen todo modal only (see todo.js)
const _DETAIL_TYPES=['network','system','weather','guardian'];
const _openDetails=new Set();      // detail windows currently open — all re-render on each WS tick
let _wxSig='';   // signature of last-rendered weather modal — gates needless rebuilds

// ── WebSocket ──────────────────────────────────────────────────────────────────
let ws;
function connect(){
  ws=new WebSocket(`ws://${location.host}/ws`);
  ws.onopen=()=>console.log('WS connected');
  ws.onmessage=e=>{last=JSON.parse(e.data);render(last);_openDetails.forEach(t=>openModal(t,true))};
  ws.onclose=()=>setTimeout(connect,3000);
  ws.onerror=()=>ws.close();
}
connect();

function render(d){
  if(d.network)  renderNetwork(d.network);
  if(d.services) renderServices(d.services);
  if(d.system)   renderSystem(d.system);
  if(d.weather)  renderWeather(d.weather);
  if(d.guardian) renderGuardian(d.guardian);
}

// ── Services (inside the network box) ─────────────────────────────────────────
function _svcDot(status){return status==='up'?'online':status==='off'?'noinet':'err'}
function renderServices(s){
  if(!s)return;
  const rows=[];
  for(const svc of (s.services||[])){
    rows.push(`<div class="svc-row"><div class="dot ${_svcDot(svc.status)}"></div>`+
              `<span class="svc-name">${svc.name}</span>`+
              `<span class="svc-detail">${svc.detail||''}</span></div>`);
  }
  if(s.modules&&s.modules.count){
    rows.push(`<div class="svc-row"><div class="dot online"></div>`+
              `<span class="svc-name">Modules</span>`+
              `<span class="svc-detail">${s.modules.count} loaded</span></div>`);
  }
  el('svc-sep').style.display=rows.length?'':'none';
  el('svc-list').innerHTML=rows.join('');
}

// ── Network ───────────────────────────────────────────────────────────────────
// SVG icons for each connection type — rendered inside fixed-width type cell
const _SVG_WIFI=`<svg viewBox="0 0 22 16" width="22" height="13" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="11" cy="14" r="1.6" fill="currentColor" stroke="none"/>
  <path d="M7,10.5 Q11,7 15,10.5" stroke-width="1.8"/>
  <path d="M3.5,7 Q11,1.5 18.5,7" stroke-width="1.8"/>
</svg>`;
const _SVG_LAN=`<svg viewBox="0 0 22 16" width="22" height="13" fill="none" stroke="currentColor" stroke-linecap="round">
  <rect x="3" y="2" width="16" height="10" rx="1.5" stroke-width="1.6"/>
  <line x1="7" y1="12" x2="7" y2="15" stroke-width="1.6"/>
  <line x1="15" y1="12" x2="15" y2="15" stroke-width="1.6"/>
  <rect x="6.5" y="4.5" width="2.5" height="3.5" rx="0.5" fill="currentColor" stroke="none"/>
  <rect x="9.75" y="4.5" width="2.5" height="3.5" rx="0.5" fill="currentColor" stroke="none"/>
  <rect x="13" y="4.5" width="2.5" height="3.5" rx="0.5" fill="currentColor" stroke="none"/>
</svg>`;
const _SVG_VPN=`<svg viewBox="0 0 18 20" width="16" height="15" fill="none" stroke="currentColor" stroke-linecap="round">
  <rect x="2" y="9" width="14" height="10" rx="2" stroke-width="1.7"/>
  <path d="M5.5,9 L5.5,5.5 Q5.5,1.5 9,1.5 Q12.5,1.5 12.5,5.5 L12.5,9" stroke-width="1.7"/>
  <circle cx="9" cy="14.5" r="1.8" fill="currentColor" stroke="none"/>
</svg>`;

function _typeIcon(type){
  const t=type.toLowerCase();
  if(t==='wifi'||t==='wireless'||t==='wlan')return _SVG_WIFI;
  if(t==='vpn'||t==='tun')return _SVG_VPN;
  return _SVG_LAN;
}
function _typeCls(type){
  const t=type.toLowerCase();
  if(t==='wifi'||t==='wireless'||t==='wlan')return 't-wifi';
  if(t==='vpn'||t==='tun')return 't-vpn';
  return 't-lan';
}

// Alternate between IP and interface name every 2.5s — WITHOUT changing layout width
let _showIfaceNames=false;
setInterval(()=>{_showIfaceNames=!_showIfaceNames;if(last.network)_renderIfaceNames(last.network.interfaces)},2500);

function _renderIfaceNames(ifaces){
  document.querySelectorAll('.iface-name-cell').forEach((cell,i)=>{
    const iface=ifaces[i];if(!iface)return;
    if(_showIfaceNames){
      cell.textContent=iface.name;
      cell.className='iface-name-cell showing-iface';
    }else{
      cell.textContent=iface.ip||'—';
      cell.className='iface-name-cell';
    }
  });
}

function renderNetwork(n){
  // Speed in label header
  const dl=n.download_mbps,ul=n.upload_mbps;
  const fmt=v=>v>=1?v.toFixed(1)+'M':(v*1000).toFixed(0)+'K';
  el('net-speed-lbl').textContent=`↓${fmt(dl)} ↑${fmt(ul)}`;

  // Build interface rows (stable layout — fixed-width cells)
  let html='';
  for(const i of n.interfaces){
    const icon=_typeIcon(i.type);
    const cls=_typeCls(i.type);
    const nameText=_showIfaceNames?i.name:(i.ip||'—');
    const nameCls=_showIfaceNames?'iface-name-cell showing-iface':'iface-name-cell';
    html+=`<div class="iface-row"><span class="iface-type-cell ${cls}">${icon}</span><span class="${nameCls}">${nameText}</span></div>`;
  }
  el('iface-list').innerHTML=html;
  el('failover-alert').style.display=n.failover_alert?'':'none';
}

// ── System — 4-level coloring ─────────────────────────────────────────────────
function _lvClass(pct){
  if(pct>=85)return'lv4';  // red     — critical
  if(pct>=60)return'lv3';  // amber   — watch
  if(pct>=30)return'lv2';  // cyan    — normal
  return'lv1';              // green   — healthy
}
function setBar(id,pct,sub){
  const cls=_lvClass(pct);
  const barEl=el(id+'-bar');
  barEl.style.width=Math.min(pct,100)+'%';
  barEl.className='bar-fill '+cls;
  const valEl=el(id+'-val');
  valEl.textContent=pct+'%';
  valEl.className='metric-val '+cls;
  const subEl=el(id+'-sub');
  if(subEl)subEl.textContent=sub||'';
}
function renderSystem(s){
  const coreStr=s.cpu_physical?` · ${s.cpu_physical}C/${s.cpu_cores}T`:s.cpu_cores?` · ${s.cpu_cores}T`:'';
  setBar('cpu',  Math.round(s.cpu_pct),  coreStr);
  setBar('ram',  Math.round(s.ram_pct),  s.ram_used_gb!=null?` · ${s.ram_used_gb}/${s.ram_total_gb}G`:'');
  setBar('swap', Math.round(s.swap_pct), s.swap_used_gb!=null?` · ${s.swap_used_gb}/${s.swap_total_gb}G`:'');
  setBar('disk', Math.round(s.disk_pct), s.disk_used_gb!=null?` · ${s.disk_used_gb}/${s.disk_total_gb}G`:'');
}

// ── Weather ────────────────────────────────────────────────────────────────────
function renderWeather(w){
  el('wx-temp').innerHTML=`${w.temp_c}<sup>°C</sup>`;
  el('wx-desc').textContent=(w.description||'').toUpperCase();
  el('wx-hum').textContent=w.humidity+'%';
  el('wx-wind').textContent=w.wind_kmh+' km/h';
  el('wx-feel').textContent=w.feels_like_c+'°C';
  el('wx-vis').textContent=w.visibility_km+' km';
  const today=new Date().toISOString().slice(0,10);
  const fcToday=(w.forecast||[]).find(f=>f.date===today)||(w.forecast||[])[0];
  el('wx-rain').textContent=fcToday&&fcToday.precip_mm!=null?fcToday.precip_mm+' mm':'-- mm';
  el('wx-rainpct').textContent=fcToday&&fcToday.rain_pct!=null?fcToday.rain_pct+'%':'--%';
  el('wx-hilo').textContent=fcToday&&fcToday.t_max!=null?`↑${Math.round(fcToday.t_max)}° ↓${Math.round(fcToday.t_min)}°`:'--° / --°';
}

// ── Guardian — blink speed by severity ───────────────────────────────────────
// Escape a string for safe use inside an HTML attribute (title="…").
function _attr(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// One-line remediation hint per audit finding, keyed by bandit test_id with a
// keyword fallback for custom (non-bandit) checks. Shown in the button tooltip.
const _AUDIT_REMEDY={
  B102:'Remove exec() of dynamic code — use an explicit dispatch dict {name:fn}, or ast.literal_eval for data.',
  B307:'Avoid eval() — use ast.literal_eval for literals or an explicit dispatch table.',
  B608:'Use parameterised SQL: cursor.execute(sql, (val,)). Never f-string/format user input into a query.',
  B104:'Bind to 127.0.0.1 instead of 0.0.0.0, or restrict the port via firewall / reverse proxy if it must be public.',
  B105:'Move the secret to a gitignored .env / secrets store, load it at runtime, and rotate the exposed value.',
  B106:'Stop passing the password as a default/arg — read it from env or a secrets store.',
  B107:'Remove the hardcoded password default; require it from config/env.',
  B303:'Use hashlib.sha256. If the hash is not security-relevant, pass usedforsecurity=False.',
  B324:'Replace weak hash (md5/sha1) with sha256, or set usedforsecurity=False for non-security use.',
  B301:'Do not unpickle untrusted data — use json, or sign/verify the payload first.',
  B403:'Avoid pickle for untrusted input; prefer json or a safe serializer.',
  B113:'Add an explicit timeout= to the request so it cannot hang indefinitely.',
  B201:'Never run with debug=True in production — it exposes a remote code console.',
  B602:'Avoid shell=True — pass an args list to subprocess and validate inputs.',
  B605:'Avoid os.system / shell strings — use subprocess with an args list.',
  B607:'Use an absolute path for the executable instead of relying on PATH.',
  B310:'Validate the URL scheme (allow only https) before urlopen to avoid file:// / SSRF.',
};
function _auditRemedy(iss){
  const t=(iss.test_id||'').toUpperCase();
  if(_AUDIT_REMEDY[t])return _AUDIT_REMEDY[t];
  const s=(iss.issue||iss.message||'').toLowerCase();
  if(s.includes('sql'))return _AUDIT_REMEDY.B608;
  if(s.includes('exec'))return _AUDIT_REMEDY.B102;
  if(s.includes('eval'))return _AUDIT_REMEDY.B307;
  if(s.includes('bind')||s.includes('all interfaces'))return _AUDIT_REMEDY.B104;
  if(s.includes('.gitignore')||s.includes('gitignored'))return 'Add the file to .gitignore and rotate any secret already committed to git.';
  if(s.includes('permission'))return 'Tighten file permissions: chmod 600 the sensitive file.';
  if(s.includes('secret')||s.includes('password')||s.includes('token')||s.includes('key'))return 'Move the secret to a gitignored .env / secrets store and rotate it.';
  return 'Fix the flagged code in the source file'+(iss.more_info?' — see '+iss.more_info:'.');
}

function renderGuardian(g){
  const panel=el('p-guardian');
  panel.className='panel clickable '+(g.overall||'ok');
  const state=el('g-state');
  state.className='guardian-state '+(g.overall||'ok');
  const icon=el('g-icon');
  const sub=el('g-sub');
  if(g.overall==='ok'){
    icon.textContent='◈';state.textContent='ALL CLEAR';sub.textContent='No threats';
  }else if(g.overall==='warn'){
    icon.textContent='⚠';state.textContent='ATTENTION';
    const parts=[];
    if(g.vuln_count)   parts.push(g.vuln_count+' CVE');
    if(g.audit_issues) parts.push(g.audit_issues+' code');
    sub.textContent=parts.join(' · ')||'Review needed';
  }else{
    icon.textContent='✕';state.textContent='ALERT';
    const parts=[];
    if(g.alert_count)   parts.push(g.alert_count+' anomaly');
    if(g.threat_vulns)  parts.push(g.threat_vulns+' threat');
    if(g.audit_critical)parts.push(g.audit_critical+' critical');
    sub.textContent=parts.join(' · ');
  }
  const aC=g.anomaly.alerts===0?'ok':'danger';
  el('g-anomaly').className='scan-val '+aC;
  el('g-anomaly').textContent=g.anomaly.alerts===0?'CLEAN':`${g.anomaly.alerts} ALERT`;
  el('g-anomaly-t').textContent=g.anomaly.at||'--';
  const vC=g.vuln.total===0?'ok':'danger';
  el('g-vuln').className='scan-val '+vC;
  el('g-vuln').textContent=g.vuln.total===0?'CLEAN':`${g.vuln.total} VULN`;
  el('g-vuln-t').textContent=g.vuln.at||'--';
  const tC=g.threat.vulns===0?'ok':'danger';
  el('g-threat').className='scan-val '+tC;
  el('g-threat').textContent=g.threat.vulns===0?'SAFE':`${g.threat.vulns} VULN`;
  el('g-threat-t').textContent=g.threat.at||'--';
  const auC=g.audit.total===0?'ok':g.audit_critical>0?'danger':'warn';
  el('g-audit').className='scan-val '+auC;
  el('g-audit').textContent=g.audit.total===0?'CLEAN':`${g.audit.total} ISSUE`;
  el('g-audit-t').textContent=g.audit.at||'--';
}

// ── Modals ─────────────────────────────────────────────────────────────────────
function _typeLabel(t){return{lan:'ETHERNET',wifi:'WI-FI',vpn:'VPN',tethering:'USB/BT',unknown:'UNKNOWN'}[t]||(t||'?').toUpperCase()}
// isRefresh=true → re-render triggered by a WS tick: keep stacking order untouched.
function openModal(type,isRefresh){
  if(_DETAIL_TYPES.includes(type)){
    _openDetails.add(type);
    if(!isRefresh){const m=el('modal-'+type);if(m&&m.classList.contains('open'))m.style.zIndex=++_detailZ;}
  }
  if(type==='network'&&last.network){
    const n=last.network;
    let rows='';
    for(const i of n.interfaces){
      rows+=sec(i.name.toUpperCase()+' ('+_typeLabel(i.type)+')');
      rows+=row('Status',i.is_up?'UP':'DOWN');
      rows+=row('Internet',i.is_connected?'<span style="color:var(--green)">YES</span>':'<span style="color:var(--red)">NO</span>');
      if(i.ip)rows+=row('IP',i.ip);
      if(i.link_mbps)rows+=row('Link',i.link_mbps+' Mbps');
    }
    rows+=sec('THROUGHPUT');
    rows+=row('Download',n.download_mbps.toFixed(2)+' Mbps');
    rows+=row('Upload',  n.upload_mbps.toFixed(2)+' Mbps');
    rows+=row('Primary',n.primary||'none');
    if(last.services){
      const sv=last.services;
      rows+=sec('SERVICES'+(sv.at?' · '+sv.at:''));
      const fmt=st=>st==='up'?'<span style="color:var(--green)">UP</span>':
                    st==='off'?'<span style="color:var(--amber)">OFF</span>':
                    '<span style="color:var(--red)">DOWN</span>';
      for(const svc of (sv.services||[])){
        rows+=row(svc.name,fmt(svc.status)+(svc.detail?' <span style="color:var(--dim)">· '+svc.detail+'</span>':''));
      }
      if(sv.modules&&sv.modules.count){
        rows+=row('Modules',sv.modules.count+' loaded');
        if(sv.modules.names)rows+=row('',`<span style="color:var(--dim);font-size:9px">${sv.modules.names.join(' · ')}</span>`);
      }
    }
    el('modal-net-body').innerHTML=rows;
    show('modal-network');
  }else if(type==='system'&&last.system){
    const s=last.system;
    let rows=sec('PROCESSOR');
    rows+=row('CPU',s.cpu_pct+'%');
    if(s.cpu_physical)rows+=row('Cores',`${s.cpu_physical} physical · ${s.cpu_cores} logical`);
    rows+=row('Freq',s.cpu_freq_mhz+' MHz');
    rows+=row('Load',s.load_avg);
    rows+=sec('MEMORY');
    rows+=row('RAM',s.ram_pct+'%  ('+s.ram_used_gb+' / '+s.ram_total_gb+' GiB)');
    rows+=row('Swap',s.swap_pct+'%  ('+s.swap_used_gb+' / '+s.swap_total_gb+' GiB)');
    rows+=sec('STORAGE');
    rows+=row('Disk',s.disk_pct+'%  ('+s.disk_used_gb+' / '+s.disk_total_gb+' GiB)');
    rows+=sec('SYSTEM');
    rows+=row('Uptime',s.uptime);
    el('modal-sys-body').innerHTML=rows;
    show('modal-system');
  }else if(type==='weather'&&last.weather){
    const w=last.weather;
    // Skip rebuilds when nothing changed — prevents flicker/scroll-reset on every WS tick.
    const wxOpen=el('modal-weather').classList.contains('open');
    const sig=(w.updated_at||'')+'|'+w.temp_c+'|'+((w.forecast||[]).length)+'|'+(_farmHealthHTML?1:0);
    if(wxOpen&&sig===_wxSig)return;
    _wxSig=sig;
    let rows=sec('CONDITIONS — '+(w.location||'').toUpperCase());
    rows+=row('Temperature',w.temp_c+'°C');
    rows+=row('Feels Like',w.feels_like_c+'°C');
    rows+=row('Description',w.description);
    rows+=sec('ATMOSPHERE');
    rows+=row('Humidity',w.humidity+'%');
    rows+=row('Wind',w.wind_kmh+' km/h '+(w.wind_dir||''));
    rows+=row('Visibility',w.visibility_km+' km');
    if(w.forecast&&w.forecast.length){
      rows+=sec('7-DAY FORECAST');
      rows+=`<table class="forecast-table"><tr><th>DATE</th><th>CONDITIONS</th><th>MAX</th><th>MIN</th><th>RAIN%</th><th>WIND</th><th>PRECIP</th></tr>`;
      const today=new Date().toISOString().slice(0,10);
      for(const f of w.forecast){
        const isToday=f.date===today;
        const d=new Date(f.date);
        const dayLabel=isToday?'TODAY':['SUN','MON','TUE','WED','THU','FRI','SAT'][d.getDay()]+' '+f.date.slice(5);
        const maxCls=f.t_max!=='--'&&Number(f.t_max)>38?'fc-hot':'fc-cool';
        rows+=`<tr class="${isToday?'fc-today':''}"><td>${dayLabel}</td><td class="fc-desc">${f.desc}</td><td class="${maxCls}">${f.t_max}°</td><td style="color:#6ab0c0">${f.t_min}°</td><td class="${f.rain_pct>=40?'fc-rain':''}">${f.rain_pct}%</td><td>${f.wind_kmh}</td><td>${f.precip_mm}</td></tr>`;
      }
      rows+='</table>';
    }
    rows+=sec('META');
    rows+=row('Updated',w.updated_at||'--');
    rows+=row('Age',(w.age_min||0)+' min');
    rows+=_farmHealthHTML||'';   // inject cached farm health in the same atomic render
    el('modal-wx-body').innerHTML=rows;
    if(!_farmHealthHTML)_loadFarmHealth(el('modal-wx-body'));   // fetch once; cached thereafter
    show('modal-weather');
  }else if(type==='guardian'&&last.guardian){
    const g=last.guardian;
    let html=sec('SCAN RESULTS');
    const aC=g.anomaly.alerts===0?'var(--green)':'var(--red)';
    html+=row('Anomaly',`<span style="color:${aC}">${g.anomaly.alerts===0?'CLEAN':g.anomaly.alerts+' ALERT(S)'}</span>`);
    const vC2=g.vuln.total===0?'var(--green)':'var(--red)';
    html+=row('CVE',`<span style="color:${vC2}">${g.vuln.total===0?'CLEAN':g.vuln.total+' VULN(S)'}</span>`);
    html+=row('Last run',g.vuln.at||'--');
    if(g.vuln.items&&g.vuln.items.length){
      html+=sec('VULNERABILITIES');
      g.vuln.items.forEach(v=>{
        const sevCls=(v.severity||'UNKNOWN').toUpperCase();
        const fix=v.fix?`fix: ${v.fix}`:'no fix yet';
        const desc=v.summary?' — '+v.summary.substring(0,70):'';
        html+=`<div class="issue-row"><div class="sev ${sevCls}">${sevCls}</div><div class="loc">${v.package} ${v.version}</div><div class="msg">${v.id}${desc} <span style="color:var(--dim)">· ${fix}</span></div></div>`;
      });
    }
    const tC2=g.threat.vulns===0?'var(--green)':'var(--red)';
    html+=row('Threat Intel',`<span style="color:${tC2}">${g.threat.vulns===0?'NOT VULNERABLE':g.threat.vulns+' VULNERABLE'}</span>`);
    if(g.threat.items&&g.threat.items.length){
      // Store patterns outside HTML attrs to avoid JSON double-quote breakage
      _threatPatterns=g.threat.items.map(t=>t.pattern||null);
      const fixableCount=_threatPatterns.filter(Boolean).length;
      html+=`<div style="display:flex;align-items:center;justify-content:space-between;margin:10px 0 4px">`;
      html+=`<div style="font-size:9px;letter-spacing:3px;color:var(--dim)">THREAT DETAILS</div>`;
      if(fixableCount)html+=`<button class="panel-btn" id="fix-all-btn" title="Add all ${fixableCount} detected attack patterns to the request firewall (security/patterns.json) so they are blocked from now on." style="margin:0;font-size:8px;padding:3px 8px;letter-spacing:2px" onclick="fixAllThreatPatterns()">⚡ FIX ALL (${fixableCount})</button>`;
      html+=`</div>`;
      g.threat.items.forEach((t,i)=>{
        const hasFix=!!t.pattern;
        const isOpen=_threatOpen.has(i);
        html+=`<div class="threat-acc" id="tacc-${i}">`;
        html+=`<div class="threat-acc-hdr" onclick="toggleThreat(${i})">`;
        html+=`<span class="tacc-arrow${isOpen?' open':''}" id="tarr-${i}">▶</span>`;
        html+=`<span class="sev HIGH" style="margin:0 6px;font-size:8px">${(t.type||'threat').toUpperCase()}</span>`;
        html+=`<span style="color:var(--dim);font-size:9px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${t.id||''} — ${(t.title||'').substring(0,55)}</span>`;
        if(hasFix)html+=`<button class="fix-btn panel-btn" id="fbtn-${i}" title="Block this attack pattern: adds it to the request firewall (security/patterns.json) so matching requests are rejected going forward." style="margin:0;font-size:8px;padding:2px 7px;letter-spacing:1px;flex-shrink:0" onclick="event.stopPropagation();fixThreatAt(${i})">FIX</button>`;
        html+=`</div>`;
        html+=`<div class="threat-acc-body" id="tbody-${i}" style="display:${isOpen?'block':'none'}">`;
        html+=`<div class="msg" style="font-size:10px;margin-bottom:6px">${t.title||''}</div>`;
        if(t.evidence)html+=`<div style="font-size:9px;color:var(--amber);margin-bottom:4px;word-break:break-word">⚠ ${t.evidence}</div>`;
        if(t.details)html+=`<div style="font-size:9px;color:var(--dim);margin-bottom:4px">${t.details}</div>`;
        if(t.pattern)html+=`<div style="font-size:9px;color:var(--dim);font-family:monospace;background:rgba(0,0,0,0.3);padding:4px 6px;border-radius:2px;word-break:break-all">pattern: ${t.pattern.pattern||''}</div>`;
        html+=`</div></div>`;
      });
    }
    const auC2=g.audit.total===0?'var(--green)':g.audit_critical>0?'var(--red)':'var(--amber)';
    const sev=g.audit.severity;
    const sevStr=sev?`CRIT:${sev.CRITICAL||0} HIGH:${sev.HIGH||0} MED:${sev.MEDIUM||0}`:'';
    html+=row('Code Audit',`<span style="color:${auC2}">${g.audit.total===0?'CLEAN':g.audit.total+' ISSUE(S)'}</span>${sevStr?' · '+sevStr:''}`);
    if(g.audit.issues&&g.audit.issues.length){
      html+=sec('CODE ISSUES');
      g.audit.issues.forEach((iss,issIdx)=>{
        const blocked=(iss.severity||'').toUpperCase()==='CRITICAL'||(iss.severity||'').toUpperCase()==='HIGH';
        const remedy=_auditRemedy(iss);
        // MUTE adds # nosec (suppresses the warning); it does not remediate code,
        // so CRITICAL/HIGH findings can't be muted — they must be fixed in code.
        const btn=blocked
          ?`<button class="fix-btn panel-btn" id="audit-fix-${issIdx}" disabled title="${_attr(`Cannot mute a ${iss.severity} finding — fix it in code.\nHow: ${remedy}`)}" style="margin:0;font-size:8px;padding:2px 7px;letter-spacing:1px;flex-shrink:0;opacity:.5;cursor:not-allowed">FIX IN CODE</button>`
          :`<button class="fix-btn panel-btn" id="audit-fix-${issIdx}" title="${_attr(`MUTE: appends # nosec to silence this ${iss.severity} linter warning. It does NOT change behaviour.\nProper fix: ${remedy}`)}" style="margin:0;font-size:8px;padding:2px 7px;letter-spacing:1px;flex-shrink:0" onclick="event.stopPropagation();fixAuditIssue(${issIdx})">MUTE</button>`;
        html+=`<div class="issue-row"><div class="sev ${iss.severity}">${iss.severity}</div><div class="loc">${iss.file||''}${iss.line?':'+iss.line:''}</div><div class="msg">${(iss.issue||iss.message||'').substring(0,120)}</div>${btn}</div>`;
      });
    }
    if(g.schedule&&g.schedule.length){
      html+=sec('SCHEDULE');
      html+=`<table class="sched-table"><tr><th>TASK</th><th>LAST</th><th>NEXT</th></tr>`;
      for(const t of g.schedule)html+=`<tr><td>${t.task.replace('_',' ')}</td><td>${t.last}</td><td class="${t.next_status}">${t.next}</td></tr>`;
      html+='</table>';
    }
    html+=sec('UPGRADE');
    html+=`<button class="panel-btn" title="Upgrade outdated pip packages to their latest versions (general maintenance, not CVE-targeted). Also lists apt updates to apply manually with sudo." style="margin:0;font-size:9px;letter-spacing:2px" onclick="runUpgrade()">⬆ UPGRADE PACKAGES NOW</button><div class="upg-result" id="upg-result" style="display:none"></div>`;
    el('modal-g-body').innerHTML=html;
    show('modal-guardian');
  }
}

// ── Floating detail windows ────────────────────────────────────────────────────
// Detail modals are free-floating windows: no overlay, draggable by the title
// bar, any number open at once. Positions persist like the panel layout.
const _DPOSKEY='gk_detail_pos_v1';
let _detailZ=300, _mdrag=null;
let _dpos={};
try{_dpos=JSON.parse(localStorage.getItem(_DPOSKEY)||'{}')}catch(e){}

function _setDetailXY(m,x,y){
  m.style.left=Math.min(Math.max(0,x),window.innerWidth-60)+'px';
  m.style.top =Math.min(Math.max(0,y),window.innerHeight-40)+'px';
  m.style.transform='none';
}
function _placeDetail(m){
  const p=_dpos[m.id];
  if(p&&p.w)m.style.width =Math.min(p.w,window.innerWidth -20)+'px';
  if(p&&p.h)m.style.height=Math.min(p.h,window.innerHeight-20)+'px';
  if(p&&typeof p.x==='number'){_setDetailXY(m,p.x,p.y);return}
  // no saved spot — cascade from centre so new windows don't stack exactly
  const n=Math.max(0,document.querySelectorAll('.modal.open').length-1);
  const r=m.getBoundingClientRect();
  _setDetailXY(m,(window.innerWidth-r.width)/2+n*34,Math.max(8,(window.innerHeight-r.height)/2+n*34));
}
function show(id){
  const m=el(id);
  if(m.classList.contains('open'))return;
  m.classList.add('open');
  m.style.zIndex=++_detailZ;
  _placeDetail(m);
}

// Drag any .modal by its title bar; mousedown anywhere on it brings it to front.
document.addEventListener('mousedown',e=>{
  const m=e.target.closest('.modal');
  if(!m)return;
  m.style.zIndex=++_detailZ;
  if(!e.target.closest('.modal-title')||e.target.closest('button,a,input,select,.modal-close'))return;
  e.preventDefault();
  const r=m.getBoundingClientRect();
  _setDetailXY(m,r.left,r.top);   // freeze the centred position before dragging
  _mdrag={m,dx:e.clientX-r.left,dy:e.clientY-r.top};
});
document.addEventListener('mousemove',e=>{
  if(_mdrag)_setDetailXY(_mdrag.m,e.clientX-_mdrag.dx,e.clientY-_mdrag.dy);
});
document.addEventListener('mouseup',()=>{
  let dirty=false;
  // Native resize (CSS resize:both) writes inline width/height — persist them.
  document.querySelectorAll('.modal.open').forEach(m=>{
    const w=parseFloat(m.style.width)||0, h=parseFloat(m.style.height)||0;
    if(!w&&!h)return;
    const p=_dpos[m.id]||(_dpos[m.id]={});
    if(p.w!==w||p.h!==h){if(w)p.w=w;if(h)p.h=h;dirty=true;}
  });
  if(_mdrag){
    const r=_mdrag.m.getBoundingClientRect();
    const p=_dpos[_mdrag.m.id]||(_dpos[_mdrag.m.id]={});
    p.x=r.left;p.y=r.top;dirty=true;
    _mdrag=null;
  }
  if(dirty)try{localStorage.setItem(_DPOSKEY,JSON.stringify(_dpos))}catch(e){}
});

// closeModal('modal-xyz') closes that window only; closeModal() closes everything.
function closeModal(id){
  if(typeof id==='string'){
    el(id).classList.remove('open');
    _openDetails.delete(id.replace(/^modal-/,''));
    return;
  }
  _openModal=null;
  _openDetails.clear();
  _threatOpen.clear();
  el('overlay').classList.remove('open');
  document.querySelectorAll('.modal').forEach(m=>m.classList.remove('open'));
  el('modal-diary').classList.remove('open');
  el('modal-todo-full').classList.remove('open');
}
function closeDiaryBook(){closeModal()}
function el(id){return document.getElementById(id)}
function row(k,v){return `<div class="detail-row"><span class="detail-key">${k}</span><span class="detail-val">${v}</span></div>`}
function sec(t){return `<div class="detail-section">${t}</div>`}

document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){closeModal();if(el('modal-livetv-overlay').classList.contains('open'))closeLiveTV();else if(el('modal-news-overlay').classList.contains('open'))closeNewsModal();else _startNewsScroll();}
  if(e.ctrlKey&&e.key==='d'){e.preventDefault();writeDiaryFromPhotos();}
});

// ── Weather refresh ────────────────────────────────────────────────────────────
async function refreshWeather(){
  const btn=el('wx-refresh-btn');
  btn.disabled=true;btn.textContent='…';
  try{const r=await fetch('/api/weather/refresh',{method:'POST'});renderWeather(await r.json());_loadNdviPanel();btn.textContent='✓';setTimeout(()=>{btn.textContent='↻';btn.disabled=false},1500)}
  catch(e){btn.textContent='!';setTimeout(()=>{btn.textContent='↻';btn.disabled=false},2000)}
}

// ── Guardian update button ─────────────────────────────────────────────────────

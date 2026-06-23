'use strict';

// ── Clock ─────────────────────────────────────────────────────────────────────
function tick(){const n=new Date(),p=x=>String(x).padStart(2,'0');el('clock').textContent=`${p(n.getHours())}:${p(n.getMinutes())}:${p(n.getSeconds())}  ${p(n.getDate())}/${p(n.getMonth()+1)}/${n.getFullYear()}`}
setInterval(tick,1000);tick();

let last={};
let _openModal=null;

// ── WebSocket ──────────────────────────────────────────────────────────────────
let ws;
function connect(){
  ws=new WebSocket(`ws://${location.host}/ws`);
  ws.onopen=()=>console.log('WS connected');
  ws.onmessage=e=>{last=JSON.parse(e.data);render(last);if(_openModal)openModal(_openModal)};
  ws.onclose=()=>setTimeout(connect,3000);
  ws.onerror=()=>ws.close();
}
connect();

function render(d){
  if(d.network)  renderNetwork(d.network);
  if(d.system)   renderSystem(d.system);
  if(d.weather)  renderWeather(d.weather);
  if(d.guardian) renderGuardian(d.guardian);
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
    const isVpn=i.type==='vpn'||i.type==='tun';
    const dc=i.is_connected?'online':i.is_up?(isVpn?'online':'noinet'):'down';
    const icon=_typeIcon(i.type);
    const cls=_typeCls(i.type);
    const nameText=_showIfaceNames?i.name:(i.ip||'—');
    const nameCls=_showIfaceNames?'iface-name-cell showing-iface':'iface-name-cell';
    html+=`<div class="iface-row"><div class="dot ${dc}"></div><span class="iface-type-cell ${cls}">${icon}</span><span class="${nameCls}">${nameText}</span></div>`;
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
}

// ── Guardian — blink speed by severity ───────────────────────────────────────
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
function openModal(type){
  _openModal=type;
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
    el('modal-wx-body').innerHTML=rows;
    // Load NDVI + soil trend async into modal
    _loadFarmHealth(el('modal-wx-body'));
    show('modal-weather');
  }else if(type==='guardian'&&last.guardian){
    const g=last.guardian;
    let html=sec('SCAN RESULTS');
    const aC=g.anomaly.alerts===0?'var(--green)':'var(--red)';
    html+=row('Anomaly',`<span style="color:${aC}">${g.anomaly.alerts===0?'CLEAN':g.anomaly.alerts+' ALERT(S)'}</span>`);
    const vC2=g.vuln.total===0?'var(--green)':'var(--red)';
    html+=row('CVE',`<span style="color:${vC2}">${g.vuln.total===0?'CLEAN':g.vuln.total+' VULN(S)'}</span>`);
    html+=row('Last run',g.vuln.at||'--');
    const tC2=g.threat.vulns===0?'var(--green)':'var(--red)';
    html+=row('Threat Intel',`<span style="color:${tC2}">${g.threat.vulns===0?'NOT VULNERABLE':g.threat.vulns+' VULNERABLE'}</span>`);
    if(g.threat.items&&g.threat.items.length){
      // Store patterns outside HTML attrs to avoid JSON double-quote breakage
      _threatPatterns=g.threat.items.map(t=>t.pattern||null);
      const fixableCount=_threatPatterns.filter(Boolean).length;
      html+=`<div style="display:flex;align-items:center;justify-content:space-between;margin:10px 0 4px">`;
      html+=`<div style="font-size:9px;letter-spacing:3px;color:var(--dim)">THREAT DETAILS</div>`;
      if(fixableCount)html+=`<button class="panel-btn" id="fix-all-btn" style="margin:0;font-size:8px;padding:3px 8px;letter-spacing:2px" onclick="fixAllThreatPatterns()">⚡ FIX ALL (${fixableCount})</button>`;
      html+=`</div>`;
      g.threat.items.forEach((t,i)=>{
        const hasFix=!!t.pattern;
        const isOpen=_threatOpen.has(i);
        html+=`<div class="threat-acc" id="tacc-${i}">`;
        html+=`<div class="threat-acc-hdr" onclick="toggleThreat(${i})">`;
        html+=`<span class="tacc-arrow${isOpen?' open':''}" id="tarr-${i}">▶</span>`;
        html+=`<span class="sev HIGH" style="margin:0 6px;font-size:8px">${(t.type||'threat').toUpperCase()}</span>`;
        html+=`<span style="color:var(--dim);font-size:9px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${t.id||''} — ${(t.title||'').substring(0,55)}</span>`;
        if(hasFix)html+=`<button class="fix-btn panel-btn" id="fbtn-${i}" style="margin:0;font-size:8px;padding:2px 7px;letter-spacing:1px;flex-shrink:0" onclick="event.stopPropagation();fixThreatAt(${i})">FIX</button>`;
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
        html+=`<div class="issue-row"><div class="sev ${iss.severity}">${iss.severity}</div><div class="loc">${iss.file||''}${iss.line?':'+iss.line:''}</div><div class="msg">${(iss.issue||iss.message||'').substring(0,120)}</div><button class="fix-btn panel-btn" id="audit-fix-${issIdx}" style="margin:0;font-size:8px;padding:2px 7px;letter-spacing:1px;flex-shrink:0" onclick="event.stopPropagation();fixAuditIssue(${issIdx})">FIX</button></div>`;
      });
    }
    if(g.schedule&&g.schedule.length){
      html+=sec('SCHEDULE');
      html+=`<table class="sched-table"><tr><th>TASK</th><th>LAST</th><th>NEXT</th></tr>`;
      for(const t of g.schedule)html+=`<tr><td>${t.task.replace('_',' ')}</td><td>${t.last}</td><td class="${t.next_status}">${t.next}</td></tr>`;
      html+='</table>';
    }
    html+=sec('UPGRADE');
    html+=`<button class="panel-btn" style="margin:0;font-size:9px;letter-spacing:2px" onclick="runUpgrade()">⬆ UPGRADE PACKAGES NOW</button><div class="upg-result" id="upg-result" style="display:none"></div>`;
    el('modal-g-body').innerHTML=html;
    show('modal-guardian');
  }
}

function show(id){el('overlay').classList.add('open');el(id).classList.add('open')}
function closeModal(){
  _openModal=null;
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

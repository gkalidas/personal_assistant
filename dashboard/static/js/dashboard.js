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
async function runCodeAnalysis(){
  const btn=el('g-scan-btn');
  const summary=el('g-code-summary');
  btn.disabled=true;btn.textContent='◈ …';
  summary.style.display='block';summary.textContent='Scanning codebase…';
  try{
    const r=await fetch('/api/code/scan');
    if(!r.ok){summary.textContent='Scan failed: '+r.status;btn.disabled=false;btn.textContent='◈ SCAN';return}
    const d=await r.json();
    const topFn=d.top_complex_functions&&d.top_complex_functions[0];
    const topFnStr=topFn?` · worst cc=${topFn.complexity} ${topFn.function}()`:'';
    const secStr=d.security_issues&&d.security_issues.length?` · ${d.security_issues.length} sec issues`:'· 0 sec issues';
    summary.innerHTML=
      `<span style="color:var(--cyan)">${d.total_files} files · ${Number(d.total_loc).toLocaleString()} LOC</span>${topFnStr}${secStr}<br>`+
      (d.by_language||[]).slice(0,4).map(l=>`${l.language}: ${l.loc.toLocaleString()}`).join(' · ');
    btn.textContent='◈ DONE';
    setTimeout(()=>{btn.textContent='◈ SCAN';btn.disabled=false},3000);
    // Open modal with full report if user clicks DONE
    btn.onclick=ev=>{ev.stopPropagation();showCodeReport(d);btn.textContent='◈ SCAN';btn.disabled=false;btn.onclick=ev2=>{ev2.stopPropagation();runCodeAnalysis()}};
  }catch(e){summary.textContent='Error: '+e.message;btn.disabled=false;btn.textContent='◈ SCAN'}
}

function showCodeReport(d){
  const topFns=(d.top_complex_functions||[]).slice(0,8)
    .map(f=>`<tr><td style="color:var(--red)">${f.complexity}</td><td style="color:var(--amber)">${f.function}()</td><td style="color:var(--dim)">${f.file}:${f.line}</td></tr>`).join('');
  const langs=(d.by_language||[]).slice(0,6)
    .map(l=>`<tr><td>${l.language}</td><td style="color:var(--cyan)">${(l.loc||0).toLocaleString()}</td><td style="color:var(--dim)">${l.files} files</td></tr>`).join('');
  const body=`
    <div style="font-size:10px;letter-spacing:1px;color:var(--dim);margin-bottom:12px">
      Total: <span style="color:var(--cyan)">${d.total_files} files · ${Number(d.total_loc).toLocaleString()} lines</span>
      · Security: <span style="color:var(--green)">✓ ${(d.security_issues||[]).length} issues</span>
    </div>
    <div style="margin-bottom:10px"><div style="font-size:9px;letter-spacing:2px;color:var(--dim);margin-bottom:6px">LANGUAGES</div>
    <table style="width:100%;border-collapse:collapse;font-size:10px">${langs}</table></div>
    <div><div style="font-size:9px;letter-spacing:2px;color:var(--dim);margin-bottom:6px">COMPLEX FUNCTIONS</div>
    <table style="width:100%;border-collapse:collapse;font-size:10px"><tr style="color:var(--dim);font-size:9px"><th style="text-align:left">CC</th><th style="text-align:left">Function</th><th style="text-align:left">File</th></tr>${topFns}</table></div>`;
  el('modal-g-body').innerHTML=body;
  openModal('guardian');
}

async function triggerUpdate(){
  const btn=el('g-upd-btn');
  btn.disabled=true;btn.className='panel-btn running';btn.textContent='⬆ …';
  try{
    await fetch('/api/guardian/patch',{method:'POST'});
    // Poll until guardian data refreshes (max 30s)
    let attempts=0;
    const poll=setInterval(async()=>{
      attempts++;
      try{const r=await fetch('/api/data');const d=await r.json();if(d.guardian){last.guardian=d.guardian;renderGuardian(d.guardian)}}catch(_){}
      if(attempts>=15){clearInterval(poll);btn.className='panel-btn';btn.textContent='⬆ DONE';setTimeout(()=>{btn.textContent='⬆ UPDATE';btn.disabled=false},2000)}
    },2000);
  }catch(e){btn.className='panel-btn';btn.textContent='⬆ ERR';setTimeout(()=>{btn.textContent='⬆ UPDATE';btn.disabled=false},2000)}
}

// ── Threat accordion ──────────────────────────────────────────────────────────
const _threatOpen=new Set();    // persists across WS re-renders
let _threatPatterns=[];         // set during modal render; avoids JSON-in-onclick

function fixThreatAt(i){
  const p=_threatPatterns[i];
  if(p)fixThreats([p],i);
}
function fixAllThreatPatterns(){
  const ps=_threatPatterns.filter(Boolean);
  if(ps.length)fixThreats(ps,null);
}

function toggleThreat(i){
  const body=el('tbody-'+i);
  const arr=el('tarr-'+i);
  if(!body||!arr)return;
  if(_threatOpen.has(i)){
    _threatOpen.delete(i);
    body.style.display='none';
    arr.classList.remove('open');
  }else{
    _threatOpen.add(i);
    body.style.display='block';
    arr.classList.add('open');
  }
}

let _fixInFlight=false;
async function fixThreats(patterns,singleIdx){
  if(_fixInFlight)return;
  _fixInFlight=true;
  // Disable all fix buttons
  document.querySelectorAll('.fix-btn,#fix-all-btn').forEach(b=>{
    b.disabled=true;b.classList.add('running');
  });
  const target=singleIdx!=null?el('fbtn-'+singleIdx):el('fix-all-btn');
  if(target)target.textContent='FIXING…';
  try{
    const r=await fetch('/api/guardian/fix-threat',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({patterns})
    });
    const d=await r.json();
    if(d.ok){
      const added=d.added||[];
      // Mark fixed rows
      if(singleIdx!=null){
        const acc=el('tacc-'+singleIdx);
        if(acc)acc.classList.add('fixed');
        if(target){target.textContent='✓ FIXED';target.classList.remove('running');}
        // Check if all are fixed → update Fix All button
        const remaining=document.querySelectorAll('.fix-btn:not([data-fixed])').length;
        if(remaining===0){const fa=el('fix-all-btn');if(fa){fa.textContent='✓ ALL FIXED';fa.classList.remove('running');}}
      }else{
        document.querySelectorAll('.threat-acc').forEach(a=>a.classList.add('fixed'));
        document.querySelectorAll('.fix-btn').forEach(b=>{b.textContent='✓ FIXED';b.classList.remove('running');});
        if(target){target.textContent='✓ ALL FIXED';target.classList.remove('running');}
      }
      // Re-enable Fix All only if something was added (others stay disabled once fixed)
    }else{
      alert('Fix failed: '+(d.error||'unknown error'));
      document.querySelectorAll('.fix-btn,#fix-all-btn').forEach(b=>{
        b.disabled=false;b.classList.remove('running');
      });
      if(target&&singleIdx!=null)target.textContent='FIX';
      else if(target)target.textContent='⚡ FIX ALL';
    }
  }catch(e){
    alert('Fix error: '+e.message);
    document.querySelectorAll('.fix-btn,#fix-all-btn').forEach(b=>{
      b.disabled=false;b.classList.remove('running');
    });
  }finally{
    _fixInFlight=false;
  }
}

async function runUpgrade(){
  const res=el('upg-result');
  res.style.display='block';res.textContent='Running pip upgrade…';
  try{
    const r=await fetch('/api/guardian/upgrade',{method:'POST'});
    const d=await r.json();
    const results=d.results||{};
    let out='';
    if(results.pip_upgraded!==undefined){
      out+=`Upgraded: ${results.pip_upgraded.length?results.pip_upgraded.join(', '):'none'}\n`;
      if(results.pip_outdated&&results.pip_outdated.length)out+=`Outdated found: ${results.pip_outdated.join(', ')}\n`;
    }
    if(results.apt_upgradable){
      out+=`\nApt upgradable (use sudo apt upgrade):\n${results.apt_upgradable.slice(0,10).join(', ')}${results.apt_upgradable.length>10?'…':''}`;
    }
    res.textContent=out||'Done.';
  }catch(e){res.textContent='Error: '+e.message}
}

async function fixAuditIssue(idx){
  const iss=(last.guardian?.audit?.issues||[])[idx];
  if(!iss)return;
  const btn=el('audit-fix-'+idx);
  if(btn){btn.textContent='FIXING…';btn.disabled=true;btn.classList.add('running');}
  try{
    const r=await fetch('/api/guardian/fix-audit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({issue:iss})});
    const d=await r.json();
    if(d.ok){
      if(btn){btn.textContent='✓ FIXED';btn.classList.remove('running');btn.style.color='var(--green)';btn.style.borderColor='var(--green)';}
      setTimeout(async()=>{try{const rd=await fetch('/api/data');const gd=await rd.json();if(gd.guardian){last.guardian=gd.guardian;renderGuardian(gd.guardian)}}catch(_){}},2000);
    }else{
      if(btn){btn.textContent='ERR';btn.disabled=false;btn.classList.remove('running');}
    }
  }catch(e){
    if(btn){btn.textContent='ERR';btn.disabled=false;btn.classList.remove('running');}
  }
}

// ── Farm health (NDVI + soil moisture) ───────────────────────────────────────
async function _loadNdviPanel(){
  try{
    const r=await fetch('/api/farming/ndvi');
    const d=await r.json();
    if(d.error)return;
    const ndviEl=el('wx-ndvi');
    if(ndviEl){
      const pct=Math.round(d.latest_ndvi*100);
      const cls=d.health_color==='good'||d.health_color==='ok'?'color:var(--green)':d.health_color==='warn'?'color:var(--amber)':'color:var(--red)';
      ndviEl.innerHTML=`<span style="${cls}">${d.latest_ndvi.toFixed(3)}</span>`;
      ndviEl.title=d.interpretation+' ('+d.latest_date+')';
    }
  }catch(e){}
  try{
    const r=await fetch('/api/farming/soil-trend?days=7');
    const d=await r.json();
    if(d.error||!d.history||!d.history.length)return;
    const latest=d.history[d.history.length-1];
    const soilEl=el('wx-soil');
    if(soilEl&&latest.soil_moisture!=null){
      const sm=(latest.soil_moisture*100).toFixed(1);
      soilEl.textContent=sm+'%';
    }
  }catch(e){}
}

async function _loadFarmHealth(container){
  try{
    const [ndviR,soilR]=await Promise.all([
      fetch('/api/farming/ndvi'),
      fetch('/api/farming/soil-trend?days=14'),
    ]);
    const ndvi=await ndviR.json();
    const soil=await soilR.json();
    let html='';
    if(!ndvi.error){
      html+=`<div class="detail-section">CROP HEALTH — NASA MODIS NDVI</div>`;
      const cls=ndvi.health_color==='good'||ndvi.health_color==='ok'?'color:var(--green)':ndvi.health_color==='warn'?'color:var(--amber)':'color:var(--red)';
      html+=`<div class="detail-row"><span class="detail-key">NDVI (${ndvi.latest_date})</span><span style="${cls}">${ndvi.latest_ndvi.toFixed(3)} — ${ndvi.interpretation}</span></div>`;
      if(ndvi.history&&ndvi.history.length>1){
        html+=`<table class="forecast-table"><tr><th>DATE</th><th>NDVI</th><th>STATUS</th></tr>`;
        for(const h of ndvi.history.slice(-6)){
          const c=h.color==='good'||h.color==='ok'?'var(--green)':h.color==='warn'?'var(--amber)':'var(--red)';
          const bar='█'.repeat(Math.max(1,Math.round(h.ndvi*10)));
          html+=`<tr><td>${h.date}</td><td style="color:${c}">${bar} ${h.ndvi.toFixed(3)}</td><td class="fc-desc">${h.label}</td></tr>`;
        }
        html+='</table>';
      }
    }
    if(!soil.error&&soil.history&&soil.history.length){
      html+=`<div class="detail-section">SOIL MOISTURE TREND (14 DAYS)</div>`;
      html+=`<table class="forecast-table"><tr><th>DATE</th><th>MOISTURE</th><th>TEMP °C</th><th>RAIN mm</th></tr>`;
      for(const h of soil.history.slice(-7)){
        const sm=h.soil_moisture!=null?(h.soil_moisture*100).toFixed(1)+'%':'--';
        const st=h.soil_temp_c!=null?h.soil_temp_c.toFixed(1):'--';
        const rain=h.rain_mm!=null?h.rain_mm.toFixed(1):'--';
        const rainCls=h.rain_mm>5?'fc-rain':'';
        html+=`<tr><td>${h.date}</td><td style="color:#6ab0c0">${sm}</td><td>${st}</td><td class="${rainCls}">${rain}</td></tr>`;
      }
      html+='</table>';
    }
    if(html)container.insertAdjacentHTML('beforeend',html);
  }catch(e){}
}

// Load NDVI panel on page startup
_loadNdviPanel();

// ── Diary ──────────────────────────────────────────────────────────────────────
let _diaryDrafts=[],_bookPages=[],_bookPage=0;

async function loadDiaryList(){
  try{const r=await fetch('/api/diary/drafts');_diaryDrafts=await r.json();renderDiaryPanel()}
  catch(e){el('diary-footer').textContent='unavailable'}
  // Load photo processing stats separately (non-blocking)
  try{
    const rs=await fetch('/api/diary/photo-stats');
    const ps=await rs.json();
    const stat=el('diary-photo-stat');
    if(stat){
      const last=ps.last_processed_at?(ps.last_processed_at||'').slice(0,10):'never';
      stat.textContent=ps.total_processed?`${ps.total_processed} photos processed · last: ${last}`:'no photos processed yet';
    }
  }catch(e){}
  loadDiaryQuestions();
}

async function loadDiaryQuestions(){
  try{
    const r=await fetch('/api/diary/questions');
    const qs=await r.json();
    const box=el('diary-questions');
    if(!box)return;
    if(!qs.length){box.style.display='none';return;}
    box.style.display='';
    let html='';
    for(const q of qs.slice(0,4)){
      const photo=q.photo_path?q.photo_path.split('/').pop():'';
      const label=photo?`<span style="color:#4a2a6a">${photo}</span> — `:'';
      html+=`<div class="dq-item"><div class="dq-dot"></div><div class="dq-text">${label}${q.question}</div><button class="dq-answer-btn" onclick="event.stopPropagation();answerDiaryQuestion(${q.id})">ANSWER</button></div>`;
    }
    if(qs.length>4)html+=`<div style="font-size:8px;letter-spacing:1px;color:#4a2a6a;padding:2px 6px">+${qs.length-4} more questions…</div>`;
    box.innerHTML=html;
  }catch(e){}
}

async function answerDiaryQuestion(qid){
  const ans=prompt('Your answer:');
  if(!ans||!ans.trim())return;
  try{
    await fetch(`/api/diary/questions/${qid}/answer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answer:ans.trim()})});
    loadDiaryQuestions();
  }catch(e){}
}

async function writeDiaryFromPhotos(){
  const btn=el('diary-write-btn');
  if(!btn)return;
  btn.disabled=true;btn.textContent='WRITING…';
  try{
    const r=await fetch('/api/diary/write-photos',{method:'POST'});
    const d=await r.json();
    btn.textContent='STARTED ✓';
    // Show brief toast
    const t=document.createElement('div');
    t.style.cssText='position:fixed;bottom:20px;right:20px;background:#1a0e2a;border:1px solid #d2a8ff;color:#d2a8ff;font-size:10px;letter-spacing:2px;padding:8px 14px;border-radius:2px;z-index:999';
    t.textContent='DIARY: writing from ~/Pictures…';
    document.body.appendChild(t);
    setTimeout(()=>{document.body.removeChild(t);btn.textContent='✎ FROM PHOTOS';btn.disabled=false;loadDiaryList()},4000);
  }catch(e){btn.textContent='✎ FROM PHOTOS';btn.disabled=false;}
}
function renderDiaryPanel(){
  const list=el('diary-list'),footer=el('diary-footer');
  if(!_diaryDrafts.length){list.innerHTML='<div style="font-size:10px;letter-spacing:2px;color:#0c2535;padding:4px 0">No entries yet — say "write diary from my photos"</div>';footer.textContent='0 entries';return}
  let html='';
  for(const d of _diaryDrafts.slice(0,6)){
    const ap=d.approved?'<span style="color:var(--green)">✓ APPROVED</span>':'<span style="color:var(--dim)">○ DRAFT</span>';
    html+=`<div class="diary-entry-row" onclick="event.stopPropagation();openDiaryBook('${d.week}')"><span class="d-week">${d.week}</span>${ap}<span style="font-size:9px;color:#0c2535">${d.created_at}</span></div>`;
  }
  list.innerHTML=html;
  footer.textContent=`${_diaryDrafts.length} entr${_diaryDrafts.length===1?'y':'ies'} · ${_diaryDrafts.filter(d=>d.approved).length} approved`;
}
async function retryDiaryWeek(week){
  const btn=el('diary-retry-btn');
  if(btn){btn.textContent='RETRYING…';btn.disabled=true;}
  try{
    const r=await fetch(`/api/diary/retry/${encodeURIComponent(week)}`,{method:'POST'});
    const d=await r.json();
    if(d.ok){
      if(btn){btn.textContent='✓ STARTED';btn.style.color='var(--green)';btn.style.borderColor='var(--green)';}
      setTimeout(()=>{loadDiaryList();},3000);
    }else{
      if(btn){btn.textContent='RETRY';btn.disabled=false;}
      alert('Retry failed: '+(d.error||'unknown'));
    }
  }catch(e){
    if(btn){btn.textContent='RETRY';btn.disabled=false;}
  }
}
async function openDiaryBook(week){
  const target=week||(_diaryDrafts[0]&&_diaryDrafts[0].week);
  if(!target){alert('No diary entries yet. Say: "write diary from my photos"');return}
  try{const r=await fetch('/api/diary/'+encodeURIComponent(target));if(!r.ok)return;_buildBook(await r.json());el('overlay').classList.add('open');el('modal-diary').classList.add('open')}
  catch(e){console.error('diary open',e)}
}
function _buildBook(draft){
  const raw=(draft.draft||'').trim();
  const hasError=raw.includes('[Could not generate');
  const sections=raw.split(/(?=^(?:──|##)\s)/m).filter(s=>s.trim());
  _bookPages=sections.length?sections.map(s=>{const nl=s.indexOf('\n');const title=s.substring(0,nl<0?undefined:nl).replace(/^──\s*/,'').replace(/\s*──\s*$/,'').replace(/^##\s*/,'').trim();return{title,body:nl<0?'':s.substring(nl+1).trim()}}):
    [{title:draft.week,body:raw||'(no content)'}];
  _bookPage=0;
  el('book-week-label').textContent=draft.week||'—';
  const retryBtn=hasError?`<button onclick="retryDiaryWeek('${draft.week}')" style="font-size:8px;letter-spacing:2px;padding:2px 8px;border:1px solid var(--amber);border-radius:2px;background:transparent;color:var(--amber);cursor:pointer;font-family:inherit;margin-left:8px" id="diary-retry-btn">↺ RETRY</button>`:'';
  el('book-status-badge').innerHTML=(draft.approved?'<span class="badge ok">APPROVED</span>':'<span class="badge warn">DRAFT</span>')+retryBtn;
  let ch='';
  _bookPages.forEach((p,i)=>ch+=`<div class="chapter-item${i===0?' active':''}" id="ch-${i}" onclick="bookGoTo(${i})">${p.title}</div>`);
  el('book-chapters').innerHTML=ch;
  el('book-page-active').innerHTML=_formatPage(_bookPages[0]);
  _updateBookNav();
}
function _formatPage(page){
  if(!page.body)return`<div class="pg-title">${page.title}</div><div class="empty-note">— no content —</div>`;
  const body=page.body.replace(/📷[^\n]*/g,m=>{
    const fn=(m.match(/📷\s*(\S+)/)||[])[1]||'';
    const safe=fn.replace(/'/g,"\\'").replace(/"/g,'&quot;');
    return`<div class="photo-note" onclick="openPhotoLightbox('${safe}')">${m}</div>`;
  }).split(/\n{2,}/).filter(p=>p.trim()).map(p=>p.startsWith('<div')?p:`<p>${p.replace(/\n/g,'<br>')}</p>`).join('');
  return`<div class="pg-title">${page.title}</div>${body}`;
}
function openPhotoLightbox(filename){
  if(!filename)return;
  const lb=el('photo-lightbox'),img=el('photo-lightbox-img'),nm=el('photo-lightbox-name'),err=el('photo-lightbox-err');
  err.style.display='none';img.style.display='';img.src='';
  nm.textContent=filename;
  img.onload=()=>{err.style.display='none';img.style.display=''};
  img.onerror=()=>{img.style.display='none';err.style.display=''};
  img.src='/api/diary/photo/'+encodeURIComponent(filename);
  lb.classList.add('open');
}
function closePhotoLightbox(){el('photo-lightbox').classList.remove('open');el('photo-lightbox-img').src=''}
function _updateBookNav(){
  el('book-prev').disabled=_bookPage===0;
  el('book-next').disabled=_bookPage>=_bookPages.length-1;
  el('book-counter').textContent=`PAGE ${_bookPage+1} / ${_bookPages.length}`;
  document.querySelectorAll('.chapter-item').forEach((c,i)=>c.className='chapter-item'+(i===_bookPage?' active':''));
}
function bookGoTo(idx){if(idx===_bookPage||idx<0||idx>=_bookPages.length)return;const dir=idx>_bookPage?'next':'prev';_bookPage=idx;_animatePage(dir)}
function bookNextPage(){bookGoTo(_bookPage+1)}
function bookPrevPage(){bookGoTo(_bookPage-1)}
function _animatePage(direction){
  const area=el('book-page-area'),oldPage=el('book-page-active');
  const newPage=document.createElement('div');newPage.className='book-page';newPage.id='book-page-active';newPage.innerHTML=_formatPage(_bookPages[_bookPage]);area.appendChild(newPage);
  const enterFrom=direction==='next'?'110%':'-110%',exitTo=direction==='next'?'-110%':'110%';
  const tiltIn=direction==='next'?'6deg':'-6deg',tiltOut=direction==='next'?'-6deg':'6deg';
  const T='transform 0.35s ease,opacity 0.35s ease';
  newPage.style.cssText=`transform:translateX(${enterFrom}) rotateY(${tiltIn});opacity:0;`;
  requestAnimationFrame(()=>requestAnimationFrame(()=>{
    if(oldPage){oldPage.style.transition=T;oldPage.style.transform=`translateX(${exitTo}) rotateY(${tiltOut})`;oldPage.style.opacity='0'}
    newPage.style.transition=T;newPage.style.transform='translateX(0) rotateY(0)';newPage.style.opacity='1';
  }));
  if(oldPage){const OLD=oldPage;OLD.id='';setTimeout(()=>OLD.parentNode&&OLD.parentNode.removeChild(OLD),400)}
  _updateBookNav();
}
loadDiaryList();

// ── News — auto-scroll ─────────────────────────────────────────────────────────
let _newsItems=[],_newsScrollTimer=null,_newsDismissed=new Set();

function _stopNewsScroll(){if(_newsScrollTimer){clearInterval(_newsScrollTimer);_newsScrollTimer=null}}
function _startNewsScroll(){
  if(_newsScrollTimer)return;
  const wrap=el('news-scroll-wrap');
  if(!wrap)return;
  _newsScrollTimer=setInterval(()=>{
    wrap.scrollTop+=1;
    const inner=el('news-list');
    if(inner&&wrap.scrollTop>=inner.offsetHeight/2)wrap.scrollTop=0;
  },80);
}
function _restartNewsScroll(){_stopNewsScroll();el('news-scroll-wrap').scrollTop=0;_startNewsScroll()}
// Set news list cards — only duplicate if content overflows the visible wrap height
function _applyNewsCards(cards){
  const list=el('news-list');
  list.innerHTML=cards;
  _stopNewsScroll();
  requestAnimationFrame(()=>{
    const wrap=el('news-scroll-wrap');
    if(wrap&&list&&list.scrollHeight>wrap.clientHeight){
      list.innerHTML=cards+cards;
    }
    _startNewsScroll();
  });
}

async function loadNews(){
  try{const r=await fetch('/api/news');renderNews(await r.json())}
  catch(e){el('news-footer').textContent='unavailable'}
}

function renderNews(data){
  const all=data.items||[];
  _newsItems=all.filter(a=>!_newsDismissed.has(a.url));
  const farming=_newsItems.filter(a=>a.category==='farming').length;
  const world=_newsItems.filter(a=>a.category==='world').length;
  const ageStr=data.age_min===0?'live':data.age_min+'m ago';
  el('news-footer').textContent=_newsItems.length
    ?`${farming} farming · ${world} world · ${ageStr}`:'no articles';
  if(!_newsItems.length){
    el('news-list').innerHTML='<div style="font-size:10px;letter-spacing:2px;color:#0c2535;padding:8px">No articles — click ↻ REFRESH</div>';
    return;
  }
  const cards=_newsItems.map((a,i)=>{
    const cat=a.category==='world'?'world':'farming';
    const label=cat==='world'?'WORLD':'FARM';
    const title=(a.title||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return`<div class="news-card">
      <span class="news-card-badge ${cat}">${label}</span>
      <div class="news-card-body" onclick="openNewsModal(${i})" style="min-width:0">
        <div class="news-card-title">${title}</div>
        <div class="news-card-meta"><span>${a.source||''}</span> · ${a.date||''}</div>
      </div>
      <span class="news-card-dismiss" title="Dismiss" onclick="dismissNews('${(a.url||'').replace(/'/g,'')}')">×</span>
    </div>`;
  }).join('');
  _applyNewsCards(cards);
}

async function dismissNews(url){
  if(!url)return;
  _newsDismissed.add(url);
  _newsItems=_newsItems.filter(a=>a.url!==url);
  // Rebuild list without the dismissed item
  const all=_newsItems;
  if(!all.length){_stopNewsScroll();el('news-list').innerHTML='<div style="font-size:10px;letter-spacing:2px;color:#0c2535;padding:8px">No articles</div>';el('news-footer').textContent='0 articles';return}
  const farming=all.filter(a=>a.category==='farming').length;
  const world=all.filter(a=>a.category==='world').length;
  el('news-footer').textContent=`${farming} farming · ${world} world`;
  const cards=all.map((a,i)=>{
    const cat=a.category==='world'?'world':'farming';
    const label=cat==='world'?'WORLD':'FARM';
    const title=(a.title||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return`<div class="news-card">
      <span class="news-card-badge ${cat}">${label}</span>
      <div class="news-card-body" onclick="openNewsModal(${i})" style="min-width:0">
        <div class="news-card-title">${title}</div>
        <div class="news-card-meta"><span>${a.source||''}</span> · ${a.date||''}</div>
      </div>
      <span class="news-card-dismiss" title="Dismiss" onclick="dismissNews('${(a.url||'').replace(/'/g,'')}')">×</span>
    </div>`;
  }).join('');
  _applyNewsCards(cards);
  // Also tell server so it stays dismissed across next refresh
  fetch('/api/news/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})}).catch(()=>{});
}

async function refreshNews(){
  const btn=el('news-refresh-btn');btn.disabled=true;btn.textContent='↻ …';
  try{const r=await fetch('/api/news/refresh',{method:'POST'});renderNews(await r.json());btn.textContent='↻ DONE';setTimeout(()=>{btn.textContent='↻ REFRESH';btn.disabled=false},1500)}
  catch(e){btn.textContent='↻ ERR';setTimeout(()=>{btn.textContent='↻ REFRESH';btn.disabled=false},2000)}
}
loadNews();
setInterval(loadNews,5*60*1000);  // poll every 5 min to match server TTL

// ── News popup ─────────────────────────────────────────────────────────────────
let _nmHasImg=false, _nmHasVid=false, _nmImgPending=false;

function _nmCheck(){
  // Only declare NO MEDIA once both video resolved AND image is not still loading.
  if(!_nmHasImg&&!_nmHasVid&&!_nmImgPending){
    el('news-img-wrap').style.display='none';
    el('news-video-hdr').style.display='none';
    el('news-video-loading').style.display='none';
    el('news-video-none').style.display='none';
    el('news-no-media').style.display='flex';
  }
}

function openNewsModal(idx){
  const a=_newsItems[idx%_newsItems.length];if(!a)return;
  _nmHasImg=false;_nmHasVid=false;_nmImgPending=false;

  el('news-modal-title').textContent=a.title;
  el('news-modal-meta').innerHTML=`<span>${a.source}</span> · ${a.date}`;
  el('news-modal-text').textContent=a.body||'Click "Read Full Article" for full content.';
  el('news-modal-link').href=a.url;

  // Reset all media states
  el('news-img-wrap').style.display='none';
  el('news-img').src='';
  el('news-video-hdr').style.display='none';
  el('news-vfull-btn').style.display='none';
  el('news-video-loading').style.display='none';
  el('news-video-frame').style.display='none';
  el('news-video-frame').src='';
  el('news-video-none').style.display='none';
  el('news-no-media').style.display='none';

  _stopNewsScroll();
  el('modal-news-overlay').classList.add('open');

  // ── Image (jpg/png/gif/webp/svg — <img> handles all natively) ──
  if(a.image){
    _nmImgPending=true;
    const img=el('news-img');
    img.onload=()=>{
      _nmHasImg=true;_nmImgPending=false;
      el('news-img-wrap').style.display='block';
    };
    img.onerror=()=>{
      _nmHasImg=false;_nmImgPending=false;
      el('news-img-wrap').style.display='none';
      _nmCheck(); // re-check now that image settled
    };
    img.src=a.image;
  }

  // ── Video (async DDGS search) ──
  el('news-video-hdr').style.display='flex';
  el('news-video-label').textContent='RELATED VIDEO';
  el('news-video-loading').style.display='flex';

  const q=a.title.replace(/[^\w\s]/g,' ').trim().slice(0,100);
  const ytFallback=`https://www.youtube.com/results?search_query=${encodeURIComponent(q)}`;
  fetch(`/api/news/video?q=${encodeURIComponent(q)}`)
    .then(r=>r.json())
    .then(d=>{
      el('news-video-loading').style.display='none';
      if(d.video_id){
        _nmHasVid=true;
        el('news-video-frame').src=`https://www.youtube-nocookie.com/embed/${d.video_id}?rel=0&modestbranding=1&autoplay=0`;
        el('news-video-frame').style.display='block';
        el('news-vfull-btn').style.display='';
      }else{
        el('news-video-none').style.display='flex';
        el('news-video-yt-link').href=ytFallback;
        el('news-video-label').textContent='VIDEO';
        _nmCheck();
      }
    })
    .catch(()=>{
      el('news-video-loading').style.display='none';
      el('news-video-none').style.display='flex';
      el('news-video-yt-link').href=ytFallback;
      _nmCheck();
    });
}

function closeNewsModal(){
  el('modal-news-overlay').classList.remove('open');
  el('news-video-frame').src='';
  el('news-video-frame').style.display='none';
  el('news-img').src='';
  _startNewsScroll();
}

/* ── Live TV multi-channel news grid ─────────────────────────────────────── */
let _ytApiReady=false, _ytApiLoading=false;
const _liveTV={players:{}, channels:[], focus:null, paused:false, muted:true};

// Load the YouTube IFrame API once; resolve when YT.Player is available.
function _loadYTApi(){
  return new Promise((resolve)=>{
    if(_ytApiReady) return resolve();
    window.onYouTubeIframeAPIReady=()=>{_ytApiReady=true;resolve();};
    if(_ytApiLoading) return;
    _ytApiLoading=true;
    const s=document.createElement('script');
    s.src='https://www.youtube.com/iframe_api';
    document.head.appendChild(s);
  });
}

async function openLiveTV(){
  el('modal-livetv-overlay').classList.add('open');
  _stopNewsScroll();
  const grid=el('livetv-grid');
  if(_liveTV.channels.length){return;}            // already built — keep streams alive
  grid.innerHTML='<div style="grid-column:1/-1;font-size:10px;letter-spacing:2px;color:var(--dim);padding:20px">loading channels…</div>';
  let data;
  try{ data=await (await fetch('/api/news/channels')).json(); }
  catch(e){ grid.innerHTML='<div style="grid-column:1/-1;color:var(--red);font-size:10px;padding:20px">failed to load channels</div>'; return; }
  _liveTV.channels=data.channels||[];
  grid.innerHTML='';
  _liveTV.channels.forEach(ch=>{
    const tile=document.createElement('div');
    tile.className='livetv-tile'; tile.id='tv-tile-'+ch.id;
    tile.onclick=()=>liveTVFocus(ch.id);
    tile.innerHTML=
      '<iframe id="tv-frame-'+ch.id+'" src="'+ch.embed_url+'" '+
        'allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe>'+
      '<div class="livetv-tile-bar"><span class="livetv-tile-live">● LIVE</span>'+
        '<span>'+ch.name+'</span><span class="livetv-tile-mute" id="tv-mute-'+ch.id+'">🔇</span></div>';
    grid.appendChild(tile);
  });
  await _loadYTApi();
  _liveTV.channels.forEach(ch=>{
    _liveTV.players[ch.id]=new YT.Player('tv-frame-'+ch.id,{
      events:{ onReady:(e)=>{ e.target.mute(); e.target.playVideo(); } }
    });
  });
}

// Focus a channel: unmute it, mute all others, reveal the control bar.
function liveTVFocus(id){
  _liveTV.focus=id; _liveTV.paused=false; _liveTV.muted=false;
  _liveTV.channels.forEach(ch=>{
    const p=_liveTV.players[ch.id], tile=el('tv-tile-'+ch.id), mi=el('tv-mute-'+ch.id);
    const on=(ch.id===id);
    tile.classList.toggle('focused',on);
    if(p&&p.mute){ if(on){p.unMute();p.setVolume(100);} else {p.mute();} }
    if(mi) mi.textContent=on?'🔊':'🔇';
  });
  const focused=_liveTV.channels.find(c=>c.id===id);
  el('livetv-focus-name').textContent=focused?('· '+focused.name):'';
  el('livetv-controls').style.display='flex';
  el('livetv-offset').textContent='';
  el('livetv-playpause').textContent='⏸ PAUSE';
  el('livetv-mute').textContent='🔊 MUTE';
}

function _focusPlayer(){ return _liveTV.focus?_liveTV.players[_liveTV.focus]:null; }

function liveTVPlayPause(){
  const p=_focusPlayer(); if(!p) return;
  _liveTV.paused=!_liveTV.paused;
  if(_liveTV.paused){ p.pauseVideo(); el('livetv-playpause').textContent='▶ PLAY'; }
  else { p.playVideo(); el('livetv-playpause').textContent='⏸ PAUSE'; }
}

function liveTVToggleMute(){
  const p=_focusPlayer(); if(!p||!_liveTV.focus) return;
  _liveTV.muted=!_liveTV.muted;
  const mi=el('tv-mute-'+_liveTV.focus);
  if(_liveTV.muted){ p.mute(); el('livetv-mute').textContent='🔇 UNMUTE'; if(mi)mi.textContent='🔇'; }
  else { p.unMute(); p.setVolume(100); el('livetv-mute').textContent='🔊 MUTE'; if(mi)mi.textContent='🔊'; }
}

// Rewind `secs` into the live DVR buffer; secs=0 jumps back to the live edge.
function liveTVSeek(secs){
  const p=_focusPlayer();
  if(!p){ alert('Click a channel first to control playback.'); return; }
  let dur=0; try{ dur=p.getDuration()||0; }catch(e){}
  if(secs===0){
    if(dur>0) p.seekTo(dur,true);
    el('livetv-offset').textContent='';
    el('livetv-playpause').textContent='⏸ PAUSE'; _liveTV.paused=false;
    return;
  }
  if(dur<=1){ el('livetv-offset').textContent='(no DVR buffer on this channel)'; return; }
  const target=Math.max(0,dur-secs);
  p.seekTo(target,true); p.playVideo();
  _liveTV.paused=false; el('livetv-playpause').textContent='⏸ PAUSE';
  const have=Math.round((dur-target)/60);
  el('livetv-offset').textContent='−'+have+' min'+(have<Math.round(secs/60)?' (max DVR)':'');
}

function closeLiveTV(){
  el('modal-livetv-overlay').classList.remove('open');
  // Pause every stream so audio/CPU stops while hidden; keep players for fast reopen.
  Object.values(_liveTV.players).forEach(p=>{ try{ p.pauseVideo(); p.mute(); }catch(e){} });
  _liveTV.focus=null;
  el('livetv-controls').style.display='none';
  el('livetv-focus-name').textContent='';
}

function videoFullscreen(){
  const fr=el('news-video-frame');
  (fr.requestFullscreen||fr.webkitRequestFullscreen||fr.mozRequestFullScreen||function(){}).call(fr);
}

// ── Voice (WAV capture, no ffmpeg) ─────────────────────────────────────────────
class WavRecorder{
  constructor(){this.actx=null;this.stream=null;this.processor=null;this.source=null;this.chunks=[];this.analyser=null;this._actualSR=16000}
  async start(){
    // Request 16kHz but some browsers/hardware may override — always use actx.sampleRate
    this.actx=new AudioContext({sampleRate:16000});
    this._actualSR=this.actx.sampleRate;  // actual rate the browser granted
    this.stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true}});
    this.source=this.actx.createMediaStreamSource(this.stream);
    this.analyser=this.actx.createAnalyser();this.analyser.fftSize=256;
    this.processor=this.actx.createScriptProcessor(4096,1,1);
    this.chunks=[];
    this.processor.onaudioprocess=e=>{const d=e.inputBuffer.getChannelData(0);this.chunks.push(new Float32Array(d))};
    this.source.connect(this.analyser);this.source.connect(this.processor);this.processor.connect(this.actx.destination);
    return this._actualSR;
  }
  getRMS(){if(!this.analyser)return 0;const d=new Uint8Array(this.analyser.frequencyBinCount);this.analyser.getByteTimeDomainData(d);let s=0;for(const v of d)s+=(v-128)*(v-128);return Math.sqrt(s/d.length)/128}
  stop(){
    if(this.source)this.source.disconnect();if(this.processor)this.processor.disconnect();
    if(this.stream)this.stream.getTracks().forEach(t=>t.stop());if(this.actx)this.actx.close();
    const len=this.chunks.reduce((s,c)=>s+c.length,0);const merged=new Float32Array(len);let off=0;for(const c of this.chunks){merged.set(c,off);off+=c.length}
    return this._wav(merged,this._actualSR);
  }
  _wav(samples,sr){
    const buf=new ArrayBuffer(44+samples.length*2);const v=new DataView(buf);
    const ws=(o,s)=>{for(let i=0;i<s.length;i++)v.setUint8(o+i,s.charCodeAt(i))};
    ws(0,'RIFF');v.setUint32(4,36+samples.length*2,true);ws(8,'WAVE');
    ws(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
    v.setUint32(24,sr,true);v.setUint32(28,sr*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);
    ws(36,'data');v.setUint32(40,samples.length*2,true);
    let o=44;for(const s of samples){const i=Math.max(-1,Math.min(1,s));v.setInt16(o,i<0?i*0x8000:i*0x7FFF,true);o+=2}
    return new Blob([buf],{type:'audio/wav'});
  }
}

let _wavRec=null,_voiceActive=false,_lvTimer=null;

async function voiceToggle(){if(_voiceActive)voiceStop();else await voiceStart()}
async function voiceStart(){
  if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){
    el('voice-status').textContent='MIC UNAVAILABLE (need HTTPS or localhost)';return}
  try{
    _wavRec=new WavRecorder();
    const sr=await _wavRec.start();
    _voiceActive=true;
    el('voice-status').textContent='RECORDING… ('+(sr/1000).toFixed(0)+'kHz)';
    const btn=el('voice-btn');btn.textContent='⏹ STOP';btn.classList.add('recording');
    el('voice-transcript').style.display='none';el('voice-response').style.display='none';
    el('voice-level').style.display='block';
    _lvTimer=setInterval(()=>{const r=_wavRec?_wavRec.getRMS():0;el('voice-level-bar').style.width=Math.min(100,r*200)+'%'},80);
  }catch(e){
    _voiceActive=false;_wavRec=null;
    const msg=e.name==='NotAllowedError'?'MIC PERMISSION DENIED':
              e.name==='NotFoundError'?'NO MIC FOUND':
              'MIC ERROR: '+e.message;
    el('voice-status').textContent=msg;
  }
}
function voiceStop(){
  if(!_wavRec||!_voiceActive)return;
  clearInterval(_lvTimer);el('voice-level').style.display='none';el('voice-level-bar').style.width='0%';
  let blob;
  try{blob=_wavRec.stop()}catch(e){el('voice-status').textContent='STOP ERROR: '+e;return}
  _wavRec=null;_voiceActive=false;
  const btn=el('voice-btn');btn.textContent='🎤 SPEAK';btn.classList.remove('recording');btn.disabled=true;
  el('voice-status').textContent='TRANSCRIBING…';
  voiceSend(blob);
}
async function voiceSend(blob){
  try{
    const fd=new FormData();fd.append('audio',blob,'voice.wav');
    const r=await fetch('/api/voice/query',{method:'POST',body:fd});
    if(!r.ok){el('voice-btn').disabled=false;el('voice-status').textContent='SERVER ERROR '+r.status;return}
    const data=await r.json();
    el('voice-btn').disabled=false;
    // Always show transcript if we got one
    if(data.transcript){
      const tr=el('voice-transcript');tr.textContent='"'+data.transcript+'"';tr.style.display='block';
    }
    if(data.error){
      el('voice-status').textContent='ERR: '+data.error;
      return;
    }
    el('voice-status').textContent='MODULE: '+(data.module||'—').toUpperCase();
    const rsp=el('voice-response');rsp.textContent=data.response||'';rsp.style.display=data.response?'block':'none';
  }catch(e){el('voice-btn').disabled=false;el('voice-status').textContent='FETCH ERROR: '+e.message}
}

// ── Mini-todo (from API) ───────────────────────────────────────────────────────
async function loadMiniTodo(){
  try{
    const r=await fetch('/api/todos');const tasks=await r.json();
    renderMiniTodo(tasks);
  }catch(e){
    // fallback: try localStorage
    try{renderMiniTodo(JSON.parse(localStorage.getItem('gk_tasks')||'[]'))}catch(_){}
  }
}
function renderMiniTodo(tasks){
  const list=el('todo-mini-list');
  const active=tasks.filter(t=>!t.done&&(t.quad==='q1'||t.quad==='q2'));
  if(!active.length){list.innerHTML='<div style="font-size:10px;letter-spacing:2px;color:#0c2535;padding:8px 0">No active tasks</div>';return}
  const q1=active.filter(t=>t.quad==='q1');
  const q2=active.filter(t=>t.quad==='q2');
  let html='';
  if(q1.length){
    html+='<div class="todo-section-lbl" style="color:var(--red)">DO FIRST</div>';
    for(const t of q1.slice(0,4))html+=`<div class="todo-item" onclick="doneTask('${t.id}')"><span class="ti-dot q1">◆</span><span class="ti-text">${t.text}</span></div>`;
  }
  if(q2.length){
    html+='<div class="todo-section-lbl" style="color:var(--amber)">SCHEDULE</div>';
    for(const t of q2.slice(0,3))html+=`<div class="todo-item" onclick="doneTask('${t.id}')"><span class="ti-dot q2">◆</span><span class="ti-text">${t.text}</span></div>`;
  }
  list.innerHTML=html;
}
async function doneTask(id){
  try{
    await fetch(`/api/todos/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({done:1})});
    loadMiniTodo();
  }catch(e){
    // fallback
    try{const tasks=JSON.parse(localStorage.getItem('gk_tasks')||'[]');const t=tasks.find(x=>x.id===id);if(t){t.done=true;localStorage.setItem('gk_tasks',JSON.stringify(tasks));loadMiniTodo()}}catch(_){}
  }
}
loadMiniTodo();
setInterval(loadMiniTodo,30*1000);

// ── Todo full modal ────────────────────────────────────────────────────────────
let _todoFull=[], _todoShowDone=false;

async function openTodoFull(){
  el('overlay').classList.add('open');
  el('modal-todo-full').classList.add('open');
  _openModal='todo';
  await _loadTodoFull();
}
function closeTodoFull(){
  el('overlay').classList.remove('open');
  el('modal-todo-full').classList.remove('open');
  _openModal=null;
}
async function _loadTodoFull(){
  try{
    const r=await fetch('/api/todos?include_done='+(_todoShowDone?'true':'false'));
    _todoFull=await r.json();
    _renderTodoFull();
  }catch(e){console.error('todo load failed',e)}
}
function _renderTodoFull(){
  const quads=['q1','q2','q3','q4'];
  for(const q of quads){
    const tasks=_todoFull.filter(t=>t.quad===q);
    const list=el('mqlist-'+q);
    el('mqcnt-'+q).textContent=tasks.filter(t=>!t.done).length||'';
    if(!tasks.length){list.innerHTML='<div class="modal-quad-empty">— empty —</div>';continue}
    list.innerHTML=tasks.map(t=>{
      const hasVerifier=!!t.verifier;
      const verifyFailed=t.verified===-1;
      const verifyOk=t.verified===1;
      const _esc=s=>(s||'').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
      const verifyBadge=hasVerifier
        ? (verifyFailed
            ? `<span class="tq-verify-badge fail" data-tooltip="BLOCKED — cannot mark done\n${_esc(t.verify_note||'verification failed')}">⚠ BLOCKED</span>`
            : verifyOk
              ? `<span class="tq-verify-badge ok" data-tooltip="Verified ✓\n${_esc(t.verify_note||'check passed')}">✓</span>`
              : `<span class="tq-verify-badge pending" data-tooltip="Auto-verified task\nNot checked yet — will run within 10 min">⟳</span>`)
        : '';
      return `<div class="modal-task-item${t.done?' done':''}${verifyFailed?' verify-fail':''}" draggable="true" ondragstart="tqDragStart(event,'${t.id}')" ondragend="tqDragEnd(event)" onclick="modalToggleTask('${t.id}',${t.done?0:1})">
        <input type="checkbox" class="modal-task-cb" ${t.done?'checked':''} onclick="event.stopPropagation()" onchange="modalToggleTask('${t.id}',this.checked)">
        <span class="modal-task-text">${(t.text||'').replace(/</g,'&lt;')}</span>
        ${verifyBadge}
        <button class="modal-task-del" onclick="event.stopPropagation();modalDeleteTask('${t.id}')">×</button>
      </div>`;
    }).join('');
  }
}
// ── Todo quadrant drag-and-drop ───────────────────────────────────────────────
let _tqDragId=null;
function tqDragStart(e,id){
  _tqDragId=id;
  e.dataTransfer.effectAllowed='move';
  e.dataTransfer.setData('text/plain',id);
  setTimeout(()=>{const el=e.target.closest('.modal-task-item');if(el)el.classList.add('tq-dragging')},0);
}
function tqDragEnd(e){
  _tqDragId=null;
  document.querySelectorAll('.modal-task-item.tq-dragging').forEach(el=>el.classList.remove('tq-dragging'));
  document.querySelectorAll('.modal-quad-tasks.tq-over').forEach(el=>el.classList.remove('tq-over'));
}
function tqOver(e){e.preventDefault();e.dataTransfer.dropEffect='move';}
function tqEnter(e){e.currentTarget.classList.add('tq-over');}
function tqLeave(e){if(!e.currentTarget.contains(e.relatedTarget))e.currentTarget.classList.remove('tq-over');}
async function tqDrop(e,quad){
  e.preventDefault();
  e.currentTarget.classList.remove('tq-over');
  const id=_tqDragId||e.dataTransfer.getData('text/plain');
  if(!id)return;
  _tqDragId=null;
  await fetch(`/api/todos/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({quad})});
  await _loadTodoFull();
  loadMiniTodo();
}

async function modalToggleTask(id,done){
  const r=await fetch(`/api/todos/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({done:done?1:0})});
  const d=await r.json();
  if(d.blocked){
    // Verifier blocked the done mark — show the reason inline
    const item=document.querySelector(`[ondragstart*="${id}"]`);
    if(item){
      item.classList.add('verify-fail');
      let tip=item.querySelector('.tq-verify-toast');
      if(!tip){tip=document.createElement('div');tip.className='tq-verify-toast';item.appendChild(tip);}
      tip.textContent='⚠ '+d.reason;
      setTimeout(()=>{if(tip&&tip.parentNode)tip.parentNode.removeChild(tip);item.classList.remove('verify-fail');},4000);
    }
  }
  await _loadTodoFull();
  loadMiniTodo();
}
async function modalDeleteTask(id){
  await fetch(`/api/todos/${id}`,{method:'DELETE'});
  await _loadTodoFull();
  loadMiniTodo();
}
async function modalAddTask(){
  const inp=el('modal-todo-input'),text=inp.value.trim();
  if(!text)return;
  const quad=el('modal-todo-quad').value;
  await fetch('/api/todos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,quad})});
  inp.value='';
  await _loadTodoFull();
  loadMiniTodo();
}
function modalToggleDone(){
  _todoShowDone=!_todoShowDone;
  const btn=document.querySelector('.modal-todo-btn');
  btn.textContent=_todoShowDone?'HIDE DONE':'SHOW DONE';
  btn.classList.toggle('active',_todoShowDone);
  _loadTodoFull();
}
async function modalClearDone(){
  const done=_todoFull.filter(t=>t.done);
  await Promise.all(done.map(t=>fetch(`/api/todos/${t.id}`,{method:'DELETE'})));
  await _loadTodoFull();
  loadMiniTodo();
}

// ── Panel layout — movable / resizable / transparent ──────────────────────────
const _PL={};          // {panelId: {x,y,w,h,z}}
let _maxZ=20, _drag=null, _dragMoved=false;
const _PMIN_W=160, _PMIN_H=80;
const _LSKEY='gk_panel_layout_v2';

function _defaultLayout(){
  const W=window.innerWidth, H=window.innerHeight-44;
  const g=8, cw=Math.floor((W-g*5)/4);
  const r1=150, r2=145, r3=Math.max(100,H-r1-r2-g*4);
  return{
    'p-network':  {x:g,        y:g,          w:cw,       h:r1,  z:10},
    'p-system':   {x:g*2+cw,   y:g,          w:cw,       h:r1,  z:10},
    'p-weather':  {x:g*3+cw*2, y:g,          w:cw,       h:r1,  z:10},
    'p-guardian': {x:g*4+cw*3, y:g,          w:cw,       h:r1,  z:10},
    'p-diary':    {x:g,        y:g*2+r1,     w:cw*3+g*2, h:r2,  z:10},
    'p-todo-mini':{x:g*4+cw*3, y:g*2+r1,     w:cw,       h:r2,  z:10},
    'p-news':     {x:g,        y:g*3+r1+r2,  w:cw*3+g*2, h:r3,  z:10},
    'p-voice':    {x:g*4+cw*3, y:g*3+r1+r2,  w:cw,       h:r3,  z:10},
  };
}

function _applyPanel(id){
  const p=_PL[id]; if(!p)return;
  const d=document.getElementById(id); if(!d)return;
  d.style.left=p.x+'px'; d.style.top=p.y+'px';
  d.style.width=p.w+'px'; d.style.height=p.h+'px';
  d.style.zIndex=p.z;
}

function _bringToFront(id){
  if(!_PL[id])return;
  _maxZ++;
  _PL[id].z=_maxZ;
  const d=document.getElementById(id); if(d)d.style.zIndex=_maxZ;
}

function _savePanelLayout(){
  try{localStorage.setItem(_LSKEY,JSON.stringify(_PL))}catch(e){}
}

function _initPanelLayout(){
  const defs=_defaultLayout();
  let saved={};
  try{saved=JSON.parse(localStorage.getItem(_LSKEY)||'{}')}catch(e){}
  for(const id of Object.keys(defs)){
    const s=saved[id];
    _PL[id]=s&&typeof s.x==='number'?{x:s.x,y:s.y,w:Math.max(_PMIN_W,s.w),h:Math.max(_PMIN_H,s.h),z:s.z||10}:{...defs[id]};
    _applyPanel(id);
  }
  _maxZ=Math.max(20,...Object.values(_PL).map(p=>p.z||10));
}

function resetPanelLayout(){
  localStorage.removeItem(_LSKEY);
  const defs=_defaultLayout();
  for(const id of Object.keys(defs)){_PL[id]={...defs[id]};_applyPanel(id);}
  _savePanelLayout();
}

// Pointer events — drag (from label) and resize (from grip)
document.addEventListener('mousedown',e=>{
  const resizeEl=e.target.closest('.panel-resize');
  const labelEl =e.target.closest('.panel-label');
  const panelEl =e.target.closest('.panel[id]');
  if(!panelEl)return;
  const id=panelEl.id; if(!_PL[id])return;
  _bringToFront(id); _dragMoved=false;
  if(resizeEl){
    e.preventDefault();
    _drag={type:'resize',id,startX:e.clientX,startY:e.clientY,origW:_PL[id].w,origH:_PL[id].h};
  }else if(labelEl&&!e.target.closest('button')&&!e.target.closest('input')&&!e.target.closest('a')&&!e.target.closest('select')&&!e.target.closest('span[onclick]')){
    _drag={type:'move',id,startX:e.clientX,startY:e.clientY,origX:_PL[id].x,origY:_PL[id].y};
  }
},true);

document.addEventListener('mousemove',e=>{
  if(!_drag)return;
  const dx=e.clientX-_drag.startX, dy=e.clientY-_drag.startY;
  if(!_dragMoved&&Math.abs(dx)<4&&Math.abs(dy)<4)return;
  _dragMoved=true;
  const p=_PL[_drag.id];
  if(_drag.type==='move'){
    p.x=Math.max(0,_drag.origX+dx);
    p.y=Math.max(0,_drag.origY+dy);
  }else{
    p.w=Math.max(_PMIN_W,_drag.origW+dx);
    p.h=Math.max(_PMIN_H,_drag.origH+dy);
  }
  _applyPanel(_drag.id);
});

document.addEventListener('mouseup',e=>{
  if(_drag&&_dragMoved)_savePanelLayout();
  _drag=null;
});

// Cancel click only on panel elements after a drag — lets modal/todo clicks through
document.addEventListener('click',e=>{
  if(_dragMoved){
    if(e.target.closest('.panel[id]')){e.stopPropagation();e.preventDefault();}
    _dragMoved=false;
  }
},true);

_initPanelLayout();

// ── Tour ───────────────────────────────────────────────────────────────────────
const TOUR=[
  {id:null,title:'WELCOME',text:'GK Personal Assistant — your private AI on your own hardware.\nAll panels update live every 2 seconds via WebSocket.\nAll your data stays on your machine.'},
  {id:'p-network',title:'NETWORK MONITOR',text:'SVG icons show connection type: WiFi arcs=wireless, port+pins=LAN, padlock=VPN. IP/interface name alternates every 2.5s.\nSpeed shown live in panel header. Colors: cyan=WiFi, green=LAN, amber=VPN.'},
  {id:'p-system',title:'SYSTEM RESOURCES',text:'4-level color coding:\n  GREEN  = 0–30%  (healthy)\n  CYAN   = 30–60% (normal)\n  AMBER  = 60–85% (watch)\n  RED    = 85%+   (critical)\nClick for full details.'},
  {id:'p-weather',title:'WEATHER WIDGET',text:'Live weather from Open-Meteo for Barloni farm.\nClick for 7-day forecast with rainfall probability.'},
  {id:'p-guardian',title:'SECURITY GUARDIAN',text:'Blink speed = severity:\n  RED   = fast  (0.7s)  — danger\n  AMBER = medium (3s)  — warn\n  GREEN = slow  (5s)   — ok\nCode audit: bandit scans codebase weekly. 0 issues found.\nCLICK → Details shows upgrade button for packages.'},
  {id:'p-diary',title:'DIARY',text:'Auto-processes ~/Pictures at startup.\nPress Ctrl+D or click ✎ FROM PHOTOS to trigger manually.\nAmber blinking = pending review questions (who\'s in this photo? where was this?).\nAnswers enrich future diary entries. Footer shows processed count.'},
  {id:'p-todo-mini',title:'TODO · QUICK VIEW',text:'Q1 (DO FIRST) items blink red slowly.\nQ2 (SCHEDULE) items blink amber.\nClick any task to mark it done.\nFULL → opens the complete Eisenhower matrix.'},
  {id:'p-news',title:'FARMING NEWS',text:'Auto-refreshes every 2.5 min from trusted sources.\nFilters out biased/sensationalist media.\nNews scrolls up automatically.\nCLICK any headline for full article + YouTube video.'},
  {id:'p-voice',title:'VOICE QUERY',text:'Click SPEAK → talk → click STOP.\nCaptures WAV directly in browser (no ffmpeg needed).\nTranscribed and routed to the right module.\nWorks from phone via Tailscale: http://100.67.193.127:8765'},
  {id:null,title:'TEXT INPUT (CLI)',text:'python main.py\n\n"show my farm weather"\n"log expense 500 groceries"\n"log BP 120/80"\n"mandi price pomegranate"\n"write diary from my photos"'},
  {id:null,title:'WHAT\'S NEXT',text:'• GodsView AI satellite integration\n• Code directory analyzer module\n• Weekly email digest\n• NDVI crop health from satellite\n\nGo to /todo to see full priority list.'},
];
let _tourIdx=0;
function tourStart(){_tourIdx=0;_tourShow()}
function tourNext(){_tourIdx++;if(_tourIdx>=TOUR.length){tourSkip();return}_tourShow()}
function tourSkip(){el('tour-highlight').style.display='none';el('tour-card').style.display='none'}
function _tourShow(){
  const step=TOUR[_tourIdx];
  el('tour-step-lbl').textContent=`STEP ${_tourIdx+1} / ${TOUR.length}`;
  el('tour-title').textContent=step.title;
  el('tour-text').textContent=step.text;
  el('tour-card').querySelector('.tour-btn.primary').textContent=_tourIdx===TOUR.length-1?'FINISH ✓':'NEXT →';
  const hl=el('tour-highlight'),tc=el('tour-card');
  if(step.id){
    const target=document.getElementById(step.id);
    if(target){
      const r=target.getBoundingClientRect(),pad=6;
      hl.style.cssText=`display:block;top:${r.top-pad}px;left:${r.left-pad}px;width:${r.width+pad*2}px;height:${r.height+pad*2}px;`;
      const top=Math.min(r.bottom+pad+12,window.innerHeight-220),left=Math.max(10,Math.min(r.left,window.innerWidth-380));
      tc.style.cssText=`display:block;top:${top}px;left:${left}px;`;
    }
  }else{hl.style.display='none';tc.style.cssText='display:block;top:50%;left:50%;transform:translate(-50%,-50%)'}
}

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

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

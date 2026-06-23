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


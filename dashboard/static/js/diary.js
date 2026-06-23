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

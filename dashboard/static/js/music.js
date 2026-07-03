// Music panel — plays the local collection (config.MUSIC_DIR) and shows two
// discovery rows: TRENDING NOW (mainstream) and NEW FOR YOU (his taste).
// Local tracks are playable in-page; discovery items open a YouTube search.

let _mtracks = [];      // [{id,title,artist,rel}]
let _mcur = -1;         // index into _mtracks currently loaded
let _mplaying = false;

function _mesc(s){return (s||'').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function _maudio(){return el('music-audio')}
function _mlabel(t){return t.artist ? `${t.title} — ${t.artist}` : t.title}

async function loadMusicLibrary(){
  try{
    const r = await fetch('/api/music/library');
    const d = await r.json();
    _mtracks = d.tracks || [];
    renderMusicPlaylist(d);
  }catch(e){ el('music-footer').textContent = 'library unavailable'; }
}

function renderMusicPlaylist(d){
  const list = el('music-playlist');
  el('music-footer').textContent = `${_mtracks.length} in collection`;
  if(!_mtracks.length){
    list.innerHTML = `<div class="music-empty">No songs yet — drop audio files into<br><code>${_mesc(d.dir||'~/Music/gk')}</code></div>`;
    return;
  }
  list.innerHTML = _mtracks.map((t,i)=>`
    <div class="music-track ${i===_mcur?'active':''}" id="mtrack-${i}" onclick="musicPlay(${i})" title="Play">
      <span class="music-track-ico">${i===_mcur&&_mplaying?'❚❚':'▶'}</span>
      <span class="music-track-name">${_mesc(_mlabel(t))}</span>
    </div>`).join('');
}

let _mTrendTries = 0;
async function loadMusicTrending(){
  try{
    const r = await fetch('/api/music/trending');
    const d = await r.json();
    renderMusicRow('music-trending', d.trending);
    renderMusicRow('music-new', d.new_for_you);
    // The server fills empty rows in the background — re-poll a few times soon so
    // freshly-fetched discovery appears without waiting for the 15-min cycle.
    const incomplete = !(d.trending||[]).length || !(d.new_for_you||[]).length;
    if(incomplete && _mTrendTries < 6){ _mTrendTries++; setTimeout(loadMusicTrending, 30000); }
    else { _mTrendTries = 0; }
  }catch(e){
    renderMusicRow('music-trending', []);
    renderMusicRow('music-new', []);
  }
}

// Discovery items are names (not local files) → clicking searches YouTube.
function renderMusicRow(id, items){
  const box = el(id);
  if(!box) return;
  items = items || [];
  if(!items.length){ box.innerHTML = '<div class="music-empty">—</div>'; return; }
  box.innerHTML = items.map(it=>{
    const label = it.artist ? `${it.title} · ${it.artist}` : it.title;
    const q = encodeURIComponent(`${it.title} ${it.artist||''}`.trim());
    return `<div class="music-chip" onclick="window.open('https://www.youtube.com/results?search_query=${q}','_blank','noopener')" title="Search on YouTube">${_mesc(label)}</div>`;
  }).join('');
}

function musicPlay(i){
  if(i<0 || i>=_mtracks.length) return;
  const a = _maudio();
  if(i !== _mcur){
    _mcur = i;
    a.src = `/api/music/file/${_mtracks[i].id}`;
  }
  a.play().then(()=>{ _mplaying = true; _msync(); })
          .catch(()=>{ _mplaying = false; _msync(); });
}

function musicToggle(){
  const a = _maudio();
  if(_mcur === -1){ musicPlay(0); return; }
  if(a.paused){ a.play(); _mplaying = true; } else { a.pause(); _mplaying = false; }
  _msync();
}

function musicNext(){ if(_mtracks.length) musicPlay((_mcur+1) % _mtracks.length); }
function musicPrev(){ if(_mtracks.length) musicPlay((_mcur-1+_mtracks.length) % _mtracks.length); }

// Reflect current state in the play button, now-playing label and track rows.
function _msync(){
  const btn = el('music-play'); if(btn) btn.textContent = _mplaying ? '❚❚' : '▶';
  const now = el('music-now');
  if(now) now.textContent = _mcur>=0 ? _mlabel(_mtracks[_mcur]) : '—';
  _mtracks.forEach((t,i)=>{
    const row = el(`mtrack-${i}`); if(!row) return;
    row.classList.toggle('active', i===_mcur);
    const ico = row.querySelector('.music-track-ico');
    if(ico) ico.textContent = (i===_mcur && _mplaying) ? '❚❚' : '▶';
  });
}

function refreshMusic(){ loadMusicLibrary(); loadMusicTrending(); }

function musicInit(){
  const a = _maudio(); if(!a) return;
  a.addEventListener('ended', musicNext);
  a.addEventListener('pause', ()=>{ _mplaying=false; _msync(); });
  a.addEventListener('play',  ()=>{ _mplaying=true;  _msync(); });
  loadMusicLibrary();
  loadMusicTrending();
  setInterval(loadMusicTrending, 15*60*1000);  // refresh discovery every 15 min
}

document.addEventListener('DOMContentLoaded', musicInit);

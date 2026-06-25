/* ── Live TV news grid — lazy-loaded (one stream at a time), draggable box ──── */
let _ytApiReady=false, _ytApiLoading=false;
// Only the *active* channel has a loaded iframe → only one channel ever streams,
// so idle channels consume no data.
const _liveTV={player:null, active:null, channels:[], paused:false, muted:false};

// Load the YouTube IFrame API once; resolve when YT.Player is available.
function _loadYTApi(){
  return new Promise((resolve)=>{
    if(_ytApiReady) return resolve();
    const prev=window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady=()=>{_ytApiReady=true;if(prev)prev();resolve();};
    if(_ytApiLoading) return;
    _ytApiLoading=true;
    const s=document.createElement('script'); s.src='https://www.youtube.com/iframe_api';
    document.head.appendChild(s);
  });
}

function _placeholderHTML(ch){
  return '<div class="livetv-ph">'+
    '<span class="livetv-tile-live">● LIVE</span>'+
    '<div class="livetv-ph-name">'+ch.name+'</div>'+
    '<div class="livetv-ph-play">▶ PLAY</div></div>';
}

// Open the box and render channel placeholders — no iframes yet, no data used.
async function openLiveTV(){
  el('modal-livetv-overlay').classList.add('open');
  if(typeof _stopNewsScroll==='function')_stopNewsScroll();
  const grid=el('livetv-grid');
  if(_liveTV.channels.length){return;}            // placeholders already built
  grid.innerHTML='<div style="grid-column:1/-1;font-size:10px;letter-spacing:2px;color:var(--dim);padding:20px">loading channels…</div>';
  let data;
  try{ data=await (await fetch('/api/news/channels')).json(); }
  catch(e){ grid.innerHTML='<div style="grid-column:1/-1;color:var(--red);font-size:10px;padding:20px">failed to load channels</div>'; return; }
  _liveTV.channels=data.channels||[];
  grid.innerHTML='';
  _liveTV.channels.forEach(ch=>{
    const tile=document.createElement('div');
    tile.className='livetv-tile placeholder'; tile.id='tv-tile-'+ch.id;
    tile.onclick=()=>liveTVPlay(ch.id);
    tile.innerHTML=_placeholderHTML(ch);
    grid.appendChild(tile);
  });
}

// Load + play one channel, unloading whatever was playing (one stream at a time).
async function liveTVPlay(id){
  if(_liveTV.active===id) return;
  _unloadActive();
  const ch=_liveTV.channels.find(c=>c.id===id); if(!ch) return;
  const tile=el('tv-tile-'+id); if(!tile) return;
  tile.classList.remove('placeholder'); tile.classList.add('focused');
  tile.innerHTML='<iframe id="tv-frame-'+id+'" src="'+ch.embed_url+'" '+
    'allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe>'+
    '<div class="livetv-tile-bar"><span class="livetv-tile-live">● LIVE</span>'+
    '<span>'+ch.name+'</span><span class="livetv-tile-mute" id="tv-mute-'+id+'">🔊</span></div>';
  _liveTV.active=id; _liveTV.paused=false; _liveTV.muted=false;
  await _loadYTApi();
  _liveTV.player=new YT.Player('tv-frame-'+id,{
    events:{ onReady:(e)=>{ e.target.unMute(); e.target.setVolume(100); e.target.playVideo(); } }
  });
  el('livetv-focus-name').textContent='· '+ch.name;
  el('livetv-controls').style.display='flex';
  el('livetv-offset').textContent='';
  el('livetv-playpause').textContent='⏸ PAUSE';
  el('livetv-mute').textContent='🔇 MUTE';
}

// Destroy the active player + revert its tile to a placeholder → stops the stream.
function _unloadActive(){
  if(!_liveTV.active) return;
  const id=_liveTV.active;
  try{ if(_liveTV.player&&_liveTV.player.destroy) _liveTV.player.destroy(); }catch(e){}
  const tile=el('tv-tile-'+id);
  if(tile){
    tile.classList.add('placeholder'); tile.classList.remove('focused');
    const ch=_liveTV.channels.find(c=>c.id===id);
    tile.innerHTML=_placeholderHTML(ch||{name:id});
    tile.onclick=()=>liveTVPlay(id);
  }
  _liveTV.player=null; _liveTV.active=null;
}

function _activePlayer(){ return _liveTV.player; }

function liveTVPlayPause(){
  const p=_activePlayer(); if(!p) return;
  _liveTV.paused=!_liveTV.paused;
  if(_liveTV.paused){ p.pauseVideo(); el('livetv-playpause').textContent='▶ PLAY'; }
  else { p.playVideo(); el('livetv-playpause').textContent='⏸ PAUSE'; }
}

function liveTVToggleMute(){
  const p=_activePlayer(); if(!p||!_liveTV.active) return;
  _liveTV.muted=!_liveTV.muted;
  const mi=el('tv-mute-'+_liveTV.active);
  if(_liveTV.muted){ p.mute(); el('livetv-mute').textContent='🔊 UNMUTE'; if(mi)mi.textContent='🔇'; }
  else { p.unMute(); p.setVolume(100); el('livetv-mute').textContent='🔇 MUTE'; if(mi)mi.textContent='🔊'; }
}

// Rewind `secs` into the live DVR buffer; secs=0 jumps back to the live edge.
function liveTVSeek(secs){
  const p=_activePlayer();
  if(!p){ alert('Click a channel to play it first.'); return; }
  let dur=0; try{ dur=p.getDuration()||0; }catch(e){}
  if(secs===0){ if(dur>0)p.seekTo(dur,true); el('livetv-offset').textContent=''; el('livetv-playpause').textContent='⏸ PAUSE'; _liveTV.paused=false; return; }
  if(dur<=1){ el('livetv-offset').textContent='(no DVR buffer on this channel)'; return; }
  const target=Math.max(0,dur-secs); p.seekTo(target,true); p.playVideo();
  _liveTV.paused=false; el('livetv-playpause').textContent='⏸ PAUSE';
  const have=Math.round((dur-target)/60);
  el('livetv-offset').textContent='−'+have+' min'+(have<Math.round(secs/60)?' (max DVR)':'');
}

function closeLiveTV(){
  el('modal-livetv-overlay').classList.remove('open');
  _unloadActive();                       // stop the stream → no more data used
  el('livetv-controls').style.display='none';
  el('livetv-focus-name').textContent='';
}

// ── Drag the live-TV box around the screen by its header ─────────────────────
let _ltvDrag=null;
function _ltvDragStart(e){
  if(e.target.closest('button, .news-modal-close, #livetv-controls')) return;
  const box=el('modal-livetv-box'); const r=box.getBoundingClientRect();
  box.style.position='absolute'; box.style.margin='0';
  box.style.left=r.left+'px'; box.style.top=r.top+'px';
  _ltvDrag={dx:e.clientX-r.left, dy:e.clientY-r.top};
  e.preventDefault();
}
document.addEventListener('mousedown',e=>{
  const hdr=e.target.closest('.livetv-header');
  if(hdr && el('modal-livetv-overlay') && el('modal-livetv-overlay').classList.contains('open')) _ltvDragStart(e);
});
document.addEventListener('mousemove',e=>{
  if(!_ltvDrag) return;
  const box=el('modal-livetv-box');
  box.style.left=Math.max(0,e.clientX-_ltvDrag.dx)+'px';
  box.style.top=Math.max(0,e.clientY-_ltvDrag.dy)+'px';
});
document.addEventListener('mouseup',()=>{_ltvDrag=null;});

function videoFullscreen(){
  const fr=el('news-video-frame');
  (fr.requestFullscreen||fr.webkitRequestFullscreen||fr.mozRequestFullScreen||function(){}).call(fr);
}

// ── Voice (WAV capture, no ffmpeg) ─────────────────────────────────────────────

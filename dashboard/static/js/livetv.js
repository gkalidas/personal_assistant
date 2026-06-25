/* ── Live TV news grid — all channels at lowest resolution, draggable box ──── */
let _ytApiReady=false, _ytApiLoading=false;
// All channels play at once (muted) at the lowest resolution to bound data use;
// closing the box destroys every player so nothing streams while hidden.
const _liveTV={players:{}, channels:[], focus:null, paused:false, muted:true, built:false};

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

// Pin a player to the lowest resolution (fights YouTube's auto-upscaling).
function _forceLow(p){ try{ p.setPlaybackQuality('tiny'); }catch(e){} }

async function openLiveTV(){
  el('modal-livetv-overlay').classList.add('open');
  if(typeof _stopNewsScroll==='function')_stopNewsScroll();
  if(_liveTV.built) return;
  const grid=el('livetv-grid');
  if(!_liveTV.channels.length){
    grid.innerHTML='<div style="grid-column:1/-1;font-size:10px;letter-spacing:2px;color:var(--dim);padding:20px">loading channels…</div>';
    try{ _liveTV.channels=((await (await fetch('/api/news/channels')).json()).channels)||[]; }
    catch(e){ grid.innerHTML='<div style="grid-column:1/-1;color:var(--red);font-size:10px;padding:20px">failed to load channels</div>'; return; }
  }
  _buildGrid();
}

// Build every tile + iframe and start all streams muted at lowest quality.
function _buildGrid(){
  const grid=el('livetv-grid'); grid.innerHTML='';
  _liveTV.channels.forEach(ch=>{
    const tile=document.createElement('div');
    tile.className='livetv-tile'; tile.id='tv-tile-'+ch.id;
    tile.onclick=()=>liveTVFocus(ch.id);
    tile.innerHTML='<iframe id="tv-frame-'+ch.id+'" src="'+ch.embed_url+'" '+
      'allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe>'+
      '<div class="livetv-tile-bar"><span class="livetv-tile-live">● LIVE</span>'+
      '<span>'+ch.name+'</span><span class="livetv-tile-mute" id="tv-mute-'+ch.id+'">🔇</span></div>';
    grid.appendChild(tile);
  });
  _loadYTApi().then(()=>{
    _liveTV.channels.forEach(ch=>{
      _liveTV.players[ch.id]=new YT.Player('tv-frame-'+ch.id,{
        events:{
          onReady:(e)=>{ e.target.mute(); _forceLow(e.target); e.target.playVideo(); },
          onPlaybackQualityChange:(e)=>_forceLow(e.target),
        }
      });
    });
  });
  _liveTV.built=true;
}

// Click a channel → unmute it (mute the rest); resolution stays low for all.
function liveTVFocus(id){
  _liveTV.focus=id; _liveTV.paused=false; _liveTV.muted=false;
  _liveTV.channels.forEach(ch=>{
    const p=_liveTV.players[ch.id], tile=el('tv-tile-'+ch.id), mi=el('tv-mute-'+ch.id);
    const on=(ch.id===id);
    if(tile)tile.classList.toggle('focused',on);
    if(p&&p.mute){ if(on){p.unMute();p.setVolume(100);} else {p.mute();} }
    if(mi) mi.textContent=on?'🔊':'🔇';
  });
  const f=_liveTV.channels.find(c=>c.id===id);
  el('livetv-focus-name').textContent=f?('· '+f.name):'';
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
  if(_liveTV.muted){ p.mute(); el('livetv-mute').textContent='🔊 UNMUTE'; if(mi)mi.textContent='🔇'; }
  else { p.unMute(); p.setVolume(100); el('livetv-mute').textContent='🔊 MUTE'; if(mi)mi.textContent='🔊'; }
}

// Rewind `secs` into the focused channel's live DVR buffer; secs=0 → live edge.
function liveTVSeek(secs){
  const p=_focusPlayer();
  if(!p){ alert('Click a channel first to control playback.'); return; }
  let dur=0; try{ dur=p.getDuration()||0; }catch(e){}
  if(secs===0){ if(dur>0)p.seekTo(dur,true); el('livetv-offset').textContent=''; el('livetv-playpause').textContent='⏸ PAUSE'; _liveTV.paused=false; return; }
  if(dur<=1){ el('livetv-offset').textContent='(no DVR buffer on this channel)'; return; }
  const target=Math.max(0,dur-secs); p.seekTo(target,true); p.playVideo();
  _liveTV.paused=false; el('livetv-playpause').textContent='⏸ PAUSE';
  const have=Math.round((dur-target)/60);
  el('livetv-offset').textContent='−'+have+' min'+(have<Math.round(secs/60)?' (max DVR)':'');
}

// Closing destroys every player → all streams stop, no data while hidden.
function closeLiveTV(){
  el('modal-livetv-overlay').classList.remove('open');
  Object.values(_liveTV.players).forEach(p=>{ try{ if(p&&p.destroy)p.destroy(); }catch(e){} });
  _liveTV.players={}; _liveTV.focus=null; _liveTV.built=false;
  el('livetv-grid').innerHTML='';
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

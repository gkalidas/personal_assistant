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

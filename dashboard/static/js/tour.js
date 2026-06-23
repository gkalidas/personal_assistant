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

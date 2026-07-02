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
    const btn=el('voice-btn');btn.textContent='⏹';btn.title='Stop recording';btn.classList.add('recording');
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
  const btn=el('voice-btn');btn.textContent='🎤';btn.title='Click to speak';btn.classList.remove('recording');btn.disabled=true;
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

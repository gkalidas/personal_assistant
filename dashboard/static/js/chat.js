// Text chat with the assistant — streams from /api/query/stream (SSE), same
// router/modules as voice; tokens are appended live for low perceived latency.
// Multiple named conversations ("sessions"), ChatGPT/Claude-style, persisted in
// localStorage so a dashboard refresh keeps every conversation. Each session
// carries its own context, sent back to the model on each turn.

const CHAT_STORE_KEY='gk_chat_sessions';   // { active:id, sessions:[{id,title,updated,messages:[...]}] }
const CHAT_LEGACY_KEY='gk_chat_history';   // pre-session flat message array (migrated on load)
const CHAT_MSG_MAX=200;                     // messages kept per session
// How many prior messages (user+GK) to send back as conversational context.
// Kept small so the local model's prompt stays fast; the server caps it again.
const CHAT_CONTEXT_TURNS=12;

let chatState={active:null,sessions:[]};

// ── persistence ────────────────────────────────────────────────────────────
function chatLoadState(){
  try{
    const raw=localStorage.getItem(CHAT_STORE_KEY);
    if(raw){
      const s=JSON.parse(raw);
      if(s&&Array.isArray(s.sessions))return s;
    }
  }catch(e){/* fall through to migration / fresh */}
  // migrate a legacy flat history into a single session, if present
  try{
    const legacy=JSON.parse(localStorage.getItem(CHAT_LEGACY_KEY)||'null');
    if(Array.isArray(legacy)&&legacy.length){
      const sess=chatBlank('Chat 1');
      sess.messages=legacy;
      localStorage.removeItem(CHAT_LEGACY_KEY);
      return {active:sess.id,sessions:[sess]};
    }
  }catch(e){/* ignore */}
  return {active:null,sessions:[]};
}

function chatPersist(){
  try{localStorage.setItem(CHAT_STORE_KEY,JSON.stringify(chatState));}
  catch(e){/* storage full or unavailable — ignore */}
}

function chatBlank(title){
  return {id:'s'+Date.now()+Math.random().toString(36).slice(2,6),
          title:title||'New chat',updated:Date.now(),messages:[]};
}

function chatActive(){
  return chatState.sessions.find(s=>s.id===chatState.active)||null;
}

// ── session actions ────────────────────────────────────────────────────────
function chatNew(){
  const sess=chatBlank('Chat '+(chatState.sessions.length+1));
  chatState.sessions.unshift(sess);
  chatState.active=sess.id;
  chatPersist();
  chatRenderSessions();
  chatRenderLog();
  const inp=el('chat-input');if(inp)inp.focus();
}

function chatSwitch(id){
  if(!chatState.sessions.some(s=>s.id===id))return;
  chatState.active=id;
  chatPersist();
  chatRenderLog();
  chatRenderSessions();
}

function chatRename(){
  const sess=chatActive();if(!sess)return;
  const name=prompt('Rename conversation:',sess.title);
  if(name===null)return;
  sess.title=name.trim()||sess.title;
  chatPersist();
  chatRenderSessions();
}

function chatDelete(){
  const sess=chatActive();if(!sess)return;
  if(!confirm('Delete "'+sess.title+'"?'))return;
  chatState.sessions=chatState.sessions.filter(s=>s.id!==sess.id);
  if(!chatState.sessions.length){
    const fresh=chatBlank('Chat 1');
    chatState.sessions.push(fresh);
    chatState.active=fresh.id;
  }else{
    chatState.active=chatState.sessions[0].id;
  }
  chatPersist();
  chatRenderSessions();
  chatRenderLog();
}

// ── rendering ──────────────────────────────────────────────────────────────
function chatRenderSessions(){
  const sel=el('chat-session-sel');
  if(!sel)return;
  sel.innerHTML='';
  chatState.sessions.forEach(s=>{
    const o=document.createElement('option');
    o.value=s.id;
    o.textContent=s.title;
    if(s.id===chatState.active)o.selected=true;
    sel.appendChild(o);
  });
}

function chatRenderLog(){
  const log=el('chat-log');
  if(!log)return;
  log.innerHTML='';
  const sess=chatActive();
  if(!sess)return;
  sess.messages.forEach(m=>{
    const row=chatMakeRow(m.cls,m.who||(m.cls==='you'?'YOU':'GK'),m.text);
    if(m.err)row.classList.add('err');
    log.appendChild(row);
  });
  log.scrollTop=log.scrollHeight;
}

function chatMakeRow(cls,who,text){
  const row=document.createElement('div');
  row.className='chat-msg '+cls;
  const w=document.createElement('div');w.className='chat-who';w.textContent=who;
  const t=document.createElement('div');t.className='chat-text';t.textContent=text;
  row.appendChild(w);row.appendChild(t);
  // Assistant replies get a small "copy to clipboard" icon after the text.
  if(cls==='gk'){
    const btn=document.createElement('button');
    btn.className='chat-copy';
    btn.type='button';
    btn.title='Copy to clipboard';
    btn.setAttribute('aria-label','Copy to clipboard');
    btn.textContent='⧉';
    btn.addEventListener('click',()=>chatCopy(btn,t.textContent));
    row.appendChild(btn);
    chatCopyToggle(row,text);
  }
  return row;
}

// Copy an assistant reply's text to the clipboard, with a brief "copied" cue.
function chatCopy(btn,text){
  const done=()=>{
    btn.classList.add('copied');
    const prev=btn.textContent;
    btn.textContent='✓';
    setTimeout(()=>{btn.textContent=prev;btn.classList.remove('copied');},1200);
  };
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(done).catch(()=>chatCopyFallback(text,done));
  }else{
    chatCopyFallback(text,done);
  }
}

// Fallback for non-secure contexts where navigator.clipboard is unavailable.
function chatCopyFallback(text,done){
  try{
    const ta=document.createElement('textarea');
    ta.value=text;ta.style.position='fixed';ta.style.opacity='0';
    document.body.appendChild(ta);ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    done();
  }catch(e){/* clipboard unavailable — ignore */}
}

// Hide the copy icon while a reply is empty or still the "…" placeholder.
function chatCopyToggle(row,text){
  const btn=row.querySelector('.chat-copy');
  if(!btn)return;
  const show=!!text&&text!=='…'&&!row.classList.contains('err');
  btn.style.display=show?'':'none';
}

// Append a DOM row to the visible log. Persistence is handled separately so the
// in-flight "…" placeholder is never saved.
function chatAppend(cls,who,text){
  const log=el('chat-log');
  const row=chatMakeRow(cls,who,text);
  log.appendChild(row);
  log.scrollTop=log.scrollHeight;
  return row;
}

// Snapshot the visible log into the active session and persist.
function chatSave(){
  const sess=chatActive();
  if(!sess)return;
  const log=el('chat-log');
  if(!log)return;
  const msgs=[];
  log.querySelectorAll('.chat-msg').forEach(row=>{
    const txt=(row.querySelector('.chat-text')||{}).textContent||'';
    if(txt==='…')return; // skip in-flight placeholder
    msgs.push({
      cls:row.classList.contains('you')?'you':'gk',
      err:row.classList.contains('err'),
      who:(row.querySelector('.chat-who')||{}).textContent||'',
      text:txt
    });
  });
  sess.messages=msgs.slice(-CHAT_MSG_MAX);
  sess.updated=Date.now();
  chatPersist();
}

// Collect recent messages as [{role,content}] for the assistant's context.
// Skips in-flight placeholders and error rows so they never pollute the prompt.
function chatHistory(){
  const log=el('chat-log');
  if(!log)return [];
  const out=[];
  log.querySelectorAll('.chat-msg').forEach(row=>{
    const txt=(row.querySelector('.chat-text')||{}).textContent||'';
    if(!txt||txt==='…')return;
    if(row.classList.contains('err'))return;
    out.push({role:row.classList.contains('you')?'user':'assistant',content:txt});
  });
  return out.slice(-CHAT_CONTEXT_TURNS);
}

// Give a fresh session a title from its first user message.
function chatAutoTitle(sess,text){
  if(!sess||!text)return;
  if(!/^chat\s+\d+$|^new chat$/i.test(sess.title))return; // only rename untouched defaults
  if(sess.messages.some(m=>m.cls==='you'))return;         // only on the first user turn
  sess.title=text.length>28?text.slice(0,28)+'…':text;
  chatRenderSessions();
}

// ── send ───────────────────────────────────────────────────────────────────
async function chatSend(){
  const inp=el('chat-input');
  const text=(inp.value||'').trim();
  if(!text)return;
  inp.value='';
  let sess=chatActive();
  if(!sess){chatNew();sess=chatActive();}
  chatAutoTitle(sess,text);
  const history=chatHistory();          // capture BEFORE appending the new turn
  chatAppend('you','YOU',text);
  chatSave();
  const pending=chatAppend('gk','GK','…');
  const txt=pending.querySelector('.chat-text');
  const who=pending.querySelector('.chat-who');
  try{
    const r=await fetch('/api/query/stream',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,history})
    });
    if(!r.ok||!r.body){
      pending.classList.add('err');
      txt.textContent='ERR: server '+r.status;
      chatSave();
      return;
    }
    // Read the SSE stream: append tokens as they arrive so the reply appears
    // word-by-word instead of after the whole generation completes.
    const reader=r.body.getReader();
    const dec=new TextDecoder();
    let buf='',reply='',gotToken=false,streamErr=null;
    for(;;){
      const {value,done}=await reader.read();
      if(done)break;
      buf+=dec.decode(value,{stream:true});
      // SSE events are separated by a blank line; each carries a "data:" JSON line.
      let sep;
      while((sep=buf.indexOf('\n\n'))>=0){
        const line=buf.slice(0,sep).trim();
        buf=buf.slice(sep+2);
        if(!line.startsWith('data:'))continue;
        let evt;try{evt=JSON.parse(line.slice(5).trim());}catch(_){continue;}
        if(evt.error){streamErr=evt.error;continue;}
        if(evt.module&&who)who.textContent='GK · '+evt.module.toUpperCase();
        if(evt.token){
          reply+=evt.token;
          if(!gotToken){txt.textContent='';gotToken=true;} // clear the "…" placeholder
          txt.textContent=reply;
          chatCopyToggle(pending,reply);
          el('chat-log').scrollTop=el('chat-log').scrollHeight;
        }
      }
    }
    if(streamErr){
      pending.classList.add('err');
      txt.textContent='ERR: '+streamErr;
    }else if(!gotToken){
      txt.textContent='(no response)';
    }
    chatCopyToggle(pending,txt.textContent);
    chatSave();
  }catch(e){
    pending.classList.add('err');
    txt.textContent='FETCH ERROR: '+e.message;
    chatSave();
  }
}

// ── init ───────────────────────────────────────────────────────────────────
function chatInit(){
  chatState=chatLoadState();
  if(!chatState.sessions.length){
    const fresh=chatBlank('Chat 1');
    chatState.sessions.push(fresh);
    chatState.active=fresh.id;
  }else if(!chatActive()){
    chatState.active=chatState.sessions[0].id;
  }
  chatPersist();
  chatRenderSessions();
  chatRenderLog();
}

if(document.readyState==='loading'){
  document.addEventListener('DOMContentLoaded',chatInit);
}else{
  chatInit();
}

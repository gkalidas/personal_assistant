async function loadMiniTodo(){
  try{
    const r=await fetch('/api/todos');const tasks=await r.json();
    renderMiniTodo(tasks);
  }catch(e){
    // fallback: try localStorage
    try{renderMiniTodo(JSON.parse(localStorage.getItem('gk_tasks')||'[]'))}catch(_){}
  }
}
function renderMiniTodo(tasks){
  const list=el('todo-mini-list');
  const active=tasks.filter(t=>!t.done&&(t.quad==='q1'||t.quad==='q2'));
  if(!active.length){list.innerHTML='<div style="font-size:10px;letter-spacing:2px;color:#0c2535;padding:8px 0">No active tasks</div>';return}
  const q1=active.filter(t=>t.quad==='q1');
  const q2=active.filter(t=>t.quad==='q2');
  let html='';
  if(q1.length){
    html+='<div class="todo-section-lbl" style="color:var(--red)">DO FIRST</div>';
    for(const t of q1.slice(0,4))html+=`<div class="todo-item" onclick="doneTask('${t.id}')"><span class="ti-dot q1">◆</span><span class="ti-text">${t.text}</span></div>`;
  }
  if(q2.length){
    html+='<div class="todo-section-lbl" style="color:var(--amber)">SCHEDULE</div>';
    for(const t of q2.slice(0,3))html+=`<div class="todo-item" onclick="doneTask('${t.id}')"><span class="ti-dot q2">◆</span><span class="ti-text">${t.text}</span></div>`;
  }
  list.innerHTML=html;
}
async function doneTask(id){
  try{
    await fetch(`/api/todos/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({done:1})});
    loadMiniTodo();
  }catch(e){
    // fallback
    try{const tasks=JSON.parse(localStorage.getItem('gk_tasks')||'[]');const t=tasks.find(x=>x.id===id);if(t){t.done=true;localStorage.setItem('gk_tasks',JSON.stringify(tasks));loadMiniTodo()}}catch(_){}
  }
}
loadMiniTodo();
setInterval(loadMiniTodo,30*1000);

// ── Todo full modal ────────────────────────────────────────────────────────────
let _todoFull=[], _todoShowDone=false;

async function openTodoFull(){
  el('overlay').classList.add('open');
  el('modal-todo-full').classList.add('open');
  _openModal='todo';
  await _loadTodoFull();
}
function closeTodoFull(){
  el('overlay').classList.remove('open');
  el('modal-todo-full').classList.remove('open');
  _openModal=null;
}
async function _loadTodoFull(){
  try{
    const r=await fetch('/api/todos?include_done='+(_todoShowDone?'true':'false'));
    _todoFull=await r.json();
    _renderTodoFull();
  }catch(e){console.error('todo load failed',e)}
}
function _renderTodoFull(){
  const quads=['q1','q2','q3','q4'];
  for(const q of quads){
    const tasks=_todoFull.filter(t=>t.quad===q);
    const list=el('mqlist-'+q);
    el('mqcnt-'+q).textContent=tasks.filter(t=>!t.done).length||'';
    if(!tasks.length){list.innerHTML='<div class="modal-quad-empty">— empty —</div>';continue}
    list.innerHTML=tasks.map(t=>{
      const hasVerifier=!!t.verifier;
      const verifyFailed=t.verified===-1;
      const verifyOk=t.verified===1;
      const _esc=s=>(s||'').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
      const verifyBadge=hasVerifier
        ? (verifyFailed
            ? `<span class="tq-verify-badge fail" data-tooltip="BLOCKED — cannot mark done\n${_esc(t.verify_note||'verification failed')}">⚠ BLOCKED</span>`
            : verifyOk
              ? `<span class="tq-verify-badge ok" data-tooltip="Verified ✓\n${_esc(t.verify_note||'check passed')}">✓</span>`
              : `<span class="tq-verify-badge pending" data-tooltip="Auto-verified task\nNot checked yet — will run within 10 min">⟳</span>`)
        : '';
      return `<div class="modal-task-item${t.done?' done':''}${verifyFailed?' verify-fail':''}" draggable="true" ondragstart="tqDragStart(event,'${t.id}')" ondragend="tqDragEnd(event)" onclick="modalToggleTask('${t.id}',${t.done?0:1})">
        <input type="checkbox" class="modal-task-cb" ${t.done?'checked':''} onclick="event.stopPropagation()" onchange="modalToggleTask('${t.id}',this.checked)">
        <span class="modal-task-text">${(t.text||'').replace(/</g,'&lt;')}</span>
        ${verifyBadge}
        <button class="modal-task-del" onclick="event.stopPropagation();modalDeleteTask('${t.id}')">×</button>
      </div>`;
    }).join('');
  }
}
// ── Todo quadrant drag-and-drop ───────────────────────────────────────────────
let _tqDragId=null;
function tqDragStart(e,id){
  _tqDragId=id;
  e.dataTransfer.effectAllowed='move';
  e.dataTransfer.setData('text/plain',id);
  setTimeout(()=>{const el=e.target.closest('.modal-task-item');if(el)el.classList.add('tq-dragging')},0);
}
function tqDragEnd(e){
  _tqDragId=null;
  document.querySelectorAll('.modal-task-item.tq-dragging').forEach(el=>el.classList.remove('tq-dragging'));
  document.querySelectorAll('.modal-quad-tasks.tq-over').forEach(el=>el.classList.remove('tq-over'));
}
function tqOver(e){e.preventDefault();e.dataTransfer.dropEffect='move';}
function tqEnter(e){e.currentTarget.classList.add('tq-over');}
function tqLeave(e){if(!e.currentTarget.contains(e.relatedTarget))e.currentTarget.classList.remove('tq-over');}
async function tqDrop(e,quad){
  e.preventDefault();
  e.currentTarget.classList.remove('tq-over');
  const id=_tqDragId||e.dataTransfer.getData('text/plain');
  if(!id)return;
  _tqDragId=null;
  await fetch(`/api/todos/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({quad})});
  await _loadTodoFull();
  loadMiniTodo();
}

async function modalToggleTask(id,done){
  const r=await fetch(`/api/todos/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({done:done?1:0})});
  const d=await r.json();
  if(d.blocked){
    // Verifier blocked the done mark — show the reason inline
    const item=document.querySelector(`[ondragstart*="${id}"]`);
    if(item){
      item.classList.add('verify-fail');
      let tip=item.querySelector('.tq-verify-toast');
      if(!tip){tip=document.createElement('div');tip.className='tq-verify-toast';item.appendChild(tip);}
      tip.textContent='⚠ '+d.reason;
      setTimeout(()=>{if(tip&&tip.parentNode)tip.parentNode.removeChild(tip);item.classList.remove('verify-fail');},4000);
    }
  }
  await _loadTodoFull();
  loadMiniTodo();
}
async function modalDeleteTask(id){
  await fetch(`/api/todos/${id}`,{method:'DELETE'});
  await _loadTodoFull();
  loadMiniTodo();
}
async function modalAddTask(){
  const inp=el('modal-todo-input'),text=inp.value.trim();
  if(!text)return;
  const quad=el('modal-todo-quad').value;
  await fetch('/api/todos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,quad})});
  inp.value='';
  await _loadTodoFull();
  loadMiniTodo();
}
function modalToggleDone(){
  _todoShowDone=!_todoShowDone;
  const btn=document.querySelector('.modal-todo-btn');
  btn.textContent=_todoShowDone?'HIDE DONE':'SHOW DONE';
  btn.classList.toggle('active',_todoShowDone);
  _loadTodoFull();
}
async function modalClearDone(){
  const done=_todoFull.filter(t=>t.done);
  await Promise.all(done.map(t=>fetch(`/api/todos/${t.id}`,{method:'DELETE'})));
  await _loadTodoFull();
  loadMiniTodo();
}

// ── Panel layout — movable / resizable / transparent ──────────────────────────

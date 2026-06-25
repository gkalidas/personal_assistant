const _PL={};          // {panelId: {x,y,w,h,z}}
let _maxZ=20, _drag=null, _dragMoved=false;
const _PMIN_W=160, _PMIN_H=80;
const _LSKEY='gk_panel_layout_v2';

function _defaultLayout(){
  const W=window.innerWidth, H=window.innerHeight-44;
  const g=8, cw=Math.floor((W-g*5)/4);
  const r1=150, r2=145, r3=Math.max(100,H-r1-r2-g*4);
  const r3a=Math.max(90,Math.floor((r3-g)/2));   // voice (top) / live-news (bottom)
  return{
    'p-network':  {x:g,        y:g,          w:cw,       h:r1,  z:10},
    'p-system':   {x:g*2+cw,   y:g,          w:cw,       h:r1,  z:10},
    'p-weather':  {x:g*3+cw*2, y:g,          w:cw,       h:r1,  z:10},
    'p-guardian': {x:g*4+cw*3, y:g,          w:cw,       h:r1,  z:10},
    'p-diary':    {x:g,        y:g*2+r1,     w:cw*3+g*2, h:r2,  z:10},
    'p-todo-mini':{x:g*4+cw*3, y:g*2+r1,     w:cw,       h:r2,  z:10},
    'p-news':     {x:g,        y:g*3+r1+r2,  w:cw*3+g*2, h:r3,  z:10},
    'p-voice':    {x:g*4+cw*3, y:g*3+r1+r2,          w:cw, h:r3a,        z:10},
    'p-livenews': {x:g*4+cw*3, y:g*3+r1+r2+r3a+g,    w:cw, h:r3-r3a-g,   z:10},
  };
}

function _applyPanel(id){
  const p=_PL[id]; if(!p)return;
  const d=document.getElementById(id); if(!d)return;
  d.style.left=p.x+'px'; d.style.top=p.y+'px';
  d.style.width=p.w+'px'; d.style.height=p.h+'px';
  d.style.zIndex=p.z;
}

function _bringToFront(id){
  if(!_PL[id])return;
  _maxZ++;
  _PL[id].z=_maxZ;
  const d=document.getElementById(id); if(d)d.style.zIndex=_maxZ;
}

function _savePanelLayout(){
  try{localStorage.setItem(_LSKEY,JSON.stringify(_PL))}catch(e){}
}

function _initPanelLayout(){
  const defs=_defaultLayout();
  let saved={};
  try{saved=JSON.parse(localStorage.getItem(_LSKEY)||'{}')}catch(e){}
  for(const id of Object.keys(defs)){
    const s=saved[id];
    _PL[id]=s&&typeof s.x==='number'?{x:s.x,y:s.y,w:Math.max(_PMIN_W,s.w),h:Math.max(_PMIN_H,s.h),z:s.z||10}:{...defs[id]};
    _applyPanel(id);
  }
  _maxZ=Math.max(20,...Object.values(_PL).map(p=>p.z||10));
}

function resetPanelLayout(){
  localStorage.removeItem(_LSKEY);
  const defs=_defaultLayout();
  for(const id of Object.keys(defs)){_PL[id]={...defs[id]};_applyPanel(id);}
  _savePanelLayout();
}

// Pointer events — drag (from label) and resize (from grip)
document.addEventListener('mousedown',e=>{
  const resizeEl=e.target.closest('.panel-resize');
  const labelEl =e.target.closest('.panel-label');
  const panelEl =e.target.closest('.panel[id]');
  if(!panelEl)return;
  const id=panelEl.id; if(!_PL[id])return;
  _bringToFront(id); _dragMoved=false;
  if(resizeEl){
    e.preventDefault();
    _drag={type:'resize',id,startX:e.clientX,startY:e.clientY,origW:_PL[id].w,origH:_PL[id].h};
  }else if(labelEl&&!e.target.closest('button')&&!e.target.closest('input')&&!e.target.closest('a')&&!e.target.closest('select')&&!e.target.closest('span[onclick]')){
    _drag={type:'move',id,startX:e.clientX,startY:e.clientY,origX:_PL[id].x,origY:_PL[id].y};
  }
},true);

document.addEventListener('mousemove',e=>{
  if(!_drag)return;
  const dx=e.clientX-_drag.startX, dy=e.clientY-_drag.startY;
  if(!_dragMoved&&Math.abs(dx)<4&&Math.abs(dy)<4)return;
  _dragMoved=true;
  const p=_PL[_drag.id];
  if(_drag.type==='move'){
    p.x=Math.max(0,_drag.origX+dx);
    p.y=Math.max(0,_drag.origY+dy);
  }else{
    p.w=Math.max(_PMIN_W,_drag.origW+dx);
    p.h=Math.max(_PMIN_H,_drag.origH+dy);
  }
  _applyPanel(_drag.id);
});

document.addEventListener('mouseup',e=>{
  if(_drag&&_dragMoved)_savePanelLayout();
  _drag=null;
});

// Cancel click only on panel elements after a drag — lets modal/todo clicks through
document.addEventListener('click',e=>{
  if(_dragMoved){
    if(e.target.closest('.panel[id]')){e.stopPropagation();e.preventDefault();}
    _dragMoved=false;
  }
},true);

_initPanelLayout();


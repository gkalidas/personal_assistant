// GK Personal Assistant — 3D system model (Three.js)
// GUARDIAN wireframe shell (outer) protecting a ROUTER core + module nodes,
// connected to the INTERNET, under constant (blocked) attack, with scripted
// real-life example animations. Orbit-drag to rotate, scroll to zoom.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const CYAN = 0x00e5ff, GREEN = 0x00ff88, RED = 0xff2255, NET = 0x9fd6ff;
const R_MODULE = 7.0;     // module orbit radius
const R_SHELL  = 13.0;    // guardian shell radius
const ATTACK_N = 5;       // concurrent incoming attacks

// name · colour · drawn glyph · distinct accent shape
const MODULES = [
  { name: 'FARMING',   color: 0x3fb950, glyph: 'wheat', shape: 'ico'   },
  { name: 'FINANCE',   color: 0xffaa00, glyph: 'coins', shape: 'octa'  },
  { name: 'HEALTH',    color: 0xff2255, glyph: 'cross', shape: 'torus' },
  { name: 'DIARY',     color: 0xbc8cff, glyph: 'book',  shape: 'box'   },
  { name: 'DASHBOARD', color: 0x56d3ff, glyph: 'chart', shape: 'tetra' },
];

let scene, camera, renderer, controls, clock, guardian, netNode, shieldPulse;
const moduleNodes = [];   // { group, sphere, basePos, color, pulse }
const flows = [];         // legit data-flow particles { mesh, from, to, t, speed }
const attacks = [];       // { line, head, origin, hit, dir, t, speed, delay }
const transient = [];     // clouds + impact rings { mesh, life, ttl, kind, vel }
let attackCount = 0, attackBurst = 0;
const pulses = [];        // active node pulses { node, t }

// ── Sprite / texture helpers ─────────────────────────────────────────────────
function spriteFromCanvas(cv, sx, sy) {
  const tex = new THREE.CanvasTexture(cv);
  tex.anisotropy = 4;
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false }));
  s.scale.set(sx, sy, 1);
  return s;
}
function makeLabel(text, hex, scale = 5.2) {
  const cv = document.createElement('canvas'); cv.width = 256; cv.height = 64;
  const ctx = cv.getContext('2d');
  ctx.font = 'bold 30px "JetBrains Mono", monospace';
  ctx.fillStyle = '#' + hex.toString(16).padStart(6, '0');
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.shadowColor = ctx.fillStyle; ctx.shadowBlur = 12;
  ctx.fillText(text, 128, 34);
  return spriteFromCanvas(cv, scale, scale / 4);
}
// Vector glyphs drawn with canvas paths — no emoji-font dependency, always render.
function drawGlyph(ctx, g) {
  if (g === 'wheat') {
    ctx.beginPath(); ctx.moveTo(64, 106); ctx.lineTo(64, 44); ctx.stroke();
    for (let i = 0; i < 3; i++) { const y = 52 + i * 16;
      ctx.beginPath(); ctx.moveTo(64, y); ctx.lineTo(49, y - 11); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(64, y); ctx.lineTo(79, y - 11); ctx.stroke(); }
    ctx.beginPath(); ctx.moveTo(64, 44); ctx.lineTo(55, 30); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(64, 44); ctx.lineTo(73, 30); ctx.stroke();
  } else if (g === 'coins') {
    ctx.lineWidth = 6;
    for (let i = 0; i < 3; i++) { ctx.beginPath(); ctx.ellipse(64, 82 - i * 17, 27, 10, 0, 0, Math.PI * 2); ctx.stroke(); }
  } else if (g === 'cross') {            // medical plus
    ctx.fillRect(53, 34, 22, 60); ctx.fillRect(34, 53, 60, 22);
  } else if (g === 'book') {
    ctx.strokeRect(40, 38, 48, 54);
    ctx.beginPath(); ctx.moveTo(64, 38); ctx.lineTo(64, 92); ctx.stroke();
    ctx.lineWidth = 4;
    for (let i = 0; i < 3; i++) { const y = 52 + i * 13;
      ctx.beginPath(); ctx.moveTo(48, y); ctx.lineTo(58, y); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(70, y); ctx.lineTo(80, y); ctx.stroke(); }
  } else if (g === 'chart') {
    ctx.beginPath(); ctx.moveTo(36, 94); ctx.lineTo(92, 94); ctx.stroke();
    for (const [x, top] of [[46, 70], [64, 46], [82, 60]]) ctx.fillRect(x - 8, top, 16, 94 - top);
  }
}
function makeIcon(glyph, hex, scale = 2.4) {
  const cv = document.createElement('canvas'); cv.width = 128; cv.height = 128;
  const ctx = cv.getContext('2d');
  const col = '#' + hex.toString(16).padStart(6, '0');
  ctx.strokeStyle = col; ctx.fillStyle = col;
  ctx.lineWidth = 7; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  ctx.shadowColor = col; ctx.shadowBlur = 9;
  drawGlyph(ctx, glyph);
  return spriteFromCanvas(cv, scale, scale);
}
function makeCloud(scale = 4) {
  const cv = document.createElement('canvas'); cv.width = 128; cv.height = 80;
  const ctx = cv.getContext('2d');
  ctx.fillStyle = 'rgba(220,235,245,0.85)';
  for (const [x, y, r] of [[42, 50, 22], [70, 44, 26], [92, 52, 18], [58, 58, 24]]) {
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
  }
  return spriteFromCanvas(cv, scale, scale * 0.62);
}
function glowSphere(radius, hex, opacity = 1) {
  return new THREE.Mesh(new THREE.SphereGeometry(radius, 28, 28),
    new THREE.MeshStandardMaterial({ color: hex, emissive: hex, emissiveIntensity: 0.9,
      roughness: 0.35, metalness: 0.1, transparent: opacity < 1, opacity }));
}
function accentGeometry(shape) {
  switch (shape) {
    case 'octa':  return new THREE.OctahedronGeometry(1.7);
    case 'torus': return new THREE.TorusGeometry(1.5, 0.18, 12, 24);
    case 'box':   return new THREE.BoxGeometry(2.2, 2.2, 2.2);
    case 'tetra': return new THREE.TetrahedronGeometry(1.9);
    default:      return new THREE.IcosahedronGeometry(1.7, 0);
  }
}

// ── Scene graph ──────────────────────────────────────────────────────────────
function modulePositions() {
  return MODULES.map((m, i) => {
    const a = (i / MODULES.length) * Math.PI * 2 - Math.PI / 2;
    const tilt = (i % 2 === 0 ? 1.6 : -1.6);
    return new THREE.Vector3(Math.cos(a) * R_MODULE, tilt, Math.sin(a) * R_MODULE);
  });
}
function buildCore() {
  scene.add(glowSphere(1.6, CYAN));
  scene.add(new THREE.Mesh(new THREE.IcosahedronGeometry(2.3, 1),
    new THREE.MeshBasicMaterial({ color: CYAN, wireframe: true, transparent: true, opacity: 0.35 })));
  const l = makeLabel('ROUTER', CYAN); l.position.set(0, 3.0, 0); scene.add(l);
  scene.add(new THREE.PointLight(CYAN, 60, 50));
}
function buildModules(positions) {
  positions.forEach((pos, i) => {
    const m = MODULES[i];
    const group = new THREE.Group(); group.position.copy(pos); scene.add(group);
    const sphere = glowSphere(0.95, m.color); group.add(sphere);
    const accent = new THREE.Mesh(accentGeometry(m.shape),
      new THREE.MeshBasicMaterial({ color: m.color, wireframe: true, transparent: true, opacity: 0.5 }));
    group.add(accent);
    const icon = makeIcon(m.glyph, m.color); icon.position.set(0, 0, 0.1); group.add(icon);
    const label = makeLabel(m.name, m.color, 4.4); label.position.set(0, 2.4, 0); group.add(label);

    const geo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), pos]);
    scene.add(new THREE.Line(geo, new THREE.LineBasicMaterial({ color: m.color, transparent: true, opacity: 0.32 })));

    const p = glowSphere(0.3, m.color); scene.add(p);
    flows.push({ mesh: p, from: new THREE.Vector3(), to: pos.clone(), t: Math.random(), speed: 0.16 + Math.random() * 0.1 });
    moduleNodes.push({ group, sphere, accent, basePos: pos.clone(), color: m.color });
  });
}
function buildGuardian() {
  guardian = new THREE.Mesh(new THREE.IcosahedronGeometry(R_SHELL, 2),
    new THREE.MeshBasicMaterial({ color: GREEN, wireframe: true, transparent: true, opacity: 0.22 }));
  scene.add(guardian);
  const l = makeLabel('GUARDIAN', GREEN, 7); l.position.set(0, R_SHELL + 1.6, 0); scene.add(l);
}
function buildInternet() {
  const dir = new THREE.Vector3(1, 0.45, 0.35).normalize();
  const at = dir.clone().multiplyScalar(R_SHELL + 7);
  netNode = new THREE.Group(); netNode.position.copy(at); scene.add(netNode);
  // a little cloud cluster + wire globe
  for (const off of [[0, 0, 0], [1.4, 0.4, 0], [-1.3, 0.5, 0.3]]) {
    const c = makeCloud(3.4); c.position.set(...off); netNode.add(c);
  }
  netNode.add(new THREE.Mesh(new THREE.IcosahedronGeometry(2.2, 1),
    new THREE.MeshBasicMaterial({ color: NET, wireframe: true, transparent: true, opacity: 0.4 })));
  const l = makeLabel('INTERNET / APIs', NET, 6); l.position.set(0, 3.0, 0); netNode.add(l);

  // outbound link from the shell surface (through the guardian) to the internet
  const surf = dir.clone().multiplyScalar(R_SHELL);
  scene.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([surf, at]),
    new THREE.LineBasicMaterial({ color: NET, transparent: true, opacity: 0.5 })));
  // legit packets riding the link, both directions
  for (let i = 0; i < 3; i++) {
    const p = glowSphere(0.26, NET); scene.add(p);
    flows.push({ mesh: p, from: surf.clone(), to: at.clone(), t: i / 3, speed: 0.16, pingpong: true });
  }
}

// ── ROUTER ↔ GUARDIAN link (the core is wrapped & secured by the shell) ──────
function fibonacciDirs(n) {
  const out = [], phi = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = 1 - (i / (n - 1)) * 2, r = Math.sqrt(1 - y * y), th = phi * i;
    out.push(new THREE.Vector3(Math.cos(th) * r, y, Math.sin(th) * r));
  }
  return out;
}
function buildShield() {
  // struts from the ROUTER core out to the GUARDIAN shell, with packets riding
  // outward — every router request is wrapped/checked by the guardian.
  for (const d of fibonacciDirs(9)) {
    const end = d.clone().multiplyScalar(R_SHELL);
    scene.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), end]),
      new THREE.LineBasicMaterial({ color: 0x13b5a6, transparent: true, opacity: 0.22 })));
    const p = glowSphere(0.2, 0x6affd6); scene.add(p);
    flows.push({ mesh: p, from: new THREE.Vector3(), to: end, t: Math.random(), speed: 0.14 });
  }
  // protective pulse that swells from the core out to the shell, on repeat
  shieldPulse = new THREE.Mesh(new THREE.SphereGeometry(1, 24, 24),
    new THREE.MeshBasicMaterial({ color: GREEN, transparent: true, opacity: 0.12, side: THREE.BackSide }));
  shieldPulse.userData.t = 0; scene.add(shieldPulse);
}

// ── Attacks (always-on, always blocked) ──────────────────────────────────────
function randDir() {
  const v = new THREE.Vector3(Math.random() * 2 - 1, Math.random() * 2 - 1, Math.random() * 2 - 1);
  if (v.lengthSq() < 0.01) v.set(1, 0, 0);
  return v.normalize();
}
function armAttack(a) {
  a.dir = randDir();
  a.origin = a.dir.clone().multiplyScalar(R_SHELL + 9 + Math.random() * 6);
  a.hit = a.dir.clone().multiplyScalar(R_SHELL);
  a.head.copy(a.origin);
  a.t = 0; a.speed = 0.13 + Math.random() * 0.12; a.delay = 1.0 + Math.random() * 3.0;
}
function buildAttacks() {
  for (let i = 0; i < ATTACK_N; i++) {
    const head = new THREE.Vector3();
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
      new THREE.LineBasicMaterial({ color: RED, transparent: true, opacity: 0.8 }));
    scene.add(line);
    const a = { line, head, origin: new THREE.Vector3(), hit: new THREE.Vector3(), dir: new THREE.Vector3() };
    armAttack(a); attacks.push(a);
  }
}
function impactRing(at) {
  const ring = new THREE.Mesh(new THREE.RingGeometry(0.3, 0.55, 24),
    new THREE.MeshBasicMaterial({ color: RED, transparent: true, opacity: 0.95, side: THREE.DoubleSide }));
  ring.position.copy(at);
  ring.lookAt(at.clone().multiplyScalar(2));   // face outward (radial normal)
  scene.add(ring);
  transient.push({ mesh: ring, life: 0, ttl: 0.55, kind: 'ring' });
}
function updateAttacks(dt) {
  for (const a of attacks) {
    if (a.delay > 0) { a.delay -= dt; a.line.visible = false; continue; }
    a.line.visible = true;
    a.t += dt * a.speed * (attackBurst > 0 ? 1.8 : 1);
    a.head.lerpVectors(a.origin, a.hit, Math.min(a.t, 1));
    const pos = a.line.geometry.attributes.position;
    pos.setXYZ(0, a.origin.x, a.origin.y, a.origin.z);
    pos.setXYZ(1, a.head.x, a.head.y, a.head.z);
    pos.needsUpdate = true;
    a.line.material.opacity = 0.35 + 0.5 * Math.min(a.t, 1);
    if (a.t >= 1) { impactRing(a.hit); attackCount++; armAttack(a); }
  }
  if (attackBurst > 0) attackBurst -= dt;
  const el = document.getElementById('atkn');
  if (el) el.textContent = attackCount;
}

// ── Transient effects (clouds, rings) ────────────────────────────────────────
function spawnClouds(center, n = 5) {
  for (let i = 0; i < n; i++) {
    const c = makeCloud(2.6 + Math.random() * 1.8);
    c.position.copy(center).add(new THREE.Vector3((Math.random() - 0.5) * 6, Math.random() * 2, (Math.random() - 0.5) * 6));
    scene.add(c);
    transient.push({ mesh: c, life: 0, ttl: 3.5, kind: 'cloud',
      vel: new THREE.Vector3((Math.random() - 0.5) * 0.6, 0.4 + Math.random() * 0.4, (Math.random() - 0.5) * 0.6) });
  }
}
function updateTransient(dt) {
  for (let i = transient.length - 1; i >= 0; i--) {
    const o = transient[i]; o.life += dt;
    const k = o.life / o.ttl;
    if (o.kind === 'ring') { const s = 0.4 + k * 2.0; o.mesh.scale.set(s, s, s); o.mesh.material.opacity = 0.95 * (1 - k); }
    else if (o.kind === 'cloud') { o.mesh.position.addScaledVector(o.vel, dt); o.mesh.material.opacity = (k < 0.3 ? k / 0.3 : (1 - k)) * 0.9; }
    if (k >= 1) { scene.remove(o.mesh); o.mesh.material.map?.dispose(); o.mesh.material.dispose(); o.mesh.geometry?.dispose(); transient.splice(i, 1); }
  }
}

// ── Node pulse (used by examples) ────────────────────────────────────────────
function pulseNode(idx) { if (moduleNodes[idx]) pulses.push({ node: moduleNodes[idx], t: 0 }); }
function updatePulses(dt) {
  for (let i = pulses.length - 1; i >= 0; i--) {
    const p = pulses[i]; p.t += dt;
    const s = 1 + Math.sin(Math.min(p.t, 1) * Math.PI) * 0.6;
    p.node.group.scale.setScalar(s);
    if (p.t >= 1) { p.node.group.scale.setScalar(1); pulses.splice(i, 1); }
  }
}

// ── Scripted real-life examples ──────────────────────────────────────────────
const EXAMPLES = [
  { label: 'Weather', cap: '<b>"how\'s the weather today?"</b> → ROUTER → FARMING → out through GUARDIAN to <span class="g">Open-Meteo</span> → "31°C, light rain after 4pm" ☁',
    run: () => { pulseNode(0); spawnClouds(netNode.position, 6); spawnClouds(moduleNodes[0].basePos, 3); } },
  { label: 'News', cap: 'DASHBOARD pulls <span class="g">live news</span> from the INTERNET — every fetch passes through the GUARDIAN first.',
    run: () => { pulseNode(4); spawnClouds(netNode.position, 3); } },
  { label: 'Crop forecast', cap: 'FARMING sends its weather forecast to the external <span class="g">farming app (llama3)</span> → 4-day pomegranate stress prediction.',
    run: () => { pulseNode(0); } },
  { label: 'Under attack', cls: 'r', cap: '<span class="r">Someone is always probing the system</span> — injection, scrapers, scanners. The <span class="g">GUARDIAN shell blocks every one</span> at the perimeter. 🛡',
    run: () => { attackBurst = 4.0; } },
];
let exampleIdx = -1, autoTimer = 0, autoHold = 6.0, userPaused = false;

function setCaption(html) { const c = document.getElementById('caption'); if (c) { c.style.opacity = 0; setTimeout(() => { c.innerHTML = html; c.style.opacity = 1; }, 200); } }
function selectExample(i, manual) {
  exampleIdx = i; autoTimer = 0;
  if (manual) { userPaused = true; setTimeout(() => { userPaused = false; }, 14000); }
  const ex = EXAMPLES[i]; setCaption(ex.cap); ex.run();
  document.querySelectorAll('#examples button').forEach((b, j) => {
    const on = j === i;
    b.classList.toggle('on', on);
    b.classList.toggle('r', on && !!EXAMPLES[j].cls);
  });
}
function buildExampleButtons() {
  const wrap = document.getElementById('examples');
  EXAMPLES.forEach((ex, i) => {
    const b = document.createElement('button'); b.textContent = ex.label;
    b.onclick = () => selectExample(i, true); wrap.appendChild(b);
  });
}

// ── Loop ─────────────────────────────────────────────────────────────────────
function animate() {
  requestAnimationFrame(animate);
  const dt = Math.min(clock.getDelta(), 0.05);
  guardian.rotation.y += dt * 0.12; guardian.rotation.x += dt * 0.04;
  // protective pulse swelling from the router out to the guardian shell
  shieldPulse.userData.t += dt * 0.3; if (shieldPulse.userData.t > 1) shieldPulse.userData.t -= 1;
  const sp = shieldPulse.userData.t;
  shieldPulse.scale.setScalar(2 + (R_SHELL - 2) * sp);
  shieldPulse.material.opacity = 0.16 * (1 - sp);
  for (const n of moduleNodes) { n.accent.rotation.y += dt * 0.8; n.accent.rotation.x += dt * 0.5; }
  for (const f of flows) {
    f.t += dt * f.speed; if (f.t > 1) f.t -= 1;
    const tt = f.pingpong ? (f.t < 0.5 ? f.t * 2 : (1 - f.t) * 2) : f.t;
    f.mesh.position.lerpVectors(f.from, f.to, tt);
  }
  updateAttacks(dt); updateTransient(dt); updatePulses(dt);
  if (!userPaused) { autoTimer += dt; if (autoTimer >= autoHold) selectExample((exampleIdx + 1) % EXAMPLES.length, false); }
  controls.update();
  renderer.render(scene, camera);
}
function onResize() {
  camera.aspect = window.innerWidth / window.innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
}
function init() {
  scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x030b10, 0.014);
  camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 0.1, 300);
  camera.position.set(2, 9, 28);
  renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('c3d'), antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.06;
  controls.autoRotate = true; controls.autoRotateSpeed = 0.55;
  controls.minDistance = 16; controls.maxDistance = 70;
  scene.add(new THREE.AmbientLight(0x223344, 2.5));
  clock = new THREE.Clock();

  buildGuardian(); buildCore(); buildModules(modulePositions());
  buildShield(); buildInternet(); buildAttacks(); buildExampleButtons();

  window.addEventListener('resize', onResize);
  const loading = document.getElementById('loading'); if (loading) loading.style.display = 'none';
  selectExample(0, false);
  animate();
}

init();

// GK Personal Assistant — 3D system model (Three.js)
// GUARDIAN wireframe shell (outer) enclosing module nodes around a ROUTER core,
// with animated data-flow particles. Orbit-drag to rotate, scroll to zoom.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const MODULES = [
  { name: 'FARMING',   color: 0x3fb950 },
  { name: 'FINANCE',   color: 0xffaa00 },
  { name: 'HEALTH',    color: 0xff2255 },
  { name: 'DIARY',     color: 0xbc8cff },
  { name: 'DASHBOARD', color: 0x56d3ff },
];
const CYAN = 0x00e5ff, GREEN = 0x00ff88;
const R_MODULE = 7.0;     // module orbit radius
const R_SHELL  = 13.0;    // guardian shell radius

let scene, camera, renderer, controls, guardian, clock;
const particles = [];     // { mesh, from, to, t, speed }

// ── Text label as a camera-facing sprite ─────────────────────────────────────
function makeLabel(text, hex) {
  const cv = document.createElement('canvas');
  cv.width = 256; cv.height = 64;
  const ctx = cv.getContext('2d');
  ctx.font = 'bold 30px "JetBrains Mono", monospace';
  ctx.fillStyle = '#' + hex.toString(16).padStart(6, '0');
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.shadowColor = ctx.fillStyle;
  ctx.shadowBlur = 12;
  ctx.fillText(text, 128, 34);
  const tex = new THREE.CanvasTexture(cv);
  tex.anisotropy = 4;
  const spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false }));
  spr.scale.set(5.2, 1.3, 1);
  return spr;
}

function glowSphere(radius, hex, opacity = 1) {
  const mat = new THREE.MeshStandardMaterial({
    color: hex, emissive: hex, emissiveIntensity: 0.9,
    roughness: 0.35, metalness: 0.1, transparent: opacity < 1, opacity,
  });
  return new THREE.Mesh(new THREE.SphereGeometry(radius, 32, 32), mat);
}

function modulePositions() {
  return MODULES.map((m, i) => {
    const a = (i / MODULES.length) * Math.PI * 2 - Math.PI / 2;
    const tilt = (i % 2 === 0 ? 1.6 : -1.6);   // slight vertical stagger for depth
    return new THREE.Vector3(Math.cos(a) * R_MODULE, tilt, Math.sin(a) * R_MODULE);
  });
}

// ── Build the scene graph ────────────────────────────────────────────────────
function buildCore() {
  const core = glowSphere(1.6, CYAN);
  scene.add(core);
  const halo = new THREE.Mesh(
    new THREE.IcosahedronGeometry(2.3, 1),
    new THREE.MeshBasicMaterial({ color: CYAN, wireframe: true, transparent: true, opacity: 0.35 }),
  );
  scene.add(halo);
  const label = makeLabel('ROUTER', CYAN);
  label.position.set(0, 3.0, 0);
  scene.add(label);
  scene.add(new THREE.PointLight(CYAN, 60, 40));
}

function buildModules(positions) {
  positions.forEach((pos, i) => {
    const m = MODULES[i];
    const node = glowSphere(1.15, m.color);
    node.position.copy(pos);
    scene.add(node);

    const label = makeLabel(m.name, m.color);
    label.position.copy(pos).add(new THREE.Vector3(0, 2.0, 0));
    scene.add(label);

    // core → module link line
    const geo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0), pos]);
    scene.add(new THREE.Line(geo, new THREE.LineBasicMaterial({
      color: m.color, transparent: true, opacity: 0.35,
    })));

    // data-flow particle for this link
    const p = glowSphere(0.34, m.color);
    scene.add(p);
    particles.push({ mesh: p, from: new THREE.Vector3(0, 0, 0), to: pos.clone(), t: Math.random(), speed: 0.4 + Math.random() * 0.3 });
  });
}

function buildGuardian() {
  guardian = new THREE.Mesh(
    new THREE.IcosahedronGeometry(R_SHELL, 2),
    new THREE.MeshBasicMaterial({ color: GREEN, wireframe: true, transparent: true, opacity: 0.22 }),
  );
  scene.add(guardian);
  const label = makeLabel('GUARDIAN', GREEN);
  label.position.set(0, R_SHELL + 1.5, 0);
  label.scale.set(7, 1.75, 1);
  scene.add(label);
}

// ── Loop ─────────────────────────────────────────────────────────────────────
function animate() {
  requestAnimationFrame(animate);
  const dt = clock.getDelta();
  guardian.rotation.y += dt * 0.12;
  guardian.rotation.x += dt * 0.04;
  for (const p of particles) {
    p.t += dt * p.speed;
    if (p.t > 1) p.t -= 1;
    p.mesh.position.lerpVectors(p.from, p.to, p.t);
  }
  controls.update();
  renderer.render(scene, camera);
}

function onResize() {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
}

function init() {
  const canvas = document.getElementById('c3d');
  scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x030b10, 0.018);

  camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 0.1, 200);
  camera.position.set(0, 9, 26);

  renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.06;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.6;
  controls.minDistance = 16;
  controls.maxDistance = 60;

  scene.add(new THREE.AmbientLight(0x223344, 2.5));

  clock = new THREE.Clock();
  buildGuardian();
  buildCore();
  buildModules(modulePositions());

  window.addEventListener('resize', onResize);
  const loading = document.getElementById('loading');
  if (loading) loading.style.display = 'none';
  animate();
}

init();

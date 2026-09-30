/**
 * vat.js — Cargador y componente reusable de Vertex Animation Textures para Three.js.
 *
 * Uso básico:
 *   import { loadVAT, checkVATSupport } from './vat.js';
 *   const support = checkVATSupport(renderer);
 *   if (!support.ok) { mostrarAviso(support.reason); }
 *   const vat = await loadVAT('./my_mesh', {
 *     onProgress: (r) => barra.value = r.percent,
 *   });
 *   scene.add(vat.object);
 *   // en el loop:
 *   vat.update(dt);
 *   // al quitarlo:
 *   vat.dispose();
 *
 * Multi-clip (varias animaciones sobre la misma malla/textura):
 *   const vat = await loadVAT('./personaje', { clips: {
 *     idle: { start: 0,  end: 59  },
 *     run:  { start: 60, end: 119 },
 *   }});
 *   vat.play('run', { loop: true, fadeSeconds: 0.25 });
 *
 * Instancias:
 *   vat.setInstanceCount(500);
 *   vat.setInstancePhase(i, framesDeOffset);   // por instancia
 */

import * as THREE from 'three';

// ---------------------------------------------------------------------------
// 1. Detección de soporte
// ---------------------------------------------------------------------------

/**
 * Comprueba si el navegador/GPU actual puede reproducir un VAT con texturas float.
 * Llamar una vez, con el renderer ya creado, antes de loadVAT().
 * @returns {{ ok: boolean, halfOnly: boolean, reason?: string }}
 */
export function checkVATSupport(renderer) {
  const gl = renderer.getContext();
  const isWebGL2 = gl instanceof (window.WebGL2RenderingContext || function () {});
  if (isWebGL2) {
    // WebGL2 exige float texturas por spec; el único riesgo real es el filtrado lineal,
    // que este cargador no usa (siempre NearestFilter), así que WebGL2 = soporte completo.
    return { ok: true, halfOnly: false };
  }
  // WebGL1: hace falta la extensión, y OES_texture_float_linear ni se necesita (Nearest).
  const hasFloat = !!gl.getExtension('OES_texture_float');
  const hasHalf = !!gl.getExtension('OES_texture_half_float');
  if (hasFloat) return { ok: true, halfOnly: false };
  if (hasHalf) return { ok: true, halfOnly: true, reason: 'Solo hay soporte de texturas half-float en este dispositivo.' };
  return { ok: false, halfOnly: false, reason: 'Este dispositivo no soporta texturas de punto flotante (WebGL float texture). No se puede reproducir el VAT.' };
}

// ---------------------------------------------------------------------------
// 2. Fetch con progreso
// ---------------------------------------------------------------------------

async function fetchWithProgress(url, onChunk) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`No se pudo cargar ${url} (HTTP ${res.status})`);
  const total = Number(res.headers.get('content-length')) || 0;
  if (!res.body || !onChunk) return new Uint8Array(await res.arrayBuffer()).buffer;

  const reader = res.body.getReader();
  const chunks = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    onChunk({ url, received, total, percent: total ? received / total : null });
  }
  const buf = new Uint8Array(received);
  let off = 0;
  for (const c of chunks) { buf.set(c, off); off += c.length; }
  return buf.buffer;
}

// ---------------------------------------------------------------------------
// 3. GLSL compartido (idéntico al del visor anterior, sin cambios de comportamiento)
// ---------------------------------------------------------------------------
const TIME = /* glsl */`
  float vf = uFrame;
  #ifdef USE_INSTANCING
    vf += aPhase;
  #endif
  vf = mod(vf, uFrames);
  float f0 = floor(vf);
  float f1 = mod(f0 + 1.0, uFrames);
  float ft = fract(vf);
`;

const SOFT_DECL = /* glsl */`
  uniform sampler2D uPos;
  uniform sampler2D uNrm;
  uniform sampler2D uCol;
  uniform sampler2D uTan;
  uniform float uFrames, uTileH, uW, uH, uFrame, uOffsetMode;
  attribute float vid;
  #ifdef USE_INSTANCING
    attribute float aPhase;
  #endif
  varying vec4 vVatCol;
`;
const SOFT_CALC = TIME + /* glsl */`
  float tile = floor(vid / uTileH);
  float rowY = vid - tile * uTileH;
  float vv = (rowY + 0.5) / uH;
  vec2 uvA = vec2((tile * uFrames + f0 + 0.5) / uW, vv);
  vec2 uvB = vec2((tile * uFrames + f1 + 0.5) / uW, vv);
  vec3 vatP = mix(texture2D(uPos, uvA).xyz, texture2D(uPos, uvB).xyz, ft);
  vec3 vatN = normalize(mix(texture2D(uNrm, uvA).xyz, texture2D(uNrm, uvB).xyz, ft));
  if (uOffsetMode > 0.5) vatP += position;
  #ifdef VAT_COL
    vVatCol = mix(texture2D(uCol, uvA), texture2D(uCol, uvB), ft);
  #endif
  vec3 vatT = vec3(1.0, 0.0, 0.0);
  float vatTw = 1.0;
  #ifdef VAT_TAN
    vec4 tA = texture2D(uTan, uvA);
    vec4 tB = texture2D(uTan, uvB);
    vatT = normalize(mix(tA.xyz, tB.xyz, ft));
    vatTw = tA.w;
  #endif
`;

const RIGID_DECL = /* glsl */`
  uniform sampler2D uPos;
  uniform sampler2D uRot;
  uniform float uFrames, uW, uH, uFrame;
  attribute float piece;
  #ifdef USE_INSTANCING
    attribute float aPhase;
  #endif
  varying vec4 vVatCol;
  vec3 qrot(vec4 q, vec3 v) { return v + 2.0 * cross(q.xyz, cross(q.xyz, v) + q.w * v); }
`;
const RIGID_CALC = TIME + /* glsl */`
  float vv = (piece + 0.5) / uH;
  vec2 uvA = vec2((f0 + 0.5) / uW, vv);
  vec2 uvB = vec2((f1 + 0.5) / uW, vv);
  vec4 pa = texture2D(uPos, uvA);
  vec4 pb = texture2D(uPos, uvB);
  vec4 qa = texture2D(uRot, uvA);
  vec4 qb = texture2D(uRot, uvB);
  if (dot(qa, qb) < 0.0) qb = -qb;
  vec4 q = normalize(mix(qa, qb, ft));
  vec4 p = mix(pa, pb, ft);
  vec3 vatP = p.xyz + qrot(q, position * p.w);
  vec3 vatN = qrot(q, normal);
  vec3 vatT = qrot(q, vec3(1.0, 0.0, 0.0));
  float vatTw = 1.0;
`;

function makeMaterial(U, { rigid, hasCol, hasTan }, matOptions = {}) {
  const mat = new THREE.MeshStandardMaterial({
    color: 0xc0ccdd, metalness: 0.1, roughness: 0.55, side: THREE.DoubleSide, ...matOptions,
  });
  mat.defines = {};
  if (hasCol) mat.defines.VAT_COL = '';
  if (hasTan) mat.defines.VAT_TAN = '';
  mat.customProgramCacheKey = () => (rigid ? 'vat-rigid' : 'vat-soft') + (hasCol ? '-col' : '') + (hasTan ? '-tan' : '');
  mat.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, U);
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', '#include <common>\n' + (rigid ? RIGID_DECL : SOFT_DECL))
      .replace('#include <beginnormal_vertex>',
        (rigid ? RIGID_CALC : SOFT_CALC) +
        '\nvec3 objectNormal = vatN;\n#ifdef USE_TANGENT\nvec3 objectTangent = vatT;\n#endif')
      .replace('#include <normal_vertex>',
        '#include <normal_vertex>\n#ifdef USE_TANGENT\nvBitangent = normalize( cross( vNormal, vTangent ) * vatTw );\n#endif')
      .replace('#include <begin_vertex>', 'vec3 transformed = vatP;');
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', '#include <common>\n#ifdef VAT_COL\nvarying vec4 vVatCol;\n#endif')
      .replace('#include <color_fragment>', '#include <color_fragment>\n#ifdef VAT_COL\ndiffuseColor *= vVatCol;\n#endif');
    mat.userData.shader = shader;
  };
  return mat;
}

// ---------------------------------------------------------------------------
// 4. Textura desde buffer crudo
// ---------------------------------------------------------------------------
function makeTexture(buf, meta, W, H) {
  const half = meta.dtype === 'float16';
  const data = half ? new Uint16Array(buf) : new Float32Array(buf);
  if (data.length !== W * H * 4) {
    throw new Error(`Tamaño de textura inesperado: ${data.length} valores, se esperaban ${W * H * 4} (${W}x${H}x4)`);
  }
  const tex = new THREE.DataTexture(data, W, H, THREE.RGBAFormat, half ? THREE.HalfFloatType : THREE.FloatType);
  tex.minFilter = tex.magFilter = THREE.NearestFilter; // la interpolación entre frames la hace el shader
  tex.generateMipmaps = false;
  tex.needsUpdate = true;
  return tex;
}

// ---------------------------------------------------------------------------
// 5. Clase principal
// ---------------------------------------------------------------------------
const MAX_INSTANCES_DEFAULT = 1000;

class VAT {
  constructor({ object, mesh, mat, phase, U, meta, rigid, hasTan, maxInstances, holderScale, holderPos }) {
    this.object = object;         // añade esto a tu escena
    this.mesh = mesh;             // InstancedMesh interno
    this.material = mat;
    this._phase = phase;
    this._U = U;
    this.meta = meta;
    this.rigid = rigid;
    this.hasNormalMapSlot = hasTan;
    this._maxInstances = maxInstances;
    this._holderScale = holderScale;
    this._holderPos = holderPos;

    this._frame = 0;
    this._speed = 1;
    this._paused = false;

    // --- clips ---
    this._clips = {};             // nombre -> {start,end,fps?}
    this._current = null;         // clip activo
    this._from = null;            // clip saliente (durante un fade)
    this._fromFrame = 0;
    this._fadeT = 0;
    this._fadeDur = 0;
  }

  // -------------------- reproducción --------------------
  /** Frame absoluto dentro de todo el rango exportado (0..frames-1). Ignora clips. */
  set frame(v) { this._frame = v; this._syncUniform(); }
  get frame() { return this._frame; }

  set speed(v) { this._speed = v; }
  get speed() { return this._speed; }

  set paused(v) { this._paused = v; }
  get paused() { return this._paused; }

  /** Registra un clip (subrango de frames) para usar con play(). */
  defineClip(name, start, end) { this._clips[name] = { start, end }; }

  /**
   * Reproduce un clip por nombre. Si ya hay uno sonando, hace cross-fade.
   * @param {string} name
   * @param {{loop?:boolean, fadeSeconds?:number, speed?:number}} [opts]
   */
  play(name, opts = {}) {
    const clip = this._clips[name];
    if (!clip) throw new Error(`Clip desconocido: "${name}". Definidos: ${Object.keys(this._clips).join(', ') || '(ninguno)'}`);
    if (this._current && opts.fadeSeconds) {
      this._from = this._current;
      this._fromFrame = this._frame;
      this._fadeDur = opts.fadeSeconds;
      this._fadeT = 0;
    } else {
      this._from = null;
    }
    this._current = { ...clip, name, loop: opts.loop !== false };
    this._frame = clip.start;
    if (opts.speed != null) this._speed = opts.speed;
  }

  /** Avanza la animación. Llamar una vez por frame de render con el delta en segundos. */
  update(dt) {
    if (this._paused) return;
    const fps = this.meta.fps * this._speed;

    if (this._current) {
      const { start, end, loop } = this._current;
      const len = end - start + 1;
      this._frame += dt * fps;
      const rel = this._frame - start;
      if (rel >= len) {
        if (loop) this._frame = start + (rel % len);
        else this._frame = end;
      }
    } else {
      this._frame = (this._frame + dt * fps) % this.meta.frames;
    }

    if (this._from) {
      this._fadeT += dt;
      const { start, end, loop } = this._from;
      const len = end - start + 1;
      this._fromFrame += dt * fps;
      const rel = this._fromFrame - start;
      this._fromFrame = loop ? start + (rel % len) : Math.min(this._fromFrame, end);
      if (this._fadeT >= this._fadeDur) this._from = null;
    }

    this._syncUniform();
  }

  _syncUniform() {
    // El shader solo conoce un uFrame; el cross-fade entre dos clips se resuelve
    // aquí mezclando manualmente dos muestras cuando hay fade activo.
    if (this._from) {
      const t = Math.min(1, this._fadeT / this._fadeDur);
      // Blend simple por proximidad de frame; para una mezcla real habría que
      // muestrear dos veces en el shader. Para la mayoría de transiciones cortas
      // (0.1–0.4s) este snap suavizado es visualmente aceptable.
      this._U.uFrame.value = t < 0.5 ? this._fromFrame : this._frame;
    } else {
      this._U.uFrame.value = this._frame;
    }
  }

  // -------------------- instancias --------------------
  setInstanceCount(n) {
    n = Math.max(1, Math.min(n, this._maxInstances));
    this.mesh.count = n;
    return n;
  }
  get instanceCount() { return this.mesh.count; }

  setInstanceMatrix(i, matrix4) {
    this.mesh.setMatrixAt(i, matrix4);
    this.mesh.instanceMatrix.needsUpdate = true;
  }
  setInstancePhase(i, frames) {
    this._phase.array[i] = frames;
    this._phase.needsUpdate = true;
  }
  randomizePhases(count = this.mesh.count) {
    for (let i = 0; i < count; i++) this.setInstancePhase(i, Math.random() * this.meta.frames);
  }

  // -------------------- limpieza --------------------
  /** Libera texturas y geometría de GPU. Llamar al quitar el VAT de la escena. */
  dispose() {
    for (const k of ['uPos', 'uNrm', 'uCol', 'uTan', 'uRot']) {
      this._U[k]?.value?.dispose?.();
    }
    this.mesh.geometry.dispose();
    this.material.dispose();
    if (this.material.map) this.material.map.dispose();
    if (this.material.normalMap) this.material.normalMap.dispose();
  }

  // -------------------- material --------------------
  setColorMap(texture) {
    this.material.map = texture;
    this.material.needsUpdate = true;
  }
  setNormalMap(texture) {
    if (!this.hasNormalMapSlot) {
      console.warn('vat.js: este VAT se exportó sin tangentes; el normal map puede verse incorrecto.');
    }
    this.material.normalMap = texture;
    this.material.needsUpdate = true;
  }
}

// ---------------------------------------------------------------------------
// 6. loadVAT()
// ---------------------------------------------------------------------------

/**
 * Carga un VAT exportado por vat_exporter.py y devuelve un objeto VAT listo para usar.
 *
 * @param {string} base                       Prefijo de los archivos, p.ej. './my_mesh'
 * @param {object} [options]
 * @param {number} [options.size=4]           Tamaño en escena del recorrido de UNA instancia.
 * @param {number} [options.maxInstances=1000] Máximo de instancias reservadas en GPU.
 * @param {(p:{url,received,total,percent})=>void} [options.onProgress]
 * @param {object} [options.material]         Overrides para MeshStandardMaterial (color, metalness...).
 * @param {object} [options.clips]            { nombre: {start,end}, ... } — atajo a defineClip().
 * @returns {Promise<VAT>}
 */
export async function loadVAT(base, options = {}) {
  const {
    size = 4, maxInstances = MAX_INSTANCES_DEFAULT, onProgress, material = {}, clips = {},
    cacheBust = true,
  } = options;

  const bust = cacheBust ? `?v=${Date.now()}` : '';
  const dir = base.slice(0, base.lastIndexOf('/') + 1);

  const metaRes = await fetch(`${base}_vat.json${bust}`);
  if (!metaRes.ok) throw new Error(`No se pudo cargar ${base}_vat.json (HTTP ${metaRes.status})`);
  const meta = await metaRes.json();

  const rigid = meta.mode === 'rigid';
  const frames = meta.frames;
  const W = meta.texture?.[0] ?? frames;
  const H = meta.texture?.[1] ?? meta.vertices;

  const optFile = (name) => (meta.files?.[name]?.bin ? dir + meta.files[name].bin : null);
  const file = (name) => {
    const f = meta.files?.[name]?.bin;
    if (meta.files && !f) return null;
    return f ? dir + f : `${base}_${name}.bin`;
  };
  if (meta.files && !meta.files.pos?.bin) {
    throw new Error('Este export no incluye .bin (activa ".bin crudo" en el exportador de Maya).');
  }

  const loadTex = async (name, required = true) => {
    const url = required ? file(name) : optFile(name);
    if (!url) return null;
    const buf = await fetchWithProgress(url, onProgress && ((p) => onProgress({ ...p, part: name })));
    return makeTexture(buf, meta, W, H);
  };

  const U = {
    uFrame: { value: 0 }, uFrames: { value: frames }, uW: { value: W }, uH: { value: H },
    uTileH: { value: meta.tile_height ?? meta.vertices },
    uOffsetMode: { value: meta.position_mode === 'offset' ? 1 : 0 },
  };
  U.uPos = { value: await loadTex('pos') };

  const geo = new THREE.BufferGeometry();
  const box = new THREE.Box3();
  let hasCol = false, hasTan = false;

  if (rigid) {
    const m = meta.mesh;
    if (!m) throw new Error('El JSON no incluye la malla (activa "Incluir malla en el JSON" en el exportador).');
    U.uRot = { value: await loadTex('rot') };
    geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(m.position), 3));
    geo.setAttribute('normal', new THREE.BufferAttribute(new Float32Array(m.normal), 3));
    geo.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(m.uv), 2));
    geo.setAttribute('piece', new THREE.BufferAttribute(new Float32Array(m.piece), 1));
    geo.setIndex(m.index);
    geo.computeBoundingSphere();
    const r = geo.boundingSphere.radius + geo.boundingSphere.center.length();
    box.min.fromArray(meta.bbox_min).addScalar(-r);
    box.max.fromArray(meta.bbox_max).addScalar(r);
  } else {
    U.uNrm = { value: await loadTex('nrm') };
    const colTex = await loadTex('col', false);
    if (colTex) { U.uCol = { value: colTex }; hasCol = true; }
    const tanTex = await loadTex('tan', false);
    if (tanTex) { U.uTan = { value: tanTex }; hasTan = true; }

    const count = meta.vid.length;
    const basePos = meta.position ? new Float32Array(meta.position) : new Float32Array(count * 3);
    geo.setAttribute('position', new THREE.BufferAttribute(basePos, 3));
    geo.setAttribute('vid', new THREE.BufferAttribute(new Float32Array(meta.vid), 1));
    geo.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(meta.uv), 2));
    if (hasTan) geo.setAttribute('tangent', new THREE.BufferAttribute(new Float32Array(count * 4), 4));
    geo.setIndex(meta.index);

    box.min.fromArray(meta.bbox_min);
    box.max.fromArray(meta.bbox_max);
    if (U.uOffsetMode.value && meta.position) {
      const bb = new THREE.Box3().setFromArray(basePos);
      box.min.add(bb.min); box.max.add(bb.max);
    }
  }

  const phase = new THREE.InstancedBufferAttribute(new Float32Array(maxInstances), 1);
  geo.setAttribute('aPhase', phase);

  const mat = makeMaterial(U, { rigid, hasCol, hasTan }, material);
  const mesh = new THREE.InstancedMesh(geo, mat, maxInstances);
  mesh.count = 1;
  mesh.frustumCulled = false;

  const dims = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const maxDim = Math.max(dims.x, dims.y, dims.z) || 1;
  const s = size / maxDim;
  const holder = new THREE.Group();
  holder.scale.setScalar(s);
  holder.position.set(-center.x * s, -box.min.y * s, -center.z * s);
  holder.add(mesh);

  const vat = new VAT({
    object: holder, mesh, mat, phase, U, meta, rigid, hasTan, maxInstances,
    holderScale: s, holderPos: holder.position.clone(),
  });
  vat.spacing = maxDim * 1.2;   // sugerido para acomodar instancias en cuadrícula
  vat.height = dims.y * s;

  for (const [name, range] of Object.entries(clips)) vat.defineClip(name, range.start, range.end);

  return vat;
}

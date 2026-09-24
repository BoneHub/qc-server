// BoneHub Quality Check -- browser review page.
//
// Signs in with a reviewer's API key, leases subjects from /api/v1 the way the 3D Slicer
// extension does, shows them with NiiVue, and sends the verdict back: each label under review
// accepted or rejected -- it needs correcting, or should not be there -- and bones the
// segmentation lacks reported missing. It cannot edit a segmentation: the verdict judges the
// segmentation as it is (use_stored_segmentation), and what it rejects goes to an editor, in
// 3D Slicer. Verdicts wait on the server until its administrator approves the subject.
//
// Every request says that it comes from a reviewer, so the server refuses the key of an
// account that is not one.
//
// Two NiiVue canvases. The 3D view renders the label volume alone; the slice view shows the
// image with the labels over it. One canvas cannot do both: NiiVue's 3D rendering draws the
// labels over the rendered image, so the CT would hide the bones.

import { Niivue, NVImage, SHOW_RENDER, SLICE_TYPE } from "./vendor/niivue-0.69.0.min.js";

const KEY_STORAGE = "bonehub_qc_review_key";
const PREFS_STORAGE = "bonehub_qc_review_prefs";

// The role this page works in, and the header that names it.
const ROLE_HEADER = "X-Client-Role";
const ROLE = "reviewer";

// Short names for the Subject_info label statuses; the server's full wording is the tooltip.
const STATUS_SHORT = { 0: "absent", 1: "unreviewed", 2: "reviewed" };

// Why a label is rejected, until the server says it in its own words.
const REASON_TEXT = { quality: "needs correction", absent: "should not be there", missing: "is missing" };

// CT windows, [min, max] in Hounsfield units.
const WINDOWS = { bone: [-450, 1050], soft: [-160, 240] };

const ACCESS_TEXT = {
  image_and_segmentation: "image + segmentation",
  segmentation: "segmentation only",
  image: "image only",
};

// Starting 3D viewpoint: from the front, slightly above.
const INITIAL_AZIMUTH = 180;
const INITIAL_ELEVATION = 15;

// Surface shading of the 3D view (NiiVue's gradient lighting, 0..1). Without it a bone renders
// as a flat silhouette with no depth.
const RENDER_ILLUMINATION = 0.6;

const NIFTI_INTENT_LABEL = 1002;
const NIFTI_TYPE_FLOAT32 = 16;

// The most voxels each canvas is given. NiiVue keeps several full-size textures per volume, and
// in testing a whole-body CT of 450 million voxels failed to display at all while 300 million
// still worked. A larger scan is shown with every second voxel along its finest axes, and the
// page says so. The 3D view shows shapes, so it gets a smaller share.
const MAX_SLICE_VOXELS = 256e6;
const MAX_3D_VOXELS = 64e6;

const $ = (id) => document.getElementById(id);

const state = {
  key: null,
  info: null, // what /api/v1/ping said about this reviewer
  statusText: {}, // label status -> the server's wording
  reasonText: { ...REASON_TEXT }, // reason to reject -> the server's wording
  labelNames: [], // every BoneHub label, to report a missing bone by
  handout: null,
  segments: [], // the handout's segments, in label-value (anatomical) order
  byNumber: new Map(), // segment number -> segment
  hidden: new Set(), // segment numbers the reviewer hid
  solo: null, // segment number shown alone, or null
  labels: new Map(), // label name -> what the quality check has made of it (the handout's labels)
  verdicts: new Map(), // label name -> "accept" or "reject", the reviewer's verdict
  reasons: new Map(), // label name -> why a label in the segmentation is rejected: "quality" or "absent"
  missing: new Set(), // bones reported missing that the subject does not list
  // image and seg2d are on the slice view, seg3d on the 3D view. segRef is the slice view's
  // mask untouched by hiding; it is on no canvas and answers "which label is here".
  volumes: { image: null, segRef: null, seg2d: null, seg3d: null },
  sliceFactors: [1, 1, 1], // every n-th voxel of the scan the slice view shows, per axis
  sliceSpacing: null, // [original mm, shown mm] per axis, when the slice view is reduced
  loaded: false, // the subject's views were built, so what is ticked was seen
  busy: false,
};

let nv3d = null;
let nv2d = null;
let prefs = loadPrefs();

// ------------------------------------------------------------------ storage
function storageGet(name, key) {
  try { return window[name].getItem(key); } catch (e) { return null; }
}
function storageSet(name, key, value) {
  try { window[name].setItem(key, value); } catch (e) { /* private mode, or storage blocked */ }
}
function storageRemove(name, key) {
  try { window[name].removeItem(key); } catch (e) { /* ignore */ }
}

const PLANES = {
  multi: SLICE_TYPE.MULTIPLANAR,
  axial: SLICE_TYPE.AXIAL,
  coronal: SLICE_TYPE.CORONAL,
  sagittal: SLICE_TYPE.SAGITTAL,
};

function loadPrefs() {
  const defaults = {
    layout: "both",
    plane: "multi",
    opacity: 45,
    outline: false,
    distinctColors: false,
    autoNext: false,
  };
  try {
    return { ...defaults, ...JSON.parse(storageGet("localStorage", PREFS_STORAGE) || "{}") };
  } catch (e) {
    return defaults;
  }
}
function savePrefs() {
  storageSet("localStorage", PREFS_STORAGE, JSON.stringify(prefs));
}

// ---------------------------------------------------------------- transport
class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function errorDetail(data, status) {
  const detail = data && data.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
  return `The server answered HTTP ${status}.`;
}

async function api(method, path, { json, form } = {}) {
  const headers = { "X-API-Key": state.key, [ROLE_HEADER]: ROLE, Accept: "application/json" };
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form !== undefined) {
    body = form; // the browser sets the multipart boundary itself
  }
  let response;
  try {
    response = await fetch(path, { method, headers, body });
  } catch (e) {
    throw new ApiError("The server cannot be reached. Check your connection and try again.", 0);
  }
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { detail: text }; }
  if (!response.ok) throw new ApiError(errorDetail(data, response.status), response.status);
  return data;
}

// Downloads a file into memory, reporting progress. Images of a whole-body CT run to
// hundreds of megabytes, so the buffer is allocated once from Content-Length rather than
// collected in pieces and copied.
async function download(url, what, onProgress) {
  let response;
  try {
    response = await fetch(url, { headers: { "X-API-Key": state.key, [ROLE_HEADER]: ROLE } });
  } catch (e) {
    throw new ApiError(`The ${what} could not be downloaded: the server cannot be reached.`, 0);
  }
  if (!response.ok) {
    let data = null;
    try { data = await response.json(); } catch (e) { /* not JSON */ }
    throw new ApiError(errorDetail(data, response.status), response.status);
  }
  // Behind a compressing proxy, Content-Length counts compressed bytes; progress is then unknown.
  const encoded = response.headers.has("Content-Encoding");
  const total = encoded ? 0 : Number(response.headers.get("Content-Length")) || 0;
  if (!response.body) {
    const buffer = await response.arrayBuffer();
    onProgress(buffer.byteLength, buffer.byteLength);
    return buffer;
  }
  const reader = response.body.getReader();
  let target = total ? new Uint8Array(total) : null;
  const chunks = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (target && received + value.length > target.length) {
      chunks.push(target.subarray(0, received)); // longer than announced: collect instead
      target = null;
    }
    if (target) target.set(value, received);
    else chunks.push(value);
    received += value.length;
    onProgress(received, total);
  }
  if (target) return received === target.length ? target.buffer : target.slice(0, received).buffer;
  const joined = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.length;
  }
  return joined.buffer;
}

// -------------------------------------------------------------------- DOM
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "className") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style") Object.assign(node.style, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

const ICONS = {
  eye: "M1 8s2.5-5 7-5 7 5 7 5-2.5 5-7 5-7-5-7-5Zm7 3a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z",
  eyeOff: "M2 2l12 12M6.2 3.3A7 7 0 0 1 8 3c4.5 0 7 5 7 5a12 12 0 0 1-2 2.6M10.1 10.1A3 3 0 0 1 5.9 5.9M4 4.6C2.2 5.8 1 8 1 8s2.5 5 7 5a6.8 6.8 0 0 0 3-.7",
  solo: "M8 1v3M8 12v3M1 8h3M12 8h3M8 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z",
};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

function banner(text, kind) {
  return el("div", { className: `banner ${kind}` }, text);
}

function nextFrame() {
  // Two frames, so a status message is painted before the main thread is busy parsing.
  return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}

function formatBytes(bytes) {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`;
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1e3))} kB`;
}

// Resolves true when the reviewer presses the OK button, false on Cancel or Escape.
function ask(title, body, okLabel, { danger = false, cancelLabel = "Cancel" } = {}) {
  return new Promise((resolve) => {
    const dialog = $("askDialog");
    $("askTitle").textContent = title;
    $("askBody").replaceChildren(...body);
    $("askOk").textContent = okLabel;
    $("askOk").className = danger ? "danger" : "primary";
    $("askCancel").textContent = cancelLabel;
    dialog.returnValue = "";
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.showModal();
  });
}

// ------------------------------------------------------------------ sign in
function showLogin(message) {
  $("app").hidden = true;
  $("login").hidden = false;
  $("loginError").hidden = !message;
  $("loginError").textContent = message || "";
  $("apiKey").focus();
}

async function signIn(key, remember) {
  state.key = key;
  const info = await api("GET", "/api/v1/ping");
  state.info = info;
  storageRemove("sessionStorage", KEY_STORAGE);
  storageRemove("localStorage", KEY_STORAGE);
  storageSet(remember ? "localStorage" : "sessionStorage", KEY_STORAGE, key);
  try {
    const labels = await api("GET", "/api/v1/labels");
    state.statusText = labels.label_status_values || {};
    state.reasonText = { ...REASON_TEXT, ...(labels.reject_reasons || {}) };
    state.labelNames = Object.keys(labels.label_name_to_value || {}).sort();
  } catch (e) {
    state.statusText = {};
  }
  $("labelNames").replaceChildren(...state.labelNames.map((name) => el("option", { value: name })));

  $("login").hidden = true;
  $("app").hidden = false;
  $("whoName").textContent = info.user;
  $("whoAccess").textContent = `sent: ${ACCESS_TEXT[info.data_access] || info.data_access}`;
  $("whoAccess").title = "What your account is sent of each subject. Your administrator sets this.";

  if (!webgl2Available()) {
    showEmpty(
      "This browser cannot show the images",
      "It has WebGL 2 switched off or does not support it. Use a current Chrome, Edge, Firefox or Safari.",
    );
    $("nextBtn").disabled = true;
    return;
  }
  await createViewers();
  await resumeOrWait();
}

function signOut() {
  storageRemove("sessionStorage", KEY_STORAGE);
  storageRemove("localStorage", KEY_STORAGE);
  window.location.reload();
}

// --------------------------------------------------------------- the viewers
function webgl2Available() {
  try {
    return !!document.createElement("canvas").getContext("webgl2");
  } catch (e) {
    return false;
  }
}

async function createViewers() {
  if (nv3d) return;
  const common = {
    logLevel: "error", // NiiVue warns about each Slicer key in a .seg.nrrd header it does not use
    dragAndDropEnabled: false,
    loadingText: "",
    showLegend: false,
    isColorbar: false,
    crosshairColor: [1, 0.78, 0.2, 1],
  };
  nv3d = new Niivue({ ...common, show3Dcrosshair: true, isOrientCube: true, backColor: [0.04, 0.045, 0.05, 1] });
  nv2d = new Niivue({
    ...common,
    multiplanarShowRender: SHOW_RENDER.NEVER,
    // The patient's right on the screen's left, as in 3D Slicer and on a radiology workstation.
    isRadiologicalConvention: true,
    backColor: [0, 0, 0, 1],
  });
  await nv3d.attachToCanvas($("view3d"));
  await nv2d.attachToCanvas($("view2d"));
  nv3d.setSliceType(SLICE_TYPE.RENDER);
  nv2d.setSliceType(PLANES[prefs.plane] ?? SLICE_TYPE.MULTIPLANAR);
  // Voxels drawn as voxels: a mask's edge is what is being judged, so it must not be blurred.
  nv2d.setInterpolation(true);
  nv3d.onLocationChange = showLocation;
  nv2d.onLocationChange = showLocation;
}

function clearViewers() {
  for (const nv of [nv3d, nv2d]) {
    if (!nv) continue;
    nv.broadcastTo([], {});
    for (let i = nv.volumes.length - 1; i >= 0; i--) nv.removeVolumeByIndex(i);
    nv.drawScene();
  }
  state.volumes = { image: null, segRef: null, seg2d: null, seg3d: null };
  state.sliceFactors = [1, 1, 1];
  state.sliceSpacing = null;
  state.loaded = false;
  for (const id of ["locLabel", "locValue", "locMM"]) $(id).textContent = "";
}

function isCT() {
  const modality = state.handout && state.handout.dataset_info && state.handout.dataset_info.modality;
  return String(modality || "").toUpperCase() === "CT";
}

function segmentShown(segment) {
  return state.solo === null ? !state.hidden.has(segment.number) : state.solo === segment.number;
}

// The colour a segment is drawn in, 0..255. By default the one its file records, which is
// also what 3D Slicer shows. That scheme shades a bone's neighbours alike -- L4 and L5 come
// out as two greens -- so "distinct colours" steps the hue by the golden angle along the
// anatomical order instead, which puts every pair of neighbours far apart.
function displayColor(segment) {
  if (!prefs.distinctColors) return segment.color.map((c) => Math.round(c * 255));
  const index = state.segments.indexOf(segment);
  return hslToRgb((index * 0.381966) % 1, 0.7, 0.55);
}

function hslToRgb(h, s, l) {
  const f = (n) => {
    const k = (n + h * 12) % 12;
    return Math.round(255 * (l - s * Math.min(l, 1 - l) * Math.max(-1, Math.min(k - 3, 9 - k, 1))));
  };
  return [f(0), f(8), f(4)];
}

// NiiVue's label lookup table: segment number -> colour, and alpha 0 for what is hidden.
function labelColormap() {
  const cm = { R: [0], G: [0], B: [0], A: [0], I: [0], labels: [""] };
  for (const segment of state.segments) {
    const [red, green, blue] = displayColor(segment);
    cm.R.push(red);
    cm.G.push(green);
    cm.B.push(blue);
    cm.A.push(segmentShown(segment) ? 255 : 0);
    cm.I.push(segment.number);
    cm.labels.push(segment.label);
  }
  return cm;
}

async function buildViews(key, imageBuffer, segBuffer) {
  const hasImage = !!imageBuffer;
  // The image and the mask share one voxel grid, so whichever arrives first sets the reduction.
  let sliceFactors = null;
  if (imageBuffer) {
    let image = await NVImage.loadFromUrl({ url: imageBuffer, name: `${key}.nii.gz`, colormap: "gray" });
    sliceFactors = subsampleFactors(image, MAX_SLICE_VOXELS);
    image = await resampled(image, sliceFactors, `${key}_image`, "gray");
    state.volumes.image = image;
    nv2d.addVolume(image);
    applyWindow();
  }
  if (segBuffer && state.segments.length) {
    const mask = await NVImage.loadFromUrl({ url: segBuffer, name: `${key}.seg.nrrd`, colormap: "gray" });
    sliceFactors = sliceFactors || subsampleFactors(mask, MAX_SLICE_VOXELS);
    const segRef = await resampled(mask, sliceFactors, `${key}_labels`);
    // The slice view hides labels by editing its copy, so it needs one of its own.
    const seg2d = await resampled(segRef, [1, 1, 1], `${key}_labels`, "gray", true);
    seg2d.opacity = hasImage ? prefs.opacity / 100 : 1;
    // NiiVue draws label outlines only for a volume marked as a label map, which a .seg.nrrd is not.
    seg2d.hdr.intent_code = NIFTI_INTENT_LABEL;
    seg2d.setColormapLabel(labelColormap());
    state.volumes.segRef = segRef;
    state.volumes.seg2d = seg2d;
    nv2d.addVolume(seg2d);

    const seg3d = await resampled(mask, subsampleFactors(mask, MAX_3D_VOXELS), `${key}_labels_3d`, "gray", true);
    seg3d.setColormapLabel(labelColormap());
    state.volumes.seg3d = seg3d;
    nv3d.addVolume(seg3d);
    await nv3d.setVolumeRenderIllumination(RENDER_ILLUMINATION);
  }
  state.sliceFactors = sliceFactors || [1, 1, 1];
  const shown = state.volumes.image || state.volumes.segRef;
  if (shown && state.sliceFactors.some((f) => f > 1)) {
    const spacing = [1, 2, 3].map((d) => Math.abs(shown.hdr.pixDims[d]));
    state.sliceSpacing = spacing.map((mm, axis) => [mm / state.sliceFactors[axis], mm]);
    $("subjectNotes").append(
      banner(
        `This scan is too large to show in full here, so the slices leave out voxels along its finest ` +
          `axes: ${formatSpacing(1)} instead of ${formatSpacing(0)}. Small errors can be missed. ` +
          "A verdict you send says so in its comment.",
        "note",
      ),
    );
  }
  // Without labels the 3D canvas stays empty. The image is not rendered in 3D instead: the
  // window that suits the slices would render a CT as a solid block of soft tissue.
  nv2d.opts.atlasOutline = prefs.outline && hasImage ? 1 : 0;
  if (state.volumes.seg2d) refreshSliceLabels();
  // Clicking in either view moves the crosshair in both.
  if (state.volumes.seg3d && nv2d.volumes.length) {
    nv3d.broadcastTo(nv2d, { crosshair: true });
    nv2d.broadcastTo(nv3d, { crosshair: true });
  }
  applyLayout();
  updateToolbar();
  resetView();
}

function applyWindow() {
  const image = state.volumes.image;
  if (!image) return;
  const preset = isCT() ? $("windowPreset").value : "auto";
  if (preset === "auto") {
    image.cal_min = image.robust_min;
    image.cal_max = image.robust_max;
  } else {
    [image.cal_min, image.cal_max] = WINDOWS[preset];
  }
  nv2d.updateGLVolume();
}

// ---------------------------------------------------------- large volumes
// The voxel size of the scan (which 0) or of what the slices show (which 1), e.g. "0.98 × 0.98 × 1.2 mm".
function formatSpacing(which) {
  return `${state.sliceSpacing.map((pair) => +pair[which].toFixed(2)).join(" × ")} mm`;
}

// The reviewer's comment, with a note when the scan was seen at reduced resolution: the verdict
// is then about a coarser picture than the one the dataset holds.
function commentToSend() {
  const comment = $("comment").value.trim();
  if (!state.sliceSpacing) return comment || null;
  const note = `[Reviewed in the browser at ${formatSpacing(1)}; the scan is ${formatSpacing(0)}.]`;
  return comment ? `${comment} ${note}` : note;
}

// Steps [x, y, z] that bring a volume under `budget` voxels: the finest axes are halved first,
// alike axes (usually the two in-plane ones) together, so the voxels stay as even as they were.
function subsampleFactors(volume, budget) {
  const dims = [1, 2, 3].map((d) => volume.hdr.dims[d]);
  const spacing = [1, 2, 3].map((d) => Math.abs(volume.hdr.pixDims[d]) || 1);
  const factors = [1, 1, 1];
  const count = () => dims.reduce((n, size, axis) => n * Math.ceil(size / factors[axis]), 1);
  while (count() > budget) {
    const current = spacing.map((mm, axis) => mm * factors[axis]);
    const finest = Math.min(...current);
    for (let axis = 0; axis < 3; axis++) if (current[axis] <= finest * 1.01) factors[axis] *= 2;
  }
  return factors;
}

// The volume with every n-th voxel along each axis, as a new NVImage on the matching grid;
// the volume itself when there is nothing to leave out, unless `copy` asks for a copy. The
// voxels keep what the original shows: an intensity scaling in the header is applied to them.
async function resampled(volume, factors, name, colormap = "gray", copy = false) {
  const [fx, fy, fz] = factors;
  if (fx === 1 && fy === 1 && fz === 1 && !copy) return volume;
  const hdr = volume.hdr;
  const [nx, ny] = [hdr.dims[1], hdr.dims[2]];
  const [mx, my, mz] = [Math.ceil(nx / fx), Math.ceil(ny / fy), Math.ceil(hdr.dims[3] / fz)];
  const slope = hdr.scl_slope || 1;
  const inter = hdr.scl_inter || 0;
  const scaled = slope !== 1 || inter !== 0;
  const source = volume.img;
  const voxels = scaled ? new Float32Array(mx * my * mz) : new source.constructor(mx * my * mz);
  let out = 0;
  for (let k = 0; k < mz; k++) {
    for (let j = 0; j < my; j++) {
      const row = (k * fz * ny + j * fy) * nx;
      if (fx === 1 && !scaled) {
        voxels.set(source.subarray(row, row + nx), out);
        out += nx;
        continue;
      }
      for (let i = 0; i < mx; i++) voxels[out++] = scaled ? source[row + i * fx] * slope + inter : source[row + i * fx];
    }
  }
  const a = hdr.affine;
  const affine = [0, 1, 2].flatMap((r) => [a[r][0] * fx, a[r][1] * fy, a[r][2] * fz, a[r][3]]).concat([0, 0, 0, 1]);
  const spacing = [1, 2, 3].map((d, axis) => Math.abs(hdr.pixDims[d]) * factors[axis]);
  const bytes = NVImage.createNiftiArray(
    [mx, my, mz],
    spacing,
    affine,
    scaled ? NIFTI_TYPE_FLOAT32 : hdr.datatypeCode,
    voxels,
  );
  return NVImage.loadFromUrl({ url: bytes.buffer, name: `${name}.nii`, colormap });
}

// Redraws only the slice view's label layer. NiiVue's updateGLVolume uploads every layer again,
// the image included, which for a large CT takes seconds; refreshLayers -- internal to NiiVue,
// but the version is pinned -- redoes the one layer.
function refreshSliceLabels() {
  const volume = state.volumes.seg2d;
  if (!volume) return;
  const layer = nv2d.volumes.indexOf(volume);
  if (layer > 0 && typeof nv2d.refreshLayers === "function") {
    nv2d.refreshLayers(volume, layer);
    nv2d.drawScene();
  } else {
    nv2d.updateGLVolume();
  }
}

// Hiding or recolouring labels. The list is updated at once and painted before the views,
// which can take a second on a large scan; clicks in between are folded into one update.
let labelUpdatePending = false;
function applyLabelColors() {
  updateLabelRows();
  if (labelUpdatePending) return;
  labelUpdatePending = true;
  nextFrame().then(() => {
    labelUpdatePending = false;
    if (!state.volumes.seg2d && !state.volumes.seg3d) return;
    syncSliceMask();
    if (state.volumes.seg2d) {
      state.volumes.seg2d.setColormapLabel(labelColormap());
      refreshSliceLabels();
    }
    if (state.volumes.seg3d) {
      state.volumes.seg3d.setColormapLabel(labelColormap());
      nv3d.updateGLVolume();
    }
  });
}

// NiiVue outlines labels by voxel value, whatever the colour table says, so a hidden label
// would keep its outline on the slices. The slice view's copy of the mask therefore has the
// voxels of hidden labels set to 0, restored from segRef, which is never changed.
function syncSliceMask() {
  const source = state.volumes.segRef;
  const target = state.volumes.seg2d;
  if (!source || !target || source.img.length !== target.img.length) return;
  const shown = new Uint8Array(Math.max(0, ...state.segments.map((s) => s.number)) + 1);
  for (const segment of state.segments) shown[segment.number] = segmentShown(segment) ? 1 : 0;
  const from = source.img;
  const to = target.img;
  for (let v = 0; v < from.length; v++) {
    const number = from[v];
    to[v] = number !== 0 && shown[number] === 1 ? number : 0;
  }
}

function applyLayout() {
  const has3d = !state.handout || !!state.volumes.seg3d;
  const layout = has3d ? prefs.layout : "slices";
  $("views").dataset.layout = layout;
  for (const button of document.querySelectorAll("#toolbar [data-layout]")) {
    button.setAttribute("aria-pressed", String(button.dataset.layout === layout));
    button.disabled = !has3d && button.dataset.layout !== "slices";
  }
  $("view2dTag").textContent = state.volumes.image ? "Slices" : "Slices · labels";
}

function updateToolbar() {
  const both = !!(state.volumes.image && state.volumes.seg2d);
  $("overlayTools").hidden = !both;
  $("windowTools").hidden = !(state.volumes.image && isCT());
  $("showAllBtn").hidden = !state.volumes.seg2d;
  $("colorsBtn").hidden = !state.volumes.seg2d;
  $("opacity").value = prefs.opacity;
  $("outlineBtn").setAttribute("aria-pressed", String(!!prefs.outline));
  $("colorsBtn").setAttribute("aria-pressed", String(!!prefs.distinctColors));
}

// A segment's bounding box in the voxels of the slice view, which may show every n-th voxel
// of the file the extents were measured in.
function shownExtent(segment) {
  if (!segment.extent) return null;
  return segment.extent.map((index, n) => Math.floor(index / state.sliceFactors[Math.floor(n / 2)]));
}

// The centre of all labels' bounding boxes in world millimetres, or null.
function labelsCentre() {
  const volume = state.volumes.segRef;
  const extents = state.segments.map(shownExtent).filter(Boolean);
  if (!volume || !extents.length) return null;
  const box = [0, 1, 2].map((axis) => [
    Math.min(...extents.map((e) => e[2 * axis])),
    Math.max(...extents.map((e) => e[2 * axis + 1])),
  ]);
  return voxelToMM(volume, box.map(([low, high]) => (low + high) / 2));
}

function resetView() {
  for (const nv of [nv2d, nv3d]) {
    if (!nv || !nv.volumes.length) continue;
    nv.scene.pan2Dxyzmm = [0, 0, 0, 1];
    nv.scene.volScaleMultiplier = 1;
    nv.scene.crosshairPos = [0.5, 0.5, 0.5];
  }
  if (nv3d && nv3d.volumes.length) nv3d.setRenderAzimuthElevation(INITIAL_AZIMUTH, INITIAL_ELEVATION);
  // Land on the bones rather than on the middle of the scan, which may be empty.
  const centre = labelsCentre();
  if (centre) {
    moveCrosshair(centre);
    return;
  }
  for (const nv of [nv2d, nv3d]) if (nv && nv.volumes.length) nv.drawScene();
  if (nv2d && nv2d.volumes.length) nv2d.createOnLocationChange();
}

// World (RAS) millimetres of a voxel given in the file's own index order.
function voxelToMM(volume, [i, j, k]) {
  const a = volume.hdr.affine;
  return [0, 1, 2].map((r) => a[r][0] * i + a[r][1] * j + a[r][2] * k + a[r][3]);
}

function moveCrosshair(mm) {
  for (const nv of [nv2d, nv3d]) {
    if (!nv || !nv.volumes.length) continue;
    // True world coordinates, as NiiVue reports locations. Without the flag NiiVue maps through
    // its axis-aligned stand-in for the volume, which misplaces the point in an oblique scan.
    nv.scene.crosshairPos = nv.mm2frac(mm, 0, true);
    nv.drawScene();
  }
  const reporter = nv2d && nv2d.volumes.length ? nv2d : nv3d;
  if (reporter && reporter.volumes.length) reporter.createOnLocationChange();
}

// A voxel of the segment near the middle of its bounding box: the middle of the box itself
// can miss a curved bone such as a rib. Searches the middle slice first, then outwards.
function findVoxel(volume, segment) {
  const dims = volume.hdr.dims;
  const [nx, ny, nz] = [dims[1], dims[2], dims[3]];
  const img = volume.img;
  const [i0, i1, j0, j1, k0, k1] = shownExtent(segment) || [0, nx - 1, 0, ny - 1, 0, nz - 1];
  const ci = (i0 + i1) / 2;
  const cj = (j0 + j1) / 2;
  const kMid = Math.round((k0 + k1) / 2);
  for (let step = 0; step <= Math.max(kMid - k0, k1 - kMid); step++) {
    for (const k of step === 0 ? [kMid] : [kMid - step, kMid + step]) {
      if (k < k0 || k > k1) continue;
      let best = null;
      let bestDistance = Infinity;
      for (let j = j0; j <= j1; j++) {
        const row = (k * ny + j) * nx;
        for (let i = i0; i <= i1; i++) {
          if (img[row + i] !== segment.number) continue;
          const distance = (i - ci) ** 2 + (j - cj) ** 2;
          if (distance < bestDistance) {
            bestDistance = distance;
            best = [i, j, k];
          }
        }
      }
      if (best) return best;
    }
  }
  return null;
}

function focusSegment(segment) {
  const volume = state.volumes.segRef;
  if (!volume) return;
  const voxel = findVoxel(volume, segment);
  if (voxel) moveCrosshair(voxelToMM(volume, voxel));
}

function valueAt(volume, mm) {
  const [x, y, z] = volume.mm2vox([mm[0], mm[1], mm[2]]);
  const dims = volume.dimsRAS;
  if (x < 0 || y < 0 || z < 0 || x >= dims[1] || y >= dims[2] || z >= dims[3]) return null;
  return volume.getValue(x, y, z);
}

function showLocation(data) {
  if (!state.handout || !data || !data.mm) return;
  const mm = data.mm;
  const seg = state.volumes.segRef; // still holds the labels hidden from view
  let labelText = "";
  if (seg) {
    const number = valueAt(seg, mm);
    if (number !== null) {
      const segment = state.byNumber.get(Math.round(number));
      labelText = segment ? segment.label : Math.round(number) === 0 ? "background" : `segment ${Math.round(number)}`;
    }
  }
  let valueText = "";
  if (state.volumes.image) {
    const value = valueAt(state.volumes.image, mm);
    if (value !== null && Number.isFinite(value)) valueText = isCT() ? `${Math.round(value)} HU` : `value ${+value.toFixed(2)}`;
  }
  $("locLabel").textContent = labelText;
  $("locValue").textContent = valueText;
  $("locMM").textContent = `RAS ${mm[0].toFixed(1)}, ${mm[1].toFixed(1)}, ${mm[2].toFixed(1)} mm`;
}

// ---------------------------------------------------------- empty / loading
function showEmpty(title, text, message) {
  $("emptyTitle").textContent = title || "No subject open";
  $("emptyText").textContent = text || "Ask for the next subject when you are ready to review.";
  $("emptyMessage").replaceChildren(...(message ? [message] : []));
  $("nextBtn").hidden = false;
  $("emptyState").hidden = false;
  $("loadingState").hidden = true;
}

function showProgress(title, received, total) {
  $("emptyState").hidden = true;
  $("loadingState").hidden = false;
  $("loadingTitle").textContent = title;
  const known = total > 0;
  $("loadingProgress").classList.toggle("indeterminate", !known);
  $("loadingBar").style.width = known ? `${Math.min(100, (100 * received) / total).toFixed(1)}%` : "";
  $("loadingDetail").textContent =
    received === undefined ? "" : known ? `${formatBytes(received)} of ${formatBytes(total)}` : formatBytes(received);
}

function hideLoading() {
  $("loadingState").hidden = true;
}

async function showHeld(excluding) {
  let held = [];
  try {
    held = await api("GET", "/api/v1/assignments");
  } catch (e) {
    held = [];
  }
  held = held.filter((a) => a.assignment_id !== excluding);
  const list = $("heldList");
  list.hidden = !held.length;
  list.replaceChildren(
    el("div", { className: "muted", style: { fontSize: "12px" } }, "You also hold:"),
    ...held.map((a) =>
      el("button", { className: "small", onclick: () => openAssignment(a.assignment_id) }, `Open ${a.subject_key}`),
    ),
  );
  return held;
}

async function resumeOrWait() {
  const held = await showHeld(null);
  if (held.length) {
    // Pick up where the reviewer left off, e.g. after closing the tab.
    const oldest = [...held].sort((a, b) => (a.assigned_at < b.assigned_at ? -1 : 1))[0];
    await openAssignment(oldest.assignment_id);
  } else {
    showEmpty();
  }
}

// ---------------------------------------------------------------- subjects
async function nextSubject() {
  setBusy(true);
  try {
    const handout = await api("POST", "/api/v1/subjects/next");
    await openSubject(handout);
  } catch (error) {
    if (error.status === 404) showEmpty("Nothing to review", error.message);
    else showEmpty("Could not get a subject", error.message, banner(error.message, "err"));
  } finally {
    setBusy(false);
  }
}

async function openAssignment(assignmentId) {
  setBusy(true);
  try {
    await openSubject(await api("GET", `/api/v1/assignments/${encodeURIComponent(assignmentId)}`));
  } catch (error) {
    showEmpty("Could not open the subject", "", banner(error.message, "err"));
  } finally {
    setBusy(false);
  }
}

async function openSubject(handout) {
  clearViewers();
  state.handout = handout;
  state.segments = [...(handout.segments || [])].sort((a, b) => a.value - b.value);
  state.byNumber = new Map(state.segments.map((s) => [s.number, s]));
  state.hidden = new Set();
  state.solo = null;
  state.labels = new Map((handout.labels || []).map((label) => [label.name, label]));
  state.verdicts = new Map();
  state.reasons = new Map();
  state.missing = new Set();
  // Every label under review starts accepted, and the reviewer rejects what is wrong -- but a
  // segmentation that cannot be accepted as it is starts rejected, and one not sent unjudged.
  for (const label of state.labels.values()) {
    if (label.state !== "pending") continue;
    if (label.painted && handout.stored_segmentation_issue) {
      state.verdicts.set(label.name, "reject");
      state.reasons.set(label.name, "quality");
    } else if (!label.painted || handout.has_segmentation) {
      state.verdicts.set(label.name, "accept");
    }
  }
  $("comment").value = "";
  $("missingInput").value = "";
  $("verdictMessage").replaceChildren();
  $("heldList").hidden = true;
  renderSubject();
  renderLabels();
  updateVerdictButtons();

  const key = handout.subject_key;
  let imageBuffer = null;
  let segBuffer = null;
  try {
    if (handout.has_image) {
      imageBuffer = await download(handout.image_url, "image", (got, total) =>
        showProgress(`Downloading the image of ${key}`, got, total),
      );
    }
    if (handout.has_segmentation) {
      segBuffer = await download(handout.segmentation_url, "segmentation", (got, total) =>
        showProgress(`Downloading the segmentation of ${key}`, got, total),
      );
    }
    showProgress("Preparing the views…");
    await nextFrame();
    await buildViews(key, imageBuffer, segBuffer);
    state.loaded = true;
    hideLoading();
    renderLabels(); // what was seen can now be judged
  } catch (error) {
    clearViewers();
    const text =
      error instanceof RangeError
        ? "The browser ran out of memory. Close other tabs, or review this subject on a computer with more memory."
        : error.message || String(error);
    showEmpty(`Could not show ${key}`, "You still hold the subject. Try again, or release it for someone else.");
    $("emptyMessage").replaceChildren(
      banner(text, "err"),
      el("p", {}, el("button", { onclick: () => openAssignment(handout.assignment_id) }, "Try again")),
    );
  }
  updateVerdictButtons();
}

function renderSubject() {
  const handout = state.handout;
  $("subjectCard").hidden = !handout;
  $("labelsCard").hidden = !handout;
  $("verdictCard").hidden = !handout;
  if (!handout) return;

  $("subjectKey").textContent = handout.subject_key;
  const dataset = handout.dataset_info || {};
  $("subjectFacts").replaceChildren(
    el("div", {}, `${dataset.name || "Dataset"} · dataset ${handout.dataset_id}${dataset.modality ? ` · ${dataset.modality}` : ""}`),
  );
  renderLease();

  const notes = [];
  if (handout.segmentation_source === "staged") {
    const edit = [...(handout.history || [])].reverse().find((event) => event.action === "edit");
    notes.push(
      `This segmentation is an editor's correction${edit ? `, by ${edit.by}` : ""}. It is not in the dataset yet: ` +
        "the dataset keeps its own until the administrator approves this one.",
    );
  }
  if (handout.data_access === "image") {
    notes.push(
      "Your account is sent images only, so it cannot accept a segmentation. Reject the subject with a comment, report missing bones, or release it.",
    );
  } else if (handout.data_access === "segmentation") {
    notes.push("Your account is sent the segmentation only; the slices show the labels without the image.");
  }
  // Corrections take 3D Slicer and the editor role, which this account may or may not hold.
  if (handout.data_access !== "image" && !handout.has_segmentation) {
    notes.push(
      isEditor()
        ? "This subject has no segmentation yet. Create one in 3D Slicer, or release the subject."
        : "This subject has no segmentation yet, and creating one is for an editor, in 3D Slicer. Release it.",
    );
  } else if (handout.has_segmentation && !state.segments.length) {
    notes.push("The server could not read which labels this segmentation holds, so it cannot be reviewed here.");
  } else if (handout.stored_segmentation_issue) {
    notes.push(
      `This segmentation cannot be accepted as it is. ${handout.stored_segmentation_issue} ` +
        "Its labels start rejected, so that an editor rewrites it on the image's grid in 3D Slicer.",
    );
  }
  $("subjectNotes").replaceChildren(...notes.map((text) => banner(text, "note")));
  renderHistory();
}

// Whether this account may also correct segmentations, which is done in 3D Slicer.
function isEditor() {
  return !!(state.info && (state.info.roles || []).includes("editor"));
}

function renderLease() {
  const handout = state.handout;
  if (!handout) return;
  const expires = new Date(handout.expires_at);
  const minutes = Math.round((expires - Date.now()) / 60000);
  const relative =
    minutes <= 0 ? "expired" : minutes < 60 ? `in ${minutes} min` : `in ${Math.round(minutes / 60)} h`;
  $("leaseText").textContent = `Yours until ${expires.toLocaleString()} (${relative})`;
  $("leaseText").title =
    "After this the subject goes back to the queue. You can still submit afterwards, unless someone else has taken it.";
}

// ------------------------------------------------------------------- labels
// What the quality check has made of a label so far, in words, for its status column.
function labelStatus(label) {
  const by = label.by ? ` · ${label.by}` : "";
  switch (label.state) {
    case "pending":
      if (!label.painted) {
        return {
          text: `removed · ${label.edited_by}`,
          title: `${label.edited_by} took it out of the segmentation. Accept if the bone should not be segmented; reject if it is missing.`,
        };
      }
      if (label.edited_by) return { text: `edited · ${label.edited_by}`, title: `Corrected by ${label.edited_by}, in 3D Slicer.` };
      if (!label.dataset_status) {
        return { text: "new", title: "Subject_info does not list this label as available, and nobody has reviewed it." };
      }
      return { text: STATUS_SHORT[label.dataset_status] || String(label.dataset_status), title: state.statusText[label.dataset_status] || "" };
    case "accepted":
      return { text: `accepted${by}`, title: "Accepted already. Reject it if you see a problem." };
    case "kept":
      return { text: "reviewed", title: "Reviewed in the dataset already, so not under review. Reject it if you see a problem." };
    case "removed":
      return {
        text: label.by ? `removed · ${label.by}` : "not painted",
        title: "Not in the segmentation: it becomes not available when the subject is approved. Reject it if the bone is missing.",
      };
    case "rejected":
      return { text: `rejected${by}`, title: state.reasonText[label.reason] || "" };
    default:
      return { text: label.state, title: "" };
  }
}

// Whether the reviewer can judge a label: one in the segmentation takes seeing it.
function canJudge(label) {
  if (!state.handout || !label) return false;
  if (!label.painted) return true;
  return !!state.handout.has_segmentation && state.segments.length > 0 && state.loaded;
}

// A segmentation that is not on its image's grid cannot be accepted as it is: an editor rewrites it.
function canAccept(label) {
  return canJudge(label) && !(label.painted && state.handout.stored_segmentation_issue);
}

// The labels under review the reviewer can judge but has not.
function unjudged() {
  return [...state.labels.values()]
    .filter((label) => label.state === "pending" && canJudge(label) && !state.verdicts.has(label.name))
    .map((label) => label.name);
}

function setVerdict(name, verdict) {
  const label = state.labels.get(name);
  // Pressing the verdict a label has takes it back -- except under review, where one is needed.
  if (state.verdicts.get(name) === verdict && !(label && label.state === "pending")) state.verdicts.delete(name);
  else state.verdicts.set(name, verdict);
  renderLabels();
  updateVerdictButtons();
}

function setAll(verdict) {
  for (const label of state.labels.values()) {
    if (label.state !== "pending" || !canJudge(label)) continue;
    if (verdict === "accept" && !canAccept(label)) continue;
    state.verdicts.set(label.name, verdict);
  }
  renderLabels();
  updateVerdictButtons();
}

function verdictButtons(label) {
  const verdict = state.verdicts.get(label.name);
  const judgeable = canJudge(label);
  return el(
    "span",
    { className: "verdict", role: "group", "aria-label": `Verdict on ${label.name}` },
    el(
      "button",
      {
        className: "accept",
        "aria-pressed": String(verdict === "accept"),
        disabled: !canAccept(label),
        title: label.painted
          ? "Accept: the segmentation of this bone is right"
          : "Accept: the bone is rightly not segmented",
        onclick: () => setVerdict(label.name, "accept"),
      },
      "✓",
    ),
    el(
      "button",
      {
        className: "reject",
        "aria-pressed": String(verdict === "reject"),
        disabled: !judgeable,
        title: label.painted
          ? "Reject: it needs correcting, or should not be there"
          : "Reject: the bone is in the scan and missing from the segmentation",
        onclick: () => setVerdict(label.name, "reject"),
      },
      "✗",
    ),
  );
}

// A label's row, and under a rejected one in the segmentation the reason for rejecting it.
function labelRows(label, segment) {
  const status = labelStatus(label);
  const rejected = state.verdicts.get(label.name) === "reject";
  const cells = [
    verdictButtons(label),
    segment ? el("span", { className: "swatch", style: { background: `rgb(${displayColor(segment).join(",")})` } }) : el("span"),
    segment
      ? el("button", { className: "lname", title: "Show where this label is", onclick: () => focusSegment(segment) }, label.name)
      : el("span", { className: "lname" }, label.name),
    el("span", { className: `lstatus ${label.state}`, title: status.title }, status.text),
  ];
  if (segment) {
    cells.push(
      el("button", { className: "icon solo", title: "Show only this label", onclick: () => toggleSolo(segment) }, icon("solo")),
      el("button", { className: "icon eye", title: "Hide this label", onclick: () => toggleHidden(segment) }, icon("eye")),
    );
  }
  const rows = [
    el(
      "div",
      {
        className: `lrow${segment ? "" : " absent"}${rejected ? " rejected" : ""}`,
        dataset: segment ? { number: segment.number } : {},
        title: segment ? undefined : "Not in the segmentation",
      },
      ...cells,
    ),
  ];
  if (rejected && label.painted) {
    const reason = state.reasons.get(label.name) || "quality";
    rows.push(
      el(
        "div",
        { className: "lrow" },
        el("span"),
        el("span"),
        el(
          "select",
          {
            className: "lreason",
            "aria-label": `Why ${label.name} is rejected`,
            onchange: (event) => state.reasons.set(label.name, event.target.value),
          },
          el("option", { value: "quality", selected: reason === "quality" }, "Needs correction"),
          el("option", { value: "absent", selected: reason === "absent" }, "Should not be there"),
        ),
      ),
    );
  }
  return rows;
}

function renderLabels() {
  const handout = state.handout;
  const list = $("labelsList");
  list.replaceChildren();
  if (!handout) return;

  // The bones in the segmentation, in anatomical order, then those it does not paint -- every
  // label, when the segmentation is not sent.
  const listed = new Set();
  for (const segment of state.segments) {
    listed.add(segment.label);
    const label = state.labels.get(segment.label) || { name: segment.label, state: "pending", painted: true };
    list.append(...labelRows(label, segment));
  }
  for (const label of state.labels.values()) {
    if (!listed.has(label.name)) list.append(...labelRows(label, null));
  }
  renderMissing();

  const pending = [...state.labels.values()].filter((label) => label.state === "pending");
  const foot = [];
  if (pending.length) {
    foot.push(`${pending.length} label(s) under review: accept (✓) or reject (✗) each.`);
  } else if (state.labels.size) {
    foot.push("No label is under review; reject one if you see a problem.");
  }
  if (!handout.has_segmentation && [...state.labels.values()].some((label) => label.painted)) {
    foot.push(
      handout.data_access === "image"
        ? "Your account is not sent segmentations, so you can reject the subject, or report missing bones, but not accept."
        : "The segmentation could not be sent.",
    );
  }
  if ([...state.labels.values()].some((label) => !label.painted) && state.info && state.info.mark_removed_labels_absent) {
    foot.push("A label not in the segmentation becomes not available when the subject is approved, unless reported missing.");
  }
  $("labelsFoot").textContent = foot.join(" ");
  $("labelsTitle").textContent = state.labels.size ? `Labels (${state.labels.size})` : "Labels";
  const bulk = pending.some(canJudge);
  $("acceptAllBtn").hidden = !bulk;
  $("rejectAllBtn").hidden = !bulk;
  updateLabelRows();
}

function renderMissing() {
  $("missingBox").hidden = !state.handout;
  $("missingList").replaceChildren(
    ...[...state.missing].sort().map((name) =>
      el(
        "span",
        { className: "chip" },
        `${name} missing`,
        el(
          "button",
          {
            title: `Take it back: ${name} is not missing`,
            "aria-label": `Take back ${name}`,
            onclick: () => {
              state.missing.delete(name);
              renderLabels();
              updateVerdictButtons();
            },
          },
          "×",
        ),
      ),
    ),
  );
}

// A bone the segmentation lacks goes to an editor, to add.
function reportMissing() {
  const name = $("missingInput").value.trim().toUpperCase();
  if (!name) return;
  if (state.labelNames.length && !state.labelNames.includes(name)) {
    showVerdictMessage(`${name} is not a BoneHub label. Pick one from the list.`, "err");
    return;
  }
  const label = state.labels.get(name);
  if (label && label.painted) {
    showVerdictMessage(`${name} is in the segmentation already. Reject it there if it needs correcting.`, "err");
    return;
  }
  if (label) state.verdicts.set(name, "reject"); // listed, not painted: rejected as missing
  else state.missing.add(name);
  $("missingInput").value = "";
  $("verdictMessage").replaceChildren();
  renderLabels();
  updateVerdictButtons();
}

function updateLabelRows() {
  for (const row of document.querySelectorAll("#labelsList .lrow[data-number]")) {
    const segment = state.byNumber.get(Number(row.dataset.number));
    if (!segment) continue;
    const shown = segmentShown(segment);
    row.classList.toggle("hidden-label", !shown);
    const eye = row.querySelector(".eye");
    eye.replaceChildren(icon(state.hidden.has(segment.number) ? "eyeOff" : "eye"));
    eye.setAttribute("aria-pressed", String(!state.hidden.has(segment.number)));
    eye.title = state.hidden.has(segment.number) ? "Show this label" : "Hide this label";
    const solo = row.querySelector(".solo");
    solo.setAttribute("aria-pressed", String(state.solo === segment.number));
    solo.title = state.solo === segment.number ? "Show every label again" : "Show only this label";
  }
}

function toggleHidden(segment) {
  state.solo = null;
  if (state.hidden.has(segment.number)) state.hidden.delete(segment.number);
  else state.hidden.add(segment.number);
  applyLabelColors();
}

function toggleSolo(segment) {
  state.solo = state.solo === segment.number ? null : segment.number;
  applyLabelColors();
  if (state.solo !== null) focusSegment(segment);
}

function showAllLabels() {
  state.hidden.clear();
  state.solo = null;
  applyLabelColors();
}

// ------------------------------------------------------------------ history
// What happened to the subject before it reached this reviewer, oldest first.
function renderHistory() {
  const events = (state.handout && state.handout.history) || [];
  const list = $("historyList");
  list.hidden = !events.length;
  list.replaceChildren(
    ...events.map((event) =>
      el(
        "li",
        {},
        el("span", { className: "who" }, `${new Date(event.at).toLocaleDateString()} · ${event.by}: `),
        describeEvent(event),
        event.comment ? el("div", {}, `“${event.comment}”`) : null,
      ),
    ),
  );
}

function describeEvent(event) {
  const details = event.details || {};
  const names = (list) => (list || []).join(", ");
  if (event.action === "review") {
    const rejected = Object.entries(details.rejected || {}).filter(([, why]) => why !== "missing");
    const parts = [];
    if ((details.accepted || []).length) parts.push(`accepted ${names(details.accepted)}`);
    if (rejected.length) parts.push(`rejected ${rejected.map(([name, why]) => `${name} (${state.reasonText[why] || why})`).join(", ")}`);
    if ((details.missing || []).length) parts.push(`reported missing ${names(details.missing)}`);
    return parts.join("; ") || "reviewed it";
  }
  if (event.action === "edit") {
    const parts = [];
    if ((details.edited || []).length) parts.push(`corrected ${names(details.edited)}`);
    if ((details.removed || []).length) parts.push(`removed ${names(details.removed)}`);
    return parts.join("; ") || "uploaded the segmentation unchanged";
  }
  if (event.action === "return") return `sent it back to the ${details.to === "edit" ? "editors" : "reviewers"}`;
  if (event.action === "escalate") return "sent it to the administrator";
  return event.action;
}

// ------------------------------------------------------------------ verdict
// The verdict as the server takes it: accepted labels, rejected ones with their reasons, and
// bones reported missing that the subject does not list.
function verdictToSend() {
  const accepted = [];
  const rejected = {};
  for (const [name, verdict] of state.verdicts) {
    const label = state.labels.get(name) || { name, painted: true };
    if (!canJudge(label)) continue;
    if (verdict === "accept") accepted.push(name);
    else rejected[name] = label.painted ? state.reasons.get(name) || "quality" : "missing";
  }
  return { accepted: accepted.sort(), rejected, missing: [...state.missing].sort() };
}

function setBusy(busy) {
  state.busy = busy;
  $("nextBtn").disabled = busy;
  updateVerdictButtons();
}

function updateVerdictButtons() {
  const holding = !!state.handout;
  const { accepted, rejected, missing } = holding ? verdictToSend() : { accepted: [], rejected: {}, missing: [] };
  const toEditors = Object.keys(rejected).length + missing.length;
  const open = holding ? unjudged() : [];
  const underReview = [...state.labels.values()].some((label) => label.state === "pending");
  const ready =
    holding && state.loaded && !open.length && (accepted.length > 0 || toEditors > 0 || !underReview);

  const confirm = $("confirmBtn");
  confirm.textContent = toEditors
    ? `Send to editors (${toEditors})`
    : accepted.length
      ? `Accept ${accepted.length} label${accepted.length === 1 ? "" : "s"}`
      : "Submit verdict";
  confirm.disabled = state.busy || !ready;
  confirm.title = !holding
    ? ""
    : open.length
      ? `Give every label under review a verdict first: ${open.join(", ")}.`
      : toEditors
        ? "The rejected labels go to the editors, to correct in 3D Slicer."
        : "The subject waits for the administrator's approval.";
  $("rejectBtn").disabled = state.busy || !holding;
  $("releaseBtn").disabled = state.busy || !holding;
  $("extendBtn").disabled = state.busy || !holding;
  $("acceptAllBtn").disabled = state.busy || !holding;
  $("rejectAllBtn").disabled = state.busy || !holding;
  $("missingAddBtn").disabled = state.busy || !holding;
}

function showVerdictMessage(text, kind) {
  $("verdictMessage").replaceChildren(banner(text, kind));
}

async function onSubmit() {
  const handout = state.handout;
  const open = unjudged();
  if (open.length) {
    showVerdictMessage(`Give every label under review a verdict first: ${open.join(", ")}.`, "err");
    return;
  }
  const { accepted, rejected, missing } = verdictToSend();
  const toCorrect = Object.entries(rejected).filter(([, why]) => why !== "missing");
  const toAdd = [...Object.keys(rejected).filter((name) => rejected[name] === "missing"), ...missing].sort();
  const toEditors = toCorrect.length + toAdd.length > 0;
  const leftOver = [...state.labels.values()].some(
    (label) => label.state === "pending" && !state.verdicts.has(label.name),
  );

  const body = [];
  if (accepted.length) {
    body.push(el("p", {}, `${accepted.length} label(s) `, el("strong", {}, "accepted"), "."));
  }
  if (toCorrect.length) {
    body.push(
      el("p", {}, "Rejected, for an editor to correct:"),
      el("ul", {}, toCorrect.map(([name, why]) => el("li", {}, `${name}: ${state.reasonText[why] || why}`))),
    );
  }
  if (toAdd.length) {
    body.push(el("p", {}, "Reported missing, for an editor to add:"), el("ul", {}, toAdd.map((name) => el("li", {}, name))));
  }
  body.push(
    el(
      "p",
      {},
      toEditors
        ? "The subject goes to the editors; the labels you accepted wait for their correction."
        : leftOver
          ? "The labels you could not judge wait for another reviewer."
          : "The subject then waits for the administrator's approval.",
    ),
    el("p", { className: "muted" }, "Nothing is written into the dataset until the administrator approves the subject."),
  );
  if (state.sliceSpacing) {
    body.push(el("p", {}, `Your comment will note that you saw the scan at ${formatSpacing(1)}.`));
  }
  if (!(await ask(`Submit your verdict on ${handout.subject_key}?`, body, toEditors ? "Send to editors" : "Submit"))) return;
  const metadata = {
    quality_check_confirmed: true,
    use_stored_segmentation: true,
    confirmed_labels: accepted,
    comment: commentToSend(),
  };
  if (Object.keys(rejected).length) metadata.rejected_labels = rejected;
  if (missing.length) metadata.missing_labels = missing;
  await submitVerdict(metadata, "Submitting…");
}

async function onReject() {
  const handout = state.handout;
  const comment = $("comment").value.trim();
  if (!comment) {
    const anyway = await ask(
      `Reject ${handout.subject_key} without a comment?`,
      [el("p", {}, "Saying what is wrong is what makes the rejection useful to whoever corrects it.")],
      "Reject anyway",
      { danger: true, cancelLabel: "Write a comment" },
    );
    if (!anyway) {
      // After the dialog has handed focus back to the button that opened it.
      window.setTimeout(() => $("comment").focus(), 0);
      return;
    }
  } else if (
    !(await ask(
      `Reject ${handout.subject_key}?`,
      [
        el("p", {}, "Every label under review goes to the editors, with your comment."),
        el("p", { className: "muted" }, "Nothing in the dataset changes."),
      ],
      "Reject",
      { danger: true },
    ))
  ) {
    return;
  }
  await submitVerdict({ quality_check_confirmed: false, comment: commentToSend() }, "Rejecting…");
}

async function submitVerdict(metadata, workingText) {
  const handout = state.handout;
  const form = new FormData();
  form.append("metadata", JSON.stringify(metadata));
  setBusy(true);
  showVerdictMessage(workingText, "note");
  try {
    const result = await api("POST", `/api/v1/assignments/${encodeURIComponent(handout.assignment_id)}/submit`, {
      form,
    });
    await finishSubject(`${handout.subject_key}: ${result.message || "submitted."}`);
  } catch (error) {
    showVerdictMessage(`Not submitted, so you still hold the subject: ${error.message}`, "err");
  } finally {
    setBusy(false);
  }
}

async function onRelease() {
  const handout = state.handout;
  const sure = await ask(
    `Release ${handout.subject_key}?`,
    [el("p", {}, "It goes back to the queue without a verdict, and someone else can review it.")],
    "Release",
  );
  if (!sure) return;
  setBusy(true);
  try {
    await api("POST", `/api/v1/assignments/${encodeURIComponent(handout.assignment_id)}/release`);
    await finishSubject(`${handout.subject_key} was released.`);
  } catch (error) {
    showVerdictMessage(error.message, "err");
  } finally {
    setBusy(false);
  }
}

async function onExtend() {
  const handout = state.handout;
  setBusy(true);
  try {
    const assignment = await api("POST", `/api/v1/assignments/${encodeURIComponent(handout.assignment_id)}/extend`);
    handout.expires_at = assignment.expires_at;
    renderLease();
  } catch (error) {
    showVerdictMessage(error.message, "err");
  } finally {
    setBusy(false);
  }
}

async function finishSubject(message) {
  const finished = state.handout ? state.handout.assignment_id : null;
  clearViewers();
  state.handout = null;
  state.segments = [];
  state.byNumber = new Map();
  state.labels = new Map();
  state.verdicts = new Map();
  state.reasons = new Map();
  state.missing = new Set();
  renderSubject();
  renderLabels();
  updateToolbar();
  showEmpty("Done", "Ask for the next subject when you are ready.", banner(message, "ok"));
  await showHeld(finished);
  if (prefs.autoNext) {
    setBusy(false);
    await nextSubject();
  }
}

// ------------------------------------------------------------------- wiring
function wire() {
  $("signInBtn").addEventListener("click", () => {
    const key = $("apiKey").value.trim();
    if (!key) return;
    $("loginError").hidden = true;
    signIn(key, $("rememberKey").checked).catch((error) => {
      state.key = null;
      showLogin(error.status === 401 || error.status === 403 ? error.message : `Could not sign in: ${error.message}`);
    });
  });
  $("apiKey").addEventListener("keydown", (event) => {
    if (event.key === "Enter") $("signInBtn").click();
  });
  $("signOutBtn").addEventListener("click", signOut);

  $("nextBtn").addEventListener("click", nextSubject);
  $("confirmBtn").addEventListener("click", onSubmit);
  $("rejectBtn").addEventListener("click", onReject);
  $("releaseBtn").addEventListener("click", onRelease);
  $("extendBtn").addEventListener("click", onExtend);
  $("acceptAllBtn").addEventListener("click", () => setAll("accept"));
  $("rejectAllBtn").addEventListener("click", () => setAll("reject"));
  $("missingAddBtn").addEventListener("click", reportMissing);
  $("missingInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") reportMissing();
  });

  $("autoNext").checked = !!prefs.autoNext;
  $("autoNext").addEventListener("change", (event) => {
    prefs.autoNext = event.target.checked;
    savePrefs();
  });

  for (const button of document.querySelectorAll("#toolbar [data-layout]")) {
    button.addEventListener("click", () => {
      prefs.layout = button.dataset.layout;
      savePrefs();
      applyLayout();
    });
  }
  let opacityPending = false;
  $("opacity").addEventListener("input", (event) => {
    prefs.opacity = Number(event.target.value);
    savePrefs();
    // A slider sends many events; the label layer is redrawn once per frame at most.
    if (opacityPending || !state.volumes.seg2d || !state.volumes.image) return;
    opacityPending = true;
    requestAnimationFrame(() => {
      opacityPending = false;
      if (!state.volumes.seg2d) return;
      state.volumes.seg2d.opacity = prefs.opacity / 100;
      refreshSliceLabels();
    });
  });
  $("outlineBtn").addEventListener("click", () => {
    prefs.outline = !prefs.outline;
    savePrefs();
    updateToolbar();
    if (nv2d && state.volumes.image && state.volumes.seg2d) {
      nv2d.opts.atlasOutline = prefs.outline ? 1 : 0;
      refreshSliceLabels();
    }
  });
  $("windowPreset").addEventListener("change", applyWindow);
  $("plane").value = PLANES[prefs.plane] !== undefined ? prefs.plane : "multi";
  $("plane").addEventListener("change", (event) => {
    prefs.plane = event.target.value;
    savePrefs();
    if (nv2d) nv2d.setSliceType(PLANES[prefs.plane]);
  });
  $("colorsBtn").addEventListener("click", () => {
    prefs.distinctColors = !prefs.distinctColors;
    savePrefs();
    updateToolbar();
    applyLabelColors();
    renderLabels(); // the swatches follow the colours
  });
  $("showAllBtn").addEventListener("click", showAllLabels);
  $("resetViewBtn").addEventListener("click", resetView);

  window.setInterval(renderLease, 30000);
}

async function boot() {
  wire();
  applyLayout();
  // An invite link carries the key after '#', which never reaches the server; it is taken
  // out of the address bar at once so it does not linger in the history.
  const fromLink = new URLSearchParams(window.location.hash.slice(1)).get("key");
  if (fromLink) window.history.replaceState(null, "", window.location.pathname + window.location.search);
  const remembered = storageGet("localStorage", KEY_STORAGE);
  const key = fromLink || storageGet("sessionStorage", KEY_STORAGE) || remembered;
  if (!key) {
    showLogin();
    return;
  }
  try {
    await signIn(key, !fromLink && !!remembered && key === remembered);
  } catch (error) {
    state.key = null;
    storageRemove("sessionStorage", KEY_STORAGE);
    if (error.status === 401 || error.status === 403) storageRemove("localStorage", KEY_STORAGE);
    showLogin(fromLink ? `This invite link did not work: ${error.message}` : error.message);
  }
}

boot();

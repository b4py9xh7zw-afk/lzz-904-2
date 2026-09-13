'use strict';

const state = {
  id: null,
  frames: [],
  frame: 0,
  preset: 'warm',
  lastCommands: null,
  cmdTab: 'render',
};

const $ = (sel) => document.querySelector(sel);
const els = {
  dropzone: $('#dropzone'), fileInput: $('#fileInput'),
  uploadMeta: $('#uploadMeta'), stageWrap: $('#stageWrap'),
  origImg: $('#origImg'), outImg: $('#outImg'),
  compareSlider: $('#compareSlider'), compareHandle: $('#compareHandle'),
  frameRow: $('#frameRow'), presetGrid: $('#presetGrid'),
  sat: $('#sat'), con: $('#con'), satVal: $('#satVal'), conVal: $('#conVal'),
  previewBtn: $('#previewBtn'), renderBtn: $('#renderBtn'),
  downloadLink: $('#downloadLink'), resolvedBox: $('#resolvedBox'),
  cmdBox: $('#cmdBox'), status: $('#status'),
};

function setStatus(kind, html) {
  els.status.className = `status ${kind}`;
  els.status.innerHTML = html;
  els.status.classList.remove('hidden');
}
function clearStatus() { els.status.classList.add('hidden'); }

// ---- 模板卡片 ----
async function loadPresets() {
  try {
    const r = await fetch('/api/presets');
    const data = await r.json();
    els.presetGrid.innerHTML = '';
    data.presets.forEach((p) => {
      const card = document.createElement('div');
      card.className = 'preset-card' + (p.id === state.preset ? ' active' : '');
      card.dataset.id = p.id;
      card.innerHTML = `<div class="preset-name">${p.label}</div>
                        <div class="preset-desc">${p.desc}</div>`;
      card.onclick = () => selectPreset(p.id);
      els.presetGrid.appendChild(card);
    });
    if (data.bounds) {
      els.sat.min = data.bounds.saturation[0]; els.sat.max = data.bounds.saturation[1];
      els.con.min = data.bounds.contrast[0]; els.con.max = data.bounds.contrast[1];
    }
  } catch (e) {
    els.presetGrid.innerHTML = '<div class="loading-hint">模板加载失败，请刷新</div>';
  }
}

function selectPreset(id) {
  state.preset = id;
  document.querySelectorAll('.preset-card').forEach((c) =>
    c.classList.toggle('active', c.dataset.id === id));
  schedulePreview();
}

// ---- 滑杆（debounce 自动预览）----
els.sat.oninput = () => { els.satVal.textContent = (+els.sat.value).toFixed(2) + '×'; schedulePreview(); };
els.con.oninput = () => { els.conVal.textContent = (+els.con.value).toFixed(2) + '×'; schedulePreview(); };

let previewTimer = null;
function schedulePreview() {
  if (!state.id) return;
  clearTimeout(previewTimer);
  previewTimer = setTimeout(requestPreview, 220);
}

// ---- 对比滑块 ----
els.compareSlider.oninput = () => {
  const v = els.compareSlider.value;
  els.outImg.style.clipPath = `inset(0 0 0 ${v}%)`;
  els.compareHandle.style.left = v + '%';
};

// ---- 上传 ----
els.dropzone.onclick = () => els.fileInput.click();
els.fileInput.onchange = () => { if (els.fileInput.files[0]) upload(els.fileInput.files[0]); };
['dragover', 'dragenter'].forEach((ev) =>
  els.dropzone.addEventListener(ev, (e) => { e.preventDefault(); els.dropzone.classList.add('drag'); }));
['dragleave', 'drop'].forEach((ev) =>
  els.dropzone.addEventListener(ev, (e) => { e.preventDefault(); els.dropzone.classList.remove('drag'); }));
els.dropzone.addEventListener('drop', (e) => {
  const f = e.dataTransfer.files[0];
  if (f) upload(f);
});

async function upload(file) {
  setStatus('info', '<span class="spinner"></span> 上传并抽取预览帧…（大文件请稍候）');
  const fd = new FormData();
  fd.append('video', file);
  try {
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || '上传失败');

    state.id = data.id;
    state.frames = data.frames;
    els.dropzone.classList.add('hidden');
    els.stageWrap.classList.remove('hidden');
    els.uploadMeta.classList.remove('hidden');
    els.uploadMeta.innerHTML =
      `<span>时长 <b>${data.duration}s</b></span><span>大小 <b>${data.size_mb}MB</b></span>`;

    els.frameRow.innerHTML = '';
    data.frames.forEach((fr, i) => {
      const t = document.createElement('div');
      t.className = 'frame-thumb' + (i === 0 ? ' active' : '');
      t.innerHTML = `<img src="${fr.url}"><span class="t">${fr.time}s</span>`;
      t.onclick = () => selectFrame(i);
      els.frameRow.appendChild(t);
    });
    selectFrame(0);
    els.previewBtn.disabled = false;
    els.renderBtn.disabled = false;
    setStatus('ok', '上传成功，已抽取 3 帧。正在生成滤镜预览…');
    requestPreview();
  } catch (e) {
    setStatus('error', '❌ ' + e.message);
  }
}

function selectFrame(i) {
  state.frame = i;
  const url = state.frames[i].url;
  els.origImg.src = url;
  document.querySelectorAll('.frame-thumb').forEach((t, j) =>
    t.classList.toggle('active', j === i));
  requestPreview();
}

// ---- 预览请求：只发白名单内 5 个字段 ----
async function requestPreview() {
  if (!state.id) return;
  const payload = {
    id: state.id,
    frame: state.frame,
    preset: state.preset,
    saturation: +els.sat.value,
    contrast: +els.con.value,
  };
  setStatus('info', '<span class="spinner"></span> 后端应用白名单滤镜…');
  try {
    const r = await fetch('/api/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || '预览失败');

    els.outImg.src = data.url + '?t=' + Date.now();
    state.lastCommands = data.commands;
    renderResolved(data.resolved);
    renderCommand();
    els.downloadLink.classList.add('hidden');
    setStatus('ok', '预览已更新。拖动上方滑块对比「原始 / 滤镜」。');
  } catch (e) {
    setStatus('error', '❌ ' + e.message);
  }
}

function renderResolved(p) {
  const keys = ['contrast', 'brightness', 'saturation', 'gamma_r', 'gamma_b',
                'rh', 'bh', 'rm', 'bm',
                'unsharp_lamount', 'unsharp_camount', 'hqdn3d_luma'];
  els.resolvedBox.innerHTML = keys
    .filter((k) => k in p)
    .map((k) => `<b>${k}</b>=${p[k]}`).join(' &nbsp; ');
}

document.querySelectorAll('.cmd-tab').forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll('.cmd-tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    state.cmdTab = tab.dataset.tab;
    renderCommand();
  };
});

function renderCommand() {
  const c = state.lastCommands;
  if (!c) return;
  els.cmdBox.querySelector('code').textContent =
    state.cmdTab === 'preview' ? (c.preview || c.render) : c.render;
}

// ---- 整片渲染 ----
els.previewBtn.onclick = requestPreview;
els.renderBtn.onclick = async () => {
  if (!state.id) return;
  els.renderBtn.disabled = true;
  setStatus('info', '<span class="spinner"></span> 正在用 libx264 渲染整片，可能需要几十秒…');
  try {
    const r = await fetch('/api/render', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: state.id, preset: state.preset,
        saturation: +els.sat.value, contrast: +els.con.value,
      }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || '渲染失败');
    state.lastCommands = data.commands;
    renderCommand();
    els.downloadLink.href = data.download;
    els.downloadLink.classList.remove('hidden');
    setStatus('ok', '渲染完成，可下载 MP4。');
  } catch (e) {
    setStatus('error', '❌ ' + e.message);
  } finally {
    els.renderBtn.disabled = false;
  }
};

loadPresets();

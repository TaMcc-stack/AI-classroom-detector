const $ = id => document.getElementById(id);
const viewer = $('viewer'), video = $('video'), captureCanvas = $('captureCanvas');
const overlay = $('overlayCanvas'), resultImage = $('resultImage'), dropzone = $('dropzone');

let stream = null, captureTimer = null, testTimer = null, testBlob = null, busy = false, previewUrl = null;
let deviceSignature = '';

// 后端返回哪些设备就渲染哪些，3 个或 4 个（含班班通）都能自适应。
const DEVICE_META = {
  light: {icon: '💡', label: '灯光'},
  fan: {icon: '🌀', label: '风扇'},
  ac: {icon: '❄️', label: '空调'},
  board: {icon: '📺', label: '班班通'},
};

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch { }
    throw new Error(detail);
  }
  return response.json();
}

function escapeHtml(v) { const d = document.createElement('div'); d.textContent = v; return d.innerHTML; }

function addLocalLog(msg) {
  $('logs').insertAdjacentHTML('afterbegin', `<div class="log error">${escapeHtml(msg)}</div>`);
}

// 结果大卡展示的是策略状态（occupancy），不是单帧结论——它才是驱动设备的东西。
function occupancyView(s) {
  switch (s.occupancy) {
    case 'occupied':
      return {icon: '🧑', text: '检测到人员', sub: '不发送设备控制指令，设备保持原样'};
    case 'checking_empty':
      return {icon: '⏳', text: '疑似无人·确认中',
        sub: `已连续无人 ${Math.round(s.emptyElapsed)}/${s.emptyRequired} 秒 · 有效帧 ${s.emptySamples}/${s.minEmptySamples}`};
    case 'empty': {
      const anyOn = Object.values(s.devices || {}).some(Boolean);
      return {icon: '🚫', text: '已确认无人',
        sub: anyOn ? '无人状态，但有设备被手动开启' : '已按顺序关闭全部设备'};
    }
    default:
      return {icon: '⏳', text: '等待检测', sub: '设备保持原状态，系统不会主动开启'};
  }
}

function ensureDevices(devices) {
  const signature = Object.keys(devices).join(',');
  if (signature === deviceSignature) return;
  deviceSignature = signature;
  $('devices').innerHTML = Object.keys(devices).map(key => {
    const meta = DEVICE_META[key] || {icon: '🔌', label: key};
    return `<button class="device" data-device="${escapeHtml(key)}"><span>${meta.icon}</span>` +
      `<div>${escapeHtml(meta.label)}<small>读取中</small></div></button>`;
  }).join('');
}

function renderState(s) {
  if (s.devices) ensureDevices(s.devices);

  const labels = {occupied: '有人（不干涉设备）', checking_empty: '疑似无人·确认中', empty: '已确认无人', unknown: '未知'};
  $('occupancy').textContent = labels[s.occupancy] || s.occupancy;

  const color = s.occupancy === 'occupied' ? '#ffb74d' : s.occupancy === 'empty' ? '#66bb6a' : '#708391';
  const dot = $('occupancyDot');
  dot.style.background = color;
  dot.style.color = color;   // currentColor 驱动光晕

  const pct = s.emptyRequired ? Math.min(100, s.emptyElapsed / s.emptyRequired * 100) : 0;
  $('progressBar').style.width = `${pct}%`;
  $('timerText').textContent = s.occupancy === 'occupied' ? '检测到人员：不发送设备控制指令' :
    `无人计时 ${Math.round(s.emptyElapsed)}/${s.emptyRequired} 秒 · 有效帧 ${s.emptySamples}/${s.minEmptySamples}`;

  const view = occupancyView(s);
  $('resultBox').dataset.state = s.occupancy;
  $('resultIcon').textContent = view.icon;
  $('resultText').textContent = view.text;
  $('resultSub').textContent = view.sub;

  document.querySelectorAll('.device').forEach(el => {
    const on = !!s.devices[el.dataset.device];
    el.classList.toggle('on', on);
    el.classList.toggle('off', !on);
    el.querySelector('small').textContent = on ? '运行中' : '已关闭';
  });

  if (s.events) {
    $('logs').innerHTML = [...s.events].reverse().map(e =>
      `<div class="log ${escapeHtml(e.level)}">${new Date(e.time * 1000).toLocaleTimeString('zh-CN', {hour12: false})} ${escapeHtml(e.message)}</div>`
    ).join('');
  }
}

// 先把本地原图显示出来，不等后端；标注图回来后再替换。
function showLocalPreview(file) {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(file);
  resultImage.src = previewUrl;
  viewer.className = 'viewer result';
}

async function sendBlob(blob, allowControl) {
  if (busy) return;
  busy = true;
  $('inference').textContent = 'YOLO分析中…';
  const form = new FormData();
  form.append('file', blob, 'frame.jpg');
  form.append('allow_control', String(allowControl));
  try {
    const r = await api('/api/detect', {method: 'POST', body: form});
    $('conclusion').textContent = r.hasPerson ? '检测到人员' : '未检测到人员';
    $('personCount').textContent = r.count;
    $('confidence').textContent = r.count ? `${(r.bestScore * 100).toFixed(1)}%` : '—';

    if (stream) {
      // 摄像头模式下画布尺寸就是送检尺寸，检测框坐标系与之一致，直接叠加。
      drawOverlay(r.detections, captureCanvas.width, captureCanvas.height);
    } else if (r.annotatedImage) {
      resultImage.src = r.annotatedImage;
      viewer.className = 'viewer result';
    }
    renderState(r.policy);
    $('inference').textContent = '完成';
  } catch (e) {
    $('inference').textContent = '失败';
    addLocalLog(`识别失败：${e.message}`);
  } finally {
    busy = false;
  }
}

async function loadFile(file) {
  if (!file.type.startsWith('image/')) { addLocalLog('请上传图片文件（JPG / PNG）'); return; }
  stopCamera();
  stopTest();
  testBlob = file;
  showLocalPreview(file);
  const controls = $('testControl').checked;
  await sendBlob(file, controls);
  if (controls) testTimer = setInterval(() => sendBlob(testBlob, true), 2500);
}

// ---------- 上传：点击 + 拖拽（投放区和预览区都能接） ----------
dropzone.addEventListener('click', () => $('fileInput').click());
[dropzone, viewer].forEach(zone => {
  zone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) loadFile(file);
  });
});
// 拖到页面其他地方时不要让浏览器直接打开这张图
document.addEventListener('dragover', e => e.preventDefault());
document.addEventListener('drop', e => e.preventDefault());

$('fileInput').addEventListener('change', e => {
  const file = e.target.files[0];
  e.target.value = '';   // 允许连续选择同一个文件
  if (file) loadFile(file);
});

$('testControl').addEventListener('change', () => {
  stopTest();
  if (testBlob && $('testControl').checked) {
    sendBlob(testBlob, true);
    testTimer = setInterval(() => sendBlob(testBlob, true), 2500);
  }
});

// ---------- 摄像头 ----------
$('cameraButton').addEventListener('click', async () => {
  if (stream) { stopCamera(); return; }
  try {
    stopTest();
    stream = await navigator.mediaDevices.getUserMedia({video: {width: {ideal: 1280}, height: {ideal: 720}}, audio: false});
    video.srcObject = stream;
    viewer.className = 'viewer live';
    $('cameraButton').textContent = '停止摄像头';
    await video.play();
    capture();
    captureTimer = setInterval(capture, 1600);
  } catch (e) {
    addLocalLog(`无法打开摄像头：${e.message}`);
  }
});

function stopCamera() {
  if (captureTimer) clearInterval(captureTimer);
  captureTimer = null;
  if (stream) stream.getTracks().forEach(t => t.stop());
  stream = null;
  video.srcObject = null;
  overlay.getContext('2d').clearRect(0, 0, overlay.width, overlay.height);
  $('cameraButton').textContent = '启动摄像头';
  viewer.className = resultImage.getAttribute('src') ? 'viewer result' : 'viewer';
}

function stopTest() {
  if (testTimer) clearInterval(testTimer);
  testTimer = null;
}

function capture() {
  if (!stream || busy || !video.videoWidth) return;
  const maxWidth = 960;
  const scale = Math.min(1, maxWidth / video.videoWidth);
  captureCanvas.width = Math.round(video.videoWidth * scale);
  captureCanvas.height = Math.round(video.videoHeight * scale);
  captureCanvas.getContext('2d').drawImage(video, 0, 0, captureCanvas.width, captureCanvas.height);
  captureCanvas.toBlob(blob => blob && sendBlob(blob, true), 'image/jpeg', .76);
}

function drawOverlay(detections, width, height) {
  overlay.width = width;
  overlay.height = height;
  const ctx = overlay.getContext('2d');
  ctx.clearRect(0, 0, width, height);
  if (!detections || !detections.length) return;

  ctx.lineWidth = Math.max(2, width / 320);
  ctx.font = `bold ${Math.max(14, width / 45)}px "Microsoft YaHei", sans-serif`;
  ctx.textBaseline = 'top';
  detections.forEach(d => {
    const [x, y, w, h] = d.bbox;
    const label = `人员 ${(d.score * 100).toFixed(0)}%`;
    ctx.strokeStyle = '#ffb74d';
    ctx.fillStyle = '#ffb74d';
    ctx.strokeRect(x, y, w, h);
    const th = Math.max(20, width / 38);
    const tw = ctx.measureText(label).width + 10;
    ctx.fillRect(x, Math.max(0, y - th), tw, th);
    ctx.fillStyle = '#0f2027';
    ctx.fillText(label, x + 5, Math.max(2, y - th + 3));
  });
}

// 设备卡片是动态重建的，所以用事件委托挂在容器上。
$('devices').addEventListener('click', async e => {
  const el = e.target.closest('.device');
  if (!el) return;
  const on = !el.classList.contains('on');
  try {
    renderState(await api('/api/devices', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({device: el.dataset.device, on}),
    }));
  } catch (err) { addLocalLog(err.message); }
});

async function refresh() {
  try {
    const h = await api('/api/health');
    $('health').textContent = `${h.model} · 已就绪`;
    $('health').className = 'pill';
    renderState(h);
  } catch {
    $('health').textContent = '服务未连接';
    $('health').className = 'pill muted';
  }
}

refresh();
setInterval(async () => { try { renderState(await api('/api/state')); } catch { } }, 2000);

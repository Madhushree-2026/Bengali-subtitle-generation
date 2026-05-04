'use strict';

const BACKEND = 'http://localhost:8000';

// ── Mode badge ────────────────────────────────────────────────────────────
async function updateModeBadge() {
  const badge = document.getElementById('mode-badge');
  if (!badge) return;
  try {
    const r = await fetch(`${BACKEND}/mode`);
    const d = await r.json();
    badge.textContent = d.online ? '🌐 Online' : '📴 Offline';
    badge.className   = d.online ? 'badge badge-done' : 'badge badge-processing';
    badge.title       = d.mode;
  } catch {
    badge.textContent = '⚠ No backend';
    badge.className   = 'badge badge-error';
  }
}
updateModeBadge();
setInterval(updateModeBadge, 10000);

// ── State ─────────────────────────────────────────────────────────────────
let subtitles    = [];
let animFrame    = null;
let currentJobId = null;
let polling      = false;
let isYouTubeJob = false;

// Speaker label colours — Person 1 = gold, Person 2 = cyan, 3+ = other colours
const SPEAKER_COLORS = ['#FFD700', '#00E5FF', '#69FF47', '#FF6EC7', '#FF9500'];

// ── DOM refs ──────────────────────────────────────────────────────────────
const video          = document.getElementById('video-player');
const overlay        = document.getElementById('subtitle-overlay');
const emptyState     = document.getElementById('empty-state');
const videoContainer = document.getElementById('video-container');
const statusBadge    = document.getElementById('status-badge');
const progressText   = document.getElementById('progress-text');
const progressWrap   = document.getElementById('progress-bar-wrap');
const progressBar    = document.getElementById('progress-bar');
const subtitleCount  = document.getElementById('subtitle-count');
const urlBar         = document.getElementById('url-bar');
const urlInput       = document.getElementById('url-input');
const btnClear       = document.getElementById('btn-clear');
const fontSlider     = document.getElementById('font-size-slider');
const fontSizeVal    = document.getElementById('font-size-val');
const posSlider      = document.getElementById('position-slider');
const bgToggle       = document.getElementById('subtitle-bg-toggle');

// ── Subtitle binary search ────────────────────────────────────────────────
function findSubtitle(t) {
  let lo = 0, hi = subtitles.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const s   = subtitles[mid];
    if      (t < s.start) hi = mid - 1;
    else if (t > s.end)   lo = mid + 1;
    else                  return s;
  }
  // Show previous subtitle during small gaps (≤1.5 s)
  if (lo > 0 && lo < subtitles.length) {
    const prev = subtitles[lo - 1];
    const next = subtitles[lo];
    if ((next.start - prev.end) <= 1.5 && t < next.start) return prev;
  }
  return null;
}

// ── Render subtitle text with coloured speaker label ─────────────────────
//
// SRT text from backend looks like:
//   "Person 1:\nবাংলা লেখা"
//   "Person 2:\nআরেকটা লাইন"
//   "বাংলা লেখা"   (no speaker if diarization was skipped)
//
// We split on the first \n.  If the first part matches "Person N:"
// we wrap it in a coloured <span>, then append the rest as plain text.
//
function renderSubtitle(text) {
  if (!text) {
    overlay.innerHTML     = '';
    overlay.style.opacity = '0';
    return;
  }

  const newlinePos = text.indexOf('\n');

  if (newlinePos !== -1) {
    const firstLine = text.slice(0, newlinePos);   // e.g. "Person 1:"
    const rest      = text.slice(newlinePos + 1);  // Bengali text

    // Check if the first line is a speaker label like "Person 1:" or "Person 12:"
    if (/^Person \d+:$/.test(firstLine.trim())) {
      const num      = parseInt(firstLine.match(/\d+/)[0], 10);
      const color    = SPEAKER_COLORS[(num - 1) % SPEAKER_COLORS.length];
      const label    = firstLine.trim();  // "Person 1:"

      overlay.innerHTML =
        `<span style="display:block;color:${color};font-size:0.75em;font-weight:bold;` +
        `letter-spacing:0.05em;margin-bottom:2px;">${label}</span>` +
        `<span style="display:block;">${rest}</span>`;
      overlay.style.opacity = '1';
      return;
    }
  }

  // No speaker label — plain text
  overlay.textContent   = text;
  overlay.style.opacity = '1';
}

// ── Subtitle render loop ──────────────────────────────────────────────────
function startSubtitleLoop() {
  if (animFrame) cancelAnimationFrame(animFrame);
  let lastText = null;
  const tick = () => {
    const cur  = findSubtitle(video.currentTime);
    const text = cur ? cur.text : '';
    if (text !== lastText) {
      renderSubtitle(text);
      lastText = text;
    }
    animFrame = requestAnimationFrame(tick);
  };
  animFrame = requestAnimationFrame(tick);
}

function stopSubtitleLoop() {
  if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
}

// ── SRT parser ────────────────────────────────────────────────────────────
function timeToSec(t) {
  try {
    const s     = t.trim().replace(',', '.');
    const parts = s.split(':');
    if (parts.length !== 3) return NaN;
    return parseInt(parts[0]) * 3600 + parseInt(parts[1]) * 60 + parseFloat(parts[2]);
  } catch { return NaN; }
}

function parseSRT(raw) {
  if (!raw || !raw.trim()) return [];

  const text   = raw.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const blocks = text.trim().split(/\n\n+/);
  const result = [];

  for (const block of blocks) {
    const lines = block.trim().split('\n');
    let timeLine = -1;
    for (let j = 0; j < lines.length; j++) {
      if (lines[j].includes('-->')) { timeLine = j; break; }
    }
    if (timeLine === -1) continue;

    const arrow   = lines[timeLine].indexOf('-->');
    const start   = timeToSec(lines[timeLine].slice(0, arrow).trim());
    const end     = timeToSec(lines[timeLine].slice(arrow + 3).trim());
    if (isNaN(start) || isNaN(end) || end <= start) continue;

    // Everything after the timing line is the subtitle text
    // (may include "Person N:\n" + Bengali on the next line)
    const subText = lines.slice(timeLine + 1).join('\n').trim();
    if (!subText) continue;

    result.push({ start, end, text: subText });
  }

  return result;
}

// ── Status / progress helpers ─────────────────────────────────────────────
function setStatus(label, cls) {
  statusBadge.textContent = label;
  statusBadge.className   = 'badge ' + cls;
}

function setProgress(pct) {
  progressWrap.classList.toggle('hidden', pct <= 0 || pct >= 100);
  progressBar.style.width  = pct + '%';
  progressText.textContent = pct > 0 && pct < 100 ? Math.round(pct) + '%' : '';
}

function showOverlayMsg(msg) {
  overlay.textContent   = msg;
  overlay.style.opacity = msg ? '1' : '0';
}

// ── View transitions ──────────────────────────────────────────────────────
function showVideoView() {
  emptyState.classList.add('hidden');
  videoContainer.classList.remove('hidden');
  btnClear.disabled = false;
}

function showEmptyView() {
  emptyState.classList.remove('hidden');
  videoContainer.classList.add('hidden');
  btnClear.disabled = true;
  stopSubtitleLoop();
  overlay.innerHTML     = '';
  overlay.style.opacity = '0';
  subtitles             = [];
  isYouTubeJob          = false;
  subtitleCount.textContent = '';
  setStatus('Idle', 'badge-idle');
  setProgress(0);
  video.pause();
  video.removeAttribute('src');
  video.load();
  currentJobId = null;
  polling      = false;
}

// ── Load local file ───────────────────────────────────────────────────────
function loadLocalFile(filePath) {
  isYouTubeJob = false;

  const safePath = filePath
    .replace(/\\/g, '/')
    .split('/')
    .map(segment => encodeURIComponent(segment))
    .join('/');
  const fileURI = 'file:///' + safePath;

  video.src = fileURI;
  video.load();
  showVideoView();

  subtitles = [];
  showOverlayMsg('⏳ Generating Bengali subtitles…');
  setStatus('Processing…', 'badge-processing');
  setProgress(5);

  startSubtitleProcess({ type: 'file', path: filePath });
}

// ── Load YouTube / direct URL ─────────────────────────────────────────────
function loadURL(url) {
  isYouTubeJob = url.includes('youtube.com') || url.includes('youtu.be');

  video.removeAttribute('src');
  video.load();
  showVideoView();

  subtitles = [];
  showOverlayMsg('⏳ Downloading & processing…');
  setStatus('Processing…', 'badge-processing');
  setProgress(5);

  startSubtitleProcess({ type: 'url', url });
}

// ── Start subtitle pipeline ───────────────────────────────────────────────
async function startSubtitleProcess(source) {
  polling = true;

  try {
    let jobId;

    if (source.type === 'file') {
      const result = await window.electronAPI.readFileAsBase64(source.path);
      if (result.error) throw new Error('Could not read file: ' + result.error);

      const binary   = atob(result.base64);
      const bytes    = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      const fileBlob = new Blob([bytes], { type: result.mimeType });

      const ext  = source.path.split('.').pop() || 'mp4';
      const form = new FormData();
      form.append('file', fileBlob, 'video.' + ext);
      form.append('source_language', 'en');

      const resp = await fetch(`${BACKEND}/transcribe/file`, { method: 'POST', body: form });
      if (!resp.ok) throw new Error('Upload failed: ' + resp.status);
      jobId = (await resp.json()).job_id;

    } else {
      const resp = await fetch(`${BACKEND}/transcribe`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ video_url: source.url, force_refresh: true }),
      });
      if (!resp.ok) throw new Error('Request failed: ' + resp.status);
      jobId = (await resp.json()).job_id;
    }

    currentJobId = jobId;
    await pollUntilDone(jobId);

  } catch (err) {
    console.error('[pipeline error]', err);
    setStatus('Error', 'badge-error');
    setProgress(0);
    showOverlayMsg('❌ ' + err.message);
  }
}

// ── Poll until done ───────────────────────────────────────────────────────
async function pollUntilDone(jobId) {
  let videoLoadedFromBackend = false;

  while (polling) {
    await new Promise(r => setTimeout(r, 3000));
    if (!polling || currentJobId !== jobId) return;

    let s;
    try {
      const resp = await fetch(`${BACKEND}/jobs/${jobId}`);
      if (!resp.ok) continue;
      s = await resp.json();
    } catch { continue; }

    setProgress(s.progress_pct || 0);

    if (isYouTubeJob && s.video_ready && !videoLoadedFromBackend) {
      videoLoadedFromBackend = true;
      video.src = `${BACKEND}/video/${jobId}`;
      video.load();
      showOverlayMsg('⏳ Generating subtitles…');
    }

    if (s.status === 'done') {
      let srtRaw = '';
      try {
        const srtResp = await fetch(`${BACKEND}/download/${jobId}`);
        if (!srtResp.ok) throw new Error(`SRT fetch ${srtResp.status}`);
        srtRaw = await srtResp.text();
      } catch (e) {
        console.error('[SRT fetch]', e);
        showOverlayMsg('❌ Could not load subtitles');
        setStatus('Error', 'badge-error');
        polling = false;
        return;
      }

      subtitles = parseSRT(srtRaw);
      console.log('[SRT] parsed:', subtitles.length, 'subtitles');
      if (subtitles.length > 0) {
        console.log('[SRT] first entry:', subtitles[0]);
      }

      subtitleCount.textContent = subtitles.length + ' subtitles loaded';
      setStatus('Done ✓', 'badge-done');
      setProgress(100);

      overlay.innerHTML     = '';
      overlay.style.opacity = '0';
      startSubtitleLoop();

      polling = false;
      return;
    }

    if (s.status === 'failed') {
      showOverlayMsg('❌ ' + (s.message || 'Processing failed'));
      setStatus('Error', 'badge-error');
      polling = false;
      return;
    }
  }
}

// ── Button wiring ─────────────────────────────────────────────────────────
document.getElementById('btn-open-file').addEventListener('click', async () => {
  const filePath = await window.electronAPI.openFile();
  if (filePath) loadLocalFile(filePath);
});

document.getElementById('btn-empty-file').addEventListener('click', async () => {
  const filePath = await window.electronAPI.openFile();
  if (filePath) loadLocalFile(filePath);
});

document.getElementById('btn-open-url').addEventListener('click', () => {
  urlBar.classList.toggle('hidden');
  if (!urlBar.classList.contains('hidden')) urlInput.focus();
});

document.getElementById('btn-empty-url').addEventListener('click', () => {
  urlBar.classList.remove('hidden');
  urlInput.focus();
});

document.getElementById('btn-url-cancel').addEventListener('click', () => {
  urlBar.classList.add('hidden');
  urlInput.value = '';
});

document.getElementById('btn-url-go').addEventListener('click', () => {
  const url = urlInput.value.trim();
  if (!url) return;
  urlBar.classList.add('hidden');
  urlInput.value = '';
  loadURL(url);
});

urlInput.addEventListener('keydown', e => {
  if (e.key === 'Enter')  document.getElementById('btn-url-go').click();
  if (e.key === 'Escape') document.getElementById('btn-url-cancel').click();
});

btnClear.addEventListener('click', showEmptyView);

// ── Subtitle appearance controls ──────────────────────────────────────────
fontSlider.addEventListener('input', () => {
  const v = fontSlider.value;
  overlay.style.fontSize  = v + 'px';
  fontSizeVal.textContent = v + 'px';
});

posSlider.addEventListener('input', () => {
  overlay.style.bottom = posSlider.value + '%';
});

bgToggle.addEventListener('change', () => {
  overlay.style.background = bgToggle.checked ? 'rgba(0,0,0,0.55)' : 'transparent';
});
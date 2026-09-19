/* ═══════════════════════════════════════════════════════════════════════════
   VideoUpload Pro — app.js
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── State ─────────────────────────────────────────────────────────────────── */
let selectedFile    = null;
let currentTaskId   = null;
let pollInterval    = null;
let igSaved         = false;
let ytConnected     = false;
let sessionUploads  = 0;
let sessionSuccess  = 0;
let connectedCount  = 0;

/* ═══════════════════════════════════════════════════════════════════════════
   TOAST
   ═══════════════════════════════════════════════════════════════════════════ */
function showToast(msg, type = '') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className   = 'toast show' + (type ? ' ' + type : '');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.className = 'toast'; }, 3800);
}

/* ═══════════════════════════════════════════════════════════════════════════
   WORKFLOW STEPS
   ═══════════════════════════════════════════════════════════════════════════ */
function gotoStep(n) {
  for (let i = 1; i <= 4; i++) {
    const el = document.getElementById('step-' + i);
    if (!el) continue;
    el.classList.remove('active', 'done');
    if (i < n)  el.classList.add('done');
    if (i === n) el.classList.add('active');
  }
}

function advanceWorkflow() {
  // Determine current furthest step
  if (!ytConnected && !igSaved) return gotoStep(1);
  if (!selectedFile)            return gotoStep(2);
  if (!document.getElementById('title').value.trim()) return gotoStep(3);
  gotoStep(4);
}

function scrollToSection(id) {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ═══════════════════════════════════════════════════════════════════════════
   YOUTUBE AUTH
   ═══════════════════════════════════════════════════════════════════════════ */
async function connectYouTube() {
  const btn = document.getElementById('yt-connect-btn');
  btn.disabled = true;
  btn.textContent = '⏳ Opening…';

  try {
    const res  = await fetch('/youtube/auth');
    const data = await res.json();

    if (data.error) {
      showToast(data.error, 'error');
      btn.disabled = false;
      btn.textContent = 'Connect';
      return;
    }

    const popup = window.open(data.auth_url, 'yt_auth',
      'width=520,height=660,left=200,top=80,resizable=yes');

    const handler = (e) => {
      if (e.data?.type === 'youtube_auth_success') {
        window.removeEventListener('message', handler);
        if (popup && !popup.closed) popup.close();
        markYouTubeConnected();
        showToast('YouTube connected successfully!', 'success');
      }
    };
    window.addEventListener('message', handler);

    // Poll in case postMessage fails (e.g. different origin edge case)
    const poller = setInterval(async () => {
      if (popup && popup.closed) {
        clearInterval(poller);
        window.removeEventListener('message', handler);
        const check = await fetch('/youtube/status').then(r => r.json()).catch(() => ({}));
        if (check.authenticated) {
          markYouTubeConnected();
          showToast('YouTube connected!', 'success');
        } else {
          btn.disabled = false;
          btn.textContent = 'Connect';
        }
      }
    }, 800);

  } catch (err) {
    showToast('Failed to start YouTube auth: ' + err.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Connect';
  }
}

function markYouTubeConnected() {
  ytConnected = true;

  // Platform item
  const item = document.getElementById('yt-platform-item');
  if (item) item.classList.add('connected');

  // Badge
  const badge = document.getElementById('yt-status-badge');
  if (badge) { badge.textContent = 'Connected'; badge.className = 'badge badge-connected'; }

  // Button
  const btn = document.getElementById('yt-connect-btn');
  if (btn) { btn.textContent = '✓ Connected'; btn.disabled = true; btn.className = 'btn btn-ghost btn-sm'; }

  // Sidebar dot
  const dot = document.getElementById('nav-yt-dot');
  if (dot) { dot.style.background = '#10b981'; dot.style.boxShadow = '0 0 6px #10b981'; }

  // Stats
  document.getElementById('stat-yt-count').textContent = 'Auth ✓';

  // Session summary
  document.getElementById('session-yt').textContent = '✓ Connected';

  // Toggle sub-label
  const sub = document.getElementById('yt-toggle-sub');
  if (sub) sub.textContent = 'Connected · Ready to publish';

  updateConnectionsBadge();
  advanceWorkflow();
}

async function checkYouTubeStatus() {
  try {
    const res  = await fetch('/youtube/status');
    const data = await res.json();
    if (data.authenticated) markYouTubeConnected();
  } catch (_) { /* ignore */ }
}

/* ═══════════════════════════════════════════════════════════════════════════
   INSTAGRAM OAUTH
   ═══════════════════════════════════════════════════════════════════════════ */
async function connectInstagram() {
  const btn = document.getElementById('ig-connect-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Opening…'; }

  try {
    const res  = await fetch('/instagram/auth');
    const data = await res.json();

    if (data.error) {
      showToast(data.error, 'error');
      if (btn) { btn.disabled = false; btn.textContent = 'Connect'; }
      return;
    }

    const popup = window.open(data.auth_url, 'ig_auth',
      'width=560,height=700,left=200,top=80,resizable=yes');

    // Listen for postMessage from auth_success page
    const handler = (e) => {
      if (e.data?.type === 'instagram_auth_success') {
        window.removeEventListener('message', handler);
        if (popup && !popup.closed) popup.close();
        markInstagramConnected();
        showToast('Instagram connected successfully!', 'success');
      }
    };
    window.addEventListener('message', handler);

    // Fallback poll in case popup closes without postMessage
    const poller = setInterval(async () => {
      if (popup && popup.closed) {
        clearInterval(poller);
        window.removeEventListener('message', handler);
        const check = await fetch('/instagram/status').then(r => r.json()).catch(() => ({}));
        if (check.authenticated) {
          markInstagramConnected();
          showToast('Instagram connected!', 'success');
        } else {
          if (btn) { btn.disabled = false; btn.textContent = 'Connect'; }
        }
      }
    }, 800);

  } catch (err) {
    showToast('Failed to start Instagram auth: ' + err.message, 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Connect'; }
  }
}

function markInstagramConnected() {
  igSaved = true;

  const item  = document.getElementById('ig-platform-item');
  if (item)  item.classList.add('connected');

  const badge = document.getElementById('ig-status-badge');
  if (badge) { badge.textContent = 'Connected'; badge.className = 'badge badge-connected'; }

  const btn = document.getElementById('ig-connect-btn');
  if (btn)  { btn.textContent = '✓ Connected'; btn.disabled = true; btn.className = 'btn btn-ghost btn-sm'; }

  const dot = document.getElementById('nav-ig-dot');
  if (dot)  { dot.style.background = '#10b981'; dot.style.boxShadow = '0 0 6px #10b981'; }

  document.getElementById('stat-ig-count').textContent = 'Auth ✓';

  const sessionIg = document.getElementById('session-ig');
  if (sessionIg) sessionIg.textContent = '✓ Connected';

  const sub = document.getElementById('ig-toggle-sub');
  if (sub) sub.textContent = 'Connected · Ready to publish';

  updateConnectionsBadge();
  advanceWorkflow();
}

async function checkInstagramStatus() {
  try {
    const res  = await fetch('/instagram/status');
    const data = await res.json();
    if (data.authenticated) markInstagramConnected();
  } catch (_) { /* ignore */ }
}

function updateConnectionsBadge() {
  connectedCount = (ytConnected ? 1 : 0) + (igSaved ? 1 : 0);
  const el = document.getElementById('connections-badge');
  if (!el) return;
  el.textContent = connectedCount + ' / 2 Connected';
  el.className = connectedCount === 2
    ? 'badge badge-connected'
    : connectedCount === 1
      ? 'badge badge-ready'
      : 'badge badge-disconnected';
}

/* ═══════════════════════════════════════════════════════════════════════════
   PLATFORM TOGGLES
   ═══════════════════════════════════════════════════════════════════════════ */
function bindPlatformToggles() {
  const ytCb = document.getElementById('yt-checkbox');
  const igCb = document.getElementById('ig-checkbox');
  const ytTg = document.getElementById('yt-toggle');
  const igTg = document.getElementById('ig-toggle');

  if (ytCb && ytTg) {
    ytCb.addEventListener('change', () => {
      ytTg.classList.toggle('selected-yt', ytCb.checked);
      updateSessionPlatforms();
    });
  }
  if (igCb && igTg) {
    igCb.addEventListener('change', () => {
      igTg.classList.toggle('selected-ig', igCb.checked);
      updateSessionPlatforms();
    });
  }
}

function updateSessionPlatforms() {
  const platforms = [...document.querySelectorAll('input[name=platforms]:checked')].map(c => c.value);
  const el = document.getElementById('session-platforms');
  if (el) el.textContent = platforms.length ? platforms.map(p => p.charAt(0).toUpperCase() + p.slice(1)).join(', ') : 'None';
}

/* ═══════════════════════════════════════════════════════════════════════════
   FILE SELECTION & DRAG-DROP
   ═══════════════════════════════════════════════════════════════════════════ */
function bindDropZone() {
  const zone = document.getElementById('drop-zone');
  if (!zone) return;

  zone.addEventListener('dragover', e => {
    e.preventDefault();
    zone.classList.add('drag-over');
  });
  zone.addEventListener('dragleave', e => {
    if (!zone.contains(e.relatedTarget)) zone.classList.remove('drag-over');
  });
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    const f = e.dataTransfer.files[0];
    if (f && f.type.startsWith('video/')) setFile(f);
    else showToast('Please drop a video file', 'error');
  });
}

function onFileSelected(input) {
  if (input.files[0]) setFile(input.files[0]);
}

function setFile(f) {
  selectedFile = f;

  document.getElementById('drop-zone-inner').style.display = 'none';
  const preview = document.getElementById('file-preview');
  preview.style.display = 'flex';

  document.getElementById('file-name').textContent = f.name;
  document.getElementById('file-size').textContent = formatBytes(f.size);

  // Session summary
  const sf = document.getElementById('session-file');
  if (sf) sf.textContent = f.name.length > 22 ? f.name.slice(0, 20) + '…' : f.name;

  advanceWorkflow();
  showToast('Video selected: ' + f.name, 'success');
}

function removeFile(e) {
  e.stopPropagation();
  selectedFile = null;
  document.getElementById('video-input').value = '';
  document.getElementById('drop-zone-inner').style.display = 'flex';
  document.getElementById('file-preview').style.display = 'none';

  const sf = document.getElementById('session-file');
  if (sf) sf.textContent = 'None';

  advanceWorkflow();
}

function formatBytes(b) {
  if (b < 1024 ** 2) return (b / 1024).toFixed(1) + ' KB';
  if (b < 1024 ** 3) return (b / 1024 ** 2).toFixed(1) + ' MB';
  return (b / 1024 ** 3).toFixed(2) + ' GB';
}

/* ═══════════════════════════════════════════════════════════════════════════
   UPLOAD
   ═══════════════════════════════════════════════════════════════════════════ */
async function startUpload(e) {
  e.preventDefault();

  const platforms = [...document.querySelectorAll('input[name=platforms]:checked')].map(c => c.value);

  if (!selectedFile)          { showToast('Please select a video file', 'error');   return; }
  if (platforms.length === 0) { showToast('Select at least one platform', 'error'); return; }
  if (!document.getElementById('title').value.trim()) {
    showToast('Please enter a video title', 'error'); return;
  }
  if (platforms.includes('youtube') && !ytConnected) {
    showToast('Connect your YouTube account first', 'error'); return;
  }
  if (platforms.includes('instagram') && !igSaved) {
    showToast('Connect your Instagram account first', 'error'); return;
  }

  // Build FormData
  const fd = new FormData(document.getElementById('upload-form'));
  fd.set('video', selectedFile, selectedFile.name);

  // Show progress card
  showProgressCard(platforms);

  // Disable upload button
  const btn = document.getElementById('upload-btn');
  btn.disabled = true;
  btn.innerHTML = '⏳&nbsp; Publishing…';

  sessionUploads++;
  document.getElementById('stat-uploads').textContent = sessionUploads;

  try {
    const res  = await fetch('/upload', { method: 'POST', body: fd });
    const data = await res.json();

    if (!res.ok) {
      showToast(data.error || 'Upload failed', 'error');
      resetBtn(btn);
      return;
    }

    currentTaskId = data.task_id;
    startPolling(data.platforms, btn);

  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
    resetBtn(btn);
  }
}

function showProgressCard(platforms) {
  const card = document.getElementById('progress-card');
  if (card) card.style.display = 'block';

  const ytItem = document.getElementById('yt-progress-item');
  const igItem = document.getElementById('ig-progress-item');

  if (ytItem) ytItem.style.display = platforms.includes('youtube')   ? 'block' : 'none';
  if (igItem) igItem.style.display = platforms.includes('instagram') ? 'block' : 'none';

  resetProgressItem('yt');
  resetProgressItem('ig');

  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function resetBtn(btn) {
  btn.disabled = false;
  btn.innerHTML = '🚀&nbsp; Publish Now';
}

function resetProgressItem(p) {
  const bar    = document.getElementById(p + '-progress-bar');
  const msg    = document.getElementById(p + '-progress-message');
  const status = document.getElementById(p + '-progress-status');
  const item   = document.getElementById(p + '-progress-item');

  if (bar)    bar.style.width = '0%';
  if (msg)    msg.textContent = '';
  if (status) { status.textContent = 'Waiting…'; status.className = 'progress-status-text'; }
  if (item)   { item.classList.remove('uploading', 'success', 'error-state'); }
}

/* ═══════════════════════════════════════════════════════════════════════════
   POLLING
   ═══════════════════════════════════════════════════════════════════════════ */
function startPolling(platforms, btn) {
  clearInterval(pollInterval);

  pollInterval = setInterval(async () => {
    try {
      const res  = await fetch('/status/' + currentTaskId);
      const data = await res.json();
      let allDone = true;

      platforms.forEach(p => {
        const info = data[p];
        if (!info) { allDone = false; return; }
        updateProgressItem(p, info);
        if (info.status !== 'success' && info.status !== 'error') allDone = false;
      });

      if (allDone) {
        clearInterval(pollInterval);
        resetBtn(btn);

        const allSuccess = platforms.every(p => data[p]?.status === 'success');
        if (allSuccess) {
          sessionSuccess++;
          document.getElementById('stat-success').textContent = sessionSuccess;
          showToast('🎉 All uploads published successfully!', 'success');
          gotoStep(4);
        } else {
          showToast('Upload finished — check progress for details', 'error');
        }
      }
    } catch (_) { /* keep polling on transient errors */ }
  }, 1200);
}

function updateProgressItem(p, info) {
  const bar    = document.getElementById(p + '-progress-bar');
  const msg    = document.getElementById(p + '-progress-message');
  const status = document.getElementById(p + '-progress-status');
  const item   = document.getElementById(p + '-progress-item');

  if (bar) bar.style.width = (info.progress || 0) + '%';
  if (msg) msg.textContent = info.message || '';

  if (info.status === 'success') {
    if (status) { status.textContent = '✓ Done'; status.className = 'progress-status-text success'; }
    if (bar)    bar.style.width = '100%';
    if (item)   { item.classList.remove('uploading','error-state'); item.classList.add('success'); }
  } else if (info.status === 'error') {
    if (status) { status.textContent = '✕ Error'; status.className = 'progress-status-text error'; }
    if (item)   { item.classList.remove('uploading','success'); item.classList.add('error-state'); }
  } else {
    if (status) { status.textContent = (info.progress || 0) + '%'; status.className = 'progress-status-text active'; }
    if (item)   { item.classList.add('uploading'); }
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
   INIT
   ═══════════════════════════════════════════════════════════════════════════ */
document.addEventListener('DOMContentLoaded', () => {
  checkYouTubeStatus();
  checkInstagramStatus();
  bindDropZone();
  bindPlatformToggles();
  updateConnectionsBadge();
  gotoStep(1);
});

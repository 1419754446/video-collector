(function () {
  'use strict';

  const params = new URLSearchParams(location.search);
  const bridge = window.chrome && window.chrome.webview;
  const video = document.getElementById('video');
  const player = document.getElementById('player');
  const filePath = params.get('path') || '';
  const fileName = params.get('name') || '视频预览';
  const fileSize = Number(params.get('size') || 0);

  const $ = (id) => document.getElementById(id);
  const seek = $('seek');
  const seekBuffer = $('seekBuffer');
  const seekPlayed = $('seekPlayed');
  const centerPlay = $('centerPlay');
  const iconPlay = $('iconPlay');
  const iconPause = $('iconPause');
  const loading = $('loading');
  const errorBox = $('error');

  // --------------------------------------------------------------- 与原生通讯
  function post(cmd, extra) {
    if (!bridge) return;
    const parts = ['cmd=' + encodeURIComponent(cmd)];
    for (const key in extra || {}) {
      parts.push(key + '=' + encodeURIComponent(extra[key]));
    }
    bridge.postMessage(parts.join('&'));
  }

  if (bridge) {
    bridge.addEventListener('message', (event) => {
      const msg = event.data;
      if (!msg || msg.type !== 'theme') return;
      document.documentElement.setAttribute(
        'data-theme',
        msg.value === 'light' ? 'light' : 'dark'
      );
    });
  }

  // --------------------------------------------------------------- 初始化
  $('fileName').textContent = fileName;
  $('path').textContent = filePath;
  document.title = fileName;

  const subParts = [];
  if (fileSize > 0) subParts.push(fmtSize(fileSize));
  $('fileSub').textContent = subParts.join(' · ');

  const src = params.get('p') || '';
  if (src) {
    // p 是路径本身（已由 URLSearchParams 解一次码）。用它从本窗口 origin 拼出 /media URL，
    // encodeURIComponent 会正确处理反斜杠、空格、+、% 等字符（H6）。
    video.src = location.origin + '/media?p=' + encodeURIComponent(src) +
      '&v=' + (fileSize || 0);
    video.load();
  } else {
    showError('没有可播放的视频地址。');
  }

  post('ready');

  // --------------------------------------------------------------- 工具
  function fmtSize(bytes) {
    let n = Number(bytes) || 0;
    if (n < 1024) return n + ' B';
    const units = ['KB', 'MB', 'GB', 'TB'];
    for (const unit of units) {
      n /= 1024;
      if (n < 1024) return n.toFixed(1) + ' ' + unit;
    }
    return n.toFixed(1) + ' PB';
  }

  function fmtTime(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const mm = h ? String(m).padStart(2, '0') : String(m);
    return (h ? h + ':' : '') + mm + ':' + String(sec).padStart(2, '0');
  }

  function setPlayIcon(playing) {
    iconPlay.style.display = playing ? 'none' : '';
    iconPause.style.display = playing ? '' : 'none';
    centerPlay.classList.toggle('hidden', playing);
  }

  function showError(text) {
    loading.classList.add('hidden');
    errorBox.hidden = false;
    if (text) {
      const sub = errorBox.querySelector('.error-sub');
      if (sub) sub.textContent = text;
    }
  }

  // --------------------------------------------------------------- 播放控制
  function togglePlay() {
    if (video.paused || video.ended) {
      video.play().catch(() => showError());
    } else {
      video.pause();
    }
  }

  centerPlay.addEventListener('click', togglePlay);
  $('btnPlay').addEventListener('click', togglePlay);
  $('viewport').addEventListener('dblclick', toggleFullscreen);

  // 独立窗口经由 ShowPreviewWindow 原生打开，页面自身没有用户点击手势，带声音的
  // video.play() 会被 Chromium 自动播放策略静默拦截。先静音自动播放（静音始终允许），
  // 开始后再恢复用户设置的声音；确实解码不了则由 error 事件给出提示。
  function startPlayback() {
    if (video.error) {
      showError('内置播放器无法解码此视频（格式或编码不受支持）。');
      return;
    }
    const wantSound = !video.muted && video.volume > 0;
    if (!wantSound) {
      video.play().catch(() => showError());
      return;
    }
    video.muted = true;
    video.play()
      .then(() => { if (video.volume > 0) video.muted = false; syncMuteIcon(); })
      .catch(() => { video.muted = false; });
  }

  video.addEventListener('play', () => {
    setPlayIcon(true);
    scheduleIdle();
  });
  video.addEventListener('pause', () => setPlayIcon(false));
  video.addEventListener('ended', () => setPlayIcon(false));

  video.addEventListener('loadedmetadata', () => {
    const parts = [];
    if (video.videoWidth && video.videoHeight) {
      parts.push(video.videoWidth + ' × ' + video.videoHeight);
    }
    if (video.duration && isFinite(video.duration)) parts.push(fmtTime(video.duration));
    if (subParts.length || parts.length) {
      $('fileSub').textContent = subParts.concat(parts).join(' · ');
    }
    updateTime();
    loading.classList.add('hidden');
  });

  video.addEventListener('progress', () => updateBuffer());
  video.addEventListener('durationchange', updateBuffer);
  video.addEventListener('timeupdate', () => {
    updateTime();
    updateSeek();
  });
  video.addEventListener('waiting', () => loading.classList.remove('hidden'));
  video.addEventListener('playing', () => loading.classList.add('hidden'));
  video.addEventListener('canplay', () => loading.classList.add('hidden'));
  video.addEventListener('error', () => {
    if (!video.error) return;
    const code = video.error.code;
    showError(
      code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED
        ? '该视频的封装格式或编码不受支持（常见于 MKV/AVI 的部分编码），请使用系统播放器。'
        : '视频加载失败，文件可能已被移动或删除。'
    );
  });

  function updateTime() {
    $('time').textContent =
      fmtTime(video.currentTime || 0) + ' / ' + fmtTime(video.duration || 0);
  }

  function updateSeek() {
    const duration = video.duration;
    const ratio =
      duration && isFinite(duration) ? Math.min(1, (video.currentTime || 0) / duration) : 0;
    seekPlayed.style.width = (ratio * 100).toFixed(3) + '%';
  }

  function updateBuffer() {
    const duration = video.duration;
    if (!duration || !isFinite(duration) || !video.buffered || !video.buffered.length) {
      seekBuffer.style.width = '0%';
      return;
    }
    const end = video.buffered.end(video.buffered.length - 1);
    seekBuffer.style.width = Math.min(100, (end / duration) * 100).toFixed(3) + '%';
  }

  // --------------------------------------------------------------- 进度拖动
  let dragging = false;

  function seekRatioFromEvent(event) {
    const rect = seek.getBoundingClientRect();
    const ratio = rect.width > 0 ? (event.clientX - rect.left) / rect.width : 0;
    return Math.min(1, Math.max(0, ratio));
  }

  function applySeek(event) {
    const duration = video.duration;
    if (!duration || !isFinite(duration)) return;
    video.currentTime = duration * seekRatioFromEvent(event);
    seekPlayed.style.width = (seekRatioFromEvent(event) * 100).toFixed(3) + '%';
  }

  seek.addEventListener('pointerdown', (event) => {
    dragging = true;
    seek.setPointerCapture(event.pointerId);
    applySeek(event);
  });
  seek.addEventListener('pointermove', (event) => {
    if (dragging) applySeek(event);
  });
  seek.addEventListener('pointerup', (event) => {
    dragging = false;
    applySeek(event);
    try {
      seek.releasePointerCapture(event.pointerId);
    } catch (err) {}
  });

  // --------------------------------------------------------------- 音量 / 速度
  const volume = $('volume');
  const savedVolume = Number(localStorage.getItem('vc.volume'));
  if (savedVolume >= 0 && savedVolume <= 1) volume.value = String(savedVolume);
  video.volume = Number(volume.value);

  volume.addEventListener('input', () => {
    video.volume = Number(volume.value);
    video.muted = video.volume === 0;
    localStorage.setItem('vc.volume', volume.value);
    syncMuteIcon();
  });

  function syncMuteIcon() {
    const muted = video.muted || video.volume === 0;
    $('iconVolume').style.display = muted ? 'none' : '';
    $('iconMuted').style.display = muted ? '' : 'none';
  }

  $('btnMute').addEventListener('click', () => {
    video.muted = !video.muted;
    syncMuteIcon();
  });
  syncMuteIcon();

  const rate = $('rate');
  const savedRate = localStorage.getItem('vc.rate');
  if (savedRate) rate.value = savedRate;
  video.playbackRate = Number(rate.value) || 1;
  rate.addEventListener('change', () => {
    video.playbackRate = Number(rate.value) || 1;
    localStorage.setItem('vc.rate', rate.value);
  });

  // --------------------------------------------------------------- 全屏 / 空闲
  function toggleFullscreen() {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if (player.requestFullscreen) {
      player.requestFullscreen().catch(() => {});
    }
  }

  $('btnFull').addEventListener('click', toggleFullscreen);

  let idleTimer = null;
  let interacted = false;  // 用户真正在窗口内操作过，才允许自动隐藏控制条

  function scheduleIdle() {
    clearTimeout(idleTimer);
    player.classList.remove('idle');
    if (video.paused) return;
    idleTimer = setTimeout(() => player.classList.add('idle'), 2600);
  }

  ['mousemove', 'pointerdown', 'keydown'].forEach((name) => {
    window.addEventListener(name, () => {
      interacted = true;
      scheduleIdle();
    });
  });

  player.addEventListener('mouseleave', () => {
    // 刚打开窗口时鼠标可能不在窗口内，避免一打开就看不到标题/控制条
    if (interacted && !video.paused) player.classList.add('idle');
  });

  // --------------------------------------------------------------- 按钮
  $('btnOpen').addEventListener('click', () => post('open', { path: filePath }));
  $('errOpen').addEventListener('click', () => post('open', { path: filePath }));
  $('btnReveal').addEventListener('click', () => post('reveal', { path: filePath }));
  $('btnClose').addEventListener('click', () => post('close'));

  $('path').addEventListener('click', () => {
    if (!filePath) return;
    const done = () => showToast('路径已复制');
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(filePath).then(done, () => showToast('复制失败'));
    } else {
      showToast('复制失败');
    }
  });

  // --------------------------------------------------------------- 提示
  let toastTimer = null;

  function showToast(text) {
    let box = document.getElementById('pvToast');
    if (!box) {
      box = document.createElement('div');
      box.id = 'pvToast';
      box.style.cssText =
        'position:absolute;left:50%;bottom:96px;transform:translateX(-50%);z-index:9;' +
        'padding:8px 14px;border-radius:10px;font-size:12px;pointer-events:none;' +
        'background:rgba(20,24,34,.92);color:#e9edf6;border:1px solid rgba(255,255,255,.14);' +
        'box-shadow:0 12px 30px rgba(0,0,0,.35);transition:opacity .2s ease';
      document.body.appendChild(box);
    }
    box.textContent = text;
    box.style.opacity = '1';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      box.style.opacity = '0';
    }, 1800);
  }

  // --------------------------------------------------------------- 键盘
  window.addEventListener('keydown', (event) => {
    if (event.target && /INPUT|SELECT|TEXTAREA/.test(event.target.tagName)) return;
    const step = event.shiftKey ? 10 : 5;
    switch (event.key) {
      case ' ':
        event.preventDefault();
        togglePlay();
        break;
      case 'ArrowLeft':
        event.preventDefault();
        video.currentTime = Math.max(0, video.currentTime - step);
        break;
      case 'ArrowRight':
        event.preventDefault();
        video.currentTime = Math.min(video.duration || 0, video.currentTime + step);
        break;
      case 'ArrowUp':
        event.preventDefault();
        volume.value = String(Math.min(1, Number(volume.value) + 0.05));
        volume.dispatchEvent(new Event('input'));
        break;
      case 'ArrowDown':
        event.preventDefault();
        volume.value = String(Math.max(0, Number(volume.value) - 0.05));
        volume.dispatchEvent(new Event('input'));
        break;
      case 'm':
      case 'M':
        video.muted = !video.muted;
        syncMuteIcon();
        break;
      case 'f':
      case 'F':
        toggleFullscreen();
        break;
      case 'Escape':
        if (document.fullscreenElement) document.exitFullscreen();
        else post('close');
        break;
      default:
        break;
    }
  });

  // 自动播放（带声音的自动播放受 Chromium 策略限制，先静音启动再恢复声音）
  startPlayback();
})();

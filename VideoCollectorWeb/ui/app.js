/* =========================================================================
   视频集中整理工具 · 前端逻辑
   与原生宿主通过 chrome.webview.postMessage 通讯（URL 编码的 key=value）
   ========================================================================= */
'use strict';

(() => {
  const DEFAULT_EXTS =
    'mp4,mkv,mov,avi,wmv,flv,webm,m4v,mpg,mpeg,ts,m2ts,mts,3gp,rmvb,rm,vob,ogv,f4v,divx';
  const ROW_H = 58;
  // 缩略图加载结果缓存（key = id|size）：成功/失败分开记，避免把失败项渲染成裂图
  const thumbOk = new Set();
  const thumbFail = new Set();
  const RECENT_MAX = 6;

  // 与 Python 版 app/main_window.py 中的 SHORTCUTS / SHORTCUT_TIPS 保持一致
  const SHORTCUTS = [
    ['Ctrl+F', '聚焦筛选框'],
    ['Ctrl+A', '全选当前可见行'],
    ['Ctrl+Shift+A', '取消所有选中'],
    ['Enter', '预览选中的视频'],
    ['Delete', '把选中项移入回收站'],
    ['Esc', '清除筛选 / 取消选中 / 关闭菜单'],
    ['F1', '显示本说明'],
  ];
  const SHORTCUT_TIPS = [
    '点击统计胶囊可按状态筛选（如「仅看失败」），再点一次或按 Esc 取消',
    '双击列表行可预览；右键行可打开、定位、复制路径或删除',
    '把文件夹拖到窗口上可快速填入源目录',
    '拖动日志面板顶部的细条可调整日志高度',
    '筛选状态下全选 / 删除只作用于可见行，不会碰到被隐藏的文件',
  ];

  const $ = (id) => document.getElementById(id);
  const bridge = window.chrome && window.chrome.webview;

  // ------------------------------------------------------------------ 状态
  const state = {
    items: [],
    byId: new Map(),
    view: [],
    selected: new Set(),
    total: 0,
    done: 0,
    running: false,
    mode: 'copy',
    conflict: 'rename',
    filter: '',
    statusFilter: null,     // null | success | fail | skip | inplace | deleted | pending
    autoOpenDst: false,
    recentSrc: [],
    recentDst: [],
    sort: { key: 'id', dir: 1 },
    previewId: null,
    previewPath: '',
    previewMode: 'panel',   // panel | popup
    popupOpen: false,
    theme: 'dark',
    lastAnchor: -1,
    ctxId: null,
    logTotal: 0,
    logPending: [],
    logTimer: null,
    scanMode: false,          // 表格里是只读搜索结果，等待勾选后整理
    jobDone: false,           // 本轮任务已收到结束消息，防止迟到的 response 复活任务
    jobKind: 'normal',        // normal | scan | files
    footerPhase: null,        // null | search：搜索中的底部提示与"下一步"提示不同
    toolbarCompact: null,     // 顶栏是否已收纳「删除选中 / 打开目标」
    drives: [],
    scanRoots: [],
    scanExcludes: '',
    scanMax: 20000,
    undoAvailable: false,   // 最近一次整理任务是否产生了可撤销的副本
    port: 0,                // 本地 HTTP 服务端口（hello 消息下发，用于拼缩略图地址）
  };

  // 列表重算标记：状态更新不一定改变排序，避免每次都全量排序/统计
  let viewDirty = true;
  let statsDirty = true;

  // ------------------------------------------------------------------ 通讯
  function sendRaw(text) {
    if (!bridge) return;
    bridge.postMessage(text);
  }

  function api(cmd, params) {
    const parts = ['cmd=' + encodeURIComponent(cmd)];
    if (params) {
      for (const key of Object.keys(params)) {
        const value = params[key];
        if (value === undefined || value === null) continue;
        parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(String(value)));
      }
    }
    sendRaw(parts.join('&'));
  }

  // ------------------------------------------------------------------ 工具
  const esc = (value) =>
    String(value === undefined || value === null ? '' : value).replace(/[&<>"']/g, (ch) => {
      switch (ch) {
        case '&': return '&amp;';
        case '<': return '&lt;';
        case '>': return '&gt;';
        case '"': return '&quot;';
        default: return '&#39;';
      }
    });

  function fmtSize(bytes) {
    let value = Number(bytes) || 0;
    if (value < 1024) return value + ' B';
    const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
    let index = -1;
    do {
      value /= 1024;
      index++;
    } while (value >= 1024 && index < units.length - 1);
    return value.toFixed(1) + ' ' + units[index];
  }

  function fmtDuration(seconds) {
    if (!isFinite(seconds) || seconds <= 0) return '';
    const total = Math.round(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const pad = (n) => (n < 10 ? '0' + n : String(n));
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  }

  function basename(path) {
    const parts = String(path || '').split(/[\\/]/);
    return parts[parts.length - 1] || '';
  }

  function statusClass(status) {
    if (!status) return 'pending';
    if (status.indexOf('撤销失败') === 0) return 'fail';
    if (status.indexOf('失败') === 0) return 'fail';
    if (status.indexOf('已撤销') === 0) return 'deleted';
    if (status.indexOf('已删除') === 0) return 'deleted';
    if (status.indexOf('同名') === 0) return 'skip';
    if (status.indexOf('已在') === 0) return 'inplace';
    if (status.indexOf('已') === 0) return 'success';
    return 'pending';
  }

  // ------------------------------------------------------------------ 提示
  function toast(text, type, ms) {
    if (!text) return;
    const box = $('toasts');
    const el = document.createElement('div');
    el.className = 'toast ' + (type || '');
    el.textContent = text;
    box.appendChild(el);
    while (box.childElementCount > 5) box.removeChild(box.firstElementChild);
    const life = ms || 3600;
    setTimeout(() => {
      el.style.transition = 'opacity .25s ease, transform .25s ease';
      el.style.opacity = '0';
      el.style.transform = 'translateX(12px)';
      setTimeout(() => el.remove(), 280);
    }, life);
  }

  // ------------------------------------------------------------------ 模态框
  let modalResolve = null;

  function showModal(options) {
    // 单槽 modalResolve：已有模态框时拒绝新请求，避免前一个 await 永久挂起
    if (!$('modalMask').hidden) {
      return Promise.resolve({ ok: false, checked: false });
    }
    return new Promise((resolve) => {
      modalResolve = resolve;
      $('modalTitle').textContent = options.title || '';
      $('modalBody').textContent = options.body || '';
      const hasCheck = !!options.checkboxLabel;
      $('modalCheckRow').hidden = !hasCheck;
      if (hasCheck) {
        $('modalCheckText').textContent = options.checkboxLabel;
        $('modalCheck').checked = options.checked !== false;
      }
      const okBtn = $('modalOk');
      okBtn.textContent = options.okText || '确定';
      okBtn.classList.remove('primary', 'danger');
      okBtn.classList.add(options.danger ? 'danger' : 'primary');
      $('modalCancel').textContent = options.cancelText || '取消';
      $('modalCancel').hidden = !!options.hideCancel;
      $('modalMask').hidden = false;
      okBtn.focus();
    });
  }

  function closeModal(result) {
    $('modalMask').hidden = true;
    if (modalResolve) {
      const resolve = modalResolve;
      modalResolve = null;
      resolve(result);
    }
  }

  $('modalOk').addEventListener('click', () => {
    closeModal({ ok: true, checked: $('modalCheck').checked });
  });
  $('modalCancel').addEventListener('click', () => closeModal({ ok: false }));
  $('modalMask').addEventListener('mousedown', (event) => {
    if (event.target === $('modalMask')) closeModal({ ok: false });
  });

  // ------------------------------------------------------------------ 日志
  function pushLog(text, level) {
    state.logTotal++;
    state.logPending.push({ text: String(text), level: level || 'info' });
    if (!state.logTimer) {
      state.logTimer = setTimeout(flushLog, 140);
    }
  }

  function flushLog() {
    state.logTimer = null;
    const body = $('logBody');
    const pending = state.logPending;
    state.logPending = [];
    if (!pending.length) return;

    const stick = body.scrollHeight - body.scrollTop - body.clientHeight < 48;
    const frag = document.createDocumentFragment();
    for (const line of pending) {
      const el = document.createElement('div');
      el.className = 'log-line ' + line.level;
      el.textContent = line.text;
      frag.appendChild(el);
    }
    const placeholder = body.querySelector('.log-empty');
    if (placeholder) placeholder.remove();
    body.appendChild(frag);
    while (body.childElementCount > 1400) body.removeChild(body.firstElementChild);
    $('logCount').textContent = state.logTotal;
    if (stick) body.scrollTop = body.scrollHeight;
  }

  function clearLog() {
    state.logTotal = 0;
    state.logPending = [];
    $('logCount').textContent = '0';
    const body = $('logBody');
    body.innerHTML = '<div class="log-empty">暂无日志</div>';
  }

  function setLogCollapsed(collapsed) {
    const card = $('logCard');
    card.classList.toggle('collapsed', collapsed);
    localStorage.setItem('vc.logCollapsed', collapsed ? '1' : '0');
    if (collapsed) {
      // 折叠时清掉内联高度，让 .collapsed 的 42px 生效
      card.style.height = '';
    } else {
      // 展开时恢复上次拖拽高度（按当前窗口大小封顶）
      clampLogHeight();
      const body = $('logBody');
      body.scrollTop = body.scrollHeight;
    }
  }

  // 日志高度按窗口大小封顶：表格区至少保留约 240px，
  // 否则小窗口（或上次拖得太高）时打开界面表格只剩一条缝。
  function logMaxHeight() {
    return Math.max(
      110,
      Math.min(Math.round(window.innerHeight * 0.42), window.innerHeight - 360)
    );
  }
  function clampLogHeight() {
    const card = $('logCard');
    if (card.classList.contains('collapsed')) return;
    const saved = parseInt(localStorage.getItem('vc.logHeight'), 10);
    const base = Number.isFinite(saved) && saved >= 96 ? saved : 172;
    const h = Math.min(base, logMaxHeight());
    // 只临时压低显示，不写回 localStorage：小窗口下的钳制不应改掉
    // 用户在大窗口里拖出的高度偏好
    card.style.height = h + 'px';
  }

  // ------------------------------------------------------------------ 设置
  const cfg = {
    src: $('src'),
    dst: $('dst'),
    exts: $('exts'),
    recursive: $('optRecursive'),
    hidden: $('optHidden'),
    dedupe: $('optDedupe'),
    autoOpen: $('optAutoOpen'),
    minSize: $('minSize'),
    maxSize: $('maxSize'),
  };

  // 向导与侧栏共用同一批控件（控件已在向导里），cfg 直接指向真实元素
  cfg.autoOpen.addEventListener('change', () => {
    state.autoOpenDst = cfg.autoOpen.checked;
  });

  function readRecent(key) {
    try {
      const value = JSON.parse(localStorage.getItem(key) || '[]');
      return Array.isArray(value) ? value.filter((p) => typeof p === 'string' && p).slice(0, RECENT_MAX) : [];
    } catch (err) {
      return [];
    }
  }

  function writeRecent(key, list) {
    try {
      localStorage.setItem(key, JSON.stringify(list.slice(0, RECENT_MAX)));
    } catch (err) {}
  }

  function pushRecent(src, dst) {
    const pairs = [
      ['vc.recentSrc', 'recentSrc', src],
      ['vc.recentDst', 'recentDst', dst],
    ];
    for (const [storageKey, stateKey, path] of pairs) {
      const value = String(path || '').trim();
      if (!value) continue;
      const lower = value.toLowerCase().replace(/\//g, '\\');
      const list = state[stateKey].filter((p) => p.toLowerCase().replace(/\//g, '\\') !== lower);
      list.unshift(value);
      state[stateKey] = list.slice(0, RECENT_MAX);
      writeRecent(storageKey, state[stateKey]);
    }
  }

  function applySettings(raw) {
    let data = {};
    try {
      // 去掉可能的 UTF-8 BOM，避免 JSON.parse 抛错导致设置整体丢失
      const text = (typeof raw === 'string' ? raw : '').replace(/^\uFEFF/, '');
      data = typeof raw === 'string' ? JSON.parse(text || '{}') : raw || {};
    } catch (err) {
      data = {};
    }
    cfg.src.value = data.src || '';
    cfg.dst.value = data.dst || '';
    cfg.exts.value = data.exts || DEFAULT_EXTS;
    renderExtPicker();
    cfg.recursive.checked = data.recursive !== false;
    cfg.hidden.checked = data.hidden !== false;
    cfg.dedupe.checked = data.dedupe !== false;
    cfg.autoOpen.checked = data.autoOpenDst === true;
    cfg.minSize.value = data.minSize ? String(data.minSize) : '';
    cfg.maxSize.value = data.maxSize ? String(data.maxSize) : '';
    state.autoOpenDst = cfg.autoOpen.checked;
    state.recentSrc = readRecent('vc.recentSrc');
    state.recentDst = readRecent('vc.recentDst');
    state.scanRoots = Array.isArray(data.scanRoots) ? data.scanRoots.slice(0, 26) : [];
    state.scanExcludes = data.scanExcludes || '';
    state.scanMax = Math.max(100, Math.min(200000, Number(data.scanMax) || 20000));
    state.mode = data.mode === 'move' ? 'move' : 'copy';
    state.conflict = data.conflict === 'skip' ? 'skip' : 'rename';
    state.searchMode = data.searchMode === 'all' ? 'all' : 'dir';
    state.setupDone = data.setupDone === true;
    state.previewMode = data.previewMode === 'popup' ? 'popup' : 'panel';
    applyTheme(data.theme, false);
    syncSegUI();
    syncChoiceUI();
    // 「独立窗口」模式下右侧预览列没有意义（视频在独立窗口里播），启动时就收起，
    // 不能只依赖 localStorage 的折叠标记：配置是 popup、本地标记是展开时，
    // 会出现截图里"选了独立窗口、右边还空占 344px"的矛盾状态。
    if (state.previewMode === 'popup') setPreviewCollapsed(true);
  }

  // ------------------------------------------------------------------ 主题
  function applyTheme(theme, notifyNative) {
    state.theme = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', state.theme);
    try {
      localStorage.setItem('vc.theme', state.theme);
    } catch (err) {}
    syncSegUI();
    if (notifyNative) api('theme', { value: state.theme });
  }

  function setPreviewMode(mode) {
    state.previewMode = mode === 'popup' ? 'popup' : 'panel';
    // 预览模式切换与右侧面板折叠联动：popup 时自动收起右栏，panel 时恢复展开
    if (state.previewMode === 'popup') setPreviewCollapsed(true);
    else {
      setPreviewCollapsed(false);
      // 窄窗口（≤1320px）下 CSS 媒体查询会把预览列收起，只有加 force-preview
      // 才显示。切到「侧栏内」时若窗口偏窄，这里补上 force-preview，
      // 否则点了「侧栏内」面板仍不出现。
      if ($('app').classList.contains('narrow-preview')) {
        $('app').classList.add('force-preview');
      }
    }
    if (state.previewMode === 'panel' && state.popupOpen) api('preview_close');
    syncSegUI();
    saveSettingsSoon();
    if (state.previewMode === 'popup' && state.previewId !== null) openPopupPreview(state.previewId);
  }

  let saveTimer = null;
  function saveSettingsSoon() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      saveTimer = null;
      const data = {
        src: cfg.src.value.trim(),
        dst: cfg.dst.value.trim(),
        exts: cfg.exts.value.trim(),
        recursive: cfg.recursive.checked,
        hidden: cfg.hidden.checked,
        dedupe: cfg.dedupe.checked,
        autoOpenDst: cfg.autoOpen.checked,
        minSize: cfg.minSize.value.trim(),
        maxSize: cfg.maxSize.value.trim(),
        scanRoots: state.scanRoots,
        scanExcludes: state.scanExcludes,
        scanMax: state.scanMax,
        mode: state.mode,
        conflict: state.conflict,
        searchMode: state.searchMode,
        setupDone: state.setupDone === true,
        theme: state.theme,
        previewMode: state.previewMode,
      };
      api('save_settings', { data: JSON.stringify(data) });
    }, 400);
  }

  // ------------------------------------------------------------------ 分段控件
  function syncSegUI() {
    for (const btn of $('modeSeg').querySelectorAll('button')) {
      btn.classList.toggle('active', btn.dataset.value === state.mode);
    }
    for (const btn of $('conflictSeg').querySelectorAll('button')) {
      btn.classList.toggle('active', btn.dataset.value === state.conflict);
    }
    for (const btn of $('themeSeg').querySelectorAll('button')) {
      btn.classList.toggle('active', btn.dataset.value === state.theme);
    }
    for (const btn of $('previewSeg').querySelectorAll('button')) {
      btn.classList.toggle('active', btn.dataset.value === state.previewMode);
    }
    updatePlanSummary();
  }

  $('themeSeg').addEventListener('click', (event) => {
    const btn = event.target.closest('button');
    if (!btn) return;
    applyTheme(btn.dataset.value, true);
    saveSettingsSoon();
  });

  $('previewSeg').addEventListener('click', (event) => {
    const btn = event.target.closest('button');
    if (!btn) return;
    setPreviewMode(btn.dataset.value);
  });

  $('modeSeg').addEventListener('click', (event) => {
    const btn = event.target.closest('button');
    if (!btn || state.running) return;
    state.mode = btn.dataset.value;
    syncSegUI();
    saveSettingsSoon();
  });

  $('conflictSeg').addEventListener('click', (event) => {
    const btn = event.target.closest('button');
    if (!btn || state.running) return;
    state.conflict = btn.dataset.value;
    syncSegUI();
    saveSettingsSoon();
  });

  // ------------------------------------------------------------------ 整理设置向导（三步引导）
  // 先选搜索方式 → 再选操作方式 → 再勾开关。所有配置项只在向导里出现一次，
  // 侧栏只显示只读摘要（点「修改」回到这里）。向导里的控件就是 cfg/state 的真实来源，
  // 不存在"两处配置各存一份"的问题。
  let setupStep = 0;
  let setupShown = false;

  // 全盘搜索不需要源目录，其余（目标文件夹 / 扩展名 / 体积）在全盘搜索时同样生效
  function syncChoiceUI() {
    const isAll = state.searchMode === 'all';
    for (const card of document.querySelectorAll('.choice-card')) {
      card.setAttribute('aria-checked', card.dataset.mode === state.searchMode ? 'true' : 'false');
    }
    const fieldSrc = $('fieldSrc');
    if (fieldSrc) fieldSrc.hidden = isAll;
  }

  function showSetupStep(index) {
    setupStep = index;
    const panels = Array.from(document.querySelectorAll('.setup-step-body'));
    panels.forEach((p, i) => { p.hidden = i !== index; });
    document.querySelectorAll('#setupSteps .step').forEach((el, i) => {
      el.classList.toggle('active', i === index);
      el.classList.toggle('done', i < index);
    });
    $('setupPrev').hidden = index === 0;
    const last = index === panels.length - 1;
    $('setupNext').innerHTML = last ? '<span>完成</span>' : '<span>下一步</span>';
  }

  function openSetup() {
    if (state.running) {
      toast('任务进行中，请先取消或等待结束。', 'warn');
      return;
    }
    setupShown = true;
    syncChoiceUI();
    closeExtPop();
    renderExtPicker();
    updatePlanSummary();
    showSetupStep(0);
    $('setupMask').hidden = false;
    $('setupNext').focus();
  }

  function closeSetupModal() {
    $('setupMask').hidden = true;
  }

  // 关闭（×/遮罩/Esc）也记为"已看过"，避免每次启动都弹；侧栏「修改」可随时重开。
  // 注意：本地存储按 origin（含随机端口）隔离，跨启动会丢，必须存进 config.json。
  function dismissSetup() {
    state.setupDone = true;
    updatePlanSummary();
    saveSettingsSoon();
    closeSetupModal();
  }

  function maybeFirstSetup() {
    if (setupShown) return;
    if (state.setupDone) return;
    openSetup();
  }

  $('setupBtn').addEventListener('click', openSetup);
  $('setupClose').addEventListener('click', dismissSetup);
  $('setupMask').addEventListener('mousedown', (event) => {
    if (event.target === $('setupMask')) dismissSetup();
  });
  $('setupPrev').addEventListener('click', () => showSetupStep(Math.max(0, setupStep - 1)));
  $('setupNext').addEventListener('click', () => {
    const count = document.querySelectorAll('.setup-step-body').length;
    if (setupStep < count - 1) showSetupStep(setupStep + 1);
    else {
      dismissSetup();
      toast('设置已保存，点「开始搜索」列出视频。', 'success');
    }
  });

  // 第 1 步：搜索方式单选
  document.querySelectorAll('.choice-card').forEach((card) => {
    card.addEventListener('click', () => {
      if (state.running) return;
      state.searchMode = card.dataset.mode === 'all' ? 'all' : 'dir';
      syncChoiceUI();
      updatePlanSummary();
      saveSettingsSoon();
    });
  });

  // ------------------------------------------------------------------ 数据刷新
  let refreshTimer = null;

  function scheduleRefresh(delay) {
    if (refreshTimer) return;
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      refreshAll();
    }, typeof delay === 'number' ? delay : 60);
  }

  function recomputeView() {
    const keyword = state.filter.trim().toLowerCase();
    const wanted = state.statusFilter;
    let list = state.items;
    if (keyword) {
      list = list.filter((item) => {
        return (
          item.name.toLowerCase().indexOf(keyword) >= 0 ||
          item.path.toLowerCase().indexOf(keyword) >= 0 ||
          (item.status || '').toLowerCase().indexOf(keyword) >= 0
        );
      });
    }
    if (wanted) {
      list = list.filter((item) => statusClass(item.status) === wanted);
    }
    const key = state.sort.key;
    const dir = state.sort.dir;
    const compare = (a, b) => {
      let result = 0;
      if (key === 'size') result = a.size - b.size;
      else if (key === 'id') result = a.id - b.id;
      else if (key === 'name') result = a.name.toLowerCase() < b.name.toLowerCase() ? -1 : a.name.toLowerCase() > b.name.toLowerCase() ? 1 : 0;
      else if (key === 'status') result = (a.status || '') < (b.status || '') ? -1 : (a.status || '') > (b.status || '') ? 1 : 0;
      else if (key === 'dir') {
        const da = a.path.toLowerCase();
        const db = b.path.toLowerCase();
        result = da < db ? -1 : da > db ? 1 : 0;
      }
      return result === 0 ? (a.id - b.id) * dir : result * dir;
    };
    const sorted = list.slice().sort(compare);
    state.view = sorted.map((item) => item.id);
  }

  function pruneSelectionToView() {
    // 被筛选隐藏的行不能留在选中态，否则「删除选中」会误删看不见的文件
    const visible = new Set(state.view);
    for (const id of Array.from(state.selected)) {
      if (!visible.has(id)) state.selected.delete(id);
    }
  }

  function refreshAll() {
    if (viewDirty) {
      recomputeView();
      viewDirty = false;
      pruneSelectionToView();
    }
    renderRows();
    if (statsDirty) {
      updateStats();
      statsDirty = false;
    }
    updateSelectionUI();
    updateSortUI();
  }

  // ------------------------------------------------------------------ 列表渲染
  const rowsEl = $('rows');
  const rowsInner = $('rowsInner');
  let scrollRaf = 0;

  function renderRows() {
    const view = state.view;
    rowsInner.style.height = view.length * ROW_H + 'px';

    const scrollTop = rowsEl.scrollTop;
    const height = rowsEl.clientHeight || 420;
    const first = Math.max(0, Math.floor(scrollTop / ROW_H) - 5);
    const last = Math.min(view.length, Math.ceil((scrollTop + height) / ROW_H) + 5);

    if (view.length === 0) {
      rowsInner.innerHTML = '';
    } else {
      const glyph = '<svg class="thumb-glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="2.5" y="4.5" width="19" height="15" rx="3"></rect><path d="M10 9.5v5l4.2-2.5z" fill="currentColor" stroke="none"></path></svg>';
      const base = state.port ? 'http://127.0.0.1:' + state.port + '/thumb?path=' : '';
      let html = '';
      for (let index = first; index < last; index++) {
        const item = state.byId.get(view[index]);
        if (!item) continue;
        const top = index * ROW_H;
        const selected = state.selected.has(item.id);
        const cls = statusClass(item.status);
        // 缩略图懒加载：只渲染可视区附近的行，滚动时浏览器命中 max-age 缓存，代价很低。
        // 成功/失败分开记录：失败项不要当成"已加载"渲染，否则会显示系统裂图图标（H5）
        const thumbUrl = base && item.path
          ? base + encodeURIComponent(item.path) + '&v=' + (item.size || 0)
          : '';
        const thumbKey = item.id + '|' + (item.size || 0);
        let thumbCell;
        if (!thumbUrl) {
          thumbCell = '<div class="cell thumb">' + glyph + '</div>';
        } else if (thumbOk.has(thumbKey)) {
          thumbCell = '<div class="cell thumb has-img" data-tid="' + item.id + '">' +
            glyph + '<img src="' + esc(thumbUrl) + '" alt=""></div>';
        } else if (thumbFail.has(thumbKey)) {
          // 确认过没有缩略图：只留占位骨架，不再请求
          thumbCell = '<div class="cell thumb" data-tid="' + item.id + '">' + glyph + '</div>';
        } else {
          thumbCell = '<div class="cell thumb" data-tid="' + item.id + '">' + glyph +
            '<img data-src="' + esc(thumbUrl) + '" data-key="' + esc(thumbKey) +
            '" alt=""></div>';
        }
        html +=
          '<div class="row' + (selected ? ' selected' : '') + '" data-id="' + item.id +
          '" style="top:' + top + 'px">' +
          '<label class="check"><input type="checkbox" class="check-box"' +
          (selected ? ' checked' : '') + ' data-role="check"></label>' +
          '<div class="cell idx">' + (item.id) + '</div>' +
          thumbCell +
          '<div class="cell name" title="' + esc(item.name) + '">' + esc(item.name) + '</div>' +
          '<div class="cell size">' + esc(fmtSize(item.size)) + '</div>' +
          '<div class="cell status"><span class="pill ' + cls + '" title="' +
          esc(item.status) + '">' + esc(item.status) + '</span></div>' +
          '<div class="cell dir" title="' + esc(item.path) + '">' + esc(dirOf(item.path)) + '</div>' +
          '</div>';
      }
      rowsInner.innerHTML = html;
      attachThumbs(rowsInner);
    }
    updateEmptyState();
  }

  // H5：缩略图先显示占位底；加载成功换成图片，失败保持占位、永不显示裂图
  function attachThumbs(root) {
    const imgs = root.querySelectorAll('img[data-src]');
    for (const img of imgs) {
      const cell = img.parentElement;
      const key = img.dataset.key || '';
      const src = img.dataset.src;
      img.removeAttribute('data-src');
      img.removeAttribute('data-key');
      img.onload = () => {
        if (cell) cell.classList.add('has-img');
        if (key) thumbOk.add(key);
      };
      img.onerror = () => {
        // 后端没有这张缩略图（格式不支持/生成失败）：撤掉 img，保持占位骨架
        if (key) thumbFail.add(key);
        if (cell) cell.classList.remove('has-img');
        img.removeAttribute('src');
        img.style.display = 'none';
      };
      img.src = src;
    }
  }

  function dirOf(path) {
    const value = String(path || '');
    const index = Math.max(value.lastIndexOf('\\'), value.lastIndexOf('/'));
    return index > 0 ? value.slice(0, index) : value;
  }

  function updateEmptyState() {
    const empty = $('empty');
    const hasItems = state.items.length > 0;
    const noMatch = hasItems && state.view.length === 0;
    empty.style.display = hasItems && !noMatch ? 'none' : 'flex';
    empty.querySelector('.empty-title').textContent = noMatch ? '没有匹配的记录' : '还没有视频记录';
    empty.querySelector('.empty-sub').textContent = noMatch
      ? '试试修改筛选条件'
      : '点「开始搜索」列出视频，勾选后点「整理选中」';
  }

  function scheduleRender() {
    if (scrollRaf) return;
    scrollRaf = requestAnimationFrame(() => {
      scrollRaf = 0;
      renderRows();
    });
  }

  rowsEl.addEventListener('scroll', scheduleRender, { passive: true });

  // ------------------------------------------------------------------ 选择
  function updateSelectionUI() {
    const count = state.selected.size;
    const deleteBtn = $('deleteBtn');
    deleteBtn.disabled = state.running || count === 0;
    $('deleteLabel').textContent = count > 0 ? `删除选中 (${count})` : '删除选中';
    $('pvDelete').disabled = state.previewId === null || state.running;
    $('reportBtn').disabled = state.items.length === 0;
    $('reportHtmlBtn').disabled = state.items.length === 0;
    $('undoBtn').disabled = state.running || !state.undoAvailable;
    $('collectSelectedBtn').disabled =
      state.running || !state.scanMode || count === 0;

    const selectAll = $('selectAll');
    const visible = state.view;
    const selectedVisible = visible.filter((id) => state.selected.has(id)).length;
    selectAll.checked = visible.length > 0 && selectedVisible === visible.length;
    selectAll.indeterminate = selectedVisible > 0 && selectedVisible < visible.length;

    // 「更多」菜单里的同款动作在收纳状态下也要跟着禁用
    $('menuDeleteBtn').disabled = deleteBtn.disabled;
    $('menuDeleteLabel').textContent =
      count > 0 ? `删除选中 (${count})` : '删除选中';

    updateFooter();
    updateBadge(count);
  }

  function updateBadge(count) {
    const badge = $('countBadge');
    if (state.running) {
      badge.textContent = String(state.items.length);
    } else if (count > 0) {
      badge.textContent = `${count} / ${state.items.length}`;
    } else {
      badge.textContent = String(state.items.length);
    }
  }

  function selectOnly(id, index) {
    state.selected.clear();
    state.selected.add(id);
    state.lastAnchor = index;
    renderRows();
    updateSelectionUI();
    syncPreviewInfo(id);
  }

  function toggleSelect(id, index) {
    if (state.selected.has(id)) state.selected.delete(id);
    else state.selected.add(id);
    state.lastAnchor = index;
    renderRows();
    updateSelectionUI();
  }

  function selectRange(index) {
    const view = state.view;
    if (state.lastAnchor < 0 || state.lastAnchor >= view.length) state.lastAnchor = index;
    const from = Math.min(state.lastAnchor, index);
    const to = Math.max(state.lastAnchor, index);
    for (let i = from; i <= to; i++) state.selected.add(view[i]);
    renderRows();
    updateSelectionUI();
  }

  function selectAllVisible() {
    const view = state.view;
    if (!view.length) return;
    view.forEach((id) => state.selected.add(id));
    renderRows();
    updateSelectionUI();
  }

  // 表头复选框：勾上=全选可见行，取消=只取消可见行（与 Python 版 Ctrl+A 语义一致）
  $('selectAll').addEventListener('change', (event) => {
    if (event.target.checked) {
      selectAllVisible();
    } else {
      state.view.forEach((id) => state.selected.delete(id));
      renderRows();
      updateSelectionUI();
    }
  });

  rowsEl.addEventListener('click', (event) => {
    const row = event.target.closest('.row');
    if (!row) return;
    const id = Number(row.dataset.id);
    const index = state.view.indexOf(id);

    const actionBtn = event.target.closest('[data-act]');
    if (actionBtn) {
      event.stopPropagation();
      handleRowAction(actionBtn.dataset.act, id);
      return;
    }

    if (event.target.closest('input[data-role="check"]')) {
      toggleSelect(id, index);
      return;
    }

    if (event.shiftKey) selectRange(index);
    else if (event.ctrlKey || event.metaKey) toggleSelect(id, index);
    else selectOnly(id, index);
  });

  rowsEl.addEventListener('dblclick', (event) => {
    const row = event.target.closest('.row');
    if (!row) return;
    if (event.target.closest('button, input')) return;
    previewItem(Number(row.dataset.id));
  });

  function handleRowAction(action, id) {
    if (action === 'preview') {
      previewItem(id);
      return;
    }
    const item = state.byId.get(id);
    if (!item) return;
    if (action === 'open') {
      api('open', { id, path: item.path });
    } else if (action === 'reveal') {
      api('reveal', { id, path: currentPathOf(item) });
    } else if (action === 'delete') {
      if (!state.selected.has(id)) {
        state.selected.clear();
        state.selected.add(id);
        updateSelectionUI();
        renderRows();
      }
      deleteSelected();
    }
  }

  // ------------------------------------------------------------------ 统计
  function updateStats() {
    const counters = { success: 0, fail: 0, skip: 0, inplace: 0, deleted: 0, pending: 0 };
    let bytes = 0;
    for (const item of state.items) {
      const cls = statusClass(item.status);
      if (counters[cls] !== undefined) counters[cls]++;
      else counters.pending++;
      bytes += item.size || 0;
    }

    const chip = (key, cls, label, count) => {
      const active = state.statusFilter && state.statusFilter === key;
      return `<span class="chip clickable ${cls}${active ? ' active' : ''}" ` +
        `data-filter="${key}" title="点击只看「${label}」；再点一次取消">` +
        `<i class="dot"></i>${label} <b>${count}</b></span>`;
    };
    const chips = [];
    chips.push('<span class="chip clickable" data-filter="" ' +
      'title="显示全部记录">全部 <b>' + state.items.length + '</b></span>');
    chips.push(chip('success', 'ok', '成功', counters.success));
    chips.push(chip('fail', 'fail', '失败', counters.fail));
    chips.push(chip('skip', 'warn', '跳过', counters.skip));
    chips.push(chip('inplace', 'gray', '已在目标', counters.inplace));
    if (counters.deleted > 0) {
      chips.push(chip('deleted', 'gray', '已删除', counters.deleted));
    }
    if (counters.pending > 0) {
      chips.push(chip('pending', '', '待处理', counters.pending));
    }
    if (state.statusFilter || state.filter.trim()) {
      chips.push(`<span class="chip filter-info">显示 <b>${state.view.length}</b> / ${state.items.length}</span>`);
    }
    chips.push(`<span class="chip size">总体积 <b>${fmtSize(bytes)}</b></span>`);
    $('stats').innerHTML = chips.join('');
  }

  function setStatusFilter(key) {
    const wanted = !key || key === state.statusFilter ? null : key;
    const previous = state.statusFilter;
    state.statusFilter = wanted;
    viewDirty = true;
    statsDirty = true;
    scheduleRefresh(0);
    if (previous && !wanted) toast('已显示全部记录', 'info', 1400);
  }

  $('stats').addEventListener('click', (event) => {
    const chip = event.target.closest('[data-filter]');
    if (chip) setStatusFilter(chip.dataset.filter || null);
  });

  function updateSortUI() {
    const headers = document.querySelectorAll('.th.sortable');
    for (const th of headers) {
      const active = th.dataset.sort === state.sort.key;
      th.classList.toggle('active', active);
      th.classList.toggle('desc', active && state.sort.dir < 0);
      let arrow = th.querySelector('.arrow');
      if (!arrow) {
        arrow = document.createElement('span');
        arrow.className = 'arrow';
        arrow.textContent = '▲';
        th.appendChild(arrow);
      }
    }
  }

  document.querySelectorAll('.th.sortable').forEach((th) => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (state.sort.key === key) state.sort.dir *= -1;
      else {
        state.sort.key = key;
        state.sort.dir = 1;
      }
      viewDirty = true;
      refreshAll();
    });
  });

  // ------------------------------------------------------------------ 筛选
  const searchBox = $('searchBox');
  $('filter').addEventListener('input', (event) => {
    state.filter = event.target.value;
    // 有内容时收起 Ctrl+F 提示，把宽度让给文字
    searchBox.classList.toggle('has-text', event.target.value.length > 0);
    viewDirty = true;
    statsDirty = true;
    scheduleRefresh(70);
  });
  // 点放大镜/提示/留白也能聚焦输入框（以前只有点输入框本身才生效）
  searchBox.addEventListener('mousedown', (event) => {
    if (event.target === $('filter')) return;
    event.preventDefault();
    $('filter').focus();
  });

  // ------------------------------------------------------- 目录快捷操作 / 帮助
  function swapDirs() {
    const src = cfg.src.value.trim();
    const dst = cfg.dst.value.trim();
    if (!src && !dst) {
      toast('两个目录都还是空的', 'warn', 1800);
      return;
    }
    cfg.src.value = dst;
    cfg.dst.value = src;
    cfg.src.dispatchEvent(new Event('input', { bubbles: true }));
    cfg.dst.dispatchEvent(new Event('input', { bubbles: true }));
    saveSettingsSoon();
    toast('已交换源目录与目标文件夹', 'success', 1800);
  }

  const histMenu = $('histMenu');

  function hideHistMenu() {
    histMenu.hidden = true;
  }

  function showHistMenu(which, anchor) {
    hideCtxMenu();
    const list = which === 'src' ? state.recentSrc : state.recentDst;
    histMenu.dataset.which = which;
    histMenu.innerHTML = '';
    if (!list.length) {
      const empty = document.createElement('button');
      empty.className = 'muted';
      empty.textContent = '还没有历史记录（开始一次整理后会记住）';
      empty.disabled = true;
      histMenu.appendChild(empty);
    } else {
      for (const path of list) {
        const btn = document.createElement('button');
        btn.className = 'path';
        btn.textContent = path;
        btn.title = path;
        btn.dataset.path = path;
        histMenu.appendChild(btn);
      }
      const sep = document.createElement('div');
      sep.className = 'ctx-sep';
      histMenu.appendChild(sep);
      const clear = document.createElement('button');
      clear.textContent = '清空历史记录';
      clear.dataset.clear = '1';
      histMenu.appendChild(clear);
    }
    histMenu.hidden = false;
    const rect = anchor.getBoundingClientRect();
    const width = histMenu.offsetWidth;
    const height = histMenu.offsetHeight;
    histMenu.style.left = Math.max(6, Math.min(rect.left, window.innerWidth - width - 8)) + 'px';
    histMenu.style.top = Math.min(rect.bottom + 4, Math.max(6, window.innerHeight - height - 8)) + 'px';
  }

  histMenu.addEventListener('click', (event) => {
    const btn = event.target.closest('button');
    if (!btn) return;
    const which = histMenu.dataset.which;
    if (btn.dataset.clear) {
      const storageKey = which === 'src' ? 'vc.recentSrc' : 'vc.recentDst';
      const stateKey = which === 'src' ? 'recentSrc' : 'recentDst';
      state[stateKey] = [];
      writeRecent(storageKey, []);
      toast('已清空历史记录', 'info', 1400);
    } else if (btn.dataset.path) {
      const input = which === 'src' ? cfg.src : cfg.dst;
      input.value = btn.dataset.path;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      saveSettingsSoon();
      toast('已填入目录：' + btn.dataset.path, 'info', 1800);
    }
    hideHistMenu();
  });

  function showShortcuts() {
    const keys = SHORTCUTS.map((pair) => pair[0].padEnd(14, ' ') + pair[1]).join('\n');
    const tips = SHORTCUT_TIPS.map((tip, i) => (i + 1) + '. ' + tip).join('\n');
    showModal({
      title: '快捷键与操作说明',
      body: '键盘快捷键\n' + keys + '\n\n操作提示\n' + tips,
      okText: '知道了',
      hideCancel: true,
    });
  }

  $('swapDirs').addEventListener('click', swapDirs);
  $('histSrc').addEventListener('click', (event) => {
    event.stopPropagation();
    showHistMenu('src', event.currentTarget);
  });
  $('histDst').addEventListener('click', (event) => {
    event.stopPropagation();
    showHistMenu('dst', event.currentTarget);
  });
  $('helpBtn').addEventListener('click', showShortcuts);
  window.addEventListener('mousedown', (event) => {
    if (!event.target.closest('#histMenu') &&
        !event.target.closest('#histSrc') && !event.target.closest('#histDst')) {
      hideHistMenu();
    }
  });

  // ------------------------------------------------------------------ 运行控制
  // 底部提示：数量（Python 版原有）+ 下一步提示（C++ 版原有）合成一条，
  // 两版文案因此完全一致
  function footerText() {
    const total = state.items.length;
    const selected = state.selected.size;
    if (state.footerPhase === 'search') {
      return (state.searchMode === 'all' ? '全盘搜索中' : '搜索中') +
        '（只读）· 已找到 ' + total + ' 个';
    }
    if (state.running) return '正在处理，请稍候…';
    let text = '共 ' + total + ' 个视频';
    if (state.view.length && state.view.length < total) {
      text += ' · 显示 ' + state.view.length + ' / ' + total;
    }
    if (selected) text += ' · 已选 ' + selected;
    const deleted = state.items.filter((i) => i.deleted).length;
    if (deleted) text += ' · 已删除 ' + deleted;
    if (total === 0) text += ' · 点「修改」配置后开始搜索';
    else if (!selected) text += ' · 勾选后点「整理选中」';
    return text;
  }

  function updateFooter() {
    $('footerHint').textContent = footerText();
  }

  function setRunning(running) {
    state.running = running;
    $('app').classList.toggle('running', running);
    $('startBtn').disabled = running;
    $('cancelBtn').disabled = !running;
    $('runIndicator').hidden = !running;
    if (running) state.footerPhase = state.footerPhase === 'search' ? 'search' : 'run';
    else state.footerPhase = null;
    updateFooter();
    updateSelectionUI();
    updateBadge(state.selected.size);
  }

  function resetItems() {
    state.items = [];
    state.byId = new Map();
    state.view = [];
    state.selected.clear();
    // 新一轮任务可能换了文件，缩略图结果缓存一并清掉重新拉取
    thumbOk.clear();
    thumbFail.clear();
    state.total = 0;
    state.done = 0;
    state.lastAnchor = -1;
    clearPreview();
    viewDirty = true;
    statsDirty = true;
    $('progressFill').style.width = '0%';
    $('progressPct').textContent = '—';
    $('progressText').textContent = '准备开始…';
    document.querySelector('.progress-card').classList.remove('indeterminate');
    refreshAll();
  }

  function readSizeLimit(input) {
    // 空 / 非数字 / 0 都视为不限制；负数按 0 处理
    const n = Math.floor(Number(input.value));
    return Number.isFinite(n) && n > 0 ? n : 0;
  }

  function buildStartParams(create) {
    return {
      src: cfg.src.value.trim(),
      dst: cfg.dst.value.trim(),
      exts: cfg.exts.value.trim(),
      mode: state.mode,
      conflict: state.conflict,
      recursive: cfg.recursive.checked ? 1 : 0,
      hidden: cfg.hidden.checked ? 1 : 0,
      dedupe: cfg.dedupe.checked ? 1 : 0,
      minsize: readSizeLimit(cfg.minSize),
      maxsize: readSizeLimit(cfg.maxSize),
      create: create ? 1 : 0,
    };
  }

  // 「开始搜索」只做只读扫描：指定目录扫源目录，全盘搜索先弹磁盘勾选再扫。
  // 真正的复制/移动只由「整理选中」触发（那里还有一次确认框）。
  function startSearch() {
    if (state.running) return;
    if (state.searchMode === 'all') {
      showScanAllDialog();
      return;
    }
    const src = cfg.src.value.trim();
    if (!src) {
      openSetup();
      toast('请先选择源目录。', 'warn');
      return;
    }
    const exts = cfg.exts.value.trim();
    if (!exts) {
      openSetup();
      toast('请先勾选至少一种视频格式。', 'warn');
      return;
    }
    const min = readSizeLimit(cfg.minSize);
    const max = readSizeLimit(cfg.maxSize);
    if (min && max && min > max) {
      toast('体积范围不合法：最小值不能大于最大值。', 'warn');
      return;
    }

    saveSettingsSoon();
    pushRecent(src, cfg.dst.value.trim());
    state.jobDone = false;
    state.jobKind = 'scan';
    api('scan_all', {
      roots: src,
      recursive: cfg.recursive.checked ? 1 : 0,
      exts,
      hidden: cfg.hidden.checked ? 1 : 0,
      exclude: state.scanExcludes || '',
      max: state.scanMax,
      dst: cfg.dst.value.trim(),
      minsize: min,
      maxsize: max,
    });
  }

  // ------------------------------------------------- 全盘搜索（只读）与整理选中项
  const KIND_LABEL = { fixed: '本地磁盘', removable: '可移动磁盘', remote: '网络盘',
                       cdrom: '光驱', other: '其他' };

  function hideScanAllDialog() {
    $('scanAllMask').hidden = true;
  }

  function showScanAllDialog() {
    if (state.running) {
      toast('任务进行中，请先取消或等待结束。', 'warn');
      return;
    }
    const list = $('driveList');
    list.innerHTML = '';
    if (!state.drives.length) {
      const empty = document.createElement('div');
      empty.className = 'drive-empty';
      empty.textContent = '没有检测到可用磁盘';
      list.appendChild(empty);
    }
    for (const drive of state.drives) {
      const row = document.createElement('label');
      row.className = 'drive-row' + (drive.selectable ? '' : ' disabled');
      const check = document.createElement('input');
      check.type = 'checkbox';
      check.dataset.path = drive.path;
      check.disabled = !drive.selectable;
      // 每次打开都全不选：盘符由用户手动勾选，避免误扫整台机器
      check.checked = false;
      const name = document.createElement('span');
      name.textContent = drive.label;
      const kind = document.createElement('span');
      kind.className = 'kind';
      kind.textContent = KIND_LABEL[drive.kind] || drive.kind;
      row.appendChild(check);
      row.appendChild(name);
      row.appendChild(kind);
      list.appendChild(row);
    }
    $('scanExclude').value = state.scanExcludes;
    $('scanMax').value = state.scanMax;
    $('scanAllMask').hidden = false;
  }

  function startScanAll() {
    const roots = Array.from($('driveList').querySelectorAll('input[type="checkbox"]'))
      .filter((check) => check.checked && !check.disabled)
      .map((check) => check.dataset.path);
    if (!roots.length) {
      toast('请至少勾选一个磁盘。', 'warn');
      return;
    }
    const exts = cfg.exts.value.trim();
    if (!exts) {
      toast('视频扩展名不能为空。', 'warn');
      return;
    }
    const min = readSizeLimit(cfg.minSize);
    const maxSize = readSizeLimit(cfg.maxSize);
    if (min && maxSize && min > maxSize) {
      toast('体积范围不合法：最小值不能大于最大值。', 'warn');
      return;
    }
    state.scanRoots = roots;
    state.scanExcludes = $('scanExclude').value.trim();
    const max = Number($('scanMax').value) || 20000;
    state.scanMax = Math.max(100, Math.min(200000, max));
    saveSettingsSoon();
    hideScanAllDialog();
    state.jobDone = false;
    state.jobKind = 'scan';
    api('scan_all', {
      roots: roots.join(','),
      exts,
      hidden: cfg.hidden.checked ? 1 : 0,
      exclude: state.scanExcludes,
      max: state.scanMax,
      dst: cfg.dst.value.trim(),
      minsize: min,
      maxsize: maxSize,
    });
  }

  // 正式整理前的确认：把去向、方式、重名、去重、数量与总大小摆出来
  async function confirmOrganize(ids) {
    const items = ids.map((id) => state.byId.get(id)).filter(Boolean);
    const total = items.reduce((n, it) => n + (Number(it.size) || 0), 0);
    const fmt = (b) => (b >= 1073741824
      ? (b / 1073741824).toFixed(2) + ' GB'
      : b >= 1048576
        ? (b / 1048576).toFixed(1) + ' MB'
        : Math.max(1, Math.round(b / 1024)) + ' KB');
    const lines = [];
    lines.push('将要处理 ' + ids.length + ' 个视频' + (total ? '（约 ' + fmt(total) + '）' : '') + '：');
    lines.push('');
    lines.push('去向：' + (cfg.dst.value.trim() || '（未设置）'));
    lines.push('方式：' + (state.mode === 'move' ? '移动（源目录中的原文件会被移除）' : '复制（保留原文件）'));
    lines.push('重名：' + (state.conflict === 'skip' ? '同名跳过（不覆盖）' : '自动重命名（xxx (1).mp4）'));
    lines.push('去重：' + (cfg.dedupe.checked ? '跳过相同视频' : '不比对重复'));
    const result = await showModal({
      title: '确认开始整理？',
      body: lines.join('\n'),
      okText: state.mode === 'move' ? '开始移动' : '开始复制',
      cancelText: '再看看',
      danger: state.mode === 'move',
    });
    return result.ok;
  }

  async function startFiles(create) {
    // 只整理当前筛选结果里看得见、且被勾选的行
    const visible = new Set(state.view);
    const ids = Array.from(state.selected).filter((id) => visible.has(id));
    if (!ids.length) {
      toast('请先勾选要整理的视频。', 'warn');
      return;
    }
    if (!cfg.dst.value.trim()) {
      openSetup();
      toast('请先选择目标文件夹。', 'warn');
      return;
    }
    // create=true 是原生返回"目标文件夹不存在"后的二次确认，已确认过就不再重复弹
    if (!create && !(await confirmOrganize(ids))) return;
    const params = buildStartParams(create);
    delete params.src;
    params.ids = ids.join(',');
    saveSettingsSoon();
    state.jobDone = false;
    state.jobKind = 'files';
    api('start_files', params);
  }

  $('scanAllCancel').addEventListener('click', hideScanAllDialog);
  $('scanAllOk').addEventListener('click', startScanAll);
  $('collectSelectedBtn').addEventListener('click', () => startFiles(false));
  $('scanAllMask').addEventListener('mousedown', (event) => {
    if (event.target === $('scanAllMask')) hideScanAllDialog();
  });

  // ------------------------------------------------------------------ 删除
  async function deleteSelected() {
    if (state.running) {
      toast('任务进行中不能删除文件，请先取消任务。', 'warn');
      return;
    }
    // 只处理当前筛选结果里看得见的行，避免误删被隐藏的文件
    const visible = new Set(state.view);
    const ids = Array.from(state.selected).filter((id) => visible.has(id));
    if (!ids.length) {
      toast('请先在列表中选择要删除的视频。', 'warn');
      return;
    }

    const hasTarget = ids.some((id) => {
      const item = state.byId.get(id);
      return item && item.targetCreated && item.target && item.target !== item.path;
    });

    const result = await showModal({
      title: `删除 ${ids.length} 个视频？`,
      body: '文件会被移入 Windows 回收站，之后仍可从回收站还原。',
      checkboxLabel: hasTarget ? '同时删除目标文件夹中的副本' : '',
      checked: true,
      okText: '移入回收站',
      danger: true,
    });
    if (!result.ok) return;

    api('delete', { ids: ids.join(','), also_target: (hasTarget && result.checked) ? 1 : 0 });
  }

  $('deleteBtn').addEventListener('click', deleteSelected);

  // ------------------------------------------------------------------ 预览
  const video = $('video');
  const videoStage = $('videoStage');

  // 侧栏内置播放器的自定义控制条（与独立预览窗口同源，见 preview.html/js）
  const pvControls = $('pvControls');
  const pvSeek = $('pvSeek');
  const pvSeekBuffer = $('pvSeekBuffer');
  const pvSeekPlayed = $('pvSeekPlayed');
  const pvTime = $('pvTime');
  const pvIconPlay = $('pvIconPlay');
  const pvIconPause = $('pvIconPause');
  const pvIconVolume = $('pvIconVolume');
  const pvIconMuted = $('pvIconMuted');
  const pvVolume = $('pvVolume');
  const pvRate = $('pvRate');
  const pvPlayLabel = $('pvPlayLabel');

  function fmtTime(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const mm = h ? String(m).padStart(2, '0') : String(m);
    return (h ? h + ':' : '') + mm + ':' + String(sec).padStart(2, '0');
  }

  function setSidebarPlayIcon(playing) {
    pvIconPlay.style.display = playing ? 'none' : '';
    pvIconPause.style.display = playing ? '' : 'none';
    if (pvPlayLabel) pvPlayLabel.textContent = playing ? '暂停' : '播放';
    videoStage.classList.toggle('playing', playing);
  }

  function syncSidebarMuteIcon() {
    const muted = video.muted || video.volume === 0;
    pvIconVolume.style.display = muted ? 'none' : '';
    pvIconMuted.style.display = muted ? '' : 'none';
  }

  function toggleSidebarPlay() {
    if (state.previewId === null) return;
    if (video.paused || video.ended) {
      video.play().catch(() => {});
    } else {
      video.pause();
    }
  }

  // Chromium 自动播放策略：带声音的媒体若不在“用户手势”里会被拦截，而
  // “双击→postMessage→native→回包”这条链路已脱离点击手势，导致 video.play()
  // 被拒（NotAllowedError：视频显示了首帧却停在暂停、无任何报错——用户看到的
  // 就是“双击没反应/播不了”）。
  // 对策：先静音播放（静音自动播放始终被允许），成功后恢复用户设置的音量；
  //       万一有声播放仍被拒，就退一步强制静音播放并给出可见提示，绝不停在首帧。
  function startInlinePlayback() {
    const wantSound = !video.muted && video.volume > 0;
    // 没有用户手势时（例如外部的程序化双击），取消静音会让 Chromium 把播放
    // 直接暂停，于是又回到“首帧停住”。这里提前记下来，播放后再校正。
    const noGesture = !!(navigator.userActivation && !navigator.userActivation.hasBeenActive);
    const keepMuted = () => {
      if (video.error) return;   // 解码失败交给 error 回调，别用“静音播放”盖掉原因
      video.muted = true;
      syncSidebarMuteIcon();
      video.play().catch(() => {});
      $('pvState').textContent = '已静音播放（浏览器限制了有声自动播放），点音量区的「静音」按钮可恢复声音。';
    };
    if (!wantSound) {
      video.play().catch(() => {
        $('pvState').textContent = '无法开始播放，请点下方「播放」或改用系统播放器。';
      });
      return;
    }
    video.muted = true;
    video.play()
      .then(() => {
        if (video.volume > 0) {
          video.muted = false;
          syncSidebarMuteIcon();
          // 有些 Chromium 构建在“没有用户手势”时会因为取消静音而立刻暂停，
          // 这里就地检查一次，避免又回到首帧停住。
          if (video.paused && !video.ended) {
            keepMuted();
            return;
          }
        }
        if (noGesture) {
          setTimeout(() => {
            if (video.paused && !video.ended && !video.error) keepMuted();
          }, 320);
        }
      })
      .catch(() => keepMuted());
  }

  $('pvCenterPlay').addEventListener('click', toggleSidebarPlay);
  $('pvBtnPlay').addEventListener('click', toggleSidebarPlay);
  $('pvBtnMute').addEventListener('click', () => {
    video.muted = !video.muted;
    syncSidebarMuteIcon();
  });

  let pvDragging = false;
  function pvSeekRatio(event) {
    const rect = pvSeek.getBoundingClientRect();
    return rect.width > 0 ? Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)) : 0;
  }
  function pvApplySeek(event) {
    const duration = video.duration;
    if (!duration || !isFinite(duration)) return;
    video.currentTime = duration * pvSeekRatio(event);
    pvSeekPlayed.style.width = (pvSeekRatio(event) * 100).toFixed(3) + '%';
  }
  pvSeek.addEventListener('pointerdown', (event) => {
    pvDragging = true;
    pvSeek.setPointerCapture(event.pointerId);
    pvApplySeek(event);
  });
  pvSeek.addEventListener('pointermove', (event) => {
    if (pvDragging) pvApplySeek(event);
  });
  pvSeek.addEventListener('pointerup', (event) => {
    pvDragging = false;
    pvApplySeek(event);
    try { pvSeek.releasePointerCapture(event.pointerId); } catch (err) {}
  });

  function pvUpdateTime() {
    pvTime.textContent = fmtTime(video.currentTime || 0) + ' / ' + fmtTime(video.duration || 0);
  }
  function pvUpdateSeek() {
    const duration = video.duration;
    const ratio = duration && isFinite(duration) ? Math.min(1, (video.currentTime || 0) / duration) : 0;
    pvSeekPlayed.style.width = (ratio * 100).toFixed(3) + '%';
  }
  function pvUpdateBuffer() {
    const duration = video.duration;
    if (!duration || !isFinite(duration) || !video.buffered || !video.buffered.length) {
      pvSeekBuffer.style.width = '0%';
      return;
    }
    const end = video.buffered.end(video.buffered.length - 1);
    pvSeekBuffer.style.width = Math.min(100, (end / duration) * 100).toFixed(3) + '%';
  }

  video.addEventListener('play', () => setSidebarPlayIcon(true));
  video.addEventListener('pause', () => setSidebarPlayIcon(false));
  video.addEventListener('ended', () => setSidebarPlayIcon(false));
  video.addEventListener('progress', pvUpdateBuffer);
  video.addEventListener('durationchange', pvUpdateBuffer);
  video.addEventListener('timeupdate', () => { pvUpdateTime(); pvUpdateSeek(); });

  // 音量 / 速度（与独立窗口共用同一份本地存储键，保持体验一致）
  const savedVolume = localStorage.getItem('vc.volume');
  if (savedVolume !== null && Number(savedVolume) >= 0 && Number(savedVolume) <= 1) {
    pvVolume.value = savedVolume;
  }
  video.volume = Number(pvVolume.value);
  pvVolume.addEventListener('input', () => {
    video.volume = Number(pvVolume.value);
    video.muted = video.volume === 0;
    localStorage.setItem('vc.volume', pvVolume.value);
    syncSidebarMuteIcon();
  });
  const savedRate = localStorage.getItem('vc.rate');
  if (savedRate) pvRate.value = savedRate;
  video.playbackRate = Number(pvRate.value) || 1;
  pvRate.addEventListener('change', () => {
    video.playbackRate = Number(pvRate.value) || 1;
    localStorage.setItem('vc.rate', pvRate.value);
  });
  syncSidebarMuteIcon();

  // ---------------------------------------------------------- 解码失败提示层
  // 内置播放器是 WebView2（Chromium），只认 H.264/H.265、VP8/VP9/AV1 + AAC/MP3/Opus。
  // 老片常见的 XVID/DIVX、RMVB、WMV、MPEG-1/2，以及 MKV 里挂 AC3/DTS 音轨的，
  // 双击后只会黑屏 + 底部一行小字，极容易被当成"双击没反应"。这里改成视频框内
  // 直接给原因 + 一个「用系统播放器打开」按钮。
  const RISKY_EXTS = ['avi', 'rmvb', 'rm', 'wmv', 'flv', 'mpg', 'mpeg', 'vob', 'divx',
                      'ts', 'm2ts', 'mts', 'f4v', 'ogv', '3gp'];

  function extOf(name) {
    const n = String(name || '');
    const dot = n.lastIndexOf('.');
    return dot < 0 ? '' : n.slice(dot + 1).toLowerCase();
  }

  function showVideoFail(reason, sub) {
    const box = $('videoFail');
    if (!box) return;
    $('vfTitle').textContent = reason || '内置播放器打不开这个视频';
    if (sub) $('vfSub').textContent = sub;
    box.hidden = false;
    videoStage.classList.add('failed');
  }

  function hideVideoFail() {
    const box = $('videoFail');
    if (box) box.hidden = true;
    videoStage.classList.remove('failed');
  }

  $('vfSystem').addEventListener('click', () => {
    if (state.previewId === null) return;
    const item = state.byId.get(state.previewId);
    if (item) api('open', { id: item.id, path: currentPathOf(item) });
  });

  function clearPreview() {
    state.previewId = null;
    state.previewPath = '';
    video.pause();
    video.removeAttribute('src');
    video.load();
    videoStage.classList.remove('has-video');
    videoStage.classList.remove('playing');
    setSidebarPlayIcon(false);
    $('pvBtnPlay').disabled = true;
    pvSeekPlayed.style.width = '0%';
    pvSeekBuffer.style.width = '0%';
    pvTime.textContent = '00:00 / 00:00';
    $('pvName').textContent = '未选择视频';
    $('pvMeta').textContent = '';
    $('pvPath').textContent = '';
    $('pvState').textContent = '';
    hideVideoFail();
    $('pvSystem').disabled = true;
    $('pvFolder').disabled = true;
    $('pvPopout').disabled = true;
    $('pvDelete').disabled = true;
  }

  function syncPreviewInfo(id) {
    const item = state.byId.get(id);
    if (!item) return;
    $('pvName').textContent = item.name;
    $('pvMeta').textContent = `${fmtSize(item.size)} · ${item.status}`;
    const path = currentPathOf(item);
    $('pvPath').textContent = path;
    // 路径现在单行省略，完整值放到 tooltip 里，避免长路径把面板撑高
    $('pvPath').title = path ? `${path}（点击复制）` : '点击复制路径';
    $('pvSystem').disabled = !!item.deleted;
    $('pvFolder').disabled = !!item.deleted;
    $('pvPopout').disabled = !!item.deleted;
    $('pvDelete').disabled = state.running || !!item.deleted;
  }

  function currentPathOf(item) {
    if (!item) return '';
    return item.path;
  }

  function previewItem(id) {
    // 整理任务进行中也允许预览：原生侧 /media 用 FILE_SHARE_READ|WRITE|DELETE 打开文件，
    // 边复制边读不会冲突；之前这里直接拒绝，用户双击后只看到一条一闪而过的 toast，
    // 被当成"双击没反应"。真正被移走/删除的记录由原生返回明确错误。
    const item = state.byId.get(id);
    if (!item) return;
    state.previewId = id;
    state.previewPath = item.path;
    syncPreviewInfo(id);
    if (state.previewMode === 'popup') {
      openPopupPreview(id);
      return;
    }
    // 侧栏内预览：面板可能被用户收起（preview-collapsed），或被窄窗口媒体查询隐藏。
    // 不展开的话视频会在看不见的面板里播放，用户看到的只是"双击没反应"。
    setPreviewCollapsed(false);
    if ($('app').classList.contains('narrow-preview')) {
      $('app').classList.add('force-preview');
    }
    hideVideoFail();
    $('pvState').textContent = '正在加载预览…';
    api('preview', { id });
  }

  // 独立窗口播放
  function openPopupPreview(id) {
    const item = state.byId.get(id);
    if (!item) return;
    state.previewId = id;
    state.previewPath = item.path;
    syncPreviewInfo(id);
    stopInlineVideo();
    $('pvState').textContent = '正在独立窗口中打开…';
    api('preview_popout', { id });
  }

  function stopInlineVideo() {
    video.pause();
    video.removeAttribute('src');
    video.load();
    videoStage.classList.remove('has-video');
    videoStage.classList.remove('playing');
    hideVideoFail();
    setSidebarPlayIcon(false);
    $('pvBtnPlay').disabled = true;
    pvSeekPlayed.style.width = '0%';
    pvSeekBuffer.style.width = '0%';
    pvTime.textContent = '00:00 / 00:00';
  }

  function popoutPreview() {
    if (state.previewId === null) {
      toast('请先在列表中双击一个视频。', 'warn', 2200);
      return;
    }
    openPopupPreview(state.previewId);
  }

  video.addEventListener('loadedmetadata', () => {
    if (state.previewId === null) return;
    const item = state.byId.get(state.previewId);
    if (!item) return;
    const duration = fmtDuration(video.duration);
    const resolution = video.videoWidth ? `${video.videoWidth}×${video.videoHeight}` : '';
    const pieces = [fmtSize(item.size)];
    if (duration) pieces.push('时长 ' + duration);
    if (resolution) pieces.push(resolution);
    pieces.push(item.status);
    $('pvMeta').textContent = pieces.join(' · ');
    $('pvState').textContent = '';
    hideVideoFail();
  });

  video.addEventListener('error', () => {
    if (state.previewId === null) return;
    const item = state.byId.get(state.previewId);
    const ext = extOf(item ? item.name : '');
    const code = video.error ? video.error.code : 0;
    // 4 = MEDIA_ERR_SRC_NOT_SUPPORTED：容器可解但里面没有可解码的流（最常见）
    const reason = code === 4 ? '这种编码内置播放器解不了' : '这个视频加载失败';
    const sub = code === 4
      ? `「${(ext || '该格式').toUpperCase()}」里的视频/音频编码不在支持列表内（如 XVID、MPEG-2、AC3/DTS 音轨）。换 H.264 + AAC 的 MP4 可以内嵌播放。`
      : '文件可能被占用、路径失效或磁盘不可读。';
    showVideoFail(reason, sub);
    $('pvState').textContent = '已尝试内置播放，失败：请用系统播放器打开。';
    $('pvBtnPlay').disabled = true;
    setSidebarPlayIcon(false);
    videoStage.classList.remove('playing');
  });

  $('pvSystem').addEventListener('click', () => {
    if (state.previewId === null) return;
    const item = state.byId.get(state.previewId);
    if (item) api('open', { id: item.id, path: currentPathOf(item) });
  });

  $('pvFolder').addEventListener('click', () => {
    if (state.previewId === null) return;
    const item = state.byId.get(state.previewId);
    if (item) api('reveal', { id: item.id, path: currentPathOf(item) });
  });

  $('pvPopout').addEventListener('click', popoutPreview);
  $('previewPopout').addEventListener('click', popoutPreview);

  $('pvDelete').addEventListener('click', () => {
    if (state.previewId === null) return;
    if (!state.selected.has(state.previewId)) {
      state.selected.clear();
      state.selected.add(state.previewId);
      updateSelectionUI();
      renderRows();
    }
    deleteSelected();
  });

  $('pvPath').addEventListener('click', () => {
    if (!state.previewPath) return;
    copyText(state.previewPath).then(
      () => toast('已复制路径', 'success', 1800),
      () => toast('复制失败', 'error', 1800)
    );
  });

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise((resolve, reject) => {
      const area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand('copy') ? resolve() : reject();
      } catch (err) {
        reject(err);
      } finally {
        area.remove();
      }
    });
  }

  // 预览面板折叠
  function setPreviewCollapsed(collapsed) {
    const app = $('app');
    app.classList.toggle('preview-collapsed', collapsed);
    // 窄窗口下用户主动收起时，同时取消「临时展开」，让悬浮按钮重新出现
    if (collapsed) app.classList.remove('force-preview');
    localStorage.setItem('vc.previewCollapsed', collapsed ? '1' : '0');
    // 预览列一收一放会改变主区宽度，顶栏要跟着重新收纳
    scheduleToolbarFit();
    syncHeaderGutter();
  }

  // 顶栏的预览面板开关：与 Python 版的方框图标同语义（收起 / 展开整列）
  $('previewToggleBtn').addEventListener('click', () => {
    const app = $('app');
    const expanded = !app.classList.contains('preview-collapsed');
    if (expanded) {
      setPreviewCollapsed(true);
    } else {
      setPreviewCollapsed(false);
      if (app.classList.contains('narrow-preview')) app.classList.add('force-preview');
    }
  });

  // 顶栏按主区宽度收纳：窄于 980px 时把「删除选中 / 打开目标」收进「更多」菜单，
  // 否则搜索框被压到下限、右侧按钮直接被裁掉（Python 版用的同一条阈值）
  function applyToolbarFit() {
    const main = document.querySelector('.main');
    if (!main) return;
    const compact = main.clientWidth < 980;
    if (compact === state.toolbarCompact) return;
    state.toolbarCompact = compact;
    $('deleteBtn').hidden = compact;
    $('openDstBtn').hidden = compact;
    $('menuDeleteBtn').hidden = !compact;
    $('menuOpenDstBtn').hidden = !compact;
  }
  // 预览列收起/展开改的是 grid 列，宽度要等一帧布局后才准；单靠 rAF 偶尔
  // 读到的还是旧宽度，于是顶栏没能回弹。补一个短延时重算兜底。
  function scheduleToolbarFit() {
    requestAnimationFrame(applyToolbarFit);
    setTimeout(applyToolbarFit, 150);
  }
  window.addEventListener('resize', applyToolbarFit);
  window.addEventListener('resize', scheduleToolbarFit);
  scheduleToolbarFit();

  // 表头和行各是一次 grid，但 .rows 有 scrollbar-gutter: stable，会永远预留
  // 一条滚动条宽度；表头没有，于是越往右列偏移越大（"大小/状态"最明显）。
  // 这里把滚动条实际宽度补进表头右侧内边距，两套 grid 的可用宽度就一致了。
  function syncHeaderGutter() {
    const rows = $('rows');
    const thead = document.querySelector('.thead');
    if (!rows || !thead) return;
    const gutter = rows.offsetWidth - rows.clientWidth;
    thead.style.paddingRight = (12 + Math.max(0, gutter)) + 'px';
  }
  window.addEventListener('resize', syncHeaderGutter);
  requestAnimationFrame(syncHeaderGutter);

  // 窄窗口（≤1320 逻辑像素，高 DPI 缩放下很常见）时标记 narrow-preview，
  // CSS 会收起预览列；用户点悬浮按钮可临时展开一个 300px 窄列。
  // 阈值必须和 style.css 里的 @media (max-width: 1320px) 一致。
  function applyNarrowPreview() {
    $('app').classList.toggle('narrow-preview', window.innerWidth <= 1320);
    if (window.innerWidth > 1320) $('app').classList.remove('force-preview');
  }

  $('previewCollapse').addEventListener('click', () => setPreviewCollapsed(true));
  $('previewFab').addEventListener('click', () => {
    setPreviewCollapsed(false);
    if ($('app').classList.contains('narrow-preview')) {
      $('app').classList.add('force-preview');
    }
  });

  // ------------------------------------------------------------------ 右键菜单
  const ctxMenu = $('ctxMenu');

  rowsEl.addEventListener('contextmenu', (event) => {
    const row = event.target.closest('.row');
    if (!row) return;
    event.preventDefault();
    const id = Number(row.dataset.id);
    if (!state.selected.has(id)) {
      state.selected.clear();
      state.selected.add(id);
      renderRows();
      updateSelectionUI();
    }
    state.ctxId = id;
    showCtxMenu(event.clientX, event.clientY);
  });

  function showCtxMenu(x, y) {
    ctxMenu.hidden = false;
    const width = ctxMenu.offsetWidth;
    const height = ctxMenu.offsetHeight;
    const left = Math.min(x, window.innerWidth - width - 8);
    const top = Math.min(y, window.innerHeight - height - 8);
    ctxMenu.style.left = Math.max(6, left) + 'px';
    ctxMenu.style.top = Math.max(6, top) + 'px';
  }

  function hideCtxMenu() {
    ctxMenu.hidden = true;
  }

  ctxMenu.addEventListener('click', (event) => {
    const btn = event.target.closest('button');
    if (!btn) return;
    const action = btn.dataset.act;
    const id = state.ctxId;
    hideCtxMenu();
    const item = state.byId.get(id);
    if (!item) return;
    if (action === 'preview') previewItem(id);
    else if (action === 'popout') openPopupPreview(id);
    else if (action === 'open') api('open', { id, path: item.path });
    else if (action === 'reveal') api('reveal', { id, path: item.path });
    else if (action === 'copy') {
      copyText(item.path).then(
        () => toast('已复制路径', 'success', 1800),
        () => toast('复制失败', 'error', 1800)
      );
    } else if (action === 'delete') deleteSelected();
  });

  window.addEventListener('mousedown', (event) => {
    if (!event.target.closest('.ctx-menu') && !event.target.closest('.row') &&
        event.target !== ctxMenu) {
      hideCtxMenu();
    }
  });
  rowsEl.addEventListener('scroll', hideCtxMenu);
  window.addEventListener('resize', () => {
    hideCtxMenu();
    applyNarrowPreview();
    clampLogHeight();
  });

  // ------------------------------------------------------------------ 其他按钮
  $('pickSrc').addEventListener('click', () => api('pick', { field: 'src', current: cfg.src.value.trim() }));
  $('pickDst').addEventListener('click', () => api('pick', { field: 'dst', current: cfg.dst.value.trim() }));

  // ------------------------------------------------------------------ 格式多选（下拉勾选）
  // 用户要求的"下拉框 + 勾选"：勾选常见格式，另有输入框补充自定义扩展名，
  // 最终仍拼成逗号串写回隐藏的 #exts，原生侧完全不用改。
  const EXT_FORMATS = DEFAULT_EXTS.split(',');

  function parseExts(value) {
    return String(value || '')
      .split(',')
      .map((s) => s.trim().toLowerCase().replace(/^\./, ''))
      .filter(Boolean);
  }

  function updateExtSummary() {
    const sum = $('extSummary');
    if (!sum) return;
    const cur = parseExts(cfg.exts.value);
    sum.textContent = cur.length
      ? '已选 ' + cur.length + ' 种：' + cur.slice(0, 5).join(', ') + (cur.length > 5 ? ' …' : '')
      : '未选择任何格式';
    sum.classList.toggle('warn', cur.length === 0);
  }

  function renderExtPicker() {
    const grid = $('extGrid');
    if (!grid) return;
    const cur = parseExts(cfg.exts.value);
    if (!grid.dataset.built) {
      const frag = document.createDocumentFragment();
      for (const ext of EXT_FORMATS) {
        const label = document.createElement('label');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = ext;
        cb.addEventListener('change', syncExtFromUI);
        label.appendChild(cb);
        label.appendChild(document.createTextNode(ext));
        frag.appendChild(label);
      }
      grid.appendChild(frag);
      grid.dataset.built = '1';
    }
    for (const cb of grid.querySelectorAll('input')) cb.checked = cur.includes(cb.value);
    // 自定义项：当前值里不在常见列表里的扩展名
    const extra = cur.filter((e) => !EXT_FORMATS.includes(e));
    if (document.activeElement !== $('extCustom')) $('extCustom').value = extra.join(', ');
    updateExtSummary();
  }

  function collectExtValue() {
    const grid = $('extGrid');
    const picked = [];
    if (grid) for (const cb of grid.querySelectorAll('input')) if (cb.checked) picked.push(cb.value);
    return [...new Set([...picked, ...parseExts($('extCustom').value)])].join(',');
  }

  function syncExtFromUI() {
    cfg.exts.value = collectExtValue();
    updateExtSummary();
    saveSettingsSoon();
  }

  function closeExtPop() {
    if (!$('extPop')) return;
    $('extPop').hidden = true;
    $('extBtn').setAttribute('aria-expanded', 'false');
  }

  $('extBtn').addEventListener('click', () => {
    const willOpen = $('extPop').hidden;
    if (willOpen) renderExtPicker();
    $('extPop').hidden = !willOpen;
    $('extBtn').setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  });
  $('extCustom').addEventListener('input', syncExtFromUI);
  $('extAll').addEventListener('click', () => {
    for (const cb of $('extGrid').querySelectorAll('input')) cb.checked = true;
    syncExtFromUI();
  });
  $('extNone').addEventListener('click', () => {
    for (const cb of $('extGrid').querySelectorAll('input')) cb.checked = false;
    $('extCustom').value = '';
    syncExtFromUI();
  });
  $('resetExts').addEventListener('click', () => {
    cfg.exts.value = DEFAULT_EXTS;
    renderExtPicker();
    saveSettingsSoon();
  });
  document.addEventListener('mousedown', (event) => {
    if ($('extPop').hidden) return;
    if (event.target.closest('.ext-picker')) return;
    closeExtPop();
  });

  $('startBtn').addEventListener('click', startSearch);
  $('cancelBtn').addEventListener('click', () => api('cancel'));

  function openTargetDir() {
    const dst = cfg.dst.value.trim();
    if (!dst) {
      toast('请先设置目标文件夹。', 'warn');
      return;
    }
    api('open_dir', { path: dst });
  }

  $('openDstBtn').addEventListener('click', openTargetDir);
  // 窄宽度下这两个动作收进「更多」菜单（与 Python 版同一套收纳规则）
  $('menuOpenDstBtn').addEventListener('click', openTargetDir);
  $('menuDeleteBtn').addEventListener('click', deleteSelected);

  $('reportBtn').addEventListener('click', () => {
    if (!state.items.length) {
      toast('还没有可导出的记录。', 'warn');
      return;
    }
    api('report', { dir: cfg.dst.value.trim() });
  });

  $('reportHtmlBtn').addEventListener('click', () => {
    if (!state.items.length) {
      toast('还没有可导出的记录。', 'warn');
      return;
    }
    api('report_html', { dir: cfg.dst.value.trim() });
  });

  $('undoBtn').addEventListener('click', () => {
    if (!state.undoAvailable) {
      toast('没有可撤销的整理成果。', 'warn');
      return;
    }
    showModal({
      title: '撤销本次整理？',
      body: '将删除本次生成的副本；移动的文件会移回源位置。此操作会把文件移入回收站，可还原。',
      okText: '撤销',
      danger: true,
    }).then((result) => {
      if (result.ok) api('undo', {});
    });
  });

  // ------------------------------------------------------------------「更多」菜单
  // 导出 / 撤销收进下拉：7 个按钮并排时，窗口宽 1261~1390px 之间工具栏必然溢出
  // （搜索框已被压到 160px 下限，右侧按钮会被裁掉）。收进菜单后剩下的
  // 删除选中 / 打开目标 / 更多 / 帮助 在任何宽度都放得下。
  const moreWrap = $('moreWrap');
  const moreMenu = $('moreMenu');
  const moreBtn = $('moreBtn');
  function setMoreOpen(open) {
    if (!moreMenu || !moreBtn) return;
    moreMenu.hidden = !open;
    moreBtn.classList.toggle('open', open);
    moreBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  moreBtn.addEventListener('click', (event) => {
    event.stopPropagation();
    setMoreOpen(moreMenu.hidden);
  });
  moreMenu.addEventListener('click', (event) => {
    if (event.target.closest('button')) setMoreOpen(false);
  });
  document.addEventListener('mousedown', (event) => {
    if (!moreMenu.hidden && !moreWrap.contains(event.target)) setMoreOpen(false);
  });

  $('logToggle').addEventListener('click', () => {
    setLogCollapsed(!$('logCard').classList.contains('collapsed'));
  });
  $('logClear').addEventListener('click', (event) => {
    event.stopPropagation();
    clearLog();
  });

  // 日志面板高度拖拽：按住手柄上下拖动，记忆到 localStorage
  const LOG_MIN_H = 42;
  function applyLogHeight(px) {
    const card = $('logCard');
    const clamped = Math.max(LOG_MIN_H, Math.min(logMaxHeight(), Math.round(px)));
    card.style.height = clamped + 'px';
    localStorage.setItem('vc.logHeight', String(clamped));
  }
  (function initLogResize() {
    const handle = $('logResize');
    if (!handle) return;
    // 恢复上次高度（未折叠时）
    const saved = parseInt(localStorage.getItem('vc.logHeight'), 10);
    if (saved >= LOG_MIN_H && !document.querySelector('#logCard').classList.contains('collapsed')) {
      $('logCard').style.height = saved + 'px';
    }
    let startY = 0;
    let startH = 0;
    function onMove(event) {
      event.preventDefault();
      applyLogHeight(startH + (event.clientY - startY));
    }
    function onUp() {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      const card = $('logCard');
      card.classList.remove('dragging');
      document.body.style.userSelect = '';
      // 恢复表头过渡动画
      card.style.transition = '';
    }
    handle.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      const card = $('logCard');
      card.classList.add('dragging');
      card.style.transition = 'none';
      document.body.style.userSelect = 'none';
      startY = event.clientY;
      startH = card.getBoundingClientRect().height;
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    });
  })();

  // 向导里的开关 / 外观卡里的分段，改动后都要落盘并刷新侧栏摘要
  document.querySelectorAll('#setupBody input, .card input, #setupBody .switch, .card .switch').forEach((input) => {
    input.addEventListener('change', () => {
      saveSettingsSoon();
      updatePlanSummary();
    });
  });
  cfg.src.addEventListener('input', () => { saveSettingsSoon(); updatePlanSummary(); });
  cfg.dst.addEventListener('input', () => { saveSettingsSoon(); updatePlanSummary(); });
  cfg.exts.addEventListener('input', saveSettingsSoon);
  cfg.minSize.addEventListener('input', updatePlanSummary);
  cfg.maxSize.addEventListener('input', updatePlanSummary);

  // ------------------------------------------------------------------ 当前设置摘要
  // 侧栏只显示只读摘要，真正的编辑控件都在向导里，改完立刻反映到这里。
  function updatePlanSummary() {
    const sSearch = $('sumSearch');
    if (!sSearch) return;
    const sDir = $('sumDir');
    const sMode = $('sumMode');
    const sConflict = $('sumConflict');
    const sToggles = $('sumToggles');

    const isAll = state.searchMode === 'all';
    sSearch.textContent = isAll ? '全盘搜索' : '指定目录';

    const src = cfg.src.value.trim();
    const dst = cfg.dst.value.trim();
    let dirText;
    if (isAll) dirText = dst ? '→ ' + dst : '目标文件夹未选';
    else if (src && dst) dirText = src + ' → ' + dst;
    else if (src) dirText = src;
    else if (dst) dirText = '→ ' + dst;
    else dirText = '未选择';
    sDir.textContent = dirText;
    sDir.title = dirText;

    sMode.textContent = state.mode === 'move' ? '移动' : '复制';
    sConflict.textContent = state.conflict === 'skip' ? '同名跳过' : '自动重命名';

    const min = readSizeLimit(cfg.minSize);
    const max = readSizeLimit(cfg.maxSize);
    if (min && max && min > max) {
      sToggles.textContent = '体积范围有误：最小不能大于最大';
      sToggles.classList.add('warn');
      return;
    }
    sToggles.classList.remove('warn');
    const bits = [];
    bits.push(cfg.recursive.checked ? '含子文件夹' : '仅当前目录');
    if (cfg.hidden.checked) bits.push('跳过隐藏');
    if (cfg.dedupe.checked) bits.push('跳过重复');
    if (cfg.autoOpen.checked) bits.push('完成后打开');
    if (min || max) {
      const fmt = (mb) => (mb >= 1024 ? (mb / 1024).toFixed(mb % 1024 ? 1 : 0) + 'GB' : mb + 'MB');
      bits.push('体积 ' + (min ? fmt(min) : '0') + '-' + (max ? fmt(max) : '不限'));
    }
    sToggles.textContent = bits.join(' · ');
    sToggles.title = bits.join(' · ');
  }

  // ------------------------------------------------------------------ 快捷键
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      if (!moreMenu.hidden) {
        setMoreOpen(false);
        return;
      }
      if (!$('extPop').hidden) {
        closeExtPop();
        return;
      }
      if (!$('scanAllMask').hidden) {
        hideScanAllDialog();
        return;
      }
      if (!$('setupMask').hidden) {
        dismissSetup();
        return;
      }
      if (!$('modalMask').hidden) {
        closeModal({ ok: false });
        hideCtxMenu();
        hideHistMenu();
        return;
      }
      hideCtxMenu();
      hideHistMenu();
      if (state.statusFilter || $('filter').value) {
        state.statusFilter = null;
        state.filter = '';
        $('filter').value = '';
        viewDirty = true;
        statsDirty = true;
        scheduleRefresh(0);
      } else {
        state.selected.clear();
        renderRows();
        updateSelectionUI();
      }
      return;
    }
    if (event.key === 'F1') {
      // 模态框 / 全盘搜索弹层打开时不弹帮助框，避免覆盖 modalResolve 造成确认框挂起
      if (!$('modalMask').hidden || !$('scanAllMask').hidden || !$('setupMask').hidden) return;
      event.preventDefault();
      showShortcuts();
      return;
    }
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') {
      return;
    }
    // 模态框存在时，列表快捷键全部禁用，避免误删
    if (!$('modalMask').hidden || !$('scanAllMask').hidden || !$('setupMask').hidden) return;
    if (event.key === '?') {
      event.preventDefault();
      showShortcuts();
      return;
    }
    if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === 'a') {
      event.preventDefault();
      state.selected.clear();
      renderRows();
      updateSelectionUI();
      return;
    }
    if (event.ctrlKey && event.key.toLowerCase() === 'f') {
      event.preventDefault();
      $('filter').focus();
      return;
    }
    if (event.ctrlKey && event.key.toLowerCase() === 'a') {
      event.preventDefault();
      selectAllVisible();
      return;
    }
    if (event.key === 'Delete') {
      event.preventDefault();
      deleteSelected();
      return;
    }
    if (event.key === 'Enter') {
      // 焦点在按钮/链接上时由浏览器处理默认 click，避免一次按键触发两个动作
      const tag = document.activeElement && document.activeElement.tagName;
      if (tag === 'BUTTON' || tag === 'A' || tag === 'SELECT' ||
          (document.activeElement && document.activeElement.isContentEditable)) {
        return;
      }
      event.preventDefault();
      const first = state.selected.values().next();
      if (!first.done) previewItem(first.value);
    }
  });

  // ------------------------------------------------------------------ 原生消息
  function handleNativeMessage(message) {
    switch (message.type) {
      case 'hello':
        state.port = Number(message.port) || 0;
        if (state.port) renderRows(); // 首次拿到端口后再拉缩略图
        break;

      case 'settings':
        applySettings(message.data);
        maybeFirstSetup();
        break;

      case 'drives':
        state.drives = Array.isArray(message.drives) ? message.drives : [];
        break;

      case 'need_create_files': {
        const dst = message.dst || '';
        showModal({
          title: '目标文件夹不存在',
          body: `是否创建以下文件夹并开始整理选中项？\n${dst}`,
          okText: '创建并开始',
        }).then((result) => {
          if (result.ok) startFiles(true);
        });
        break;
      }

      case 'scan_progress':
        $('progressText').textContent =
          `已扫描 ${message.dirs || 0} 个目录 · 发现 ${message.found || 0} 个视频`;
        $('progressPct').textContent = '—';
        break;

      case 'scan_done': {
        state.jobDone = true;
        setRunning(false);
        document.querySelector('.progress-card').classList.remove('indeterminate');
        const s = message.stats || {};
        $('progressFill').style.width = '0%';
        $('progressPct').textContent = '—';
        const label = state.searchMode === 'all' ? '全盘搜索' : '搜索';
        const summary = `${label}${s.cancelled ? '已取消' : '完成'} · 共 ${s.total || 0} 个视频` +
          ` · 已扫描 ${s.dirs || 0} 个目录` + (s.truncated ? '（已达上限）' : '');
        $('progressText').textContent = summary;
        state.footerPhase = null;
        updateFooter();
        toast(summary, (s.cancelled || s.truncated) ? 'warn' : 'success', 6000);
        if (s.error) toast(s.error, 'error', 7000);
        scheduleRefresh(120);
        break;
      }

      case 'items': {
        state.total = message.total || 0;
        for (const item of message.items || []) {
          const record = {
            id: item.id,
            name: item.name,
            size: item.size,
            path: item.path,
            dir: item.dir,
            status: '排队中',
            target: '',
          };
          if (state.byId.has(item.id)) continue;
          state.items.push(record);
          state.byId.set(item.id, record);
        }
        viewDirty = true;
        statsDirty = true;
        scheduleRefresh(70);
        break;
      }

      case 'status': {
        const item = state.byId.get(message.id);
        if (!item) break;
        if (message.status) item.status = message.status;
        if (message.target) item.target = message.target;
        if (message.target_created) item.targetCreated = true;
        if (message.deleted) item.deleted = true;
        statsDirty = true;
        // 状态胶囊筛选也依赖状态值，状态变化必须重算视图并清理隐藏行的选中态
        if (state.sort.key === 'status' || state.filter || state.statusFilter) {
          viewDirty = true;
        }
        scheduleRefresh(90);
        if (state.previewId === message.id) {
          const meta = $('pvMeta');
          const now = state.byId.get(message.id);
          if (meta && now) meta.textContent = `${fmtSize(now.size)} · ${now.status}`;
          if (now) syncPreviewInfo(message.id);
          if (now && now.deleted) {
            $('pvState').textContent = '文件已移入回收站。';
            video.pause();
          }
        }
        break;
      }

      case 'progress': {
        const total = message.total || 0;
        const done = message.done || 0;
        state.done = done;
        document.querySelector('.progress-card').classList.remove('indeterminate');
        const percent = total > 0 ? Math.round((done / total) * 100) : 0;
        $('progressFill').style.width = percent + '%';
        $('progressPct').textContent = percent + '%';
        const name = message.name || '';
        const shown = name.length > 40 ? name.slice(0, 40) + '…' : name;
        $('progressText').textContent =
          `正在处理 ${done} / ${total}` + (shown ? `　·　${shown}` : '');
        break;
      }

      case 'log':
        pushLog(message.text, message.level);
        break;

      case 'done': {
        state.jobDone = true;
        setRunning(false);
        if (state.jobKind !== 'scan') api('undo_available');
        document.querySelector('.progress-card').classList.remove('indeterminate');
        const stats = message.stats || {};
        // scanMode 只在 scan_all 响应里置为 true：表格里始终是可勾选的只读搜索结果。
        // 「整理选中项」只处理子集，剩余搜索结果仍可继续勾选整理。
        if (state.jobKind === 'files' && !stats.cancelled) {
          state.footerPhase = null;
          updateFooter();
        }
        const total = stats.total || 0;
        $('progressFill').style.width = stats.cancelled ? `${total ? Math.round((state.done / total) * 100) : 0}%` : '100%';
        $('progressPct').textContent = stats.cancelled ? '已取消' : '100%';
        if (stats.cancelled) {
          $('progressText').textContent = `任务已取消（${state.done} / ${total}）`;
        } else {
          $('progressText').textContent =
            `完成：成功 ${stats.success || 0} · 跳过 ${stats.skipped || 0} · 失败 ${stats.failed || 0} · 已在目标 ${stats.inplace || 0}`;
        }
        const summary =
          `整理结束（用时 ${(stats.seconds || 0).toFixed(1)} 秒）\n` +
          `成功 ${stats.success || 0} 个，跳过 ${stats.skipped || 0} 个，` +
          `失败 ${stats.failed || 0} 个，已在目标 ${stats.inplace || 0} 个，` +
          `共处理 ${fmtSize(stats.bytes || 0)}。`;
        toast(summary, stats.failed > 0 ? 'warn' : stats.cancelled ? 'warn' : 'success', 6000);
        if (stats.error) toast(stats.error, 'error', 7000);
        // 「完成后打开目标」：只在正常完成且有成功项时触发，取消/异常不打扰
        if (state.autoOpenDst && !stats.cancelled && !stats.error && (stats.success || 0) > 0) {
          const dst = cfg.dst.value.trim();
          if (dst) api('open_dir', { path: dst });
        }
        scheduleRefresh(120);
        break;
      }

      case 'preview': {
        if (!message.ok) {
          $('pvState').textContent = message.err || '预览失败';
          toast(message.err || '预览失败', 'warn');
          break;
        }
        state.previewId = message.id;
        state.previewPath = message.path || '';
        syncPreviewInfo(message.id);
        if (message.path) $('pvPath').textContent = message.path;
        if (message.popout) {
          // 已在独立窗口打开，面板不再重复拉流
          state.popupOpen = true;
          stopInlineVideo();
          $('pvState').textContent = '已在独立窗口中播放。';
          break;
        }
        videoStage.classList.add('has-video');
        $('pvBtnPlay').disabled = false;
        pvSeekPlayed.style.width = '0%';
        pvSeekBuffer.style.width = '0%';
        pvTime.textContent = '00:00 / 00:00';
        hideVideoFail();
        video.src = message.url;
        video.load();
        startInlinePlayback();
        // 扩展名预判：这些格式内置播放器大概率解不了，先提示，真能播会在
        // loadedmetadata 里自动清掉；解不了则 error 会重写成更明确的原因。
        const ext = extOf(message.path || (state.byId.get(message.id) || {}).name || '');
        $('pvState').textContent = RISKY_EXTS.includes(ext)
          ? `正在尝试内置播放…（${ext.toUpperCase()} 等格式可能不支持，可在下方改用系统播放器）`
          : '';
        break;
      }

      case 'drop': {
        const path = (message.path || '').trim();
        if (!path) break;
        // 原生传的是物理像素坐标，转到 CSS 像素后判断落在哪个输入框
        const dpr = window.devicePixelRatio || 1;
        const el = document.elementFromPoint(
          (Number(message.x) || 0) / dpr,
          (Number(message.y) || 0) / dpr
        );
        let input = null;
        if (el) {
          if (el.id === 'src') input = cfg.src;
          else if (el.id === 'dst') input = cfg.dst;
        }
        if (!input) input = cfg.src.value.trim() ? cfg.dst : cfg.src;
        input.value = path;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        toast('已填入：' + path, 'success', 2200);
        break;
      }

      case 'preview_window':
        state.popupOpen = !!message.open;
        if (!state.popupOpen && state.previewMode === 'popup') {
          $('pvState').textContent = '独立窗口已关闭。';
        }
        break;

      case 'undo_available':
        state.undoAvailable = !!message.available;
        updateSelectionUI();
        break;

      case 'undo_locked':
        // 撤销正在进行：禁用撤销按钮，避免重复触发
        state.undoAvailable = false;
        updateSelectionUI();
        break;

      case 'undo_done':
        // 撤销完成：后端已用 response 通知结果，这里仅确保按钮状态与统计刷新
        state.undoAvailable = false;
        updateSelectionUI();
        scheduleRefresh(90);
        break;

      case 'response':
        handleResponse(message);
        break;

      default:
        break;
    }
  }

  function handleResponse(message) {
    switch (message.cmd) {
      case 'cancel':
        break;

      case 'scan_all':
        if (message.ok) {
          if (state.jobDone) break;
          resetItems();
          state.scanMode = true;
          state.statusFilter = null;
          viewDirty = true;
          statsDirty = true;
          setRunning(true);
          const allScan = state.searchMode === 'all';
          $('progressText').textContent = allScan ? '正在全盘搜索…' : '正在搜索…';
          state.footerPhase = 'search';
          updateFooter();
          document.querySelector('.progress-card').classList.add('indeterminate');
        } else {
          toast(message.msg || '无法开始全盘搜索。', 'error');
        }
        break;

      case 'start_files':
        if (message.ok) {
          if (state.jobDone) break;
          setRunning(true);
          state.undoAvailable = false;   // 新一轮整理选中项会清空撤销快照
          $('progressText').textContent = '正在整理选中项…';
          document.querySelector('.progress-card').classList.remove('indeterminate');
        } else {
          toast(message.msg || '无法整理选中项。', 'error');
        }
        break;

      case 'pick': {
        if (!message.ok) break;
        const field = message.field === 'dst' ? 'dst' : 'src';
        cfg[field].value = message.path || '';
        wpath[field].value = cfg[field].value;
        if (field === 'src' && !cfg.dst.value.trim() && message.path) {
          cfg.dst.value = message.path.replace(/\\$/, '') + '\\视频汇总';
          wpath.dst.value = cfg.dst.value;
        }
        saveSettingsSoon();
        updatePlanSummary();
        break;
      }

      case 'delete': {
        const type = !message.ok ? 'error' : (message.failed ? 'warn' : 'success');
        toast(message.msg || (message.ok ? '已移入回收站。' : '删除失败。'), type);
        break;
      }

      case 'undo':
        if (message.ok) {
          if (message.changed) {
            statsDirty = true;
            viewDirty = true;
            scheduleRefresh(90);
          }
          toast(message.msg || '已撤销。', message.failed ? 'warn' : 'success');
        } else {
          toast(message.msg || '撤销失败。', 'error');
        }
        break;

      case 'report':
        if (message.ok) toast('报告已保存：' + (message.path || ''), 'success', 5000);
        else if (message.msg) toast(message.msg, 'error');
        break;

      case 'preview_popout':
        if (!message.ok) {
          $('pvState').textContent = message.msg || '无法打开独立预览窗口';
          toast(message.msg || '无法打开独立预览窗口', 'warn');
        }
        break;

      case 'open':
      case 'reveal':
      case 'open_dir':
        if (!message.ok && message.msg) toast(message.msg, 'error');
        break;

      default:
        break;
    }
  }

  // ------------------------------------------------------------------ 初始化
  function init() {
    if (!bridge) {
      toast('当前环境不支持 WebView2 消息通道，功能不可用。', 'error', 8000);
      return;
    }
    bridge.addEventListener('message', (event) => {
      const data = event.data;
      if (data && typeof data === 'object' && data.type) handleNativeMessage(data);
    });

    cfg.exts.value = DEFAULT_EXTS;
    renderExtPicker();

    // 原生启动时通过 ?theme= 传入 config.json 中的主题，优先级高于 localStorage
    let queryTheme = null;
    try {
      queryTheme = new URLSearchParams(location.search).get('theme');
    } catch (err) {}
    const stored = queryTheme || localStorage.getItem('vc.theme');
    applyTheme(stored === 'light' ? 'light' : 'dark', false);

    // 恢复面板状态
    applyNarrowPreview();
    if (localStorage.getItem('vc.previewCollapsed') === '1') setPreviewCollapsed(true);
    setLogCollapsed(localStorage.getItem('vc.logCollapsed') === '1');
    clampLogHeight();
    clearLog();

    refreshAll();
    setRunning(false);
    syncHeaderGutter();
    api('ready');
  }

  init();
})();

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主窗口
======

界面按 C++（WebView2）版的设计语言重做：左侧目录/选项栏、中部任务区、右侧预览栏。

- 左侧：品牌区 + 目录设置卡 + 整理选项卡（分段控件 / 开关）+ 固定底部主操作
- 中部：标题与计数徽章、搜索工具条、统计胶囊、渐变进度条、文件列表、可折叠日志
- 右侧：视频预览（内嵌播放 / 独立窗口）、文件信息、快捷操作

数据安全约定保持不变：回收站删除、移动后大小校验、内容去重、线程安全、取消语义。
"""

import csv
import os
import subprocess
import sys
import time

from PySide6.QtCore import (
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QPoint,
    QPointF,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPixmap,
    QShortcut,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .constants import DEFAULT_EXTS, RISKY_EXTS, SIZE_LIMIT_MAX_MB, SOFT_RISKY_EXTS  # noqa: E402
from .setup_dialog import SetupDialog
from .table_model import (
    COL_CHECK,
    COL_DIR,
    COL_NAME,
    COL_SEQ,
    COL_SIZE,
    COL_STATUS,
    COL_THUMB,
    THUMB_ROLE,
    UID_ROLE,
    VideoFilterProxy,
    VideoTableModel,
)
from .theme import COLORS
from .utils import (
    ThumbnailCache,
    ThumbnailWorker,
    app_icon,
    format_duration,
    human_size,
    norm_path,
)
from .widgets import (
    AutoHideLabel,
    BackgroundWidget,
    Badge,
    Card,
    CheckBox,
    CheckHeaderView,
    Chip,
    ComboBox,
    Divider,
    ElideLabel,
    EmptyArt,
    FlowRow,
    IconButton,
    InputGroup,
    LogoBadge,
    ProgressTrack,
    PulseDot,
    SegmentedControl,
    StatusPillDelegate,
    SwitchRow,
    ToastManager,
    clear_icon_caches,
    line_icon,
    status_dot_icon,
    status_kind,
    thumb_placeholder,
)
from .scan_all import (
    DEFAULT_MAX_RESULTS,
    ScanAllDialog,
    ScanAllWorker,
    parse_custom_excludes,
)
from .worker import CollectWorker, DeleteWorker, UndoWorker

APP_NAME = "视频集中整理工具"
APP_VERSION = "2.0"

# 两端保持一致的快捷键说明（C++ 版 ui/app.js 中有等价定义）
SHORTCUTS = [
    ("Ctrl+F", "聚焦筛选框"),
    ("Ctrl+A", "全选当前可见行"),
    ("Ctrl+Shift+A", "取消所有选中"),
    ("Enter", "预览选中的视频"),
    ("Delete", "把选中项移入回收站"),
    ("Esc", "清除筛选 / 取消选中 / 关闭菜单"),
    ("F1", "显示本说明"),
]
SHORTCUT_TIPS = [
    "点击统计胶囊可按状态筛选（如「仅看失败」），再点一次或按 Esc 取消",
    "双击列表行可预览；右键行可打开、定位、复制路径或删除",
    "把文件夹拖到窗口上可快速填入源目录",
    "拖动日志面板顶部的细条可调整日志高度",
    "筛选状态下全选 / 删除只作用于可见行，不会碰到被隐藏的文件",
]

# 默认扩展名 / 体积上限 / 解码风险清单放在 constants 里，setup_dialog 也要用，
# 避免两个界面模块互相 import。这里直接再导出，模块内引用名保持不变。
from .constants import DEFAULT_EXTS, RISKY_EXTS, SIZE_LIMIT_MAX_MB, SOFT_RISKY_EXTS  # noqa: E402

# 缩略图单元格尺寸（与行高保持一致）
THUMB_COL_W = 104
THUMB_ROW_H = 68

# 关闭窗口时仍未停下的后台线程放这里，避免它在「运行中」被析构导致 abort。
_ORPHAN_THREADS = []


class ClickableLabel(AutoHideLabel):
    """可点击的标签（用于「点击复制路径」），空文本时同样自动隐藏。"""

    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setCursor(Qt.PointingHandCursor)
        self.clicked = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and callable(self.clicked):
            self.clicked()
        super().mousePressEvent(event)


class PreviewDialog(QDialog):
    """独立窗口播放：自带播放器与完整控制条，避免和侧栏共用同一个视频输出。

    这个窗口固定走「深色播放器」外观（视频画面周围用黑色最耐看），所以样式
    全部限定在 #PreviewDialog 作用域内：否则会继承浅色主题的深色文字，
    在黑色控制条上直接看不见。
    """

    _QSS = """
    #PreviewDialog { background-color: #05070a; }
    #PreviewDialog QWidget#PreviewBar {
        background-color: #0a0d12;
        border-top: 1px solid #1e293b;
    }
    #PreviewDialog QPushButton {
        background-color: rgba(255, 255, 255, 0.08);
        color: #dbe3f0;
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 8px;
        padding: 3px 14px;
        font-size: 12px;
    }
    #PreviewDialog QPushButton:hover { background-color: rgba(255, 255, 255, 0.16); }
    #PreviewDialog QPushButton:pressed { background-color: rgba(79, 141, 255, 0.35); }
    #PreviewDialog QLabel { color: #9aa8bc; }
    #PreviewDialog QComboBox {
        background-color: rgba(255, 255, 255, 0.08);
        color: #dbe3f0;
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 8px;
        padding: 2px 6px;
    }
    #PreviewDialog QComboBox QAbstractItemView {
        background-color: #12161f;
        color: #dbe3f0;
        selection-background-color: rgba(79, 141, 255, 0.4);
    }
    #PreviewDialog QSlider::groove:horizontal {
        height: 4px;
        background: rgba(255, 255, 255, 0.16);
        border-radius: 2px;
    }
    #PreviewDialog QSlider::sub-page:horizontal {
        background: #4f8dff;
        border-radius: 2px;
    }
    #PreviewDialog QSlider::handle:horizontal {
        background: #ffffff;
        border: none;
        width: 12px;
        height: 12px;
        margin: -5px 0;
        border-radius: 6px;
    }
    """

    def __init__(self, path, volume=60, parent=None):
        super().__init__(parent)
        self.setObjectName("PreviewDialog")
        self.setWindowTitle(os.path.basename(path) or APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(960, 600)
        self.setMinimumSize(560, 360)
        self._seeking = False
        self._path = path

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.video = QVideoWidget(self)
        self.video.setAspectRatioMode(Qt.KeepAspectRatio)
        self.video.setStyleSheet("background-color: #000000;")
        layout.addWidget(self.video, 1)

        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.video)
        self.audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_out)
        self.audio_out.setVolume(max(0.0, min(1.0, volume / 100.0)))

        # -- 控制条 -------------------------------------------------------
        bar = QWidget(self)
        bar.setObjectName("PreviewBar")
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(14, 8, 14, 10)
        bar_layout.setSpacing(8)

        self.slider = QSlider(Qt.Horizontal, bar)
        self.slider.setRange(0, 0)
        self.slider.setSingleStep(1000)
        self.slider.setPageStep(10000)
        bar_layout.addWidget(self.slider)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.btn_play = QPushButton("暂停", bar)
        self.btn_play.setFixedHeight(30)
        self.btn_play.setMinimumWidth(72)
        self.btn_play.setCursor(Qt.PointingHandCursor)
        self.lbl_time = QLabel("00:00 / 00:00", bar)
        self.lbl_time.setStyleSheet(
            "color: #9aa8bc; font-family: Consolas, monospace; font-size: 11px;")
        self.rate_combo = ComboBox(bar)
        self.rate_combo.setFixedHeight(28)
        self.rate_combo.setFixedWidth(76)
        self.rate_combo.addItems(["0.5x", "0.75x", "1x", "1.25x", "1.5x", "2x"])
        self.rate_combo.setCurrentIndex(2)
        self.rate_combo.setCursor(Qt.PointingHandCursor)
        self.vol_label = QLabel("音量", bar)
        self.vol_label.setStyleSheet("color: #9aa8bc; font-size: 12px;")
        self.vol_slider = QSlider(Qt.Horizontal, bar)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(round(max(0.0, min(1.0, volume / 100.0)) * 100))
        self.vol_slider.setFixedWidth(72)
        self.vol_slider.setCursor(Qt.PointingHandCursor)
        self.btn_mute = QPushButton("静音", bar)
        self.btn_mute.setFixedHeight(28)
        self.btn_mute.setCursor(Qt.PointingHandCursor)
        self.btn_system = QPushButton("系统播放器", bar)
        self.btn_system.setFixedHeight(28)
        self.btn_system.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.btn_play)
        row.addWidget(self.lbl_time)
        row.addStretch(1)
        row.addWidget(self.rate_combo)
        row.addWidget(self.vol_label)
        row.addWidget(self.vol_slider)
        row.addWidget(self.btn_mute)
        row.addWidget(self.btn_system)
        bar_layout.addLayout(row)
        layout.addWidget(bar)

        # -- 信号 ---------------------------------------------------------
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.playbackStateChanged.connect(self._on_state_changed)
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.vol_slider.valueChanged.connect(self._on_volume_changed)
        self.rate_combo.currentIndexChanged.connect(self._on_rate_changed)
        self.btn_system.clicked.connect(self._open_system)

        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        self.setStyleSheet(self._QSS)

    # -- 槽 -------------------------------------------------------------- #
    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _toggle_mute(self):
        self.audio_out.setMuted(not self.audio_out.isMuted())
        self.btn_mute.setText("取消静音" if self.audio_out.isMuted() else "静音")

    def _on_volume_changed(self, value):
        # 和主流播放器一致：拖动音量条时自动取消静音，避免"滑了没声音"的困惑
        if self.audio_out.isMuted():
            self.audio_out.setMuted(False)
            self.btn_mute.setText("静音")
        self.audio_out.setVolume(max(0.0, min(1.0, value / 100.0)))

    def _on_rate_changed(self):
        try:
            rate = float(self.rate_combo.currentText().rstrip("x"))
        except (ValueError, AttributeError):
            rate = 1.0
        self.player.setPlaybackRate(rate)

    def _open_system(self):
        try:
            if not os.path.isfile(self._path):
                return
            os.startfile(self._path)
        except Exception:
            pass

    def _on_slider_pressed(self):
        self._seeking = True

    def _on_slider_released(self):
        self._seeking = False
        self.player.setPosition(self.slider.value())

    def _on_position_changed(self, position):
        if self._seeking:
            return
        if self.player.duration() > 0:
            self.slider.setValue(position)
        self.lbl_time.setText("{0} / {1}".format(
            format_duration(position), format_duration(self.player.duration())))

    def _on_duration_changed(self, duration):
        self.slider.setRange(0, duration)

    def _on_state_changed(self, state):
        if state == QMediaPlayer.PlayingState:
            self.btn_play.setText("暂停")
        elif state == QMediaPlayer.StoppedState:
            self.btn_play.setText("播放")
        else:
            self.btn_play.setText("播放")

    def closeEvent(self, event):
        self.player.stop()
        self.player.setSource(QUrl())
        super().closeEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("{0} v{1}".format(APP_NAME, APP_VERSION))
        # 多尺寸 ico：标题栏、任务栏、Alt-Tab 各取最合适的一档
        self.setWindowIcon(app_icon())
        # 默认尺寸同时受屏幕可用区域约束，1366×768 的笔记本上不会开到屏幕外
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            width = min(1400, max(1280, avail.width() - 40))
            height = min(880, max(720, avail.height() - 60))
        else:
            width, height = 1400, 880
        self.resize(width, height)
        self.setMinimumSize(min(1280, width), min(720, height))
        self.setAcceptDrops(True)

        self.settings = QSettings("DoubaoVideoTools", "VideoCollector")

        # -- 主题（先于界面构建，图标取色才正确） ---------------------------
        saved_theme = self.settings.value("theme", "light", type=str)
        self._theme_name = saved_theme if saved_theme in ("light", "dark") else "light"
        theme.set_palette(self._theme_name)

        self.worker = None
        self._delete_worker = None
        self._delete_result = None
        self._delete_pairs = None
        self._undo_worker = None
        self.rows = {}              # uid -> {path, name, size, status, target, deleted}
        self._uid_seq = 0
        self._closing = False
        self._current_uid = None    # 当前已载入播放器的行
        self._media_loaded = False
        self._seeking = False

        self._index_uids = {}
        self._uid_rows = {}
        self._uid_rows_valid = False

        # 统计胶囊数据
        self._stat = {
            "total": 0, "success": 0, "skipped": 0, "failed": 0,
            "inplace": 0, "deleted": 0, "bytes": 0, "elapsed": 0.0,
        }
        self._counted_uids = set()
        self._deleted_uids = set()
        self._undo_snapshot = None
        self._task_started = 0.0
        self._last_progress = (0, 0)

        self._thumb_worker = None
        # 缩略图懒加载状态：是否启用、已处理过的 uid、当前批次正在解码的 uid
        self._thumb_active = False
        self._thumb_done = set()
        self._thumb_running = set()
        self._thumb_loaded = set()   # 已换成真实封面的 uid（换主题时不能覆盖）
        self._log_count = 0
        self._log_collapsed = False
        self._log_height = 164
        self._log_drag = None
        self._preview_hidden = False
        self._popup_windows = []

        # 状态筛选：None 表示不过滤，取值与 widgets.status_kind 一致
        self._status_filter = None
        self._visible_rows = 0
        self._status_chips = {}
        self._auto_open_dst = False
        # 最近使用目录（源 / 目标各自最多 6 条）
        self._recent_src = []
        self._recent_dst = []
        # 全盘搜索：表格当前是否为「只读搜索结果」，以及上次勾选的扫描参数
        self._scan_mode = False
        self._scan_roots = []
        self._scan_excludes = ""
        self._scan_max = DEFAULT_MAX_RESULTS
        # 列表批量插入缓冲：worker 逐条发信号，这里攒一小批再一次性插行
        self._pending_items = []
        self._flush_scheduled = False
        self._log_pending = []

        # 主题切换时需要重设的资源
        self._themed_assets = []
        # 工具条是否处于「窄窗口收纳」状态；None 表示还没测量过
        self._toolbar_compact = None

        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(1000)
        self._stats_timer.timeout.connect(self._update_stats)

        # 状态回写的 UI 刷新合并器：上万文件时 worker 每个文件都发一次 item_status，
        # 若每次都重算胶囊/状态栏，界面会被拖垮。这里最多 ~80ms 刷一次。
        self._status_ui_timer = QTimer(self)
        self._status_ui_timer.setSingleShot(True)
        self._status_ui_timer.setInterval(80)
        self._status_ui_timer.timeout.connect(self._flush_status_ui)

        # 筛选输入防抖：每敲一个字就对全表 setRowHidden 一次，行多时明显卡顿
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(110)
        self._filter_timer.timeout.connect(self._apply_filter)

        # 日志合并写入
        self._log_timer = QTimer(self)
        self._log_timer.setSingleShot(True)
        self._log_timer.setInterval(120)
        self._log_timer.timeout.connect(self._flush_log)

        # 缩略图懒加载滚动防抖
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(120)
        self._thumb_timer.timeout.connect(self._pump_thumbnails)

        # 整理设置向导：侧栏只保留只读摘要，真正的控件都在这里（只 hide 不销毁）
        self.setup_dialog = SetupDialog(self)
        self.setup_dialog.closed.connect(self._on_setup_closed)
        self.setup_dialog.search_mode_changed.connect(self._on_search_mode_changed)
        self._search_mode = "dir"
        self._setup_done = False
        self._scan_label = "全盘搜索"
        self._alias_setting_widgets()

        self._build_ui()
        self._toasts = ToastManager(self)
        self._apply_theme(self._theme_name, persist=False)
        self._load_settings()
        self._update_actions()
        self._update_status_bar()
        self._update_stats()
        # 首次启动（设置还没确认过）自动弹一次向导，与 C++ 的 maybeFirstSetup 对齐
        if not self._setup_done:
            QTimer.singleShot(320, self.open_setup)
        # 启动后延迟一次缓存巡检：上次会话可能留下几百 MB 首帧缓存
        QTimer.singleShot(2500, self._prune_thumb_cache)

    def _alias_setting_widgets(self):
        """把向导里的控件挂到主窗口原有属性名上。

        下游（start_search / collect_selected / worker 传参 / QSettings 读写 /
        运行态禁用）全部沿用旧名字，配置也就只有向导这一份，不会两处各存一套。
        """
        dialog = self.setup_dialog
        self.src_edit = dialog.src_edit
        self.src_group = dialog.src_group
        self.btn_src = dialog.btn_src
        self.btn_src_history = dialog.btn_src_history
        self.btn_swap_dirs = dialog.btn_swap_dirs
        self.dst_edit = dialog.dst_edit
        self.dst_group = dialog.dst_group
        self.btn_dst = dialog.btn_dst
        self.btn_dst_history = dialog.btn_dst_history
        self.ext_edit = dialog.ext_picker
        self.ext_group = dialog.ext_picker
        self.min_size = dialog.min_size
        self.max_size = dialog.max_size
        self.min_size_group = dialog.min_size_group
        self.max_size_group = dialog.max_size_group
        self.mode_seg = dialog.mode_seg
        self.conflict_seg = dialog.conflict_seg
        self.sw_recursive = dialog.sw_recursive
        self.sw_hidden = dialog.sw_hidden
        self.sw_dedupe = dialog.sw_dedupe
        self.sw_auto_open = dialog.sw_auto_open

    # =================================================================== #
    # 界面搭建
    # =================================================================== #
    def _build_ui(self):
        central = BackgroundWidget(self)
        central.setObjectName("AppRoot")
        self.setCentralWidget(central)
        self._central = central

        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar(central))

        self.main_splitter = QSplitter(Qt.Horizontal, central)
        # 手柄宽度 0 会被 Qt 当成"用样式默认值"，实际落回 5px 并用系统浅色绘制
        # （深色主题下就是一条刺眼的白线）。这里固定 1px，颜色由 QSS 的
        # QSplitter::handle 跟随主题，正好当预览面板的左边线。
        self.main_splitter.setHandleWidth(1)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_area = self._build_main_area(self.main_splitter)
        self.preview_panel = self._build_preview_panel(self.main_splitter)
        self.main_splitter.addWidget(self.main_area)
        self.main_splitter.addWidget(self.preview_panel)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 0)
        self.main_splitter.setSizes([900, 344])
        root.addWidget(self.main_splitter, 1)

        self._wire_actions()

    # ------------------------------------------------------------------ #
    # 左侧栏
    # ------------------------------------------------------------------ #
    # 侧栏摘要卡的行顺序：搜索 / 目录 / 操作 / 重名 / 开关
    SUMMARY_ROWS = (
        ("search", "搜索"),
        ("dir", "目录"),
        ("mode", "操作"),
        ("conflict", "重名"),
        ("options", "开关"),
    )

    def _build_sidebar(self, parent):
        sidebar = QFrame(parent)
        sidebar.setObjectName("Sidebar")
        sidebar.setAttribute(Qt.WA_StyledBackground, True)
        sidebar.setFixedWidth(320)

        outer = QVBoxLayout(sidebar)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)

        # -- 品牌 ---------------------------------------------------------
        brand_row = QHBoxLayout()
        brand_row.setSpacing(12)
        logo = LogoBadge(42, sidebar)
        brand_row.addWidget(logo)

        brand_text = QVBoxLayout()
        brand_text.setContentsMargins(0, 0, 0, 0)
        brand_text.setSpacing(2)
        title = QLabel("视频集中整理", sidebar)
        title.setObjectName("BrandTitle")
        subtitle = QLabel("Video Collector · PySide6", sidebar)
        subtitle.setObjectName("BrandSub")
        brand_text.addWidget(title)
        brand_text.addWidget(subtitle)
        brand_row.addLayout(brand_text, 1)
        outer.addLayout(brand_row)

        # -- 可滚动区：当前设置摘要 / 外观 / 小提示 -----------------------
        scroll = QScrollArea(sidebar)
        scroll.setObjectName("SidebarScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("background: transparent;")

        body = QWidget()
        body.setObjectName("ScrollBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 2, 0)
        body_layout.setSpacing(10)
        body_layout.addWidget(self._build_summary_card(body))
        body_layout.addWidget(self._build_appearance_card(body))
        body_layout.addWidget(self._build_side_note(body))
        body_layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # -- 底部主操作（固定可见） ---------------------------------------
        footer = QVBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)

        # 三键流程：开始搜索（只读）→ 勾选 → 整理选中（真正写盘）
        main_row = QHBoxLayout()
        main_row.setContentsMargins(0, 0, 0, 0)
        main_row.setSpacing(8)
        self.btn_start = QPushButton("开始搜索", sidebar)
        self.btn_start.setProperty("role", "primary")
        self.btn_start.setFixedHeight(46)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setToolTip("只读扫描，列出符合条件的视频，不会移动或删除任何文件")
        self._themed_icon(self.btn_start, "search", "text_on_accent", 16)

        self.btn_collect = QPushButton("整理选中", sidebar)
        self.btn_collect.setProperty("role", "primary")
        self.btn_collect.setFixedHeight(46)
        self.btn_collect.setCursor(Qt.PointingHandCursor)
        self.btn_collect.setEnabled(False)
        self.btn_collect.setToolTip("把列表里勾选的视频复制 / 移动到目标文件夹")
        self._themed_icon(self.btn_collect, "layers", "text_on_accent", 16)

        main_row.addWidget(self.btn_start, 1)
        main_row.addWidget(self.btn_collect, 1)
        footer.addLayout(main_row)

        self.btn_cancel = QPushButton("取消任务", sidebar)
        self.btn_cancel.setProperty("role", "ghost")
        self.btn_cancel.setFixedHeight(34)
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.setEnabled(False)
        footer.addWidget(self.btn_cancel)

        self.lbl_status = QLabel("共 0 个视频", sidebar)
        self.lbl_status.setProperty("role", "muted")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        footer.addWidget(self.lbl_status)
        outer.addLayout(footer)
        return sidebar

    def _summary_card(self, parent):
        return Card(None, parent, spacing=7, margins=(13, 11, 13, 11), variant="card")

    def _build_summary_card(self, parent):
        """「当前设置」只读卡：五条摘要 + 右上角「修改」入口。

        真正的控件都在 SetupDialog 里，这里只做渲染，保证只有一份配置。
        """
        card = self._summary_card(parent)
        head = QWidget(card)
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(0, 0, 0, 0)
        head_layout.setSpacing(6)
        head_label = QLabel("当前设置", head)
        head_label.setObjectName("CardTitle")
        head_layout.addWidget(head_label)
        head_layout.addStretch(1)
        self.btn_setup = QPushButton("修改", head)
        self.btn_setup.setProperty("role", "link")
        self.btn_setup.setCursor(Qt.PointingHandCursor)
        self.btn_setup.setToolTip("打开四步向导，修改搜索方式、格式、体积与其它选项")
        head_layout.addWidget(self.btn_setup)
        card.add(head)

        self.sum_rows = {}
        for key, label_text in self.SUMMARY_ROWS:
            row = QWidget(card)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            name = QLabel(label_text, row)
            name.setProperty("role", "hint")
            name.setFixedWidth(30)
            value = ElideLabel("—", row)
            row_layout.addWidget(name)
            row_layout.addWidget(value, 1)
            card.add(row)
            self.sum_rows[key] = value
        return card

    def _build_appearance_card(self, parent):
        """外观卡：界面主题 / 预览方式。这两项不进向导，常驻侧栏（与 C++ 一致）。"""
        card = self._summary_card(parent)
        head = QLabel("外观", card)
        head.setObjectName("CardTitle")
        card.add(head)

        theme_row = QWidget(card)
        theme_layout = QHBoxLayout(theme_row)
        theme_layout.setContentsMargins(0, 0, 0, 0)
        theme_layout.setSpacing(8)
        theme_label = QLabel("界面主题", theme_row)
        theme_label.setProperty("role", "field-label")
        theme_label.setFixedWidth(52)
        self.theme_seg = SegmentedControl([
            {"value": "dark", "title": "深色", "icon": "moon"},
            {"value": "light", "title": "浅色", "icon": "sun"},
        ], theme_row, compact=True)
        theme_layout.addWidget(theme_label)
        theme_layout.addWidget(self.theme_seg, 1)
        card.add(theme_row)

        preview_row = QWidget(card)
        preview_layout = QHBoxLayout(preview_row)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(8)
        preview_label = QLabel("预览方式", preview_row)
        preview_label.setProperty("role", "field-label")
        preview_label.setFixedWidth(52)
        self.preview_mode_seg = SegmentedControl([
            {"value": "panel", "title": "侧栏内", "icon": "panel"},
            {"value": "popup", "title": "独立窗口", "icon": "popout"},
        ], preview_row, compact=True)
        self.preview_mode_seg.changed.connect(self._on_preview_mode_changed)
        preview_layout.addWidget(preview_label)
        preview_layout.addWidget(self.preview_mode_seg, 1)
        card.add(preview_row)
        return card

    def _build_side_note(self, parent):
        """小提示：把「搜索只读 / 整理才写盘」这条最重要的约定说清楚。"""
        card = self._summary_card(parent)
        note = QLabel(
            "· 「开始搜索」只读列出视频，不改动任何文件\n"
            "· 勾选后点「整理选中」，确认后才复制 / 移动", card)
        note.setProperty("role", "hint")
        note.setWordWrap(True)
        card.add(note)
        return card


    def _field_label(self, parent, text, hint=None):
        row = QWidget(parent)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(8)
        label = QLabel(text, row)
        label.setProperty("role", "field-label")
        layout.addWidget(label)
        if hint:
            hint_label = QLabel(hint, row)
            hint_label.setProperty("role", "hint")
            layout.addWidget(hint_label)
        layout.addStretch(1)
        return row

    # ------------------------------------------------------------------ #
    def _build_main_area(self, parent):
        main = QWidget(parent)
        main.setObjectName("MainArea")
        layout = QVBoxLayout(main)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)

        layout.addWidget(self._build_topbar(main))
        self.stats_row = self._build_stats_row(main)
        layout.addWidget(self.stats_row)
        layout.addWidget(self._build_progress(main))
        table_card = self._build_table_card(main)
        layout.addWidget(table_card, 1)
        log_card = self._build_log_card(main)
        layout.addWidget(log_card)
        return main

    def _build_topbar(self, parent):
        topbar = QWidget(parent)
        topbar.setObjectName("TopBar")
        layout = QVBoxLayout(topbar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title = QLabel("任务列表", topbar)
        title.setProperty("role", "title")
        self.badge = Badge("0", topbar)
        self.dot_running = PulseDot(topbar)
        self.lbl_running = QLabel("处理中", topbar)
        self.lbl_running.setStyleSheet(
            "color: %s; font-size: 11.5px;" % COLORS["accent_2"])
        self.run_indicator = QWidget(topbar)
        run_layout = QHBoxLayout(self.run_indicator)
        run_layout.setContentsMargins(0, 0, 0, 0)
        run_layout.setSpacing(6)
        run_layout.addWidget(self.dot_running)
        run_layout.addWidget(self.lbl_running)
        self.run_indicator.hide()

        title_row.addWidget(title)
        title_row.addWidget(self.badge)
        title_row.addWidget(self.run_indicator)
        title_row.addStretch(1)
        layout.addLayout(title_row)

        # -- 工具条 -------------------------------------------------------
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        search_box = QFrame(topbar)
        search_box.setObjectName("SearchBox")
        search_box.setAttribute(Qt.WA_StyledBackground, True)
        search_box.setFixedHeight(36)
        search_box.setMinimumWidth(210)
        search_box.setMaximumWidth(380)
        search_box.setToolTip("按 Ctrl+F 聚焦筛选框")
        search_box.installEventFilter(self)
        self.search_frame = search_box
        search_layout = QHBoxLayout(search_box)
        search_layout.setContentsMargins(10, 0, 9, 0)
        search_layout.setSpacing(8)

        search_icon = QLabel(search_box)
        search_icon.setFixedSize(15, 15)
        self._themed_pixmap(search_icon, "search", "text_mute", 15)
        self.filter_edit = QLineEdit(search_box)
        self.filter_edit.setPlaceholderText("筛选文件名 / 路径")
        self.filter_edit.installEventFilter(self)
        # 快捷键提示只在"空且未聚焦"时显示：不然它既像按钮（点了没反应）又占地方
        self.search_kbd = QLabel("Ctrl+F", search_box)
        self.search_kbd.setObjectName("Kbd")
        search_layout.addWidget(search_icon)
        search_layout.addWidget(self.filter_edit, 1)
        # 徽章要垂直居中、按自身高度显示；否则 QLabel 会被拉满 36px，
        # 上下边缘贴着输入框描边，看着像和搜索框叠在一起
        search_layout.addWidget(self.search_kbd, 0, Qt.AlignVCenter)
        self._sync_search_hint()
        toolbar.addWidget(search_box, 0)
        toolbar.addStretch(1)

        self.btn_delete = QPushButton("删除选中", topbar)
        self.btn_delete.setProperty("role", "danger")
        self.btn_delete.setFixedHeight(36)
        self.btn_delete.setCursor(Qt.PointingHandCursor)
        self.btn_delete.setEnabled(False)
        self.btn_delete.setToolTip("把选中的视频移入回收站（Delete 键同效）")
        self._themed_icon(self.btn_delete, "trash", "danger", 15)

        self.btn_open = QPushButton("打开目标", topbar)
        self.btn_open.setFixedHeight(36)
        self.btn_open.setCursor(Qt.PointingHandCursor)
        self._themed_icon(self.btn_open, "folder_open", "text_dim", 15)
        self.btn_open.setToolTip("打开目标文件夹")

        self.btn_more = QPushButton("更多", topbar)
        self.btn_more.setFixedHeight(36)
        self.btn_more.setCursor(Qt.PointingHandCursor)
        self.btn_more.setToolTip("导出报告 / 撤销本次整理")
        self._themed_icon(self.btn_more, "more", "text_dim", 15)

        # 次要操作收进下拉菜单：7 个按钮并排时，主区域在 1280px 窗口下装不下，
        # 搜索框会被压到下限、右侧按钮直接被裁掉（和 C++ 版同一个问题）。
        self.more_menu = QMenu(self.btn_more)
        self.more_menu.setToolTipsVisible(True)
        # 窗口窄时「删除选中 / 打开目标」会从工具条收进这里（见 _fit_toolbar），
        # 避免搜索框被挤到下限、右侧按钮被裁掉。
        self.act_delete = self.more_menu.addAction("删除选中")
        self.act_delete.setToolTip("把选中的视频移入回收站（Delete 键同效）")
        self.act_open = self.more_menu.addAction("打开目标")
        self.act_open.setToolTip("打开目标文件夹")
        self.more_menu.addSeparator()
        self.act_report = self.more_menu.addAction("导出 CSV")
        self.act_report.setToolTip("把当前列表导出为 CSV 报告")
        self.act_report_html = self.more_menu.addAction("导出报告")
        self.act_report_html.setToolTip("把当前列表导出为可双击打开的 HTML 报告")
        self.more_menu.addSeparator()
        self.act_undo = self.more_menu.addAction("撤销本次整理")
        self.act_undo.setToolTip("删除本次生成的副本，移动的文件移回源位置")
        self.act_report.triggered.connect(self.export_report)
        self.act_report_html.triggered.connect(self.export_report_html)
        self.act_undo.triggered.connect(self.undo_last)
        self.more_menu.addSeparator()
        # 缩略图缓存是"只增不减"的，给一个手动清空入口，顺带显示当前占用
        self.act_clear_cache = self.more_menu.addAction("清空缩略图缓存")
        self.act_clear_cache.setToolTip(
            "删除 ~/.videocollector/thumbnails 下的首帧缓存；只删缓存，不动视频")
        self.act_clear_cache.triggered.connect(self.clear_thumb_cache)
        self.act_delete.triggered.connect(self.delete_selected)
        self.act_open.triggered.connect(self.open_target)
        # 默认按宽窗口算：这两个动作平时不在菜单里，窄到装不下时 _fit_toolbar 再放进来
        self.act_delete.setVisible(False)
        self.act_open.setVisible(False)
        self.btn_more.clicked.connect(self._show_more_menu)

        self.btn_help = IconButton("help", "text_dim", 16, 36, topbar,
                                   "快捷键说明（F1）")

        self.btn_toggle_preview = IconButton("panel", "text_dim", 16, 36, topbar, "收起 / 展开预览面板")

        toolbar.addWidget(self.btn_delete)
        toolbar.addWidget(self.btn_open)
        toolbar.addWidget(self.btn_more)
        toolbar.addWidget(self.btn_help)
        toolbar.addWidget(self.btn_toggle_preview)
        for button in (self.btn_delete, self.btn_open, self.btn_more):
            button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout.addLayout(toolbar)
        return topbar

    def _show_more_menu(self):
        """弹出「更多」菜单（打开前刷新缓存占用，数字才不是上次的）。"""
        try:
            size = self._thumb_cache().size_bytes()
            self.act_clear_cache.setText(
                "清空缩略图缓存（{0}）".format(human_size(size)) if size
                else "清空缩略图缓存（空）")
        except Exception:  # noqa: BLE001
            self.act_clear_cache.setText("清空缩略图缓存")
        self.more_menu.exec(self.btn_more.mapToGlobal(
            QPoint(0, self.btn_more.height() + 4)))

    def _build_stats_row(self, parent):
        # 胶囊自带宽度，窗口压窄时排不下会换行，不会被裁掉半截
        row = FlowRow(parent, h_spacing=8, v_spacing=8)
        row.setObjectName("StatsRow")

        # 胶囊同时充当状态筛选按钮：key 与 widgets.status_kind 的返回值一致
        specs = [
            ("total", "全部", "", "0"),
            ("success", "成功", "ok", "0"),
            ("fail", "失败", "fail", "0"),
            ("skip", "跳过", "warn", "0"),
            ("inplace", "已在目标", "gray", "0"),
            ("deleted", "已删除", "gray", "0"),
            ("pending", "待处理", "pending", "0"),
        ]
        for key, caption, kind, value in specs:
            chip = Chip(caption, kind, value, row, checkable=True)
            chip.clicked.connect(lambda k=key: self._set_status_filter(k))
            self._status_chips[key] = chip
            setattr(self, "chip_" + key, chip)
            row.add(chip)
        self.chip_deleted.hide()
        self.chip_pending.hide()
        self.chip_size = Chip("总体积", "", "0 B", row)
        row.add(self.chip_size)
        row.refresh()
        return row

    def _build_progress(self, parent):
        host = QWidget(parent)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        info = QHBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        self.lbl_progress = QLabel("就绪", host)
        self.lbl_progress.setProperty("role", "muted")
        self.lbl_pct = QLabel("—", host)
        self.lbl_pct.setProperty("role", "muted")
        self.lbl_pct.setFont(QFont("Consolas", 10))
        info.addWidget(self.lbl_progress)
        info.addStretch(1)
        info.addWidget(self.lbl_pct)

        self.progress = ProgressTrack(host)
        layout.addLayout(info)
        layout.addWidget(self.progress)
        self.progress_host = host
        return host

    def _build_table_card(self, parent):
        card = QFrame(parent)
        card.setObjectName("TableCard")
        card.setAttribute(Qt.WA_StyledBackground, True)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        # Model/View：数据留在 self.rows 里，模型只做取数、排序与筛选。
        # 两万行时 QTableWidget 会创建十几万个 item 对象，排序/筛选都卡在控件层。
        self.model = VideoTableModel(self.rows, self)
        self.proxy = VideoFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self.table = QTableView(card)
        self.table.setModel(self.proxy)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(THUMB_ROW_H)
        self.table.setIconSize(QSize(16, 16))
        self.table.setFocusPolicy(Qt.StrongFocus)
        self.table.setMouseTracking(True)
        self.table.viewport().setAttribute(Qt.WA_Hover, True)
        self.table.setItemDelegate(
            StatusPillDelegate(COL_STATUS, COL_THUMB, COL_CHECK, self.table))
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        # 缩略图按可视行懒加载：滚动停下来再补一批，避免上万行一次性解码
        self.table.verticalScrollBar().valueChanged.connect(self._on_thumb_scroll)

        # 表头第 0 列换成自绘全选框：与 C++ 版 #selectAll 同一语义
        header = CheckHeaderView(Qt.Horizontal, self.table, COL_CHECK)
        self.table.setHorizontalHeader(header)
        header.setSectionsClickable(True)
        # 表头统一左对齐，和单元格文字同一条基线，读数才不跳
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setMinimumSectionSize(30)
        # 默认按序号升序排（Qt 默认会落到降序，行一多就从大到小，读着别扭）
        header.setSortIndicatorShown(True)
        header.setSortIndicator(COL_SEQ, Qt.AscendingOrder)
        header.setSectionResizeMode(COL_CHECK, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_SEQ, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_THUMB, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_SIZE, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.Fixed)
        header.setSectionResizeMode(COL_DIR, QHeaderView.Fixed)
        # 列宽：勾选 40 / 序号 60（「序号」两个字 + 排序箭头）/
        # 大小 92 才放得下「822.1 MB」这种带空格的完整值 /
        # 状态 108 与 C++ 版 minmax(108px,.9fr) 对齐 /
        # 省下来的宽度全部给文件名列
        self.table.setColumnWidth(COL_CHECK, 40)
        self.table.setColumnWidth(COL_SEQ, 60)
        self.table.setColumnWidth(COL_THUMB, THUMB_COL_W)
        self.table.setColumnWidth(COL_SIZE, 92)
        self.table.setColumnWidth(COL_STATUS, 108)
        self.table.setColumnWidth(COL_DIR, 142)

        layout.addWidget(self.table, 1)
        self._build_empty_hint()
        return card

    def _build_log_card(self, parent):
        card = QFrame(parent)
        card.setObjectName("LogCard")
        card.setAttribute(Qt.WA_StyledBackground, True)
        self.log_card = card

        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        head = QWidget(card)
        head.setFixedHeight(38)
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(10, 0, 10, 0)
        head_layout.setSpacing(6)

        self.btn_log_toggle = QPushButton("运行日志", head)
        self.btn_log_toggle.setProperty("role", "ghost")
        self.btn_log_toggle.setCursor(Qt.PointingHandCursor)
        self.btn_log_toggle.setFixedHeight(28)
        self._themed_icon(self.btn_log_toggle, "chevron_down", "text_dim", 13)

        self.lbl_log_count = QLabel("0", head)
        self.lbl_log_count.setStyleSheet(
            "color: %s; font-size: 11px;" % COLORS["text_mute"])

        self.btn_log_clear = QPushButton("清空", head)
        self.btn_log_clear.setProperty("role", "link")
        self.btn_log_clear.setCursor(Qt.PointingHandCursor)

        head_layout.addWidget(self.btn_log_toggle)
        head_layout.addWidget(self.lbl_log_count)
        head_layout.addStretch(1)
        head_layout.addWidget(self.btn_log_clear)

        self.log_view = QPlainTextEdit(card)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setPlaceholderText("运行日志会显示在这里…")
        self.log_view.setMinimumHeight(96)

        layout.addWidget(head)
        # 顶部细手柄：上下拖拽调整日志高度（与 C++ 版 log-resize 对齐）
        self.log_resize = QWidget(card)
        self.log_resize.setObjectName("LogResize")
        self.log_resize.setFixedHeight(7)
        self.log_resize.setCursor(Qt.SizeVerCursor)
        self.log_resize.setToolTip("上下拖拽调整日志高度")
        self.log_resize.installEventFilter(self)
        layout.addWidget(self.log_resize)
        layout.addWidget(self.log_view, 1)
        card.setFixedHeight(self._log_height)
        return card

    # ------------------------------------------------------------------ #
    # 右侧预览
    # ------------------------------------------------------------------ #
    def _build_preview_panel(self, parent):
        panel = QFrame(parent)
        panel.setObjectName("PreviewPanel")
        panel.setAttribute(Qt.WA_StyledBackground, True)
        panel.setMinimumWidth(322)
        panel.setMaximumWidth(420)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(14)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(2)
        head_title = QLabel("视频预览", panel)
        head_title.setProperty("role", "subtitle")
        self.btn_pv_popout = IconButton("popout", "text_dim", 16, 28, panel, "在独立窗口播放")
        self.btn_pv_collapse = IconButton("chevron_right", "text_dim", 16, 28, panel, "收起预览面板")
        head.addWidget(head_title)
        head.addStretch(1)
        head.addWidget(self.btn_pv_popout)
        head.addWidget(self.btn_pv_collapse)
        layout.addLayout(head)

        # -- 画面 ---------------------------------------------------------
        stage = QFrame(panel)
        stage.setObjectName("VideoStage")
        stage.setAttribute(Qt.WA_StyledBackground, True)
        stage.setMinimumHeight(200)
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(1, 1, 1, 1)
        stage_layout.setSpacing(0)

        self.video_stack = QStackedWidget(stage)

        self.video = QVideoWidget(self.video_stack)
        self.video.setStyleSheet("background-color: #05070a;")
        self.video.setAspectRatioMode(Qt.KeepAspectRatio)
        self.video_stack.addWidget(self.video)

        self.video_placeholder = QWidget(self.video_stack)
        self.video_placeholder.setObjectName("VideoPlaceholder")
        ph_layout = QVBoxLayout(self.video_placeholder)
        ph_layout.setContentsMargins(16, 16, 16, 16)
        ph_layout.setSpacing(8)
        ph_icon = QLabel(self.video_placeholder)
        ph_icon.setAlignment(Qt.AlignCenter)
        self._themed_pixmap(ph_icon, "film", "stage_ink", 44)
        self.lbl_ph_title = QLabel("选择一个视频", self.video_placeholder)
        self.lbl_ph_title.setAlignment(Qt.AlignCenter)
        self.lbl_ph_title.setWordWrap(True)
        self.lbl_ph_title.setStyleSheet(
            "color: %s; font-size: 13px; font-weight: 600;" % COLORS["stage_text"])
        self.lbl_ph_sub = QLabel("双击列表中的视频，这里会显示画面", self.video_placeholder)
        self.lbl_ph_sub.setAlignment(Qt.AlignCenter)
        self.lbl_ph_sub.setWordWrap(True)
        self.lbl_ph_sub.setStyleSheet(
            "color: %s; font-size: 12px;" % COLORS["stage_mute"])
        ph_layout.addStretch(1)
        ph_layout.addWidget(ph_icon)
        ph_layout.addWidget(self.lbl_ph_title)
        ph_layout.addWidget(self.lbl_ph_sub)
        ph_layout.addStretch(1)
        self.video_stack.addWidget(self.video_placeholder)
        self.video_stack.setCurrentWidget(self.video_placeholder)
        stage_layout.addWidget(self.video_stack)
        # 画面区吃掉剩余高度：预览面板本来就该以画面为主，底部不留空洞
        layout.addWidget(stage, 1)

        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.video)
        self.audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_out)
        self.audio_out.setVolume(0.6)

        # -- 播放控制 -----------------------------------------------------
        self.slider = QSlider(Qt.Horizontal, panel)
        self.slider.setFixedHeight(16)
        self.slider.setRange(0, 0)
        self.slider.setSingleStep(1000)
        self.slider.setPageStep(10000)
        layout.addWidget(self.slider)

        time_row = QHBoxLayout()
        time_row.setContentsMargins(0, 0, 0, 0)
        time_row.setSpacing(8)
        self.lbl_time = QLabel("00:00 / 00:00", panel)
        self.lbl_time.setProperty("role", "muted")
        self.lbl_time.setFont(QFont("Consolas", 9))
        self.btn_mute = QPushButton("静音", panel)
        self.btn_mute.setFixedHeight(26)
        self.btn_mute.setCursor(Qt.PointingHandCursor)
        self.btn_mute.setEnabled(False)
        volume_label = QLabel("音量", panel)
        volume_label.setProperty("role", "muted")
        self.volume = QSlider(Qt.Horizontal, panel)
        self.volume.setRange(0, 100)
        self.volume.setValue(60)
        self.volume.setFixedWidth(76)
        time_row.addWidget(self.lbl_time)
        time_row.addStretch(1)
        time_row.addWidget(self.btn_mute)
        time_row.addWidget(volume_label)
        time_row.addWidget(self.volume)
        layout.addLayout(time_row)

        # -- 倍速 ----------------------------------------------------------
        rate_row = QHBoxLayout()
        rate_row.setContentsMargins(0, 0, 0, 0)
        rate_row.setSpacing(8)
        rate_label = QLabel("倍速", panel)
        rate_label.setProperty("role", "muted")
        self.rate_combo = ComboBox(panel)
        self.rate_combo.setFixedHeight(28)
        self.rate_combo.addItems(["0.5x", "0.75x", "1x", "1.25x", "1.5x", "2x"])
        self.rate_combo.setCurrentIndex(2)   # 默认 1x
        self.rate_combo.setCursor(Qt.PointingHandCursor)
        self.rate_combo.setEnabled(False)
        rate_row.addWidget(rate_label)
        rate_row.addWidget(self.rate_combo)
        rate_row.addStretch(1)
        layout.addLayout(rate_row)

        play_row = QHBoxLayout()
        play_row.setSpacing(8)
        self.btn_play = QPushButton("播放", panel)
        self.btn_play.setFixedHeight(34)
        self.btn_play.setCursor(Qt.PointingHandCursor)
        self.btn_play.setEnabled(False)
        self._themed_icon(self.btn_play, "play", "accent", 15)
        self.btn_system = QPushButton("系统播放器", panel)
        self.btn_system.setFixedHeight(34)
        self.btn_system.setCursor(Qt.PointingHandCursor)
        self.btn_system.setEnabled(False)
        self._themed_icon(self.btn_system, "computer", "text_dim", 15)
        play_row.addWidget(self.btn_play, 1)
        play_row.addWidget(self.btn_system, 1)
        layout.addLayout(play_row)

        layout.addWidget(Divider(panel))

        # -- 信息 ---------------------------------------------------------
        info_caption = QLabel("文件信息", panel)
        info_caption.setProperty("role", "section")
        layout.addWidget(info_caption)

        self.lbl_pv_name = QLabel("未选择视频", panel)
        self.lbl_pv_name.setObjectName("PvName")
        self.lbl_pv_name.setWordWrap(True)
        layout.addWidget(self.lbl_pv_name)

        self.lbl_pv_meta = AutoHideLabel("", panel)
        self.lbl_pv_meta.setObjectName("PvMeta")
        self.lbl_pv_meta.setWordWrap(True)
        layout.addWidget(self.lbl_pv_meta)

        self.lbl_pv_path = ClickableLabel(panel)
        self.lbl_pv_path.setObjectName("PvPath")
        self.lbl_pv_path.setWordWrap(False)
        self.lbl_pv_path.setToolTip("点击复制完整路径")
        self.lbl_pv_path.clicked = self._copy_current_preview_path
        layout.addWidget(self.lbl_pv_path)

        self.lbl_pv_state = AutoHideLabel("", panel)
        self.lbl_pv_state.setObjectName("PvState")
        self.lbl_pv_state.setWordWrap(True)
        layout.addWidget(self.lbl_pv_state)

        # -- 快捷操作 -----------------------------------------------------
        actions = QGridLayout()
        actions.setContentsMargins(0, 2, 0, 0)
        actions.setHorizontalSpacing(8)
        actions.setVerticalSpacing(8)
        self.btn_pv_folder = QPushButton("所在文件夹", panel)
        self.btn_pv_popout2 = QPushButton("独立窗口", panel)
        self.btn_pv_delete = QPushButton("删除", panel)
        self.btn_pv_delete.setProperty("role", "danger")
        for button in (self.btn_pv_folder, self.btn_pv_popout2, self.btn_pv_delete):
            button.setFixedHeight(32)
            button.setCursor(Qt.PointingHandCursor)
            button.setEnabled(False)
        self._themed_icon(self.btn_pv_folder, "folder", "text_dim", 14)
        self._themed_icon(self.btn_pv_popout2, "popout", "text_dim", 14)
        self._themed_icon(self.btn_pv_delete, "trash", "danger", 14)
        actions.addWidget(self.btn_pv_folder, 0, 0)
        actions.addWidget(self.btn_pv_popout2, 0, 1)
        actions.addWidget(self.btn_pv_delete, 1, 0, 1, 2)
        layout.addLayout(actions)

        tip = QLabel("提示：内置播放器对 MKV / RMVB / AVI 等格式支持有限，"
                     "打不开时可点「系统播放器」。", panel)
        tip.setObjectName("PvTip")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        layout.addStretch(1)
        return panel

    def _build_empty_hint(self):
        """表格为空时覆盖在视口中央的引导层（不拦截鼠标事件）。"""
        self.empty_hint = QWidget(self.table.viewport())
        self.empty_hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout = QVBoxLayout(self.empty_hint)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        icon = EmptyArt(self.empty_hint)
        title = QLabel("还没有视频记录", self.empty_hint)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            "color: %s; font-size: 15px; font-weight: 600;" % COLORS["text"])
        sub = QLabel("点「修改」配置搜索条件，再点「开始搜索」列出视频", self.empty_hint)
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)
        sub.setStyleSheet("color: %s; font-size: 12.5px;" % COLORS["text_mute"])

        layout.addStretch(1)
        layout.addWidget(icon, 0, Qt.AlignHCenter)
        layout.addWidget(title)
        layout.addWidget(sub)
        layout.addStretch(1)

        self.table.viewport().installEventFilter(self)
        self._layout_empty_hint()

    def _layout_empty_hint(self):
        if not hasattr(self, "empty_hint"):
            return
        self.empty_hint.setGeometry(self.table.viewport().rect())

    def _fit_table_columns(self):
        """按可用宽度决定「来源文件夹」是否显示，保证文件名列至少有 180px。

        7 列并排在 1280 档窗口下会把文件名压到只剩几十像素（只能看到
        "Avengers.Endgame...."），而来源文件夹那一列的内容几乎都一样，
        所以窄窗口优先收起它；完整目录仍可从右键/预览面板里看到。
        """
        if not hasattr(self, "table") or self.table.model().columnCount() < 7:
            return
        available = self.table.viewport().width()
        fixed = (self.table.columnWidth(COL_CHECK) + self.table.columnWidth(COL_SEQ)
                 + self.table.columnWidth(COL_THUMB) + self.table.columnWidth(COL_SIZE)
                 + self.table.columnWidth(COL_STATUS))
        source_w = 142
        show_source = available - fixed - source_w >= 140
        if show_source != (not self.table.isColumnHidden(COL_DIR)):
            self.table.setColumnHidden(COL_DIR, not show_source)
        if show_source:
            self.table.setColumnWidth(COL_DIR, source_w)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_preview_width()
        self._fit_toolbar()

    def _fit_preview_width(self):
        """窄窗口优先保证列表可读：预览列从 420 收到 322。

        1280 档窗口下侧栏 320 + 预览 420 只剩 540 给主区，文件名列被压到
        120px 只能显示「旅…」；把预览列收到下限，主区能多出近 100px。
        """
        if not hasattr(self, "preview_panel"):
            return
        limit = 322 if self.width() < 1360 else 420
        if self.preview_panel.maximumWidth() == limit:
            return
        self.preview_panel.setMaximumWidth(limit)
        # 只改最大宽不会重新分配空间，多出来的宽度要手动还给主区，
        # 否则预览列会在右下角留一条空白
        sizes = self.main_splitter.sizes() if hasattr(self, "main_splitter") else []
        if len(sizes) == 2:
            total = sum(sizes)
            self.main_splitter.setSizes([max(total - limit, 240), limit])

    def _fit_toolbar(self):
        """窄窗口把「删除选中 / 打开目标」收进「更多」菜单。

        主区域装不下 5 个按钮时，搜索框会被压到 210px 下限，提示文字变成
        「筛选文件名 / ...」；这里按主区实际宽度决定是否收纳，窗口拉宽自动还原。
        """
        if not hasattr(self, "main_area") or not hasattr(self, "btn_delete"):
            return
        compact = self.main_area.width() < 980
        if compact == self._toolbar_compact:
            return
        self._toolbar_compact = compact
        for button in (self.btn_delete, self.btn_open):
            button.setVisible(not compact)
        for action in (self.act_delete, self.act_open):
            action.setVisible(compact)

    def _update_empty_hint(self):
        # 用源模型计数：筛选到 0 行时不该冒出「还没有视频记录」的空状态
        visible = self.model.rowCount() == 0
        self.empty_hint.setVisible(visible)
        if visible:
            self._layout_empty_hint()
            self.empty_hint.raise_()

    def eventFilter(self, obj, event):
        # 界面是边构建边安装过滤器的：早期事件触发时后面的控件还不存在，
        # 所以每个分支都先取到对象再比较，不能直接 self.xxx。
        log_resize = getattr(self, "log_resize", None)
        if log_resize is not None and obj is log_resize:
            etype = event.type()
            if etype == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._log_drag = (event.globalY(), self.log_card.height())
                return True
            if etype == QEvent.MouseMove and self._log_drag is not None:
                if not (event.buttons() & Qt.LeftButton):
                    self._log_drag = None
                    return False
                start_y, start_h = self._log_drag
                self._set_log_height(start_h + (start_y - event.globalY()))
                return True
            if etype == QEvent.MouseButtonRelease:
                self._log_drag = None
                return True
            return False
        table = getattr(self, "table", None)
        search_frame = getattr(self, "search_frame", None)
        filter_edit = getattr(self, "filter_edit", None)
        if table is not None and obj is table.viewport():
            etype = event.type()
            if etype == QEvent.Resize:
                self._layout_empty_hint()
                self._fit_table_columns()
            elif etype == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                # 勾选列的点击必须抢在视图默认选择逻辑之前处理：
                # 否则第一下会被视图吃掉（先清空再选中），表现为"点一下没反应"
                index = table.indexAt(event.position().toPoint())
                if index.isValid() and index.column() == COL_CHECK:
                    self._toggle_row_check(index)
                    return True
        elif search_frame is not None and obj is search_frame and event.type() == QEvent.MouseButtonPress:
            # 点放大镜/提示/留白也能聚焦输入框（以前只有点输入框本身才生效）
            if filter_edit is not None:
                filter_edit.setFocus(Qt.MouseFocusReason)
            return True
        elif filter_edit is not None and obj is filter_edit:
            if event.type() == QEvent.FocusIn:
                filter_edit.parent().setProperty("focused", "true")
                filter_edit.parent().style().unpolish(filter_edit.parent())
                filter_edit.parent().style().polish(filter_edit.parent())
                self._sync_search_hint()
            elif event.type() == QEvent.FocusOut:
                filter_edit.parent().setProperty("focused", "false")
                filter_edit.parent().style().unpolish(filter_edit.parent())
                filter_edit.parent().style().polish(filter_edit.parent())
                self._sync_search_hint()
        return super().eventFilter(obj, event)

    def _sync_ext_tooltip(self):
        """把当前扩展名清单完整写进提示，单行输入框看不全时也不丢信息。"""
        edit = getattr(self, "ext_edit", None)
        if edit is None:
            return
        raw = edit.text().strip()
        if not raw:
            edit.setToolTip("用逗号分隔，不需要加点。例如：mp4, mkv, avi")
            return
        items = [t.strip().lstrip(".") for t in raw.replace("，", ",").split(",") if t.strip()]
        edit.setToolTip("当前 {0} 个扩展名：{1}".format(len(items), ", ".join(items)))

    def _sync_search_hint(self):
        """搜索框为空且没聚焦时才显示 Ctrl+F 提示。"""
        kbd = getattr(self, "search_kbd", None)
        if kbd is None:
            return
        kbd.setVisible(not self.filter_edit.text() and not self.filter_edit.hasFocus())

    # ------------------------------------------------------------------ #
    # 信号连接
    # ------------------------------------------------------------------ #
    def _wire_actions(self):
        self.btn_src.clicked.connect(self.pick_source)
        self.btn_dst.clicked.connect(self.pick_target)
        # 扩展名清单现在由勾选面板管理，完整清单与条数放在按钮提示里
        self.ext_edit.textChanged.connect(self._sync_ext_tooltip)
        self._sync_ext_tooltip()
        self.btn_start.clicked.connect(self.start_search)
        self.btn_cancel.clicked.connect(self.cancel_job)
        self.btn_setup.clicked.connect(self.open_setup)
        self.btn_collect.clicked.connect(self.collect_selected)
        self.btn_delete.clicked.connect(self.delete_selected)
        self.btn_open.clicked.connect(self.open_target)
        self.btn_help.clicked.connect(self._show_shortcuts)
        self.btn_swap_dirs.clicked.connect(self._swap_dirs)
        self.btn_src_history.clicked.connect(self._show_recent_src)
        self.btn_dst_history.clicked.connect(self._show_recent_dst)
        self.filter_edit.textChanged.connect(self._on_filter_text_changed)

        self.btn_log_toggle.clicked.connect(self._toggle_log)
        self.btn_log_clear.clicked.connect(self._clear_log)
        self.btn_toggle_preview.clicked.connect(self._toggle_preview)
        self.btn_pv_collapse.clicked.connect(self._toggle_preview)
        self.theme_seg.changed.connect(self._on_theme_changed)

        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_system.clicked.connect(self._open_with_system_player)
        self.btn_pv_folder.clicked.connect(self._open_folder_of_selection)
        self.btn_pv_popout.clicked.connect(self._popout_preview)
        self.btn_pv_popout2.clicked.connect(self._popout_preview)
        self.btn_pv_delete.clicked.connect(self.delete_selected)

        # 「当前设置」摘要卡：任何选项变化都即时刷新（控件在向导里，信号照样接）
        self.mode_seg.changed.connect(self._update_settings_summary)
        self.conflict_seg.changed.connect(self._update_settings_summary)
        for _switch in (self.sw_recursive, self.sw_hidden, self.sw_dedupe,
                        self.sw_auto_open):
            _switch.toggled.connect(self._update_settings_summary)
        self.min_size.textChanged.connect(self._update_settings_summary)
        self.max_size.textChanged.connect(self._update_settings_summary)
        self.src_edit.textChanged.connect(self._update_settings_summary)
        self.dst_edit.textChanged.connect(self._update_settings_summary)
        self.ext_edit.textChanged.connect(self._update_settings_summary)
        self._update_settings_summary()

        self.volume.valueChanged.connect(self._on_volume_changed)
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.rate_combo.currentIndexChanged.connect(self._on_rate_changed)
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.playbackStateChanged.connect(self._on_player_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.main_splitter.splitterMoved.connect(lambda *_: self._elide_preview_path())

        self.table.selectionModel().selectionChanged.connect(
            lambda *_args: self._on_selection_changed())
        self.table.doubleClicked.connect(lambda index: self._preview_row(index.row()))
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_menu)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self._on_sort_changed)

        # -- 快捷键 -------------------------------------------------------
        shortcuts = [
            ("Ctrl+F", self._focus_filter, self),
            ("Ctrl+A", self._select_all_rows, self.table),
            ("Ctrl+Shift+A", self._clear_selection, self.table),
            ("Delete", self.delete_selected, self.table),
            ("Return", self._preview_selected, self.table),
            ("Esc", self._on_escape, self),
            ("F1", self._show_shortcuts, self),
        ]
        for sequence, slot, context in shortcuts:
            shortcut = QShortcut(QKeySequence(sequence), context)
            if context is not self:
                shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
            if not hasattr(self, "_shortcuts"):
                self._shortcuts = []
            self._shortcuts.append(shortcut)

    def _focus_filter(self):
        self.filter_edit.setFocus(Qt.ShortcutFocusReason)
        self.filter_edit.selectAll()

    def _select_all_rows(self):
        """只选中当前可见的行：避免筛选状态下误删被隐藏的文件。"""
        if not self.table.isEnabled():
            return
        selection = QItemSelection()
        last_col = self.table.model().columnCount() - 1
        for row in range(self.table.model().rowCount()):
            # 代理模型里出现的行本来就都是筛选后可见的，不用再判断隐藏
            model = self.table.model()
            selection.select(model.index(row, 0), model.index(row, last_col))
        self.table.selectionModel().select(
            selection, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)

    def _clear_selection(self):
        self.table.clearSelection()

    def _on_escape(self):
        # 菜单/模态框弹出时由它们自己处理 Esc，不要顺带清掉筛选或选中
        if QApplication.activePopupWidget() or QApplication.activeModalWidget():
            return
        if self._status_filter or self.filter_edit.text():
            self._status_filter = None
            for chip in self._status_chips.values():
                chip.set_active(False)
            self.filter_edit.clear()
            self._apply_filter()
        else:
            self.table.clearSelection()

    # =================================================================== #
    # 主题
    # =================================================================== #
    def _themed_icon(self, widget, kind, token="text_dim", size=16):
        self._themed_assets.append((widget, kind, token, size, "icon"))
        widget.setIcon(line_icon(kind, COLORS.get(token, token), size))
        widget.setIconSize(QSize(size, size))
        return widget

    def _themed_pixmap(self, label, kind, token="text_dim", size=16):
        self._themed_assets.append((label, kind, token, size, "pixmap"))
        icon = line_icon(kind, COLORS.get(token, token), size)
        label.setPixmap(icon.pixmap(QSize(size, size)))
        return label

    def _on_theme_changed(self, name):
        if name == self._theme_name:
            return
        self._apply_theme(name)

    def _apply_theme(self, name, persist=True):
        self._theme_name = name
        theme.set_palette(name)
        clear_icon_caches()
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.build_qss())

        for widget, kind, token, size, mode in self._themed_assets:
            try:
                color = COLORS.get(token, token)
                if mode == "icon":
                    widget.setIcon(line_icon(kind, color, size))
                else:
                    widget.setPixmap(line_icon(kind, color, size).pixmap(QSize(size, size)))
            except RuntimeError:
                continue

        for widget in self.findChildren(QWidget):
            apply = getattr(widget, "apply_theme", None)
            if callable(apply):
                try:
                    apply()
                except RuntimeError:
                    continue

        # 主题相关的行内样式
        self.lbl_running.setStyleSheet(
            "color: %s; font-size: 11.5px;" % COLORS["accent_2"])
        self.lbl_ph_title.setStyleSheet(
            "color: %s; font-size: 13px; font-weight: 600;" % COLORS["stage_text"])
        self.lbl_ph_sub.setStyleSheet(
            "color: %s; font-size: 12px;" % COLORS["stage_mute"])
        self.lbl_log_count.setStyleSheet(
            "color: %s; font-size: 11px;" % COLORS["text_mute"])
        self.log_resize.setStyleSheet(
            "QWidget#LogResize { background: transparent; border-top: 1px solid %s; }"
            "QWidget#LogResize:hover { background: %s; }"
            % (COLORS["border"], COLORS["accent_soft"]))
        # 行文字色由模型按主题现取，换主题只需通知模型重绘可见区
        self.model.refresh_theme()
        for label in self.empty_hint.findChildren(QLabel):
            if label.text().startswith("还没有"):
                label.setStyleSheet(
                    "color: %s; font-size: 15px; font-weight: 600;" % COLORS["text"])
            elif label.text().startswith("在左侧"):
                label.setStyleSheet(
                    "color: %s; font-size: 12.5px;" % COLORS["text_mute"])

        self.btn_log_toggle.setIcon(
            line_icon("chevron_right" if self._log_collapsed else "chevron_down",
                      COLORS["text_dim"], 13))
        # btn_play 的图标随播放状态变化，主题重绘后要按当前状态恢复
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        self._set_play_button(self.player.playbackState() == QMediaPlayer.PlayingState)
        self.theme_seg.set_value(name)
        # 摘要卡的颜色是行内样式设定的，换主题后必须重刷一遍
        self._update_settings_summary()
        if self.table is not None:
            self.table.viewport().update()
        self.update()
        if persist:
            self.settings.setValue("theme", name)
            self._toasts.show("已切换到{0}主题".format(theme.COLORS["name"]), "info", 1600)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and os.path.isdir(url.toLocalFile()):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.isdir(path):
                self.src_edit.setText(os.path.normpath(path))
                if not self.dst_edit.text().strip():
                    self.dst_edit.setText(os.path.normpath(os.path.join(path, "视频汇总")))
                self._toasts.show("已填入源目录", "success", 1800)
                event.acceptProposedAction()
                return
        event.ignore()

    # =================================================================== #
    # 折叠 / 提示
    # =================================================================== #
    def _toggle_log(self):
        self._set_log_collapsed(not self._log_collapsed)

    def _set_log_collapsed(self, collapsed):
        self._log_collapsed = bool(collapsed)
        self._log_drag = None
        self.log_view.setVisible(not self._log_collapsed)
        self.log_resize.setVisible(not self._log_collapsed)
        self.log_card.setFixedHeight(40 if self._log_collapsed else self._log_height)
        self.btn_log_toggle.setIcon(
            line_icon("chevron_right" if self._log_collapsed else "chevron_down",
                      COLORS["text_dim"], 13))

    def _set_log_height(self, height):
        """限制在 120~420px，折叠状态下只记住数值、不改动当前高度。"""
        self._log_height = max(120, min(420, int(height)))
        if not self._log_collapsed:
            self.log_card.setFixedHeight(self._log_height)

    def _clear_log(self):
        self._log_pending = []
        self._log_timer.stop()
        self.log_view.clear()
        self._log_count = 0
        self.lbl_log_count.setText("0")

    def _toggle_preview(self):
        self._set_preview_hidden(not self._preview_hidden)

    def _on_preview_mode_changed(self, mode):
        # 「独立窗口」模式下右侧预览面板自动收起；切回「侧栏内」恢复展开。
        self._set_preview_hidden(mode == "popup")

    def _set_preview_hidden(self, hidden):
        self._preview_hidden = bool(hidden)
        self.preview_panel.setVisible(not self._preview_hidden)
        self.btn_toggle_preview.setToolTip(
            "展开预览面板" if self._preview_hidden else "收起预览面板")

    def _toast(self, text, kind="info", ms=2600):
        self._toasts.show(text, kind, ms)

    # =================================================================== #
    # 设置读写
    # =================================================================== #
    def _load_settings(self):
        # 只恢复仍然存在的路径：上次如果是临时目录或已拔掉的移动盘，把死路径直接
        # 填进输入框会让人以为"我什么都没选，怎么冒出一串字符"。
        # 源目录必须存在；目标文件夹允许尚未创建（下面会问要不要新建），
        # 但连父目录都不在就没有意义，一并丢掉。
        saved_src = self.settings.value("src", "", type=str) or ""
        saved_dst = self.settings.value("dst", "", type=str) or ""
        if saved_src and not os.path.isdir(saved_src):
            saved_src = ""
        if saved_dst and not os.path.isdir(saved_dst) and not os.path.isdir(
                os.path.dirname(saved_dst)):
            saved_dst = ""
        self.src_edit.setText(saved_src)
        self.dst_edit.setText(saved_dst)
        self.ext_edit.setText(self.settings.value("exts", DEFAULT_EXTS, type=str) or DEFAULT_EXTS)
        self.ext_edit.setCursorPosition(0)
        self.mode_seg.set_value(self.settings.value("mode", "copy", type=str))
        self.conflict_seg.set_value(self.settings.value("conflict", "rename", type=str))
        self.sw_recursive.setChecked(self.settings.value("recursive", True, type=bool))
        self.sw_hidden.setChecked(self.settings.value("skip_hidden", True, type=bool))
        self.sw_dedupe.setChecked(self.settings.value("dedupe", True, type=bool))
        self.min_size.setText(self.settings.value("min_size", "", type=str) or "")
        self.max_size.setText(self.settings.value("max_size", "", type=str) or "")
        self._auto_open_dst = self.settings.value("auto_open_dst", False, type=bool)
        self.sw_auto_open.setChecked(self._auto_open_dst)
        self.preview_mode_seg.set_value(self.settings.value("preview_mode", "panel", type=str))
        self.theme_seg.set_value(self._theme_name)
        # 搜索方式与"向导是否看过"跟着 config 走（localStorage 那套跨启动会丢）
        self._search_mode = "all" if str(
            self.settings.value("search_mode", "dir", type=str)).lower() == "all" else "dir"
        self.setup_dialog.set_search_mode(self._search_mode)
        self._setup_done = self.settings.value("setup_done", False, type=bool)
        self._recent_src = self._read_recent("recent_src")
        self._recent_dst = self._read_recent("recent_dst")
        self._scan_roots = self.settings.value("scan_roots", [], type=list)
        if isinstance(self._scan_roots, str):
            self._scan_roots = [self._scan_roots]
        self._scan_excludes = self.settings.value("scan_excludes", "", type=str)
        try:
            self._scan_max = int(self.settings.value("scan_max", DEFAULT_MAX_RESULTS, type=int))
        except (TypeError, ValueError):
            self._scan_max = DEFAULT_MAX_RESULTS
        try:
            self._log_height = max(120, min(420, int(
                self.settings.value("log_height", 178, type=int))))
        except (TypeError, ValueError):
            self._log_height = 164

        try:
            volume = int(self.settings.value("volume", 60, type=int))
        except (TypeError, ValueError):
            volume = 60
        self.volume.setValue(max(0, min(volume, 100)))

        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        state = self.settings.value("split_main")
        if state:
            self.main_splitter.restoreState(state)
        self._log_collapsed = self.settings.value("log_collapsed", False, type=bool)
        self._set_log_collapsed(self._log_collapsed)
        hidden = self.settings.value("preview_hidden", False, type=bool)
        # 「独立窗口」模式下右侧面板本来就该收起，恢复设置时不要露出空栏
        self._set_preview_hidden(hidden or self.preview_mode_seg.value() == "popup")

    def _save_settings(self):
        self.settings.setValue("src", self.src_edit.text().strip())
        self.settings.setValue("dst", self.dst_edit.text().strip())
        self.settings.setValue("exts", self.ext_edit.text().strip())
        self.settings.setValue("mode", self.mode_seg.value())
        self.settings.setValue("conflict", self.conflict_seg.value())
        self.settings.setValue("recursive", self.sw_recursive.isChecked())
        self.settings.setValue("skip_hidden", self.sw_hidden.isChecked())
        self.settings.setValue("dedupe", self.sw_dedupe.isChecked())
        self.settings.setValue("min_size", self.min_size.text().strip())
        self.settings.setValue("max_size", self.max_size.text().strip())
        self.settings.setValue("auto_open_dst", self.sw_auto_open.isChecked())
        self.settings.setValue("preview_mode", self.preview_mode_seg.value())
        self.settings.setValue("theme", self._theme_name)
        self.settings.setValue("volume", self.volume.value())
        self.settings.setValue("log_height", self._log_height)
        self.settings.setValue("recent_src", self._recent_src)
        self.settings.setValue("recent_dst", self._recent_dst)
        self.settings.setValue("scan_roots", self._scan_roots)
        self.settings.setValue("scan_excludes", self._scan_excludes)
        self.settings.setValue("scan_max", self._scan_max)
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("split_main", self.main_splitter.saveState())
        self.settings.setValue("log_collapsed", self._log_collapsed)
        self.settings.setValue("preview_hidden", self._preview_hidden)
        self.settings.setValue("search_mode", self._search_mode)
        self.settings.setValue("setup_done", bool(self._setup_done))

    # =================================================================== #
    # 实用小功能：状态筛选 / 目录历史 / 交换目录 / 快捷键说明
    # =================================================================== #
    def _set_status_filter(self, key):
        """点击统计胶囊：只看某一种状态；再点同一项或点「全部」取消。"""
        previous = self._status_filter
        wanted = None if key in (None, "total", self._status_filter) else key
        self._status_filter = wanted
        for name, chip in self._status_chips.items():
            chip.set_active(wanted is not None and name == wanted)
        self._apply_filter()
        if wanted is None and previous is not None and key is not None:
            self._toast("已显示全部记录", "info", 1400)

    def _reset_filters(self):
        """开始新一轮任务时彻底清空文字 + 状态筛选（内部调用，不弹提示）。"""
        self._status_filter = None
        for chip in self._status_chips.values():
            chip.set_active(False)
        self.filter_edit.blockSignals(True)
        self.filter_edit.clear()
        self.filter_edit.blockSignals(False)
        self._sync_search_hint()
        self._apply_filter()

    def _worker_busy(self):
        """线程已结束但尚未被 finished 槽清理时，不再阻塞新任务。"""
        if self.worker is None:
            return False
        if self.worker.isRunning():
            return True
        self.worker = None
        return False

    def _read_recent(self, key):
        value = self.settings.value(key, [], type=list)
        if isinstance(value, str):
            value = [value]
        return [str(item) for item in (value or []) if str(item).strip()][:6]

    def _push_recent(self, src, dst):
        """任务真正开始时记录目录；同一路径去重后置顶，最多 6 条。"""
        for key, attr, path in (("recent_src", "_recent_src", src),
                                ("recent_dst", "_recent_dst", dst)):
            path = os.path.normpath(path.strip()) if path and path.strip() else ""
            if not path:
                continue
            items = [p for p in getattr(self, attr) if norm_path(p) != norm_path(path)]
            items.insert(0, path)
            setattr(self, attr, items[:6])
            self.settings.setValue(key, getattr(self, attr))

    def _show_recent_menu(self, which):
        button = self.btn_src_history if which == "src" else self.btn_dst_history
        items = self._recent_src if which == "src" else self._recent_dst
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        if not items:
            empty = menu.addAction("还没有历史记录（开始一次整理后会记住）")
            empty.setEnabled(False)
        else:
            for path in items:
                # 目录已不存在时在菜单里标出来，免得点进去才发现是死路径
                label = path.replace("&", "&&")
                if not os.path.isdir(path):
                    label += "（目录不存在）"
                action = menu.addAction(label)
                action.setToolTip(path)
                action.setData(path)
            menu.addSeparator()
            clear_action = menu.addAction("清空历史记录")
            clear_action.setData("")
        chosen = menu.exec(button.mapToGlobal(QPoint(0, button.height() + 4)))
        if chosen is None or chosen.data() is None:
            return
        value = chosen.data()
        if value == "":
            key = "recent_src" if which == "src" else "recent_dst"
            attr = "_recent_src" if which == "src" else "_recent_dst"
            setattr(self, attr, [])
            self.settings.setValue(key, [])
            self._toast("已清空历史记录", "info", 1400)
            return
        target = self.src_edit if which == "src" else self.dst_edit
        target.setText(value)
        target.setCursorPosition(0)
        self._toast("已填入目录：{0}".format(value), "info", 1800)

    def _show_recent_src(self):
        self._show_recent_menu("src")

    def _show_recent_dst(self):
        self._show_recent_menu("dst")

    def _swap_dirs(self):
        src = self.src_edit.text().strip()
        dst = self.dst_edit.text().strip()
        if not src and not dst:
            self._toast("两个目录都还是空的", "warn", 1800)
            return
        self.src_edit.setText(dst)
        self.dst_edit.setText(src)
        self.src_edit.setCursorPosition(0)
        self.dst_edit.setCursorPosition(0)
        self._toast("已交换源目录与目标文件夹", "success", 1800)

    def _show_shortcuts(self):
        if QApplication.activePopupWidget() or QApplication.activeModalWidget():
            return
        rows = "".join(
            "<tr><td style='padding:3px 18px 3px 0;color:{0};"
            "font-family:Consolas,monospace;white-space:nowrap'>{1}</td>"
            "<td style='padding:3px 0;color:{2}'>{3}</td></tr>".format(
                COLORS["accent"], key, COLORS["text"], desc)
            for key, desc in SHORTCUTS
        )
        tips = "".join("<li style='margin:3px 0;color:{0}'>{1}</li>".format(
            COLORS["text_dim"], tip) for tip in SHORTCUT_TIPS)
        box = QMessageBox(self)
        box.setWindowTitle("快捷键与操作说明")
        box.setTextFormat(Qt.RichText)
        box.setText(
            "<div style='font-size:12px'>"
            "<b style='color:{0}'>键盘快捷键</b>"
            "<table style='margin:8px 0 12px 0'>{1}</table>"
            "<b style='color:{0}'>操作提示</b><ul style='margin:8px 0 0 0'>{2}</ul>"
            "</div>".format(COLORS["text"], rows, tips))
        box.setStandardButtons(QMessageBox.Ok)
        box.button(QMessageBox.Ok).setText("知道了")
        box.exec()

    # =================================================================== #
    # 目录选择
    # =================================================================== #
    def pick_source(self):
        path = QFileDialog.getExistingDirectory(self, "选择源目录", self.src_edit.text() or "")
        if path:
            self.src_edit.setText(os.path.normpath(path))
            if not self.dst_edit.text().strip():
                self.dst_edit.setText(os.path.normpath(os.path.join(path, "视频汇总")))

    def pick_target(self):
        path = QFileDialog.getExistingDirectory(self, "选择目标文件夹", self.dst_edit.text() or "")
        if path:
            self.dst_edit.setText(os.path.normpath(path))

    def open_target(self):
        path = self.dst_edit.text().strip()
        if not path or not os.path.isdir(path):
            self._toast("目标文件夹还不存在", "warn")
            return
        try:
            self._open_path(path)
        except Exception as exc:  # noqa: BLE001
            self._toast("无法打开文件夹：{0}".format(exc), "error")

    @staticmethod
    def _open_path(path):
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    # =================================================================== #
    # 开始 / 取消
    # =================================================================== #
    # =================================================================== #
    # 开始搜索（只读）/ 取消
    # =================================================================== #
    def start_search(self):
        """「开始搜索」：只读列出视频，一个字节都不写。

        与 C++ 版保持一致：指定目录与全盘搜索都只做扫描，勾选后再点
        「整理选中」才真正复制 / 移动。
        """
        if self._worker_busy():
            self._toast("上一轮任务还在收尾，请稍等片刻", "warn")
            return
        exts = self._parse_exts()
        if not exts:
            self._toast("还没有选格式：点「修改」勾选至少一种扩展名", "warn")
            self.open_setup(1)
            return
        min_bytes, max_bytes = self._read_size_limits()
        if min_bytes and max_bytes and min_bytes > max_bytes:
            self._toast("体积范围有误：最小不能大于最大", "warn")
            self.open_setup(1)
            return

        if self._search_mode == "all":
            self.start_scan_all()
            return

        src = self.src_edit.text().strip()
        if not src or not os.path.isdir(src):
            self._toast("还没有可用的源目录：点「修改」设置搜索位置", "warn")
            self.open_setup(0)
            return
        self._start_dir_scan(src, exts, min_bytes, max_bytes)

    def _start_dir_scan(self, src, exts, min_bytes, max_bytes):
        """指定目录的只读扫描：与全盘搜索共用同一套 ScanAllWorker。"""
        dst = self.dst_edit.text().strip()
        self._push_recent(src, dst)
        self._reset_table()
        self._scan_mode = True
        self._scan_label = "扫描"

        # 播放器可能锁着文件，开始前先释放；旧的缩略图线程也不再需要
        self._stop_media()
        self._stop_thumbnails()
        self.progress.set_busy(True)
        self.lbl_progress.setText("正在扫描源目录…")
        self.lbl_pct.setText("—")
        self.append_log("搜索 | 目录：{0} | 只读，不会移动或删除任何文件".format(src))
        if min_bytes or max_bytes:
            self.append_log("体积过滤：{0} – {1}".format(
                self._fmt_mb(min_bytes // (1024 * 1024)) if min_bytes else "不限",
                self._fmt_mb(max_bytes // (1024 * 1024)) if max_bytes else "不限"))
        if not self.sw_recursive.isChecked():
            self.append_log("仅扫描当前目录，不进入子文件夹")

        worker = ScanAllWorker(
            [src], exts, self.sw_hidden.isChecked(), [],
            dst, self._scan_max,
            min_bytes=min_bytes, max_bytes=max_bytes,
            recursive=self.sw_recursive.isChecked(),
        )
        worker.progress.connect(self._on_scan_progress)
        worker.items.connect(self._on_scan_items)
        worker.log.connect(self.append_log)
        worker.finished_stats.connect(self._on_scan_finished)
        worker.finished.connect(self._on_worker_finished)
        self.worker = worker
        worker.start()
        self._set_running(True)
        self._update_status_bar()

    # =================================================================== #
    # 全盘搜索（只读）→ 浏览筛选 → 整理选中项
    # =================================================================== #
    def _parse_exts(self):
        return [e for e in (
            x.strip().lstrip(".").lower()
            for x in self.ext_edit.text().replace(";", ",").split(",")
        ) if e]

    # ----------------------------------------------------------------- #
    # 体积范围 / 选项摘要
    # ----------------------------------------------------------------- #
    @staticmethod
    def _parse_size_limit(text):
        """把输入框里的 MB 数字换成字节；留空 / 0 / 非法输入都当作「不限」(0)。"""
        raw = (text or "").strip().replace("，", "").replace(",", "")
        if not raw:
            return 0
        try:
            mb = float(raw)
        except ValueError:
            return 0
        if mb <= 0:
            return 0
        mb = min(mb, SIZE_LIMIT_MAX_MB)
        return int(mb * 1024 * 1024)

    def _read_size_limits(self):
        return (self._parse_size_limit(self.min_size.text()),
                self._parse_size_limit(self.max_size.text()))

    @staticmethod
    def _fmt_mb(mb):
        if mb >= 1024:
            return "{0:g}GB".format(round(mb / 1024.0, 1) if mb % 1024 else mb / 1024)
        return "{0:g}MB".format(mb)

    def _settings_summary_rows(self):
        """算出「当前设置」卡五行的文本；返回 (文本字典, 需要标红的行集合)。

        侧栏不再放控件，这一屏就是用户开工前唯一的核对入口，
        所以体积范围写错时必须当场红出来，而不是等点了按钮才 toast。
        """
        mode = self.mode_seg.value()
        conflict = self.conflict_seg.value()
        src = self.src_edit.text().strip()
        dst = self.dst_edit.text().strip()
        warn = set()

        search_text = "全盘搜索" if self._search_mode == "all" else "指定目录"
        if self._search_mode == "all":
            dir_text = "→ {0}".format(dst) if dst else "未设置目标"
        elif src and dst:
            dir_text = "{0} → {1}".format(src, dst)
        elif src:
            dir_text = "{0} → 未设置目标".format(src)
        else:
            dir_text = "源目录未设置"
            warn.add("dir")

        option_bits = ["含子文件夹" if self.sw_recursive.isChecked() else "仅当前目录"]
        if self.sw_hidden.isChecked():
            option_bits.append("跳过隐藏")
        if self.sw_dedupe.isChecked():
            option_bits.append("跳过重复")
        if self.sw_auto_open.isChecked():
            option_bits.append("完成后打开")

        min_b, max_b = self._read_size_limits()
        if min_b and max_b and min_b > max_b:
            options_text = "体积范围有误：最小不能大于最大"
            warn.add("options")
        else:
            if min_b or max_b:
                option_bits.append("体积 {0}–{1}".format(
                    self._fmt_mb(min_b // (1024 * 1024)) if min_b else "0",
                    self._fmt_mb(max_b // (1024 * 1024)) if max_b else "不限"))
            options_text = " · ".join(option_bits)

        rows = {
            "search": search_text,
            "dir": dir_text,
            "mode": "移动（原文件移走）" if mode == "move" else "复制（保留原文件）",
            "conflict": "同名跳过（不覆盖）" if conflict == "skip" else "自动重命名",
            "options": options_text,
        }
        return rows, warn

    def _update_settings_summary(self, *_args):
        if not hasattr(self, "sum_rows"):
            return
        rows, warn = self._settings_summary_rows()
        for key, label in self.sum_rows.items():
            text = rows.get(key, "—")
            label.setText(text)
            label.setToolTip(text)
            label.setStyleSheet(
                "color: %s; font-size: 12.5px; font-weight: %s;"
                % (COLORS["danger"] if key in warn else COLORS["text_dim"],
                   "600" if key in warn else "500"))
        self._refresh_collect_button()

    # 旧名字保留：外部/旧调用点仍可能按这个名字刷新摘要
    def _update_plan_summary(self, *_args):
        self._update_settings_summary()

    def _reset_table(self):
        """开启一轮新的只读搜索时清空表格，不复用整理任务的清空逻辑。"""
        self._stop_media()
        self._stop_thumbnails()
        self._reset_filters()
        self.table.setSortingEnabled(False)
        self.model.reset_rows()
        self._update_empty_hint()
        self.rows.clear()
        self._uid_seq = 0
        self._index_uids.clear()
        self._uid_rows.clear()
        self._uid_rows_valid = False
        self._pending_items = []
        self._flush_scheduled = False
        self._counted_uids.clear()
        self._deleted_uids.clear()
        self._stat = {
            "total": 0, "success": 0, "skipped": 0, "failed": 0,
            "inplace": 0, "deleted": 0, "bytes": 0, "elapsed": 0.0,
        }
        self._task_started = time.time()
        self._stats_timer.start()
        self._update_stats()
        self._clear_log()

    def start_scan_all(self):
        if self._worker_busy():
            self._toast("上一轮任务还在收尾，请稍等片刻", "warn")
            return
        exts = self._parse_exts()
        if not exts:
            self._toast("视频扩展名不能为空", "warn")
            return

        dialog = ScanAllDialog(self, self._scan_roots,
                               self._scan_excludes, self._scan_max)
        if dialog.exec() != QDialog.Accepted:
            return
        roots = dialog.selected_roots()
        if not roots:
            self._toast("请至少勾选一个磁盘", "warn")
            return
        self._scan_roots = roots
        self._scan_excludes = dialog.excludes_text()
        self._scan_max = dialog.max_results()
        self.settings.setValue("scan_roots", roots)
        self.settings.setValue("scan_excludes", self._scan_excludes)
        self.settings.setValue("scan_max", self._scan_max)

        self._reset_table()
        self._scan_mode = True
        self._scan_label = "全盘搜索"
        self.progress.set_busy(True)
        self.lbl_progress.setText("正在全盘搜索…")
        self.lbl_pct.setText("—")
        self.append_log(
            "全盘搜索 | 磁盘：{0} | 只读，不会移动或删除任何文件".format("、".join(roots)))
        min_bytes, max_bytes = self._read_size_limits()
        if min_bytes or max_bytes:
            self.append_log("体积过滤：{0} – {1}".format(
                self._fmt_mb(min_bytes // (1024 * 1024)) if min_bytes else "不限",
                self._fmt_mb(max_bytes // (1024 * 1024)) if max_bytes else "不限"))

        worker = ScanAllWorker(
            roots, exts, self.sw_hidden.isChecked(),
            parse_custom_excludes(self._scan_excludes),
            self.dst_edit.text().strip(), self._scan_max,
            min_bytes=min_bytes, max_bytes=max_bytes)
        worker.progress.connect(self._on_scan_progress)
        worker.items.connect(self._on_scan_items)
        worker.log.connect(self.append_log)
        worker.finished_stats.connect(self._on_scan_finished)
        worker.finished.connect(self._on_worker_finished)
        self.worker = worker
        worker.start()
        self._set_running(True)
        self._update_status_bar()

    def _on_scan_progress(self, dirs, found):
        self.lbl_progress.setText(
            "已扫描 {0} 个目录 · 发现 {1} 个视频".format(dirs, found))
        self.lbl_pct.setText("—")
        self._stat["total"] = max(self._stat["total"], found)
        self.chip_total.set_value(found)

    def _on_scan_items(self, batch):
        for index, name, size, dirname, fullpath in batch:
            self.on_item_added(index, name, size, dirname)
            uid = self._index_uids.get(index)
            if uid is not None:
                # 保留扫描线程给出的完整路径（含长路径前缀），供后续整理使用
                self.rows[uid]["path"] = fullpath

    def _on_scan_finished(self, stats):
        self._flush_items()
        self._stats_timer.stop()
        self._set_running(False)
        self.table.setSortingEnabled(True)
        self._uid_rows_valid = False
        self._apply_filter()
        self._stat["total"] = stats.get("total", 0)
        self._update_stats()
        self.progress.set_progress(0, 0)
        self.lbl_pct.setText("—")

        label = getattr(self, "_scan_label", "全盘搜索")
        summary = "{0}{1} | 共 {2} 个视频 · 已扫描 {3} 个目录 · 用时 {4:.1f} 秒".format(
            label, "已取消" if stats.get("cancelled") else "完成",
            stats.get("total", 0), stats.get("dirs", 0), stats.get("seconds", 0.0))
        if stats.get("truncated"):
            summary += "（已达上限，请缩小范围或提高上限）"
        self.append_log(summary)
        self.lbl_progress.setText(
            "{0}{1} · 共 {2} 个视频".format(
                label, "已取消" if stats.get("cancelled") else "完成",
                stats.get("total", 0)))
        if stats.get("error"):
            self._toast(label + "异常：" + stats["error"], "error", 6000)
        elif stats.get("truncated") or stats.get("cancelled"):
            self._toast(summary, "warn", 6000)
        else:
            self._toast(summary, "success", 5000)
        self._update_actions()
        self._update_status_bar()
        # 搜索结果同样按可视行补缩略图（只解码屏幕上看得到的那些）
        if self.table.model().rowCount():
            self._start_thumbnails()

    def collect_selected(self):
        """把搜索列表里勾选的行交给整理链路，统一放到目标文件夹。"""
        if self._worker_busy():
            self._toast("上一轮任务还在收尾，请稍等片刻", "warn")
            return
        if not self._scan_mode:
            self._toast("请先点「开始搜索」列出视频，再勾选要整理的项", "warn")
            return

        selected = set(self._selected_uids())
        pairs = []
        for row in range(self.table.model().rowCount()):
            uid = self._uid_at(row)
            if uid not in selected:
                continue
            path = self.rows.get(uid, {}).get("path", "")
            if path and os.path.isfile(path):
                pairs.append((uid, path))
        if not pairs:
            self._toast("请先勾选要整理的视频（被隐藏的行不会参与）", "warn")
            return
        if len(pairs) < len(selected):
            self._toast("有 {0} 个选中文件已不存在，已跳过".format(len(selected) - len(pairs)),
                        "warn", 3000)

        exts = self._parse_exts()
        if not exts:
            self._toast("视频扩展名不能为空", "warn")
            return
        dst = self.dst_edit.text().strip()
        if not dst:
            self._toast("请先设置目标文件夹", "warn")
            return
        if not os.path.isdir(dst):
            ret = QMessageBox.question(
                self, APP_NAME,
                "目标文件夹还不存在：\n{0}\n\n要现在创建它吗？".format(dst),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if ret != QMessageBox.Yes:
                return
            try:
                os.makedirs(dst, exist_ok=True)
            except OSError as exc:
                QMessageBox.critical(self, APP_NAME, "创建目标文件夹失败：{0}".format(exc))
                return

        mode = self.mode_seg.value()
        if mode == "move":
            ret = QMessageBox.question(
                self, APP_NAME,
                "移动模式会把源文件从原位置移走（相当于剪切），确认开始？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes:
                return

        min_bytes, max_bytes = self._read_size_limits()
        if min_bytes and max_bytes and min_bytes > max_bytes:
            self._toast("体积范围有误：最小不能大于最大", "warn")
            return

        self._stop_media()
        self._stop_thumbnails()
        self._reset_filters()
        self.table.clearSelection()
        self.table.setSortingEnabled(False)
        # 让 worker 的 item_status(index) 精确回写到这些已有的行，而不是新建行
        self._index_uids = {i: uid for i, (uid, _path) in enumerate(pairs)}
        self._uid_rows_valid = False
        file_list = [path for _uid, path in pairs]

        # 保留扫描结果：未勾选的行仍是全盘搜索结果，可继续分批整理
        self._counted_uids.clear()
        self._deleted_uids.clear()
        self._pending_items = []
        self._stat = {
            "total": 0, "success": 0, "skipped": 0, "failed": 0,
            "inplace": 0, "deleted": 0, "bytes": 0, "elapsed": 0.0,
        }
        self._task_started = time.time()
        self._stats_timer.start()
        self._update_stats()
        self._clear_log()
        self.progress.set_busy(False)
        self.lbl_progress.setText("准备整理选中项…")
        self.lbl_pct.setText("—")
        self.append_log(
            "整理选中项 | 共 {0} 个文件 · 模式：{1} · 目标：{2}".format(
                len(file_list), "移动" if mode == "move" else "复制", dst))

        worker = CollectWorker(
            "", dst, exts, mode, self.conflict_seg.value(),
            self.sw_recursive.isChecked(), self.sw_hidden.isChecked(),
            self.sw_dedupe.isChecked(), file_list=file_list,
            min_bytes=min_bytes, max_bytes=max_bytes)
        worker.scan_done.connect(self.on_scan_done)
        worker.item_status.connect(self.on_item_status)
        worker.progress.connect(self.on_progress)
        worker.log.connect(self.append_log)
        worker.done.connect(self.on_done)
        worker.finished.connect(self._on_worker_finished)
        self.worker = worker
        worker.start()
        self._set_running(True)
        self._update_actions()
        self._update_status_bar()

    def cancel_job(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.lbl_progress.setText("正在取消，等待当前文件结束…")
            self.btn_cancel.setEnabled(False)

    # -- 向导 ------------------------------------------------------------ #
    def open_setup(self, index=0):
        """打开四步向导。运行中不允许改配置（对齐 C++ 的 openSetup 拦截）。"""
        if self._worker_busy():
            self._toast("任务进行中，请先取消或等待结束", "warn")
            return
        if isinstance(index, bool):
            index = 0
        self.setup_dialog.open_at(max(0, min(int(index), 3)))

    def _on_setup_closed(self):
        # 关掉向导即视为"看过设置"，下次启动不再自动弹（与 C++ setupDone 一致）
        self._setup_done = True
        self.settings.setValue("setup_done", True)
        self._update_settings_summary()

    def _on_search_mode_changed(self, mode):
        self._search_mode = "all" if mode == "all" else "dir"
        self.settings.setValue("search_mode", self._search_mode)
        self._update_settings_summary()

    def _refresh_collect_button(self):
        """「整理选中」只有在"搜过一次 + 有勾选 + 没在跑"时才亮。"""
        if not hasattr(self, "btn_collect"):
            return
        running = bool(self.worker and self.worker.isRunning())
        has_selection = bool(self._selected_uids())
        ok = not running and self._scan_mode and has_selection
        self.btn_collect.setEnabled(ok)
        self.btn_collect.setToolTip(
            "把列表里勾选的视频复制 / 移动到目标文件夹"
            if ok else ("先点「开始搜索」并把要整理的视频勾上"))

    def _set_running(self, running):
        self.btn_start.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        self.btn_setup.setEnabled(not running)
        self.btn_src.setEnabled(not running)
        self.btn_dst.setEnabled(not running)
        self.ext_edit.setEnabled(not running)
        self.mode_seg.setEnabled(not running)
        self.conflict_seg.setEnabled(not running)
        self.sw_recursive.setEnabled(not running)
        self.sw_hidden.setEnabled(not running)
        self.sw_dedupe.setEnabled(not running)
        self.sw_auto_open.setEnabled(not running)
        self.min_size_group.setEnabled(not running)
        self.max_size_group.setEnabled(not running)
        self.run_indicator.setVisible(running)
        if running:
            # 运行中把向导收起来：里面的控件已经禁用，还开着只会让人以为卡住
            self.setup_dialog.hide()
        if not running:
            self.progress.set_busy(False)
        self._update_actions()

    def _update_actions(self):
        running = bool(self.worker and self.worker.isRunning())
        has_selection = bool(self._selected_uids())
        self.btn_delete.setEnabled(not running and has_selection)
        self.act_delete.setEnabled(not running and has_selection)
        self._refresh_collect_button()
        # 预览类操作不再受任务运行状态限制：整理过程中照样能双击看片，
        # 只保留「删除」在运行中禁用（删的是正在被复制的文件，风险太大）。
        self.btn_play.setEnabled(has_selection)
        self.btn_system.setEnabled(has_selection)
        self.btn_pv_folder.setEnabled(has_selection)
        self.btn_pv_popout.setEnabled(has_selection)
        self.btn_pv_popout2.setEnabled(has_selection)
        self.btn_pv_delete.setEnabled(not running and has_selection)
        can_export = not running and bool(self.rows)
        self.act_report.setEnabled(can_export)
        self.act_report_html.setEnabled(can_export)
        self.act_undo.setEnabled(not running and bool(self._undo_snapshot))

    # =================================================================== #
    # 线程回调
    # =================================================================== #
    def on_scan_count(self, count):
        self.lbl_progress.setText("正在扫描…已发现 {0} 个视频".format(count))
        self._stat["total"] = max(self._stat["total"], count)
        self._update_stats()

    def on_scan_done(self, total):
        self.progress.set_progress(0, max(total, 1))
        self.lbl_progress.setText("共找到 {0} 个视频，开始处理…".format(total))
        self.lbl_pct.setText("0%")
        self._stat["total"] = total
        self._update_stats()
        self.append_log("扫描完成，共找到 {0} 个视频".format(total))

    def on_item_added(self, index, name, size, source_dir):
        self._uid_seq += 1
        uid = self._uid_seq
        self._index_uids[index] = uid
        self.rows[uid] = {
            "path": os.path.join(source_dir, name),
            "name": name,
            "size": size,
            "dir": source_dir,
            "seq": index + 1,
            "status": "排队中",
            "target": "",
            "target_created": False,
            "deleted": False,
        }
        self._pending_items.append((uid, index, name, size, source_dir))
        self._stat["total"] = max(self._stat["total"], len(self.rows))
        if not self._flush_scheduled:
            self._flush_scheduled = True
            QTimer.singleShot(0, self._flush_items)

    def _flush_items(self):
        """把缓冲里的行一次性交给模型，避免逐条触发布局与重绘。"""
        self._flush_scheduled = False
        pending = self._pending_items
        self._pending_items = []
        if not pending:
            return

        uids = [uid for uid, _i, _n, _s, _d in pending]
        self.model.append_uids(uids)

        self._uid_rows_valid = False
        if self.filter_edit.text().strip() or self._status_filter:
            self._apply_filter()
        if self._thumb_active:
            self._thumb_timer.start()
        self._update_empty_hint()
        self._update_stats()
        self._update_status_bar()

    def on_item_status(self, index, status, target=""):
        uid = self._index_uids.get(index)
        if uid is None and 0 <= index < self.table.model().rowCount():
            uid = self._uid_at(index)
        if uid is None:
            return
        self._set_row_status(uid, status, target)

    def on_progress(self, done, total):
        self._last_progress = (done, total)
        self.progress.set_progress(done, total)
        self.lbl_progress.setText("已处理 {0} / {1}".format(done, total))
        pct = 0 if not total else int(done * 100 / total)
        self.lbl_pct.setText("{0}%".format(pct))
        self._schedule_status_ui()

    def append_log(self, text):
        # worker 每个文件都会写一行日志；逐行 appendPlainText + 滚到底在上万行时
        # 会明显拖慢界面，这里先攒起来，120ms 合并写一次。
        self._log_pending.append(text)
        self._log_count += 1
        self.lbl_log_count.setText(str(self._log_count))
        if not self._log_timer.isActive():
            self._log_timer.start()

    def _flush_log(self):
        if not self._log_pending:
            return
        pending = self._log_pending
        self._log_pending = []
        self.log_view.appendPlainText("\n".join(pending))
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def on_done(self, stats):
        if self._closing:
            return
        self._flush_items()
        self._stats_timer.stop()
        self._status_ui_timer.stop()
        self._set_running(False)
        self.table.setSortingEnabled(True)
        self._uid_rows_valid = False
        self._apply_filter()
        done = (stats.get("success", 0) + stats.get("failed", 0)
                + stats.get("skipped", 0) + stats.get("in_place", 0))
        total = max(stats.get("total", 1), 1)
        self.progress.set_progress(done, total)
        self.lbl_pct.setText("{0}%".format(int(done * 100 / total)))
        self._update_actions()
        self._update_status_bar()

        self._stat = {
            "total": stats.get("total", 0),
            "success": stats.get("success", 0),
            "skipped": stats.get("skipped", 0),
            "failed": stats.get("failed", 0),
            "inplace": stats.get("in_place", 0),
            "deleted": sum(1 for record in self.rows.values()
                           if record.get("deleted")),
            "bytes": stats.get("bytes", 0),
            "elapsed": stats.get("seconds", 0.0),
        }
        self._update_stats()

        summary = (
            "完成 | 共 {total} 个视频 · 成功 {success} · 失败 {failed} · "
            "跳过 {skipped} · 无需处理 {in_place} · 共 {size} · 用时 {sec:.1f} 秒"
        ).format(
            total=stats.get("total", 0),
            success=stats.get("success", 0),
            failed=stats.get("failed", 0),
            skipped=stats.get("skipped", 0),
            in_place=stats.get("in_place", 0),
            size=human_size(stats.get("bytes", 0)),
            sec=stats.get("seconds", 0.0),
        )
        if stats.get("cancelled"):
            summary = "已取消 | " + summary
        self.append_log(summary)
        self.lbl_progress.setText("{0} · 用时 {1}".format(
            "已取消" if stats.get("cancelled") else "完成",
            format_duration(int(stats.get("seconds", 0.0) * 1000))))

        # 任务结束后再生成缩略图：避免与复制/移动同一文件抢读
        self._start_thumbnails()

        # 记录本次任务的撤销快照：仅真正生成的副本（target_created），含源/目标/操作类型
        snapshot = []
        for uid, record in self.rows.items():
            if not record.get("target_created"):
                continue
            status = record.get("status", "") or ""
            if status.startswith("已复制") or status.startswith("已移动"):
                snapshot.append({
                    "uid": uid,
                    "path": record.get("path", ""),
                    "target": record.get("target", ""),
                    "mode": "move" if status.startswith("已移动") else "copy",
                })
            elif status.startswith("复制失败") or status.startswith("移动失败"):
                continue
        self._undo_snapshot = snapshot
        if snapshot:
            self.append_log("本次整理产生了 {0} 个可撤销的副本，可点「撤销」一键还原。".format(len(snapshot)))

        if stats.get("error"):
            self._toast("任务异常：" + stats["error"], "error", 6000)
            QMessageBox.critical(self, APP_NAME, summary.replace(" | ", "\n") + "\n\n错误信息：" + stats["error"])
        elif stats.get("cancelled") or stats.get("failed"):
            self._toast(summary, "warn", 6000)
        else:
            self._toast(summary, "success", 5000)

        # 「完成后打开目标」：只在正常完成且有成功项时触发，取消/异常不打扰
        if (self.sw_auto_open.isChecked() and not stats.get("cancelled")
                and not stats.get("error") and stats.get("success", 0) > 0):
            path = self.dst_edit.text().strip()
            if path and os.path.isdir(path):
                try:
                    self._open_path(path)
                except Exception as exc:  # noqa: BLE001
                    self._toast("自动打开目标失败：{0}".format(exc), "warn", 2600)

    def _on_worker_finished(self):
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.deleteLater()
        self._update_actions()

    # =================================================================== #
    # 缩略图
    # =================================================================== #
    def _thumb_cache(self):
        cache = getattr(self, "_thumb_cache_obj", None)
        if cache is None:
            cache = ThumbnailCache()
            self._thumb_cache_obj = cache
        return cache

    def _thumb_cache_limit_mb(self):
        """缓存上限（MB），默认 256；留一个下限 32，避免设置成 0 后每次都全删。"""
        try:
            value = int(self.settings.value(
                "thumb_cache_mb", ThumbnailCache.DEFAULT_LIMIT_MB, type=int))
        except (TypeError, ValueError):
            value = ThumbnailCache.DEFAULT_LIMIT_MB
        return max(value, 32)

    def _prune_thumb_cache(self, throttle_seconds=60):
        """超出上限就按 LRU 删到限额以内；解码线程还在跑时跳过，避免删掉正在用的图。"""
        worker = getattr(self, "_thumb_worker", None)
        if worker is not None and worker.isRunning():
            return
        now = time.time()
        last = getattr(self, "_thumb_prune_at", 0.0)
        if throttle_seconds and now - last < throttle_seconds:
            return
        try:
            freed = self._thumb_cache().prune(self._thumb_cache_limit_mb())
        except Exception:  # noqa: BLE001
            return
        self._thumb_prune_at = now
        if freed:
            self.append_log("缩略图缓存超出上限，已按最久未用清理 {0}".format(
                human_size(freed)))

    def clear_thumb_cache(self):
        """手动清空缩略图缓存（只删缓存，不动任何视频文件）。"""
        cache = self._thumb_cache()
        size = cache.size_bytes()
        if size <= 0:
            self._toast("缩略图缓存已经是空的", "info", 2200)
            return
        ret = QMessageBox.question(
            self, APP_NAME,
            "清空缩略图缓存？\n\n占用：{0}\n位置：{1}\n\n"
            "只删除首帧缓存图片，不会动视频文件；下次浏览会重新生成。".format(
                human_size(size), cache.root),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        freed = cache.clear()
        self.model.forget_thumbs()
        self._thumb_done = set()
        self._thumb_loaded = set()
        self.table.viewport().update()
        self.append_log("已清空缩略图缓存，释放 {0}".format(human_size(freed)))
        self._toast("已清空缩略图缓存，释放 {0}".format(human_size(freed)),
                    "success", 2400)
        if getattr(self, "_thumb_active", False):
            self._thumb_timer.start()

    def _thumb_cell_pixmap(self, source):
        """把封面裁切填满单元格并做圆角，返回带设备像素比的画布。"""
        width, height = THUMB_COL_W, THUMB_ROW_H
        dpr = self.devicePixelRatioF() or 1.0
        canvas = QPixmap(int(width * dpr), int(height * dpr))
        canvas.setDevicePixelRatio(dpr)
        canvas.fill(Qt.transparent)

        inner_w, inner_h = width - 12, height - 12
        target_w, target_h = int(inner_w * dpr), int(inner_h * dpr)
        scaled = source.scaled(
            target_w, target_h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        # 居中裁切，保证不同比例的封面都铺满且不变形
        crop_x = max(0, (scaled.width() - target_w) // 2)
        crop_y = max(0, (scaled.height() - target_h) // 2)
        scaled = scaled.copy(crop_x, crop_y, min(target_w, scaled.width()),
                             min(target_h, scaled.height()))
        scaled.setDevicePixelRatio(dpr)

        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        x = (width - inner_w) / 2.0
        y = (height - inner_h) / 2.0
        clip = QPainterPath()
        clip.addRoundedRect(x, y, inner_w, inner_h, 7, 7)
        painter.setClipPath(clip)
        painter.drawPixmap(QPointF(x, y), scaled)
        painter.end()
        return canvas

    def _start_thumbnails(self):
        """
        缩略图改为按可视行懒加载。

        原先任务一结束就把全部行塞进解码队列：全盘搜索结果动辄上万条，
        opencv 要连续解码几小时、还不停抢磁盘。现在只解码当前屏幕看到的
        一小批，滚动时再补；已生成/已失败的行记在 _thumb_done 里不会重试。
        """
        self._stop_thumbnails()
        self._thumb_active = True
        self._thumb_done = set()
        self._thumb_loaded = set()
        self._thumb_running = set()
        self._pump_thumbnails()

    def _visible_thumb_requests(self, limit=24):
        """取当前视口内（上下各留 2 行余量）还没缩略图的行。"""
        table = self.table
        rows = table.model().rowCount()
        if rows == 0:
            return []
        first = table.rowAt(0)
        if first < 0:
            first = 0
        last = table.rowAt(max(table.viewport().height() - 1, 0))
        if last < 0:
            last = min(rows - 1, first + 10)
        start = max(first - 2, 0)
        stop = min(last + 2, rows - 1)

        requests = []
        for row in range(start, stop + 1):
            uid = self._uid_at(row)
            if uid is None or uid in self._thumb_done or uid in self._thumb_running:
                continue
            record = self.rows.get(uid)
            if not record:
                continue
            path = self._media_path(record)
            if not path:
                continue
            requests.append((path, record.get("size", 0), uid))
            if len(requests) >= limit:
                break
        return requests

    def _pump_thumbnails(self):
        """有可见空闲行且当前没有解码线程时，启动下一批。"""
        if not getattr(self, "_thumb_active", False) or self._closing:
            return
        existing = self._thumb_worker
        if existing is not None and existing.isRunning():
            return
        requests = self._visible_thumb_requests()
        if not requests:
            # 可视区都填满了：顺手检查一次缓存是否超限（内部有 60s 节流）
            self._prune_thumb_cache()
            return
        worker = ThumbnailWorker(requests)
        worker.thumb_ready.connect(self.on_thumb_ready)
        worker.finished.connect(self._on_thumb_finished)
        self._thumb_worker = worker
        self._thumb_running = {uid for _p, _s, uid in requests}
        worker.start()

    def _on_thumb_scroll(self, *_args):
        """滚动时防抖：停下来 120ms 后再按新的可视区补一批。"""
        if not getattr(self, "_thumb_active", False):
            return
        self._thumb_timer.start()

    def on_thumb_ready(self, cache_path, uid):
        if self._closing:
            return
        row = self._row_for_uid(uid)
        if row is None:
            return
        pixmap = QPixmap(cache_path)
        if pixmap.isNull():
            return
        self._thumb_loaded.add(uid)
        self.model.set_thumb(uid, pixmap)

    def _on_thumb_finished(self):
        worker = self.sender()
        if worker is self._thumb_worker:
            self._thumb_worker = None
        self._thumb_done |= self._thumb_running
        self._thumb_running = set()
        if worker is not None:
            worker.deleteLater()
        # 可视区可能还有没填满的行（例如同屏超过一批），继续补
        QTimer.singleShot(0, self._pump_thumbnails)

    def _stop_thumbnails(self):
        self._thumb_active = False
        self._thumb_timer.stop()
        worker = self._thumb_worker
        self._thumb_worker = None
        self._thumb_running = set()
        if worker is None:
            return
        for signal in (worker.thumb_ready, worker.finished):
            try:
                signal.disconnect()
            except TypeError:
                pass
        worker.cancel()
        if worker.isRunning() and not worker.wait(1500):
            worker.setParent(None)
            _ORPHAN_THREADS.append(worker)
            return
        worker.deleteLater()

    # =================================================================== #
    # 统计
    # =================================================================== #
    def _update_stats(self):
        if not hasattr(self, "chip_total"):
            return
        elapsed = self._stat.get("elapsed", 0.0)
        running = bool(self.worker and self.worker.isRunning() and self._task_started)
        if running:
            elapsed = max(time.time() - self._task_started, 0.0)
            done, total = self._last_progress
            if total:
                self.lbl_progress.setText("已处理 {0} / {1} · 用时 {2}".format(
                    done, total, format_duration(int(elapsed * 1000))))

        counted = (self._stat["success"] + self._stat["skipped"]
                   + self._stat["failed"] + self._stat["inplace"]
                   + self._stat["deleted"])
        pending = max(self._stat["total"] - counted, 0)

        self.chip_total.set_value(self._stat["total"])
        self.chip_success.set_value(self._stat["success"])
        self.chip_fail.set_value(self._stat["failed"])
        self.chip_skip.set_value(self._stat["skipped"])
        self.chip_inplace.set_value(self._stat["inplace"])
        self.chip_deleted.set_value(self._stat["deleted"])
        self.chip_pending.set_value(pending)
        self.chip_size.set_value(human_size(self._stat["bytes"]))
        self.chip_deleted.setVisible(self._stat["deleted"] > 0)
        self.chip_pending.setVisible(pending > 0)
        # 胶囊增减会改变换行结果，重排一次（内部有变化才真正动布局）
        stats_row = getattr(self, "stats_row", None)
        if stats_row is not None:
            stats_row.refresh()
        self._update_badge()

    def _count_status(self, uid, status, size):
        """每个视频只记一次终态；删除状态用独立集合，允许从成功态转为已删除。"""
        # 撤销只改行状态，不参与本轮统计（成功/失败数在任务结束时已经结算过）
        if status.startswith("已撤销") or status.startswith("撤销失败"):
            return
        if status.startswith("已删除") or status.startswith("删除失败"):
            if uid not in self._deleted_uids:
                self._deleted_uids.add(uid)
                self._stat["deleted"] += 1
            return
        if uid in self._counted_uids:
            return
        if status in ("", "排队中") or status.startswith("待处理"):
            return
        self._counted_uids.add(uid)
        if status.startswith("失败"):
            self._stat["failed"] += 1
        elif status.startswith("已在"):
            self._stat["inplace"] += 1
        elif status.startswith("同名"):
            self._stat["skipped"] += 1
        elif status.startswith("已"):
            self._stat["success"] += 1
            self._stat["bytes"] += size or 0

    def _update_badge(self):
        if not hasattr(self, "badge"):
            return
        count = len(self.rows)
        text = str(count)
        self.badge.setText(text)
        self.badge.setFixedWidth(max(26, self.badge.fontMetrics().horizontalAdvance(text) + 18))

    # =================================================================== #
    # 表格辅助
    # =================================================================== #
    def _row_for_uid(self, uid):
        """uid → 视图行号（代理模型里的位置，已考虑排序与筛选）。"""
        return self.proxy.proxy_row_of(uid)

    def _uid_at(self, row):
        """视图行号 → uid。"""
        return self.proxy.uid_at(row)

    def _selected_uids(self):
        uids = []
        for index in self.table.selectionModel().selectedRows():
            uid = self._uid_at(index.row())
            if uid is not None and uid not in uids:
                uids.append(uid)
        return uids

    def _ordered_records(self):
        """按表格当前显示顺序取记录（排序/筛选后导出顺序与眼前一致）。"""
        records = []
        for row in range(self.table.model().rowCount()):
            uid = self._uid_at(row)
            record = self.rows.get(uid)
            if record is not None:
                records.append(record)
        return records or list(self.rows.values())

    def _single_selection(self):
        uids = self._selected_uids()
        if not uids:
            return None
        return self.rows.get(uids[0])

    def _set_row_status(self, uid, status, target=None):
        record = self.rows.get(uid)
        if record is None:
            return
        record["status"] = status
        if target is not None:
            record["target"] = target or ""
        # 只有真正由本次任务复制/移动生成的目标才可被「同时删除目标副本」删掉；
        # 去重/同名跳过时 target 指向的是目标目录里原有的第三方文件，绝不能删。
        if status.startswith(("已复制", "已移动")) and record.get("target"):
            record["target_created"] = True

        row = self._row_for_uid(uid)
        if row is not None:
            self.model.set_status(uid, status, target)

        self._count_status(uid, status, record.get("size", 0))
        # 不在这里直接刷统计：worker 每个文件都会回写状态，合并到 80ms 后统一刷
        self._schedule_status_ui()

    def _on_sort_changed(self, *_args):
        self._uid_rows_valid = False
        QTimer.singleShot(0, self._apply_filter)

    def _on_filter_text_changed(self, _text=""):
        """输入时只重启防抖计时器，停手 110ms 后再真正筛选。"""
        self._sync_search_hint()
        self._filter_timer.start()

    def _schedule_status_ui(self):
        """把统计胶囊/状态栏的刷新合并到一次定时器回调里。"""
        if not self._status_ui_timer.isActive():
            self._status_ui_timer.start()

    def _flush_status_ui(self):
        if self._closing:
            return
        self._update_stats()
        self._update_status_bar()

    def _apply_filter(self, _text=None):
        """文字 + 状态双重筛选。

        筛选现在落在代理模型上：源数据不增不删，被筛掉的行自然也不会留在
        「选中」集合里，原先逐行 setRowHidden + 取消选中的那套循环不再需要。
        """
        keyword = self.filter_edit.text().strip().lower()
        self.proxy.set_filter(keyword, self._status_filter)
        self._visible_rows = self.proxy.rowCount()
        self._update_status_bar()
        # 筛选改变了可见行集合，按新的可视区补缩略图
        if self._thumb_active:
            self._thumb_timer.start()

    def _update_status_bar(self):
        total = len(self.rows)
        # 用维护好的集合取已删除数量（原先每次遍历全部记录，任务进行中会被高频调用）
        deleted = len(self._deleted_uids)
        selected = len(self._selected_uids())
        text = "共 {0} 个视频".format(total)
        if self._status_filter or self.filter_edit.text().strip():
            text += " · 显示 {0} / {1}".format(self._visible_rows, total)
        if selected:
            text += " · 已选 {0}".format(selected)
        if deleted:
            text += " · 已删除 {0}".format(deleted)
        # 与 C++ 版合并后的同一条文案：数量在前，"下一步该干什么"跟在后面。
        # 已勾选时不再追加提示——312px 侧栏塞三句会折行被裁。
        running = bool(self.worker and self.worker.isRunning())
        if not running:
            if total == 0:
                text += " · 点「修改」配置后开始搜索"
            elif not selected:
                text += " · 勾选后点「整理选中」"
        self.lbl_status.setText(text)

    def _on_selection_changed(self):
        uids = self._selected_uids()
        if len(uids) == 1:
            record = self.rows.get(uids[0], {})
            self.lbl_pv_name.setText(record.get("name", "未选择视频"))
            self.lbl_pv_meta.setText("{0} · {1}".format(
                human_size(record.get("size", 0)), record.get("status", "")))
            self._set_preview_path(self._media_path(record))
        elif len(uids) > 1:
            self.lbl_pv_name.setText("已选 {0} 个视频".format(len(uids)))
            self.lbl_pv_meta.setText("可批量删除，或双击逐个预览")
            self._set_preview_path("")
        else:
            self.lbl_pv_name.setText("未选择视频")
            self.lbl_pv_meta.setText("")
            self._set_preview_path("")
            if self._current_uid is None:
                self._show_video_placeholder("选择一个视频", "双击列表中的视频，这里会显示画面")

        if self._current_uid is not None and self._current_uid not in uids:
            self._stop_media()
            self.lbl_pv_state.setText("")
        # 表头全选框是自绘的，选中集一变就得让表头重画（全选/半选/未选）
        header = self.table.horizontalHeader()
        header.viewport().update()
        self._update_actions()
        self._update_status_bar()

    def _toggle_row_check(self, index):
        """切换单行勾选：勾选态就是行选中态，所以只是把这一行选中/取消。"""
        selection = self.table.selectionModel()
        if selection is None:
            return
        flags = (QItemSelectionModel.Select | QItemSelectionModel.Rows)
        if selection.isSelected(index):
            flags = QItemSelectionModel.Deselect | QItemSelectionModel.Rows
        selection.select(index, flags)

    # =================================================================== #
    # 预览 / 播放
    # =================================================================== #
    @staticmethod
    def _media_path(record):
        for key in ("path", "target"):
            candidate = record.get(key) or ""
            if candidate and os.path.isfile(candidate):
                return candidate
        return ""

    def _preview_row(self, row):
        uid = self._uid_at(row)
        if uid is None:
            return
        if self.preview_mode_seg.value() == "popup":
            self._popout_uid(uid)
        else:
            self.load_preview(uid, autoplay=True)

    def _preview_selected(self):
        uids = self._selected_uids()
        if not uids:
            self._toast("请先在列表中选中一个视频", "warn")
            return
        if self.preview_mode_seg.value() == "popup":
            self._popout_uid(uids[0])
        else:
            self.load_preview(uids[0], autoplay=True)

    def load_preview(self, uid, autoplay=False):
        # 运行中也能预览：整理任务在后台线程里跑，播放器读的是另一个文件句柄，
        # 不再一刀切禁掉（用户对"双击没反应"的感知就是 bug）。
        record = self.rows.get(uid)
        if record is None:
            return
        path = self._media_path(record)
        if not path:
            self.lbl_pv_state.setText("文件已不存在（可能被移动或删除）。")
            self._stop_media()
            return

        if self._preview_hidden:
            self._toggle_preview()

        if self._current_uid == uid and self._media_loaded:
            if autoplay:
                self.player.play()
            return

        self._stop_media()
        self._current_uid = uid
        self._media_loaded = True
        braced = os.path.splitext(path)[1].lower().lstrip(".")
        if braced in RISKY_EXTS:
            # 预判：这类容器常见 XVID / MPEG-2 / DivX 或 AC3 / DTS 音轨。
            # 只写状态行、不盖占位层——本机的 FFmpeg 后端多数能解，盖上去会误报；
            # 真解不开时 InvalidMedia 会把完整覆盖层摆出来（见 _on_media_status）。
            self.video_stack.setCurrentWidget(self.video)
            self.lbl_pv_state.setText(
                "正在解码 {0} · 这类容器常见 XVID / MPEG-2 / AC3，解不开就点「系统播放器」".format(
                    braced.upper()))
        elif braced in SOFT_RISKY_EXTS:
            self.video_stack.setCurrentWidget(self.video)
            self.lbl_pv_state.setText("正在解码 {0}（容器格式，能否播放取决于内部编码）".format(
                braced.upper()))
        else:
            self.video_stack.setCurrentWidget(self.video)
            self.lbl_pv_state.setText("正在加载：{0}".format(os.path.basename(path)))
        self.btn_mute.setEnabled(True)
        self.rate_combo.setEnabled(True)
        self.player.setSource(QUrl.fromLocalFile(path))
        if autoplay:
            self.player.play()

    def _popout_uid(self, uid):
        record = self.rows.get(uid)
        if record is None:
            return
        self._popout_path(self._media_path(record))

    def _popout_preview(self):
        record = self._single_selection()
        if not record:
            self._toast("请先在列表中选中一个视频", "warn")
            return
        self._popout_path(self._media_path(record))

    def _popout_path(self, path):
        if not path:
            self._toast("文件已不存在", "warn")
            return
        # 独立窗口同样不再受任务运行状态限制（自带播放器，与整理互不干扰）
        self._stop_media()
        dialog = PreviewDialog(path, self.volume.value(), self)
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        self._popup_windows.append(dialog)
        dialog.destroyed.connect(lambda _=None, d=dialog: self._forget_popup(d))
        dialog.show()
        dialog.raise_()

    def _forget_popup(self, dialog):
        if dialog in self._popup_windows:
            self._popup_windows.remove(dialog)

    def _copy_current_preview_path(self):
        if self._current_uid is not None:
            record = self.rows.get(self._current_uid) or {}
            path = self._media_path(record) or record.get("path", "")
        else:
            record = self._single_selection() or {}
            path = self._media_path(record) or record.get("path", "")
        if path:
            QApplication.clipboard().setText(path)
            self._toast("已复制路径", "success", 1600)

    def _stop_media(self):
        self._current_uid = None
        self._media_loaded = False
        self._seeking = False
        self.player.stop()
        self.player.setSource(QUrl())
        self.audio_out.setMuted(False)
        self._reset_rate()
        self.btn_mute.setEnabled(False)
        self.rate_combo.setEnabled(False)
        self.slider.setRange(0, 0)
        self.slider.setValue(0)
        self.lbl_time.setText("00:00 / 00:00")
        self._set_play_button(False)
        self._show_video_placeholder("选择一个视频", "双击列表中的视频，这里会显示画面")

    def _set_play_button(self, playing):
        if playing:
            self.btn_play.setText("暂停")
            self.btn_play.setIcon(line_icon("pause", COLORS["accent"], 15))
        else:
            self.btn_play.setText("播放")
            self.btn_play.setIcon(line_icon("play", COLORS["accent"], 15))

    def _show_video_placeholder(self, title=None, sub=None):
        if title:
            self.lbl_ph_title.setText(title)
        if sub:
            self.lbl_ph_sub.setText(sub)
        self.video_stack.setCurrentWidget(self.video_placeholder)

    def _set_preview_path(self, path):
        self._pv_path_full = path or ""
        self.lbl_pv_path.setToolTip(path or "点击复制完整路径")
        if not path:
            self.lbl_pv_path.setText("")
            return
        self._elide_preview_path()

    def _elide_preview_path(self):
        """按面板当前宽度重新省略路径：只算一次的旧宽度在拖动分栏后会错位。"""
        full = getattr(self, "_pv_path_full", "")
        if not full:
            return
        width = max(self.preview_panel.width() - 40, 180)
        self.lbl_pv_path.setText(
            self.lbl_pv_path.fontMetrics().elidedText(full, Qt.ElideMiddle, width))

    def _on_volume_changed(self, value):
        # 和主流播放器一致：拖动音量条时自动取消静音，避免"滑了没声音"的困惑
        if self.audio_out.isMuted():
            self.audio_out.setMuted(False)
            self._sync_mute_ui()
        self.audio_out.setVolume(max(0.0, min(1.0, value / 100.0)))

    def _toggle_mute(self):
        self.audio_out.setMuted(not self.audio_out.isMuted())
        self._sync_mute_ui()

    def _sync_mute_ui(self):
        muted = self.audio_out.isMuted()
        self.btn_mute.setText("取消静音" if muted else "静音")

    def _on_rate_changed(self):
        try:
            rate = float(self.rate_combo.currentText().rstrip("x"))
        except (ValueError, AttributeError):
            rate = 1.0
        self.player.setPlaybackRate(rate)

    def _reset_rate(self):
        self.rate_combo.setCurrentIndex(2)   # 1x
        self.player.setPlaybackRate(1.0)
        self._sync_mute_ui()

    def _toggle_play(self):
        if not self._media_loaded:
            uids = self._selected_uids()
            if not uids:
                self._toast("请先在列表中选中一个视频", "warn")
                return
            self.load_preview(uids[0], autoplay=True)
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_slider_pressed(self):
        self._seeking = True

    def _on_slider_released(self):
        self._seeking = False
        self.player.setPosition(self.slider.value())

    def _on_position_changed(self, position):
        if self._seeking:
            return
        if self.player.duration() > 0:
            self.slider.setValue(position)
        total = self.player.duration()
        self.lbl_time.setText("{0} / {1}".format(format_duration(position), format_duration(total)))

    def _on_duration_changed(self, duration):
        if duration > 0:
            self.slider.setRange(0, duration)
            self.slider.setPageStep(max(duration // 20, 1000))
        self.lbl_time.setText("{0} / {1}".format(
            format_duration(self.player.position()), format_duration(duration)))

    def _on_player_state(self, state):
        self._set_play_button(state == QMediaPlayer.PlayingState)

    def _on_media_status(self, status):
        if self._current_uid is None:
            return
        if status == QMediaPlayer.InvalidMedia:
            record = self.rows.get(self._current_uid) or {}
            path = record.get("path") or record.get("target") or ""
            ext = os.path.splitext(path)[1].lower().lstrip(".").upper()
            if ext:
                self.lbl_pv_state.setText(
                    "已尝试内置播放，失败：「{0}」里的视频/音频编码不在支持列表内。".format(ext))
                if ext.lower() in RISKY_EXTS:
                    sub = ("「{0}」常见 XVID / MPEG-2 / DivX 视频流或 AC3 / DTS 音轨。"
                           "换 H.264 + AAC 的 MP4 能内嵌播放，也可以点「系统播放器」。").format(ext)
                else:
                    # mp4 / mkv 里塞了不支持的编码（H.265、AV1、AC3…）或文件本身损坏，
                    # 别硬套「这类容器常见 XVID」，会误导人。
                    sub = ("文件可能损坏，或内部编码（如 H.265 / AV1 / AC3）不在内置播放器"
                           "的支持范围。可以点「系统播放器」用本机解码器打开。")
                self._show_video_placeholder("这种编码内置播放器解不了", sub)
            else:
                self.lbl_pv_state.setText(
                    "内置播放器打不开这个文件（可能缺少解码器），可点「系统播放器」。")
                self._show_video_placeholder("无法预览该格式", "可改用系统播放器打开")
            self._set_play_button(False)
        elif status in (QMediaPlayer.BufferedMedia, QMediaPlayer.BufferingMedia):
            self.lbl_pv_state.setText("")
            self.video_stack.setCurrentWidget(self.video)
        elif status == QMediaPlayer.EndOfMedia:
            self._set_play_button(False)

    def _open_with_system_player(self):
        record = self._single_selection()
        path = self._media_path(record or {})
        if not path:
            self._toast("选中的文件不存在", "warn")
            return
        try:
            self._open_path(path)
        except Exception as exc:  # noqa: BLE001
            self._toast("无法用系统播放器打开：{0}".format(exc), "error")

    def _open_folder_of_selection(self):
        record = self._single_selection()
        if not record:
            self._toast("请先选中一个视频", "warn")
            return
        path = self._media_path(record) or record.get("path", "")
        folder = os.path.dirname(path)
        if not folder or not os.path.isdir(folder):
            self._toast("所在文件夹不存在", "warn")
            return
        try:
            self._open_path(folder)
        except Exception as exc:  # noqa: BLE001
            self._toast("无法打开文件夹：{0}".format(exc), "error")

    def _copy_path_of_selection(self):
        record = self._single_selection()
        if not record:
            return
        path = self._media_path(record) or record.get("path", "")
        if not path:
            return
        QApplication.clipboard().setText(path)
        self.append_log("已复制路径：" + path)

    def _show_table_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        if not self.table.selectionModel().isRowSelected(row, self.table.rootIndex()):
            self.table.selectRow(row)
        menu = QMenu(self)
        act_preview = menu.addAction("预览播放")
        act_system = menu.addAction("用系统播放器打开")
        act_popout = menu.addAction("独立窗口播放")
        menu.addSeparator()
        act_folder = menu.addAction("打开所在文件夹")
        act_copy = menu.addAction("复制文件路径")
        menu.addSeparator()
        act_delete = menu.addAction("删除（移入回收站）")

        running = bool(self.worker and self.worker.isRunning())
        for action in (act_preview, act_system, act_popout, act_delete):
            action.setEnabled(not running)

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is act_preview:
            self._preview_selected()
        elif chosen is act_system:
            self._open_with_system_player()
        elif chosen is act_popout:
            self._popout_preview()
        elif chosen is act_folder:
            self._open_folder_of_selection()
        elif chosen is act_copy:
            self._copy_path_of_selection()
        elif chosen is act_delete:
            self.delete_selected()

    # =================================================================== #
    # 删除
    # =================================================================== #
    def delete_selected(self):
        if self.worker and self.worker.isRunning():
            self._toast("任务进行中不能删除文件，请先取消任务", "warn")
            return
        uids = self._selected_uids()
        if not uids:
            self._toast("请先在列表中选择要删除的视频", "warn")
            return

        pairs = [(uid, self.rows[uid]) for uid in uids if uid in self.rows]
        source_paths = [r["path"] for _, r in pairs if r.get("path") and os.path.isfile(r["path"])]
        target_paths = []
        for _, record in pairs:
            target = record.get("target") or ""
            if (record.get("target_created") and target and os.path.isfile(target)
                    and norm_path(target) != norm_path(record.get("path") or "")):
                target_paths.append(target)
        if not source_paths and not target_paths:
            self._toast("选中的文件都已经不存在了", "warn")
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(APP_NAME)
        box.setText("确定删除选中的 {0} 个视频？".format(len(pairs)))
        detail = "将删除 {0} 个源文件".format(len(source_paths))
        if target_paths:
            detail += "、{0} 个目标副本".format(len(target_paths))
        box.setInformativeText(detail + "。\n文件会移入回收站，需要时可以还原。")
        target_checkbox = None
        if target_paths:
            target_checkbox = QCheckBox("同时删除目标文件夹中的对应副本", box)
            target_checkbox.setChecked(True)
            box.setCheckBox(target_checkbox)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText("删除")
        if box.exec() != QMessageBox.Yes:
            return

        also_target = bool(target_checkbox and target_checkbox.isChecked())
        delete_list = list(source_paths)
        if also_target:
            delete_list.extend(target_paths)

        if self._current_uid in uids:
            self._stop_media()

        result = {"deleted": set(), "errors": []}
        delete_worker = DeleteWorker(delete_list)
        # 必须连到绑定方法而不是 lambda：lambda 在 PyQt 里按「直连」处理，
        # 会在删除线程里直接改界面（_finalize_delete 要动表格/胶囊），可能崩。
        delete_worker.deleted.connect(self._on_delete_file_done)
        delete_worker.failed.connect(self._on_delete_file_failed)
        delete_worker.finished_all.connect(self._on_delete_all_done)
        self._delete_result = result
        self._delete_pairs = pairs
        self._delete_worker = delete_worker
        delete_worker.start()
        self.append_log("正在移入回收站：{0} 个文件…".format(len(delete_list)))

    def _on_delete_file_done(self, path):
        if self._delete_result is not None:
            self._delete_result["deleted"].add(norm_path(path))

    def _on_delete_file_failed(self, path, error):
        if self._delete_result is not None:
            self._delete_result["errors"].append(
                "{0}：{1}".format(os.path.basename(path), error))

    def _on_delete_all_done(self, _ok, _bad):
        result = self._delete_result or {"deleted": set(), "errors": []}
        pairs = self._delete_pairs or []
        self._delete_result = None
        self._delete_pairs = None
        if self._delete_worker is not None:
            self._delete_worker.deleteLater()
            self._delete_worker = None
        self._finalize_delete(result["deleted"], result["errors"], pairs)

    def _finalize_delete(self, deleted, errors, pairs):
        for uid, record in pairs:
            source_deleted = bool(record.get("path")) and norm_path(record["path"]) in deleted
            target_deleted = False
            target = record.get("target") or ""
            if target and norm_path(target) in deleted and norm_path(target) != norm_path(record.get("path") or ""):
                target_deleted = True
            if not source_deleted and not target_deleted:
                continue
            if source_deleted and target_deleted:
                status = "已删除源文件和目标副本"
            elif source_deleted:
                status = "已删除源文件"
            else:
                status = "已删除目标副本"
            record["deleted"] = True
            self._set_row_status(uid, status)

        if deleted:
            self.append_log("已移入回收站：{0} 个文件".format(len(deleted)))
            self._toast("已移入回收站：{0} 个文件".format(len(deleted)), "success")
        if errors:
            for line in errors:
                self.append_log("[删除失败] " + line)
            self._toast("有 {0} 个文件没能删除".format(len(errors)), "error", 5000)
        self._update_status_bar()
        self._update_actions()

    # =================================================================== #
    # 报告
    # =================================================================== #
    def export_report(self):
        if not self.rows:
            self._toast("还没有可导出的记录", "warn")
            return
        default_name = "视频整理报告_{0}.csv".format(time.strftime("%Y%m%d_%H%M%S"))
        default_dir = self.dst_edit.text().strip() or os.path.expanduser("~")
        path, _ = QFileDialog.getSaveFileName(
            self, "保存报告", os.path.join(default_dir, default_name), "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fp:
                writer = csv.writer(fp)
                writer.writerow(["序号", "文件名", "大小(字节)", "状态", "源文件路径", "目标文件路径"])
                for index, record in enumerate(self._ordered_records(), 1):
                    writer.writerow([
                        index,
                        record.get("name", ""),
                        record.get("size", 0),
                        record.get("status", ""),
                        record.get("path", ""),
                        record.get("target", ""),
                    ])
        except OSError as exc:
            self._toast("写报告失败：{0}".format(exc), "error", 5000)
            return
        self.append_log("报告已保存：" + path)
        self._toast("报告已保存：" + os.path.basename(path), "success", 4000)

    def export_report_html(self):
        if not self.rows:
            self._toast("还没有可导出的记录", "warn")
            return
        default_name = "视频整理报告_{0}.html".format(time.strftime("%Y%m%d_%H%M%S"))
        default_dir = self.dst_edit.text().strip() or os.path.expanduser("~")
        path, _ = QFileDialog.getSaveFileName(
            self, "保存报告", os.path.join(default_dir, default_name), "HTML 文件 (*.html)")
        if not path:
            return

        records = self._ordered_records()
        total = len(records)
        by_status = {}
        for r in records:
            key = r.get("status", "") or "待处理"
            by_status[key] = by_status.get(key, 0) + 1

        def esc(text):
            import html
            return html.escape(str(text))

        def status_class(status):
            s = status or ""
            if s.startswith(("已复制", "已移动")):
                return "ok"
            if s.startswith("失败") or s.startswith("删除失败"):
                return "fail"
            if s.startswith("已在目标") or s.startswith("已跳过"):
                return "warn"
            if s.startswith("已删除"):
                return "del"
            if s.startswith("待处理") or s in ("", "排队中"):
                return "pend"
            return "warn"

        rows_html = "".join(
            "<tr><td>{i}</td><td class='cell-name'>{name}</td>"
            "<td>{size}</td><td><span class='tag {cls}'>{status}</span></td>"
            "<td class='cell-large'>{src}</td><td class='cell-large'>{dst}</td></tr>".format(
                i=idx,
                name=esc(rec.get("name", "")),
                size=human_size(rec.get("size", 0) or 0),
                cls=status_class(rec.get("status", "")),
                status=esc(rec.get("status", "") or "待处理"),
                src=esc(rec.get("path", "")),
                dst=esc(rec.get("target", "") or ""),
            )
            for idx, rec in enumerate(records, 1)
        )

        summary = "".join(
            "<div class='stat'><div class='stat-num'>{n}</div>"
            "<div class='stat-label'>{k}</div></div>".format(n=v, k=esc(k))
            for k, v in sorted(by_status.items())
        ) or "<div class='stat'><div class='stat-num'>0</div><div class='stat-label'>无</div></div>"

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        report = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>视频整理报告</title>
</head>
<body>
<style>
  *{{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
         background: linear-gradient(135deg,#f3f5fb 0%,#eef2fb 100%); color:#20242e; }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 32px 20px 56px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color:#6b7280; font-size:13px; margin-bottom:22px; }}
  .stats {{ display:flex; flex-wrap:wrap; gap:12px; margin-bottom:22px; }}
  .stat {{ background:rgba(255,255,255,.86); border:1px solid #e6eaf5;
          border-radius:14px; box-shadow:0 6px 18px rgba(80,100,160,.08);
          padding:14px 20px; min-width:120px; }}
  .stat-num {{ font-size:22px; font-weight:700; color:#4f46e5; }}
  .stat-label {{ font-size:12px; color:#7c8290; margin-top:2px; }}
  table {{ width:100%; border-collapse:collapse; background:rgba(255,255,255,.92);
          border-radius:14px; overflow:hidden; box-shadow:0 8px 24px rgba(80,100,160,.10); }}
  th, td {{ text-align:left; padding:10px 14px; font-size:13px; }}
  thead th {{ position:sticky; top:0; background:#eef1fb; color:#4b5264;
             font-weight:600; white-space:nowrap; }}
  tbody tr {{ border-top:1px solid #eef1f8; }}
  tbody tr:nth-child(even) {{ background:#fafbff; }}
  .cell-name {{ font-weight:600; color:#20242e; }}
  .cell-large {{ max-width:380px; word-break:break-all; color:#5b6270; font-size:12px; }}
  .tag {{ display:inline-block; padding:2px 10px; border-radius:20px; font-size:12px; white-space:nowrap; }}
  .tag.ok {{ background:#e6f7ef; color:#0f9d68; }}
  .tag.fail {{ background:#fdeaea; color:#d03434; }}
  .tag.warn {{ background:#fdf3e4; color:#b97f10; }}
  .tag.del {{ background:#eef0f5; color:#707a8a; }}
  .tag.pend {{ background:#eaf0fe; color:#4f6fd6; }}
  .foot {{ margin-top:16px; font-size:12px; color:#9aa1af; }}
</style>
<div class="wrap">
  <h1>视频整理报告</h1>
  <div class="sub">生成时间：{now} · 共 {total} 条记录</div>
  <div class="stats">{summary}</div>
  <table>
    <thead><tr><th>#</th><th>文件名</th><th>大小</th><th>状态</th><th>源文件路径</th><th>目标文件路径</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <div class="foot">由 Video Collector 导出 · 本文件为自包含 HTML，可直接双击用浏览器打开</div>
</div>
</body>
</html>""".format(now=now, total=total, summary=summary, rows=rows_html)

        try:
            with open(path, "w", encoding="utf-8") as fp:
                fp.write(report)
        except OSError as exc:
            self._toast("写报告失败：{0}".format(exc), "error", 5000)
            return
        self.append_log("HTML 报告已保存：" + path)
        self._toast("报告已保存：" + os.path.basename(path), "success", 4000)

    def undo_last(self):
        """撤销最近一次整理的成果：删除本次生成的副本，移动的文件移回源位置。异步执行，不阻塞 GUI。"""
        snapshot = self._undo_snapshot or []
        if not snapshot:
            self._toast("没有可撤销的整理操作", "warn")
            return
        moves = sum(1 for a in snapshot if a.get("mode") == "move")
        total = len(snapshot)
        desc = "{0} 个副本（其中 {1} 个移动需移回源文件）".format(total, moves)

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(APP_NAME)
        box.setText("撤销本次整理？")
        box.setInformativeText(
            "将撤销 {0}。\n"
            "复制模式：把目标副本移入回收站（可还原）。\n"
            "移动模式：把目标副本移回源文件夹（完整还原）。".format(desc))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText("撤销")
        if box.exec() != QMessageBox.Yes:
            return

        self._undo_snapshot = None
        self._update_actions()
        self._undo_worker = UndoWorker(snapshot)
        self._undo_worker.processed.connect(self._on_undo_item)
        self._undo_worker.finished_all.connect(self._on_undo_finished)
        self._undo_worker.finished.connect(self._undo_worker.deleteLater)
        self._undo_worker.start()

    def _on_undo_item(self, action, ok, note):
        """撤销是逐条做的：每处理完一条就把对应行的状态改掉，和 C++ 版一致。"""
        uid = (action or {}).get("uid")
        if uid is None:
            return
        record = self.rows.get(uid)
        if record is None:
            return
        if ok:
            record["target_created"] = False
            self._set_row_status(uid, "已撤销（{0}）".format(note))
        else:
            self._set_row_status(uid, "撤销失败：{0}".format(note))

    def _on_undo_finished(self, ok, fail):
        self._undo_worker = None
        self._undo_snapshot = None
        self._update_actions()
        self._update_stats()
        self._update_status_bar()
        if fail:
            self._toast("撤销完成：成功 {0} 项，{1} 项失败。".format(ok, fail), "warn", 6000)
            self.append_log("撤销完成：成功 {0} 项，失败 {1} 项。".format(ok, fail))
        else:
            self._toast("已撤销本次整理，共 {0} 项。".format(ok), "success", 4000)
            self.append_log("已撤销本次整理，共 {0} 项。".format(ok))

    # =================================================================== #
    # 关闭
    # =================================================================== #
    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            ret = QMessageBox.question(
                self, APP_NAME, "任务还在进行，确定要退出吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                event.ignore()
                return

            self._closing = True
            worker = self.worker
            worker.cancel()
            # 取消后优先让当前文件自然结束：copy 模式由 _copy_noclobber/_cleanup_partial
            # 负责清掉半成品，绝不靠强制中断（否则会留下不完整副本）。
            # 先给 15s（对齐 C++ 的 WaitDone(15000)），若仍在跑，再按 5s 一档
            # 等到每个 30s 的上限，尽量让它收尾；极端卡死才最后 resort 到 terminate。
            if not worker.wait(15000):
                import time as _time
                _deadline = _time.monotonic() + 30.0
                while worker.isRunning() and _time.monotonic() < _deadline:
                    if worker.wait(5000):
                        break
                if worker.isRunning():
                    self._disconnect_worker(worker)
                    worker.setParent(None)
                    worker.terminate()
                    if worker.wait(3000):
                        worker.deleteLater()
                    else:
                        _ORPHAN_THREADS.append(worker)
                    self.worker = None
                    self.log_view.appendPlainText(
                        "警告：后台线程未在预期时间内结束，已强制中断，当前文件可能留下不完整的副本。")
            else:
                self.worker = None
                worker.deleteLater()
        # 等待仍在进行的后台删除/撤销结束，避免窗口关闭时被系统中断
        self._wait_background_workers()
        self._closing = True
        self._stats_timer.stop()
        self._stop_thumbnails()
        self._stop_media()
        self._save_settings()
        super().closeEvent(event)

    def _wait_background_workers(self):
        """关闭前收尾删除/撤销线程。

        删几百个文件（网络盘）时 15 秒不一定够，超时后不能直接丢掉 QThread 对象
        （运行中的 QThread 被析构会让 Qt 直接 abort），所以断开信号并交给孤儿列表。
        """
        for attr in ("_delete_worker", "_undo_worker"):
            worker = getattr(self, attr, None)
            if worker is None or not worker.isRunning():
                continue
            if worker.wait(15000):
                continue
            self._disconnect_worker(worker)
            worker.setParent(None)
            _ORPHAN_THREADS.append(worker)
        self._delete_worker = None
        self._undo_worker = None

    @staticmethod
    def _disconnect_worker(worker):
        # CollectWorker / ScanAllWorker / DeleteWorker / UndoWorker 的信号集合不同，
        # 按名字取，缺失的跳过
        names = ("scan_count", "scan_done", "item_added", "item_status",
                 "progress", "log", "done", "items", "finished_stats", "finished",
                 "deleted", "failed", "finished_all", "processed")
        for name in names:
            signal = getattr(worker, name, None)
            if signal is None:
                continue
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass

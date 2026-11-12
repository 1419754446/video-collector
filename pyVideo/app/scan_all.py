#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全盘只读视频发现
================

与「开始整理」完全解耦：本模块只枚举 / 遍历 / 上报，**绝不复制、移动、
重命名或删除任何文件**。扫描结果进入主窗口表格，由用户勾选后再交给
CollectWorker 的 file_list 模式统一整理。

约定（两端一致）：
- 盘符每次由用户手动勾选，默认一个都不选；网络盘 / 光驱 / RAM 盘不可选。
- 跳过系统目录、隐藏项、符号链接 / 重解析点、目标文件夹及其子路径。
- 单目录读取失败只记日志并继续；每个目录检查一次取消。
- 命中数达到上限即停止，避免几十万行把界面拖死。
"""

import os
import stat
import time

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from . import theme

# 默认排除的目录名（小写比较，按名字匹配，任何层级都生效）
DEFAULT_EXCLUDE_NAMES = {
    "windows", "program files", "program files (x86)", "programdata",
    "appdata", "$recycle.bin", "system volume information", "recovery",
    "perflogs", "msocache", "config.msi", "documents and settings",
    "node_modules", ".git", ".svn", ".hg", ".venv", "venv",
    "__pycache__", "$windows.~ws", "$windows.~bt", "windows.old",
}
DEFAULT_MAX_RESULTS = 20000

DRIVE_UNKNOWN, DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_CDROM, DRIVE_RAMDISK = range(6)


# --------------------------------------------------------------------------- #
# 磁盘枚举
# --------------------------------------------------------------------------- #
def list_drives():
    """返回 [{path, label, kind, selectable}]，Windows 优先用 Win32 API。"""
    if os.name != "nt":
        home = os.path.expanduser("~")
        return [{"path": home, "label": "当前用户目录  {0}".format(home),
                 "kind": "fixed", "selectable": True}]

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_logical = kernel32.GetLogicalDrives
    get_logical.restype = wintypes.DWORD
    get_type = kernel32.GetDriveTypeW
    get_type.argtypes = [wintypes.LPCWSTR]
    get_type.restype = wintypes.UINT
    get_volume = kernel32.GetVolumeInformationW
    get_volume.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                          wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD,
                          wintypes.LPWSTR, wintypes.DWORD]

    mask = get_logical()
    drives = []
    for index in range(26):
        if not (mask & (1 << index)):
            continue
        root = "{0}:\\".format(chr(ord("A") + index))
        kind_value = get_type(root)
        selectable = True
        if kind_value == 3:
            kind = "fixed"
        elif kind_value == 2:
            kind = "removable"
        elif kind_value == 4:
            kind, selectable = "remote", False
        elif kind_value == 5:
            kind, selectable = "cdrom", False
        elif kind_value == 6:
            kind, selectable = "ramdisk", False
        else:
            kind, selectable = "unknown", False

        label_buf = ctypes.create_unicode_buffer(256)
        fs_buf = ctypes.create_unicode_buffer(64)
        volume = ""
        try:
            ok = get_volume(root, label_buf, 255, None, None, None, fs_buf, 63)
            if ok and label_buf.value.strip():
                volume = label_buf.value.strip()
        except Exception:  # noqa: BLE001 - 卷标读取失败不影响扫描
            pass
        label = "{0}{1}  ({2}:)".format(
            volume or ("本地磁盘" if kind == "fixed" else "可移动磁盘"),
            "" if kind == "fixed" else " · " + kind,
            root[0])
        drives.append({"path": root, "label": label,
                        "kind": kind, "selectable": selectable})
    return drives


# --------------------------------------------------------------------------- #
# 路径辅助
# --------------------------------------------------------------------------- #
def _is_reparse(entry):
    """目录是符号链接 / junction 时跳过；stat 失败时保守跳过，防止目录环。"""
    try:
        if entry.is_symlink():
            return True
        info = entry.stat(follow_symlinks=False)
        attrs = getattr(info, "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return True


def _hidden_entry(entry):
    """复用 DirEntry 缓存的属性判断隐藏/系统项，避免每个条目多一次 stat。"""
    name = entry.name
    if name.startswith("."):
        return True
    if os.name != "nt":
        return False
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    flags = (getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
             | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4))
    return bool(attrs & flags)


def _under(path, base):
    """path 是否等于 base 或位于 base 之下（纯字符串比较，不做 realpath）。"""
    if not base:
        return False
    p = os.path.normcase(os.path.normpath(path))
    b = os.path.normcase(os.path.normpath(base))
    return p == b or p.startswith(b.rstrip("\\/") + os.sep)


def parse_custom_excludes(text):
    """用户自定义排除项：目录名或绝对路径，逗号分隔。"""
    return [piece.strip() for piece in (text or "").replace(";", ",").split(",")
            if piece.strip()]


# --------------------------------------------------------------------------- #
# 只读扫描线程
# --------------------------------------------------------------------------- #
class ScanAllWorker(QThread):
    progress = Signal(int, int)        # 已扫描目录数, 已发现视频数
    items = Signal(list)               # [(index, name, size, dirname, fullpath), ...]
    log = Signal(str)
    finished_stats = Signal(dict)

    def __init__(self, roots, exts, skip_hidden, custom_excludes,
                 dst_dir="", max_results=DEFAULT_MAX_RESULTS,
                 min_bytes=0, max_bytes=0, recursive=True, parent=None):
        super().__init__(parent)
        self.roots = [os.path.abspath(r) for r in roots if r]
        # 指定目录搜索时「包含子文件夹」是可选开关；全盘搜索始终递归
        self.recursive = bool(recursive)
        self.exts = set(e.lower().lstrip(".") for e in exts)
        self.skip_hidden = bool(skip_hidden)
        self.exclude_names = set(DEFAULT_EXCLUDE_NAMES)
        self.exclude_paths = []
        for item in custom_excludes:
            low = item.lower()
            if os.path.isabs(item) or (len(item) > 2 and item[1] == ":"):
                self.exclude_paths.append(
                    os.path.normcase(os.path.normpath(os.path.abspath(item))))
            else:
                self.exclude_names.add(low.rstrip("\\/"))
        self.dst_dir = (os.path.normcase(os.path.normpath(os.path.abspath(dst_dir)))
                        if dst_dir else "")
        try:
            self.max_results = max(100, min(200000, int(max_results)))
        except (TypeError, ValueError):
            self.max_results = DEFAULT_MAX_RESULTS
        # 体积过滤：与整理任务同一套语义，0 表示这一端不限
        self.min_bytes = max(0, int(min_bytes or 0))
        self.max_bytes = max(0, int(max_bytes or 0))
        self._cancel = False

    def _size_allowed(self, size):
        if size <= 0:
            return True
        if self.min_bytes and size < self.min_bytes:
            return False
        if self.max_bytes and size > self.max_bytes:
            return False
        return True

    def cancel(self):
        self._cancel = True

    def _excluded(self, lower_name, full_path):
        if lower_name in self.exclude_names:
            return True
        if self.exclude_paths:
            key = os.path.normcase(os.path.normpath(full_path))
            for excluded in self.exclude_paths:
                if key == excluded or key.startswith(excluded.rstrip("\\/") + os.sep):
                    return True
        return False

    def run(self):
        started = time.time()
        stats = {"total": 0, "dirs": 0, "cancelled": False,
                 "truncated": False, "error": "", "seconds": 0.0}
        seen = set()
        visited_dirs = set()
        batch = []
        found = 0
        dirs = 0
        stack = list(reversed(self.roots))

        try:
            while stack and not self._cancel and found < self.max_results:
                current = stack.pop()
                dirs += 1
                current_key = os.path.normcase(os.path.normpath(current))
                if current_key in visited_dirs:
                    continue
                visited_dirs.add(current_key)
                if self.dst_dir and _under(current, self.dst_dir):
                    continue
                if self._excluded(os.path.basename(current).lower(), current):
                    continue
                subdirs = []
                try:
                    with os.scandir(current) as entries:
                        for entry in entries:
                            if self._cancel or found >= self.max_results:
                                break
                            try:
                                name = entry.name
                                lower = name.lower()
                                full = entry.path
                                if entry.is_dir(follow_symlinks=False):
                                    if self._excluded(lower, full):
                                        continue
                                    if _is_reparse(entry):
                                        continue
                                    if self.skip_hidden and _hidden_entry(entry):
                                        continue
                                    if self.dst_dir and _under(full, self.dst_dir):
                                        continue
                                    key = os.path.normcase(os.path.normpath(full))
                                    if key in visited_dirs:
                                        continue
                                    subdirs.append(full)
                                else:
                                    if entry.is_symlink():
                                        continue
                                    if os.path.splitext(name)[1].lower().lstrip(".") not in self.exts:
                                        continue
                                    if self.skip_hidden and _hidden_entry(entry):
                                        continue
                                    if self._excluded(lower, full):
                                        continue
                                    key = os.path.normcase(os.path.normpath(full))
                                    if key in seen:
                                        continue
                                    seen.add(key)
                                    try:
                                        size = entry.stat(follow_symlinks=False).st_size
                                    except OSError:
                                        size = 0
                                    # 体积不在范围的直接不收录：全盘搜索列表里少一堆点不动的条目
                                    if not self._size_allowed(size):
                                        continue
                                    # 不加 \\?\ 前缀：回收站、Shell 打开、路径规范化都依赖普通路径，
                                    # 超长路径仍是已知限制（README 已注明），不做半吊子支持。
                                    batch.append((found, name, size,
                                                  os.path.dirname(full), full))
                                    found += 1
                            except OSError:
                                continue
                except OSError as exc:
                    self.log.emit("[跳过] 无法读取 {0}：{1}".format(current, exc))

                # 「仅当前目录」模式不再往下走：子目录只用于递归扫描
                if self.recursive:
                    for path in reversed(subdirs):
                        stack.append(path)

                if len(batch) >= 50:
                    self.items.emit(batch)
                    batch = []
                if dirs % 20 == 0:
                    self.progress.emit(dirs, found)
        except Exception as exc:  # noqa: BLE001 - 兜底，保证线程一定发结束信号
            stats["error"] = "{0}: {1}".format(type(exc).__name__, exc)
            self.log.emit("[异常] 全盘搜索中止：" + stats["error"])

        if batch:
            self.items.emit(batch)
        stats["total"] = found
        stats["dirs"] = dirs
        stats["cancelled"] = bool(self._cancel)
        # 取消与截断互斥：用户主动取消时不再报「已达上限」
        stats["truncated"] = not stats["cancelled"] and found >= self.max_results
        stats["seconds"] = time.time() - started
        self.progress.emit(dirs, found)
        self.finished_stats.emit(stats)


# --------------------------------------------------------------------------- #
# 盘符勾选对话框
# --------------------------------------------------------------------------- #
class ScanAllDialog(QDialog):
    """手动勾选盘符；默认一个都不选，避免误扫整台机器。"""

    def __init__(self, parent=None, saved_roots=None, excludes="",
                 max_results=DEFAULT_MAX_RESULTS):
        super().__init__(parent)
        self.setWindowTitle("全盘搜索")
        self.setMinimumWidth(400)
        c = theme.COLORS
        self.setStyleSheet(
            "QDialog {{ background: {bg}; color: {text}; }}"
            "QLabel {{ color: {dim}; font-size: 12px; }}"
            "QCheckBox {{ color: {text}; font-size: 12px; padding: 3px 0; spacing: 8px; }}"
            "QCheckBox::indicator {{ width: 15px; height: 15px; border-radius: 5px;"
            " border: 1px solid {border}; background: {input}; }}"
            "QCheckBox::indicator:checked {{ background-color: {accent}; border-color: {accent}; }}"
            "QCheckBox::indicator:hover {{ border-color: {accent}; }}"
            "QCheckBox:disabled {{ color: {dim}; }}"
            "QCheckBox::indicator:disabled {{ border-color: {border}; background: transparent; }}"
            "QLineEdit, QSpinBox {{ background: {input}; color: {text}; border: 1px solid {border};"
            " border-radius: 8px; padding: 6px 8px; font-size: 12px; }}"
            "QPushButton {{ background: {panel}; color: {text}; border: 1px solid {border};"
            " border-radius: 9px; padding: 8px 16px; font-size: 12px; }}"
            "QPushButton#ok {{ background: {accent}; color: {onaccent}; border: 0; }}"
            .format(bg=c["bg_2"], text=c["text"], dim=c["text_dim"],
                    input=c["input_bg"], border=c["border_strong"],
                    panel=c["panel_solid"], accent=c["accent"],
                    onaccent=c["text_on_accent"]))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        head = QLabel("选择要搜索的磁盘（默认全不选）", self)
        head.setStyleSheet("color: %s; font-size: 14px; font-weight: 600;" % c["text"])
        layout.addWidget(head)
        note = QLabel("搜索全程只读：不会移动、复制或删除任何文件。\n"
                      "系统目录、隐藏项、链接目录与目标文件夹会自动跳过。", self)
        note.setWordWrap(True)
        layout.addWidget(note)

        line = QFrame(self)
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: %s;" % c["border"])
        layout.addWidget(line)

        self._boxes = []
        for drive in list_drives():
            box = QCheckBox(drive["label"], self)
            box.setEnabled(drive["selectable"])
            box.setProperty("drivePath", drive["path"])
            # 每次打开都全不选：盘符由用户手动勾选，避免误扫整台机器
            box.setChecked(False)
            if not drive["selectable"]:
                box.setToolTip("网络盘 / 光驱 / 内存盘不支持全盘搜索")
            layout.addWidget(box)
            self._boxes.append(box)
        if not self._boxes:
            layout.addWidget(QLabel("没有检测到可用磁盘", self))

        layout.addSpacing(4)
        layout.addWidget(QLabel("额外排除的目录名或路径（逗号分隔，可选）", self))
        self.exclude_edit = QLineEdit(excludes or "", self)
        self.exclude_edit.setPlaceholderText("例如：临时素材, D:\\归档")
        layout.addWidget(self.exclude_edit)

        row = QHBoxLayout()
        row.addWidget(QLabel("最多列出", self))
        self.max_spin = QSpinBox(self)
        self.max_spin.setRange(100, 200000)
        self.max_spin.setSingleStep(1000)
        self.max_spin.setValue(int(max_results))
        row.addWidget(self.max_spin)
        row.addWidget(QLabel("个视频", self))
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消", self)
        ok = QPushButton("开始搜索", self)
        ok.setObjectName("ok")
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

    def selected_roots(self):
        return [box.property("drivePath") for box in self._boxes
                if box.isEnabled() and box.isChecked()]

    def excludes_text(self):
        return self.exclude_edit.text().strip()

    def max_results(self):
        return self.max_spin.value()

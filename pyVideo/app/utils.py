#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用工具函数
============

从原来的单文件重构而来，包含：
- 文件处理：human_size、format_duration、is_hidden、unique_target、norm_path
- 回收站：send_to_recycle_bin（Windows SHFileOperation，可还原）
- 去重签名：quick_signature
- 缩略图缓存：ThumbnailCache（基于 md5 的本地 JPG 缓存）
"""

import ctypes
import hashlib
import os
import stat
import struct
import sys

from PySide6.QtCore import QThread, Qt, Signal, QMutex, QMutexLocker
from PySide6.QtGui import QImage


# --------------------------------------------------------------------------- #
# 资源路径与应用图标
# --------------------------------------------------------------------------- #
def resource_path(*parts):
    """
    定位随程序一起分发的资源文件。

    - 源码运行：<项目根>/assets/...
    - PyInstaller 打包后：<_MEIPASS>/assets/...
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def app_icon():
    """
    应用图标。优先用多尺寸 .ico：标题栏/任务栏/Alt-Tab 各取最合适的一档，
    不会像单张 png 那样被缩放糊掉；找不到 ico 时退回 png。
    """
    from PySide6.QtGui import QIcon

    for name in ("app.ico", "app.png"):
        path = resource_path("assets", name)
        if os.path.exists(path):
            return QIcon(path)
    return QIcon()


# --------------------------------------------------------------------------- #
# 文件格式化
# --------------------------------------------------------------------------- #
def human_size(num):
    """把字节数格式化成易读的大小。"""
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "-"
    if num < 1024:
        return f"{int(num)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        num /= 1024.0
        if num < 1024:
            return f"{num:.1f} {unit}"
    return f"{num:.1f} PB"


def format_duration(ms):
    """毫秒 -> mm:ss（超过 1 小时显示 h:mm:ss）。"""
    seconds = max(int(ms or 0), 0) // 1000
    if seconds >= 3600:
        return "{0}:{1:02d}:{2:02d}".format(seconds // 3600, seconds // 60 % 60, seconds % 60)
    return "{0:02d}:{1:02d}".format(seconds // 60, seconds % 60)


def describe_error(exc, path=""):
    """
    把 OSError 翻成更可操作的中文提示。

    重点是"路径过长"：没开长路径支持时 Win32 只抛 ENOENT/ENAMETOOLONG，
    原样显示会让人以为是文件被删了。这里按长度判断并直接给出开关名。
    """
    if isinstance(exc, OSError):
        err = getattr(exc, "errno", None)
        where = path or ""
        if len(where) < 260:
            where = getattr(exc, "filename", "") or where
        if err in (3, 123, 206) and len(where) >= 260:
            return "路径超过 260 字符，且系统未开启长路径支持（LongPathsEnabled）"
        if err == 32:
            return "文件正被其他程序占用"
        if err == 5:
            return "没有访问权限（目标目录只读或被占用）"
    return str(exc)


def is_hidden(path):
    """判断文件或文件夹是否为隐藏/系统项。"""
    name = os.path.basename(path)
    if name.startswith("."):
        return True
    if os.name == "nt":
        try:
            attrs = os.stat(path).st_file_attributes
            flags = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM
            return bool(attrs & flags)
        except (OSError, AttributeError):
            return False
    return False


def unique_target(dst_dir, filename, reserved):
    """
    在 dst_dir 下给 filename 找一个不冲突的完整路径。
    reserved 用于避免同一次任务内部相互撞名（大小写不敏感）。
    """
    stem, ext = os.path.splitext(filename)
    candidate = filename
    index = 1
    while True:
        full = os.path.join(dst_dir, candidate)
        if not os.path.exists(full) and candidate.lower() not in reserved:
            reserved.add(candidate.lower())
            return full
        candidate = "{0} ({1}){2}".format(stem, index, ext)
        index += 1


def norm_path(path):
    """
    规范化路径用于比较：统一大小写/分隔符，并用 realpath 展开符号链接与
    Windows 8.3 短名，避免"目标目录就在源目录里"因路径写法不同而识别失败。
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


# --------------------------------------------------------------------------- #
# Windows 回收站
# --------------------------------------------------------------------------- #
FO_DELETE = 3
FOF_ALLOWUNDO = 0x0040
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_NOERRORUI = 0x0400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def send_to_recycle_bin(path):
    """
    把单个文件移入 Windows 回收站（可还原）。成功返回空字符串，失败返回原因。
    非 Windows 平台直接删除（调用方在界面上已确认过）。
    回收站不可用时（比如网络盘）这里会返回错误，不会偷偷改成永久删除。
    """
    if os.name != "nt":
        try:
            os.remove(path)
        except OSError as exc:
            return str(exc)
        return ""

    if not os.path.isfile(path):
        return "文件不存在"
    absolute = os.path.abspath(path)
    # 路径以双 NUL 结尾（SHFileOperation 要求以 NUL 分隔的路径列表）
    buffer = ctypes.create_unicode_buffer(absolute + "\0")
    operation = _SHFILEOPSTRUCTW()
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.cast(buffer, ctypes.c_wchar_p)
    operation.fFlags = FOF_ALLOWUNDO | FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI
    try:
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    except Exception as exc:  # noqa: BLE001 - 系统调用失败不应该让界面崩掉
        return str(exc)
    if result != 0:
        return "移入回收站失败（错误码 {0}）".format(result)
    if operation.fAnyOperationsAborted:
        return "操作被系统取消（文件可能正被占用）"
    if os.path.exists(absolute):
        return "系统未删除该文件（可能正被占用）"
    return ""


# --------------------------------------------------------------------------- #
# 去重签名
# --------------------------------------------------------------------------- #
FINGERPRINT_CHUNK = 64 * 1024


def fingerprint_ranges(size, chunk=FINGERPRINT_CHUNK):
    """
    采样区间 [(offset, length), ...]：首、中、尾各 chunk 字节。

    只取首尾时，两个仅"中间部分不同"的视频（例如同片头同片尾、正片不同的合集）
    会算出同一个指纹，从而被误判为重复而跳过整理。中间再取一段可以把这种
    碰撞概率压到可以忽略，代价只是多读 64KB。

    小文件直接整块读，避免区间重叠重复读；超过两段就必定带上中间段——
    否则 128KB~192KB 的文件只比较首尾，仅中间内容不同的两个视频会撞成同一
    指纹而被误判重复、直接跳过（复制模式下等于漏文件）。
    """
    if size <= 0:
        return []
    if size <= chunk * 2:
        return [(0, size)]
    ranges = [(0, chunk)]
    ranges.append((size // 2 - chunk // 2, chunk))
    ranges.append((size - chunk, chunk))
    return ranges


def quick_signature(path, size):
    """
    便宜的内容指纹：文件大小 + 首/中/尾三段内容（每段 64KB）的 blake2b。

    用来确认"目标里已存在的同名文件"是不是同一个视频，避免误跳过。
    区间偏移量也参与哈希，防止不同布局被拼成同一串字节。
    """
    digest = hashlib.blake2b(digest_size=16)
    ranges = fingerprint_ranges(size)
    digest.update(struct.pack("<QQ", int(size), len(ranges)))
    with open(path, "rb") as fp:
        for offset, length in ranges:
            fp.seek(offset)
            data = fp.read(length)
            digest.update(struct.pack("<QQ", offset, length))
            digest.update(data)
    return digest.digest()


# --------------------------------------------------------------------------- #
# 缩略图（可选依赖 opencv）
# --------------------------------------------------------------------------- #
# 缩略图尺寸：16:9，按 2 倍图生成后由 Qt 缩放，行高 64 时一屏能多显示两行
THUMB_WIDTH = 116
THUMB_HEIGHT = 64


def _load_cv2():
    """尽力加载 cv2；未安装返回 None。缩略图为可选功能。"""
    try:
        import cv2  # noqa: PLC0415
        return cv2
    except Exception:  # noqa: BLE001  (ImportError / OSError 等)
        return None


class ThumbnailCache:
    """
    视频首帧缩略图缓存，位于用户目录 ~/.videocollector/thumbnails/。

    缓存的 key 是文件路径的 md5，带上修改时间/大小，避免文件变动后仍用旧图。
    抓帧失败会写入 `*.nop` 文件作为失败记号，避免反复对坏文件重试。

    容量：缓存是"只增不减"的，全盘搜索几万个视频、每个封面几十 KB，很容易
    涨到几百 MB 甚至上 GB。这里按 mtime 做 LRU：读取时 touch 一下，超出上限
    （默认 256MB）就按"最久没用"的顺序删到限额以内。
    """

    DEFAULT_LIMIT_MB = 256

    def __init__(self, root=None):
        base = root if root is not None else os.path.join(
            os.path.expanduser("~"), ".videocollector", "thumbnails")
        self.root = os.path.abspath(base)
        os.makedirs(self.root, exist_ok=True)

    def _files(self):
        try:
            with os.scandir(self.root) as it:
                for entry in it:
                    if entry.is_file() and (entry.name.endswith(".jpg")
                                            or entry.name.endswith(".nop")):
                        yield entry
        except OSError:
            return

    def size_bytes(self):
        """缓存目录当前占用（只统计自己的缓存文件，公式与清理口径一致）。"""
        total = 0
        for entry in self._files():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
        return total

    def count(self):
        return sum(1 for _ in self._files())

    def prune(self, limit_mb=None, protect=()):
        """按 mtime 从旧到新删除，直到总量不超过 limit_mb；返回释放的字节数。

        protect：正在使用/刚生成的缓存路径集合，不会被删。
        """
        limit = int(limit_mb or self.DEFAULT_LIMIT_MB)
        limit_bytes = limit * 1024 * 1024
        entries = []
        total = 0
        for entry in self._files():
            try:
                st = entry.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, entry.path))
            total += st.st_size
        if total <= limit_bytes:
            return 0
        protected = {os.path.abspath(p) for p in protect}
        entries.sort(key=lambda item: item[0])   # 最久没用的排最前
        freed = 0
        for _mtime, size, path in entries:
            if total - freed <= limit_bytes:
                break
            if os.path.abspath(path) in protected:
                continue
            try:
                os.remove(path)
                freed += size
            except OSError:
                continue
        return freed

    def clear(self):
        """清空整份缓存，返回释放的字节数。"""
        freed = 0
        for entry in self._files():
            try:
                freed += entry.stat().st_size
            except OSError:
                pass
            try:
                os.remove(entry.path)
            except OSError:
                continue
        return freed

    # -- 键 ------------------------------------------------------------- #
    @staticmethod
    def _key(filepath, mtime, size):
        digest = hashlib.md5(
            "{0}\x00{1}\x00{2}".format(os.path.normcase(os.path.abspath(filepath)), mtime, size)
            .encode("utf-8", "surrogateescape")
        ).hexdigest()
        return digest

    def _image_path(self, filepath, with_meta=True):
        meta = ""
        if with_meta:
            try:
                st = os.stat(filepath)
                meta = "\x00{0}\x00{1}".format(int(st.st_mtime), st.st_size)
            except OSError:
                meta = "\x00\x00"
        digest = hashlib.md5(
            (os.path.normcase(os.path.abspath(filepath)) + meta)
            .encode("utf-8", "surrogateescape")
        ).hexdigest()
        return os.path.join(self.root, digest + ".jpg")

    def load(self, filepath):
        """返回缓存缩略图文件路径；无缓存返回 None。

        命中时 touch 一次 mtime，让 LRU 清理知道这张还在用。
        """
        path = self._image_path(filepath)
        if os.path.isfile(path):
            try:
                os.utime(path, None)
            except OSError:
                pass
            return path
        return None

    def save(self, filepath, source_image):
        """把给定 QImage 压缩保存为 JPG 缓存，返回文件路径。

        注意：这里必须用 QImage 而不是 QPixmap——本方法由缩略图后台线程调用，
        QPixmap 只能在 GUI 线程使用（在子线程创建会导致 Qt 直接崩溃）。
        """
        path = self._image_path(filepath)
        try:
            scaled = source_image.scaled(
                THUMB_WIDTH, THUMB_HEIGHT,
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            top = (scaled.height() - THUMB_HEIGHT) // 2
            crop = scaled.copy(
                0, top, min(THUMB_WIDTH, scaled.width()), THUMB_HEIGHT)
            crop.save(path, "JPG", 78)
        except Exception:  # noqa: BLE001
            try:
                source_image.save(path, "JPG", 78)
            except Exception:  # noqa: BLE001
                return None
        return path if os.path.isfile(path) else None

    def mark_bad(self, filepath):
        """标记抓帧失败，写一个 `.nop` 旁路文件，避免反复重试。"""
        key = self._key(filepath, 0, 0)
        try:
            with open(os.path.join(self.root, key + ".nop"), "w") as fp:
                fp.write("1")
        except OSError:
            pass

    def is_bad(self, filepath):
        """是否已被标记为失败。"""
        key = self._key(filepath, 0, 0)
        return os.path.exists(os.path.join(self.root, key + ".nop"))


# --------------------------------------------------------------------------- #
# 缩略图后台线程
# --------------------------------------------------------------------------- #
class ThumbnailWorker(QThread):
    """
    后台逐线程生成视频封面，绝不阻塞 UI。

    信号：
        thumb_ready(str 缓存路径, int row_uid)
    """

    thumb_ready = Signal(str, int)   # 缓存图片路径, 行 uid
    finished_all = Signal()

    def __init__(self, videos, parent=None):
        """
        videos: [(filepath, size, row_uid), ...]
        """
        super().__init__(parent)
        self._videos = list(videos)
        self._cancel = False
        self._cache = ThumbnailCache()
        self._cv2 = _load_cv2()
        self._mutex = QMutex()

    def cancel(self):
        locker = QMutexLocker(self._mutex)
        self._cancel = True
        del locker

    def _is_cancelled(self):
        locker = QMutexLocker(self._mutex)
        cancelled = self._cancel
        del locker
        return cancelled

    def run(self):
        cv2 = self._cv2
        if cv2 is None:
            # 没有 opencv 则直接结束（UI 会显示占位图）
            self.finished_all.emit()
            return

        # 单线程逐条解码在屏幕填满几十张封面时要花上秒；用少量线程并行，
        # 把首帧解码负载摊到多核，滚动长列表补图明显变快。
        # 上限取 2：cv2 解码各核互不干扰，但太多线程抢磁盘 I/O 反而变慢。
        total = len(self._videos)
        n_threads = min(max(1, os.cpu_count() or 2) - 1, 2) if total >= 4 else 1

        if n_threads <= 1:
            for _path, _size, uid in self._videos:
                if self._is_cancelled():
                    break
                img_path = self._generate_one(cv2, _path)
                if img_path:
                    self.thumb_ready.emit(img_path, uid)
            self.finished_all.emit()
            return

        # 各线程解码自己的分片、把结果攒回列表；由本线程统一按 uid 回发信号，
        # 保证 thumb_ready 始终只在同一线程里 emit，避免并发信号竞态。
        results = [[] for _ in range(n_threads)]
        chunk_size = (total + n_threads - 1) // n_threads

        def decode_slice(chunk_index, out):
            start = chunk_index * chunk_size
            end = min(start + chunk_size, total)
            for _path, _size, uid in self._videos[start:end]:
                if self._is_cancelled():
                    break
                img_path = self._generate_one(cv2, _path)
                if img_path:
                    out.append((img_path, uid))

        import threading
        threads = [
            threading.Thread(target=decode_slice, args=(i, results[i]), daemon=True)
            for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for batch in results:
            for img_path, uid in batch:
                self.thumb_ready.emit(img_path, uid)

        self.finished_all.emit()

    def _generate_one(self, cv2, filepath):
        cached = self._cache.load(filepath)
        if cached:
            return cached
        if self._cache.is_bad(filepath):
            return None
        # true statement: capture may block for a while, so we are on a thread.
        cap = cv2.VideoCapture(filepath)
        frame = None
        if cap.isOpened():
            # 读几帧取靠中一点的帧，尽量避开片头黑屏
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            wanted = total // 2 if total > 1 else 0
            if wanted > 0:
                cap.set(cv2.CAP_PROP_POS_FRAMES, wanted)
            ok, frame = cap.read()
            if (not ok or frame is None) and total > 0:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
        cap.release()

        if frame is None or frame.size == 0:
            self._cache.mark_bad(filepath)
            return None

        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            height, width, _ = rgb.shape
            bytes_per_line = 3 * width
            img = QImage(rgb.data, width, height, bytes_per_line, QImage.Format_RGB888).copy()
            if img.isNull():
                self._cache.mark_bad(filepath)
                return None
            # 只使用 QImage（线程安全）；QPixmap 留给 GUI 线程加载缓存后创建
            return self._cache.save(filepath, img)
        except Exception:  # noqa: BLE001
            self._cache.mark_bad(filepath)
            return None
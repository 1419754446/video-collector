#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台工作线程
============

CollectWorker：扫描源目录并复制/移动视频到目标文件夹。
含去重（文件名+大小+首尾内容指纹）、同名重命名/跳过、取消、移动校验。

从原单文件重构而来，逻辑与原版一致。
"""

import os
import shutil
import time

from PySide6.QtCore import QThread, Signal

from .utils import (
    describe_error,
    human_size,
    is_hidden,
    norm_path,
    quick_signature,
    send_to_recycle_bin,
    unique_target,
)


class CollectWorker(QThread):
    scan_count = Signal(int)              # 扫描中：已发现数量
    scan_done = Signal(int)               # 扫描结束：视频总数
    item_added = Signal(int, str, int, str)   # 行号, 文件名, 大小, 来源目录
    item_status = Signal(int, str, str)   # 行号, 状态文字, 目标路径
    progress = Signal(int, int)           # 已处理, 总数
    log = Signal(str)
    done = Signal(dict)                   # 统计结果

    def __init__(self, src_dir, dst_dir, exts, mode, on_conflict,
                 recursive, skip_hidden, dedupe=True, file_list=None,
                 min_bytes=0, max_bytes=0, parent=None):
        super().__init__(parent)
        # file_list 模式：全盘搜索已经把结果列进表格了，这里直接整理选中项，
        # 不再重新扫描源目录、也不再重复插行；其余处理逻辑完全不变。
        self.file_list = list(file_list) if file_list is not None else None
        self.emit_added = self.file_list is None
        self.src = os.path.abspath(src_dir)
        self.dst = os.path.abspath(dst_dir)
        self._dst_norm = norm_path(self.dst)
        self.exts = set(e.lower().lstrip(".") for e in exts)
        self.mode = mode                    # "copy" / "move"
        self.on_conflict = on_conflict      # "rename" / "skip"
        self.recursive = recursive
        self.skip_hidden = skip_hidden
        self.dedupe = dedupe
        # 体积过滤：0 表示这一端不限（等价 C++ backend.h 的 minBytes / maxBytes）
        self.min_bytes = max(0, int(min_bytes or 0))
        self.max_bytes = max(0, int(max_bytes or 0))
        self._cancel = False
        self._reserved = set()
        self._sig_cache = {}
        self._dir_keys = {}
        self._dst_entries = None     # 目标目录文件名快照（首次去重时读取一次）
        self._dst_index = None       # 目标目录文件名索引（小写键 → 文件名列表）

    # -- 对外 ------------------------------------------------------------- #
    def cancel(self):
        self._cancel = True

    def run(self):
        stats = {
            "total": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "in_place": 0,
            "bytes": 0,
            "cancelled": False,
            "error": "",
            "seconds": 0.0,
            "mode": self.mode,
        }
        started = time.time()
        try:
            files = list(self.file_list) if self.file_list is not None else self._scan()
            total = len(files)
            stats["total"] = total
            self.scan_done.emit(total)

            for index, path in enumerate(files):
                if self._cancel:
                    stats["cancelled"] = True
                    break
                if not self.emit_added:
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                self.item_added.emit(index, os.path.basename(path), size, os.path.dirname(path))
            self.progress.emit(0, total)

            for index, path in enumerate(files):
                if self._cancel:
                    stats["cancelled"] = True
                    break
                status, target, size = self._process(path, stats)
                self.item_status.emit(index, status, target)
                if status.startswith("失败"):
                    self.log.emit("[失败] {0}  →  {1}".format(path, status))
                elif status.startswith("已"):
                    self.log.emit("[{0}] {1}  →  {2}".format(status, path, target))
                self.progress.emit(index + 1, total)
        except Exception as exc:  # 兜底，保证 UI 不会卡死
            stats["error"] = "{0}: {1}".format(type(exc).__name__, exc)
            self.log.emit("[异常] 任务中止：" + stats["error"])
        finally:
            # 空列表 / 纯扫描阶段取消时两个循环都不会执行到 cancel 分支，
            # 统一在这里补标，避免「点了取消却报完成」
            if self._cancel:
                stats["cancelled"] = True
            stats["seconds"] = time.time() - started
            self.done.emit(stats)

    # -- 扫描 ------------------------------------------------------------- #
    def _scan(self):
        files = []
        seen = set()
        for root, dirs, names in os.walk(self.src, topdown=True, followlinks=False):
            if self._cancel:
                break
            kept = []
            for d in dirs:
                full = os.path.join(root, d)
                # 目标文件夹如果位于源目录内，必须跳过，否则会自己扫自己
                if self._dir_key(full) == self._dst_norm:
                    continue
                if self.skip_hidden and is_hidden(full):
                    continue
                kept.append(d)
            dirs[:] = kept if self.recursive else []

            for name in names:
                if self._cancel:
                    break
                ext = os.path.splitext(name)[1].lower().lstrip(".")
                if ext not in self.exts:
                    continue
                full = os.path.join(root, name)
                if self.skip_hidden and is_hidden(full):
                    continue
                if self.min_bytes or self.max_bytes:
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        size = 0
                    # 体积过滤在扫描阶段直接剔除：不插行、不计入总数，
                    # 免得表格里出现一堆"跳过"把真正要整理的视频埋掉。
                    if not self._size_allowed(size):
                        continue
                key = os.path.normcase(full)
                if key in seen:
                    continue
                seen.add(key)
                files.append(full)
                if len(files) == 1 or len(files) % 25 == 0:
                    self.scan_count.emit(len(files))
        files.sort(key=lambda p: p.lower())
        return files

    # -- 单个文件处理 ----------------------------------------------------- #
    def _size_allowed(self, size):
        """体积是否落在 [min_bytes, max_bytes] 内；0 表示该端不限。"""
        if size <= 0:
            # 读不到大小（无权限 / 特殊文件）不该被体积规则误伤
            return True
        if self.min_bytes and size < self.min_bytes:
            return False
        if self.max_bytes and size > self.max_bytes:
            return False
        return True

    def _process(self, src, stats):
        name = os.path.basename(src)
        try:
            size = os.path.getsize(src)
        except OSError:
            size = 0

        # 体积不在范围：扫描模式已经在 _scan 里滤掉了，走到这里的是
        # 「整理选中项」（file_list 模式，行号必须与表格一一对应，不能丢行）。
        if not self._size_allowed(size):
            stats["skipped"] += 1
            return "跳过：体积不在范围", src, size

        # 已经躺在目标文件夹里，不用动
        if self._dir_key(os.path.dirname(src)) == self._dst_norm:
            stats["in_place"] += 1
            return "已在目标目录", src, size

        target = ""
        moved = False  # 标记源文件是否已经被移动走
        try:
            base_target = os.path.join(self.dst, name)
            # 防重复：目标里已有同名（含自动重命名后的 xxx (1).mp4 等）且内容一致的文件，直接跳过
            if self.dedupe and size:
                duplicate = self._find_duplicate(src, name, size)
                if duplicate:
                    stats["skipped"] += 1
                    return "同名同内容已在目标中，跳过", duplicate, size

            if self.on_conflict == "skip" and (
                os.path.exists(base_target) or name.lower() in self._reserved
            ):
                stats["skipped"] += 1
                return "同名已存在，已跳过", base_target, size

            verb = "移动" if self.mode == "move" else "复制"
            # 复制/移动 + 磁盘抢占换名重试：目标名在预留后若恰被磁盘上的新文件占用，
            # 则换下一个名字重试（最多 5 次），全程绝不覆盖任何已存在的文件，
            # 等价于 C++ backend.cpp 的 attempt 循环 + CopyFileW/MoveFileExW 不覆盖语义。
            target, moved = self._do_transfer(src, name, size)

            if not os.path.exists(target):
                raise OSError("目标文件没有生成")
            new_size = os.path.getsize(target)
            if size and new_size != size:
                raise OSError(
                    "大小校验不通过（{0} != {1}）".format(human_size(new_size), human_size(size))
                )
            self._remember_target(target)
        except Exception as exc:
            if moved:
                # 源已被移动成功，但后续校验等环节抛了异常：文件实际已到位，
                # 这是该视频的唯一副本，绝不能当失败处理或删除，否则数据丢失。
                try:
                    if os.path.isfile(target) and (
                        not size or os.path.getsize(target) == size
                    ):
                        self._remember_target(target)
                        stats["success"] += 1
                        stats["bytes"] += size
                        return "已" + verb, target, size
                except OSError:
                    pass
                stats["failed"] += 1
                return "失败：{0}".format(describe_error(exc, target or src)), target, size

            stats["failed"] += 1
            self._cleanup_partial(target, size, moved)
            return "失败：{0}".format(describe_error(exc, target or src)), target, size

        stats["success"] += 1
        stats["bytes"] += size
        return "已" + verb, target, size

    def _do_transfer(self, src, name, size):
        """
        分配目标名并执行复制/移动，带"磁盘抢占换名重试"。

        与 C++ backend.cpp 的 attempt 循环一致：
          - 每个候选名先用 unique_target 预留（内存 reserved 去重），再落盘执行；
          - 若预留的瞬间目标名恰被磁盘上的第三方/外部文件占据（FileExistsError），
            换下一个候选名重试，最多 5 次；
          - 复制用 O_EXCL 原子占用保证绝不覆盖（等价 CopyFileW(..., TRUE)）；
            移动用 shutil.move（同卷 rename 在目标已存在时抛 FileExistsError，不覆盖，
            跨卷行为与 C++ MoveFileExW(COPY_ALLOWED) 一致）。
        5 次仍被抢占则抛异常，交由上层按失败处理。
        """
        moved = False
        last_target = ""
        for _attempt in range(5):
            target = unique_target(self.dst, name, self._reserved)
            last_target = target
            try:
                if self.mode == "move":
                    if not os.path.exists(src):
                        raise OSError("源文件不存在，无法移动")
                    shutil.move(src, target)
                    moved = True
                else:
                    self._copy_noclobber(src, target)
                return target, moved
            except FileExistsError:
                # 预留后目标名被磁盘新文件抢占：换下一个名字重试
                continue
            except PermissionError:
                # 文件被别的进程占用：最典型的就是用户正在右侧预览这个视频，
                # 播放器握着读句柄，Windows 不允许此时 rename / 覆盖（WinError 32）。
                # 先清掉 copy 留下的占位，短暂等待后重试同一个候选流程。
                if self.mode != "move":
                    self._cleanup_partial(last_target, size, False)
                if _attempt < 4:
                    time.sleep(0.5)
                    continue
                raise OSError(
                    "文件正被占用（可能正在预览或其它程序打开），请稍后重试")
            except Exception:
                # copy 模式下 os.open 留下的 O_EXCL 占位 / copy2 半成品此时仍在目标里；
                # 源文件没有动过，安全清掉这个不完整的目标，绝不把它留给去重判断。
                if self.mode != "move":
                    self._cleanup_partial(last_target, size, False)
                raise
        # 5 次仍被抢占，视为失败（保留 reserved 记号，避免后续重复占用）
        raise OSError("目标名称被占用的重试次数用尽，仍未能创建")

    @staticmethod
    def _copy_noclobber(src, target):
        """
        绝不覆盖的复制：先以 O_EXCL 原子创建目标占位（目标已存在则直接抛
        FileExistsError），再用 shutil.copy2 写进这个属于我们的空文件。
        若 copy2 中途失败，残留的占位/半成品由 _cleanup_partial 在 copy 模式清理。
        """
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        os.close(fd)
        shutil.copy2(src, target)

    def _dir_key(self, path):
        """目录路径比较键（带缓存，避免每个文件都 realpath 一次）。"""
        key = self._dir_keys.get(path)
        if key is None:
            key = norm_path(path)
            self._dir_keys[path] = key
        return key

    def _dst_snapshot(self):
        """
        目标目录文件名快照：[(原名, 小写名), ...]，只读取一次目录。
        原先每处理一个文件就 listdir 一次，几千个文件时会退化成 O(n²) 的目录扫描。
        """
        if self._dst_entries is None:
            try:
                names = os.listdir(self.dst)
            except OSError:
                names = []
            self._dst_entries = [(n, n.lower()) for n in names]
        return self._dst_entries

    def _remember_target(self, path):
        """把本次新生成的目标文件登记进快照/索引，保证同一次任务内的去重判断依然准确。"""
        if self._dst_entries is None:
            return
        name = os.path.basename(path)
        lower = name.lower()
        self._dst_entries.append((name, lower))
        if self._dst_index is not None:
            self._index_name(self._dst_index, name, lower)

    @staticmethod
    def _index_name(index, name, lower):
        """
        把一个目标文件名登记进索引，与 C++ 版 dupIndex_ 语义一致：
          - 小写全名（精确同名）
          - 小写 stem（去掉扩展名）
          - stem 中每个 " (" 处截断出的基础名（覆盖 "foo (1) (2)" 这类多级重命名）
        """
        stem = os.path.splitext(lower)[0]
        index.setdefault(lower, []).append(name)
        index.setdefault(stem, []).append(name)
        pos = stem.find(" (")
        while pos != -1:
            index.setdefault(stem[:pos], []).append(name)
            pos = stem.find(" (", pos + 1)

    def _dst_index_map(self):
        if self._dst_index is None:
            index = {}
            for name, lower in self._dst_snapshot():
                self._index_name(index, name, lower)
            self._dst_index = index
        return self._dst_index

    def _signature(self, path, size):
        """带缓存的快速内容指纹，避免同一个目标文件被反复读。"""
        key = (norm_path(path), size)
        if key in self._sig_cache:
            return self._sig_cache[key]
        sig = quick_signature(path, size)
        self._sig_cache[key] = sig
        return sig

    def _find_duplicate(self, src, filename, size):
        """在目标文件夹里找内容相同、名字相同（含 xxx (1).mp4 系列）的文件。

        先按索引取候选（万级文件时把每文件的 O(n) 全表扫描降成 O(1)），
        再用与原来完全相同的判定式过滤，语义不变。
        """
        stem, ext = os.path.splitext(filename)
        stem_lower = stem.lower()
        ext_lower = ext.lower()
        prefix = stem_lower + " ("
        filename_lower = filename.lower()

        index = self._dst_index_map()
        seen = set()
        candidates = []
        for key in (filename_lower, stem_lower):
            for name in index.get(key, ()):  # 两个键可能有交集，去重
                if name in seen:
                    continue
                seen.add(name)
                candidates.append(name)

        matched = []
        for name in candidates:
            lower = name.lower()
            if lower == filename_lower:
                matched.append(name)
            elif lower.startswith(prefix) and lower.endswith(ext_lower):
                matched.append(name)

        same_size = []
        for candidate in matched:
            full = os.path.join(self.dst, candidate)
            try:
                if os.path.isfile(full) and os.path.getsize(full) == size:
                    same_size.append(full)
            except OSError:
                continue
        if not same_size:
            return None

        try:
            src_sig = self._signature(src, size)
        except OSError:
            return None

        for full in same_size:
            try:
                if self._signature(full, size) == src_sig:
                    return full
            except OSError:
                continue
        return None

    @staticmethod
    def _cleanup_partial(target, size, moved):
        """
        清理失败时残留的"半成品"目标文件。

        仅在 copy 模式下清理：copy2 可能只写了一半，此时删除不完整的目标是安全的，
        因为源文件仍在原处。move 模式下如果源已被移走（moved=True），绝不能再删目标，
        否则该视频会彻底丢失——此时把"已是完整目标"当作一次成功即可（由调用方决定）。
        """
        if not target:
            return
        if not size:
            # 与 C++ backend.cpp 的 needCleanup 前置一致：0 字节源不做半成品清理，
            # 避免"0 != size 恒真"把本就应有的 0 字节目标误删。
            return
        try:
            if moved:
                return
            if os.path.exists(target):
                if os.path.getsize(target) != size:
                    os.remove(target)
        except OSError:
            pass


class DeleteWorker(QThread):
    """
    后台删除线程：把一批文件逐项移入回收站，避免大批量删除时阻塞 GUI。

    C++ 版删除跑在独立后台线程（main.cpp g_deleteThread + g_deleteMutex 串行化），
    这里用 QThread + 信号回传做等价实现。

    信号：
        deleted(str)                    # 删除成功的路径
        failed(str, str)                # 删除失败：路径, 原因
        finished_all(int, int)          # 成功数, 失败数
    """

    deleted = Signal(str)
    failed = Signal(str, str)
    finished_all = Signal(int, int)

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self._paths = list(paths)

    def run(self):
        ok = 0
        fail = 0
        for path in self._paths:
            error = send_to_recycle_bin(path)
            if error:
                self.failed.emit(path, error)
                fail += 1
            else:
                self.deleted.emit(path)
                ok += 1
        self.finished_all.emit(ok, fail)


class UndoWorker(QThread):
    """
    撤销「本次整理」的后台线程。

    对每个本条整理真正生成的副本（target_created）：
        copy 模式 -> 把目标副本移入回收站（源保持在原位置）
        move 模式 -> 把目标副本移回源路径（move 已把源移走，必须回移才有完整还原）

    信号：
        processed(dict, bool, str)       # 动作记录, 是否成功, 结果说明
        failed(str, str)                 # 路径, 原因
        finished_all(int, int)           # 成功数, 失败数
    """

    processed = Signal(object, bool, str)
    failed = Signal(str, str)
    finished_all = Signal(int, int)

    def __init__(self, snapshot, parent=None):
        super().__init__(parent)
        self._snapshot = list(snapshot)

    def run(self):
        ok = 0
        fail = 0
        for action in self._snapshot:
            src = action.get("path", "")
            target = action.get("target", "")
            mode = action.get("mode", "copy")
            try:
                if not target or not os.path.exists(target):
                    # 目标已被外部改动/删除，视为无需处理
                    ok += 1
                    self.processed.emit(action, True, "目标副本不存在")
                    continue
                if mode == "move" and src and os.path.dirname(target):
                    # move：源已被移走，把目标副本移回源路径（跨卷 fallback 用 replace）
                    parent = os.path.dirname(src)
                    if parent and not os.path.isdir(parent):
                        os.makedirs(parent, exist_ok=True)
                    if os.path.exists(src):
                        # 源位置出现了新文件，避免覆盖：先回收站删除目标副本
                        error = send_to_recycle_bin(target)
                        if error:
                            raise OSError(error)
                        note = "源位置已被占用，已改为删除目标副本"
                    else:
                        os.replace(target, src)
                        note = "已移回源位置"
                else:
                    # copy：源仍在原位，只把目标副本移入回收站
                    error = send_to_recycle_bin(target)
                    if error:
                        raise OSError(error)
                    note = "已删除本次生成的副本"
                ok += 1
                self.processed.emit(action, True, note)
            except OSError as exc:
                fail += 1
                self.processed.emit(action, False, str(exc))
                self.failed.emit(target or src, str(exc))
        self.finished_all.emit(ok, fail)
// backend.cpp —— 扫描 / 复制 / 移动 / 去重 的实现
#include "backend.h"

#include <algorithm>
#include <cstdio>

namespace {

std::wstring Lower(std::wstring s) {
    for (wchar_t& c : s) c = static_cast<wchar_t>(std::towlower(c));
    return s;
}

bool HiddenByAttr(const std::wstring& name, DWORD attr) {
    if (!name.empty() && name[0] == L'.') return true;
    return (attr & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)) != 0;
}

std::string FailText(const char* ctx, DWORD code) {
    return std::string("失败：") + ctx + "（错误 " + std::to_string(code) + "）";
}

std::string Num(uint64_t v) {
    char buf[32]{};
    snprintf(buf, sizeof(buf), "%llu", static_cast<unsigned long long>(v));
    return std::string(buf);
}

std::wstring NormForCompare(std::wstring s) {
    for (wchar_t& c : s) {
        if (c == L'/') c = L'\\';
        c = static_cast<wchar_t>(std::towlower(c));
    }
    while (s.size() > 3 && s.back() == L'\\') s.pop_back();
    return s;
}

bool UnderPath(const std::wstring& path, const std::wstring& base) {
    if (base.empty()) return false;
    const std::wstring p = NormForCompare(path);
    const std::wstring b = NormForCompare(base);
    if (p == b) return true;
    return p.size() > b.size() && p.compare(0, b.size(), b) == 0 && p[b.size()] == L'\\';
}

const wchar_t* const kDefaultExcludeNames[] = {
    L"windows", L"program files", L"program files (x86)", L"programdata",
    L"appdata", L"$recycle.bin", L"system volume information", L"recovery",
    L"perflogs", L"msocache", L"config.msi", L"documents and settings",
    L"node_modules", L".git", L".svn", L".hg", L".venv", L"venv",
    L"__pycache__", L"$windows.~ws", L"$windows.~bt", L"windows.old",
};

}  // namespace

// ---------------------------------------------------------------------------

CollectorJob::CollectorJob(const JobOptions& options, Emit emit, ItemSink sink, Finish finish)
    : opt_(options), emit_(std::move(emit)), sink_(std::move(sink)), finish_(std::move(finish)) {
    dstKey_ = PathKey(opt_.dst);
}

CollectorJob::~CollectorJob() {
    cancel_.store(true);
    if (thread_.joinable()) thread_.join();
}

void CollectorJob::Start() {
    running_.store(true);
    thread_ = std::thread([this]() { Run(); });
}

void CollectorJob::Cancel() { cancel_.store(true); }

bool CollectorJob::WaitDone(unsigned timeoutMs) {
    std::unique_lock<std::mutex> lock(doneMutex_);
    return doneCv_.wait_for(lock, std::chrono::milliseconds(timeoutMs), [this]() { return finished_; });
}

// ---------------------------------------------------------------------------
// 消息输出
// ---------------------------------------------------------------------------
void CollectorJob::EmitItems(int total, const std::vector<ItemInfo>& batch) {
    std::string json = "{\"type\":\"items\",\"total\":" + std::to_string(total) + ",\"items\":[";
    for (size_t i = 0; i < batch.size(); ++i) {
        if (i) json += ",";
        const ItemInfo& it = batch[i];
        json += "{\"id\":" + std::to_string(it.id);
        json += ",\"name\":\"" + JsonW(it.name) + "\"";
        json += ",\"size\":" + Num(it.size);
        json += ",\"dir\":\"" + JsonW(DirNameOf(it.srcPath)) + "\"";
        json += ",\"path\":\"" + JsonW(it.srcPath) + "\"}";
    }
    json += "]}";
    emit_(json);
}

void CollectorJob::EmitStatus(int id, const std::wstring& status,
                              const std::wstring& target, bool targetCreated) {
    std::string json = "{\"type\":\"status\",\"id\":" + std::to_string(id) +
                       ",\"status\":\"" + JsonW(status) + "\"";
    if (!target.empty()) json += ",\"target\":\"" + JsonW(target) + "\"";
    if (targetCreated) json += ",\"target_created\":true";
    json += "}";
    emit_(json);
}

void CollectorJob::EmitProgress(int done, int total, const std::wstring& name) {
    std::string json = "{\"type\":\"progress\",\"done\":" + std::to_string(done) +
          ",\"total\":" + std::to_string(total);
    if (!name.empty()) json += ",\"name\":\"" + JsonW(name) + "\"";
    json += "}";
    emit_(json);
}

void CollectorJob::EmitLog(const std::string& text, const std::string& level) {
    emit_("{\"type\":\"log\",\"level\":\"" + level + "\",\"text\":\"" + JsonEscape(text) + "\"}");
}

void CollectorJob::EmitDone(const JobStats& s) {
    std::string json = "{\"type\":\"done\",\"stats\":{";
    json += "\"total\":" + std::to_string(s.total);
    json += ",\"success\":" + std::to_string(s.success);
    json += ",\"failed\":" + std::to_string(s.failed);
    json += ",\"skipped\":" + std::to_string(s.skipped);
    json += ",\"inplace\":" + std::to_string(s.inplace);
    json += ",\"bytes\":" + Num(s.bytes);
    json += std::string(",\"cancelled\":") + (s.cancelled ? "true" : "false");
    json += ",\"error\":\"" + JsonEscape(s.error) + "\"";
    char sec[32]{};
    snprintf(sec, sizeof(sec), "%.2f", s.seconds);
    json += ",\"seconds\":" + std::string(sec);
    json += std::string(",\"mode\":\"") + (opt_.move ? "move" : "copy") + "\"}}";
    emit_(json);
}

// ---------------------------------------------------------------------------
// 全盘只读搜索：多根栈式遍历，只上报结果，绝不写盘
// ---------------------------------------------------------------------------
void CollectorJob::RunScanOnly(JobStats& stats) {
    std::set<std::wstring> extSet;
    for (const auto& e : opt_.exts) extSet.insert(Lower(e));

    std::set<std::wstring> excludeNames;
    for (const auto* name : kDefaultExcludeNames) excludeNames.insert(name);
    for (const auto& name : opt_.excludeNames) excludeNames.insert(Lower(name));

    std::vector<std::wstring> stack(opt_.roots.rbegin(), opt_.roots.rend());
    std::set<std::wstring> seen;
    std::set<std::wstring> visitedDirs;
    std::vector<ItemInfo> batch;
    int found = 0;
    int dirs = 0;

    while (!stack.empty() && !cancel_.load() && found < opt_.maxResults) {
        const std::wstring dir = stack.back();
        stack.pop_back();
        ++dirs;
        const std::wstring dirKey = NormForCompare(dir);
        if (!visitedDirs.insert(dirKey).second) continue;
        if (UnderPath(dir, opt_.dstPath)) continue;
        {
            bool excluded = false;
            for (const auto& p : opt_.excludePaths) {
                if (UnderPath(dir, p) || dirKey == NormForCompare(p)) {
                    excluded = true;
                    break;
                }
            }
            if (excluded) continue;
        }

        WIN32_FIND_DATAW fd{};
        HANDLE h = FindFirstFileW(JoinPath(dir, L"*").c_str(), &fd);
        if (h == INVALID_HANDLE_VALUE) {
            EmitLog("[跳过] 无法读取 " + WideToUtf8(dir), "warn");
            continue;
        }

        std::vector<std::wstring> subdirs;
        do {
            const std::wstring name = fd.cFileName;
            if (name == L"." || name == L"..") continue;
            if (cancel_.load() || found >= opt_.maxResults) break;
            const std::wstring full = JoinPath(dir, name);
            const bool isDir = (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
            if (isDir) {
                const std::wstring lower = Lower(name);
                if (excludeNames.count(lower) != 0) continue;
                if (fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) continue;
                if (opt_.skipHidden && HiddenByAttr(name, fd.dwFileAttributes)) continue;
                if (UnderPath(full, opt_.dstPath)) continue;
                bool excluded = false;
                for (const auto& p : opt_.excludePaths) {
                    if (UnderPath(full, p) || NormForCompare(full) == NormForCompare(p)) {
                        excluded = true;
                        break;
                    }
                }
                if (excluded) continue;
                if (visitedDirs.count(NormForCompare(full)) != 0) continue;
                subdirs.push_back(full);
            } else {
                // 同上：只跳过目录软链，云占位符文件要照常收录
                if (opt_.skipHidden && HiddenByAttr(name, fd.dwFileAttributes)) continue;
                if (extSet.count(Lower(ExtOf(name))) == 0) continue;
                {
                    bool excluded = false;
                    for (const auto& p : opt_.excludePaths) {
                        if (NormForCompare(full) == NormForCompare(p)) {
                            excluded = true;
                            break;
                        }
                    }
                    if (excluded) continue;
                }
                const std::wstring key = NormForCompare(full);
                if (!seen.insert(key).second) continue;
                bool ok = false;
                const uint64_t size = FileSizeOf(full, &ok);
                if (!opt_.SizeAllowed(ok ? size : 0)) continue;
                ItemInfo it;
                it.id = found;
                it.name = name;
                // 不加 \\?\ 前缀：回收站、Shell 打开、PathKey 都依赖普通路径；
                // 超长路径仍是已知限制，不做半吊子支持（与 Python 版一致）。
                it.srcPath = full;
                it.size = ok ? size : 0;
                batch.push_back(it);
                sink_(it);
                ++found;
            }
            if (cancel_.load()) break;
        } while (FindNextFileW(h, &fd));
        FindClose(h);

        // 指定目录搜索时按「包含子文件夹」开关决定是否深入；全盘搜索不受影响（recursive 默认开）
        if (opt_.recursive) {
            for (auto it = subdirs.rbegin(); it != subdirs.rend(); ++it) stack.push_back(*it);
        }

        if (batch.size() >= 50) {
            EmitItems(found, batch);
            batch.clear();
        }
        if (dirs % 20 == 0) EmitScanProgress(dirs, found);
    }

    if (!batch.empty()) EmitItems(found, batch);
    stats.total = found;
    stats.dirs = dirs;
    EmitScanProgress(dirs, found);
    EmitLog("搜索结束，共找到 " + std::to_string(found) + " 个视频");
}

void CollectorJob::EmitScanProgress(int dirs, int found) {
    emit_("{\"type\":\"scan_progress\",\"dirs\":" + std::to_string(dirs) +
          ",\"found\":" + std::to_string(found) + "}");
}

void CollectorJob::EmitScanDone(const JobStats& s) {
    std::string json = "{\"type\":\"scan_done\",\"stats\":{";
    json += "\"total\":" + std::to_string(s.total);
    json += ",\"dirs\":" + std::to_string(s.dirs);
    json += ",\"seconds\":" + std::to_string(s.seconds);
    json += ",\"cancelled\":" + std::string(s.cancelled ? "true" : "false");
    json += ",\"truncated\":" + std::string(s.truncated ? "true" : "false");
    json += ",\"error\":\"" + JsonEscape(s.error) + "\"}}";
    emit_(json);
}

// ---------------------------------------------------------------------------
// 去重与命名
// ---------------------------------------------------------------------------
// 把一个目标文件名登记进 dupIndex_：
//  - 小写全名（精确同名查询用）
//  - 小写 stem（去掉扩展名）
//  - stem 中每个 " (" 出现处截断出的基础名（覆盖 "foo (1) (2)" 这类多级重命名）
void CollectorJob::IndexDstName(size_t idx, const std::wstring& lower) {
    dupIndex_[lower].push_back(idx);
    const std::wstring stem = Lower(StemOf(dstNames_[idx]));
    dupIndex_[stem].push_back(idx);
    for (size_t p = stem.find(L" ("); p != std::wstring::npos; p = stem.find(L" (", p + 1)) {
        dupIndex_[stem.substr(0, p)].push_back(idx);
    }
}

void CollectorJob::LoadDstSnapshot() {
    if (dstSnapshotLoaded_) return;
    dstSnapshotLoaded_ = true;
    WIN32_FIND_DATAW fd{};
    HANDLE h = FindFirstFileW(JoinPath(opt_.dst, L"*").c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE) return;
    do {
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) continue;
        dstNames_.push_back(fd.cFileName);
        dstNamesLower_.push_back(Lower(fd.cFileName));  // 每个文件只转一次小写
        IndexDstName(dstNames_.size() - 1, dstNamesLower_.back());
    } while (FindNextFileW(h, &fd));
    FindClose(h);
}

std::string CollectorJob::SignatureOf(const std::wstring& path, uint64_t size) {
    std::wstring key = PathKey(path);
    auto it = sigCache_.find(key + L"|" + std::to_wstring(size));
    if (it != sigCache_.end()) return it->second;
    std::string sig;
    if (!FileFingerprint(path, size, &sig)) sig.clear();
    sigCache_[key + L"|" + std::to_wstring(size)] = sig;
    return sig;
}

bool CollectorJob::FindDuplicate(const std::wstring& src, const std::wstring& name,
                                 uint64_t size, std::wstring* found) {
    LoadDstSnapshot();
    const std::wstring stem = Lower(StemOf(name));
    const size_t dotPos = name.find_last_of(L'.');
    const std::wstring ext = dotPos == std::wstring::npos ? std::wstring() : Lower(name.substr(dotPos));
    const std::wstring prefix = stem + L" (";
    const std::wstring nameLower = Lower(name);

    // 从索引取候选（全名精确匹配 + stem 前缀系列），再按原判定式过滤，语义不变
    std::vector<size_t> candidates;
    auto addAll = [&candidates](const std::vector<size_t>& v) {
        for (size_t i : v) {
            if (std::find(candidates.begin(), candidates.end(), i) == candidates.end())
                candidates.push_back(i);
        }
    };
    auto it = dupIndex_.find(nameLower);
    if (it != dupIndex_.end()) addAll(it->second);
    it = dupIndex_.find(stem);
    if (it != dupIndex_.end()) addAll(it->second);

    std::vector<size_t> matched;
    for (size_t i : candidates) {
        const std::wstring& lower = dstNamesLower_[i];
        if (lower == nameLower) {
            matched.push_back(i);
        } else if (lower.size() > prefix.size() + ext.size() &&
                   lower.compare(0, prefix.size(), prefix) == 0 &&
                   !ext.empty() &&
                   lower.compare(lower.size() - ext.size(), ext.size(), ext) == 0) {
            matched.push_back(i);
        }
    }
    if (matched.empty()) return false;

    std::vector<std::wstring> sameSize;
    for (size_t idx : matched) {
        std::wstring full = JoinPath(opt_.dst, dstNames_[idx]);
        bool ok = false;
        uint64_t sz = FileSizeOf(full, &ok);
        if (ok && sz == size) sameSize.push_back(full);
    }
    if (sameSize.empty()) return false;

    const std::string srcSig = SignatureOf(src, size);
    if (srcSig.empty()) return false;
    for (const auto& full : sameSize) {
        if (SignatureOf(full, size) == srcSig) {
            *found = full;
            return true;
        }
    }
    return false;
}

std::wstring CollectorJob::UniqueTarget(const std::wstring& name) {
    std::wstring stem = StemOf(name);
    std::wstring dotExt;
    const size_t pos = name.find_last_of(L'.');
    if (pos != std::wstring::npos) dotExt = name.substr(pos);

    std::wstring candidate = name;
    int index = 1;
    while (FileExists(JoinPath(opt_.dst, candidate)) || reserved_.count(Lower(candidate)) > 0) {
        candidate = stem + L" (" + std::to_wstring(index) + L")" + dotExt;
        ++index;
    }
    reserved_.insert(Lower(candidate));
    return JoinPath(opt_.dst, candidate);
}

void CollectorJob::RememberTarget(const std::wstring& path) {
    if (!dstSnapshotLoaded_) return;
    const std::wstring name = FileNameOf(path);
    dstNames_.push_back(name);
    dstNamesLower_.push_back(Lower(name));
    IndexDstName(dstNames_.size() - 1, dstNamesLower_.back());
}

// ---------------------------------------------------------------------------
// 单文件处理
// ---------------------------------------------------------------------------
void CollectorJob::Process(int index, JobStats& stats) {
    ItemInfo& it = items_[index];
    const std::wstring& src = it.srcPath;
    std::wstring target;
    std::wstring status;
    std::wstring verb = opt_.move ? L"移动" : L"复制";
    bool moved = false;

    do {
        // 1. 已在目标目录
        if (PathKey(DirNameOf(src)) == dstKey_) {
            status = L"已在目标目录";
            target = src;
            ++stats.inplace;
            break;
        }

        // 2. 内容去重
        if (opt_.dedupe && it.size > 0) {
            std::wstring dup;
            if (FindDuplicate(src, it.name, it.size, &dup)) {
                status = L"同名同内容已在目标中，跳过";
                target = dup;
                ++stats.skipped;
                break;
            }
        }

        // 3. 同名跳过
        const std::wstring baseTarget = JoinPath(opt_.dst, it.name);
        if (opt_.skipConflict &&
            (FileExists(baseTarget) || reserved_.count(Lower(it.name)) > 0)) {
            status = L"同名已存在，已跳过";
            target = baseTarget;
            ++stats.skipped;
            break;
        }

        // 4. 生成唯一目标名并执行（复制绝不覆盖已有文件；被抢占时换名重试）
        target = UniqueTarget(it.name);

        DWORD err = 0;
        for (int attempt = 0; attempt < 5; ++attempt) {
            if (opt_.move) {
                if (!FileExists(src)) {
                    err = ERROR_FILE_NOT_FOUND;
                    break;
                }
                if (MoveFileExW(src.c_str(), target.c_str(), MOVEFILE_COPY_ALLOWED)) {
                    moved = true;
                    err = 0;
                    break;
                }
                err = GetLastError();
                if (err == ERROR_ALREADY_EXISTS || err == ERROR_FILE_EXISTS) {
                    target = UniqueTarget(it.name);  // 期间出现同名文件，顺延下一个名字
                    continue;
                }
                break;
            }
            if (CopyFileW(src.c_str(), target.c_str(), TRUE)) {  // TRUE = 不覆盖
                err = 0;
                break;
            }
            err = GetLastError();
            if (err == ERROR_ALREADY_EXISTS || err == ERROR_FILE_EXISTS) {
                target = UniqueTarget(it.name);
                continue;
            }
            break;
        }

        if (err != 0) {
            // 没开长路径支持时 Win32 只会给 ERROR_PATH_NOT_FOUND，光看错误码
            // 会以为是文件被删了 —— 这里按路径长度直接点明原因和开关名。
            if ((err == ERROR_PATH_NOT_FOUND || err == ERROR_FILENAME_EXCED_RANGE) &&
                (src.size() >= 260 || target.size() >= 260)) {
                status = L"失败：路径超过 260 字符（系统未开启长路径支持 LongPathsEnabled）";
            } else {
                status = Utf8ToWide(FailText(opt_.move ? "移动失败" : "复制失败", err));
            }
            ++stats.failed;
            break;
        }

        // 5. 校验目标文件
        bool needCleanup = false;
        if (!FileExists(target)) {
            status = L"失败：目标文件没有生成";
        } else {
            const uint64_t newSize = FileSizeOf(target);
            if (it.size != 0 && newSize != it.size) {
                status = L"失败：大小校验不通过";
                needCleanup = !moved;
            }
        }

        if (status.empty()) {
            RememberTarget(target);
            status = L"已" + verb;
            ++stats.success;
            stats.bytes += it.size;
        } else {
            if (moved) {
                // 源已被移走，目标就是唯一副本：只要目标完整就按成功记账，绝不删除
                if (FileExists(target) && (it.size == 0 || FileSizeOf(target) == it.size)) {
                    RememberTarget(target);
                    status = L"已" + verb;
                    ++stats.success;
                    stats.bytes += it.size;
                } else {
                    ++stats.failed;
                }
            } else {
                ++stats.failed;
                if (needCleanup) {
                    if (FileExists(target) && FileSizeOf(target) != it.size) DeleteFileW(target.c_str());
                }
            }
        }
    } while (false);

    it.status = status;
    it.target = target;
    if (status.rfind(L"已复制", 0) == 0 || status.rfind(L"已移动", 0) == 0) {
        it.targetCreated = true;
    }
    sink_(it);
    EmitStatus(it.id, status, target, it.targetCreated);

    if (status.rfind(L"失败", 0) == 0) {
        EmitLog("[失败] " + WideToUtf8(src) + "  →  " + WideToUtf8(status), "err");
    } else if (status.rfind(L"已", 0) == 0) {
        std::string line = "[" + WideToUtf8(status) + "] " + WideToUtf8(src);
        if (!target.empty()) line += "  →  " + WideToUtf8(target);
        EmitLog(line, "ok");
    }
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------
void CollectorJob::Run() {
    const auto t0 = std::chrono::steady_clock::now();
    JobStats stats;

    try {
        if (opt_.scanOnly) {
            // 全盘搜索：只读扫描 + 增量上报，不做任何文件处理
            RunScanOnly(stats);
        } else {
            // 整理选中项：沿用表格里的原始 id，直接处理给定路径，不重新扫描。
            // fileList 永远由 CommandStartFiles 填充（不会为空）。
            std::vector<std::wstring> files;
            std::vector<int> ids;
            for (const auto& pair : opt_.fileList) {
                ids.push_back(pair.first);
                files.push_back(pair.second);
            }
            const int total = static_cast<int>(files.size());
            stats.total = total;

            items_.clear();
            items_.reserve(files.size());
            for (int i = 0; i < total; ++i) {
                ItemInfo it;
                it.id = ids[static_cast<size_t>(i)];
                it.srcPath = files[static_cast<size_t>(i)];
                it.name = FileNameOf(it.srcPath);
                bool ok = false;
                it.size = FileSizeOf(it.srcPath, &ok);
                if (!ok) it.size = 0;
                items_.push_back(it);
            }

            EmitLog("开始整理选中项，共 " + std::to_string(total) + " 个视频");
            EmitProgress(0, total);

            for (int i = 0; i < total; ++i) {
                if (cancel_.load()) {
                    stats.cancelled = true;
                    break;
                }
                Process(i, stats);
                EmitProgress(i + 1, total, items_[static_cast<size_t>(i)].name);
            }
        }
    } catch (const std::exception& e) {
        stats.error = std::string("异常：") + e.what();
        EmitLog("[异常] 任务中止：" + stats.error, "err");
    } catch (...) {
        stats.error = "异常：未知错误";
        EmitLog("[异常] 任务中止：未知错误", "err");
    }

    if (cancel_.load()) stats.cancelled = true;
    stats.seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    // 结束消息在 catch 之后统一发送：扫描异常、取消、正常结束都恰好发一次
    if (opt_.scanOnly) {
        if (!stats.cancelled && stats.total >= opt_.maxResults) stats.truncated = true;
        EmitScanDone(stats);
    } else {
        EmitDone(stats);
    }

    running_.store(false);
    {
        std::lock_guard<std::mutex> lock(doneMutex_);
        finished_ = true;
    }
    doneCv_.notify_all();
    if (finish_) finish_();
}

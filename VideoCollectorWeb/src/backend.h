// backend.h —— 扫描 / 复制 / 移动 / 去重 的后台任务
#pragma once

#include "util.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <functional>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

struct ItemInfo {
    int id = 0;
    std::wstring name;
    std::wstring srcPath;
    std::wstring target;
    std::wstring status = L"排队中";
    uint64_t size = 0;
    bool deleted = false;
    // 仅当本次任务真的复制/移动生成了 target 才为 true；
    // 去重/同名跳过时 target 指向目标目录里原有的第三方文件，绝不能删。
    bool targetCreated = false;
};

struct JobOptions {
    std::wstring src;
    std::wstring dst;
    std::vector<std::wstring> exts;  // 小写、不含点
    bool move = false;               // true=移动，false=复制
    bool skipConflict = false;       // true=同名跳过，false=自动重命名
    bool recursive = true;
    bool skipHidden = true;
    bool dedupe = true;

    // 体积过滤：0 表示不限制（下界 0 = 从头，上界 0 = 不限）
    uint64_t minBytes = 0;
    uint64_t maxBytes = 0;
    bool SizeAllowed(uint64_t size) const {
        if (minBytes && size < minBytes) return false;
        if (maxBytes && size > maxBytes) return false;
        return true;
    }

    // 全盘只读搜索：只扫描并上报，不处理任何文件
    bool scanOnly = false;
    std::vector<std::wstring> roots;          // 用户手动勾选的盘符
    std::vector<std::wstring> excludeNames;    // 小写目录名，任意层级生效
    std::vector<std::wstring> excludePaths;   // 小写绝对路径前缀
    std::wstring dstPath;                     // 目标文件夹及其子目录要跳过
    int maxResults = 20000;

    // 整理选中项：直接处理这些 (原行 id, 路径)，跳过扫描与下发新行
    std::vector<std::pair<int, std::wstring>> fileList;
};

struct JobStats {
    int total = 0;
    int success = 0;
    int failed = 0;
    int skipped = 0;
    int inplace = 0;
    uint64_t bytes = 0;
    bool cancelled = false;
    std::string error;
    double seconds = 0.0;
    int dirs = 0;              // 全盘搜索：已扫描目录数
    bool truncated = false;    // 全盘搜索：命中数达到上限
};

class CollectorJob {
public:
    // 输出给 WebView 的 JSON 消息（在后台线程调用）
    using Emit = std::function<void(const std::string&)>;
    // 条目新增 / 状态更新（在后台线程调用）
    using ItemSink = std::function<void(const ItemInfo&)>;
    // 任务结束（在后台线程调用）
    using Finish = std::function<void()>;

    CollectorJob(const JobOptions& options, Emit emit, ItemSink sink, Finish finish);
    ~CollectorJob();

    void Start();
    void Cancel();
    bool Running() const { return running_.load(); }
    // 等待任务结束（用于退出前收尾），返回是否已结束
    bool WaitDone(unsigned timeoutMs);

private:
    void Run();
    void RunScanOnly(JobStats& stats);
    void Process(int index, JobStats& stats);
    void EmitScanProgress(int dirs, int found);
    void EmitScanDone(const JobStats& stats);
    void EmitItems(int total, const std::vector<ItemInfo>& batch);
    void EmitStatus(int id, const std::wstring& status, const std::wstring& target,
                    bool targetCreated = false);
    void EmitProgress(int done, int total, const std::wstring& name = std::wstring());
    void EmitLog(const std::string& text, const std::string& level = "info");
    void EmitDone(const JobStats& stats);

    // 去重 / 命名
    void LoadDstSnapshot();
    void IndexDstName(size_t idx, const std::wstring& lower);
    bool FindDuplicate(const std::wstring& src, const std::wstring& name, uint64_t size,
                       std::wstring* found);
    std::string SignatureOf(const std::wstring& path, uint64_t size);
    std::wstring UniqueTarget(const std::wstring& name);
    void RememberTarget(const std::wstring& path);

    JobOptions opt_;
    Emit emit_;
    ItemSink sink_;
    Finish finish_;

    std::atomic<bool> cancel_{false};
    std::atomic<bool> running_{false};
    std::thread thread_;
    std::mutex doneMutex_;
    std::condition_variable doneCv_;
    bool finished_ = false;

    std::wstring dstKey_;
    std::vector<ItemInfo> items_;
    std::set<std::wstring> reserved_;          // 本次任务内已占用的目标名（小写）
    std::map<std::wstring, std::string> sigCache_;
    std::vector<std::wstring> dstNames_;       // 目标目录文件名快照
    std::vector<std::wstring> dstNamesLower_;  // 与 dstNames_ 平行的小写缓存
    // 小写全名 / 可匹配的“基础名” -> 索引列表（覆盖精确同名与 stem (N) 系列）
    std::unordered_map<std::wstring, std::vector<size_t>> dupIndex_;
    bool dstSnapshotLoaded_ = false;
};

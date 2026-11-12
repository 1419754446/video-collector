// main.cpp —— Win32 窗口 + WebView2 宿主 + 消息桥接
#include "resource.h"   // 资源 ID（含 IDI_APPICON），与 src\app.rc 共用
#include "util.h"
#include "backend.h"
#include "http_server.h"
#include "embedded_loader.h"
#include "WebView2.h"

#include <windowsx.h>
#include <dwmapi.h>
#include <winsock2.h>

#include <atomic>
#include <algorithm>
#include <cctype>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

// ---------------------------------------------------------------------------
// 全局状态
// ---------------------------------------------------------------------------

// 应用图标资源 ID 定义在 resource.h，与 src\app.rc 共用同一份，避免两边写岔

// 从内置资源加载大/小图标（用于窗口标题栏与任务栏）
static HICON LoadAppIcon(int cx, int cy) {
    return (HICON)LoadImageW(GetModuleHandleW(nullptr), MAKEINTRESOURCEW(IDI_APPICON),
                             IMAGE_ICON, cx, cy, LR_DEFAULTCOLOR);
}

static HWND g_hwnd = nullptr;
static HttpServer g_http;

template <typename T>
struct ComRef {
    T* p = nullptr;
    ComRef() = default;
    ComRef(const ComRef&) = delete;
    ComRef& operator=(const ComRef&) = delete;
    ~ComRef() {
        if (p) p->Release();
    }
    T* operator->() const { return p; }
    T** operator&() { return &p; }
    T* Get() const { return p; }
    void Attach(T* q) {
        if (p) p->Release();
        p = q;
    }
    void Reset() {
        if (p) {
            p->Release();
            p = nullptr;
        }
    }
};

static ComRef<ICoreWebView2Controller> g_controller;
static ComRef<ICoreWebView2> g_webview;
static bool g_uiReady = false;
// 主窗口延迟显示：界面就绪后再 ShowWindow，避免启动时先黑屏一帧
static int g_wantShowCmd = SW_SHOWNORMAL;
static bool g_mainShown = false;
static DWORD g_startTick = 0;

// 独立预览窗口（第二个 WebView2 控制器，共用同一个 Environment）
static ComRef<ICoreWebView2Environment> g_env;
static ComRef<ICoreWebView2Controller> g_previewController;
static ComRef<ICoreWebView2> g_previewWeb;
static HWND g_previewHwnd = nullptr;
static std::wstring g_pendingPreviewUrl;
static std::string g_theme = "dark";  // dark / light

static std::mutex g_queueMutex;
static std::vector<std::string> g_queue;

static std::mutex g_itemsMutex;
static std::map<int, ItemInfo> g_items;

// 撤销快照：记录「最近一次整理任务」真正生成的目标副本 {src, target, move}。
// targetCreated 才记录，去重/跳过的第三方文件绝不入快照。
struct UndoEntry {
    int id = 0;
    std::wstring src;
    std::wstring target;
    bool move = false;
};
static std::mutex g_undoMutex;
static std::map<int, UndoEntry> g_undoSnapshot;
// 每次整理任务开始置 false；sink 收到首个成功项后置 true，标记「本任务有可撤销成果」
static bool g_undoAvailable = false;

static std::unique_ptr<CollectorJob> g_job;

// 删除线程（移入回收站）：受管理生命周期，退出时 join，避免与进程退出竞态
static std::mutex g_deleteMutex;
static std::condition_variable g_deleteCv;
static std::unique_ptr<std::thread> g_deleteThread;
static bool g_deleteDone = true;

static void WaitDeleteThreadDone(unsigned timeoutMs) {
    std::unique_lock<std::mutex> lock(g_deleteMutex);
    g_deleteCv.wait_for(lock, std::chrono::milliseconds(timeoutMs), []() { return g_deleteDone; });
}

// ---------------------------------------------------------------------------
// 发往页面的消息队列（后台线程写入，UI 线程定时发送）
// ---------------------------------------------------------------------------
static void QueueMessage(const std::string& json) {
    std::lock_guard<std::mutex> lock(g_queueMutex);
    g_queue.push_back(json);
}

static void FlushQueue() {
    if (!g_uiReady || !g_webview.p) return;
    std::vector<std::string> pending;
    {
        std::lock_guard<std::mutex> lock(g_queueMutex);
        pending.swap(g_queue);
    }
    for (const auto& json : pending) {
        std::wstring wide = Utf8ToWide(json);
        g_webview.p->PostWebMessageAsJson(wide.c_str());
    }
}

static void QueueResponse(const std::string& cmd, bool ok, const std::string& msg,
                          const std::string& extra = std::string()) {
    std::string json = "{\"type\":\"response\",\"cmd\":\"" + JsonEscape(cmd) +
                       "\",\"ok\":" + (ok ? "true" : "false") +
                       ",\"msg\":\"" + JsonEscape(msg) + "\"";
    if (!extra.empty()) json += "," + extra;
    json += "}";
    QueueMessage(json);
}

static void QueueLog(const std::string& text, const std::string& level = "info") {
    QueueMessage("{\"type\":\"log\",\"level\":\"" + level + "\",\"text\":\"" +
                 JsonEscape(text) + "\"}");
}

// ---------------------------------------------------------------------------
// 启动诊断日志（startup.log，UTF-8）
// ---------------------------------------------------------------------------
static void Trace(const std::string& text) {
    std::wstring path = JoinPath(ExeDir(), L"startup.log");
    FILE* fp = _wfopen(path.c_str(), L"a");
    if (!fp) return;
    SYSTEMTIME st{};
    GetLocalTime(&st);
    fprintf(fp, "[%02d:%02d:%02d.%03d] %s\n", st.wHour, st.wMinute, st.wSecond,
            st.wMilliseconds, text.c_str());
    fclose(fp);
}

// ---------------------------------------------------------------------------
// COM 回调实现
// ---------------------------------------------------------------------------
static const IID kIIDUnknown = {0x00000000, 0x0000, 0x0000,
                                {0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46}};

template <typename Fn>
class EnvironmentCompleted
    : public ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler {
public:
    explicit EnvironmentCompleted(Fn fn) : fn_(std::move(fn)) {}
    virtual ~EnvironmentCompleted() = default;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        static const IID kIID = {0x4e8a3389, 0xc9d8, 0x4bd2,
                                 {0xb6, 0xb5, 0x12, 0x4f, 0xee, 0x6c, 0xc1, 0x4d}};
        if (IsEqualIID(riid, kIIDUnknown) || IsEqualIID(riid, kIID)) {
            *ppv = this;
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override {
        return static_cast<ULONG>(InterlockedIncrement(&ref_));
    }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG r = static_cast<ULONG>(InterlockedDecrement(&ref_));
        if (r == 0) delete this;
        return r;
    }
    HRESULT STDMETHODCALLTYPE Invoke(HRESULT hr, ICoreWebView2Environment* env) override {
        if (fn_) fn_(hr, env);
        return S_OK;
    }

private:
    LONG ref_ = 1;
    Fn fn_;
};

template <typename Fn>
class ControllerCompleted
    : public ICoreWebView2CreateCoreWebView2ControllerCompletedHandler {
public:
    explicit ControllerCompleted(Fn fn) : fn_(std::move(fn)) {}
    virtual ~ControllerCompleted() = default;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        static const IID kIID = {0x6c4819f3, 0xc9b7, 0x4260,
                                 {0x81, 0x27, 0xc9, 0xf5, 0xbd, 0xe7, 0xf6, 0x8c}};
        if (IsEqualIID(riid, kIIDUnknown) || IsEqualIID(riid, kIID)) {
            *ppv = this;
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override {
        return static_cast<ULONG>(InterlockedIncrement(&ref_));
    }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG r = static_cast<ULONG>(InterlockedDecrement(&ref_));
        if (r == 0) delete this;
        return r;
    }
    HRESULT STDMETHODCALLTYPE Invoke(HRESULT hr, ICoreWebView2Controller* ctrl) override {
        if (fn_) fn_(hr, ctrl);
        return S_OK;
    }

private:
    LONG ref_ = 1;
    Fn fn_;
};

// 前置声明
static void HandleUiMessage(const std::wstring& text);
static void HandlePopupMessage(const std::wstring& text);
static void StartWebView();
static void ApplySettings(ICoreWebView2* web);
static void ShowPreviewWindow(const std::wstring& url);
static void SendThemeToPreview();
static void ApplyTitleBar(HWND hwnd, const std::string& theme);
static void SetWebBackground(ICoreWebView2Controller* ctrl, const std::string& theme);
static std::wstring ResolveExistingPath(const ItemInfo& info);

class WebMessageReceived : public ICoreWebView2WebMessageReceivedEventHandler {
public:
    explicit WebMessageReceived(std::function<void(const std::wstring&)> fn)
        : fn_(std::move(fn)) {}
    virtual ~WebMessageReceived() = default;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        static const IID kIID = {0x57213f19, 0x00e6, 0x49fa,
                                 {0x8e, 0x07, 0x89, 0x8e, 0xa0, 0x1e, 0xcb, 0xd2}};
        if (IsEqualIID(riid, kIIDUnknown) || IsEqualIID(riid, kIID)) {
            *ppv = this;
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override {
        return static_cast<ULONG>(InterlockedIncrement(&ref_));
    }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG r = static_cast<ULONG>(InterlockedDecrement(&ref_));
        if (r == 0) delete this;
        return r;
    }
    HRESULT STDMETHODCALLTYPE Invoke(ICoreWebView2*,
                                     ICoreWebView2WebMessageReceivedEventArgs* args) override {
        LPWSTR text = nullptr;
        if (SUCCEEDED(args->TryGetWebMessageAsString(&text)) && text) {
            if (fn_) fn_(text);
            CoTaskMemFree(text);
        }
        return S_OK;
    }

private:
    LONG ref_ = 1;
    std::function<void(const std::wstring&)> fn_;
};

// ---------------------------------------------------------------------------
// 设置文件（原样保存页面传来的 JSON 文本）
// ---------------------------------------------------------------------------
static std::wstring ConfigPath() { return JoinPath(ExeDir(), L"config.json"); }

// ---------------------------------------------------------------------------
// 窗口位置/大小记忆（写入 LOCALAPPDATA，避免 exe 目录只读时写不进去）
// ---------------------------------------------------------------------------
static std::wstring WindowIniPath() {
    wchar_t buf[MAX_PATH] = {};
    std::wstring base;
    if (GetEnvironmentVariableW(L"LOCALAPPDATA", buf, MAX_PATH) && buf[0]) {
        base = buf;
    } else {
        wchar_t tmp[MAX_PATH] = {};
        if (GetTempPathW(MAX_PATH, tmp)) base = tmp;
        else base = L"C:\\";
    }
    if (!base.empty() && base.back() != L'\\') base += L'\\';
    base += L"VideoCollectorWeb";
    MakeDirs(base);
    return base + L"\\window.ini";
}

static void SaveWindowPlacement(HWND hwnd) {
    if (!hwnd) return;
    WINDOWPLACEMENT wp{};
    wp.length = sizeof(wp);
    if (!GetWindowPlacement(hwnd, &wp)) return;
    const std::wstring ini = WindowIniPath();
    const RECT& r = wp.rcNormalPosition;
    auto put = [&ini](const wchar_t* key, int value) {
        WritePrivateProfileStringW(L"window", key, std::to_wstring(value).c_str(), ini.c_str());
    };
    put(L"left", r.left);
    put(L"top", r.top);
    put(L"right", r.right);
    put(L"bottom", r.bottom);
    put(L"maximized", wp.showCmd == SW_SHOWMAXIMIZED ? 1 : 0);
}

static bool LoadWindowPlacement(int* x, int* y, int* w, int* h, bool* maximized) {
    const std::wstring ini = WindowIniPath();
    if (!FileExists(ini)) return false;
    auto get = [&ini](const wchar_t* key) {
        return static_cast<int>(GetPrivateProfileIntW(L"window", key, 0, ini.c_str()));
    };
    RECT r{get(L"left"), get(L"top"), get(L"right"), get(L"bottom")};
    if (r.right - r.left < 900 || r.bottom - r.top < 560) return false;
    if (!MonitorFromRect(&r, MONITOR_DEFAULTTONULL)) return false;  // 显示器配置变了，旧位置不可见
    *x = r.left;
    *y = r.top;
    *w = r.right - r.left;
    *h = r.bottom - r.top;
    *maximized = get(L"maximized") != 0;
    return true;
}

// 拖拽目录：WM_DROPFILES 在宿主窗口触发，把路径和落点转给页面命中输入框
static void SendDropToUi(const std::wstring& path, int x, int y) {
    QueueMessage("{\"type\":\"drop\",\"path\":\"" + JsonW(path) + "\",\"x\":" +
                 std::to_string(x) + ",\"y\":" + std::to_string(y) + "}");
}

static std::string ReadText(const std::wstring& path) {
    std::string out;
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return out;
    LARGE_INTEGER size{};
    if (GetFileSizeEx(h, &size) && size.QuadPart > 0 && size.QuadPart < (1 << 20)) {
        out.resize(static_cast<size_t>(size.QuadPart));
        DWORD read = 0;
        if (!ReadFile(h, out.data(), static_cast<DWORD>(out.size()), &read, nullptr) ||
            read != out.size()) {
            out.clear();
        }
    }
    CloseHandle(h);
    // 去掉 UTF-8 BOM（外部工具写入的 config.json 可能带 BOM）
    if (out.size() >= 3 && static_cast<unsigned char>(out[0]) == 0xEF &&
        static_cast<unsigned char>(out[1]) == 0xBB && static_cast<unsigned char>(out[2]) == 0xBF) {
        out.erase(0, 3);
    }
    return out;
}

static bool WriteText(const std::wstring& path, const std::string& text) {
    HANDLE h = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;
    size_t offset = 0;
    bool ok = true;
    while (offset < text.size()) {
        DWORD written = 0;
        const DWORD chunk = static_cast<DWORD>(std::min<size_t>(text.size() - offset, 1 << 20));
        if (!WriteFile(h, text.data() + offset, chunk, &written, nullptr) || written == 0) {
            ok = false;
            break;
        }
        offset += written;
    }
    CloseHandle(h);
    return ok;
}

// ---------------------------------------------------------------------------
// 主题（深色 / 浅色）与窗口外观
// ---------------------------------------------------------------------------
static std::string ReadThemeFromConfig() {
    const std::string raw = ReadText(ConfigPath());
    const size_t keyPos = raw.find("\"theme\"");
    if (keyPos == std::string::npos) return "dark";
    const size_t colon = raw.find(':', keyPos);
    if (colon == std::string::npos) return "dark";
    const size_t quote = raw.find('"', colon + 1);
    if (quote == std::string::npos) return "dark";
    const size_t endQuote = raw.find('"', quote + 1);
    if (endQuote == std::string::npos) return "dark";
    return raw.substr(quote + 1, endQuote - quote - 1) == "light" ? "light" : "dark";
}

// 优先返回仍然存在的文件路径：源文件 → 目标文件
static std::wstring ResolveExistingPath(const ItemInfo& info) {
    if (FileExists(info.srcPath)) return info.srcPath;
    if (!info.target.empty() && FileExists(info.target)) return info.target;
    return std::wstring();
}

static void ApplyTitleBar(HWND hwnd, const std::string& theme) {
    if (!hwnd) return;
    const BOOL dark = theme != "light";
    DwmSetWindowAttribute(hwnd, 20, &dark, sizeof(dark));  // USE_IMMERSIVE_DARK_MODE
    COLORREF caption = dark ? RGB(15, 17, 24) : RGB(246, 247, 251);
    DwmSetWindowAttribute(hwnd, 35, &caption, sizeof(caption));  // CAPTION_COLOR
    COLORREF text = dark ? RGB(233, 237, 245) : RGB(26, 32, 51);
    DwmSetWindowAttribute(hwnd, 36, &text, sizeof(text));  // TEXT_COLOR
    COLORREF border = dark ? RGB(28, 33, 46) : RGB(208, 215, 228);
    DwmSetWindowAttribute(hwnd, 34, &border, sizeof(border));  // BORDER_COLOR
}

// 设置 WebView2 默认背景，避免页面加载前的白/黑闪屏
static void SetWebBackground(ICoreWebView2Controller* ctrl, const std::string& theme) {
    if (!ctrl) return;
    ICoreWebView2Controller2* c2 = nullptr;
    if (SUCCEEDED(ctrl->QueryInterface(IID_ICoreWebView2Controller2,
                                       reinterpret_cast<void**>(&c2))) &&
        c2) {
        COREWEBVIEW2_COLOR color{};
        color.A = 255;
        if (theme == "light") {
            color.R = 238;
            color.G = 241;
            color.B = 247;
        } else {
            color.R = 11;
            color.G = 13;
            color.B = 18;
        }
        c2->put_DefaultBackgroundColor(color);
        c2->Release();
    }
}

static void SendThemeToPreview() {
    if (!g_previewWeb.p) return;
    const std::wstring json =
        L"{\"type\":\"theme\",\"value\":\"" + Utf8ToWide(g_theme) + L"\"}";
    g_previewWeb.p->PostWebMessageAsJson(json.c_str());
}

static void CommandTheme(const std::vector<std::pair<std::string, std::string>>& kv) {
    const std::string value = GetParam(kv, "value") == "light" ? "light" : "dark";
    if (value == g_theme) return;
    g_theme = value;
    ApplyTitleBar(g_hwnd, g_theme);
    ApplyTitleBar(g_previewHwnd, g_theme);
    SetWebBackground(g_controller.p, g_theme);
    SetWebBackground(g_previewController.p, g_theme);
    SendThemeToPreview();
}

// ---------------------------------------------------------------------------
// 条目记录
// ---------------------------------------------------------------------------
static void UpsertItem(const ItemInfo& info) {
    std::lock_guard<std::mutex> lock(g_itemsMutex);
    g_items[info.id] = info;
}

static bool FindItem(int id, ItemInfo* out) {
    std::lock_guard<std::mutex> lock(g_itemsMutex);
    auto it = g_items.find(id);
    if (it == g_items.end()) return false;
    *out = it->second;
    return true;
}

// /thumb 授权：只有当前列表里确实存在的记录路径才允许生成缩略图
static bool IsKnownItemPath(const std::wstring& path) {
    const std::wstring key = PathKey(path);
    std::lock_guard<std::mutex> lock(g_itemsMutex);
    for (const auto& it : g_items) {
        if (PathKey(it.second.srcPath) == key) return true;
    }
    return false;
}

// ---------------------------------------------------------------------------
// 命令处理
// ---------------------------------------------------------------------------
static JobOptions ParseJobOptions(const std::vector<std::pair<std::string, std::string>>& kv) {
    JobOptions opt;
    opt.src = Utf8ToWide(GetParam(kv, "src"));
    opt.dst = Utf8ToWide(GetParam(kv, "dst"));
    opt.move = GetParam(kv, "mode") == "move";
    opt.skipConflict = GetParam(kv, "conflict") == "skip";
    opt.recursive = GetFlag(kv, "recursive", true);
    opt.skipHidden = GetFlag(kv, "hidden", true);
    opt.dedupe = GetFlag(kv, "dedupe", true);
    // 体积限制：界面以 MB 输入，空/0 表示不限制
    {
        const auto MB = [](const std::string& v) -> uint64_t {
            double mb = atof(v.c_str());
            if (!(mb > 0)) return 0;
            if (mb > 1048576.0) mb = 1048576.0;  // 上限 1 TB，防手滑
            return static_cast<uint64_t>(mb * 1048576.0);
        };
        opt.minBytes = MB(GetParam(kv, "minsize"));
        opt.maxBytes = MB(GetParam(kv, "maxsize"));
    }

    std::string exts = GetParam(kv, "exts");
    std::replace(exts.begin(), exts.end(), ';', ',');
    size_t start = 0;
    while (start <= exts.size()) {
        size_t comma = exts.find(',', start);
        std::string piece = exts.substr(start, comma == std::string::npos ? std::string::npos
                                                                         : comma - start);
        // 去掉空白与点
        std::string clean;
        for (char c : piece) {
            if (!isspace(static_cast<unsigned char>(c)) && c != '.') clean += c;
        }
        if (!clean.empty()) {
            for (char& c : clean) c = static_cast<char>(tolower(static_cast<unsigned char>(c)));
            opt.exts.push_back(Utf8ToWide(clean));
        }
        if (comma == std::string::npos) break;
        start = comma + 1;
    }
    return opt;
}

static void SendSettings() {
    std::string raw = ReadText(ConfigPath());
    if (raw.empty()) raw = "{}";
    QueueMessage("{\"type\":\"settings\",\"data\":\"" + JsonEscape(raw) + "\"}");
}

static std::vector<std::string> SplitCsv(const std::string& text) {
    std::vector<std::string> out;
    std::string normalized = text;
    std::replace(normalized.begin(), normalized.end(), ';', ',');
    size_t start = 0;
    while (start <= normalized.size()) {
        const size_t comma = normalized.find(',', start);
        std::string piece = normalized.substr(start, comma == std::string::npos
                                                         ? std::string::npos : comma - start);
        while (!piece.empty() && isspace(static_cast<unsigned char>(piece.front()))) piece.erase(piece.begin());
        while (!piece.empty() && isspace(static_cast<unsigned char>(piece.back()))) piece.pop_back();
        if (!piece.empty()) out.push_back(piece);
        if (comma == std::string::npos) break;
        start = comma + 1;
    }
    return out;
}

static void SendDrives() {
    const DWORD mask = GetLogicalDrives();
    std::string json = "{\"type\":\"drives\",\"drives\":[";
    bool first = true;
    for (int i = 0; i < 26; ++i) {
        if (!(mask & (1u << i))) continue;
        wchar_t root[4] = {static_cast<wchar_t>(L'A' + i), L':', L'\\', 0};
        const UINT type = GetDriveTypeW(root);
        const bool selectable = (type == DRIVE_FIXED || type == DRIVE_REMOVABLE);
        const char* kind = type == DRIVE_FIXED ? "fixed"
                         : type == DRIVE_REMOVABLE ? "removable"
                         : type == DRIVE_REMOTE ? "remote"
                         : type == DRIVE_CDROM ? "cdrom" : "other";
        wchar_t label[256] = {};
        wchar_t fs[64] = {};
        std::wstring volume;
        if (GetVolumeInformationW(root, label, 255, nullptr, nullptr, nullptr, fs, 63) && label[0]) {
            volume = label;
        }
        std::wstring display = (volume.empty() ? std::wstring(L"本地磁盘") : volume) +
                               L"  (" + std::wstring(1, root[0]) + L":)";
        if (!first) json += ",";
        first = false;
        json += "{\"path\":\"" + JsonW(root) + "\",\"label\":\"" + JsonW(display) +
                "\",\"kind\":\"" + kind + "\",\"selectable\":" +
                (selectable ? "true" : "false") + "}";
    }
    json += "]}";
    QueueMessage(json);
}

static void CommandScanAll(const std::vector<std::pair<std::string, std::string>>& kv) {
    if (g_job && g_job->Running()) {
        QueueResponse("scan_all", false, "上一轮任务还在收尾，请稍等片刻再开始。");
        return;
    }
    {
        std::lock_guard<std::mutex> lock(g_deleteMutex);
        if (!g_deleteDone) {
            QueueResponse("scan_all", false, "正在删除文件，请稍候再开始。");
            return;
        }
    }

    JobOptions opt = ParseJobOptions(kv);
    opt.scanOnly = true;
    // PathKey 展开 .. / 8.3 短名并小写化，保证与 UnderPath 的字符串比较一致
    opt.dstPath = PathKey(opt.dst);
    for (const auto& piece : SplitCsv(GetParam(kv, "roots"))) {
        std::wstring root = Utf8ToWide(piece);
        for (wchar_t& ch : root) if (ch == L'/') ch = L'\\';
        while (root.size() > 3 && root.back() == L'\\') root.pop_back();
        opt.roots.push_back(root);
    }
    for (const auto& piece : SplitCsv(GetParam(kv, "exclude"))) {
        const bool absolute = piece.size() > 2 && piece[1] == ':';
        if (absolute) opt.excludePaths.push_back(PathKey(Utf8ToWide(piece)));
        else opt.excludeNames.push_back(Utf8ToWide(piece));
    }
    opt.maxResults = atoi(GetParam(kv, "max", "20000").c_str());
    if (opt.maxResults < 100) opt.maxResults = 100;
    if (opt.maxResults > 200000) opt.maxResults = 200000;

    if (opt.roots.empty()) {
        QueueResponse("scan_all", false, "请至少勾选一个磁盘。");
        return;
    }
    if (opt.exts.empty()) {
        QueueResponse("scan_all", false, "视频扩展名不能为空。");
        return;
    }

    {
        std::lock_guard<std::mutex> lock(g_itemsMutex);
        g_items.clear();
    }
    QueueLog("开始搜索 | 只读，不会移动、复制或删除任何文件");

    auto emit = [](const std::string& json) { QueueMessage(json); };
    auto sink = [](const ItemInfo& info) { UpsertItem(info); };
    auto finish = []() {};
    QueueResponse("scan_all", true, "");
    g_job.reset(new CollectorJob(opt, emit, sink, finish));
    g_job->Start();
}

static void CommandStartFiles(const std::vector<std::pair<std::string, std::string>>& kv) {
    if (g_job && g_job->Running()) {
        QueueResponse("start_files", false, "上一轮任务还在收尾，请稍等片刻再开始。");
        return;
    }
    {
        std::lock_guard<std::mutex> lock(g_deleteMutex);
        if (!g_deleteDone) {
            QueueResponse("start_files", false, "正在删除文件，请稍候再开始。");
            return;
        }
    }

    JobOptions opt = ParseJobOptions(kv);
    if (opt.dst.empty()) {
        QueueResponse("start_files", false, "请先设置目标文件夹。");
        return;
    }
    if (!DirExists(opt.dst)) {
        if (!GetFlag(kv, "create", false)) {
            QueueMessage("{\"type\":\"need_create_files\",\"dst\":\"" + JsonW(opt.dst) + "\"}");
            return;
        }
        if (!MakeDirs(opt.dst)) {
            QueueResponse("start_files", false, "创建目标文件夹失败。");
            return;
        }
    }
    if (opt.exts.empty()) {
        QueueResponse("start_files", false, "视频扩展名不能为空。");
        return;
    }

    // 只处理表格里当前存在的选中行，沿用原始 id，避免重复插行
    std::vector<std::pair<int, std::wstring>> files;
    for (const auto& piece : SplitCsv(GetParam(kv, "ids"))) {
        const int id = atoi(piece.c_str());
        ItemInfo info;
        if (!FindItem(id, &info)) continue;
        const std::wstring path = ResolveExistingPath(info);
        if (!path.empty()) files.emplace_back(id, path);
    }
    if (files.empty()) {
        QueueResponse("start_files", false, "选中的文件已不存在，请重新搜索。");
        return;
    }
    opt.fileList = std::move(files);

    // 新一轮整理(选中项)：清空上一轮撤销快照
    {
        std::lock_guard<std::mutex> lock(g_undoMutex);
        g_undoSnapshot.clear();
        g_undoAvailable = false;
    }

    QueueLog("整理选中项 | 共 " + std::to_string(opt.fileList.size()) + " 个文件 | 模式：" +
             std::string(opt.move ? "移动" : "复制") + " | 目标：" + WideToUtf8(opt.dst));

    auto emit = [](const std::string& json) { QueueMessage(json); };
    auto sink = [](const ItemInfo& info) {
        UpsertItem(info);
        // 只收集「本任务真正生成的目标副本」：targetCreated 且目标存在才入撤销快照
        if (info.targetCreated && !info.target.empty() && FileExists(info.target) &&
            PathKey(info.target) != PathKey(info.srcPath)) {
            UndoEntry e;
            e.id = info.id;
            e.src = info.srcPath;
            e.target = info.target;
            e.move = info.status.compare(0, 3, L"已移动") == 0;  // 状态前缀区分 move/copy
            {
                std::lock_guard<std::mutex> lock(g_undoMutex);
                g_undoAvailable = true;
                g_undoSnapshot[info.id] = e;
            }
        }
    };
    auto finish = []() {};
    QueueResponse("start_files", true, "");
    g_job.reset(new CollectorJob(opt, emit, sink, finish));
    g_job->Start();
}

static void CommandPreview(const std::vector<std::pair<std::string, std::string>>& kv) {
    int id = atoi(GetParam(kv, "id", "-1").c_str());
    ItemInfo info;
    if (!FindItem(id, &info)) {
        QueueMessage("{\"type\":\"preview\",\"id\":" + std::to_string(id) +
                     ",\"ok\":false,\"err\":\"记录不存在\"}");
        return;
    }
    std::wstring path = ResolveExistingPath(info);
    if (path.empty()) {
        QueueMessage("{\"type\":\"preview\",\"id\":" + std::to_string(id) +
                     ",\"ok\":false,\"err\":\"文件已不存在（可能被移动或删除）。\"}");
        return;
    }
    g_http.AllowPath(path);
    std::string url = g_http.Url() + "media?p=" + UrlEncodePath(path);
    QueueMessage("{\"type\":\"preview\",\"id\":" + std::to_string(id) +
                 ",\"ok\":true,\"url\":\"" + JsonEscape(url) + "\",\"path\":\"" +
                 JsonW(path) + "\",\"size\":" + std::to_string(info.size) + "}");
}

// 在独立窗口里播放（第二个 WebView2 控制器）
static void CommandPreviewPopout(const std::vector<std::pair<std::string, std::string>>& kv) {
    const int id = atoi(GetParam(kv, "id", "-1").c_str());
    ItemInfo info;
    if (!FindItem(id, &info)) {
        QueueResponse("preview_popout", false, "记录不存在。");
        return;
    }
    const std::wstring path = ResolveExistingPath(info);
    if (path.empty()) {
        QueueResponse("preview_popout", false, "文件已不存在（可能被移动或删除）。");
        return;
    }
    if (!g_env.p) {
        QueueResponse("preview_popout", false, "预览窗口还在初始化，请稍后重试。");
        return;
    }

    g_http.AllowPath(path);
    // 只传路径本身（单次编码），由 preview.js 用自身 origin 拼出完整 /media URL。
    // 避免把整个 URL 嵌套编码进 p 参数：那套做法会让 \\ / : 以字面量混进查询串，
    // 且 + 与 % 在多层解码里极易失真（H6）。
    std::wstring url = Utf8ToWide(g_http.Url()) + L"preview.html?p=" +
                       Utf8ToWide(UrlEncodePath(path)) + L"&path=" +
                       Utf8ToWide(UrlEncodePath(path)) + L"&name=" +
                       Utf8ToWide(UrlEncodePath(info.name)) + L"&size=" +
                       std::to_wstring(info.size) + L"&theme=" + Utf8ToWide(g_theme);
    ShowPreviewWindow(url);

    QueueMessage("{\"type\":\"preview\",\"id\":" + std::to_string(id) +
                 ",\"ok\":true,\"popout\":true,\"path\":\"" + JsonW(path) +
                 "\",\"size\":" + std::to_string(info.size) + "}");
}

static void CommandDelete(const std::vector<std::pair<std::string, std::string>>& kv) {
    if (g_job && g_job->Running()) {
        QueueResponse("delete", false, "任务进行中不能删除文件，请先取消任务。");
        return;
    }
    {
        std::lock_guard<std::mutex> lock(g_deleteMutex);
        if (!g_deleteDone) {
            QueueResponse("delete", false, "正在删除其他文件，请稍候。");
            return;
        }
    }
    std::vector<int> ids;
    {
        std::string raw = GetParam(kv, "ids");
        size_t start = 0;
        while (start <= raw.size()) {
            size_t comma = raw.find(',', start);
            std::string piece = raw.substr(start, comma == std::string::npos ? std::string::npos
                                                                             : comma - start);
            if (!piece.empty()) ids.push_back(atoi(piece.c_str()));
            if (comma == std::string::npos) break;
            start = comma + 1;
        }
    }
    if (ids.empty()) {
        QueueResponse("delete", false, "请先在列表中选择要删除的视频。");
        return;
    }

    // 默认不动目标副本：只有前端在确认框里显式勾选“同时删除目标副本”时（传 1）才删。
    // 防止其他调用方漏传该参数时把目标文件夹里的副本一并误删。
    const bool alsoTarget = GetFlag(kv, "also_target", false);

    std::vector<ItemInfo> batch;
    {
        std::lock_guard<std::mutex> lock(g_itemsMutex);
        for (int id : ids) {
            auto it = g_items.find(id);
            if (it != g_items.end()) batch.push_back(it->second);
        }
    }
    if (batch.empty()) {
        QueueResponse("delete", false, "选中的文件都已经不存在了。");
        return;
    }

    // 登记到全局，退出时可 join。上一次线程此时已把 g_deleteDone 置回 true，
    // join 必须在锁外完成：它收尾时还要拿同一把锁，持锁 join 会互相等待（死锁）。
    std::unique_ptr<std::thread> previous;
    {
        std::lock_guard<std::mutex> lock(g_deleteMutex);
        previous = std::move(g_deleteThread);
        g_deleteDone = false;
    }
    if (previous && previous->joinable()) previous->join();

    // 线程对象只在 UI 线程读写，创建时不必持锁；持锁会让 lambda 里的同名 lock
    // 与外层互相遮蔽（-Wshadow），也延长了临界区。
    g_deleteThread =
            std::make_unique<std::thread>([batch, alsoTarget]() {
                // SHFileOperationW 属于 Shell API，文档要求调用线程先初始化 COM
                const HRESULT hrCo =
                    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
                std::vector<std::string> errors;
                int deletedFiles = 0;
                int changedItems = 0;

                for (const auto& item : batch) {
                    const bool srcExists = FileExists(item.srcPath);
                    const bool tgtExists =
                        item.targetCreated &&
                        !item.target.empty() && FileExists(item.target) &&
                        PathKey(item.target) != PathKey(item.srcPath);
                    if (!srcExists && !(alsoTarget && tgtExists)) continue;

                    bool srcDeleted = false;
                    bool tgtDeleted = false;

                    if (srcExists) {
                        std::string err;
                        if (SendToRecycleBin(item.srcPath, &err)) {
                            srcDeleted = true;
                            ++deletedFiles;
                        } else {
                            errors.push_back(WideToUtf8(FileNameOf(item.srcPath)) + "：" + err);
                        }
                    }
                    if (alsoTarget && tgtExists) {
                        std::string err;
                        if (SendToRecycleBin(item.target, &err)) {
                            tgtDeleted = true;
                            ++deletedFiles;
                        } else {
                            errors.push_back(WideToUtf8(FileNameOf(item.target)) + "：" + err);
                        }
                    }

                    if (!srcDeleted && !tgtDeleted) continue;

                    std::wstring status;
                    if (srcDeleted && tgtDeleted) {
                        status = L"已删除源文件和目标副本";
                    } else if (srcDeleted) {
                        status = L"已删除源文件";
                    } else {
                        status = L"已删除目标副本";
                    }

                    ItemInfo updated = item;
                    updated.status = status;
                    updated.deleted = true;
                    UpsertItem(updated);
                    QueueMessage("{\"type\":\"status\",\"id\":" + std::to_string(item.id) +
                                 ",\"status\":\"" + JsonW(status) + "\",\"deleted\":true}");
                    ++changedItems;
                }

                std::string msg = "已移入回收站：" + std::to_string(deletedFiles) + " 个文件";
                if (!errors.empty()) {
                    msg += "；" + std::to_string(errors.size()) + " 个失败：" + errors.front();
                }
                QueueLog(msg, errors.empty() ? "ok" : "err");
                QueueResponse("delete", true, msg,
                              "\"deleted\":" + std::to_string(deletedFiles) +
                                  ",\"failed\":" + std::to_string(errors.size()) +
                                  ",\"changed\":" + std::to_string(changedItems));
                {
                    std::lock_guard<std::mutex> lock(g_deleteMutex);
                    g_deleteDone = true;
                }
                g_deleteCv.notify_all();
                if (SUCCEEDED(hrCo)) CoUninitialize();
            });
}

static std::string CsvField(const std::string& value) {
    if (value.find_first_of(",\"\r\n") == std::string::npos) return value;
    std::string out = "\"";
    for (char c : value) {
        if (c == '"') out += "\"\"";
        else out += c;
    }
    out += "\"";
    return out;
}

static void CommandReport(const std::vector<std::pair<std::string, std::string>>& kv) {
    std::vector<ItemInfo> items;
    {
        std::lock_guard<std::mutex> lock(g_itemsMutex);
        for (const auto& it : g_items) items.push_back(it.second);
    }
    if (items.empty()) {
        QueueResponse("report", false, "还没有可导出的记录。");
        return;
    }

    std::wstring dir = Utf8ToWide(GetParam(kv, "dir"));
    if (!DirExists(dir)) dir = ExeDir();

    SYSTEMTIME st{};
    GetLocalTime(&st);
    char name[64]{};
    snprintf(name, sizeof(name), "视频整理报告_%04d%02d%02d_%02d%02d%02d.csv", st.wYear, st.wMonth,
             st.wDay, st.wHour, st.wMinute, st.wSecond);

    std::wstring path;
    if (!PickSaveFileDialog(g_hwnd, dir, Utf8ToWide(name), L"CSV 文件", L"*.csv", L"csv", &path)) return;

    std::string csv = "\xEF\xBB\xBF";
    csv += "序号,文件名,大小(字节),状态,源文件路径,目标文件路径\r\n";
    int index = 0;
    for (const auto& it : items) {
        ++index;
        csv += CsvField(std::to_string(index)) + ",";
        csv += CsvField(WideToUtf8(it.name)) + ",";
        csv += CsvField(std::to_string(it.size)) + ",";
        csv += CsvField(WideToUtf8(it.status)) + ",";
        csv += CsvField(WideToUtf8(it.srcPath)) + ",";
        csv += CsvField(WideToUtf8(it.target)) + "\r\n";
    }

    if (!WriteText(path, csv)) {
        QueueResponse("report", false, "写报告失败，请检查路径权限。");
        return;
    }
    QueueLog("报告已保存：" + WideToUtf8(path), "ok");
    QueueResponse("report", true, "报告已保存", "\"path\":\"" + JsonW(path) + "\"");
}

static void CommandReportHtml(const std::vector<std::pair<std::string, std::string>>& kv) {
    std::vector<ItemInfo> items;
    {
        std::lock_guard<std::mutex> lock(g_itemsMutex);
        for (const auto& it : g_items) items.push_back(it.second);
    }
    if (items.empty()) {
        QueueResponse("report_html", false, "还没有可导出的记录。");
        return;
    }

    std::wstring dir = Utf8ToWide(GetParam(kv, "dir"));
    if (!DirExists(dir)) dir = ExeDir();

    SYSTEMTIME st{};
    GetLocalTime(&st);
    char name[64]{};
    snprintf(name, sizeof(name), "视频整理报告_%04d%02d%02d_%02d%02d%02d.html", st.wYear, st.wMonth,
             st.wDay, st.wHour, st.wMinute, st.wSecond);

    std::wstring path;
    if (!PickSaveFileDialog(g_hwnd, dir, Utf8ToWide(name), L"HTML 文件", L"*.html", L"html", &path)) return;

    auto HtmlEsc = [](const std::string& s) {
        std::string o;
        o.reserve(s.size());
        for (char ch : s) {
            switch (ch) {
                case '&': o += "&amp;"; break;
                case '<': o += "&lt;"; break;
                case '>': o += "&gt;"; break;
                case '"': o += "&quot;"; break;
                case '\'': o += "&#39;"; break;
                default: o += ch;
            }
        }
        return o;
    };
    auto StatusClass = [](const std::wstring& s) {
        if (s.compare(0, 3, L"已复制") == 0 || s.compare(0, 3, L"已移动") == 0) return std::string("ok");
        if (s.compare(0, 4, L"撤销失败") == 0) return std::string("fail");
        if (s.compare(0, 2, L"失败") == 0) return std::string("fail");
        if (s.compare(0, 3, L"已撤销") == 0) return std::string("del");
        if (s.compare(0, 4, L"已在目标") == 0 || s.compare(0, 3, L"已跳过") == 0) return std::string("warn");
        if (s.compare(0, 3, L"已删除") == 0) return std::string("del");
        if (s.empty() || s.compare(0, 3, L"待处理") == 0 || s == L"排队中") return std::string("pend");
        return std::string("warn");
    };

    std::map<std::string, int> byStatus;
    for (const auto& it : items) {
        std::string key = it.status.empty() ? "待处理" : WideToUtf8(it.status);
        byStatus[key]++;
    }

    std::string rows;
    int index = 0;
    for (const auto& it : items) {
        ++index;
        rows += "<tr><td>" + std::to_string(index) + "</td>";
        rows += "<td class='cell-name'>" + HtmlEsc(WideToUtf8(it.name)) + "</td>";
        rows += "<td>" + std::to_string(it.size) + " B</td>";
        rows += "<td><span class='tag " + StatusClass(it.status) + "'>" +
                HtmlEsc(it.status.empty() ? "待处理" : WideToUtf8(it.status)) + "</span></td>";
        rows += "<td class='cell-large'>" + HtmlEsc(WideToUtf8(it.srcPath)) + "</td>";
        rows += "<td class='cell-large'>" + HtmlEsc(WideToUtf8(it.target)) + "</td></tr>";
    }

    std::string summary;
    for (const auto& kvp : byStatus) {
        summary += "<div class='stat'><div class='stat-num'>" + std::to_string(kvp.second) +
                   "</div><div class='stat-label'>" + HtmlEsc(kvp.first) + "</div></div>";
    }

    char now[64]{};
    snprintf(now, sizeof(now), "%04d-%02d-%02d %02d:%02d:%02d", st.wYear, st.wMonth, st.wDay,
             st.wHour, st.wMinute, st.wSecond);

    std::string html = R"xxx(<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>视频整理报告</title>
</head>
<body>
<style>
  *{ box-sizing:border-box; }
  body{ margin:0; font-family:"Segoe UI","Microsoft YaHei",sans-serif;
        background:linear-gradient(135deg,#f3f5fb 0%,#eef2fb 100%); color:#20242e; }
  .wrap{ max-width:1080px; margin:0 auto; padding:32px 20px 56px; }
  h1{ font-size:22px; margin:0 0 4px; }
  .sub{ color:#6b7280; font-size:13px; margin-bottom:22px; }
  .stats{ display:flex; flex-wrap:wrap; gap:12px; margin-bottom:22px; }
  .stat{ background:rgba(255,255,255,.86); border:1px solid #e6eaf5; border-radius:14px;
         box-shadow:0 6px 18px rgba(80,100,160,.08); padding:14px 20px; min-width:120px; }
  .stat-num{ font-size:22px; font-weight:700; color:#4f46e5; }
  .stat-label{ font-size:12px; color:#7c8290; margin-top:2px; }
  table{ width:100%; border-collapse:collapse; background:rgba(255,255,255,.92);
         border-radius:14px; overflow:hidden; box-shadow:0 8px 24px rgba(80,100,160,.10); }
  th,td{ text-align:left; padding:10px 14px; font-size:13px; }
  thead th{ position:sticky; top:0; background:#eef1fb; color:#4b5264; font-weight:600; white-space:nowrap; }
  tbody tr{ border-top:1px solid #eef1f8; }
  tbody tr:nth-child(even){ background:#fafbff; }
  .cell-name{ font-weight:600; color:#20242e; }
  .cell-large{ max-width:380px; word-break:break-all; color:#5b6270; font-size:12px; }
  .tag{ display:inline-block; padding:2px 10px; border-radius:20px; font-size:12px; white-space:nowrap; }
  .tag.ok{ background:#e6f7ef; color:#0f9d68; }
  .tag.fail{ background:#fdeaea; color:#d03434; }
  .tag.warn{ background:#fdf3e4; color:#b97f10; }
  .tag.del{ background:#eef0f5; color:#707a8a; }
  .tag.pend{ background:#eaf0fe; color:#4f6fd6; }
  .foot{ margin-top:16px; font-size:12px; color:#9aa1af; }
</style>
<div class="wrap">
  <h1>视频整理报告</h1>
  <div class="sub">生成时间：)xxx";
    html += now;
    html += " · 共 " + std::to_string(items.size()) + " 条记录</div>";
    html += "<div class=\"stats\">" + summary + "</div>";
    html += "<table><thead><tr><th>#</th><th>文件名</th><th>大小</th><th>状态</th><th>源文件路径</th><th>目标文件路径</th></tr></thead><tbody>";
    html += rows;
    html += "</tbody></table><div class='foot'>由 Video Collector 导出 · 本文件为自包含 HTML，可直接双击用浏览器打开</div></div></body></html>";

    if (!WriteText(path, html)) {
        QueueResponse("report_html", false, "写报告失败，请检查路径权限。");
        return;
    }
    QueueLog("HTML 报告已保存：" + WideToUtf8(path), "ok");
    QueueResponse("report_html", true, "报告已保存", "\"path\":\"" + JsonW(path) + "\"");
}

// 撤销最近一次整理任务：copy 删除目标副本(回收站)，move 把目标副本移回源位置。
// 与删除共用同一个后台回收站线程，保证「删除 / 撤销」互斥且退出时统一 join。
static void CommandUndoAvailable(const std::vector<std::pair<std::string, std::string>>& kv) {
    (void)kv;
    std::lock_guard<std::mutex> lock(g_undoMutex);
    QueueMessage(std::string("{\"type\":\"undo_available\",\"available\":") +
                 (g_undoAvailable ? "true" : "false") + "}");
}

static void CommandUndo(const std::vector<std::pair<std::string, std::string>>& kv) {
    (void)kv;
    if (g_job && g_job->Running()) {
        QueueResponse("undo", false, "任务进行中不能撤销，请先取消任务。");
        return;
    }
    {
        std::lock_guard<std::mutex> lock(g_deleteMutex);
        if (!g_deleteDone) {
            QueueResponse("undo", false, "正在删除或撤销其他文件，请稍候。");
            return;
        }
    }
    std::vector<UndoEntry> snapshot;
    {
        std::lock_guard<std::mutex> lock(g_undoMutex);
        if (!g_undoAvailable || g_undoSnapshot.empty()) {
            QueueResponse("undo", false, "没有可撤销的整理成果。");
            return;
        }
        for (const auto& kvp : g_undoSnapshot) snapshot.push_back(kvp.second);
        g_undoSnapshot.clear();
        g_undoAvailable = false;
    }

    // 任务结束的信号：由后端 CollectorJob 在 finish 时发，前端据此刷新按钮
    QueueMessage("{\"type\":\"undo_locked\",\"count\":" + std::to_string(snapshot.size()) + "}");

    // 与删除线程共用同一个槽位，join 在锁外完成，避免与线程收尾抢锁
    std::unique_ptr<std::thread> previousUndo;
    {
        std::lock_guard<std::mutex> lock(g_deleteMutex);
        previousUndo = std::move(g_deleteThread);
        g_deleteDone = false;
    }
    if (previousUndo && previousUndo->joinable()) previousUndo->join();

    g_deleteThread = std::make_unique<std::thread>([snapshot]() {
            // 撤销同样会调用 SHFileOperationW（Shell API），线程内先初始化 COM
            const HRESULT hrCo =
                CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
            int undoneFiles = 0;
            int changedItems = 0;
            std::vector<std::string> errors;

            for (const auto& e : snapshot) {
                std::wstring newStatus;
                const bool tgtExists = !e.target.empty() && FileExists(e.target);
                if (!tgtExists) {
                    newStatus = L"已撤销(目标副本不存在)";
                } else if (e.move) {
                    // 移动：目标副本应移回源位置。源若已存在(被外部占用)，不能覆盖源，
                    // 退回为「删除目标副本」。
                    if (!FileExists(e.src)) {
                        if (MoveFileExW(e.target.c_str(), e.src.c_str(), MOVEFILE_REPLACE_EXISTING)) {
                            newStatus = L"已撤销(已移回源位置)";
                            ++undoneFiles;
                        } else {
                            errors.push_back(WideToUtf8(FileNameOf(e.src)) + "：移回源位置失败");
                            newStatus = L"撤销失败：移回源位置失败";
                        }
                    } else {
                        std::string err;
                        if (SendToRecycleBin(e.target, &err)) {
                            newStatus = L"已撤销(源位置被占用，改为删除目标副本)";
                            ++undoneFiles;
                        } else {
                            errors.push_back(WideToUtf8(FileNameOf(e.target)) + "：" + err);
                            newStatus = L"撤销失败：" + Utf8ToWide(err);
                        }
                    }
                } else {
                    // 复制：直接删除本次生成的副本(回收站)
                    std::string err;
                    if (SendToRecycleBin(e.target, &err)) {
                        newStatus = L"已撤销(已删除本次生成的副本)";
                        ++undoneFiles;
                    } else {
                        errors.push_back(WideToUtf8(FileNameOf(e.target)) + "：" + err);
                        newStatus = L"撤销失败：" + Utf8ToWide(err);
                    }
                }

                // 回写整条记录状态
                ItemInfo info;
                if (e.id >= 0 && FindItem(e.id, &info)) {
                    // 只有「已撤销…」才算文件真的不在了；撤销失败时文件还在，
                    // 不能把记录标成 deleted，否则后续删除/报告会按已删除处理。
                    const bool gone = newStatus.rfind(L"已撤销", 0) == 0;
                    ItemInfo updated = info;
                    updated.status = newStatus;
                    updated.deleted = gone;
                    UpsertItem(updated);
                    QueueMessage("{\"type\":\"status\",\"id\":" + std::to_string(updated.id) +
                                 ",\"status\":\"" + JsonW(newStatus) + "\",\"deleted\":" +
                                 (gone ? "true" : "false") + "}");
                    ++changedItems;
                }
            }

            std::string msg = "已撤销：" + std::to_string(undoneFiles) + " 个文件";
            if (!errors.empty()) {
                msg += "；" + std::to_string(errors.size()) + " 个失败：" + errors.front();
            }
            QueueLog(msg, errors.empty() ? "ok" : "err");
            QueueMessage(std::string("{\"type\":\"undo_done\",\"undone\":") +
                         std::to_string(undoneFiles) + ",\"failed\":" +
                         std::to_string(errors.size()) +
                         ",\"changed\":" + std::to_string(changedItems) + "}");
            QueueResponse("undo", true, msg,
                          "\"undone\":" + std::to_string(undoneFiles) +
                              ",\"failed\":" + std::to_string(errors.size()) +
                              ",\"changed\":" + std::to_string(changedItems));
            {
                std::lock_guard<std::mutex> lock(g_deleteMutex);
                g_deleteDone = true;
            }
            g_deleteCv.notify_all();
            if (SUCCEEDED(hrCo)) CoUninitialize();
        });
}

static void HandleUiMessage(const std::wstring& text) {
    const std::string raw = WideToUtf8(text);
    const auto kv = ParseParams(raw);
    const std::string cmd = GetParam(kv, "cmd");

    if (cmd == "ready") {
        g_uiReady = true;
        Trace("ui ready");
        // 界面已就绪（页面已解析、主题已应用），此时再显示主窗口，
        // 首帧直接是渲染好的深/浅色 UI，而不是窗口刚创建时的纯黑背景（防黑屏闪烁）
        if (!g_mainShown) {
            g_mainShown = true;
            ShowWindow(g_hwnd, g_wantShowCmd);
            UpdateWindow(g_hwnd);
        }
        SendSettings();
        SendDrives();
        QueueMessage("{\"type\":\"hello\",\"port\":" + std::to_string(g_http.Port()) + "}");
        return;
    }
    if (cmd == "scan_all") {
        CommandScanAll(kv);
        return;
    }
    if (cmd == "start_files") {
        CommandStartFiles(kv);
        return;
    }
    if (cmd == "cancel") {
        if (g_job) g_job->Cancel();
        QueueLog("正在取消，等待当前文件处理结束…", "warn");
        QueueResponse("cancel", true, "");
        return;
    }
    if (cmd == "pick") {
        std::wstring initial = Utf8ToWide(GetParam(kv, "current"));
        std::wstring picked;
        if (PickFolderDialog(g_hwnd, initial, &picked)) {
            QueueResponse("pick", true, "", "\"field\":\"" + JsonEscape(GetParam(kv, "field")) +
                                                   "\",\"path\":\"" + JsonW(picked) + "\"");
        } else {
            QueueResponse("pick", false, "");
        }
        return;
    }
    if (cmd == "save_settings") {
        std::string data = GetParam(kv, "data");
        if (data.empty()) data = "{}";
        bool ok = WriteText(ConfigPath(), data);
        if (!ok) QueueLog("设置保存失败（config.json 无法写入）", "warn");
        return;
    }
    if (cmd == "preview") {
        CommandPreview(kv);
        return;
    }
    if (cmd == "preview_popout") {
        CommandPreviewPopout(kv);
        return;
    }
    if (cmd == "preview_close") {
        if (g_previewHwnd) PostMessageW(g_previewHwnd, WM_CLOSE, 0, 0);
        return;
    }
    if (cmd == "theme") {
        CommandTheme(kv);
        return;
    }
    if (cmd == "open" || cmd == "reveal" || cmd == "open_dir") {
        std::wstring path;
        // 传了 id 就以记录为准，自动挑一个仍然存在的路径（移动模式下源文件已不在）
        const std::string idText = GetParam(kv, "id");
        if (!idText.empty() && cmd != "open_dir") {
            ItemInfo info;
            if (FindItem(atoi(idText.c_str()), &info)) path = ResolveExistingPath(info);
        }
        if (path.empty()) path = Utf8ToWide(GetParam(kv, "path"));
        bool ok = false;
        if (cmd == "open") {
            ok = ShellOpen(path);
        } else if (cmd == "reveal") {
            ok = ShellReveal(path);
        } else {
            ok = DirExists(path) && ShellOpen(path);
        }
        QueueResponse(cmd, ok, ok ? "" : "无法打开：" + WideToUtf8(path));
        return;
    }
    if (cmd == "delete") {
        CommandDelete(kv);
        return;
    }
    if (cmd == "report") {
        CommandReport(kv);
        return;
    }
    if (cmd == "report_html") {
        CommandReportHtml(kv);
        return;
    }
    if (cmd == "undo") {
        CommandUndo(kv);
        return;
    }
    if (cmd == "undo_available") {
        CommandUndoAvailable(kv);
        return;
    }
    if (cmd == "close") {
        PostMessageW(g_hwnd, WM_CLOSE, 0, 0);
        return;
    }
}

// ---------------------------------------------------------------------------
// 独立预览窗口（第二个 WebView2 控制器）
// ---------------------------------------------------------------------------
static void HandlePopupMessage(const std::wstring& text) {
    const auto kv = ParseParams(WideToUtf8(text));
    const std::string cmd = GetParam(kv, "cmd");
    if (cmd == "ready") {
        SendThemeToPreview();
        return;
    }
    if (cmd == "close") {
        if (g_previewHwnd) PostMessageW(g_previewHwnd, WM_CLOSE, 0, 0);
        return;
    }
    if (cmd == "open" || cmd == "reveal") {
        const std::wstring path = Utf8ToWide(GetParam(kv, "path"));
        if (path.empty()) return;
        if (cmd == "open") ShellOpen(path);
        else ShellReveal(path);
    }
}

static const wchar_t* kPreviewClass = L"VideoCollectorPreviewWindow";

static void UpdatePreviewBounds() {
    if (!g_previewController.p || !g_previewHwnd) return;
    RECT rc{};
    GetClientRect(g_previewHwnd, &rc);
    g_previewController.p->put_Bounds(rc);
}

static LRESULT CALLBACK PreviewWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
        case WM_SIZE:
            if (wParam != SIZE_MINIMIZED) UpdatePreviewBounds();
            return 0;

        case WM_GETMINMAXINFO: {
            auto* info = reinterpret_cast<MINMAXINFO*>(lParam);
            info->ptMinTrackSize.x = 620;
            info->ptMinTrackSize.y = 400;
            return 0;
        }

        case WM_CLOSE:
            DestroyWindow(hwnd);
            return 0;

        case WM_DESTROY:
            g_pendingPreviewUrl.clear();
            g_previewWeb.Reset();
            if (g_previewController.p) {
                g_previewController.p->Close();
                g_previewController.Reset();
            }
            g_previewHwnd = nullptr;
            QueueMessage("{\"type\":\"preview_window\",\"open\":false}");
            return 0;

        default:
            break;
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}

static void ShowPreviewWindow(const std::wstring& url) {
    if (g_previewHwnd && IsWindow(g_previewHwnd)) {
        g_pendingPreviewUrl = url;
        SetForegroundWindow(g_previewHwnd);
        if (g_previewWeb.p) g_previewWeb.p->Navigate(url.c_str());
        return;
    }
    if (!g_env.p) return;

    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.style = CS_HREDRAW | CS_VREDRAW;
    wc.lpfnWndProc = PreviewWndProc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hbrBackground = reinterpret_cast<HBRUSH>(GetStockObject(BLACK_BRUSH));
    wc.lpszClassName = kPreviewClass;
    // 只在第一次注册时加载图标：RegisterClassExW 对已注册的类返回 0，
    // 每次都 LoadAppIcon 会白白泄漏两个 HICON。
    if (!GetClassInfoExW(GetModuleHandleW(nullptr), kPreviewClass, nullptr)) {
        wc.hIcon = LoadAppIcon(GetSystemMetrics(SM_CXICON), GetSystemMetrics(SM_CYICON));
        wc.hIconSm = LoadAppIcon(GetSystemMetrics(SM_CXSMICON), GetSystemMetrics(SM_CYSMICON));
        RegisterClassExW(&wc);
    }

    RECT rc{0, 0, 1000, 640};
    AdjustWindowRectEx(&rc, WS_OVERLAPPEDWINDOW, FALSE, 0);
    const int w = rc.right - rc.left;
    const int h = rc.bottom - rc.top;
    int x = (GetSystemMetrics(SM_CXSCREEN) - w) / 2;
    int y = (GetSystemMetrics(SM_CYSCREEN) - h) / 2;
    g_previewHwnd = CreateWindowExW(WS_EX_APPWINDOW, kPreviewClass,
                                    L"视频预览 · Video Collector", WS_OVERLAPPEDWINDOW,
                                    x < 0 ? 0 : x, y < 0 ? 0 : y, w, h, nullptr, nullptr,
                                    GetModuleHandleW(nullptr), nullptr);
    if (!g_previewHwnd) return;

    g_pendingPreviewUrl = url;
    ApplyTitleBar(g_previewHwnd, g_theme);
    ShowWindow(g_previewHwnd, SW_SHOW);

    auto* ctrl = new ControllerCompleted<std::function<void(HRESULT, ICoreWebView2Controller*)>>(
        [](HRESULT chr, ICoreWebView2Controller* controller) {
            Trace("preview controller hr=" + std::to_string(static_cast<long>(chr)));
            if (FAILED(chr) || !controller) {
                if (g_previewHwnd) DestroyWindow(g_previewHwnd);
                return;
            }
            if (!g_previewHwnd) return;  // 用户在控制器就绪前已关闭窗口
            controller->AddRef();
            g_previewController.Attach(controller);
            g_previewController.p->put_IsVisible(TRUE);
            SetWebBackground(g_previewController.p, g_theme);
            UpdatePreviewBounds();

            ICoreWebView2* web = nullptr;
            if (SUCCEEDED(g_previewController.p->get_CoreWebView2(&web)) && web) {
                g_previewWeb.Attach(web);
                ApplySettings(web);
                auto* msgHandler = new WebMessageReceived(HandlePopupMessage);
                EventRegistrationToken token{};
                web->add_WebMessageReceived(msgHandler, &token);
                msgHandler->Release();
                if (!g_pendingPreviewUrl.empty()) {
                    g_previewWeb.p->Navigate(g_pendingPreviewUrl.c_str());
                }
            }
        });
    g_env.p->CreateCoreWebView2Controller(g_previewHwnd, ctrl);
    ctrl->Release();
}

// ---------------------------------------------------------------------------
// WebView2 初始化
// ---------------------------------------------------------------------------
static void ApplySettings(ICoreWebView2* web) {
    ICoreWebView2Settings* settings = nullptr;
    if (SUCCEEDED(web->get_Settings(&settings)) && settings) {
        settings->put_IsScriptEnabled(TRUE);
        settings->put_IsWebMessageEnabled(TRUE);
        settings->put_AreDefaultContextMenusEnabled(FALSE);
        settings->put_AreDevToolsEnabled(FALSE);
        settings->put_IsStatusBarEnabled(FALSE);
        settings->put_IsZoomControlEnabled(FALSE);
        settings->Release();
    }
}

static void UpdateBounds() {
    if (!g_controller.p) return;
    RECT rc{};
    GetClientRect(g_hwnd, &rc);
    g_controller.p->put_Bounds(rc);
}

// WebView2Loader.dll 已内嵌进 exe，运行时释放到本地目录再动态调用导出函数，
// 这样分发时只需要一个 exe 文件。
static HRESULT CreateEnvironmentWithOptions(
    const std::wstring& dataDir,
    ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler* handler) {
    typedef HRESULT(STDMETHODCALLTYPE* Fn)(
        PCWSTR, PCWSTR, ICoreWebView2EnvironmentOptions*,
        ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler*);
    static Fn fn = []() -> Fn {
        HMODULE mod = LoadWebView2LoaderDll();
        if (!mod) return nullptr;
        return reinterpret_cast<Fn>(
            reinterpret_cast<void*>(GetProcAddress(mod, "CreateCoreWebView2EnvironmentWithOptions")));
    }();
    if (!fn) {
        Trace("load WebView2Loader.dll failed");
        return E_FAIL;
    }
    return fn(nullptr, dataDir.c_str(), nullptr, handler);
}

static void StartWebView() {
    Trace("StartWebView begin");
    std::wstring dataDir = JoinPath(ExeDir(), L"data\\webview2");
    MakeDirs(dataDir);

    auto* handler = new EnvironmentCompleted<std::function<void(HRESULT, ICoreWebView2Environment*)>>(
        [](HRESULT hr, ICoreWebView2Environment* env) {
            Trace("env completed hr=" + std::to_string(static_cast<long>(hr)));
            if (FAILED(hr) || !env) {
                std::wstring msg = L"WebView2 初始化失败（0x" +
                                   std::to_wstring(static_cast<unsigned long>(hr)) +
                                   L"）。\n请确认已安装 Microsoft Edge WebView2 Runtime。";
                Trace("env failed -> messagebox");
                MessageBoxW(g_hwnd, msg.c_str(), L"视频集中整理工具", MB_ICONERROR);
                PostMessageW(g_hwnd, WM_CLOSE, 0, 0);
                return;
            }
            env->AddRef();
            g_env.Attach(env);  // 独立预览窗口复用同一个 Environment

            auto* ctrl = new ControllerCompleted<std::function<void(HRESULT, ICoreWebView2Controller*)>>(
                [](HRESULT chr, ICoreWebView2Controller* controller) {
                    Trace("controller completed hr=" + std::to_string(static_cast<long>(chr)));
                    if (FAILED(chr) || !controller) {
                        MessageBoxW(g_hwnd, L"创建 WebView2 控制器失败。", L"视频集中整理工具",
                                    MB_ICONERROR);
                        PostMessageW(g_hwnd, WM_CLOSE, 0, 0);
                        return;
                    }
                    controller->AddRef();
                    g_controller.Attach(controller);
                    // 关闭 WebView2 自带的外部拖放，让宿主窗口收到 WM_DROPFILES
                    ICoreWebView2Controller4* ctrl4 = nullptr;
                    if (SUCCEEDED(controller->QueryInterface(
                            IID_ICoreWebView2Controller4, reinterpret_cast<void**>(&ctrl4))) &&
                        ctrl4) {
                        ctrl4->put_AllowExternalDrop(FALSE);
                        ctrl4->Release();
                    }
                    g_controller.p->put_IsVisible(TRUE);
                    SetWebBackground(g_controller.p, g_theme);
                    UpdateBounds();

                    ICoreWebView2* web = nullptr;
                    if (SUCCEEDED(g_controller.p->get_CoreWebView2(&web)) && web) {
                        Trace("got webview core");
                        g_webview.Attach(web);
                        ApplySettings(web);

                        auto* msgHandler = new WebMessageReceived(HandleUiMessage);
                        EventRegistrationToken token{};
                        web->add_WebMessageReceived(msgHandler, &token);
                        msgHandler->Release();

                        std::wstring url = Utf8ToWide(g_http.Url()) + L"?theme=" +
                                           Utf8ToWide(g_theme);
                        Trace("navigate " + WideToUtf8(url));
                        web->Navigate(url.c_str());
                    } else {
                        MessageBoxW(g_hwnd, L"获取 WebView2 核心对象失败。", L"视频集中整理工具",
                                    MB_ICONERROR);
                        PostMessageW(g_hwnd, WM_CLOSE, 0, 0);
                    }
                });
            g_env.p->CreateCoreWebView2Controller(g_hwnd, ctrl);
            ctrl->Release();
        });

    CreateEnvironmentWithOptions(dataDir, handler);
    handler->Release();
}

// ---------------------------------------------------------------------------
// 窗口
// ---------------------------------------------------------------------------
static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
        case WM_SIZE:
            if (wParam != SIZE_MINIMIZED) UpdateBounds();
            return 0;

        case WM_EXITSIZEMOVE:
            SaveWindowPlacement(hwnd);
            return 0;

        case WM_DROPFILES: {
            HDROP drop = reinterpret_cast<HDROP>(wParam);
            POINT pt{};
            DragQueryPoint(drop, &pt);
            const DWORD len = DragQueryFileW(drop, 0, nullptr, 0);
            std::wstring path;
            if (len > 0) {
                path.resize(static_cast<size_t>(len) + 1);
                DragQueryFileW(drop, 0, &path[0], len + 1);
                path.resize(static_cast<size_t>(len));
            }
            DragFinish(drop);
            if (!path.empty()) {
                if (FileExists(path)) path = DirNameOf(path);  // 拖的是视频文件 → 取所在目录
                if (DirExists(path)) SendDropToUi(path, pt.x, pt.y);
            }
            return 0;
        }

        case WM_TIMER:
            // 兜底：若界面就绪消息迟迟不来（如 Web 消息通道异常），
            // 4 秒后也强行显示主窗口，避免窗口一直隐藏、用户以为没启动
            if (wParam == 1) {
                if (!g_mainShown && GetTickCount() - g_startTick > 4000) {
                    g_mainShown = true;
                    ShowWindow(g_hwnd, g_wantShowCmd);
                    UpdateWindow(g_hwnd);
                }
                FlushQueue();
            }
            return 0;

        case WM_GETMINMAXINFO: {
            auto* info = reinterpret_cast<MINMAXINFO*>(lParam);
            // 最小尺寸必须按当前 DPI 换算：直接写物理像素时，150% 缩放下
            // 窗口被压到设计下限以下，右侧预览栏会被挤没。
            UINT dpi = GetDpiForWindow(hwnd);
            if (dpi == 0) dpi = 96;
            info->ptMinTrackSize.x = MulDiv(1120, static_cast<int>(dpi), 96);
            info->ptMinTrackSize.y = MulDiv(700, static_cast<int>(dpi), 96);
            return 0;
        }

        case WM_CLOSE: {
            // 静默收尾：任务进行中则取消并等待其结束，不弹窗打断用户
            if (g_job && g_job->Running()) {
                g_job->Cancel();
                g_job->WaitDone(15000);
            }
            // 等待删除线程收尾，避免 detach 线程与进程退出竞态。
            // 注意：join 必须先把线程所有权移出锁，否则线程收尾时抢同一把锁
            // 置 g_deleteDone 会与本线程互等 —— 超时后整程序永久卡死。
            WaitDeleteThreadDone(8000);
            std::unique_ptr<std::thread> deleteThread;
            {
                std::lock_guard<std::mutex> lock(g_deleteMutex);
                deleteThread = std::move(g_deleteThread);
            }
            if (deleteThread && deleteThread->joinable()) deleteThread->join();
            if (g_previewHwnd && IsWindow(g_previewHwnd)) {
                SendMessageW(g_previewHwnd, WM_CLOSE, 0, 0);
            }
            SaveWindowPlacement(hwnd);
            DestroyWindow(hwnd);
            return 0;
        }

        case WM_DESTROY:
            Trace("WM_DESTROY");
            KillTimer(hwnd, 1);
            if (g_previewHwnd && IsWindow(g_previewHwnd)) DestroyWindow(g_previewHwnd);
            if (g_controller.p) {
                g_controller.p->Close();
                g_controller.Reset();
            }
            g_webview.Reset();
            g_env.Reset();
            g_http.Stop();
            PostQuitMessage(0);
            return 0;

        default:
            break;
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}

// ---------------------------------------------------------------------------
// 后端自检（--selftest，不启动界面）
// ---------------------------------------------------------------------------
static bool CreateTestFile(const std::wstring& path, const std::string& content, int repeat) {
    std::string body;
    for (int i = 0; i < repeat; ++i) body += content;
    return WriteText(path, body);
}

static std::vector<std::wstring> ListFileNames(const std::wstring& dir) {
    std::vector<std::wstring> names;
    WIN32_FIND_DATAW fd{};
    HANDLE h = FindFirstFileW(JoinPath(dir, L"*").c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE) return names;
    do {
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) continue;
        names.push_back(fd.cFileName);
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    std::sort(names.begin(), names.end());
    return names;
}

static void RemoveTree(const std::wstring& dir) {
    WIN32_FIND_DATAW fd{};
    HANDLE h = FindFirstFileW(JoinPath(dir, L"*").c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE) return;
    std::vector<std::wstring> subdirs;
    do {
        const std::wstring name = fd.cFileName;
        if (name == L"." || name == L"..") continue;
        std::wstring full = JoinPath(dir, name);
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) subdirs.push_back(full);
        else DeleteFileW(full.c_str());
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    for (const auto& sub : subdirs) RemoveTree(sub);
    RemoveDirectoryW(dir.c_str());
}

static int RunSelfTest() {
    std::string report;
    auto add = [&report](const std::string& line) { report += line + "\r\n"; };

    wchar_t tempBuf[MAX_PATH]{};
    GetTempPathW(MAX_PATH, tempBuf);
    std::wstring root = JoinPath(tempBuf, L"vc_selftest_" + std::to_wstring(GetTickCount64()));
    std::wstring src = JoinPath(root, L"src");
    std::wstring sub = JoinPath(src, L"sub");
    std::wstring dst = JoinPath(root, L"dst");
    std::wstring src2 = JoinPath(root, L"src2");
    std::wstring dst2 = JoinPath(root, L"dst2");
    MakeDirs(sub);
    MakeDirs(dst);
    MakeDirs(src2);
    MakeDirs(dst2);

    // 复制模式的测试数据
    CreateTestFile(JoinPath(src, L"a.mp4"), "AAAA", 2000);
    CreateTestFile(JoinPath(sub, L"a.mp4"), "AAAA", 2000);  // 同名同内容 → 应被去重跳过
    CreateTestFile(JoinPath(src, L"b.mp4"), "BBBB", 2000);
    CreateTestFile(JoinPath(sub, L"b.mp4"), "CCCC", 2000);  // 同名不同内容 → 自动重命名
    CreateTestFile(JoinPath(src, L"note.txt"), "hello", 5);
    // 移动模式的测试数据
    CreateTestFile(JoinPath(src2, L"x.mp4"), "XXXX", 2000);
    CreateTestFile(JoinPath(src2, L"y.mp4"), "YYYY", 2000);

    std::vector<std::string> messages;
    std::mutex msgMutex;
    auto emit = [&](const std::string& json) {
        std::lock_guard<std::mutex> lock(msgMutex);
        messages.push_back(json);
    };
    std::map<int, ItemInfo> records;
    auto sink = [&](const ItemInfo& info) { records[info.id] = info; };

    // 1. 复制 + 去重
    JobOptions opt;
    opt.src = src;
    opt.dst = dst;
    opt.exts.push_back(L"mp4");
    opt.recursive = true;
    opt.skipHidden = true;
    opt.dedupe = true;
    opt.move = false;
    opt.skipConflict = false;

    auto job = std::make_unique<CollectorJob>(opt, emit, sink, []() {});
    job->Start();
    if (!job->WaitDone(60000)) {
        add("[FAIL] 复制任务超时");
        return 1;
    }
    job.reset();

    auto dstNames = ListFileNames(dst);
    add("[INFO] 目标目录文件：" +
        [&]() {
            std::string names;
            for (const auto& n : dstNames) names += WideToUtf8(n) + " ";
            return names;
        }());
    const bool okCopy = dstNames.size() == 3 &&
                        std::find(dstNames.begin(), dstNames.end(), L"a.mp4") != dstNames.end() &&
                        std::find(dstNames.begin(), dstNames.end(), L"b.mp4") != dstNames.end() &&
                        std::find(dstNames.begin(), dstNames.end(), L"b (1).mp4") != dstNames.end();
    add(okCopy ? "[PASS] 复制 / 去重 / 自动重命名" : "[FAIL] 复制结果不符合预期");

    int success = 0, skipped = 0, failed = 0, total = 0;
    {
        std::lock_guard<std::mutex> lock(msgMutex);
        for (const auto& m : messages) {
            if (m.find("\"type\":\"done\"") != std::string::npos) {
                add("[INFO] " + m);
            }
        }
    }
    for (const auto& pair : records) {
        const ItemInfo& info = pair.second;
        ++total;
        if (info.status.rfind(L"已", 0) == 0) ++success;
        else if (info.status.rfind(L"同名", 0) == 0) ++skipped;
        else if (info.status.rfind(L"失败", 0) == 0) ++failed;
    }
    add("[INFO] 共 " + std::to_string(total) + " 项：成功 " + std::to_string(success) +
        "，跳过 " + std::to_string(skipped) + "，失败 " + std::to_string(failed));
    const bool okStats = total == 4 && success == 3 && skipped == 1 && failed == 0;
    add(okStats ? "[PASS] 状态统计正确" : "[FAIL] 状态统计不正确");

    // 2. 移动
    records.clear();
    messages.clear();
    JobOptions opt2 = opt;
    opt2.src = src2;
    opt2.dst = dst2;
    opt2.move = true;
    auto job2 = std::make_unique<CollectorJob>(opt2, emit, sink, []() {});
    job2->Start();
    const bool finished = job2->WaitDone(60000);
    job2.reset();
    const auto src2Left = ListFileNames(src2);
    const auto dst2Names = ListFileNames(dst2);
    add("[INFO] 移动后源目录剩余：" + std::to_string(src2Left.size()) + " 个文件");
    add("[INFO] 移动后目标目录：" + std::to_string(dst2Names.size()) + " 个文件");
    const bool okMove = finished && src2Left.empty() && dst2Names.size() == 2;
    add(okMove ? "[PASS] 移动模式" : "[FAIL] 移动模式不符合预期");

    // 3. 超长路径（> 260 字符）：系统开启 LongPathsEnabled + 程序清单声明
    //    longPathAware 才生效；没开启时跳过而不是判失败，方便在老系统上跑自检。
    bool okLongPath = true;
    {
        std::wstring deep = JoinPath(root, L"longpath");
        for (int i = 0; i < 7; ++i) {
            deep = JoinPath(deep, std::wstring(30, static_cast<wchar_t>(L'a' + i)));
        }
        if (deep.size() <= 260) {
            add("[SKIP] 超长路径：测试路径未超过 260 字符");
        } else if (!MakeDirs(deep)) {
            add("[SKIP] 超长路径：系统未开启 LongPathsEnabled（路径长 " +
                std::to_string(deep.size()) + " 字符）");
        } else {
            records.clear();
            std::wstring dst3 = JoinPath(root, L"dst3");
            MakeDirs(dst3);
            CreateTestFile(JoinPath(deep, L"deep.mp4"), "LONG", 3000);
            JobOptions opt3 = opt;
            opt3.src = deep;
            opt3.dst = dst3;
            auto job3 = std::make_unique<CollectorJob>(opt3, emit, sink, []() {});
            job3->Start();
            const bool done3 = job3->WaitDone(60000);
            job3.reset();
            const auto names3 = ListFileNames(dst3);
            okLongPath = done3 && names3.size() == 1 && names3[0] == L"deep.mp4";
            add(okLongPath
                    ? "[PASS] 超长路径（" + std::to_string(deep.size()) + " 字符）"
                    : "[FAIL] 超长路径复制失败（路径长 " + std::to_string(deep.size()) + " 字符）");
        }
    }

    const bool allOk = okCopy && okStats && okMove && okLongPath;
    add(allOk ? "[RESULT] SELFTEST PASS" : "[RESULT] SELFTEST FAIL");

    WriteText(JoinPath(ExeDir(), L"selftest.log"), "\xEF\xBB\xBF" + report);
    RemoveTree(root);
    return allOk ? 0 : 1;
}

int WINAPI WinMain(HINSTANCE instance, HINSTANCE, LPSTR, int) {
    Trace("====== WinMain begin ======");
    if (std::wstring(GetCommandLineW()).find(L"--selftest") != std::wstring::npos) {
        return RunSelfTest();
    }

    // 高 DPI
    {
        typedef BOOL(WINAPI * SetDpiCtx)(void*);
        HMODULE user32 = GetModuleHandleW(L"user32.dll");
        SetDpiCtx fn = user32 ? reinterpret_cast<SetDpiCtx>(
                                   reinterpret_cast<void*>(GetProcAddress(user32, "SetProcessDpiAwarenessContext")))
                             : nullptr;
        if (fn) {
            fn(reinterpret_cast<void*>(-4));  // PER_MONITOR_AWARE_V2
        } else {
            SetProcessDPIAware();
        }
    }

    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

    WSADATA wsa{};
    WSAStartup(MAKEWORD(2, 2), &wsa);

    // ui 目录可选：存在时优先使用（便于调试），否则使用 exe 内嵌的界面资源
    std::wstring uiDir = JoinPath(ExeDir(), L"ui");
    if (!g_http.Start(uiDir)) {
        MessageBoxW(nullptr, L"本地 HTTP 服务启动失败，程序无法启动。", L"视频集中整理工具",
                    MB_ICONERROR);
        return 1;
    }
    Trace("http started port=" + std::to_string(g_http.Port()));

    // 缩略图仅放行当前列表已知的记录路径
    g_http.SetThumbAuthorizer(&IsKnownItemPath);

    g_theme = ReadThemeFromConfig();

    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.style = CS_HREDRAW | CS_VREDRAW;
    wc.lpfnWndProc = WndProc;
    wc.hInstance = instance;
    wc.hIcon = LoadAppIcon(GetSystemMetrics(SM_CXICON), GetSystemMetrics(SM_CYICON));
    wc.hIconSm = LoadAppIcon(GetSystemMetrics(SM_CXSMICON), GetSystemMetrics(SM_CYSMICON));
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hbrBackground = reinterpret_cast<HBRUSH>(GetStockObject(BLACK_BRUSH));
    wc.lpszClassName = L"VideoCollectorWebWindow";
    RegisterClassExW(&wc);

    // 默认窗口按系统 DPI 放大：1400x880 是逻辑像素，150% 缩放下若仍用
    // 1400 物理像素，WebView2 里只剩 933 逻辑像素，预览列会被直接挤没
    int dpi = 96;
    {
        typedef UINT(WINAPI * GetDpiSys)(void);
        HMODULE user32 = GetModuleHandleW(L"user32.dll");
        GetDpiSys fn = user32 ? reinterpret_cast<GetDpiSys>(
                                   reinterpret_cast<void*>(GetProcAddress(user32, "GetDpiForSystem")))
                              : nullptr;
        if (fn) dpi = static_cast<int>(fn());
        if (dpi < 96) dpi = 96;
    }
    RECT work{};
    SystemParametersInfoW(SPI_GETWORKAREA, 0, &work, 0);
    int maxW = (work.right - work.left) * 92 / 100;
    int maxH = (work.bottom - work.top) * 92 / 100;
    int defW = std::min(1400 * dpi / 96, maxW);
    int defH = std::min(880 * dpi / 96, maxH);
    RECT rc{0, 0, defW, defH};
    AdjustWindowRectEx(&rc, WS_OVERLAPPEDWINDOW, FALSE, 0);
    int width = rc.right - rc.left;
    int height = rc.bottom - rc.top;

    int x = (GetSystemMetrics(SM_CXSCREEN) - (rc.right - rc.left)) / 2;
    int y = (GetSystemMetrics(SM_CYSCREEN) - (rc.bottom - rc.top)) / 2;
    bool maximized = false;
    // 上次关闭时的窗口位置/大小
    LoadWindowPlacement(&x, &y, &width, &height, &maximized);
    // 旧版本（未按 DPI 放大）保存的小尺寸直接丢弃，回到缩放后的默认大小，
    // 否则高 DPI 屏上永远以“预览列消失、表格被日志挤扁”的小窗口打开
    int minW = 1120 * dpi / 96;
    int minH = 700 * dpi / 96;
    if (width < minW || height < minH) {
        width = rc.right - rc.left;
        height = rc.bottom - rc.top;
        x = (GetSystemMetrics(SM_CXSCREEN) - width) / 2;
        y = (GetSystemMetrics(SM_CYSCREEN) - height) / 2;
        maximized = false;
    }
    if (width > maxW) width = maxW;
    if (height > maxH) height = maxH;

    g_hwnd = CreateWindowExW(0, wc.lpszClassName, L"视频集中整理工具 · Video Collector",
                             WS_OVERLAPPEDWINDOW, x < 0 ? 0 : x, y < 0 ? 0 : y, width, height,
                             nullptr, nullptr, instance, nullptr);
    if (!g_hwnd) {
        MessageBoxW(nullptr, L"窗口创建失败。", L"视频集中整理工具", MB_ICONERROR);
        return 1;
    }

    // 允许把文件夹拖进窗口（WebView2 内的外部拖放已在控制器上关闭）
    DragAcceptFiles(g_hwnd, TRUE);

    ApplyTitleBar(g_hwnd, g_theme);
    // 显式设置窗口大小图标与任务栏图标（标题栏/任务栏闪烁时均使用）
    SendMessageW(g_hwnd, WM_SETICON, (WPARAM)ICON_BIG,
                 (LPARAM)LoadAppIcon(GetSystemMetrics(SM_CXICON), GetSystemMetrics(SM_CYICON)));
    SendMessageW(g_hwnd, WM_SETICON, (WPARAM)ICON_SMALL,
                 (LPARAM)LoadAppIcon(GetSystemMetrics(SM_CXSMICON), GetSystemMetrics(SM_CYSMICON)));
    // 记录显示意图，等界面就绪（"ready" 消息）后再真正 ShowWindow，消除启动黑屏
    g_wantShowCmd = maximized ? SW_SHOWMAXIMIZED : SW_SHOW;
    g_startTick = GetTickCount();
    Trace("window created");

    SetTimer(g_hwnd, 1, 40, nullptr);
    StartWebView();

    MSG msgLoop{};
    while (GetMessageW(&msgLoop, nullptr, 0, 0)) {
        TranslateMessage(&msgLoop);
        DispatchMessageW(&msgLoop);
    }

    g_job.reset();
    WSACleanup();
    CoUninitialize();
    return 0;
}

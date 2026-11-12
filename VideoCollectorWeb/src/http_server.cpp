// http_server.cpp —— 极简 HTTP/1.1 实现，只服务于应用自身的 UI 与视频流
#include "http_server.h"
#include "embedded_ui.h"

#include <gdiplus.h>
#include <shlguid.h>
#include <bcrypt.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <sstream>
#include <vector>

namespace {

std::string ContentTypeForName(const std::wstring& name) {
    const std::wstring ext = ExtOf(name);
    if (ext == L"html" || ext == L"htm") return "text/html; charset=utf-8";
    if (ext == L"css") return "text/css; charset=utf-8";
    if (ext == L"js") return "text/javascript; charset=utf-8";
    if (ext == L"json") return "application/json; charset=utf-8";
    if (ext == L"svg") return "image/svg+xml";
    if (ext == L"png") return "image/png";
    if (ext == L"ico") return "image/x-icon";
    if (ext == L"woff2") return "font/woff2";
    if (ext == L"mp4" || ext == L"m4v") return "video/mp4";
    if (ext == L"mkv") return "video/x-matroska";
    if (ext == L"webm") return "video/webm";
    if (ext == L"mov") return "video/quicktime";
    if (ext == L"avi") return "video/x-msvideo";
    if (ext == L"ts" || ext == L"m2ts" || ext == L"mts") return "video/mp2t";
    if (ext == L"flv") return "video/x-flv";
    if (ext == L"wmv" || ext == L"asf") return "video/x-ms-wmv";
    if (ext == L"mpg" || ext == L"mpeg") return "video/mpeg";
    return "application/octet-stream";
}

std::string ReadWholeFile(const std::wstring& path) {
    std::string out;
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return out;
    LARGE_INTEGER size{};
    // UI 静态文件不应超过 32MB，超出直接拒绝，避免一次读入过大内存
    if (GetFileSizeEx(h, &size) && size.QuadPart > 0 &&
        size.QuadPart < (32LL << 20)) {
        out.resize(static_cast<size_t>(size.QuadPart));
        DWORD read = 0;
        if (!ReadFile(h, out.data(), static_cast<DWORD>(out.size()), &read, nullptr) ||
            read != out.size()) {
            out.clear();
        }
    }
    CloseHandle(h);
    return out;
}

// 取请求头（头名大小写不敏感），找不到返回空串
std::string HeaderValue(const std::string& request, const char* name) {
    const size_t nameLen = strlen(name);
    size_t pos = 0;
    while ((pos = request.find("\r\n", pos)) != std::string::npos) {
        pos += 2;
        if (pos >= request.size()) break;
        if (_strnicmp(request.c_str() + pos, name, nameLen) == 0) {
            size_t colon = request.find(':', pos);
            if (colon == std::string::npos) return std::string();
            size_t eol = request.find("\r\n", colon);
            std::string value = request.substr(colon + 1, eol == std::string::npos
                                                              ? std::string::npos
                                                              : eol - colon - 1);
            size_t b = value.find_first_not_of(" \t");
            return b == std::string::npos ? std::string() : value.substr(b);
        }
    }
    return std::string();
}

// 百分号解码（路径专用）：请求行里的路径是 URL 编码的（%20、%E4%B8%AD…）。
// 和 util.h 的 UrlDecode 区别在于不把 '+' 当空格 —— 文件名里的 '+' 是字面量。
std::string DecodePathEscapes(const std::string& in) {
    auto hexValue = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return -1;
    };
    std::string out;
    out.reserve(in.size());
    for (size_t i = 0; i < in.size(); ++i) {
        if (in[i] == '%' && i + 2 < in.size()) {
            const int hi = hexValue(in[i + 1]);
            const int lo = hexValue(in[i + 2]);
            if (hi >= 0 && lo >= 0) {
                out += static_cast<char>((hi << 4) | lo);
                i += 2;
                continue;
            }
        }
        out += in[i];
    }
    return out;
}

// ---------------------------------------------------------------------------
// 视频缩略图（/thumb）：用 Windows Shell 缩略图（IShellItemImageFactory）出图，
// 经 GDI+ 编码为 PNG，按「路径 + 文件大小 + 修改时间」做磁盘缓存。
// 目录与 Python 版共用 ~/.videocollector/thumbnails/，两版可互相复用缓存。
// ---------------------------------------------------------------------------

std::wstring HomeDir() {
    wchar_t buf[MAX_PATH] = {};
    if (GetEnvironmentVariableW(L"USERPROFILE", buf, MAX_PATH) && buf[0]) {
        return buf;
    }
    if (GetEnvironmentVariableW(L"USERPROFILE2", buf, MAX_PATH) && buf[0]) return buf;
    return L"C:\\";
}

std::string ThumbCacheDir() {
    const std::wstring dir = JoinPath(HomeDir(), L".videocollector\\thumbnails");
    if (!DirExists(dir)) MakeDirs(dir);
    return WideToUtf8(dir);
}

// ---------------------------------------------------------------------------
// 缩略图缓存键：与 Python 版（pyVideo/app/utils.py ThumbnailCache）完全一致，
// 两版可以共用 ~/.videocollector/thumbnails/：换版本不用重新生成，谁写的都能读。
//   key = md5(normcase(abspath(path)) + "\0" + int(mtime) + "\0" + size) + ".jpg"
//   失败标记 key_bad = md5(normcase(abspath(path)) + "\0" + "0" + "\0" + "0") + ".nop"
// 注意：abspath 只做路径规范化，不展开 8.3 短名（与 Python 保持一致）。
std::wstring PyNormPathW(const std::wstring& path) {
    std::wstring abs = path;
    const DWORD needed = GetFullPathNameW(path.c_str(), 0, nullptr, nullptr);
    if (needed > 0) {
        std::vector<wchar_t> buf(static_cast<size_t>(needed) + 1, L'\0');
        const DWORD written = GetFullPathNameW(path.c_str(), needed, buf.data(), nullptr);
        if (written > 0 && written < needed) abs.assign(buf.data(), written);
    }
    for (wchar_t& ch : abs) {
        if (ch == L'/') ch = L'\\';
        ch = static_cast<wchar_t>(std::towlower(ch));
    }
    return abs;
}

// FILETIME → Unix 秒（Python os.stat().st_mtime 的整数部分）
long long UnixSecondsOf(const FILETIME& ft) {
    const long long ticks =
        (static_cast<long long>(ft.dwHighDateTime) << 32) | ft.dwLowDateTime;
    const long long epochOffset = 116444736000000000LL;  // 1601-01-01 → 1970-01-01
    return (ticks - epochOffset) / 10000000LL;
}

std::string Md5Hex(const std::string& data) {
    BCRYPT_ALG_HANDLE alg = nullptr;
    if (!BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(&alg, BCRYPT_MD5_ALGORITHM, nullptr, 0))) {
        return std::string();
    }
    unsigned char digest[16] = {};
    const NTSTATUS st = BCryptHash(
        alg, nullptr, 0, reinterpret_cast<PUCHAR>(const_cast<char*>(data.data())),
        static_cast<ULONG>(data.size()), digest, sizeof(digest));
    BCryptCloseAlgorithmProvider(alg, 0);
    if (!BCRYPT_SUCCESS(st)) return std::string();
    static const char* kHex = "0123456789abcdef";
    std::string out;
    out.reserve(32);
    for (unsigned char b : digest) {
        out.push_back(kHex[b >> 4]);
        out.push_back(kHex[b & 0x0F]);
    }
    return out;
}

// 缩略图缓存文件名（不含目录）；返回空串表示失败
std::string ThumbCacheName(const std::wstring& path, uint64_t size, long long unixMtime) {
    std::string key = WideToUtf8(PyNormPathW(path));
    key.push_back('\0');
    key += std::to_string(unixMtime);
    key.push_back('\0');
    key += std::to_string(size);
    const std::string digest = Md5Hex(key);
    return digest.empty() ? std::string() : digest + ".jpg";
}

// 失败标记文件名（与 Python 版 .nop 一致），命中就不再尝试生成
std::string ThumbBadName(const std::wstring& path) {
    // Python: _key(filepath, 0, 0) -> "{path}\x00{0}\x00{1}"，即路径 + NUL + "0" + NUL + "0"
    std::string key = WideToUtf8(PyNormPathW(path));
    key.push_back('\0');
    key.push_back('0');
    key.push_back('\0');
    key.push_back('0');
    const std::string digest = Md5Hex(key);
    return digest.empty() ? std::string() : digest + ".nop";
}

bool WriteWholeFile(const std::wstring& path, const void* data, size_t len) {
    HANDLE h = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;
    DWORD written = 0;
    BOOL ok = WriteFile(h, data, static_cast<DWORD>(len), &written, nullptr) && written == len;
    CloseHandle(h);
    return ok;
}

// GDI+ 全局启动一次（进程内所有 HTTP 线程共用）
bool EnsureGdiplus() {
    static const bool ok = [] {
        Gdiplus::GdiplusStartupInput in;
        ULONG_PTR token = 0;
        return Gdiplus::GdiplusStartup(&token, &in, nullptr) == Gdiplus::Ok;
    }();
    return ok;
}

// 用 Shell 生成视频缩略图 → HBITMAP，经 GDI+ 存成 JPG，成功返回 true。
// 尺寸/格式/质量都与 Python 版一致（116×64、JPG q78），共用同一份缓存目录。
// 注意：调用线程必须先初始化 COM（见 AcceptLoop 里的客户端线程）。
bool MakeShellThumbnailJpg(const std::wstring& path, const std::wstring& outFile) {
    if (!EnsureGdiplus()) return false;
    // IShellItemImageFactory 直接由解析路径得到，这是官方推荐用法
    // （不能用 IShellItem::BindToHandler(BHID_ThumbnailHandler)，那绑定的是 IThumbnailProvider，
    //  查询 IShellItemImageFactory 会返回 E_NOTIMPL，导致所有缩略图 404）
    IShellItemImageFactory* factory = nullptr;
    if (FAILED(SHCreateItemFromParsingName(path.c_str(), nullptr, IID_PPV_ARGS(&factory))) ||
        !factory) {
        return false;
    }

    // 不要用 SIIGBF_BIGGERSIZEOK：那会返回系统缓存的大图（实测 768×432 / 262KB）。
    // RESIZETOFIT 会按请求尺寸缩放，配合 JPG 一张约 3~4KB。
    SIZE sz{116, 64};
    HBITMAP hbm = nullptr;
    HRESULT hr = factory->GetImage(sz, SIIGBF_RESIZETOFIT, &hbm);
    factory->Release();
    if (FAILED(hr) || !hbm) return false;

    bool ok = false;
    {
        Gdiplus::Bitmap bmp(hbm, nullptr);

        UINT num = 0, sizeOf = 0;
        Gdiplus::GetImageEncodersSize(&num, &sizeOf);
        if (sizeOf > 0) {
            std::vector<Gdiplus::ImageCodecInfo> codecs(sizeOf);
            Gdiplus::GetImageEncoders(num, sizeOf, codecs.data());
            CLSID jpgClsid{};
            bool found = false;
            for (UINT i = 0; i < num; ++i) {
                if (wcscmp(codecs[i].MimeType, L"image/jpeg") == 0) {
                    jpgClsid = codecs[i].Clsid;
                    found = true;
                    break;
                }
            }
            if (found) {
                ULONG quality = 78;  // 与 Python 版一致
                Gdiplus::EncoderParameters params;
                params.Count = 1;
                params.Parameter[0].Guid = Gdiplus::EncoderQuality;
                params.Parameter[0].Type = Gdiplus::EncoderParameterValueTypeLong;
                params.Parameter[0].NumberOfValues = 1;
                params.Parameter[0].Value = &quality;
                ok = bmp.Save(outFile.c_str(), &jpgClsid, &params) == Gdiplus::Ok;
            }
        }
        // 必须在 bmp 析构之后再释放 HBITMAP：Bitmap 不拥有该位图
    }
    DeleteObject(hbm);
    if (!ok) {
        DeleteFileW(outFile.c_str());  // 清理半成品
        return false;
    }
    return true;
}

}  // namespace

HttpServer::~HttpServer() { Stop(); }

bool HttpServer::Start(const std::wstring& uiDir) {
    uiDir_ = uiDir;

    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) return false;

    // 独占地址：否则别的本地进程可以用 SO_REUSEADDR 抢走"同一个端口"，
    // 换成自己的页面，而那个页面拥有 postMessage 桥，能直接操作本地文件。
    BOOL exclusive = TRUE;
    setsockopt(s, SOL_SOCKET, SO_EXCLUSIVEADDRUSE,
               reinterpret_cast<const char*>(&exclusive), sizeof(exclusive));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = inet_addr("127.0.0.1");
    addr.sin_port = 0;

    if (bind(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        closesocket(s);
        return false;
    }
    if (listen(s, 64) != 0) {
        closesocket(s);
        return false;
    }
    // accept 设 1 秒超时：Stop() 里跨线程 closesocket 不保证能唤醒阻塞的 accept，
    // 有了超时循环就能自己看到 running_ == false 并退出，退出不会卡住
    DWORD acceptTimeout = 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&acceptTimeout),
               sizeof(acceptTimeout));

    sockaddr_in bound{};
    int len = sizeof(bound);
    if (getsockname(s, reinterpret_cast<sockaddr*>(&bound), &len) != 0) {
        closesocket(s);
        return false;
    }
    port_ = ntohs(bound.sin_port);
    listen_ = s;
    running_.store(true);
    thread_ = std::thread([this]() { AcceptLoop(); });
    return true;
}

void HttpServer::Stop() {
    if (!running_.exchange(false)) return;
    if (listen_ != INVALID_SOCKET) {
        closesocket(listen_);  // 让阻塞的 accept 立即返回
        listen_ = INVALID_SOCKET;
    }
    if (thread_.joinable()) thread_.join();

    // 收走所有客户端线程：先主动 shutdown 打断它们手里的 recv/send，
    // 再逐个 join。以前是 detach，进程退出时线程可能还在访问已经析构的成员。
    std::vector<ClientEntry> pending;
    {
        std::lock_guard<std::mutex> lock(clientsMutex_);
        pending.swap(clients_);
    }
    for (auto& entry : pending) {
        if (entry.done && !entry.done->load()) shutdown(entry.sock, SD_BOTH);
    }
    // 最多再等 3 秒：万一有人截到了网络盘上的视频、ReadFile 卡在 SMB 超时里，
    // 也不能让程序关不掉；这类线程只剩最后几条指令，交给进程退出去收拾。
    for (int i = 0; i < 300 && activeClients_.load() > 0; ++i) Sleep(10);
    for (auto& entry : pending) {
        if (!entry.thread.joinable()) continue;
        if (entry.done && entry.done->load()) entry.thread.join();
        else entry.thread.detach();
    }
}

void HttpServer::ReapClients() {
    std::lock_guard<std::mutex> lock(clientsMutex_);
    for (auto it = clients_.begin(); it != clients_.end();) {
        if (it->done && it->done->load()) {
            if (it->thread.joinable()) it->thread.join();
            it = clients_.erase(it);
        } else {
            ++it;
        }
    }
}

void HttpServer::AllowPath(const std::wstring& path) {
    const std::wstring key = PathKey(path);
    std::lock_guard<std::mutex> lock(allowedMutex_);
    if (!allowed_.insert(key).second) return;
    allowedOrder_.push_back(key);
    while (allowedOrder_.size() > 512) {
        allowed_.erase(allowedOrder_.front());
        allowedOrder_.pop_front();
    }
}

void HttpServer::ClearAllowed() {
    std::lock_guard<std::mutex> lock(allowedMutex_);
    allowed_.clear();
    allowedOrder_.clear();
}

void HttpServer::AcceptLoop() {
    while (running_.load()) {
        SOCKET s = accept(listen_, nullptr, nullptr);
        if (s == INVALID_SOCKET) {
            if (!running_.load()) break;
            continue;
        }
        // 限制并发连接数：视频拖动会频繁建立短连接，但不应无限堆积线程
        if (activeClients_.load() >= 64) {
            closesocket(s);
            continue;
        }
        auto done = std::make_shared<std::atomic<bool>>(false);
        ClientEntry entry;
        entry.sock = s;
        entry.done = done;
        entry.thread = std::thread([this, s, done]() {
            // Shell 缩略图 API 需要 COM；每个连接线程都是新线程，必须自己初始化。
            // 用 STA（与资源管理器/Shell 缩略图处理器的预期一致）。
            const HRESULT hrCo = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
            HandleClient(s);
            if (SUCCEEDED(hrCo)) CoUninitialize();
            // 先置 done 再关 socket：Stop() 只 shutdown 仍标记为"未完成"的连接，
            // 这样不会去 shutdown 一个已经被回收、可能被复用的句柄值
            done->store(true);
            shutdown(s, SD_BOTH);
            closesocket(s);
            activeClients_.fetch_sub(1);
        });
        {
            std::lock_guard<std::mutex> lock(clientsMutex_);
            clients_.push_back(std::move(entry));
        }
        activeClients_.fetch_add(1);
        ReapClients();
    }
}

bool HttpServer::SendAll(SOCKET s, const char* data, size_t len) {
    size_t sent = 0;
    while (sent < len) {
        int n = send(s, data + sent, static_cast<int>(std::min<size_t>(len - sent, 1 << 20)), 0);
        if (n <= 0) return false;
        sent += static_cast<size_t>(n);
    }
    return true;
}

bool HttpServer::SendHeaders(SOCKET s, const char* head) {
    return SendAll(s, head, strlen(head));
}

void HttpServer::HandleClient(SOCKET s) {
    // 空闲/卡死连接不会永久占用线程
    DWORD timeout = 15000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout),
               sizeof(timeout));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&timeout),
               sizeof(timeout));

    // 读取请求头（最多 16KB）
    std::string request;
    char buf[2048];
    while (request.size() < 16384) {
        int n = recv(s, buf, sizeof(buf), 0);
        if (n <= 0) break;
        request.append(buf, static_cast<size_t>(n));
        if (request.find("\r\n\r\n") != std::string::npos) break;
    }

    std::string firstLine = request.substr(0, request.find("\r\n"));
    std::string method, target;
    {
        size_t sp1 = firstLine.find(' ');
        if (sp1 == std::string::npos) return;  // socket 由调用方（连接线程）关闭
        method = firstLine.substr(0, sp1);
        size_t sp2 = firstLine.find(' ', sp1 + 1);
        target = firstLine.substr(sp1 + 1, sp2 == std::string::npos ? std::string::npos : sp2 - sp1 - 1);
    }

    // 只支持 GET / HEAD
    bool headOnly = method == "HEAD";
    if (method != "GET" && !headOnly) {
        char head[96];
        snprintf(head, sizeof(head),
                 "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        SendHeaders(s, head);
        return;
    }

    const std::string rangeHeader = HeaderValue(request, "Range");

    size_t qpos = target.find('?');
    std::string path = target.substr(0, qpos);
    std::string query = qpos == std::string::npos ? std::string() : target.substr(qpos + 1);

    if (path == "/media") {
        ServeMedia(s, query, rangeHeader, headOnly);
    } else if (path == "/thumb") {
        ServeThumb(s, query, headOnly);
    } else {
        ServeStatic(s, path, headOnly);
    }
}

void HttpServer::ServeStatic(SOCKET s, const std::string& path, bool headOnly) {
    std::string name = path;
    if (name == "/" || name.empty()) name = "/index.html";
    // 先做百分号解码，再做 ".." 检查：否则 %2e%2e%2f 之类的编码能绕过校验
    name = DecodePathEscapes(name);
    if (name.find("..") != std::string::npos) {
        SendHeaders(s, "HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        return;
    }
    while (!name.empty() && (name[0] == '/' || name[0] == '\\')) name.erase(name.begin());

    std::wstring wideName = Utf8ToWide(name);
    std::wstring file = JoinPath(uiDir_, wideName);

    // 优先读取磁盘上的 ui 目录（便于调试）；不存在时回退到 exe 内嵌资源，
    // 这样单独分发 exe 也能正常显示界面。
    std::string body;
    bool found = false;
    if (FileExists(file)) {
        body = ReadWholeFile(file);
        found = !body.empty();
    }
    if (!found) found = LoadEmbeddedUiFile(wideName, body);
    if (!found) {
        SendHeaders(s, "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        return;
    }
    std::string type = ContentTypeForName(wideName);
    char head[512];
    snprintf(head, sizeof(head),
             "HTTP/1.1 200 OK\r\nContent-Type: %s\r\nContent-Length: %llu\r\n"
             "Cache-Control: no-store\r\nConnection: close\r\n\r\n",
             type.c_str(), static_cast<unsigned long long>(body.size()));
    SendHeaders(s, head);
    if (!headOnly && !body.empty()) SendAll(s, body.data(), body.size());
}

void HttpServer::ServeMedia(SOCKET s, const std::string& rawQuery,
                            const std::string& rangeHeader, bool headOnly) {
    std::string fileParam;
    for (const auto& kv : ParseParams(rawQuery)) {
        if (kv.first == "p") fileParam = kv.second;
    }
    if (fileParam.empty()) {
        SendHeaders(s, "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        return;
    }

    std::wstring path = Utf8ToWide(fileParam);
    {
        std::lock_guard<std::mutex> lock(allowedMutex_);
        if (allowed_.count(PathKey(path)) == 0) {
            SendHeaders(s, "HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
            return;
        }
    }

    bool ok = false;
    const uint64_t size = FileSizeOf(path, &ok);
    if (!ok) {
        SendHeaders(s, "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        return;
    }

    const std::string type = ContentTypeForName(path);

    uint64_t start = 0;
    uint64_t end = size == 0 ? 0 : size - 1;
    bool partial = false;

    if (!rangeHeader.empty() && rangeHeader.rfind("bytes=", 0) == 0) {
        std::string spec = rangeHeader.substr(6);
        size_t comma = spec.find(',');
        if (comma != std::string::npos) spec = spec.substr(0, comma);
        // 去掉空格
        spec.erase(std::remove(spec.begin(), spec.end(), ' '), spec.end());

        size_t dash = spec.find('-');
        if (dash != std::string::npos && size > 0) {
            const std::string a = spec.substr(0, dash);
            const std::string b = spec.substr(dash + 1);
            if (a.empty() && !b.empty()) {
                // bytes=-N：最后 N 个字节
                uint64_t suffix = strtoull(b.c_str(), nullptr, 10);
                if (suffix > 0) {
                    start = suffix >= size ? 0 : size - suffix;
                    end = size - 1;
                    partial = true;
                }
            } else if (!a.empty()) {
                start = strtoull(a.c_str(), nullptr, 10);
                end = b.empty() ? size - 1 : strtoull(b.c_str(), nullptr, 10);
                if (end >= size) end = size - 1;
                if (start <= end && start < size) partial = true;
            }
        }
        if (partial && start >= size) partial = false;
    }

    if (!partial) {
        start = 0;
        end = size == 0 ? 0 : size - 1;
    }

    const uint64_t length = (size == 0 || end + 1 <= start) ? 0 : (end - start + 1);

    // 先打开文件再回响应头：打开失败时还能如实返回 404，
    // 而不是先发 206/200 再静默断开（播放器会停在花屏状态）。
    HANDLE h = INVALID_HANDLE_VALUE;
    if (!headOnly && length > 0) {
        h = CreateFileW(path.c_str(), GENERIC_READ,
                        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                        nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (h == INVALID_HANDLE_VALUE) {
            SendHeaders(s, "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
            return;
        }
    }

    char head[512];
    if (partial) {
        snprintf(head, sizeof(head),
                 "HTTP/1.1 206 Partial Content\r\nContent-Type: %s\r\n"
                 "Accept-Ranges: bytes\r\nContent-Range: bytes %llu-%llu/%llu\r\n"
                 "Content-Length: %llu\r\nCache-Control: private, max-age=86400\r\nConnection: close\r\n\r\n",
                 type.c_str(), static_cast<unsigned long long>(start),
                 static_cast<unsigned long long>(end),
                 static_cast<unsigned long long>(size),
                 static_cast<unsigned long long>(length));
    } else {
        snprintf(head, sizeof(head),
                 "HTTP/1.1 200 OK\r\nContent-Type: %s\r\n"
                 "Accept-Ranges: bytes\r\nContent-Length: %llu\r\n"
                 "Cache-Control: private, max-age=86400\r\nConnection: close\r\n\r\n",
                 type.c_str(), static_cast<unsigned long long>(size));
    }
    SendHeaders(s, head);
    if (h == INVALID_HANDLE_VALUE) return;  // HEAD 或 0 字节文件

    LARGE_INTEGER pos{};
    pos.QuadPart = static_cast<LONGLONG>(start);
    if (!SetFilePointerEx(h, pos, nullptr, FILE_BEGIN)) {
        CloseHandle(h);
        return;
    }

    std::vector<char> chunk(256 * 1024);
    uint64_t remaining = length;
    bool okSend = true;
    while (remaining > 0 && okSend && running_.load()) {
        DWORD toRead = static_cast<DWORD>(std::min<uint64_t>(remaining, chunk.size()));
        DWORD read = 0;
        if (!ReadFile(h, chunk.data(), toRead, &read, nullptr) || read == 0) break;
        okSend = SendAll(s, chunk.data(), read);
        remaining -= read;
    }
    CloseHandle(h);
}

void HttpServer::ServeThumb(SOCKET s, const std::string& rawQuery, bool headOnly) {
    std::string fileParam;
    for (const auto& kv : ParseParams(rawQuery)) {
        if (kv.first == "path") fileParam = kv.second;
    }
    if (fileParam.empty()) {
        SendHeaders(s, "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        return;
    }

    std::wstring path = Utf8ToWide(fileParam);

    // 授权：只有当前已知的记录项（由 main.cpp 注入的回调判定）才允许出图
    if (thumbAuthorizer_ && !thumbAuthorizer_(path)) {
        SendHeaders(s, "HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        return;
    }

    // 拿大小 + 修改时间，作为缓存键的一部分（文件一变，键就变）
    WIN32_FILE_ATTRIBUTE_DATA attr{};
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &attr)) {
        SendHeaders(s, "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        return;
    }
    const uint64_t fileSize = (static_cast<uint64_t>(attr.nFileSizeHigh) << 32) | attr.nFileSizeLow;
    const long long unixMtime = UnixSecondsOf(attr.ftLastWriteTime);

    const std::string cacheName = ThumbCacheName(path, fileSize, unixMtime);
    if (cacheName.empty()) {
        SendHeaders(s, "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        return;
    }
    const std::string cacheDir = ThumbCacheDir();
    const std::wstring cacheFile = Utf8ToWide(cacheDir + "\\" + cacheName);
    const std::wstring tmpFile = cacheFile + L".tmp";
    // 与 Python 版共用的失败标记：命中就不必再花几百毫秒去抓帧
    const std::string badName = ThumbBadName(path);
    const std::wstring badFile = Utf8ToWide(cacheDir + "\\" + badName);

    if (!FileExists(cacheFile)) {
        if (!badName.empty() && FileExists(badFile)) {
            SendHeaders(s, "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
            return;
        }
        // 生成 + 写盘（多线程并发请求同一文件时用互斥避免半成品互相覆盖；
        // 先写 .tmp 再改名为成品，崩溃残留的 .tmp 会被下一次生成顶掉）
        static std::mutex genMutex;
        std::lock_guard<std::mutex> lock(genMutex);
        if (!FileExists(cacheFile)) {
            if (!MakeShellThumbnailJpg(path, tmpFile)) {
                DeleteFileW(tmpFile.c_str());
                if (!badName.empty()) WriteWholeFile(badFile, "1", 1);  // 负缓存
                SendHeaders(s, "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                return;
            }
            MoveFileExW(tmpFile.c_str(), cacheFile.c_str(),
                        MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
        }
    }

    std::string body = ReadWholeFile(cacheFile);
    if (body.empty()) {
        SendHeaders(s, "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        return;
    }

    // 命中缓存时 touch 一次修改时间：Python 版的 LRU 清理按 mtime 淘汰，
    // 这样两版共用的缓存目录里，正在用的缩略图不会被当成"最久没用"删掉。
    {
        HANDLE h = CreateFileW(cacheFile.c_str(), FILE_WRITE_ATTRIBUTES,
                               FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                               OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (h != INVALID_HANDLE_VALUE) {
            FILETIME now{};
            GetSystemTimeAsFileTime(&now);
            SetFileTime(h, nullptr, nullptr, &now);
            CloseHandle(h);
        }
    }

    char head[512];
    snprintf(head, sizeof(head),
             "HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: %llu\r\n"
             "Cache-Control: max-age=604800\r\nConnection: close\r\n\r\n",
             static_cast<unsigned long long>(body.size()));
    SendHeaders(s, head);
    if (!headOnly) SendAll(s, body.data(), body.size());
}

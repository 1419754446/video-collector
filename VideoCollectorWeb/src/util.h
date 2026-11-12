// util.h —— 通用工具：编码转换、JSON 转义、路径处理、文件指纹、回收站、系统对话框
#pragma once

#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif

#include <windows.h>
#include <shellapi.h>
#include <shlobj.h>
#include <shobjidl.h>
#include <objbase.h>

#include <string>
#include <vector>
#include <cstdint>
#include <cstdio>
#include <cwctype>
#include <algorithm>

// ---------------------------------------------------------------------------
// 字符串编码
// ---------------------------------------------------------------------------
inline std::wstring Utf8ToWide(const std::string& s) {
    if (s.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), nullptr, 0);
    if (n <= 0) return std::wstring();
    std::wstring w(static_cast<size_t>(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), &w[0], n);
    return w;
}

inline std::string WideToUtf8(const std::wstring& w) {
    if (w.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), static_cast<int>(w.size()),
                                nullptr, 0, nullptr, nullptr);
    if (n <= 0) return std::string();
    std::string s(static_cast<size_t>(n), '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), static_cast<int>(w.size()),
                        &s[0], n, nullptr, nullptr);
    return s;
}

// ---------------------------------------------------------------------------
// JSON 转义（输入/输出均为 UTF-8 文本，非 ASCII 字节原样保留）
// ---------------------------------------------------------------------------
inline std::string JsonEscape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 16);
    for (unsigned char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[8]{};
                    snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    return out;
}

inline std::string JsonW(const std::wstring& w) { return JsonEscape(WideToUtf8(w)); }

// ---------------------------------------------------------------------------
// URL 编码 / 解码
// ---------------------------------------------------------------------------
inline std::string UrlDecode(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (size_t i = 0; i < in.size(); ++i) {
        char c = in[i];
        if (c == '+') {
            out += ' ';
        } else if (c == '%' && i + 2 < in.size()) {
            auto hex = [](char ch) -> int {
                if (ch >= '0' && ch <= '9') return ch - '0';
                if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
                if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
                return -1;
            };
            int h = hex(in[i + 1]);
            int l = hex(in[i + 2]);
            if (h >= 0 && l >= 0) {
                out += static_cast<char>((h << 4) | l);
                i += 2;
            } else {
                out += c;
            }
        } else {
            out += c;
        }
    }
    return out;
}

inline std::string UrlEncodePath(const std::wstring& path) {
    const std::string u = WideToUtf8(path);
    std::string out;
    out.reserve(u.size() * 3);
    for (unsigned char c : u) {
        const bool keep = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                          (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' ||
                          c == '~' || c == '/' || c == '\\' || c == ':';
        if (keep) {
            out += static_cast<char>(c);
        } else {
            char buf[4]{};
            snprintf(buf, sizeof(buf), "%%%02X", static_cast<unsigned>(c));
            out += buf;
        }
    }
    return out;
}

// 解析 "a=1&b=2..." 形式的 key=value 参数（value 已 URL 解码）
inline std::vector<std::pair<std::string, std::string>> ParseParams(const std::string& s) {
    std::vector<std::pair<std::string, std::string>> out;
    size_t start = 0;
    while (start <= s.size()) {
        size_t amp = s.find('&', start);
        std::string piece = s.substr(start, amp == std::string::npos ? std::string::npos : amp - start);
        if (!piece.empty()) {
            size_t eq = piece.find('=');
            if (eq == std::string::npos) {
                out.emplace_back(UrlDecode(piece), std::string());
            } else {
                out.emplace_back(UrlDecode(piece.substr(0, eq)), UrlDecode(piece.substr(eq + 1)));
            }
        }
        if (amp == std::string::npos) break;
        start = amp + 1;
    }
    return out;
}

inline std::string GetParam(const std::vector<std::pair<std::string, std::string>>& kv,
                            const std::string& key, const std::string& def = std::string()) {
    for (const auto& it : kv) {
        if (it.first == key) return it.second;
    }
    return def;
}

inline bool GetFlag(const std::vector<std::pair<std::string, std::string>>& kv,
                    const std::string& key, bool def = false) {
    const std::string v = GetParam(kv, key);
    if (v.empty()) return def;
    return v == "1" || v == "true" || v == "yes";
}

// ---------------------------------------------------------------------------
// 路径辅助
// ---------------------------------------------------------------------------
inline bool FileExists(const std::wstring& path) {
    DWORD attr = GetFileAttributesW(path.c_str());
    return attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY);
}

inline bool DirExists(const std::wstring& path) {
    DWORD attr = GetFileAttributesW(path.c_str());
    return attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY);
}

// 规范化路径用于比较：绝对化 + 展开 8.3 短名 + 小写
inline std::wstring PathKey(const std::wstring& path) {
    if (path.empty()) return std::wstring();
    // 两趟调用动态分配，避免 MAX_PATH*2 固定缓冲区在长路径上被截断
    std::wstring abs = path;
    const DWORD needed = GetFullPathNameW(path.c_str(), 0, nullptr, nullptr);
    if (needed > 0) {
        std::vector<wchar_t> buf(static_cast<size_t>(needed) + 1, L'\0');
        const DWORD written = GetFullPathNameW(path.c_str(), needed, buf.data(), nullptr);
        if (written > 0 && written < needed) abs.assign(buf.data(), written);
    }
    const DWORD longNeeded = GetLongPathNameW(abs.c_str(), nullptr, 0);
    if (longNeeded > 0) {
        std::vector<wchar_t> buf(static_cast<size_t>(longNeeded) + 1, L'\0');
        const DWORD written = GetLongPathNameW(abs.c_str(), buf.data(), longNeeded);
        if (written > 0 && written < longNeeded) abs.assign(buf.data(), written);
    }
    while (!abs.empty() && (abs.back() == L'\\' || abs.back() == L'/')) abs.pop_back();
    for (wchar_t& ch : abs) ch = static_cast<wchar_t>(std::towlower(ch));
    return abs;
}

inline std::wstring ExeDir() {
    wchar_t buf[MAX_PATH * 2] = {0};
    GetModuleFileNameW(nullptr, buf, MAX_PATH * 2);
    std::wstring path = buf;
    size_t pos = path.find_last_of(L"\\/");
    return pos == std::wstring::npos ? std::wstring() : path.substr(0, pos);
}

inline std::wstring JoinPath(const std::wstring& dir, const std::wstring& name) {
    if (dir.empty()) return name;
    wchar_t last = dir.back();
    if (last == L'\\' || last == L'/') return dir + name;
    return dir + L"\\" + name;
}

inline std::wstring FileNameOf(const std::wstring& path) {
    size_t pos = path.find_last_of(L"\\/");
    return pos == std::wstring::npos ? path : path.substr(pos + 1);
}

inline std::wstring DirNameOf(const std::wstring& path) {
    size_t pos = path.find_last_of(L"\\/");
    if (pos == std::wstring::npos) return std::wstring();
    std::wstring dir = path.substr(0, pos);
    if (dir.size() == 2 && dir[1] == L':') return dir + L"\\";
    return dir;
}

inline std::wstring ExtOf(const std::wstring& name) {
    size_t pos = name.find_last_of(L'.');
    if (pos == std::wstring::npos || pos == 0) return std::wstring();
    std::wstring ext = name.substr(pos + 1);
    for (wchar_t& ch : ext) ch = static_cast<wchar_t>(std::towlower(ch));
    return ext;
}

inline std::wstring StemOf(const std::wstring& name) {
    size_t pos = name.find_last_of(L'.');
    if (pos == std::wstring::npos) return name;
    return name.substr(0, pos);
}

inline uint64_t FileSizeOf(const std::wstring& path, bool* ok = nullptr) {
    WIN32_FILE_ATTRIBUTE_DATA data{};
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &data)) {
        if (ok) *ok = false;
        return 0;
    }
    if (ok) *ok = true;
    return (static_cast<uint64_t>(data.nFileSizeHigh) << 32) | data.nFileSizeLow;
}

inline bool IsHiddenItem(const std::wstring& path) {
    const std::wstring name = FileNameOf(path);
    if (!name.empty() && name[0] == L'.') return true;
    DWORD attr = GetFileAttributesW(path.c_str());
    if (attr == INVALID_FILE_ATTRIBUTES) return false;
    return (attr & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)) != 0;
}

inline bool MakeDirs(const std::wstring& path) {
    if (path.empty()) return false;
    if (DirExists(path)) return true;
    std::wstring parent = DirNameOf(path);
    if (!parent.empty() && parent != path) {
        if (!MakeDirs(parent)) return false;
    }
    if (CreateDirectoryW(path.c_str(), nullptr)) return true;
    return GetLastError() == ERROR_ALREADY_EXISTS;
}

// 易读文件大小（UTF-8）
inline std::string HumanSize(uint64_t bytes) {
    char buf[64]{};
    if (bytes < 1024ull) {
        snprintf(buf, sizeof(buf), "%llu B", static_cast<unsigned long long>(bytes));
    } else {
        double v = static_cast<double>(bytes);
        const char* units[] = {"KB", "MB", "GB", "TB", "PB"};
        int idx = -1;
        do {
            v /= 1024.0;
            ++idx;
        } while (v >= 1024.0 && idx < 4);
        snprintf(buf, sizeof(buf), "%.1f %s", v, units[idx]);
    }
    return std::string(buf);
}

// ---------------------------------------------------------------------------
// 内容指纹：首 / 中 / 尾各 64KB 的 FNV-1a 64 位哈希 + 文件大小 + 区间偏移
// 只用于同一次任务内判断“目标里是否已有同一份视频”，不追求密码学强度。
// 只取首尾时，仅中间不同的两个视频会撞成同一指纹而被误跳过；中间加一段后
// 碰撞概率可忽略，代价是多读 64KB。
// ---------------------------------------------------------------------------
inline bool FileFingerprint(const std::wstring& path, uint64_t size, std::string* out) {
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;

    struct Range {
        uint64_t off;
        uint64_t len;
    };
    const uint64_t kChunk = 64ull * 1024ull;
    std::vector<Range> ranges;
    if (size > 0 && size <= kChunk * 2) {
        ranges.push_back({0, size});            // 小文件整块读，避免区间重叠重复读
    } else if (size > 0) {
        ranges.push_back({0, kChunk});
        // 只要超过两段就必须带上中间段：否则 128KB~192KB 的文件只比较首尾，
        // 仅中间内容不同的两个视频会被判成重复而被静默跳过（复制模式下等于漏文件）。
        ranges.push_back({size / 2 - kChunk / 2, kChunk});
        ranges.push_back({size - kChunk, kChunk});
    }

    bool ok = true;
    std::vector<std::vector<unsigned char>> chunks;
    chunks.reserve(ranges.size());
    for (const Range& r : ranges) {
        LARGE_INTEGER pos{};
        pos.QuadPart = static_cast<LONGLONG>(r.off);
        if (!SetFilePointerEx(h, pos, nullptr, FILE_BEGIN)) {
            ok = false;
            break;
        }
        std::vector<unsigned char> buffer(static_cast<size_t>(r.len));
        DWORD read = 0;
        if (!ReadFile(h, buffer.data(), static_cast<DWORD>(buffer.size()), &read, nullptr) ||
            read != buffer.size()) {
            ok = false;
            break;
        }
        chunks.push_back(std::move(buffer));
    }
    CloseHandle(h);
    if (!ok) return false;

    uint64_t h1 = 1469598103934665603ull;  // FNV offset
    auto mix = [&h1](const void* data, size_t n) {
        const unsigned char* p = static_cast<const unsigned char*>(data);
        for (size_t i = 0; i < n; ++i) {
            h1 ^= static_cast<uint64_t>(p[i]);
            h1 *= 1099511628211ull;
        }
    };
    mix(&size, sizeof(size));
    const uint64_t count = ranges.size();
    mix(&count, sizeof(count));
    for (size_t i = 0; i < ranges.size(); ++i) {
        mix(&ranges[i].off, sizeof(ranges[i].off));
        mix(&ranges[i].len, sizeof(ranges[i].len));
        if (!chunks[i].empty()) mix(chunks[i].data(), chunks[i].size());
    }

    char buf[40]{};
    snprintf(buf, sizeof(buf), "%016llx", static_cast<unsigned long long>(h1));
    *out = std::string(buf);
    return true;
}

// ---------------------------------------------------------------------------
// 回收站删除
// ---------------------------------------------------------------------------
inline bool SendToRecycleBin(const std::wstring& path, std::string* err) {
    if (!FileExists(path)) {
        if (err) *err = "文件不存在";
        return false;
    }
    // SHFileOperationW 要求 pFrom 以"双 NUL"结尾；多补一个 NUL 即可
    std::wstring buffer = path;
    buffer.push_back(L'\0');

    SHFILEOPSTRUCTW op{};
    op.hwnd = nullptr;
    op.wFunc = FO_DELETE;
    op.pFrom = buffer.c_str();
    op.pTo = nullptr;
    op.fFlags = FOF_ALLOWUNDO | FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI;
    const int rc = SHFileOperationW(&op);
    if (rc != 0) {
        if (err) *err = "移入回收站失败（错误码 " + std::to_string(rc) + "）";
        return false;
    }
    if (op.fAnyOperationsAborted) {
        if (err) *err = "操作被系统取消（文件可能正被占用）";
        return false;
    }
    if (FileExists(path)) {
        if (err) *err = "系统未删除该文件（可能正被占用）";
        return false;
    }
    return true;
}

// ---------------------------------------------------------------------------
// 系统对话框（必须在 STA 的 UI 线程调用）
// ---------------------------------------------------------------------------
inline bool PickFolderDialog(HWND owner, const std::wstring& initial, std::wstring* out) {
    IFileOpenDialog* dlg = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_FileOpenDialog, nullptr, CLSCTX_INPROC_SERVER,
                                  IID_IFileOpenDialog, reinterpret_cast<void**>(&dlg));
    if (FAILED(hr) || !dlg) return false;

    DWORD opts = 0;
    dlg->GetOptions(&opts);
    dlg->SetOptions(opts | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST);

    if (!initial.empty() && DirExists(initial)) {
        IShellItem* item = nullptr;
        if (SUCCEEDED(SHCreateItemFromParsingName(initial.c_str(), nullptr, IID_IShellItem,
                                                 reinterpret_cast<void**>(&item))) &&
            item) {
            dlg->SetFolder(item);
            item->Release();
        }
    }

    bool ok = false;
    hr = dlg->Show(owner);
    if (SUCCEEDED(hr)) {
        IShellItem* item = nullptr;
        if (SUCCEEDED(dlg->GetResult(&item)) && item) {
            PWSTR psz = nullptr;
            if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &psz)) && psz) {
                *out = psz;
                CoTaskMemFree(psz);
                ok = true;
            }
            item->Release();
        }
    }
    dlg->Release();
    return ok;
}

inline bool PickSaveFileDialog(HWND owner, const std::wstring& initialDir,
                               const std::wstring& defaultName, const wchar_t* filterText,
                               const wchar_t* pattern, const wchar_t* defaultExt,
                               std::wstring* out) {
    IFileSaveDialog* dlg = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_FileSaveDialog, nullptr, CLSCTX_INPROC_SERVER,
                                  IID_IFileSaveDialog, reinterpret_cast<void**>(&dlg));
    if (FAILED(hr) || !dlg) return false;

    // 过滤器和默认扩展名必须一起换：只改名字不改扩展名的话，
    // HTML 报告在用户输入无扩展名文件名时会被存成 .csv
    COMDLG_FILTERSPEC spec[] = {{filterText ? filterText : L"文件", pattern ? pattern : L"*.*"}};
    dlg->SetFileTypes(1, spec);
    dlg->SetDefaultExtension(defaultExt ? defaultExt : L"txt");
    dlg->SetFileName(defaultName.c_str());

    if (!initialDir.empty() && DirExists(initialDir)) {
        IShellItem* item = nullptr;
        if (SUCCEEDED(SHCreateItemFromParsingName(initialDir.c_str(), nullptr, IID_IShellItem,
                                                 reinterpret_cast<void**>(&item))) &&
            item) {
            dlg->SetDefaultFolder(item);
            item->Release();
        }
    }

    bool ok = false;
    hr = dlg->Show(owner);
    if (SUCCEEDED(hr)) {
        IShellItem* item = nullptr;
        if (SUCCEEDED(dlg->GetResult(&item)) && item) {
            PWSTR psz = nullptr;
            if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &psz)) && psz) {
                *out = psz;
                CoTaskMemFree(psz);
                ok = true;
            }
            item->Release();
        }
    }
    dlg->Release();
    return ok;
}

// 用默认关联程序打开文件 / 文件夹
inline bool ShellOpen(const std::wstring& path) {
    if (path.empty()) return false;
    HINSTANCE r = ShellExecuteW(nullptr, L"open", path.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
    return reinterpret_cast<INT_PTR>(r) > 32;
}

// 在资源管理器中定位文件
inline bool ShellReveal(const std::wstring& path) {
    if (path.empty()) return false;
    std::wstring arg = L"/select,\"" + path + L"\"";
    HINSTANCE r = ShellExecuteW(nullptr, L"open", L"explorer.exe", arg.c_str(), nullptr, SW_SHOWNORMAL);
    return reinterpret_cast<INT_PTR>(r) > 32;
}

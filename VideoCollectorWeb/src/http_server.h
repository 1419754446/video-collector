// http_server.h —— 本地 localhost HTTP 服务：提供 UI 静态文件与视频预览流（支持 Range）
#pragma once

#include "util.h"

#include <winsock2.h>

#include <atomic>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

class HttpServer {
public:
    HttpServer() = default;
    ~HttpServer();

    bool Start(const std::wstring& uiDir);
    void Stop();

    int Port() const { return port_; }
    std::string Url() const { return "http://127.0.0.1:" + std::to_string(port_) + "/"; }

    // 允许通过 /media 读取某个文件的绝对路径（预览用，防止任意文件读取）
    void AllowPath(const std::wstring& path);
    void ClearAllowed();

    // /thumb 的画布授权回调：返回 true 才允许为某个绝对路径生成缩略图。
    // 由 main.cpp 注入（仅放行当前已知的记录项），避免任意本地文件被缩略图读取。
    using ThumbAuthorizer = std::function<bool(const std::wstring& path)>;
    void SetThumbAuthorizer(ThumbAuthorizer fn) { thumbAuthorizer_ = std::move(fn); }

private:
    // 每个连接一个线程；线程对象由这里持有，Stop() 时统一收尾，
    // 不再 detach（detach 的线程会活到进程退出之后，去访问已析构的成员）。
    struct ClientEntry {
        SOCKET sock = INVALID_SOCKET;
        std::thread thread;
        std::shared_ptr<std::atomic<bool>> done;
    };

    void AcceptLoop();
    void ReapClients();
    void HandleClient(SOCKET s);
    void ServeStatic(SOCKET s, const std::string& path, bool headOnly);
    void ServeMedia(SOCKET s, const std::string& rawQuery, const std::string& rangeHeader,
                    bool headOnly);
    void ServeThumb(SOCKET s, const std::string& rawQuery, bool headOnly);
    bool SendAll(SOCKET s, const char* data, size_t len);
    bool SendHeaders(SOCKET s, const char* head);

    SOCKET listen_ = INVALID_SOCKET;
    int port_ = 0;
    std::atomic<bool> running_{false};
    std::atomic<int> activeClients_{0};
    std::thread thread_;
    std::wstring uiDir_;
    std::set<std::wstring> allowed_;
    // 预览白名单按加入顺序淘汰，避免"满了就整体清空"把正在播放的视频打断
    std::deque<std::wstring> allowedOrder_;
    std::mutex allowedMutex_;
    std::vector<ClientEntry> clients_;
    std::mutex clientsMutex_;
    ThumbAuthorizer thumbAuthorizer_;
};

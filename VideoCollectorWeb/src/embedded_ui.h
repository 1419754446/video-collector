// embedded_ui.h —— 读取编译时嵌入 exe 的 UI 静态文件
#pragma once

#include <string>

// name 为 ui 目录下的文件名（如 "index.html"），成功时写入 out 并返回 true
bool LoadEmbeddedUiFile(const std::wstring& name, std::string& out);

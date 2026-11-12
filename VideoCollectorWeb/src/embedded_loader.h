// embedded_loader.h —— 释放并加载内嵌 exe 的 WebView2Loader.dll
#pragma once

#include <windows.h>

// 从 exe 内嵌数据释放 WebView2Loader.dll 并加载；失败返回 nullptr
HMODULE LoadWebView2LoaderDll();

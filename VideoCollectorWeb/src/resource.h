// resource.h —— 资源 ID 统一定义
//
// app.rc（编译进 exe 的资源）与 main.cpp（运行时加载资源的代码）必须引用同一份定义。
// 之前 app.rc 里写的是未定义的标识符 IDI_APPICON，windres 把它当成了字符串资源名
// "IDI_APPICON"，而 main.cpp 用 MAKEINTRESOURCEW(101) 查找，导致窗口和任务栏
// 一直拿不到图标（exe 文件图标却是正常的）。
#pragma once

#define IDI_APPICON 101

@echo off
setlocal
rem ==========================================================================
rem  VideoCollectorWeb build script (MSYS2 UCRT64 g++)
rem  Produces a single self-contained exe:
rem    - UI files (ui\*.html/css/js) are embedded as C++ byte arrays
rem    - WebView2Loader is linked statically (no WebView2Loader.dll)
rem ==========================================================================

set "ROOT=%~dp0"
set "GXX=D:\SoftWareNew\msys64\ucrt64\bin\g++.exe"
set "WINDRES=D:\SoftWareNew\msys64\ucrt64\bin\windres.exe"
set "SDK=%ROOT%sdk\build\native"
set "OUT=%ROOT%bin"
set "OBJ=%ROOT%obj"

if not exist "%GXX%" (
    echo [ERROR] Compiler not found: %GXX%
    echo Edit the GXX variable in this file to point to your g++.exe
    exit /b 1
)

if not exist "%OUT%" mkdir "%OUT%"
if not exist "%OBJ%" mkdir "%OBJ%"

echo [1/3] Embedding UI files...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%tools\embed_ui.ps1" "%ROOT%" || (
    echo [ERROR] UI embedding failed.
    exit /b 1
)

rem Note: ">" inside echo must be escaped as "^>", otherwise cmd treats it as
rem a redirect and creates a junk file named "app_res.o)" in the repo root.
echo [2/3] Compiling resources (app.rc -^> app_res.o)...
if not exist "%WINDRES%" (
    echo [ERROR] windres not found: %WINDRES%
    echo Edit the WINDRES variable in this file to point to your windres.exe
    exit /b 1
)
"%WINDRES%" "%ROOT%src\app.rc" "%OBJ%\app_res.o"
if errorlevel 1 (
    echo [ERROR] Resource compile failed.
    exit /b 1
)

echo [3/3] Compiling and linking...
"%GXX%" -std=c++17 -O2 -DUNICODE -D_UNICODE -D_WIN32_WINNT=0x0A00 ^
  -I "%SDK%\include" ^
  -L "%SDK%\x64" ^
  "%ROOT%src\main.cpp" "%ROOT%src\backend.cpp" "%ROOT%src\http_server.cpp" ^
  "%ROOT%src\embedded_ui.cpp" "%ROOT%src\embedded_loader.cpp" ^
  "%OBJ%\app_res.o" ^
  -o "%OUT%\VideoCollectorWeb.exe" ^
  -lole32 -loleaut32 -luuid -luser32 -lgdi32 ^
  -lshell32 -lws2_32 -ldwmapi -lgdiplus -lbcrypt ^
  -mwindows -static -static-libgcc -static-libstdc++
if errorlevel 1 (
    echo [ERROR] Build failed.
    exit /b 1
)

echo.
echo Done: %OUT%\VideoCollectorWeb.exe  (single file, no DLL / ui folder needed)
endlocal

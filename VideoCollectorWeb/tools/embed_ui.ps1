param([string]$Root)

$ErrorActionPreference = 'Stop'

# Generate C++ byte arrays for UI static files (src\embedded_ui.cpp)
# and for WebView2Loader.dll (src\embedded_loader.cpp).
# Called by build.bat before compiling so embedded content always matches ui\.
if (-not $Root) {
    $Root = Split-Path -Parent $MyInvocation.MyCommand.Path
    $Root = Split-Path -Parent $Root
}
$Root = $Root.Trim().Trim('"').TrimEnd('\')
$uiDir = Join-Path $Root 'ui'
$sdkDir = Join-Path $Root 'sdk\build\native\x64'

function Add-ByteArray($sb, $symbol, $bytes) {
    [void]$sb.AppendLine('static const unsigned char ' + $symbol + '[] = {')
    $line = New-Object System.Text.StringBuilder
    for ($j = 0; $j -lt $bytes.Length; $j++) {
        [void]$line.Append(('0x{0:x2},' -f $bytes[$j]))
        if ((($j + 1) % 16) -eq 0) {
            [void]$sb.AppendLine($line.ToString())
            $line.Length = 0
        }
    }
    if ($line.Length -gt 0) { [void]$sb.AppendLine($line.ToString()) }
    [void]$sb.AppendLine('};')
    [void]$sb.AppendLine('')
}

# ---------------------------------------------------------------- UI static files
$files   = @('index.html', 'style.css', 'app.js', 'preview.html', 'preview.css', 'preview.js')
$symbols = @('kIndexHtml', 'kStyleCss', 'kAppJs', 'kPreviewHtml', 'kPreviewCss', 'kPreviewJs')

$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine('#include "embedded_ui.h"')
[void]$sb.AppendLine('')
[void]$sb.AppendLine('namespace {')
[void]$sb.AppendLine('struct Entry { const wchar_t* name; const unsigned char* data; unsigned int size; };')
[void]$sb.AppendLine('')

for ($i = 0; $i -lt $files.Count; $i++) {
    $path = Join-Path $uiDir $files[$i]
    $bytes = [System.IO.File]::ReadAllBytes($path)
    if ($bytes.Length -eq 0) { throw "empty ui file: $path" }
    Add-ByteArray $sb $symbols[$i] $bytes
}

[void]$sb.AppendLine('const Entry kEntries[] = {')
for ($i = 0; $i -lt $files.Count; $i++) {
    [void]$sb.AppendLine('    {L"' + $files[$i] + '", ' + $symbols[$i] + ', sizeof(' + $symbols[$i] + ')},')
}
[void]$sb.AppendLine('};')
[void]$sb.AppendLine('')
[void]$sb.AppendLine('}  // namespace')
[void]$sb.AppendLine('')
[void]$sb.AppendLine('bool LoadEmbeddedUiFile(const std::wstring& name, std::string& out) {')
[void]$sb.AppendLine('    for (const auto& e : kEntries) {')
[void]$sb.AppendLine('        if (name == e.name) {')
[void]$sb.AppendLine('            out.assign(reinterpret_cast<const char*>(e.data), e.size);')
[void]$sb.AppendLine('            return true;')
[void]$sb.AppendLine('        }')
[void]$sb.AppendLine('    }')
[void]$sb.AppendLine('    return false;')
[void]$sb.AppendLine('}')
[System.IO.File]::WriteAllText((Join-Path $Root 'src\embedded_ui.cpp'), $sb.ToString(), (New-Object System.Text.UTF8Encoding $false))
Write-Host ('embedded ui  -> ' + (Join-Path $Root 'src\embedded_ui.cpp'))

# ---------------------------------------------------------------- WebView2Loader.dll
$dllPath = Join-Path $sdkDir 'WebView2Loader.dll'
$dllBytes = [System.IO.File]::ReadAllBytes($dllPath)
if ($dllBytes.Length -eq 0) { throw "empty dll: $dllPath" }

$sb2 = New-Object System.Text.StringBuilder
[void]$sb2.AppendLine('#include "embedded_loader.h"')
[void]$sb2.AppendLine('')
[void]$sb2.AppendLine('#include <windows.h>')
[void]$sb2.AppendLine('#include <string>')
[void]$sb2.AppendLine('')
[void]$sb2.AppendLine('namespace {')
[void]$sb2.AppendLine('')
Add-ByteArray $sb2 'kWebView2LoaderDll' $dllBytes
[void]$sb2.AppendLine('const unsigned int kWebView2LoaderDllSize = sizeof(kWebView2LoaderDll);')
[void]$sb2.AppendLine('')
[void]$sb2.AppendLine('std::wstring BaseDir() {')
[void]$sb2.AppendLine('    wchar_t buf[MAX_PATH] = {};')
[void]$sb2.AppendLine('    std::wstring base;')
[void]$sb2.AppendLine('    if (GetEnvironmentVariableW(L"LOCALAPPDATA", buf, MAX_PATH) && buf[0]) {')
[void]$sb2.AppendLine('        base = buf;')
[void]$sb2.AppendLine('    } else {')
[void]$sb2.AppendLine('        wchar_t tmp[MAX_PATH] = {};')
[void]$sb2.AppendLine('        if (GetTempPathW(MAX_PATH, tmp)) base = tmp;')
[void]$sb2.AppendLine('        else base = L"C:\\";')
[void]$sb2.AppendLine('    }')
[void]$sb2.AppendLine('    if (!base.empty() && base.back() != L''\\'') base += L"\\";')
[void]$sb2.AppendLine('    base += L"VideoCollectorWeb";')
[void]$sb2.AppendLine('    CreateDirectoryW(base.c_str(), nullptr);')
[void]$sb2.AppendLine('    return base;')
[void]$sb2.AppendLine('}')
[void]$sb2.AppendLine('')
[void]$sb2.AppendLine('// write embedded dll to disk; reuse existing file when size matches')
[void]$sb2.AppendLine('bool ExtractDll(const std::wstring& path) {')
[void]$sb2.AppendLine('    WIN32_FILE_ATTRIBUTE_DATA attr{};')
[void]$sb2.AppendLine('    if (GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &attr)) {')
[void]$sb2.AppendLine('        if (attr.nFileSizeLow == kWebView2LoaderDllSize && attr.nFileSizeHigh == 0) return true;')
[void]$sb2.AppendLine('    }')
[void]$sb2.AppendLine('    HANDLE h = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS,')
[void]$sb2.AppendLine('                           FILE_ATTRIBUTE_NORMAL, nullptr);')
[void]$sb2.AppendLine('    if (h == INVALID_HANDLE_VALUE) return false;')
[void]$sb2.AppendLine('    DWORD written = 0;')
[void]$sb2.AppendLine('    const bool ok = WriteFile(h, kWebView2LoaderDll, kWebView2LoaderDllSize, &written, nullptr) &&')
[void]$sb2.AppendLine('                    written == kWebView2LoaderDllSize;')
[void]$sb2.AppendLine('    CloseHandle(h);')
[void]$sb2.AppendLine('    return ok;')
[void]$sb2.AppendLine('}')
[void]$sb2.AppendLine('')
[void]$sb2.AppendLine('}  // namespace')
[void]$sb2.AppendLine('')
[void]$sb2.AppendLine('HMODULE LoadWebView2LoaderDll() {')
[void]$sb2.AppendLine('    static HMODULE cached = []() -> HMODULE {')
[void]$sb2.AppendLine('        const std::wstring dir = BaseDir();')
[void]$sb2.AppendLine('        const std::wstring path = dir + L"\\WebView2Loader.dll";')
[void]$sb2.AppendLine('        if (ExtractDll(path)) {')
[void]$sb2.AppendLine('            HMODULE mod = LoadLibraryW(path.c_str());')
[void]$sb2.AppendLine('            if (mod) return mod;')
[void]$sb2.AppendLine('        }')
[void]$sb2.AppendLine('        // fallback: temp dir')
[void]$sb2.AppendLine('        wchar_t tmp[MAX_PATH] = {};')
[void]$sb2.AppendLine('        if (GetTempPathW(MAX_PATH, tmp)) {')
[void]$sb2.AppendLine('            std::wstring p = std::wstring(tmp) + L"VideoCollectorWeb_WebView2Loader.dll";')
[void]$sb2.AppendLine('            if (ExtractDll(p)) {')
[void]$sb2.AppendLine('                HMODULE mod = LoadLibraryW(p.c_str());')
[void]$sb2.AppendLine('                if (mod) return mod;')
[void]$sb2.AppendLine('            }')
[void]$sb2.AppendLine('        }')
[void]$sb2.AppendLine('        return nullptr;')
[void]$sb2.AppendLine('    }();')
[void]$sb2.AppendLine('    return cached;')
[void]$sb2.AppendLine('}')
[System.IO.File]::WriteAllText((Join-Path $Root 'src\embedded_loader.cpp'), $sb2.ToString(), (New-Object System.Text.UTF8Encoding $false))
Write-Host ('embedded dll -> ' + (Join-Path $Root 'src\embedded_loader.cpp'))

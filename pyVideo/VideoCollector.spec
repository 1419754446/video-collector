# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置

要点：
- assets/ 必须随包分发，否则 exe 里的窗口/任务栏图标会退化成 Windows 默认图标
  （代码通过 app/utils.py 的 resource_path() 在 _MEIPASS 下找 assets/app.ico）
- icon= 与 datas= 都用相对路径，以本 spec 所在目录为基准，换机器也能直接打包
"""


a = Analysis(
    ['video_collector.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets/app.ico', 'assets'),
        ('assets/app.png', 'assets'),
        ('assets/app.svg', 'assets'),
    ],
    hiddenimports=['cv2'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='VideoCollector',
    icon='assets/app.ico',
    # 版本资源：不填的话 exe 属性里产品名/版本号全是空的（见 version_info.txt）
    version='version_info.txt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

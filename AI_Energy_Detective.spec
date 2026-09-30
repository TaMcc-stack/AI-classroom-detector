# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import copy_metadata

datas = [('static', 'static'), ('yolo11s.pt', '.')]
binaries = [('.venv/Lib/site-packages/torchvision/_C_stable.pyd', 'torchvision'), ('.venv/Lib/site-packages/torchvision/image_stable.pyd', 'torchvision')]
datas += copy_metadata('ultralytics')
binaries += collect_dynamic_libs('torchvision')

# PyInstaller 6.x 的 VC 运行库白名单（depend/dylib.py 的 _win_includes）没有收录
# vcruntime140_threads.dll，而 torch_cpu.dll 依赖它。目标机若没装新版 VC++ 可再发行
# 组件，import torch 会在加载 torch\lib\shm.dll 时报 WinError 126。
_vc_threads = os.path.join(
    os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'vcruntime140_threads.dll'
)
if not os.path.exists(_vc_threads):
    raise SystemExit(
        f'构建中断：缺少 {_vc_threads}\n'
        '请先安装 VC++ 2015-2022 可再发行组件 (x64)：https://aka.ms/vs/17/release/vc_redist.x64.exe'
    )
binaries.append((_vc_threads, 'torch/lib'))


a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
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
    [],
    exclude_binaries=True,
    name='AI_Energy_Detective',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AI_Energy_Detective',
)

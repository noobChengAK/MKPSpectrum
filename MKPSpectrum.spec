# -*- mode: python ; coding: utf-8 -*-

import sys
import os

block_cipher = None

# 前端文件
frontend_files = [
    ('frontend/index.html', 'frontend'),
    ('frontend/app.js', 'frontend'),
    ('frontend/style.css', 'frontend'),
    ('frontend/lib/three/three.module.js', 'frontend/lib/three'),
    ('frontend/lib/three/addons/controls/OrbitControls.js', 'frontend/lib/three/addons/controls'),
    ('frontend/lib/three/addons/controls/TransformControls.js', 'frontend/lib/three/addons/controls'),
    ('frontend/lib/three/addons/loaders/STLLoader.js', 'frontend/lib/three/addons/loaders'),
    ('frontend/lib/three/addons/loaders/GLTFLoader.js', 'frontend/lib/three/addons/loaders'),
    ('frontend/lib/three/addons/loaders/OBJLoader.js', 'frontend/lib/three/addons/loaders'),
    ('frontend/lib/three/addons/loaders/MTLLoader.js', 'frontend/lib/three/addons/loaders'),
    ('frontend/lib/three/addons/loaders/FBXLoader.js', 'frontend/lib/three/addons/loaders'),
    ('frontend/lib/three/addons/utils/BufferGeometryUtils.js', 'frontend/lib/three/addons/utils'),
    # FBXLoader 依赖
    ('frontend/lib/three/addons/libs/fflate.module.js', 'frontend/lib/three/addons/libs'),
    ('frontend/lib/three/addons/curves/NURBSCurve.js', 'frontend/lib/three/addons/curves'),
    ('frontend/lib/three/addons/curves/NURBSUtils.js', 'frontend/lib/three/addons/curves'),
]

# 额外的 Python 数据文件（子进程调用或动态导入）
extra_datas = [
    ('slice_standalone.py', '.'),
    ('strip_processor.py', '.'),
]

# 图标文件 — 使用 SPECPATH（PyInstaller 内置变量，指向 spec 文件所在目录）
ico_path = os.path.join(SPECPATH, 'MKPSpectrum.ico')
print(f'[spec] 图标路径: {ico_path}  存在: {os.path.exists(ico_path)}')
if not os.path.exists(ico_path):
    raise FileNotFoundError(f'找不到图标文件: {ico_path}')

a = Analysis(
    ['MKPSpectrum.py'],
    pathex=[],
    binaries=[],
    datas=frontend_files + extra_datas,
    hiddenimports=[
        'webview',
        'webview.http',
        'webview.platforms.winforms',
        'slice_standalone',
        'strip_processor',
        'clr',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'numpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='MKPSpectrum',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ico_path,
)

# -*- mode: python ; coding: utf-8 -*-

import json
from pathlib import Path


repo_root = Path(SPECPATH)
config = json.loads(
    (repo_root / 'ril_config.json').read_text(encoding='utf-8-sig')
)
installation = config['installation']
client_build_name = Path(installation['client_executable']).stem
client_runtime_directory = installation['client_runtime_directory']
client_icon = str(repo_root / installation['icon_file'])

block_cipher = None

a = Analysis(
    ['RIL_client.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ril_config.json', '.'),
        (installation['client_ui_file'], '.'),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=['pandas', 'numpy', 'pillow'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=client_build_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
    icon=[client_icon],
    contents_directory=client_runtime_directory,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=client_build_name,
)

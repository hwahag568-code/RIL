# -*- mode: python ; coding: utf-8 -*-

import json
from pathlib import Path


repo_root = Path(SPECPATH)
config = json.loads(
    (repo_root / 'ril_config.json').read_text(encoding='utf-8-sig')
)
installation = config['installation']
server_build_name = Path(installation['server_executable']).stem
server_icon = str(repo_root / installation['icon_file'])
server_runtime_directory = installation['server_runtime_directory']
server_update_helper = installation['server_update_helper_script']
server_restarter_ps1 = installation[
    'server_restarter_power_shell_script'
]

a = Analysis(
    ['RIL_server.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ril_config.json', '.'),
        (installation['icon_file'], '.'),
        (
            str(Path('dist/make_setup') / server_update_helper),
            '.',
        ),
        (
            str(Path('dist/make_setup') / server_restarter_ps1),
            '.',
        ),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pandas', 'numpy', 'pillow'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=server_build_name,
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
    uac_admin=True,
    icon=[server_icon],
    contents_directory=server_runtime_directory,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=server_build_name,
)

# PyInstaller onedir preparation for Mamba Data Hub.
# Build manually from the repository root:
#   host_app\.venv\Scripts\pyinstaller.exe host_app\mamba_host.spec

from PyInstaller.utils.hooks import collect_submodules


a = Analysis(
    ["host_app/app.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=collect_submodules("sounddevice"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pyqtgraph"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MambaHost",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="MambaHost",
)

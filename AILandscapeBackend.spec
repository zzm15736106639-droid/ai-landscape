from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH)
datas = [
    (str(ROOT / "frameshift" / "models" / "face_detection_yunet_2023mar.onnx"), "frameshift/models"),
    (str(ROOT / "static"), "static"),
    (str(ROOT / "assets" / "fonts"), "subtitle_fonts"),
]
datas += collect_data_files("cv2")
hiddenimports = collect_submodules("cv2")

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "_pytest", "numpy.tests", "numpy.typing.tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AILandscapeBackend",
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
)

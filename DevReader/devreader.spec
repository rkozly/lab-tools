# PyInstaller spec for DevReader.  Build with:  pyinstaller devreader.spec --noconfirm
# Produces dist/DevReader/DevReader.exe (one-folder build: fast start-up, easy to share).

import os

block_cipher = None
ICON = os.path.join("devreader", "resources", "devreader.ico")

a = Analysis(
    ["run_devreader.py"],
    pathex=["."],
    binaries=[],
    datas=[("devreader/resources", "devreader/resources")] if os.path.isdir("devreader/resources") else [],
    hiddenimports=[],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtPdf"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DevReader",
    console=False,
    icon=ICON if os.path.exists(ICON) else None,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, name="DevReader")

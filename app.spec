# app.spec — Mask POS (Windows)
# Build architecture depends on the Python used in your venv (32-bit venv => 32-bit EXE).
# WITH matplotlib, NO pandas

from PyInstaller.utils.hooks import collect_all, collect_submodules
from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT
from PyInstaller.building.datastruct import Tree
import os

datas = []
binaries = []
hiddenimports = []


def add_pkg(pkg: str):
    """Collect package datas/binaries/hiddenimports safely."""
    try:
        d, b, h = collect_all(pkg)
        datas.extend(d)
        binaries.extend(b)
        hiddenimports.extend(h)
    except Exception as e:
        print(f"[spec] WARN: could not collect {pkg}: {e}")


# -------------------------------------------------
# Core runtime packages
# -------------------------------------------------
for pkg in [
    "requests",
    "urllib3",
    "reportlab",
    "openpyxl",
    "fastapi",
    "starlette",
    "pydantic",
    "uvicorn",
    "h11",
    "pyautogui",
    "pygetwindow",
]:
    add_pkg(pkg)


# -------------------------------------------------
# ReportLab barcodes (extra safety)
# -------------------------------------------------
try:
    hiddenimports += collect_submodules("reportlab.graphics.barcode")
except Exception:
    pass

hiddenimports += [
    "reportlab.graphics.barcode.code93",
    "reportlab.graphics.barcode.code128",
    "reportlab.graphics.barcode.code39",
    "reportlab.graphics.barcode.eanbc",
    "reportlab.graphics.barcode.usps",
    "reportlab.graphics.barcode.qr",
]


# -------------------------------------------------
# Matplotlib
# -------------------------------------------------
add_pkg("matplotlib")

hiddenimports += [
    "matplotlib.backends.backend_tkagg",
    "matplotlib.backends.backend_agg",
]


# -------------------------------------------------
# Server / internal modules
# -------------------------------------------------
hiddenimports += [
    "uvicorn.main",
    "uvicorn.config",
    "uvicorn.server",
    "server",
    "daily_report",
    "supabase_sync",
    # "server_new",  # only if it exists in your project
]


# -------------------------------------------------
# Runtime files next to EXE
# -------------------------------------------------
runtime_files = [
    "daily_report.py",
    "pos.db",
    "pos_config.json",
    "config.json",
    "products.csv",
    "SumatraPDF.exe",
    "SumatraPDF-settings.txt",
    "cloudflare_pos_config.json",
]

for f in runtime_files:
    if os.path.exists(f):
        datas.append((f, "."))
    else:
        package_src = os.path.join("package_runtime", f)
        if os.path.exists(package_src):
            datas.append((package_src, "."))


# -------------------------------------------------
# Include folders safely (Tree -> (src, dest_dir))
# Fixes: ValueError too many values to unpack
# -------------------------------------------------
for folder in ["assets"]:
    if os.path.isdir(folder):
        for dest, src, _ in Tree(folder, prefix=folder):
            dest_dir = os.path.dirname(dest) or "."
            datas.append((src, dest_dir))


# -------------------------------------------------
# Analysis
# -------------------------------------------------
a = Analysis(
    ["app.py"],
    pathex=[os.getcwd()],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pandas"],  # keep pandas excluded
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)


# -------------------------------------------------
# Executable
# -------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MaskPOS",
    debug=False,
    strip=False,
    upx=True,
    console=False,
    icon="assets/maskpos_icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    contents_directory="_internal",
    name="MaskPOS",
)

# Mask POS 32-bit package.
# Build with a 32-bit Python interpreter. Matplotlib is excluded because modern
# Windows 32-bit wheels are not available; Analytics shows a friendly fallback.

from PyInstaller.utils.hooks import collect_all, collect_submodules
from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT
from PyInstaller.building.datastruct import Tree
import os

datas = []
binaries = []
hiddenimports = []


def add_pkg(pkg: str):
    try:
        d, b, h = collect_all(pkg)
        datas.extend(d)
        binaries.extend(b)
        hiddenimports.extend(h)
    except Exception as e:
        print(f"[spec32] WARN: could not collect {pkg}: {e}")


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
    "uvicorn.main",
    "uvicorn.config",
    "uvicorn.server",
    "app_update",
    "server",
    "daily_report",
    "supabase_sync",
]

runtime_files = [
    "daily_report.py",
    "products.csv",
    "SumatraPDF.exe",
    "SumatraPDF-settings.txt",
    "PdfPreview.dll",
    "PdfFilter.dll",
    "libmupdf.dll",
]

for f in runtime_files:
    package32_src = os.path.join("package_runtime32", f)
    package_src = os.path.join("package_runtime", f)
    if os.path.exists(package32_src):
        datas.append((package32_src, "."))
    elif os.path.exists(f):
        datas.append((f, "."))
    elif os.path.exists(package_src):
        datas.append((package_src, "."))

for folder in ["assets", "SumatraPDF"]:
    if os.path.isdir(folder):
        for dest, src, _ in Tree(folder, prefix=folder):
            dest_dir = os.path.dirname(dest) or "."
            datas.append((src, dest_dir))

a = Analysis(
    ["app.py"],
    pathex=[os.getcwd()],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "pandas"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

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

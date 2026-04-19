# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[
        ('ffmpeg.exe', '.'),
        ('ffprobe.exe', '.'),
    ],
    datas=[
        ('icon.ico', '.'),
    ],
    hiddenimports=['piexif', 'win32api', 'win32file', 'win32con', 'pywintypes', 'imagehash'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy ML/scientific packages that are NOT used by this app.
        # They may be present in the build environment but must not bloat the .exe.
        'torch', 'torchvision', 'torchaudio',
        'numpy', 'scipy', 'sklearn', 'matplotlib',
        'tensorflow', 'keras',
        'cv2', 'IPython', 'jupyter',
        'pandas', 'sympy', 'bokeh',
        'PyQt5', 'tkinter',
        'cryptography', 'Crypto',
    ],
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
    name='ExifManager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',
)

# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['installer.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\data\\images\\app_icon.ico', 'data/images'), ('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\assets\\SteamlessController_seethrough_64.png', 'assets'), ('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\data\\fonts\\PlusJakartaSans-Regular.ttf', 'data/fonts'), ('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\install_uia_relay.ps1', '.')],
    hiddenimports=['tkinter', 'tkinter.filedialog', 'tkinter.messagebox', 'autostart'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'scipy', 'PIL', 'pandas', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'IPython', 'pytest', 'setuptools', 'pip', 'pystray', 'pynput', 'vgamepad', 'sdl3w', 'hid'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SteamlessInput-Setup',
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
    icon=['C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\data\\images\\app_icon.ico'],
)

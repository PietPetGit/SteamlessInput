# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['tray.py'],
    pathex=[],
    binaries=[('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\sdl3w\\dll\\SDL3.dll', '.'), ('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\sdl3w\\dll\\SDL3_ttf.dll', '.'), ('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\vgamepad\\win\\vigem\\client\\x64\\ViGEmClient.dll', 'vgamepad/win/vigem/client/x64'), ('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\vgamepad\\win\\vigem\\client\\x86\\ViGEmClient.dll', 'vgamepad/win/vigem/client/x86')],
    datas=[('C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\data', 'data')],
    hiddenimports=['pystray._win32', 'pynput.keyboard._win32', 'pynput.mouse._win32', 'PIL._tkinter_finder', 'PIL.ImageTk', 'vgamepad', 'vgamepad.win.virtual_gamepad', 'vgamepad.win.vigem_client', 'vgamepad.win.vigem_commons', 'sdl3w', 'nintendo_bt', 'keybinds_picker', 'tutorial', 'tkinter', 'tkinter.ttk', 'media_demo', 'winrt.runtime', 'winrt.system', 'winrt.windows.foundation', 'winrt.windows.media', 'winrt.windows.media.core', 'winrt.windows.media.playback', 'winrt.windows.storage', 'winrt.windows.storage.streams'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'scipy', 'PIL._avif', 'PIL.AvifImagePlugin', 'pillow_avif', 'pandas', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'IPython', 'pytest', 'setuptools', 'pip'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SteamlessInput-windows',
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
    icon=['C:\\Users\\Administrator\\Desktop\\SteamlessKeyboard-main\\SteamlessKeyboard-main\\windows\\data\\images\\app_icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SteamlessInput-windows',
)

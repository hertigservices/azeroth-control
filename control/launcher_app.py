# -*- coding: utf-8 -*-
"""Azeroth Realm - a native window around the launcher.

The panel is already a clean split: control.py serves the API, launcher.html is
the UI.  This adds only the missing third piece, a window that owns the page,
and deliberately duplicates nothing else.

Three rules shape it:

  * If a panel is already listening, attach to it.  Two listeners would fight
    over the port and, far worse, over the game processes they both manage - so
    this never starts a second one.  Launching the exe while the tray panel is
    up simply gives that panel a window.
  * The hub's Python and HTML are loaded from disk, never bundled.  refdata.json
    alone is 5.6 MB, and a frozen copy of launcher.html would silently go stale
    the next time it is edited.  The exe is a shell; the hub folder is the app.
  * One window per machine.  A second launch raises the first rather than
    opening a rival window onto the same realm.

Run from source with the hub's own Python, or frozen by tools/build-launcher.py.
"""
import ctypes
import ctypes.wintypes as wt
import importlib.util
import os
import socket
import sys
import threading

APP_TITLE = 'Azeroth Realm'
APP_ID = 'AzerothControl.Launcher'
MUTEX_NAME = 'Local\\AzerothControlLauncher'
ICON_NAME = 'AzerothControl.ico'
# Both are outer window sizes, so each carries ~16px of frame plus headroom over
# the two widths the nav bar was measured at: 1506px of client width shows every
# tab and button unshrunk, and 1365px is the floor below which the brand and the
# Hub/Classic buttons stop wrapping and start clipping mid-word.
WIN_SIZE = (1560, 940)
WIN_MIN = (1420, 720)
BACKDROP = '#0d0d0f'          # --background, so the frame does not flash white


# --------------------------------------------------------------- locating the hub

def control_dir():
    """Find the directory holding control.py.

    Frozen, the exe sits in the hub root or beside the panel; from source this
    file already lives in control/.  AZCTL_HOME overrides both, which is how a
    copy of the hub on another drive gets used without moving the exe.
    """
    def hit(d):
        for c in (d, os.path.join(d, 'control')):
            if os.path.isfile(os.path.join(c, 'control.py')):
                return os.path.abspath(c)
        return None

    env = os.environ.get('AZCTL_HOME')
    if env:
        found = hit(env)
        if found:
            return found

    base = os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, 'frozen', False) else __file__))
    # Walk up rather than checking fixed offsets: --onedir lands the exe in
    # app\AzerothControl\, two levels below the hub, and dropping that folder
    # somewhere else should still work as long as it is inside the hub.
    d = base
    for _ in range(6):
        found = hit(d)
        if found:
            return found
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def load_control(ctl_dir):
    """Import control.py from disk by path.

    Deliberately dynamic: a plain `import control` would let PyInstaller follow
    the import and freeze a copy of the whole panel into the exe, which is the
    one thing this design is trying to avoid.
    """
    # control.py resolves ROOT from its own __file__, but its request handler
    # serves files relative to the process cwd - so both have to be right.
    os.chdir(ctl_dir)
    if ctl_dir not in sys.path:
        sys.path.insert(0, ctl_dir)
    spec = importlib.util.spec_from_file_location(
        'control', os.path.join(ctl_dir, 'control.py'))
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: control.py ends with `launcher.bind(sys.modules[__name__])`,
    # which needs to find itself under the name it was imported as.
    sys.modules['control'] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------- the server

def port_busy(port, host='127.0.0.1'):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        return s.connect_ex((host, port)) == 0


def serve_background(control):
    """Run control.py's own server in this process, on a daemon thread.

    A frozen exe cannot rely on a python.exe being on PATH to spawn the panel as
    a subprocess, and re-executing ourselves would need a second window to hide.
    Hosting it here keeps it to one process that dies cleanly with the window.
    """
    srv = control.Server(('127.0.0.1', control.PORT), control.Handler)
    t = threading.Thread(target=srv.serve_forever, name='azctl-http', daemon=True)
    t.start()
    return srv


# --------------------------------------------------------------- single instance

def claim_single_instance():
    """True if we are the first window; False if one is already up (and raised)."""
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateMutexW.argtypes = [wt.LPCVOID, wt.BOOL, wt.LPCWSTR]
    k32.CreateMutexW.restype = wt.HANDLE
    handle = k32.CreateMutexW(None, True, MUTEX_NAME)
    already = ctypes.get_last_error() == 183          # ERROR_ALREADY_EXISTS
    if not handle:
        return True                                   # cannot tell; let it run
    if already:
        raise_existing_window()
        return False
    # Leak the handle on purpose: it must outlive this function for the whole
    # run, and Windows releases it when the process exits.
    globals()['_MUTEX'] = handle
    return True


def raise_existing_window():
    try:
        u32 = ctypes.WinDLL('user32', use_last_error=True)
        u32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
        u32.FindWindowW.restype = wt.HWND
        hwnd = u32.FindWindowW(None, APP_TITLE)
        if hwnd:
            u32.ShowWindow(hwnd, 9)                   # SW_RESTORE
            u32.SetForegroundWindow(hwnd)
    except Exception:
        pass                                          # raising is a nicety, not a job


# ------------------------------------------------------------------ taskbar identity

def icon_path():
    """The .ico on disk, frozen or not.

    PyInstaller ships it as a data file rather than only as the exe's resource,
    so the same LoadImageW path works when running from source - otherwise the
    window would wear Python's icon during development.
    """
    if getattr(sys, 'frozen', False):
        p = os.path.join(getattr(sys, '_MEIPASS', ''), ICON_NAME)
        if os.path.isfile(p):
            return p
    ctl = control_dir()
    if ctl:
        p = os.path.join(os.path.dirname(ctl), 'tools', ICON_NAME)
        if os.path.isfile(p):
            return p
    return None


def set_app_id():
    """Give the process its own taskbar identity.

    Without this Windows groups the window under whatever is hosting it - which
    for an unfrozen run is python.exe, so the launcher would share a taskbar
    button (and icon) with any other Python window.
    """
    try:
        ctypes.WinDLL('shell32').SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def apply_window_icon():
    """Push the icon onto the window itself once it exists.

    The exe's own resource covers the taskbar for the frozen build, but the
    title-bar icon and the from-source case both need an explicit WM_SETICON.
    """
    path = icon_path()
    if not path:
        return
    try:
        u32 = ctypes.WinDLL('user32', use_last_error=True)
        u32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
        u32.FindWindowW.restype = wt.HWND
        hwnd = u32.FindWindowW(None, APP_TITLE)
        if not hwnd:
            return
        u32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                                   ctypes.c_int, ctypes.c_int, wt.UINT]
        u32.LoadImageW.restype = wt.HANDLE
        IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        # Ask for each size explicitly; LR_DEFAULTSIZE alone yields one 32x32
        # that Windows then squashes into the 16x16 title-bar slot.
        for which, cx, cy in ((ICON_BIG, 32, 32), (ICON_SMALL, 16, 16)):
            h = u32.LoadImageW(None, path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
            if not h:
                h = u32.LoadImageW(None, path, IMAGE_ICON, 0, 0,
                                   LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                u32.SendMessageW(hwnd, WM_SETICON, which, h)
    except Exception:
        pass                                          # cosmetic; never fatal


def alert(text):
    try:
        ctypes.WinDLL('user32').MessageBoxW(None, text, APP_TITLE, 0x10)
    except Exception:
        sys.stderr.write(text + '\n')


# ------------------------------------------------------------------------- main

def main():
    debug = '--debug' in sys.argv

    # Before any window exists, or the shell has already grouped us elsewhere.
    set_app_id()

    if not claim_single_instance():
        return 0

    ctl_dir = control_dir()
    if not ctl_dir:
        alert('Could not find control.py.\n\n'
              'Put AzerothControl.exe in the hub folder (the one containing '
              '"control"), or set AZCTL_HOME to the hub path.')
        return 2

    try:
        control = load_control(ctl_dir)
    except Exception as e:
        alert('The panel failed to load from:\n%s\n\n%s: %s'
              % (ctl_dir, type(e).__name__, e))
        return 3

    port = control.PORT
    attached = port_busy(port)
    if not attached:
        try:
            serve_background(control)
        except Exception as e:
            alert('Could not start the panel on port %d.\n\n%s: %s'
                  % (port, type(e).__name__, e))
            return 4

    url = 'http://127.0.0.1:%d/launcher' % port

    try:
        import webview
    except ImportError:
        alert('pywebview is not installed for this interpreter.\n\n'
              'The panel is running - open %s in a browser.' % url)
        return 5

    window = webview.create_window(APP_TITLE, url,
                                   width=WIN_SIZE[0], height=WIN_SIZE[1],
                                   min_size=WIN_MIN, background_color=BACKDROP)
    # The HWND does not exist until the window is shown, so the icon has to be
    # applied from the event rather than before start().
    try:
        window.events.shown += apply_window_icon
    except Exception:
        pass
    try:
        # Pin the backend: pywebview would otherwise fall back to the MSHTML
        # (IE11) engine if WebView2 is missing, and launcher.html would render
        # as an unstyled mess rather than saying what is actually wrong.
        webview.start(gui='edgechromium', debug=debug)
    except Exception as e:
        alert('The window could not open - the Microsoft Edge WebView2 runtime '
              'is probably missing.\n\n%s: %s\n\nThe panel is still running: %s'
              % (type(e).__name__, e, url))
        return 6
    return 0


if __name__ == '__main__':
    sys.exit(main())

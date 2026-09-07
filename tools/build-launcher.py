# -*- coding: utf-8 -*-
"""Freeze control/launcher_app.py into <hub>\\app\\AzerothControl.exe.

Run with the build venv's interpreter:

    tools\\appbuild-venv\\Scripts\\python.exe tools\\build-launcher.py

Two choices worth knowing about:

  * --onedir, not --onefile.  onefile unpacks the whole bundle to a temp folder
    on every launch, which is slow and makes an unsigned exe far more likely to
    trip SmartScreen.  onedir starts immediately and keeps the DLLs visible.
  * The panel itself is NOT bundled.  launcher_app.py loads control.py from disk
    by path, so editing launcher.html or control.py takes effect on the next
    window without rebuilding anything here.  Rebuild only when launcher_app.py
    or the icon changes.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = os.path.dirname(HERE)
ENTRY = os.path.join(HUB, 'control', 'launcher_app.py')
ICON = os.path.join(HERE, 'AzerothControl.ico')
DIST = os.path.join(HUB, 'app')
WORK = os.path.join(HERE, 'appbuild-cache')
NAME = 'AzerothControl'


def main():
    for path, what in ((ENTRY, 'launcher_app.py'), (ICON, 'the icon')):
        if not os.path.isfile(path):
            sys.exit('missing %s: %s' % (what, path))

    # PyInstaller writes the spec next to the cwd; keep it out of the hub root.
    cmd = [sys.executable, '-m', 'PyInstaller',
           '--noconfirm', '--clean',
           '--onedir', '--windowed',
           '--name', NAME,
           '--icon', ICON,
           # Also ship it as a data file: the exe resource drives the taskbar,
           # but WM_SETICON needs a real path to LoadImageW from.
           '--add-data', ICON + os.pathsep + '.',
           '--distpath', DIST,
           '--workpath', WORK,
           '--specpath', WORK,
           # control.py is imported by path at runtime; if PyInstaller ever
           # picks it up transitively, the frozen copy would shadow the real one.
           '--exclude-module', 'control',
           '--exclude-module', 'launcher',
           '--exclude-module', 'panel',
           '--exclude-module', 'realms',
           ENTRY]

    print(' '.join(cmd))
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        sys.exit('PyInstaller failed (%d)' % r.returncode)

    exe = os.path.join(DIST, NAME, NAME + '.exe')
    if not os.path.isfile(exe):
        sys.exit('build reported success but %s is missing' % exe)

    total = sum(os.path.getsize(os.path.join(dp, f))
                for dp, _, fs in os.walk(os.path.join(DIST, NAME)) for f in fs)
    print()
    print('built : %s' % exe)
    print('size  : %.1f MB in %s' % (total / 1048576.0, os.path.join(DIST, NAME)))
    print()
    print('The hub is not bundled - this exe reads <hub>\\control from disk.')


if __name__ == '__main__':
    main()

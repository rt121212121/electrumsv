# -*- mode: python -*-

import os
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs

PYHOME = 'c:/python3'

cmdline_name = "ElectrumSV"
home = 'C:\\electrum\\'

# Add libusb binary
binaries = [(PYHOME+"/libusb-1.0.dll", ".")]

# Workaround for "Retro Look":
binaries += [b for b in collect_dynamic_libs('PyQt5') if 'qwindowsvista' in b[0]]

binaries += [('C:/tmp/libsecp256k1.dll', '.')]

datas = [
    (home+'electrumsv/data', 'electrumsv/data'),
    ('C:\\Program Files (x86)\\ZBar\\bin\\', '.'),
]

# We don't put these files in to actually include them in the script but to make the
# Analysis method scan them for imports
a = Analysis([home+'electrum-sv'],
             binaries=binaries,
             datas=datas)

# http://stackoverflow.com/questions/19055089/pyinstaller-onefile-warning-pyconfig-h-when-importing-scipy-or-scipy-signal
for d in a.datas:
    if 'pyconfig' in d[0]:
        a.datas.remove(d)
        break

# Strip out parts of Qt that we never use to reduce binary size
# Note we need qtdbus and qtprintsupport.
qt_bins2remove = {'qt5web', 'qt53d', 'qt5game', 'qt5designer', 'qt5quick',
                  'qt5location', 'qt5test', 'qt5xml', r'pyqt5\qt\qml\qtquick'}
for x in a.binaries.copy():
    lower = x[0].lower()
    if lower in qt_bins2remove:
        a.binaries.remove(x)

qt_data2remove=(r'pyqt5\qt\translations\qtwebengine_locales', )
print("Removing Qt datas:", *qt_data2remove)
for x in a.datas.copy():
    for r in qt_data2remove:
        if x[0].lower().startswith(r.lower()):
            a.datas.remove(x)
            print('----> Removed x =', x)

# hotfix for #3171 (pre-Win10 binaries)
a.binaries = [x for x in a.binaries if not x[1].lower().startswith(r'c:\windows')]

pyz = PYZ(a.pure)


#####
# "standalone" exe with all dependencies packed into it

exe_standalone = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name=os.path.join('build\\pyi.win32\\electrum', cmdline_name + ".exe"),
    debug=False,
    strip=None,
    upx=False,
    icon=home+'electrumsv\\data\\icons\\electrum-sv.ico',
    console=False)
    # console=True makes an annoying black box pop up, but it does make Electrum output command line commands, with this turned off no output will be given but commands can still be used

exe_portable = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas + [ ('is_portable', 'README.md', 'DATA' ) ],
    name=os.path.join('build\\pyi.win32\\electrum', cmdline_name + "-portable.exe"),
    debug=False,
    strip=None,
    upx=False,
    icon=home+'electrumsv\\data\\icons\\electrum-sv.ico',
    console=False)

#####
# exe and separate files that NSIS uses to build installer "setup" exe

exe_dependent = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name=os.path.join('build\\pyi.win32\\electrum', cmdline_name),
    debug=False,
    strip=None,
    upx=False,
    icon=home+'electrumsv\\data\\icons\\electrum-sv.ico',
    console=False)

coll = COLLECT(
    exe_dependent,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=None,
    upx=True,
    debug=False,
    icon=home+'electrumsv\\data\\icons\\electrum-sv.ico',
    console=False,
    name=os.path.join('dist', 'electrum'))

"""CS2 Porter launcher (double-click)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    # show the program's own icon in the taskbar instead of the Python icon
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("tehlikeli91.CS2Porter")
except Exception:  # noqa: BLE001
    pass

from cs2porter.app import main  # noqa: E402

main()

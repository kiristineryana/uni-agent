import os
import sys

if sys.platform == "win32":
    # Load the system's (newest) MSVC++ runtime before anything else. The winrt package (Windows OCR) bundles an
    # old msvcp140.dll; if that copy loads first, ctranslate2 (voice transcription) crashes the process with an
    # access violation. Once a DLL is loaded, later loads by name reuse it, and newer runtimes are backward-compatible.
    import ctypes

    try:
        ctypes.WinDLL(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "msvcp140.dll"))
    except OSError:
        pass

from __future__ import annotations

import sys
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "startup_error.log"
RUNTIME_FILE = ROOT / "startup_runtime.log"


def show_error(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Startup failed", message)
        root.destroy()
    except Exception:
        pass


try:
    try:
        if LOG_FILE.exists():
            LOG_FILE.unlink()
    except OSError:
        pass
    RUNTIME_FILE.write_text(
        f"executable={sys.executable}\nversion={sys.version}\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(ROOT))
    from ip_exchange_gui import App

    App().mainloop()
except Exception:
    detail = traceback.format_exc()
    LOG_FILE.write_text(detail, encoding="utf-8")
    show_error(f"Startup failed. Details were written to:\n{LOG_FILE}")

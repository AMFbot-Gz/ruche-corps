"""
computer/input.py — Mains de La Ruche
Click, type, hotkeys, scroll, drag — PyAutoGUI sérialisé + safe
"""
import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import pyautogui

pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.25

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gui")
_gui_lock  = asyncio.Lock()


async def _gui(fn, *args, **kwargs):
    """Toujours dans un thread dédié + lock pour éviter les race conditions."""
    async with _gui_lock:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, lambda: fn(*args, **kwargs))


# ─── Position ─────────────────────────────────────────────────────────────────
async def get_position() -> dict:
    x, y = pyautogui.position()
    return {"x": x, "y": y}

async def get_screen_size() -> dict:
    w, h = pyautogui.size()
    return {"width": w, "height": h}


# ─── Souris ───────────────────────────────────────────────────────────────────
async def click(x: int, y: int, button: str = "left", clicks: int = 1) -> dict:
    try:
        await _gui(pyautogui.click, x, y, button=button, clicks=clicks)
        await asyncio.sleep(0.3)
        return {"ok": True, "action": "click", "x": x, "y": y, "button": button}
    except pyautogui.FailSafeException:
        return {"ok": False, "error": "FailSafe — souris en coin supérieur gauche"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def double_click(x: int, y: int) -> dict:
    return await click(x, y, clicks=2)

async def right_click(x: int, y: int) -> dict:
    return await click(x, y, button="right")

async def move(x: int, y: int, duration: float = 0.3) -> dict:
    try:
        await _gui(pyautogui.moveTo, x, y, duration=duration)
        return {"ok": True, "action": "move", "x": x, "y": y}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> dict:
    try:
        await _gui(pyautogui.moveTo, x1, y1)
        await _gui(pyautogui.dragTo, x2, y2, duration=duration, button="left")
        return {"ok": True, "action": "drag", "from": [x1, y1], "to": [x2, y2]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def scroll(x: int, y: int, clicks: int = 3) -> dict:
    try:
        await _gui(pyautogui.moveTo, x, y)
        await _gui(pyautogui.scroll, clicks)
        return {"ok": True, "action": "scroll", "clicks": clicks}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Clavier ──────────────────────────────────────────────────────────────────
def _type_safe(text: str, interval: float = 0.04):
    """Frappe unicode via clipboard (gère accents, emojis)."""
    try:
        import pyperclip
        pyperclip.copy(text)
        pyautogui.hotkey("command", "v")
    except ImportError:
        # fallback ASCII uniquement
        safe = "".join(c for c in text if ord(c) < 128)
        pyautogui.typewrite(safe, interval=interval)

async def type_text(text: str, interval: float = 0.04) -> dict:
    try:
        await _gui(_type_safe, text, interval)
        return {"ok": True, "action": "type", "chars": len(text)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def hotkey(*keys: str) -> dict:
    """Ex: hotkey('command', 'c')  hotkey('ctrl', 'shift', 'escape')"""
    try:
        await _gui(pyautogui.hotkey, *keys)
        return {"ok": True, "action": "hotkey", "keys": list(keys)}
    except pyautogui.FailSafeException:
        return {"ok": False, "error": "FailSafe déclenché"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def press(key: str) -> dict:
    try:
        await _gui(pyautogui.press, key)
        return {"ok": True, "action": "press", "key": key}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Applications macOS ───────────────────────────────────────────────────────
async def open_app(app_name: str) -> dict:
    try:
        result = subprocess.run(["open", "-a", app_name], capture_output=True, timeout=10)
        ok = result.returncode == 0
        return {"ok": ok, "app": app_name, "error": result.stderr.decode() if not ok else ""}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def focus_app(app_name: str) -> dict:
    script = f'tell application "{app_name}" to activate'
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        return {"ok": result.returncode == 0, "app": app_name}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def run_applescript(script: str) -> dict:
    try:
        result = subprocess.run(["osascript", "-e", script],
                                 capture_output=True, text=True, timeout=30)
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

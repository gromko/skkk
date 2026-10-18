#!/usr/bin/env python3
"""Visible classroom screen-sharing agent. Python 3.10+."""
import argparse
import ctypes
import ctypes.util
import getpass
import hashlib
import json
import math
import os
import platform
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from io import BytesIO

from PIL import Image

from common import (MAX_FRAME_BYTES, MAX_MESSAGE_CHARS, MSG_DEMO, MSG_FILE_DATA,
                    MSG_FILE_END, MSG_FILE_RESULT, MSG_FILE_START, MSG_FRAME,
                    MSG_INPUT, MSG_INPUT_STATE, MSG_LOCK, MSG_LOCK_STATE,
                    MSG_POWER,
                    MSG_POWER_STATE, MSG_STOP, MSG_TEXT, MIN_TOKEN_LEN,
                    MSG_VIDEO_RESULT, MSG_VIDEO_STOP, MSG_YOUTUBE,
                    app_dir, app_icon_path,
                    capture_jpeg,
                    client_handshake, default_save_dir, ensure_firewall_port,
                    ensure_time_sync,
                    clean_env, format_size,
                    is_risky_file, is_video_file, is_youtube_url,
                    load_config, load_token, new_mss, discover_teacher,
                    program_command, program_path,
                    recv_packet, safe_filename, send_packet, token_file_path,
                    student_setup_complete)

try:
    import tkinter as tk
except ImportError:      # tkinter не встановлено — працюємо без вікон
    tk = None

ImageTk = None
if tk is not None:
    try:
        from PIL import ImageTk
    except ImportError:  # Pillow без підтримки Tk — немає показу демонстрації
        ImageTk = None


class PowerManager:
    """Вимкнення комп'ютера за командою вчителя з відліком.
    Працює у фонових потоках і не залежить від tkinter. Відлік не переривається
    при втраті зв'язку; скасувати його може лише вчитель."""

    MAX_DELAY = 600

    def __init__(self, min_delay=10):
        self.min_delay = min_delay
        self._lock = threading.Lock()
        self._deadline = None
        self._gen = 0
        self.state = "none"
        self.error = ""
        self.version = 0

    # --- керування ---
    def request(self, action, delay=0):
        with self._lock:
            if action == "shutdown":
                delay = min(self.MAX_DELAY, max(self.min_delay, int(delay)))
                self._gen += 1
                self._deadline = time.time() + delay
                self.state, self.error = "pending", ""
                self.version += 1
                threading.Thread(target=self._run, args=(self._gen,), daemon=True).start()
            elif action == "cancel" and self.state == "pending":
                self._gen += 1
                self._deadline = None
                self.state = "cancelled"
                self.version += 1

    def snapshot(self):
        with self._lock:
            return self.version, self.state, self._remaining_locked(), self.error

    def remaining(self):
        """Секунди до вимкнення або None, якщо вимкнення не заплановано."""
        with self._lock:
            return self._remaining_locked() if self.state in ("pending", "executing") else None

    def _remaining_locked(self):
        if self.state == "pending" and self._deadline is not None:
            return max(0, math.ceil(self._deadline - time.time()))
        return 0

    # --- виконання ---
    def _run(self, gen):
        while True:
            with self._lock:
                if gen != self._gen or self.state != "pending":
                    return
                due = time.time() >= self._deadline
            if due:
                break
            time.sleep(0.2)
        with self._lock:
            if gen != self._gen:
                return
            self.state = "executing"
            self.version += 1
        ok, error = self._execute()
        if ok:
            time.sleep(60)    # якщо система досі працює — команду не виконано фактично
            error = "Команду прийнято, але комп'ютер не вимкнувся"
        with self._lock:
            if gen == self._gen and self.state == "executing":
                self.state, self.error = "failed", error
                self.version += 1

    @staticmethod
    def commands():
        system = platform.system()
        if system == "Windows":
            return [["shutdown", "/s", "/f", "/t", "0"]]
        if system == "Darwin":
            return [["osascript", "-e", 'tell application "System Events" to shut down'],
                    ["shutdown", "-h", "now"]]
        return [["systemctl", "poweroff"], ["systemctl", "poweroff", "-i"],
                ["loginctl", "poweroff"], ["shutdown", "-h", "now"], ["poweroff"]]

    def _execute(self):
        error = "Не знайдено команди вимкнення"
        flags = 0x08000000 if sys.platform == "win32" else 0     # CREATE_NO_WINDOW
        env = clean_env()
        for command in self.commands():
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                        creationflags=flags, env=env)
            except (OSError, subprocess.SubprocessError) as exc:
                error = str(exc)[:200]
                continue
            if result.returncode == 0:
                return True, ""
            error = ((result.stderr or result.stdout).strip() or f"код {result.returncode}")[:200]
        return False, error


class ScreenLocker:
    """Блокування комп'ютера учня: екран перекривається написом, клавіатура й миша
    блокуються. Усе виконується в головному потоці Tk.

    Windows: глобальні low-level хуки клавіатури та миші (WH_KEYBOARD_LL/WH_MOUSE_LL)
             поглинають усі події; додатково BlockInput (діє, якщо запущено від адміністратора).
             Ctrl+Alt+Del система не дозволяє перехопити ніяк.
    Linux:   X11 — ексклюзивний захоплення клавіатури та вказівника (XGrabKeyboard/XGrabPointer);
             будь-яка сесія — ексклюзивне захоплення /dev/input/event* (EVIOCGRAB), якщо є доступ
             (група input або root). У Wayland без доступу до /dev/input введення заблокувати
             неможливо — лишається лише перекриття екрана (mode == "visual").
    """
    EVIOCGRAB = 0x40044590
    TITLE = "Комп'ютер заблоковано"
    SUBTITLE = "Очікуйте вказівок вчителя"

    def __init__(self, root, on_mode_change=None):
        self.root = root
        self.on_mode_change = on_mode_change
        self.active = False
        self.mode = "none"
        self.input_only = False
        self.input_grabbed = False
        self._last_mode = "none"
        self.windows = []
        self._geoms = []
        self._tick_id = None
        self._ticks = 0
        self._win = None          # Windows: (callback, hook_kb, hook_ms)
        self._x = None            # Linux/X11: (lib, display, root_window, event_buffer)
        self._evdev = {}          # шлях -> файловий дескриптор із захопленням
        self._evdev_denied = set()

    # ---------- публічне ----------
    def lock(self):
        """Повне блокування: перекриття екрана + блокування введення."""
        self.set_overlay(True)

    def unlock(self):
        """Повне зняття блокування (екран + введення)."""
        was_active = self.active or self.input_only
        self.active = False
        self.input_only = False
        self._stop_tick()
        self._release()
        self.input_grabbed = False
        self._destroy_windows()
        self.mode = "none"
        self._last_mode = "none"
        if was_active and self.on_mode_change:
            self.on_mode_change()

    def set_overlay(self, flag):
        """Перекриття екрана (разом із блокуванням введення)."""
        if flag:
            if self.active:
                return
            self.active = True
            self._build_windows()
            self.input_grabbed = self._grab() or self.input_grabbed
            self._set_mode()
            self._start_tick()
        else:
            if not self.active:
                return
            self.active = False
            self._destroy_windows()
            if not self.input_only:
                self._release()
                self.input_grabbed = False
            self._set_mode()
            if not (self.active or self.input_only):
                self._stop_tick()

    def set_input_lock(self, flag):
        """Блокування лише миші та клавіатури — без перекриття екрана."""
        flag = bool(flag)
        if flag == self.input_only:
            return
        self.input_only = flag
        if flag:
            self.input_grabbed = self._grab()
            self._start_tick()
        else:
            if not self.active:
                self._release()
                self.input_grabbed = False
            if not (self.active or self.input_only):
                self._stop_tick()
        self._set_mode()

    def _set_mode(self):
        if self.active:
            self.mode = "full" if self.input_grabbed else "visual"
        elif self.input_only:
            self.mode = "full" if self.input_grabbed else "unsupported"
        else:
            self.mode = "none"

    def _start_tick(self):
        if self._tick_id is None and (self.active or self.input_only):
            self._tick_id = self.root.after(500, self._tick)

    def _stop_tick(self):
        if self._tick_id is not None:
            try:
                self.root.after_cancel(self._tick_id)
            except tk.TclError:
                pass
            self._tick_id = None

    def _destroy_windows(self):
        for win in self.windows:
            try:
                win.destroy()
            except tk.TclError:
                pass
        self.windows = []
        self._geoms = []

    # ---------- вікна-перекриття ----------
    def _monitor_geometries(self):
        try:
            with new_mss() as sct:
                mons = sct.monitors[1:] or [sct.monitors[0]]
                return [(m["left"], m["top"], m["width"], m["height"]) for m in mons]
        except Exception:
            return [(0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())]

    def _build_windows(self):
        for win in self.windows:
            try:
                win.destroy()
            except tk.TclError:
                pass
        self.windows = []
        self._geoms = self._monitor_geometries()
        for left, top, width, height in self._geoms:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.configure(bg="#0b0f14")
            self._hide_cursor(win)
            win.geometry(f"{width}x{height}+{left}+{top}")
            win.attributes("-topmost", True)
            holder = tk.Frame(win, bg="#0b0f14")
            holder.place(relx=0.5, rely=0.5, anchor="center")
            tk.Label(holder, text="🔒", font=("", 72), fg="#e6edf3", bg="#0b0f14").pack()
            tk.Label(holder, text=self.TITLE, font=("", 48, "bold"),
                     fg="#e6edf3", bg="#0b0f14").pack(pady=(10, 6))
            tk.Label(holder, text=self.SUBTITLE, font=("", 22),
                     fg="#8b98a5", bg="#0b0f14").pack()
            win.lift()
            self.windows.append(win)
        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass

    @staticmethod
    def _hide_cursor(win):
        try:
            win.configure(cursor="none")
        except tk.TclError:
            pass

    # ---------- захоплення введення ----------
    def _grab(self):
        if platform.system() == "Windows":
            return self._grab_windows()
        return self._refresh_grab()

    def _refresh_grab(self):
        system = platform.system()
        if system == "Windows":
            return self._win is not None
        if system == "Linux":
            evdev_ok = self._scan_evdev() > 0
            x11_ok = False
            if not (os.environ.get("WAYLAND_DISPLAY") or
                    os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"):
                x11_ok = self._x11_grab()
            else:
                self._x11_drain()
            return evdev_ok or x11_ok
        return False   # macOS тощо: лише перекриття екрана

    def _release(self):
        system = platform.system()
        if system == "Windows":
            self._release_windows()
        elif system == "Linux":
            self._release_x11()
            self._release_evdev()

    def _tick(self):
        self._tick_id = None
        if not (self.active or self.input_only):
            return
        try:
            self._ticks += 1
            if self.active:
                if self._ticks % 4 == 0 and self._monitor_geometries() != self._geoms:
                    self._build_windows()
                for win in self.windows:
                    try:
                        win.lift()
                        win.attributes("-topmost", True)
                    except tk.TclError:
                        pass
            if self.active or self.input_only:
                self.input_grabbed = self._refresh_grab()
            self._set_mode()
            if self.mode != self._last_mode:
                self._last_mode = self.mode
                if self.on_mode_change:
                    self.on_mode_change()
        finally:
            if self.active or self.input_only:
                self._tick_id = self.root.after(500, self._tick)

    # ---------- Windows ----------
    def _grab_windows(self):
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            from ctypes import wintypes
            lresult = ctypes.c_ssize_t
            hookproc = ctypes.WINFUNCTYPE(lresult, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, hookproc, wintypes.HINSTANCE, wintypes.DWORD]
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            user32.CallNextHookEx.restype = lresult
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE

            def swallow(n_code, w_param, l_param):
                if n_code < 0:
                    return user32.CallNextHookEx(None, n_code, w_param, l_param)
                return 1          # подію поглинено — далі вона не потрапляє

            callback = hookproc(swallow)   # посилання зберігаємо, інакше його видалить GC
            module = kernel32.GetModuleHandleW(None)
            hook_kb = user32.SetWindowsHookExW(13, callback, module, 0)   # WH_KEYBOARD_LL
            hook_ms = user32.SetWindowsHookExW(14, callback, module, 0)   # WH_MOUSE_LL
            try:
                user32.BlockInput(True)        # потрібні права адміністратора; без них ігнорується
            except Exception:
                pass
            self._win = (callback, hook_kb, hook_ms)
            return bool(hook_kb) and bool(hook_ms)
        except Exception:
            self._win = None
            return False

    def _release_windows(self):
        if self._win is None:
            return
        _callback, hook_kb, hook_ms = self._win
        try:
            user32 = ctypes.windll.user32
            for hook in (hook_kb, hook_ms):
                if hook:
                    user32.UnhookWindowsHookEx(hook)
            user32.BlockInput(False)
        except Exception:
            pass
        self._win = None

    # ---------- Linux: X11 ----------
    def _x11_open(self):
        if self._x is not None:
            return self._x
        name = ctypes.util.find_library("X11")
        if not name or not os.environ.get("DISPLAY"):
            return None
        lib = ctypes.CDLL(name)
        c_void, c_ulong, c_int, c_uint = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_uint
        lib.XOpenDisplay.restype, lib.XOpenDisplay.argtypes = c_void, [ctypes.c_char_p]
        lib.XDefaultRootWindow.restype, lib.XDefaultRootWindow.argtypes = c_ulong, [c_void]
        lib.XGrabKeyboard.restype = c_int
        lib.XGrabKeyboard.argtypes = [c_void, c_ulong, c_int, c_int, c_int, c_ulong]
        lib.XGrabPointer.restype = c_int
        lib.XGrabPointer.argtypes = [c_void, c_ulong, c_int, c_uint, c_int, c_int, c_ulong, c_ulong, c_ulong]
        lib.XUngrabKeyboard.argtypes = [c_void, c_ulong]
        lib.XUngrabPointer.argtypes = [c_void, c_ulong]
        lib.XPending.restype, lib.XPending.argtypes = c_int, [c_void]
        lib.XNextEvent.argtypes = [c_void, c_void]
        lib.XFlush.argtypes = [c_void]
        lib.XCloseDisplay.argtypes = [c_void]
        display = lib.XOpenDisplay(None)
        if not display:
            return None
        self._x = (lib, display, lib.XDefaultRootWindow(display), ctypes.create_string_buffer(256))
        return self._x

    def _x11_grab(self):
        try:
            conn = self._x11_open()
            if conn is None:
                return False
            lib, display, root, _buf = conn
            # Повторне захоплення щоразу: якщо хтось інший (скрінсейвер, меню) перехопив — повертаємо
            kb = lib.XGrabKeyboard(display, root, 0, 1, 1, 0)       # GrabModeAsync = 1
            pt = lib.XGrabPointer(display, root, 0, 0, 1, 1, root, 0, 0)
            lib.XFlush(display)
            self._x11_drain()
            return kb == 0 and pt == 0                              # 0 = GrabSuccess
        except Exception:
            return False

    def _x11_drain(self):
        """Події натискань, які іде до нас через захоплення, викидаємо — інакше черга росте."""
        if self._x is None:
            return
        lib, display, _root, buf = self._x
        try:
            for _ in range(2000):
                if not lib.XPending(display):
                    break
                lib.XNextEvent(display, buf)
        except Exception:
            pass

    def _release_x11(self):
        if self._x is None:
            return
        lib, display, _root, _buf = self._x
        try:
            lib.XUngrabKeyboard(display, 0)
            lib.XUngrabPointer(display, 0)
            lib.XFlush(display)
            lib.XCloseDisplay(display)
        except Exception:
            pass
        self._x = None

    # ---------- Linux: evdev ----------
    @staticmethod
    def _input_event_paths():
        try:
            with open("/proc/bus/input/devices", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return []
        paths = []
        for block in text.split("\n\n"):
            for line in block.splitlines():
                if line.startswith("H: Handlers="):
                    handlers = line.split("=", 1)[1].split()
                    if any(h == "kbd" or h.startswith("mouse") for h in handlers):
                        paths += ["/dev/input/" + h for h in handlers if re.fullmatch(r"event\d+", h)]
        return paths

    def _scan_evdev(self):
        """Ексклюзивно захоплює клавіатури/миші (також нові, підключені під час блокування)."""
        try:
            import fcntl
        except ImportError:
            return 0
        for path in self._input_event_paths():
            if path in self._evdev or path in self._evdev_denied:
                continue
            fd = None
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                fcntl.ioctl(fd, self.EVIOCGRAB, 1)
                self._evdev[path] = fd
            except PermissionError:
                self._evdev_denied.add(path)
                if fd is not None:
                    os.close(fd)
            except OSError:
                if fd is not None:
                    os.close(fd)
        for path, fd in list(self._evdev.items()):       # вичитуємо події, щоб не накопичувались
            try:
                for _ in range(10):
                    os.read(fd, 4096)
            except BlockingIOError:
                pass
            except OSError:                              # пристрій відключено
                try:
                    os.close(fd)
                except OSError:
                    pass
                del self._evdev[path]
        return len(self._evdev)

    def _release_evdev(self):
        for fd in self._evdev.values():
            try:
                os.close(fd)           # закриття дескриптора знімає захоплення
            except OSError:
                pass
        self._evdev.clear()
        self._evdev_denied.clear()


class DemoViewer:
    """Вікно, у якому учень бачить екран вчителя. Працює в головному потоці Tk;
    інші потоки лише передають дані через push()/stop().
    Вікно звичайне (не блокує введення): його можна закрити хрестиком,
    F11 — повноекранний режим, Esc — вихід з нього."""

    def __init__(self, root, done_event, failsafe_minutes=0, power=None):
        self.root = root
        self.done = done_event
        self.failsafe_minutes = failsafe_minutes
        self.power = power
        self.power_win = None
        self.power_label = None
        self.locker = ScreenLocker(root, on_mode_change=self._bump_lock_state)
        self.lock_request = None          # True/False — запит від вчителя (з іншого потоку)
        self.lock_state = (False, "none")
        self.lock_version = 0
        self.input_request = None         # True/False — блокування лише введення
        self.input_state = (False, "none")
        self.input_version = 0
        self.connected = False
        self.disconnected_since = time.time()
        self.cur_msg_text = None
        self.cur_msg_auto = False
        self.close_msg_request = False
        self.lock = threading.Lock()
        self.latest = None
        self.stop_requested = False
        self.win = None
        self.label = None
        self.photo = None
        self.dismissed = False   # учень закрив вікно — не відкриваємо до наступної демонстрації
        self.fullscreen = False
        self.messages = []       # черга повідомлень (text, auto_close)
        self.msg_win = None
        self.root.after(150, self.poll)

    # --- викликається з інших потоків ---
    def push(self, jpeg):
        with self.lock:
            self.latest = jpeg

    def stop(self):
        with self.lock:
            self.latest = None
            self.stop_requested = True

    def message(self, text):
        with self.lock:
            if len(self.messages) < 20:
                self.messages.append((text, False))

    def media_message(self, text):
        """Повідомлення про відео/YouTube: закривається автоматично, щойно
        починається відтворення (див. request_close_message)."""
        with self.lock:
            if len(self.messages) < 20:
                self.messages.append((text, True))

    def request_close_message(self):
        """Просить закрити поточне (або наступне) авто-повідомлення про відео."""
        with self.lock:
            self.close_msg_request = True

    def request_lock(self, flag):
        with self.lock:
            self.lock_request = bool(flag)

    def request_input(self, flag):
        with self.lock:
            self.input_request = bool(flag)

    def lock_snapshot(self):
        with self.lock:
            return self.lock_version, self.lock_state[0], self.lock_state[1]

    def input_snapshot(self):
        with self.lock:
            return self.input_version, self.input_state[0], self.input_state[1]

    def set_connected(self, flag):
        with self.lock:
            self.connected = flag
            if not flag:
                self.disconnected_since = time.time()

    def _bump_lock_state(self):
        with self.lock:
            self.lock_state = (self.locker.active, self.locker.mode)
            self.lock_version += 1
            blocked = self.locker.input_only
            mode = (("full" if self.locker.input_grabbed else "unsupported")
                    if blocked else "none")
            self.input_state = (blocked, mode)
            self.input_version += 1

    # --- головний потік Tk: блокування ---
    def apply_lock(self):
        with self.lock:
            want, self.lock_request = self.lock_request, None
            want_input, self.input_request = self.input_request, None
            expired = (self.failsafe_minutes > 0
                       and (self.locker.active or self.locker.input_only)
                       and not self.connected
                       and time.time() - self.disconnected_since > self.failsafe_minutes * 60)
        if expired:
            print("Зв'язок із учителем відсутній занадто довго — блокування знято (аварійний режим).",
                  file=sys.stderr)
            want = want_input = False
        if want is None and want_input is None:
            return
        if want is not None:
            if want and not self.locker.active:
                self.close_window()  # демонстрація і повідомлення не повинні лишатися над екраном блокування
                self.dismissed = False
                if self.msg_win is not None and self.cur_msg_text is not None:
                    with self.lock:
                        self.messages.insert(0, (self.cur_msg_text, self.cur_msg_auto))
                self.close_message()
                try:
                    self.locker.set_overlay(True)
                except Exception as exc:
                    print(f"Не вдалося заблокувати: {exc}", file=sys.stderr)
                    self.locker.set_overlay(False)
                    with self.lock:
                        self.lock_state = (False, "error")
                        self.lock_version += 1
                    return
            elif not want and self.locker.active:
                self.locker.set_overlay(False)
        if want_input is not None:
            try:
                self.locker.set_input_lock(want_input)
            except Exception as exc:
                print(f"Не вдалося змінити блокування введення: {exc}", file=sys.stderr)
        self._bump_lock_state()

    # --- головний потік Tk ---
    def poll(self):
        if self.done.is_set():
            self.root.quit()
            return
        self.apply_lock()
        if self.locker.active:
            self.close_power_window()
            with self.lock:              # поки заблоковано, демонстрацію не показуємо
                self.latest = None
                self.stop_requested = False
            self.root.after(150, self.poll)
            return
        self.update_power_window()
        with self.lock:
            data, self.latest = self.latest, None
            stop, self.stop_requested = self.stop_requested, False
        if stop:
            self.close_window()
            self.dismissed = False
        if data and not self.dismissed:
            self.show(data)
        if self.msg_win is None:
            with self.lock:
                item = self.messages.pop(0) if self.messages else None
            if item is not None:
                self.show_message(item[0], item[1])
        if self.msg_win is not None and self.cur_msg_auto:
            with self.lock:
                want_close = self.close_msg_request
                if want_close:
                    self.close_msg_request = False
            if want_close:
                self.close_message()
        self.root.after(150, self.poll)

    def update_power_window(self):
        """Відлік до вимкнення: вікно не можна закрити, але воно й не блокує роботу."""
        remaining = self.power.remaining() if self.power else None
        if remaining is None:
            self.close_power_window()
            return
        text = (f"Комп'ютер буде вимкнено через {remaining // 60:02d}:{remaining % 60:02d}"
                if remaining > 0 else "Вимкнення комп'ютера…")
        if self.power_win is None or not self.power_win.winfo_exists():
            win = tk.Toplevel(self.root)
            win.title("Вимкнення комп'ютера")
            win.attributes("-topmost", True)
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            width = int(min(max(sw * 0.6, 600), sw - 40))
            self.power_label = tk.Label(win, text=text, font=("", 26, "bold"), fg="#b00020",
                                        wraplength=width - 80, padx=40, pady=24)
            self.power_label.pack(fill="both", expand=True)
            tk.Label(win, text="Збережіть свою роботу та закрийте програми.",
                     font=("", 18), padx=40).pack(pady=(0, 24))
            win.update_idletasks()
            height = min(max(int(sh * 0.22), win.winfo_reqheight()), sh - 80)
            win.geometry(f"{width}x{height}+{(sw - width) // 2}+{(sh - height) // 3}")
            win.protocol("WM_DELETE_WINDOW", lambda: None)
            win.lift()
            self.power_win = win
        else:
            self.power_label.configure(text=text)

    def close_power_window(self):
        if self.power_win is not None:
            try:
                self.power_win.destroy()
            except tk.TclError:
                pass
        self.power_win = None
        self.power_label = None

    def show_message(self, text, auto=False):
        """Широке вікно по центру екрана; збільшений шрифт; не блокує роботу."""
        self.cur_msg_text = text
        self.cur_msg_auto = auto
        win = tk.Toplevel(self.root)
        win.title("Повідомлення від вчителя")
        win.attributes("-topmost", True)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        width = int(min(max(sw * 0.6, 600), sw - 40))
        tk.Label(win, text=text, font=("", 18), wraplength=width - 80,
                 justify="center", padx=40, pady=30).pack(fill="both", expand=True)
        tk.Button(win, text="OK", font=("", 14), width=10,
                  command=self.close_message).pack(pady=(0, 20))
        win.update_idletasks()
        height = min(max(int(sh * 0.2), win.winfo_reqheight()), sh - 80)
        win.geometry(f"{width}x{height}+{(sw - width) // 2}+{(sh - height) // 2}")
        win.bind("<Return>", lambda _e: self.close_message())
        win.bind("<Escape>", lambda _e: self.close_message())
        win.protocol("WM_DELETE_WINDOW", self.close_message)
        win.lift()
        win.focus_force()
        self.msg_win = win

    def close_message(self):
        if self.msg_win is not None:
            try:
                self.msg_win.destroy()
            except tk.TclError:
                pass
        self.msg_win = None
        self.cur_msg_auto = False

    def open_window(self):
        win = tk.Toplevel(self.root)
        win.title("Екран вчителя")
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{int(sw * 0.9)}x{int(sh * 0.9)}+{int(sw * 0.05)}+{int(sh * 0.05)}")
        win.configure(bg="black")
        win.attributes("-topmost", True)
        self.label = tk.Label(win, bg="black")
        self.label.pack(fill="both", expand=True)
        win.protocol("WM_DELETE_WINDOW", self.on_user_close)
        win.bind("<F11>", lambda _e: self.set_fullscreen(not self.fullscreen))
        win.bind("<Escape>", lambda _e: self.set_fullscreen(False))
        win.update_idletasks()
        self.win = win

    def set_fullscreen(self, value):
        self.fullscreen = value
        if self.win:
            self.win.attributes("-fullscreen", value)

    def show(self, data):
        if ImageTk is None:      # Pillow без підтримки Tk — кадр не показати
            return
        try:
            img = Image.open(BytesIO(data))
            img.load()
        except Exception:
            return
        if self.win is None or not self.win.winfo_exists():
            self.open_window()
        w = max(200, self.win.winfo_width())
        h = max(200, self.win.winfo_height())
        scale = min(w / img.width, h / img.height)
        size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        if size != img.size:
            img = img.resize(size, Image.Resampling.BILINEAR)
        self.photo = ImageTk.PhotoImage(img)
        self.label.configure(image=self.photo)

    def on_user_close(self):
        self.dismissed = True
        self.close_window()

    def close_window(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
        self.win = None
        self.label = None
        self.fullscreen = False


_GUI_BROWSERS = ("xdg-open", "x-www-browser", "firefox-esr", "firefox",
                 "chromium", "chromium-browser", "google-chrome", "falkon")
_ENV_KEYS = ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
             "DBUS_SESSION_BUS_ADDRESS", "HOME", "WAYLAND_DISPLAY")
_OPEN_LOG = os.path.join(tempfile.gettempdir(), "classroom_open_url.log")


def _desktop_session():
    """Повертає (env, uid, gid) графічної сесії учня.
    Якщо агент не від root — uid/gid = None."""
    env = dict(os.environ)
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        env.setdefault("DISPLAY", ":0")
        return env, None, None
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            st = os.stat(f"/proc/{entry}")
            if st.st_uid == 0:
                continue
            with open(f"/proc/{entry}/environ", "rb") as fh:
                raw = fh.read().split(b"\0")
        except OSError:
            continue
        found = {}
        for item in raw:
            key, _, value = item.decode("utf-8", "replace").partition("=")
            if key in _ENV_KEYS:
                found[key] = value
        if found.get("DISPLAY") or found.get("WAYLAND_DISPLAY"):
            env.update(found)
            if "XDG_RUNTIME_DIR" not in found:
                env["XDG_RUNTIME_DIR"] = f"/run/user/{st.st_uid}"
            return env, st.st_uid, st.st_gid
    return env, None, None


def _clean_env(env):
    """Прибирає підміни середовища (PyInstaller тощо) і гарантує PATH."""
    return clean_env(env)


def open_url_in_browser(url):
    """Відкриває url у браузері. Повертає (ok, повідомлення).
    Деталі кожної спроби пишуться в журнал /tmp/classroom_open_url.log."""
    env, uid, gid = _desktop_session()
    env = _clean_env(env)
    candidates = [c for c in env.get("BROWSER", "").split(":") if c] + list(_GUI_BROWSERS)
    last_error = "не знайдено браузера"
    try:
        log = open(_OPEN_LOG, "a", encoding="utf-8")
    except OSError:
        log = open(os.devnull, "w")
    with log:
        log.write(f"\n=== {time.strftime('%H:%M:%S')} {url}\n"
                  f"uid={os.getuid() if hasattr(os, 'getuid') else '?'} "
                  f"DISPLAY={env.get('DISPLAY')} "
                  f"DBUS={env.get('DBUS_SESSION_BUS_ADDRESS')} "
                  f"frozen={getattr(sys, 'frozen', False)}\n")
        log.flush()
        for name in candidates:
            exe = shutil.which(name.split()[0], path=env["PATH"])
            if not exe:
                log.write(f"{name}: не знайдено у PATH\n")
                continue
            kwargs = {}
            if uid is not None:
                kwargs.update(user=uid, group=gid, cwd=env.get("HOME") or "/")
            try:
                proc = subprocess.Popen([exe, url], env=env, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=log,
                                        start_new_session=True, **kwargs)
                try:
                    code = proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    log.write(f"{name} ({exe}): працює (OK)\n")
                    return True, name
                log.write(f"{name} ({exe}): завершився з кодом {code}\n")
                if code == 0:
                    return True, name
                last_error = f"{name}: код {code}"
            except (OSError, ValueError) as exc:
                log.write(f"{name}: помилка запуску: {exc}\n")
                last_error = f"{name}: {exc}"
    return False, last_error


class MediaController:
    """Керує відтворенням відео на комп'ютері учня.

    Відеофайл від учителя зберігається в тимчасовій теці, відтворюється
    штатним відеоплеєром операційної системи і видаляється після завершення
    перегляду (або команди зупинки). Посилання YouTube відкривається у
    браузері за замовчуванням."""

    # Типові відеоплеєри Linux (перший-ліпший встановлений). Їх запускаємо
    # безпосередньо, щоб дочекатися завершення перегляду перед видаленням.
    _LINUX_PLAYERS = ("mpv", "vlc", "cvlc", "mplayer", "ffplay", "totem",
                      "smplayer", "parole", "xplayer", "dragon")

    def __init__(self, notify, send=None, video_dir=None, notify_auto=None,
                 dismiss=None):
        self.notify = notify
        self.notify_auto = notify_auto or notify
        self.dismiss = dismiss
        self.send = send
        self.video_dir = video_dir or os.path.join(tempfile.gettempdir(),
                                                   "classroom_video")
        self.lock = threading.Lock()
        self.proc = None
        self.playing_path = None
        self.playing_id = None

    def _dismiss(self):
        """Закриває сповіщення про відео, щойно почалося відтворення."""
        if self.dismiss is None:
            return
        try:
            self.dismiss()
        except Exception:
            pass

    # --- відтворення відеофайлу ---
    def play_video(self, path, fid=None):
        """Запускає відтворення у фоновому потоці, щоб не блокувати мережу."""
        self.stop()
        with self.lock:
            self.playing_path = path
            self.playing_id = fid
        self._report(fid, "playing", True)
        try:
            self.notify_auto(f"Відтворюється відео:\n{os.path.basename(path)}")
        except Exception:
            pass
        threading.Thread(target=self._play_worker, args=(path, fid),
                         daemon=True).start()

    def _play_worker(self, path, fid):
        try:
            proc = self._launch(path)
            if proc is not None:
                with self.lock:
                    self.proc = proc
                self._dismiss()
                proc.wait()
            else:
                self._launch_default(path)
        except Exception as exc:
            self._report(fid, "error", False, str(exc))
        finally:
            with self.lock:
                current = self.playing_path == path
                if current:
                    self.proc = None
                    self.playing_path = None
                    self.playing_id = None
            if current:
                self._remove(path)
                self._report(fid, "done", True)

    def _launch(self, path):
        """Запускає відтворення, якщо вдалося підібрати плеєр. Повертає Popen
        (щоб дочекатися завершення) або None — тоді запускаємо штатний обробник."""
        if sys.platform == "win32":
            try:
                return subprocess.Popen(
                    ["cmd", "/c", "start", "/wait", "", path],
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            except OSError:
                return None
        env = clean_env()
        for name in self._LINUX_PLAYERS:
            exe = shutil.which(name, path=env["PATH"])
            if exe:
                for extra in (["--fullscreen"], []):
                    try:
                        return subprocess.Popen(
                            [exe, *extra, path], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except OSError:
                        continue
        return None

    def _launch_default(self, path):
        """Відкриває файл штатним відеоплеєром ОС (через обробник за замовчуванням)."""
        if sys.platform == "win32":
            try:
                os.startfile(path)  # noqa: S606 (специфічно для Windows)
            except OSError:
                pass
            self._dismiss()
            self._wait_until_idle(path)
            return
        env = clean_env()
        opener = shutil.which("xdg-open", path=env["PATH"])
        if not opener:
            try:
                self.notify("У системі не знайдено відеоплеєра для відтворення.")
            except Exception:
                pass
            return
        try:
            subprocess.Popen([opener, path], env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            return
        self._dismiss()
        self._wait_until_idle(path)

    def _wait_until_idle(self, path, timeout=6 * 3600, poll=2.0):
        """Чекає, доки жоден процес не триматиме шлях до файлу відкритим,
        щоб видалити його після завершення перегляду (лише Linux)."""
        if sys.platform == "win32":
            time.sleep(0.5)
            return
        deadline = time.time() + timeout
        seen = False
        absent = 0
        while time.time() < deadline:
            with self.lock:
                if self.playing_path != path:
                    return            # зупинено командою вчителя
            if _path_in_use(path):
                seen = True
                absent = 0
            elif seen:
                absent += 1
                if absent >= 2:       # плеєр закрив файл — перегляд завершено
                    return
            time.sleep(poll)

    def stop(self):
        """Зупиняє поточне відтворення та видаляє файл."""
        with self.lock:
            proc = self.proc
            path = self.playing_path
            fid = self.playing_id
            self.proc = None
            self.playing_path = None
            self.playing_id = None
        if proc is not None:
            self._terminate(proc)
        if path is not None:
            self._remove(path)
            self._report(fid, "done", True)

    @staticmethod
    def _terminate(proc):
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError:
            pass

    def _remove(self, path):
        for attempt in range(5):
            try:
                os.remove(path)
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.3)

    # --- YouTube ---
    def open_youtube(self, url):
        if not is_youtube_url(url):
            try:
                self.notify("Посилання YouTube відхилено (невірний домен).")
            except Exception:
                pass
            return False
        opened, info = False, ""
        if sys.platform == "win32":
            try:
                opened = bool(webbrowser.open(url, new=2))
                if not opened:
                    os.startfile(url)  # noqa: S606
                    opened = True
            except Exception as exc:
                info = str(exc)
        else:
            try:
                opened, info = open_url_in_browser(url)
            except Exception as exc:
                opened, info = False, f"виняток: {exc}"
        try:
            if opened:
                self.notify_auto("Відкриваю відео на YouTube у браузері.")
            else:
                self.notify("Не вдалося відкрити браузер для перегляду "
                            f"YouTube.\n({info})")
        except Exception:
            pass
        if opened:
            self._dismiss()
        return opened

    def _report(self, fid, phase, ok, error=""):
        if self.send is None or fid is None:
            return
        try:
            self.send(MSG_VIDEO_RESULT + json.dumps(
                {"id": fid, "phase": phase, "ok": ok,
                 "error": error}).encode("utf-8"))
        except OSError:
            pass


def _path_in_use(path):
    """Чи тримає якийсь процес (Linux) шлях до файлу у своєму cmdline."""
    target = os.fsencode(path)
    try:
        pids = os.listdir("/proc")
    except OSError:
        return False
    for entry in pids:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                if target in handle.read():
                    return True
        except OSError:
            continue
    return False


class FileReceiver:
    """Приймає файли від вчителя. Перевірки на боці учня:
    ім'я очищається від шляхів (немає виходу за межі папки), розмір і тип обмежені,
    дані пишуться у тимчасовий файл і з'являються під справжнім іменем лише після
    перевірки розміру та SHA-256. Існуючі файли не перезаписуються. Звичайні файли
    не відкриваються; файли з полем "mode": "video" зберігаються у тимчасовій теці,
    відтворюються штатним плеєром і видаляються після перегляду."""

    def __init__(self, save_dir, max_bytes, allow_exec, send, notify,
                 media=None, video_dir=None):
        self.save_dir = save_dir
        self.max_bytes = max_bytes
        self.allow_exec = allow_exec
        self.send = send
        self.notify = notify
        self.media = media
        self.video_dir = video_dir
        self.cur = None

    def _result(self, fid, ok, error=""):
        try:
            self.send(MSG_FILE_RESULT + json.dumps(
                {"id": fid, "ok": ok, "error": error}).encode("utf-8"))
        except OSError:
            pass

    def _discard(self):
        if self.cur:
            try:
                self.cur["fh"].close()
            except OSError:
                pass
            try:
                os.remove(self.cur["tmp"])
            except OSError:
                pass
            self.cur = None

    def _fail(self, reason):
        fid = self.cur["id"] if self.cur else None
        self._discard()
        if fid is not None:
            self._result(fid, False, reason)
            try:
                self.notify(reason)
            except Exception:
                pass

    def cancel(self, reason="Передавання скасовано.", video_only=False):
        """Примусове зупинення приймання (напр., учитель зупинив відео).
        Файл тихо видаляється, учитель отримує результат, учень — сповіщення."""
        if self.cur is None:
            return
        if video_only and not self.cur.get("is_video"):
            return
        self._fail(reason)

    abort = _discard      # обрив зв'язку: тихо прибираємо незавершений файл

    def start(self, data):
        self._discard()
        try:
            meta = json.loads(data.decode("utf-8"))
            fid = int(meta["id"])
            name = safe_filename(meta["name"])
            size = int(meta["size"])
            sha = str(meta["sha256"]).lower()
            mode = str(meta.get("mode", "file"))
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        is_video = mode == "video"
        if size < 0 or size > self.max_bytes:
            return self._result(fid, False, f"Файл завеликий (ліміт {self.max_bytes // (1024 * 1024)} МБ)")
        if is_video:
            if not is_video_file(name):
                return self._result(fid, False, "Дозволено лише відеофайли")
            dest_dir = self.video_dir or self.save_dir
        else:
            if is_risky_file(name) and not self.allow_exec:
                return self._result(fid, False, "Тип файлу заблоковано на комп'ютері учня")
            dest_dir = self.save_dir
        try:
            os.makedirs(dest_dir, exist_ok=True)
            if shutil.disk_usage(dest_dir).free < size + 50 * 1024 * 1024:
                return self._result(fid, False, "Недостатньо місця на диску")
            fh = tempfile.NamedTemporaryFile(dir=dest_dir, prefix="incoming-",
                                             suffix=".part", delete=False)
        except OSError as exc:
            return self._result(fid, False, f"Не вдалося створити файл: {exc.strerror or exc}")
        self.cur = {"id": fid, "name": name, "size": size, "sha": sha, "fh": fh,
                    "tmp": fh.name, "received": 0, "hash": hashlib.sha256(),
                    "mode": mode, "is_video": is_video, "dest": dest_dir}

    def data(self, payload):
        if len(payload) < 4 or self.cur is None:
            return
        if struct.unpack("!I", payload[:4])[0] != self.cur["id"]:
            return
        chunk = payload[4:]
        self.cur["received"] += len(chunk)
        if self.cur["received"] > self.cur["size"]:
            return self._fail("Отримано більше даних, ніж заявлено")
        try:
            self.cur["fh"].write(chunk)
        except OSError as exc:
            return self._fail(f"Помилка запису: {exc.strerror or exc}")
        self.cur["hash"].update(chunk)

    def end(self, payload):
        if len(payload) < 5 or self.cur is None:
            return
        fid, status = struct.unpack("!IB", payload[:5])
        if fid != self.cur["id"]:
            return
        if status != 1:
            return self._fail("Передавання скасовано вчителем.")
        cur = self.cur
        try:
            cur["fh"].close()
        except OSError as exc:
            return self._fail(f"Помилка запису: {exc.strerror or exc}")
        if cur["received"] != cur["size"] or cur["hash"].hexdigest() != cur["sha"]:
            return self._fail("Файл отримано пошкодженим (не збігається контрольна сума)")
        stem, ext = os.path.splitext(cur["name"])
        final = os.path.join(cur["dest"], cur["name"])
        n = 1
        while os.path.exists(final):
            final = os.path.join(cur["dest"], f"{stem} ({n}){ext}")
            n += 1
        try:
            os.replace(cur["tmp"], final)
        except OSError as exc:
            return self._fail(f"Не вдалося зберегти файл: {exc.strerror or exc}")
        self.cur = None
        self._result(fid, True)
        if cur["is_video"] and self.media is not None:
            self.media.play_video(final, fid)
        else:
            self.notify(f"Вчитель надіслав файл:\n{os.path.basename(final)} ({format_size(cur['size'])})"
                        f"\n\nЗбережено в папці:\n{self.save_dir}")


def receiver(sock, viewer, dead, stop_event, files, send, power, media=None):
    """Читає пакети від вчителя (кадри демонстрації / сигнал завершення)."""
    try:
        while not dead.is_set() and not stop_event.is_set():
            ready, _, _ = select.select([sock], [], [], 1.0)
            if not ready:
                continue
            packet = recv_packet(sock)
            kind, data = packet[:1], packet[1:]
            if kind == MSG_TEXT:
                text = data.decode("utf-8", errors="replace")[:MAX_MESSAGE_CHARS]
                if viewer is not None:
                    viewer.message(text)
                else:
                    print(f"\n=== Повідомлення від вчителя ===\n{text}\n", flush=True)
                continue
            if kind == MSG_POWER:
                try:
                    command = json.loads(data.decode("utf-8"))
                    action = str(command.get("action"))
                    delay = int(command.get("delay", 0))
                except (ValueError, TypeError, AttributeError):
                    continue
                if action in ("shutdown", "cancel"):
                    power.request(action, delay)
                continue
            if kind == MSG_LOCK:
                want = data[:1] == b"\x01"
                if viewer is not None:
                    viewer.request_lock(want)
                elif want:    # без графічного вікна заблокувати не можемо — чесно повідомляємо вчителя
                    send(MSG_LOCK_STATE + json.dumps(
                        {"locked": False, "mode": "unsupported", "initial": False}).encode())
                continue
            if kind == MSG_INPUT:
                want = data[:1] == b"\x01"
                if viewer is not None:
                    viewer.request_input(want)
                else:
                    send(MSG_INPUT_STATE + json.dumps(
                        {"blocked": False, "mode": "unsupported", "initial": False}).encode())
                continue
            if kind in (MSG_FILE_START, MSG_FILE_DATA, MSG_FILE_END):
                if kind == MSG_FILE_START:
                    files.start(data)
                elif kind == MSG_FILE_DATA:
                    files.data(data)
                else:
                    files.end(data)
                continue
            if kind == MSG_VIDEO_STOP:
                if media is not None:
                    media.stop()
                files.cancel("Передавання відео скасовано вчителем.",
                             video_only=True)
                continue
            if kind == MSG_YOUTUBE:
                if media is not None:
                    try:
                        url = str(json.loads(data.decode("utf-8")).get("url", ""))
                    except (ValueError, TypeError, AttributeError):
                        continue
                    media.open_youtube(url)
                continue
            if viewer is None:
                continue
            if kind == MSG_DEMO:
                viewer.push(data)
            elif kind == MSG_STOP:
                viewer.stop()
    except Exception as exc:
        print(f"Помилка приймання даних від учителя: {exc!r}", file=sys.stderr)
    finally:
        dead.set()
        files.abort()        # незавершений файл видаляється
        if viewer is not None:
            viewer.stop()    # зв'язок втрачено — закриваємо вікно демонстрації


def stream(ip, port, token, name, interval, viewer, stop_event, file_cfg, power):
    """Одна сесія. Повертає False, якщо автентифікація не пройшла."""
    with socket.create_connection((ip, port), timeout=10) as sock:
        sock.settimeout(15)
        if not client_handshake(sock, token, name):
            return False
        print(f"Підключено до {ip}:{port}. Екран ПЕРЕДАЄТЬСЯ вчителю.")
        dead = threading.Event()
        send_lock = threading.Lock()     # відправляють два потоки: кадри та результати файлів

        def send(packet):
            with send_lock:
                send_packet(sock, packet)

        def notify(text):
            if viewer is not None:
                viewer.message(text)
            else:
                print(f"\n=== {text} ===\n", flush=True)

        def notify_auto(text):
            if viewer is not None:
                viewer.media_message(text)
            else:
                print(f"\n=== {text} ===\n", flush=True)

        video_dir = file_cfg.get("video_dir") or os.path.join(
            tempfile.gettempdir(), "classroom_video")
        media = MediaController(
            notify, send, video_dir, notify_auto=notify_auto,
            dismiss=viewer.request_close_message if viewer is not None else None)
        files = FileReceiver(file_cfg["save_dir"], file_cfg["max_bytes"],
                             file_cfg["allow_exec"], send, notify,
                             media=media, video_dir=video_dir)
        threading.Thread(target=receiver, args=(sock, viewer, dead, stop_event,
                                                files, send, power, media),
                         daemon=True).start()

        def report_lock(initial=False):
            version, locked, mode = viewer.lock_snapshot() if viewer else (0, False, "unsupported")
            send(MSG_LOCK_STATE + json.dumps(
                {"locked": locked, "mode": mode, "initial": initial}).encode("utf-8"))
            return version

        def report_input(initial=False):
            version, blocked, mode = viewer.input_snapshot() if viewer else (0, False, "unsupported")
            send(MSG_INPUT_STATE + json.dumps(
                {"blocked": blocked, "mode": mode, "initial": initial}).encode("utf-8"))
            return version

        if viewer is not None:
            viewer.set_connected(True)
        last_version = report_lock(initial=True)
        last_input = report_input(initial=True)

        def report_power():
            version, state, remaining, error = power.snapshot()
            send(MSG_POWER_STATE + json.dumps(
                {"state": state, "remaining": remaining, "error": error}).encode("utf-8"))
            return version

        last_power = report_power()
        try:
            with new_mss() as sct:
                while not stop_event.is_set() and not dead.is_set():
                    frame = capture_jpeg(sct, max_width=960, quality=55, monitor=0)
                    if len(frame) > MAX_FRAME_BYTES:
                        raise ValueError("Captured frame exceeds size limit")
                    send(MSG_FRAME + frame)
                    if viewer is not None and viewer.lock_snapshot()[0] != last_version:
                        last_version = report_lock()
                    if viewer is not None and viewer.input_snapshot()[0] != last_input:
                        last_input = report_input()
                    if power.snapshot()[0] != last_power:
                        last_power = report_power()
                    stop_event.wait(interval)
        finally:
            dead.set()
            if viewer is not None:
                viewer.set_connected(False)
        if not stop_event.is_set():
            raise ConnectionError("Teacher connection lost")
        return True


def _ensure_discovery_firewall(port, on_elevate=None):
    """Відкриває вхідний UDP-порт виявлення у фаєрволі, щоб отримувати
    оголошення та відповіді вчителя. Помилки не критичні для роботи."""
    try:
        result = ensure_firewall_port(port, proto="udp", on_elevate=on_elevate)
    except Exception as exc:
        print(f"Не вдалося перевірити фаєрвол (UDP {port}): {exc}", file=sys.stderr)
        return
    status = result.get("status")
    if status == "added":
        print(f"Фаєрвол: {result.get('message', '')}")
    elif status == "failed":
        print(f"Увага: {result.get('message', '')}", file=sys.stderr)
        if result.get("command"):
            print(f"  Виконайте вручну: {result['command']}", file=sys.stderr)


def _ensure_time_sync(on_elevate=None):
    """Вмикає автоматичну синхронізацію часу (NTP). Розбіжність годинників
    між комп'ютерами ускладнює автовизначення вчителя. Помилки не критичні."""
    try:
        result = ensure_time_sync(on_elevate=on_elevate)
    except Exception as exc:
        print(f"Не вдалося перевірити синхронізацію часу: {exc}", file=sys.stderr)
        return
    status = result.get("status")
    if status == "added":
        print(f"Синхронізація часу: {result.get('message', '')}")
    elif status == "failed":
        print(f"Увага: {result.get('message', '')}", file=sys.stderr)
        if result.get("command"):
            print(f"  Виконайте вручну: {result['command']}", file=sys.stderr)


def run(args, token, viewer, stop_event, file_cfg, power):
    """Основний цикл: пошук вчителя → сесія → при обриві пошук знову."""
    discovery_fw_checked = False
    time_sync_checked = False

    def elevate_note(text):
        """Пояснює, навіщо потрібні права адміністратора (перед запитом пароля)."""
        print(f"\n=== Потрібні права адміністратора ===\n{text}\n",
              file=sys.stderr, flush=True)
        if viewer is not None:
            viewer.message(text)

    while not stop_event.is_set():
        try:
            if not time_sync_checked:
                time_sync_checked = True
                _ensure_time_sync(elevate_note)
            if args.teacher_ip:
                ip, port = args.teacher_ip, args.port
            else:
                if not discovery_fw_checked:
                    discovery_fw_checked = True
                    _ensure_discovery_firewall(args.discovery_port, elevate_note)
                print(f"Пошук комп'ютера вчителя (UDP {args.discovery_port})…")
                found = discover_teacher(token, args.discovery_port, stop_event, args.port)
                if found is None:
                    break
                ip, port, tname = found
                print(f"Знайдено вчителя «{tname}» за адресою {ip}")
            if not stream(ip, port, token, args.name, args.interval, viewer, stop_event,
                      file_cfg, power):
                print("Автентифікація не пройдена (перевірте токен).", file=sys.stderr)
                if args.teacher_ip:
                    return 3
                stop_event.wait(3)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"Connection interrupted: {exc}. Retrying in 3 seconds…", file=sys.stderr)
            stop_event.wait(3)
    return 0


SETUP_SCRIPT = program_path("skkk_student_setup")


def _has_display():
    """Чи є графічна сесія (потрібна для вікна меню налаштування)."""
    if sys.platform == "win32":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _student_setup_needed(args):
    """Чи потрібно перед стартом запустити меню налаштування учня."""
    if args.token or args.token_file:
        return False
    token = load_token()
    if token is None or len(token) < MIN_TOKEN_LEN:
        return True
    return not student_setup_complete()


def _run_student_setup():
    """Запускає меню налаштування учня (skkk_student_setup) і чекає на
    завершення. Для зібраного застосунку — сусідній виконуваний файл, для
    вихідного коду — skkk_student_setup.py."""
    if not SETUP_SCRIPT.exists():
        print(f"Не знайдено меню налаштування: {SETUP_SCRIPT}", file=sys.stderr)
        return
    print("Середовище ще не налаштоване — запускаю меню налаштування…")
    try:
        subprocess.run(program_command("skkk_student_setup", "--no-launch"),
                       cwd=str(app_dir()), check=False, env=clean_env())
    except OSError as exc:
        print(f"Не вдалося запустити меню налаштування: {exc}", file=sys.stderr)


def _set_window_icon(root):
    """Встановлює іконку вікна з icon.png (якщо файл доступний)."""
    if tk is None:
        return
    path = app_icon_path()
    if path is None:
        return
    try:
        photo = tk.PhotoImage(file=str(path))
        root.iconphoto(True, photo)
        root._app_icon = photo  # тримаємо посилання, щоб не зібрало GC
    except Exception:                             # noqa: BLE001
        pass


def main():
    cfg = load_config()
    parser = argparse.ArgumentParser(
        description="Visible screen-sharing agent for an authorized classroom network."
    )
    parser.add_argument("teacher_ip", nargs="?",
                        help="(необов'язково) IP вчителя; без нього — автоматичне виявлення")
    parser.add_argument("--port", type=int, default=cfg["tcp_port"], help="TCP-порт (резервний)")
    parser.add_argument("--discovery-port", type=int, default=cfg["discovery_port"],
                        help="UDP-порт виявлення")
    parser.add_argument("--token", help="спільний токен (необов'язково; інакше — з файлу)")
    parser.add_argument("--token-file", help="шлях до файлу з токеном "
                        "(за замовчуванням — classroom_token.txt поряд з агентом)")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between screen updates (default: 1)")
    parser.add_argument("--name", default=platform.node() or getpass.getuser(),
                        help="display name shown to the teacher")
    parser.add_argument("--save-dir", help="куди зберігати файли від вчителя "
                        "(за замовчуванням — «Файли від вчителя» у Завантаженнях)")
    parser.add_argument("--no-viewer", action="store_true",
                        help="не відкривати вікна (демонстрація екрана та повідомлення вчителя); повідомлення виводяться в термінал")
    args = parser.parse_args()

    if not args.no_viewer and _has_display() and _student_setup_needed(args):
        _run_student_setup()

    token = args.token or load_token(args.token_file)
    if not token:
        print("Токен доступу не знайдено.\n"
              "Покладіть файл classroom_token.txt поряд з агентом учня. Очікується шлях:\n"
              f"  {token_file_path(args.token_file)}\n"
              "або передайте токен аргументом --token.", file=sys.stderr)
        return 2
    if len(token) < MIN_TOKEN_LEN:
        print("Token must be at least 12 characters.", file=sys.stderr)
        return 2
    if args.interval < 0.5:
        print("For network stability, --interval must be at least 0.5 seconds.", file=sys.stderr)
        return 2

    file_cfg = {
        "save_dir": os.path.abspath(args.save_dir or cfg["save_dir"] or default_save_dir()),
        "max_bytes": cfg["max_file_mb"] * 1024 * 1024,
        "allow_exec": cfg["allow_executable_files"],
    }
    print("Screen sharing is ACTIVE (classroom monitoring). Press Ctrl+C to stop.")
    print(f"Файли від вчителя зберігаються в: {file_cfg['save_dir']}")
    stop_event = threading.Event()
    power = PowerManager(cfg["shutdown_min_delay_seconds"])

    root = None
    if tk is not None and not args.no_viewer:
        if sys.platform == "win32":
            try:       # координати Tk мають збігатися з фізичними пікселями моніторів
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
        try:
            root = tk.Tk()
            root.withdraw()
            _set_window_icon(root)
        except tk.TclError as exc:
            print(f"Вікно демонстрації недоступне ({exc}); працюю без нього.", file=sys.stderr)
            root = None
    elif not args.no_viewer:
        print("Модуль tkinter недоступний — блокування комп'ютера та показ демонстрації "
              "екрана працювати не будуть.\n"
              "У Debian/antiX встановіть його командою:  sudo apt install python3-tk",
              file=sys.stderr)

    if tk is not None and ImageTk is None and not args.no_viewer:
        print("Pillow без підтримки Tk (немає PIL.ImageTk) — показ демонстрації екрана "
              "вчителя буде недоступним.\n"
              "Встановіть:  sudo apt install python3-pil.imagetk  (або перевстановіть Pillow)",
              file=sys.stderr)

    if root is None:
        try:
            return run(args, token, None, stop_event, file_cfg, power)
        except KeyboardInterrupt:
            print("\nScreen sharing stopped.")
            return 0

    # Tk має працювати в головному потоці, мережевий цикл — у робочому
    done = threading.Event()
    viewer = DemoViewer(root, done, cfg["lock_failsafe_minutes"], power)
    result = [0]

    def worker():
        try:
            result[0] = run(args, token, viewer, stop_event, file_cfg, power)
        finally:
            done.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        root.mainloop()
    except KeyboardInterrupt:
        print("\nScreen sharing stopped.")
    finally:
        stop_event.set()
        thread.join(timeout=5)
        try:
            viewer.locker.unlock()     # знімає хуки та захоплення введення при виході агента
        except Exception:
            pass
    return result[0]


if __name__ == "__main__":
    raise SystemExit(main())

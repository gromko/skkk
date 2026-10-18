#!/usr/bin/env python3
"""Visible classroom screen viewer (teacher side). Python 3.10+.
"""
import hashlib
import json
import math
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from io import BytesIO
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from common import (FILE_CHUNK, MAX_FILES_PER_SEND, MAX_MESSAGE_CHARS, MSG_DEMO,
                    MSG_FILE_DATA, MSG_FILE_END, MSG_FILE_RESULT, MSG_FILE_START,
                    MSG_FRAME, MSG_INPUT, MSG_INPUT_STATE, MSG_LOCK, MSG_LOCK_STATE,
                    MSG_NET, MSG_NET_STATE,
                    MSG_POWER, MSG_POWER_STATE,
                    MSG_STOP, MSG_TEXT,
                    MSG_VIDEO_RESULT, MSG_VIDEO_STOP, MSG_YOUTUBE,
                    app_dir,
                    app_icon_path,
                    build_announcement,
                    bundle_dir,
                    capture_jpeg,
                    format_size,
                    is_risky_file, is_video_file, is_youtube_url,
                    load_config, load_token, local_broadcast_targets,
                    new_mss,
                    parse_query, program_command, program_path,
                    recv_packet,
                    ROLE_NET,
                    send_packet, server_handshake, setup_complete, token_file_path,
                    MIN_TOKEN_LEN)

CFG = load_config()
HOST = "0.0.0.0"
PORT = CFG["tcp_port"]
DISCOVERY_PORT = CFG["discovery_port"]
ANNOUNCE_INTERVAL = CFG["announce_interval"]
BROADCAST_ADDR = CFG["broadcast_address"]
DEMO_INTERVAL = CFG["demo_interval"]
DEMO_MAX_WIDTH = CFG["demo_max_width"]
DEMO_QUALITY = CFG["demo_quality"]
MAX_FILE_MB = CFG["max_file_mb"]
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_MESSAGE_LOG = 500           # максимум записів у журналі повідомлень
ALLOW_EXEC = CFG["allow_executable_files"]
SHUTDOWN_DELAY = CFG["shutdown_delay_seconds"]
SHUTDOWN_MIN = CFG["shutdown_min_delay_seconds"]

THUMB_SIZE = (220, 140)
COLUMNS = 5
ROWS = 3
CARD_W = 236
CARD_H = 226
REFRESH_MS = 500
IMG_DIR = os.path.join(str(bundle_dir()), "img")
_ICON_DEBUG = bool(os.environ.get("SKKK_DEBUG_ICONS"))

# ---------------------------------------------------------------------------
# Кольорова палітра теми
# ---------------------------------------------------------------------------
PALETTE = {
    "bg":          "#f4f6fa",
    "surface":     "#ffffff",
    "surface_alt": "#eef2f8",
    "border":      "#d8dee8",
    "text":        "#1f2937",
    "muted":       "#6b7280",
    "accent":      "#2563eb",
    "accent_fg":   "#ffffff",
    "danger":      "#dc2626",
    "danger_fg":   "#ffffff",
    "warning":     "#d97706",
    "success":     "#059669",
    "net":         "#0891b2",
    "demo":        "#16a34a",
    "chat":        "#7c3aed",
    "file":        "#ea580c",
}


def _configure_theme():
    """Створює власну тему Classroom для ttk. Безпечна для повторного виклику."""
    style = ttk.Style()
    # Якщо тема вже зареєстрована — просто активуємо її (випадок повторного виклику)
    if "Classroom" not in style.theme_names():
        available = style.theme_names()
        base = "clam" if "clam" in available else style.theme_use()
        style.theme_create("Classroom", parent=base, settings={
            ".": {
                "configure": {
                    "background": PALETTE["bg"],
                    "foreground": PALETTE["text"],
                    "font": ("Segoe UI", 10),
                    "borderwidth": 0,
                }
            },
            "TFrame":        {"configure": {"background": PALETTE["bg"]}},
            "TLabel":        {"configure": {"background": PALETTE["bg"],
                                            "foreground": PALETTE["text"]}},
            "TLabelframe":   {"configure": {"background": PALETTE["surface"],
                                            "relief": "solid", "borderwidth": 1,
                                            "bordercolor": PALETTE["border"],
                                            "lightcolor": PALETTE["border"],
                                            "darkcolor": PALETTE["border"]}},
            "TLabelframe.Label": {"configure": {"background": PALETTE["surface"],
                                                "foreground": PALETTE["accent"],
                                                "font": ("Segoe UI", 10, "bold")}},
            "TButton": {
                "configure": {
                    "background": PALETTE["surface"],
                    "foreground": PALETTE["text"],
                    "borderwidth": 1,
                    "relief": "solid",
                    "padding": (10, 6),
                    "font": ("Segoe UI", 10),
                },
                "map": {
                    "background": [("active", PALETTE["surface_alt"]),
                                   ("disabled", PALETTE["border"])],
                    "foreground": [("disabled", PALETTE["muted"])],
                },
            },
            "Accent.TButton": {
                "configure": {
                    "background": PALETTE["accent"],
                    "foreground": PALETTE["accent_fg"],
                    "borderwidth": 0,
                    "padding": (12, 7),
                    "font": ("Segoe UI", 10, "bold"),
                },
                "map": {"background": [("active", "#1d4ed8"),
                                       ("disabled", PALETTE["border"])]},
            },
            "Danger.TButton": {
                "configure": {
                    "background": PALETTE["danger"],
                    "foreground": PALETTE["danger_fg"],
                    "borderwidth": 0,
                    "padding": (12, 7),
                    "font": ("Segoe UI", 10, "bold"),
                },
                "map": {"background": [("active", "#b91c1c"),
                                       ("disabled", PALETTE["border"])]},
            },
            "Success.TButton": {
                "configure": {
                    "background": PALETTE["success"],
                    "foreground": "#ffffff",
                    "borderwidth": 0,
                    "padding": (12, 7),
                    "font": ("Segoe UI", 10, "bold"),
                },
                "map": {"background": [("active", "#047857")]},
            },
            "Toolbutton.TButton": {
                "configure": {"padding": (8, 5), "font": ("Segoe UI", 10)},
            },
            "TMenubutton": {
                "configure": {
                    "background": PALETTE["surface"],
                    "foreground": PALETTE["text"],
                    "borderwidth": 1,
                    "relief": "solid",
                    "padding": (10, 6),
                    "font": ("Segoe UI", 10, "bold"),
                    "arrowcolor": PALETTE["text"],
                },
                "map": {"background": [("active", PALETTE["surface_alt"])]},
            },
            "TCheckbutton": {
                "configure": {
                    "background": PALETTE["surface"],
                    "foreground": PALETTE["text"],
                    "font": ("Segoe UI", 10),
                    "indicatorcolor": PALETTE["accent"],
                }
            },
            "TEntry":   {"configure": {"padding": 6, "borderwidth": 1}},
            "TSpinbox": {"configure": {"padding": 4}},
            "Treeview": {
                "configure": {"background": PALETTE["surface"],
                              "fieldbackground": PALETTE["surface"],
                              "foreground": PALETTE["text"],
                              "rowheight": 28,
                              "font": ("Segoe UI", 10)},
            },
            "Treeview.Heading": {
                "configure": {"background": PALETTE["surface_alt"],
                              "foreground": PALETTE["text"],
                              "font": ("Segoe UI", 10, "bold"),
                              "relief": "flat"}
            },
        })
    # Активуємо тему (безпечно викликати скільки завгодно разів)
    style.theme_use("Classroom")
    style.configure("Surface.TFrame", background=PALETTE["surface"])

class ToolTip:
    """Спливна підказка з назвою кнопки при наведенні курсора."""

    def __init__(self, widget, text, delay=400):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip = None
        self.after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        if self.text:
            self.after_id = self.widget.after(self.delay, self._show)

    def _show(self):
        if self.tip or not self.text or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        try:
            self.tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(self.tip, text=self.text, justify="left",
                 background="#1f2937", foreground="#ffffff",
                 relief="solid", borderwidth=1, font=("Segoe UI", 9),
                 padx=8, pady=4).pack()

    def _cancel(self):
        if self.after_id:
            try:
                self.widget.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None

    def _hide(self, _event=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


class ScreenViewer:
    def __init__(self, root, token):
        self.root = root
        self.token = token
        self.lock = threading.Lock()

        self.clients = {}
        self.selected = set()
        self.cards = {}
        self.order = []
        self._grid_sig = None
        self.server_socket = None
        self.running = True
        self.stop_event = threading.Event()

        self.demo_active = False
        self.demo_stop = None
        self.demo_panel = None
        self.demo_label = None

        self.video_panel = None
        self.video_label = None
        self.video_targets = set()
        self.video_pending = set()

        self.transfers = {}
        self.next_file_id = 0
        self.tr_win = None
        self.tr_body = None
        self.tr_rows = {}

        self.msg_log = []
        self.msg_win = None
        self.msg_tree = None

        self.locked_addrs = set()
        self.input_blocked_addrs = set()
        self.net_blocked_addrs = set()
        self.net_state = {}        # address -> {"state","mode","error"}
        self.net_clients = {}      # address -> привілейований клієнт netguard

        self.large_windows = {}

        self._icons = {}
        self._icon_cache = {}

        _configure_theme()
        self._build_icons()
        self._load_button_icons()

        self.root.title("Система керування комп'ютерним класом")
        self.root.geometry("1320x800")
        self.root.configure(background=PALETTE["bg"])

        self._build_toolbar_primary()
        self._build_statusbar()
        self._build_grid()

        threading.Thread(target=self.server_loop, daemon=True).start()
        threading.Thread(target=self.announce_loop, daemon=True).start()
        self.root.after(REFRESH_MS, self.refresh)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # ------------------------------------------------------------------
    # Іконки
    # ------------------------------------------------------------------
    def _build_icons(self):
        """Завантажує PNG-іконки із теки img/ у двох розмірах (28 і 20)."""
        if _ICON_DEBUG:
            print(f"[icons] IMG_DIR={IMG_DIR!r} exists={os.path.isdir(IMG_DIR)} "
                  f"files={len(os.listdir(IMG_DIR)) if os.path.isdir(IMG_DIR) else 0}",
                  file=sys.stderr)
        files = {
            "demo":    "share_screen.png",
            "stop":    "stop.png",
            "chat":    "sent_msg.png",
            "file":    "sent_file.png",
            "lock":    "lock_student.png",
            "unlock":  "unlock_student.png",
            "power":   "shutdown.png",
            "net":     "globe.png",
            "net_off": "lock_web.png",
            "select":  "check_all.png",
            "clear":   "uncheck_all.png",
            "info":    "info.png",
            "trash":   "trash.png",
            "plus":    "plus.png",
            "minus":   "minus.png",
            "eye":     "sent_log.png",
            "package": "package.png",
            "check":   "check.png",
            "error":   "error.png",
            "warning": "warning.png",
            "upload":  "upload.png",
            "undo":    "undo.png",
            "cursor":  "cursor.png",
            "clock":   "clock.png",
            "live":    "live.png",
        }
        for key, filename in files.items():
            self._icons[key] = self._load_icon(filename, 28)
            self._icons[key + "_sm"] = self._load_icon(filename, 20)

    def _load_icon(self, filename, size):
        """Повертає ImageTk.PhotoImage з img/<filename>, масштабований до size.
        Результат кешується; якщо файл відсутній — повертає None."""
        key = (filename, size)
        if key in self._icon_cache:
            return self._icon_cache[key]
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:  # старіші версії Pillow
            resample = Image.LANCZOS
        photo = None
        try:
            img = Image.open(os.path.join(IMG_DIR, filename)).convert("RGBA")
            img = img.resize((size, size), resample)
            photo = ImageTk.PhotoImage(img)
        except Exception as exc:
            if _ICON_DEBUG:
                print(f"[icons] _load_icon {filename}@{size} FAILED: {exc!r}",
                      file=sys.stderr)
            photo = None
        self._icon_cache[key] = photo
        return photo

    def _load_button_icons(self):
        """Завантажує PNG-іконки кнопок із теки img/ (лише зображення)."""
        mapping = {
            "select_all":  ("check_all.png", "select"),
            "select_none": ("uncheck_all.png", "clear"),
            "demo":        ("share_screen.png", "demo"),
            "message":     ("sent_msg.png", "chat"),
            "send_file":   ("sent_file.png", "file"),
            "video":       ("view_video.png", "demo"),
            "youtube":     ("view_youtube.png", "net"),
            "transfers":   ("sent_log.png", "eye"),
            "msg_log":     ("msg_log.png", "eye"),
            "details":     ("info.png", "info"),
            "lock":        ("lock_student.png", "lock"),
            "unlock":      ("unlock_student.png", "unlock"),
            "lock_input":  ("lock_input.png", "lock"),
            "unlock_input":("unlock_input.png", "unlock"),
            "shutdown":    ("shutdown.png", "power"),
            "lock_web":    ("lock_web.png", "net_off"),
            "unlock_web":  ("unlock_web.png", "net"),
        }
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:  # старіші версії Pillow
            resample = Image.LANCZOS
        self._btn_icons = {}
        for name, (filename, fallback) in mapping.items():
            path = os.path.join(IMG_DIR, filename)
            try:
                img = Image.open(path).convert("RGBA")
                img = img.resize((40, 40), resample)
                self._btn_icons[name] = ImageTk.PhotoImage(img)
            except Exception as exc:
                if _ICON_DEBUG:
                    print(f"[icons] _load_button_icons {name} ({filename}) FAILED: "
                          f"{exc!r}", file=sys.stderr)
                self._btn_icons[name] = self._icons.get(fallback + "_sm")

    # ------------------------------------------------------------------
    # UI: статус-бар та тулбари
    # ------------------------------------------------------------------
    def _build_statusbar(self):
        bar = ttk.Frame(self.root, style="TFrame")
        bar.pack(side="bottom", fill="x", padx=10, pady=(4, 10))
        self.status_icon = ttk.Label(bar, image=self._icons["info"])
        self.status_icon.pack(side="left")
        self.status = ttk.Label(bar, text="", font=("Segoe UI", 10))
        self.status.pack(side="left", padx=(6, 0))

    def _build_toolbar_primary(self):
        row = ttk.Frame(self.root)
        row.pack(fill="x", pady=(0, 8))

        # Спільний білий фрейм на всю ширину вікна для всіх трьох панелей
        bar = ttk.Frame(row, style="Surface.TFrame", padding=(10, 2))
        bar.pack(side="left", fill="both", expand=True)

        # --- Вибір ---
        select_bar = ttk.LabelFrame(bar, text="  Вибір  ", padding=8)
        select_bar.pack(side="left", fill="y", padx=(0, 6))

        btn_all = ttk.Button(select_bar, image=self._btn_icons["select_all"],
                             style="Toolbutton.TButton",
                             command=self.select_all)
        btn_all.pack(side="left", padx=(0, 2))
        ToolTip(btn_all, "Усі")

        btn_none = ttk.Button(select_bar, image=self._btn_icons["select_none"],
                              style="Toolbutton.TButton",
                              command=self.select_none)
        btn_none.pack(side="left", padx=2)
        ToolTip(btn_none, "Зняти")

        # --- Керування виділеними комп'ютерами ---
        manage_bar = ttk.LabelFrame(
            bar, text="  Керування комп'ютерами в мережі ", padding=8)
        manage_bar.pack(side="left", fill="y", padx=6)

        btn_demo = ttk.Button(manage_bar, image=self._btn_icons["demo"],
                              style="Toolbutton.TButton",
                              command=self.start_demo)
        btn_demo.pack(side="left", padx=(0, 2))
        ToolTip(btn_demo, "Показати мій екран")

        btn_msg = ttk.Button(manage_bar, image=self._btn_icons["message"],
                             style="Toolbutton.TButton",
                             command=self.open_compose)
        btn_msg.pack(side="left", padx=2)
        ToolTip(btn_msg, "Повідомлення")

        btn_file = ttk.Button(manage_bar, image=self._btn_icons["send_file"],
                              style="Toolbutton.TButton",
                              command=self.open_file_dialog)
        btn_file.pack(side="left", padx=2)
        ToolTip(btn_file, "Надіслати файли")

        btn_video = ttk.Button(manage_bar, image=self._btn_icons["video"],
                               style="Toolbutton.TButton",
                               command=self.open_video_dialog)
        btn_video.pack(side="left", padx=2)
        ToolTip(btn_video, "Перегляд відеофайлу")

        btn_youtube = ttk.Button(manage_bar, image=self._btn_icons["youtube"],
                                 style="Toolbutton.TButton",
                                 command=self.open_youtube_dialog)
        btn_youtube.pack(side="left", padx=2)
        ToolTip(btn_youtube, "Відео з YouTube")

        ttk.Separator(manage_bar, orient="vertical").pack(side="left", fill="y", padx=8)

        btn_lock = ttk.Button(manage_bar, image=self._btn_icons["lock"],
                              style="Toolbutton.TButton",
                              command=lambda: self.apply_lock("selected", True))
        btn_lock.pack(side="left", padx=(0, 2))
        ToolTip(btn_lock, "Блокувати")

        btn_unlock = ttk.Button(manage_bar, image=self._btn_icons["unlock"],
                                style="Toolbutton.TButton",
                                command=lambda: self.apply_lock("selected", False))
        btn_unlock.pack(side="left", padx=2)
        ToolTip(btn_unlock, "Розблокувати")

        ttk.Separator(manage_bar, orient="vertical").pack(side="left", fill="y", padx=8)

        btn_lock_input = ttk.Button(
            manage_bar, image=self._btn_icons["lock_input"],
            style="Toolbutton.TButton",
            command=lambda: self.apply_input("selected", True))
        btn_lock_input.pack(side="left", padx=2)
        ToolTip(btn_lock_input, "Блокувати введення (мишу/клавіатуру)")

        btn_unlock_input = ttk.Button(
            manage_bar, image=self._btn_icons["unlock_input"],
            style="Toolbutton.TButton",
            command=lambda: self.apply_input("selected", False))
        btn_unlock_input.pack(side="left", padx=2)
        ToolTip(btn_unlock_input, "Розблокувати введення")

        ttk.Separator(manage_bar, orient="vertical").pack(side="left", fill="y", padx=8)

        btn_off = ttk.Button(manage_bar, image=self._btn_icons["shutdown"],
                             style="Toolbutton.TButton",
                             command=lambda: self.open_shutdown_dialog("selected"))
        btn_off.pack(side="left", padx=2)
        ToolTip(btn_off, "Вимкнути")

        ttk.Separator(manage_bar, orient="vertical").pack(side="left", fill="y", padx=8)

        btn_lock_web = ttk.Button(manage_bar, image=self._btn_icons["lock_web"],
                                  style="Toolbutton.TButton",
                                  command=lambda: self.apply_net("selected", True))
        btn_lock_web.pack(side="left", padx=2)
        ToolTip(btn_lock_web, "Блокувати Інтернет")

        btn_unlock_web = ttk.Button(manage_bar, image=self._btn_icons["unlock_web"],
                                    style="Toolbutton.TButton",
                                    command=lambda: self.apply_net("selected", False))
        btn_unlock_web.pack(side="left", padx=(2, 0))
        ToolTip(btn_unlock_web, "Розблокувати Інтернет")

        # --- Інфо: журнали та загальна інформація ---
        info_bar = ttk.LabelFrame(bar, text="  Інфо  ", padding=8)
        info_bar.pack(side="right", fill="y", padx=(6, 0))

        btn_tr = ttk.Button(info_bar, image=self._btn_icons["transfers"],
                            style="Toolbutton.TButton",
                            command=self.open_transfers)
        btn_tr.pack(side="left", padx=(0, 2))
        ToolTip(btn_tr, "Журнал передачі файлів")

        btn_msg_log = ttk.Button(info_bar, image=self._btn_icons["msg_log"],
                                 style="Toolbutton.TButton",
                                 command=self.open_messages)
        btn_msg_log.pack(side="left", padx=2)
        ToolTip(btn_msg_log, "Журнал повідомлень")

        btn_details = ttk.Button(info_bar, image=self._btn_icons["details"],
                                 style="Toolbutton.TButton",
                                 command=self.show_details)
        btn_details.pack(side="left", padx=2)
        ToolTip(btn_details, "Загальна інформація")

    def _build_grid(self):
        container = ttk.Frame(self.root)
        container.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.canvas = tk.Canvas(container, highlightthickness=0,
                                bg=PALETTE["bg"], bd=0)
        self.scrollbar = ttk.Scrollbar(container, orient="vertical",
                                       command=self.canvas.yview)
        self.grid_frame = ttk.Frame(self.canvas)
        self.grid_frame.bind("<Configure>",
                             lambda _e: self.canvas.configure(
                                 scrollregion=self.canvas.bbox("all")))
        self.canvas_window = self.canvas.create_window(
            (0, 0), window=self.grid_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(
                             self.canvas_window, width=e.width))
        # Прокрутка коліщатком миші
        self.canvas.bind_all("<MouseWheel>",
                             lambda e: self.canvas.yview_scroll(
                                 -1 * (e.delta // 120), "units"))
        self.canvas.bind_all("<Button-4>",
                             lambda _e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>",
                             lambda _e: self.canvas.yview_scroll(1, "units"))

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

    # ------------------------------------------------------------------
    # Деталі підключення
    # ------------------------------------------------------------------
    def show_details(self):
        win = tk.Toplevel(self.root)
        win.title("Деталі підключення")
        win.configure(bg=PALETTE["surface"])
        win.resizable(False, False)
        body = ttk.Frame(win, padding=18)
        body.pack(fill="both", expand=True)

        wrap = 600

        def section(title):
            ttk.Label(body, text=title,
                      font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(10, 2))

        def paragraph(text):
            ttk.Label(body, text=text, justify="left",
                      wraplength=wrap).pack(anchor="w")

        ttk.Label(body, text="СККК — Система керування комп'ютерним класом",
                  image=self._load_icon("info.png", 22), compound="left",
                  font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(body, foreground=PALETTE["muted"],
                  text="Мережеві параметри поточного підключення").pack(anchor="w", pady=(2, 0))
        ttk.Separator(body).pack(fill="x", pady=8)
        ttk.Label(body, text=f"TCP (зображення):   порт {PORT}",
                  font=("Segoe UI", 10)).pack(anchor="w", pady=2)
        ttk.Label(body, text=f"UDP (виявлення):    порт {DISCOVERY_PORT}",
                  font=("Segoe UI", 10)).pack(anchor="w", pady=2)
        ttk.Label(body, text=f"Інтервал оголошень: {ANNOUNCE_INTERVAL:g} с",
                  font=("Segoe UI", 10)).pack(anchor="w", pady=2)

        section("Призначення")
        paragraph(
            "СККК призначена для роботи вчителя в комп'ютерному класі: вона "
            "об'єднує вчительський і учнівські комп'ютери в межах однієї "
            "локальної мережі та дає змогу бачити їхні екрани й централізовано "
            "керувати класом під час уроку.")

        section("Можливості")
        paragraph(
            "• Перегляд екранів учнів у реальному часі та збільшений перегляд "
            "окремого учня.\n"
            "• Демонстрація екрана вчителя на вибрані комп'ютери учнів.\n"
            "• Надсилання текстових повідомлень та ведення їх журналу.\n"
            "• Передавання файлів і відеофайлів, відкриття відео з YouTube у "
            "браузері учня.\n"
            "• Блокування екрана, введення (миша/клавіатура) та виходу в "
            "інтернет.\n"
            "• Віддалене вимкнення комп'ютерів учнів із відліком і можливістю "
            "скасування.")

        section("Запуск на комп'ютері вчителя")
        paragraph("Запустіть:  python3 teacher.py\n"
                  "Вікно програми має залишатися відкритим протягом уроку.")

        section("Запуск на комп'ютерах учнів")
        paragraph("На кожному учнівському комп'ютері запустіть:  "
                  "python student_agent.py\n"
                  "IP-адресу вчителя вводити не потрібно — агент знаходить її "
                  "автоматично через UDP. Введіть той самий токен, що й на "
                  "комп'ютері вчителя. Використовуйте систему лише в шкільній "
                  "мережі, де моніторинг оголошено.")

        ttk.Button(body, text="Закрити", style="Accent.TButton",
                   command=win.destroy).pack(anchor="e", pady=(14, 0))

    # ------------------------------------------------------------------
    # UDP broadcast-оголошення
    # ------------------------------------------------------------------
    def announce_loop(self):
        name = socket.gethostname()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        can_receive = True
        try:
            sock.bind(("", DISCOVERY_PORT))
            sock.settimeout(1.0)
        except OSError:
            # порт зайнятий — працюємо в режимі лише розсилки
            can_receive = False
            try:
                sock.close()
            except OSError:
                pass
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if can_receive:
            print(f"Виявлення: слухаю UDP-порт {DISCOVERY_PORT} (запити учнів). "
                  f"Оголошення надсилаю на {', '.join(local_broadcast_targets())}.")
        else:
            print(f"Виявлення: UDP-порт {DISCOVERY_PORT} зайнятий — лише розсилка оголошень.",
                  file=sys.stderr)
        targets = local_broadcast_targets()
        if BROADCAST_ADDR not in targets:
            targets.append(BROADCAST_ADDR)
        try:
            last_sent = 0.0
            last_reply_log = 0.0
            while not self.stop_event.is_set():
                now = time.time()
                if now - last_sent >= ANNOUNCE_INTERVAL:
                    last_sent = now
                    packet = build_announcement(self.token, PORT, name)
                    for target in targets:
                        try:
                            sock.sendto(packet, (target, DISCOVERY_PORT))
                        except OSError:
                            pass
                if not can_receive:
                    self.stop_event.wait(1.0)
                    continue
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                # відповідь на запит «де вчитель?» — надсилаємо напряму учню
                if parse_query(self.token, data):
                    if now - last_reply_log >= 30.0:
                        last_reply_log = now
                        print(f"Виявлення: відповідаю учню {addr[0]} на запит.")
                    try:
                        sock.sendto(build_announcement(self.token, PORT, name),
                                     (addr[0], addr[1]))
                    except OSError:
                        pass
        finally:
            sock.close()

    # ------------------------------------------------------------------
    # TCP-сервер
    # ------------------------------------------------------------------
    def server_loop(self):
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((HOST, PORT))
            srv.listen(50)
            srv.settimeout(1.0)
            self.server_socket = srv
        except OSError as exc:
            error_text = str(exc)
            self.root.after(0, lambda msg=error_text: messagebox.showerror(
                "Помилка сервера", msg))
            return
        while self.running:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.handle_client, args=(conn, addr),
                             daemon=True).start()

    def handle_client(self, conn, addr):
        client_id = f"{addr[0]}:{addr[1]}"
        registered = False
        alive = threading.Event()
        wake = threading.Event()
        try:
            conn.settimeout(15)
            hello = server_handshake(conn, self.token)
            if hello is None:
                return
            name, role = hello
            name = name or addr[0]
            if role == ROLE_NET:
                self.serve_net_client(conn, addr, name)
                return
            with self.lock:
                self.clients[client_id] = {
                    "name": name, "image": None, "seen": time.time(),
                    "address": addr[0], "conn": conn, "wake": wake,
                    "pending": None, "outbox": [], "file_queue": [],
                    "locked": False, "lock_mode": "",
                    "input_locked": False, "input_mode": "",
                    "power": {"state": "none", "deadline": None, "error": ""},
                }
                registered = True
            threading.Thread(target=self.writer_loop,
                             args=(client_id, conn, wake, alive),
                             daemon=True).start()
            while self.running:
                packet = recv_packet(conn)
                kind = packet[:1]
                if kind == MSG_FRAME:
                    with self.lock:
                        if client_id in self.clients:
                            self.clients[client_id]["image"] = packet[1:]
                            self.clients[client_id]["seen"] = time.time()
                elif kind == MSG_FILE_RESULT:
                    self.on_file_result(client_id, packet[1:])
                elif kind == MSG_LOCK_STATE:
                    self.on_lock_state(client_id, packet[1:])
                elif kind == MSG_INPUT_STATE:
                    self.on_input_state(client_id, packet[1:])
                elif kind == MSG_POWER_STATE:
                    self.on_power_state(client_id, packet[1:])
                elif kind == MSG_VIDEO_RESULT:
                    self.on_video_result(client_id, packet[1:])
        except (OSError, ValueError, ConnectionError, json.JSONDecodeError,
                KeyError, TypeError, AttributeError):
            pass
        finally:
            alive.set()
            wake.set()
            with self.lock:
                for (cid, _fid), t in self.transfers.items():
                    if cid == client_id and t["state"] not in ("done", "error"):
                        t["state"] = "error"
                        t["error"] = "З'єднання втрачено"
            if registered:
                with self.lock:
                    self.clients.pop(client_id, None)
            try:
                conn.close()
            except OSError:
                pass

    def writer_loop(self, client_id, conn, wake, alive):
        queue = []
        current = None
        try:
            while self.running and not alive.is_set():
                with self.lock:
                    client = self.clients.get(client_id)
                    packets = []
                    if client:
                        packets, client["outbox"] = client["outbox"], []
                        if client["pending"]:
                            packets.append(client["pending"])
                            client["pending"] = None
                        queue.extend(client["file_queue"])
                        client["file_queue"] = []
                try:
                    for packet in packets:
                        send_packet(conn, packet)
                    if current is None and queue:
                        info = queue.pop(0)
                        try:
                            fh = open(info["path"], "rb")
                        except OSError as exc:
                            self.update_transfer(
                                client_id, info["id"], state="error",
                                error=f"Не вдалося відкрити файл: "
                                      f"{exc.strerror or exc}")
                        else:
                            meta = {"id": info["id"], "name": info["name"],
                                    "size": info["size"], "sha256": info["sha256"],
                                    "mode": info.get("mode", "file")}
                            send_packet(conn, MSG_FILE_START +
                                        json.dumps(meta).encode("utf-8"))
                            self.update_transfer(client_id, info["id"],
                                                 state="sending")
                            current = (info, fh)
                    if current is not None:
                        info, fh = current
                        fid = info["id"]
                        if self.transfer_state(client_id, fid) == "error":
                            send_packet(conn, MSG_FILE_END +
                                        struct.pack("!IB", fid, 0))
                            fh.close()
                            current = None
                        else:
                            try:
                                chunk = fh.read(FILE_CHUNK)
                            except OSError:
                                chunk = None
                            if chunk:
                                send_packet(conn, MSG_FILE_DATA +
                                            struct.pack("!I", fid) + chunk)
                                self.bump_sent(client_id, fid, len(chunk))
                            else:
                                ok = chunk is not None
                                send_packet(conn, MSG_FILE_END +
                                            struct.pack("!IB", fid, 1 if ok else 0))
                                fh.close()
                                current = None
                                if ok:
                                    with self.lock:
                                        t = self.transfers.get((client_id, fid))
                                        if t and t["state"] == "sending":
                                            t["state"] = "waiting"
                                else:
                                    self.update_transfer(client_id, fid,
                                                         state="error",
                                                         error="Помилка читання файлу")
                except OSError:
                    try:
                        conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    break
                if not packets and current is None and not queue:
                    wake.wait(1.0)
                    wake.clear()
        finally:
            if current is not None:
                current[1].close()

    def queue_packet(self, client_id, packet):
        with self.lock:
            client = self.clients.get(client_id)
            if client:
                client["pending"] = packet
                client["wake"].set()

    # ------------------------------------------------------------------
    # Привілейований клієнт керування інтернетом (netguard)
    # ------------------------------------------------------------------
    def serve_net_client(self, conn, addr, name):
        """Обслуговує окреме підключення netguard (роль «net») для адреси addr[0].

        Netguard лише отримує MSG_NET і повідомляє MSG_NET_STATE — екран і файли
        він не передає, тому не реєструється серед звичайних клієнтів."""
        address = addr[0]
        alive = threading.Event()
        wake = threading.Event()
        try:
            conn.settimeout(90)
            with self.lock:
                old = self.net_clients.pop(address, None)
                if old is not None:
                    try:
                        old["conn"].close()
                    except OSError:
                        pass
                self.net_clients[address] = {
                    "name": name, "seen": time.time(), "address": address,
                    "conn": conn, "wake": wake, "outbox": [],
                }
                desired = address in self.net_blocked_addrs
                state = self.net_state.get(address)
            if state and state.get("state") == "blocked":
                self.net_blocked_addrs.add(address)
            elif state is None:
                self.net_state.setdefault(address, {"state": "none", "mode": "",
                                                    "error": ""})
            threading.Thread(target=self.net_writer_loop,
                             args=(address, conn, wake, alive),
                             daemon=True).start()
            if desired and not (state and state.get("state") == "blocked"):
                self.send_net_to(address, True)
            while self.running:
                packet = recv_packet(conn)
                if packet[:1] == MSG_NET_STATE:
                    self.on_net_state_addr(address, packet[1:])
        except (OSError, ValueError, ConnectionError, json.JSONDecodeError,
                KeyError, TypeError, AttributeError):
            pass
        finally:
            alive.set()
            wake.set()
            with self.lock:
                current = self.net_clients.get(address)
                if current and current["conn"] is conn:
                    self.net_clients.pop(address, None)
            try:
                conn.close()
            except OSError:
                pass

    def net_writer_loop(self, address, conn, wake, alive):
        try:
            while self.running and not alive.is_set():
                with self.lock:
                    client = self.net_clients.get(address)
                    packets = []
                    if client and client["conn"] is conn:
                        packets, client["outbox"] = client["outbox"], []
                for packet in packets:
                    send_packet(conn, packet)
                if not packets:
                    wake.wait(1.0)
                    wake.clear()
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            alive.set()

    def send_net_to(self, address, blocked):
        """Надсилає MSG_NET привілейованому клієнту за адресою. True — якщо
        клієнт підключений (запит поставлено в чергу)."""
        packet = MSG_NET + (b"\x01" if blocked else b"\x00")
        with self.lock:
            client = self.net_clients.get(address)
            if not client:
                return False
            client["outbox"].append(packet)
            client["wake"].set()
            return True

    # ------------------------------------------------------------------
    # Текстові повідомлення
    # ------------------------------------------------------------------
    def send_text(self, client_ids, text):
        packet = MSG_TEXT + text.encode("utf-8")
        sent = 0
        names = []
        with self.lock:
            for cid in client_ids:
                client = self.clients.get(cid)
                if client:
                    client["outbox"].append(packet)
                    client["wake"].set()
                    names.append(client["name"] or client["address"])
                    sent += 1
        if sent:
            self.log_message(names, text)
        return sent

    def log_message(self, recipients, text):
        """Додає запис про надіслане повідомлення до журналу повідомлень."""
        if len(recipients) > 3:
            to = ", ".join(recipients[:3]) + f" та ще {len(recipients) - 3}"
        else:
            to = ", ".join(recipients)
        entry = {"time": time.time(), "to": to, "text": text}
        with self.lock:
            self.msg_log.append(entry)
            if len(self.msg_log) > MAX_MESSAGE_LOG:
                del self.msg_log[:-MAX_MESSAGE_LOG]
        if self.msg_win is not None and self.msg_win.winfo_exists():
            self.root.after(0, self.update_messages)

    def open_compose(self, single_cid=None):
        with self.lock:
            total = len(self.clients)
            chosen = sum(1 for c in self.clients.values()
                         if c["address"] in self.selected)
            single = self.clients.get(single_cid) if single_cid is not None else None
            single_name = single["name"] if single else None
            if single_cid is not None and single_name is None:
                return

        win = tk.Toplevel(self.root)
        win.title(f"Повідомлення — {single_name}" if single_cid is not None
                  else "Повідомлення учням")
        win.geometry("600x340")
        win.configure(bg=PALETTE["surface"])
        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)

        title = ttk.Label(body,
                          text=single_name if single_cid is not None
                          else "Повідомлення учням",
                          image=self._load_icon("sent_msg.png", 22),
                          compound="left",
                          font=("Segoe UI", 12, "bold"))
        title.pack(anchor="w")
        info = (f"Одержувач: {single_name}" if single_cid is not None
                else f"Підключено: {total}  ·  обрано: {chosen}")
        ttk.Label(body, text=info, foreground=PALETTE["muted"]).pack(anchor="w", pady=(2, 8))
        ttk.Label(body,
                  text=f"Текст повідомлення (до {MAX_MESSAGE_CHARS} символів):").pack(anchor="w")

        text = tk.Text(body, height=8, wrap="word", font=("Segoe UI", 11),
                       bg=PALETTE["surface"], fg=PALETTE["text"],
                       insertbackground=PALETTE["text"],
                       relief="solid", bd=1, padx=6, pady=6)
        text.pack(fill="both", expand=True, pady=(4, 10))
        text.focus_set()

        def send(mode):
            message = text.get("1.0", "end").strip()
            if not message:
                messagebox.showinfo("Повідомлення",
                                    "Введіть текст повідомлення.", parent=win)
                return
            message = message[:MAX_MESSAGE_CHARS]
            with self.lock:
                if mode == "one":
                    ids = [single_cid]
                elif mode == "selected":
                    ids = [cid for cid, c in self.clients.items()
                           if c["address"] in self.selected]
                else:
                    ids = list(self.clients)
            if not ids:
                messagebox.showinfo("Повідомлення",
                                    "Немає адресатів.", parent=win)
                return
            sent = self.send_text(ids, message)
            win.destroy()
            messagebox.showinfo("Повідомлення",
                                f"Надіслано комп'ютерам: {sent}")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Скасувати",
                   command=win.destroy).pack(side="right")
        if single_cid is not None:
            ttk.Button(buttons, text="Надіслати", style="Accent.TButton",
                       command=lambda: send("one")).pack(side="right", padx=4)
        else:
            ttk.Button(buttons, text="Всім", style="Accent.TButton",
                       command=lambda: send("all")).pack(side="right", padx=4)
            ttk.Button(buttons, text="Обраним", style="Accent.TButton",
                       command=lambda: send("selected")).pack(side="right", padx=4)

    # ------------------------------------------------------------------
    # Передавання файлів
    # ------------------------------------------------------------------
    def update_transfer(self, cid, fid, **fields):
        with self.lock:
            t = self.transfers.get((cid, fid))
            if t:
                t.update(fields)

    def transfer_state(self, cid, fid):
        with self.lock:
            t = self.transfers.get((cid, fid))
            return t["state"] if t else None

    def bump_sent(self, cid, fid, n):
        with self.lock:
            t = self.transfers.get((cid, fid))
            if t:
                t["sent"] += n

    def on_file_result(self, client_id, payload):
        try:
            res = json.loads(payload.decode("utf-8"))
            fid = int(res["id"])
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        with self.lock:
            t = self.transfers.get((client_id, fid))
            if not t:
                return
            if res.get("ok"):
                t["state"] = "done"
                t["sent"] = t["size"]
            else:
                t["state"] = "error"
                t["error"] = str(res.get("error", "Помилка"))[:200]
            video_failed = t.get("mode") == "video" and t["state"] == "error"
            client = self.clients.get(client_id)
            address = client["address"] if client else None
        if video_failed and address is not None:
            self.root.after(0, lambda a=address: self._video_drop(a))

    def send_files(self, client_ids, paths, mode="file"):
        errors = []
        for path in paths:
            name = os.path.basename(path)
            try:
                digest = hashlib.sha256()
                size = 0
                with open(path, "rb") as f:
                    while True:
                        block = f.read(1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        size += len(block)
            except OSError as exc:
                errors.append(f"{name}: {exc.strerror or exc}")
                continue
            if size > MAX_FILE_BYTES:
                errors.append(f"{name}: більше {MAX_FILE_MB} МБ")
                continue
            with self.lock:
                self.next_file_id += 1
                fid = self.next_file_id
                info = {"id": fid, "name": name, "path": path, "size": size,
                        "sha256": digest.hexdigest(), "mode": mode}
                for cid in client_ids:
                    client = self.clients.get(cid)
                    if client:
                        self.transfers[(cid, fid)] = {
                            "student": client["name"], "file": name,
                            "size": size, "sent": 0, "mode": mode,
                            "state": "queued", "error": ""}
                        client["file_queue"].append(info)
                        client["wake"].set()
        if errors:
            text = "Не вдалося підготувати:\n" + "\n".join(errors)
            self.root.after(0, lambda: messagebox.showerror("Файли", text))

    def resolve_targets(self, mode, single_cid):
        with self.lock:
            if mode == "one":
                return [single_cid] if single_cid in self.clients else []
            if mode == "selected":
                return [cid for cid, c in self.clients.items()
                        if c["address"] in self.selected]
            return list(self.clients)

    def open_file_dialog(self, single_cid=None):
        with self.lock:
            total = len(self.clients)
            chosen = sum(1 for c in self.clients.values()
                         if c["address"] in self.selected)
            single = self.clients.get(single_cid) if single_cid is not None else None
            single_name = single["name"] if single else None
            if single_cid is not None and single_name is None:
                return

        win = tk.Toplevel(self.root)
        win.title(f"Файли — {single_name}" if single_cid is not None
                  else "Надіслати файли учням")
        win.geometry("660x440")
        win.configure(bg=PALETTE["surface"])
        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)

        title = ttk.Label(body,
                          text=single_name if single_cid is not None
                          else "Надіслати файли учням",
                          image=self._load_icon("sent_file.png", 22),
                          compound="left",
                          font=("Segoe UI", 12, "bold"))
        title.pack(anchor="w")
        info = (f"Одержувач: {single_name}" if single_cid is not None
                else f"Підключено: {total}  ·  обрано: {chosen}")
        ttk.Label(body, text=info, foreground=PALETTE["muted"]).pack(anchor="w", pady=(2, 6))
        ttk.Label(body, wraplength=600, justify="left",
                  text=f"Ліміт: {MAX_FILE_MB} МБ на файл, до {MAX_FILES_PER_SEND} файлів за раз. "
                       "Файли зберігаються у папці учня, автоматично не відкриваються.").pack(anchor="w")

        items = []

        top = ttk.Frame(body)
        top.pack(fill="x", pady=8)
        ttk.Button(top, text="Додати файли…", image=self._icons["plus_sm"],
                   compound="left", style="Accent.TButton",
                   command=lambda: add()).pack(side="left")
        ttk.Button(top, text="Прибрати вибрані", image=self._icons["minus_sm"],
                   compound="left",
                   command=lambda: remove()).pack(side="left", padx=6)

        listbox = tk.Listbox(body, selectmode="extended", height=10,
                             font=("Segoe UI", 10),
                             bg=PALETTE["surface"], fg=PALETTE["text"],
                             selectbackground=PALETTE["accent"],
                             selectforeground="#ffffff",
                             relief="solid", bd=1)
        listbox.pack(fill="both", expand=True, pady=4)

        total_label = ttk.Label(body, anchor="w")
        total_label.pack(fill="x")

        def redraw():
            listbox.delete(0, "end")
            for path, size in items:
                listbox.insert("end",
                               f"  {os.path.basename(path)}   —   {format_size(size)}")
            total_label.configure(
                text=f"Файлів: {len(items)}   ·   разом {format_size(sum(s for _, s in items))}")

        def add():
            picked = filedialog.askopenfilenames(parent=win,
                                                 title="Оберіть файли для учнів")
            problems = []
            for path in picked:
                name = os.path.basename(path)
                if any(path == p for p, _ in items):
                    continue
                if len(items) >= MAX_FILES_PER_SEND:
                    problems.append(
                        f"Ліміт: не більше {MAX_FILES_PER_SEND} файлів за раз.")
                    break
                try:
                    if not os.path.isfile(path):
                        raise OSError("це не файл")
                    size = os.path.getsize(path)
                except OSError as exc:
                    problems.append(f"{name}: "
                                    f"{getattr(exc, 'strerror', None) or exc}")
                    continue
                if size > MAX_FILE_BYTES:
                    problems.append(f"{name}: більше {MAX_FILE_MB} МБ")
                    continue
                if is_risky_file(name) and not ALLOW_EXEC:
                    problems.append(
                        f"{name}: виконувані типи файлів заблоковано")
                    continue
                items.append((path, size))
            redraw()
            if problems:
                messagebox.showwarning("Файли",
                                       "Пропущено:\n" + "\n".join(problems),
                                       parent=win)

        def remove():
            for index in reversed(listbox.curselection()):
                del items[index]
            redraw()

        def send(mode):
            if not items:
                messagebox.showinfo("Файли", "Додайте хоча б один файл.",
                                    parent=win)
                return
            ids = self.resolve_targets(mode, single_cid)
            if not ids:
                messagebox.showinfo("Файли", "Немає адресатів.", parent=win)
                return
            paths = [p for p, _ in items]
            win.destroy()
            threading.Thread(target=self.send_files, args=(ids, paths),
                             daemon=True).start()
            self.open_transfers()

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Скасувати",
                   command=win.destroy).pack(side="right")
        if single_cid is not None:
            ttk.Button(buttons, text="Надіслати", style="Accent.TButton",
                       command=lambda: send("one")).pack(side="right", padx=4)
        else:
            ttk.Button(buttons, text="Всім", style="Accent.TButton",
                       command=lambda: send("all")).pack(side="right", padx=4)
            ttk.Button(buttons, text="Обраним", style="Accent.TButton",
                       command=lambda: send("selected")).pack(side="right", padx=4)

        redraw()
        win.after(150, add)

    # ------------------------------------------------------------------
    # Перегляд відео: відеофайл і YouTube
    # ------------------------------------------------------------------
    VIDEO_FILETYPES = [
        ("Відеофайли", "*.mp4 *.mkv *.avi *.webm *.mov *.wmv *.flv *.mpg "
                       "*.mpeg *.ogv *.m4v *.3gp *.ts *.m2ts *.vob"),
        ("Усі файли", "*.*"),
    ]

    def open_video_dialog(self, single_cid=None):
        with self.lock:
            total = len(self.clients)
            chosen = sum(1 for c in self.clients.values()
                         if c["address"] in self.selected)
            single = self.clients.get(single_cid) if single_cid is not None else None
            single_name = single["name"] if single else None
            if single_cid is not None and single_name is None:
                return

        win = tk.Toplevel(self.root)
        win.title(f"Перегляд відео — {single_name}" if single_cid is not None
                  else "Перегляд відео в учнів")
        win.geometry("620x300")
        win.configure(bg=PALETTE["surface"])
        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body,
                  text=single_name if single_cid is not None
                  else "Перегляд відеофайлу",
                  image=self._load_icon("view_video.png", 22),
                  compound="left",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        info = (f"Одержувач: {single_name}" if single_cid is not None
                else f"Підключено: {total}  ·  обрано: {chosen}")
        ttk.Label(body, text=info, foreground=PALETTE["muted"]).pack(anchor="w", pady=(2, 6))
        ttk.Label(body, wraplength=580, justify="left",
                  text="Оберіть відеофайл. Його буде надіслано в тимчасову папку "
                       "учня, відтворено штатним відеоплеєром і видалено після "
                       "завершення перегляду.").pack(anchor="w")

        chosen_path = {"path": None}
        file_label = ttk.Label(body, text="Файл не обрано.",
                               foreground=PALETTE["muted"], wraplength=580)
        file_label.pack(anchor="w", pady=(8, 4))

        def pick():
            path = filedialog.askopenfilename(
                parent=win, title="Оберіть відеофайл", filetypes=self.VIDEO_FILETYPES)
            if not path:
                return
            name = os.path.basename(path)
            if not is_video_file(name):
                messagebox.showwarning("Відео", "Дозволено лише відеофайли.",
                                       parent=win)
                return
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                messagebox.showerror("Відео", f"{name}: "
                                     f"{getattr(exc, 'strerror', None) or exc}",
                                     parent=win)
                return
            if size > MAX_FILE_BYTES:
                messagebox.showwarning(
                    "Відео", f"{name}: більше {MAX_FILE_MB} МБ", parent=win)
                return
            chosen_path["path"] = path
            file_label.configure(text=f"Обрано: {name} ({format_size(size)})",
                                 foreground=PALETTE["text"])

        ttk.Button(body, text="Обрати відеофайл…",
                   image=self._icons["plus_sm"], compound="left",
                   command=pick).pack(anchor="w")

        def send(mode):
            path = chosen_path["path"]
            if not path:
                messagebox.showinfo("Відео", "Оберіть відеофайл.", parent=win)
                return
            ids = self.resolve_targets(mode, single_cid)
            if not ids:
                messagebox.showinfo("Відео", "Немає адресатів.", parent=win)
                return
            with self.lock:
                addresses = {self.clients[cid]["address"] for cid in ids
                             if cid in self.clients}
            win.destroy()
            self.send_video(ids, path, addresses)
            self.open_transfers()

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", side="bottom", pady=(10, 0))
        ttk.Button(buttons, text="Скасувати",
                   command=win.destroy).pack(side="right")
        if single_cid is not None:
            ttk.Button(buttons, text="Надіслати", style="Accent.TButton",
                       command=lambda: send("one")).pack(side="right", padx=4)
        else:
            ttk.Button(buttons, text="Всім", style="Accent.TButton",
                       command=lambda: send("all")).pack(side="right", padx=4)
            ttk.Button(buttons, text="Обраним", style="Accent.TButton",
                       command=lambda: send("selected")).pack(side="right", padx=4)

    def send_video(self, client_ids, path, addresses=None):
        self.video_targets = set(addresses or ())
        self.video_pending = set(addresses or ())
        self.send_files(client_ids, [path], mode="video")
        if self.video_targets:
            self.show_video_panel()

    def show_video_panel(self):
        if self.video_panel is not None and self.video_panel.winfo_exists():
            return
        panel = tk.Toplevel(self.root)
        panel.title("Перегляд відео")
        panel.attributes("-topmost", True)
        panel.configure(bg=PALETTE["surface"])
        body = ttk.Frame(panel, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Перегляд відео в учнів",
                  image=self._load_icon("view_video.png", 22),
                  compound="left",
                  font=("Segoe UI", 12, "bold"),
                  foreground=PALETTE["success"]).pack(anchor="w")
        self.video_label = ttk.Label(body, text="Надсилання відео…",
                                     font=("Segoe UI", 11),
                                     foreground=PALETTE["muted"],
                                     wraplength=300, justify="left")
        self.video_label.pack(anchor="w", pady=(4, 12))
        ttk.Button(body, text="Зупинити й видалити файл",
                   image=self._icons["stop"], compound="left",
                   style="Danger.TButton",
                   command=self.stop_video).pack()
        panel.geometry("340x170")
        panel.resizable(False, False)
        panel.protocol("WM_DELETE_WINDOW", self.stop_video)
        self.video_panel = panel

    def close_video_panel(self):
        if self.video_panel is not None:
            try:
                self.video_panel.destroy()
            except tk.TclError:
                pass
        self.video_panel = None
        self.video_label = None
        self.video_targets = set()
        self.video_pending = set()

    def _video_drop(self, address):
        if address is None:
            return
        self.video_pending.discard(address)
        if not self.video_pending:
            self.close_video_panel()

    def stop_video(self):
        with self.lock:
            for cid, client in self.clients.items():
                if client["address"] in self.video_targets:
                    client["outbox"].append(MSG_VIDEO_STOP)
                    client["file_queue"] = [i for i in client["file_queue"]
                                            if i.get("mode") != "video"]
                    for (c, _f), t in self.transfers.items():
                        if (c == cid and t.get("mode") == "video"
                                and t["state"] not in ("done", "error")):
                            t["state"] = "error"
                            t["error"] = "Скасовано вчителем"
                    client["wake"].set()
        self.close_video_panel()

    def on_video_result(self, client_id, payload):
        try:
            res = json.loads(payload.decode("utf-8"))
            phase = str(res.get("phase"))
        except (ValueError, TypeError, AttributeError):
            return
        with self.lock:
            item = self.clients.get(client_id)
            name = item["name"] if item else "Учень"
            address = item["address"] if item else None
        if phase == "done":
            text = f"{name}: перегляд завершено"
        elif phase == "error":
            text = f"{name}: помилка відтворення ({str(res.get('error', ''))[:60]})"
        else:
            text = f"{name}: відтворюється відео…"

        def apply():
            if phase in ("done", "error"):
                self._video_drop(address)
                return
            if self.video_label is not None and self.video_label.winfo_exists():
                self.video_label.configure(text=text)
        self.root.after(0, apply)

    def open_youtube_dialog(self, single_cid=None):
        with self.lock:
            total = len(self.clients)
            chosen = sum(1 for c in self.clients.values()
                         if c["address"] in self.selected)
            single = self.clients.get(single_cid) if single_cid is not None else None
            single_name = single["name"] if single else None
            if single_cid is not None and single_name is None:
                return

        win = tk.Toplevel(self.root)
        win.title(f"Відео з YouTube — {single_name}" if single_cid is not None
                  else "Відео з YouTube для учнів")
        win.geometry("620x300")
        win.configure(bg=PALETTE["surface"])
        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body,
                  text=single_name if single_cid is not None
                  else "Відео з YouTube",
                  image=self._load_icon("view_youtube.png", 22),
                  compound="left",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        info = (f"Одержувач: {single_name}" if single_cid is not None
                else f"Підключено: {total}  ·  обрано: {chosen}")
        ttk.Label(body, text=info, foreground=PALETTE["muted"]).pack(anchor="w", pady=(2, 6))
        ttk.Label(body, wraplength=580, justify="left",
                  text="Вставте посилання на відео YouTube. У браузері учня за "
                       "замовчуванням буде відкрито цю сторінку.").pack(anchor="w")

        entry = ttk.Entry(body, font=("Segoe UI", 11))
        entry.pack(fill="x", pady=(10, 4))
        entry.focus_set()

        def send(mode):
            url = entry.get().strip()
            if not is_youtube_url(url):
                messagebox.showwarning(
                    "YouTube", "Введіть коректне посилання на youtube.com "
                    "або youtu.be.", parent=win)
                return
            ids = self.resolve_targets(mode, single_cid)
            if not ids:
                messagebox.showinfo("YouTube", "Немає адресатів.", parent=win)
                return
            win.destroy()
            sent = self.send_youtube(ids, url)
            messagebox.showinfo("YouTube", f"Відкрито на комп'ютерах: {sent}")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", side="bottom", pady=(10, 0))
        ttk.Button(buttons, text="Скасувати",
                   command=win.destroy).pack(side="right")
        if single_cid is not None:
            ttk.Button(buttons, text="Відкрити", style="Accent.TButton",
                       command=lambda: send("one")).pack(side="right", padx=4)
        else:
            ttk.Button(buttons, text="Всім", style="Accent.TButton",
                       command=lambda: send("all")).pack(side="right", padx=4)
            ttk.Button(buttons, text="Обраним", style="Accent.TButton",
                       command=lambda: send("selected")).pack(side="right", padx=4)

    def send_youtube(self, client_ids, url):
        packet = MSG_YOUTUBE + json.dumps({"url": url}).encode("utf-8")
        sent = 0
        with self.lock:
            for cid in client_ids:
                client = self.clients.get(cid)
                if client:
                    client["outbox"].append(packet)
                    client["wake"].set()
                    sent += 1
        return sent

    # --- вікно «Передачі» ---
    @staticmethod
    def transfer_text(t):
        """Повертає (текст, назва_файлу_іконки) для рядка передачі."""
        state = t["state"]
        video = t.get("mode") == "video"
        if state == "queued":
            return "У черзі", "clock.png"
        if state == "sending":
            pct = int(100 * t["sent"] / t["size"]) if t["size"] else 100
            return f"Передається… {min(pct, 100)}%", "upload.png"
        if state == "waiting":
            return "Очікуємо підтвердження", "clock.png"
        if state == "done":
            return (("Відтворено на комп'ютері учня" if video
                     else "Збережено на комп'ютері учня"), "check.png")
        return (t["error"] or "Помилка"), "error.png"

    def open_transfers(self):
        if self.tr_win is not None and self.tr_win.winfo_exists():
            self.tr_win.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("Передавання файлів")
        win.geometry("760x440")
        win.configure(bg=PALETTE["surface"])
        body = ttk.Frame(win, padding=10)
        body.pack(fill="both", expand=True)
        head = ttk.Frame(body)
        head.pack(fill="x", pady=(0, 6))
        ttk.Label(head, text="Активні та завершені передачі",
                  image=self._load_icon("package.png", 22), compound="left",
                  font=("Segoe UI", 12, "bold")).pack(side="left")
        ttk.Button(head, text="Очистити завершені",
                   image=self._icons["trash_sm"], compound="left",
                   command=self.clear_finished).pack(side="right")

        bottom = ttk.Frame(body)
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(bottom, text="Закрити",
                   command=win.destroy).pack(side="right")

        middle = ttk.Frame(body)
        middle.pack(fill="both", expand=True)
        canvas = tk.Canvas(middle, bg=PALETTE["surface"], highlightthickness=0)
        scroll = ttk.Scrollbar(middle, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(win_id, width=canvas.winfo_width())
        inner.bind("<Configure>", _sync)
        canvas.bind("<Configure>", _sync)

        self.tr_win, self.tr_body, self.tr_rows = win, inner, {}
        self.update_transfers()

    def clear_finished(self):
        with self.lock:
            for key in [k for k, t in self.transfers.items()
                        if t["state"] in ("done", "error")]:
                del self.transfers[key]

    def cancel_transfer(self, cid, fid):
        """Примусово зупиняє передачу. Повторно не поновлюється."""
        with self.lock:
            t = self.transfers.get((cid, fid))
            client = self.clients.get(cid)
            if not t or t["state"] in ("done", "error"):
                return
            t["state"] = "error"
            t["error"] = "Скасовано вчителем"
            if client:
                client["file_queue"] = [i for i in client["file_queue"]
                                        if i.get("id") != fid]
                client["wake"].set()

    def _make_tr_row(self, key, t):
        row = ttk.Frame(self.tr_body, padding=(8, 6))
        row.pack(fill="x", pady=2)
        top = ttk.Frame(row)
        top.pack(fill="x")
        is_video = t.get("mode") == "video"
        head = ttk.Label(top, text=f"{t['student']}  —  {t['file']}  ({format_size(t['size'])})",
                         font=("Segoe UI", 10, "bold"))
        if is_video:
            head.configure(image=self._load_icon("view_video.png", 16),
                           compound="left")
        head.pack(side="left")
        cancel = ttk.Button(top, text="Зупинити", width=11,
                            image=self._load_icon("stop.png", 16), compound="left",
                            command=lambda k=key: self.cancel_transfer(*k))
        cancel.pack(side="right")
        pct = ttk.Label(top, text="", width=6, anchor="e",
                        font=("Segoe UI", 10, "bold"))
        pct.pack(side="right", padx=(0, 6))
        bar = ttk.Progressbar(row, maximum=100, value=0, length=100)
        bar.pack(fill="x", pady=(4, 2))
        status = ttk.Label(row, text="", foreground=PALETTE["muted"])
        status.pack(anchor="w")
        widgets = {"row": row, "bar": bar, "pct": pct, "status": status,
                   "cancel": cancel}
        self.tr_rows[key] = widgets
        return widgets

    def _update_tr_row(self, widgets, t, pct):
        state = t["state"]
        active = state in ("queued", "sending", "waiting")
        widgets["bar"]["value"] = pct
        widgets["pct"].configure(text=f"{pct}%")
        color = PALETTE["muted"]
        if state == "sending":
            color = PALETTE["accent"]
        elif state == "done":
            color = PALETTE["success"]
        elif state == "error":
            color = PALETTE["danger"]
        status_text, status_icon = self.transfer_text(t)
        widgets["status"].configure(
            text=status_text, foreground=color,
            image=self._load_icon(status_icon, 14) if status_icon else "",
            compound="left" if status_icon else "none")
        widgets["cancel"].configure(state="normal" if active else "disabled")

    def update_transfers(self):
        win = self.tr_win
        if win is None or not win.winfo_exists():
            self.tr_win = self.tr_body = None
            self.tr_rows = {}
            return
        with self.lock:
            data = {}
            for (cid, fid), t in self.transfers.items():
                size = t["size"]
                pct = int(100 * t["sent"] / size) if size else 100
                if t["state"] == "done":
                    pct = 100
                pct = max(0, min(100, pct))
                data[(cid, fid)] = (dict(t), pct)
        try:
            for key in [k for k in self.tr_rows if k not in data]:
                self.tr_rows.pop(key)["row"].destroy()
            for key, (t, pct) in data.items():
                widgets = self.tr_rows.get(key) or self._make_tr_row(key, t)
                self._update_tr_row(widgets, t, pct)
        except tk.TclError:
            self.tr_rows = {}
        win.after(500, self.update_transfers)

    # ------------------------------------------------------------------
    # Журнал повідомлень
    # ------------------------------------------------------------------
    def open_messages(self):
        if self.msg_win is not None and self.msg_win.winfo_exists():
            self.msg_win.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("Журнал повідомлень")
        win.geometry("760x420")
        win.configure(bg=PALETTE["surface"])
        body = ttk.Frame(win, padding=10)
        body.pack(fill="both", expand=True)
        head = ttk.Frame(body)
        head.pack(fill="x")
        ttk.Label(head, text="Надіслані повідомлення",
                  image=self._load_icon("sent_msg.png", 22), compound="left",
                  font=("Segoe UI", 12, "bold")).pack(side="left")
        ttk.Button(head, text="Очистити",
                   image=self._icons["trash_sm"], compound="left",
                   command=self.clear_messages).pack(side="right")

        bottom = ttk.Frame(body)
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(bottom, text="Закрити",
                   command=win.destroy).pack(side="right")

        middle = ttk.Frame(body)
        middle.pack(fill="both", expand=True, pady=(8, 0))
        tree = ttk.Treeview(middle, columns=("time", "to", "text"),
                            show="headings", height=14)
        for col, title, width in (("time", "Час", 90),
                                  ("to", "Одержувачі", 220),
                                  ("text", "Текст", 400)):
            tree.heading(col, text=title)
            tree.column(col, width=width, anchor="w")
        scroll = ttk.Scrollbar(middle, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.msg_win, self.msg_tree = win, tree
        self.update_messages()

    def clear_messages(self):
        with self.lock:
            self.msg_log.clear()
        self.update_messages()

    def update_messages(self):
        win, tree = self.msg_win, self.msg_tree
        if win is None or not win.winfo_exists():
            self.msg_win = self.msg_tree = None
            return
        with self.lock:
            entries = list(self.msg_log)
        tree.delete(*tree.get_children())
        for e in entries:
            tree.insert("", "end", values=(
                time.strftime("%H:%M:%S", time.localtime(e["time"])),
                e["to"], e["text"].replace("\n", " ")))
        if entries:
            tree.see(tree.get_children()[-1])

    # ------------------------------------------------------------------
    # Блокування комп'ютерів учнів
    # ------------------------------------------------------------------
    def apply_lock(self, mode, locked, single_cid=None):
        ids = self.resolve_targets(mode, single_cid)
        if locked:
            if not ids:
                messagebox.showinfo("Блокування", "Немає адресатів.")
                return
            if mode == "all" and not messagebox.askyesno(
                    "Блокування",
                    f"Заблокувати всі комп'ютери ({len(ids)})?"):
                return
        packet = MSG_LOCK + (b"\x01" if locked else b"\x00")
        with self.lock:
            for cid in ids:
                client = self.clients.get(cid)
                if client:
                    client["outbox"].append(packet)
                    client["wake"].set()
                    if locked:
                        self.locked_addrs.add(client["address"])
                    else:
                        self.locked_addrs.discard(client["address"])
        if not locked:
            if mode == "all":
                self.locked_addrs.clear()
            elif mode == "selected":
                self.locked_addrs -= self.selected

    def on_lock_state(self, client_id, payload):
        try:
            state = json.loads(payload.decode("utf-8"))
            locked = bool(state["locked"])
            mode = str(state.get("mode", ""))
            initial = bool(state.get("initial"))
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        with self.lock:
            client = self.clients.get(client_id)
            if not client:
                return
            client["locked"] = locked
            client["lock_mode"] = mode
            address = client["address"]
            if mode in ("unsupported", "error"):
                self.locked_addrs.discard(address)
            elif initial:
                if locked:
                    self.locked_addrs.add(address)
                elif address in self.locked_addrs:
                    client["outbox"].append(MSG_LOCK + b"\x01")
                    client["wake"].set()

    @staticmethod
    def lock_badge(item, desired):
        """Повертає (текст, колір, назва_файлу_іконки)."""
        if item["locked"]:
            if item["lock_mode"] == "full":
                return "Заблоковано", PALETTE["danger"], "lock_student.png"
            return ("Екран перекрито, введення НЕ заблоковано",
                    PALETTE["warning"], "lock_student.png")
        if desired:
            return "Блокування…", PALETTE["danger"], "lock_student.png"
        if item["lock_mode"] in ("unsupported", "error"):
            return "Блокування недоступне", PALETTE["warning"], "warning.png"
        return "", PALETTE["text"], None

    # ------------------------------------------------------------------
    # Блокування введення (миша/клавіатура) без перекриття екрана
    # ------------------------------------------------------------------
    def apply_input(self, mode, blocked, single_cid=None):
        ids = self.resolve_targets(mode, single_cid)
        if blocked and not ids:
            messagebox.showinfo("Блокування введення", "Немає адресатів.")
            return
        packet = MSG_INPUT + (b"\x01" if blocked else b"\x00")
        with self.lock:
            for cid in ids:
                client = self.clients.get(cid)
                if client is None:
                    continue
                if client["input_mode"] == "unsupported":
                    continue
                client["outbox"].append(packet)
                client["wake"].set()
                if blocked:
                    self.input_blocked_addrs.add(client["address"])
                else:
                    self.input_blocked_addrs.discard(client["address"])
        if not blocked:
            if mode == "all":
                self.input_blocked_addrs.clear()
            elif mode == "selected":
                self.input_blocked_addrs -= self.selected

    def on_input_state(self, client_id, payload):
        try:
            state = json.loads(payload.decode("utf-8"))
            blocked = bool(state["blocked"])
            mode = str(state.get("mode", ""))
            initial = bool(state.get("initial"))
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        with self.lock:
            client = self.clients.get(client_id)
            if not client:
                return
            client["input_locked"] = blocked
            client["input_mode"] = mode
            address = client["address"]
            if mode == "unsupported":
                self.input_blocked_addrs.discard(address)
            elif initial:
                if blocked:
                    self.input_blocked_addrs.add(address)
                elif address in self.input_blocked_addrs:
                    client["outbox"].append(MSG_INPUT + b"\x01")
                    client["wake"].set()

    @staticmethod
    def input_badge(item, desired):
        """Повертає (текст, колір, назва_файлу_іконки)."""
        if item["input_locked"]:
            return "Введення заблоковано", PALETTE["danger"], "cursor.png"
        if desired:
            return "Блокування введення…", PALETTE["danger"], "cursor.png"
        if item["input_mode"] == "unsupported":
            return ("Блокування введення недоступне",
                    PALETTE["warning"], "warning.png")
        return "", PALETTE["text"], None

    # ------------------------------------------------------------------
    # Блокування інтернету
    # ------------------------------------------------------------------
    def apply_net(self, mode, blocked, single_cid=None):
        ids = self.resolve_targets(mode, single_cid)
        if not ids:
            messagebox.showinfo("Інтернет", "Немає адресатів.")
            return
        if blocked and mode == "all" and not messagebox.askyesno(
                "Інтернет",
                f"Заблокувати вихід в інтернет на всіх ({len(ids)})? "
                "Локальна мережа та зв'язок із цим комп'ютером залишаться."):
            return
        with self.lock:
            addresses = {self.clients[cid]["address"] for cid in ids
                         if cid in self.clients}
            if not blocked and mode == "all":
                addresses |= set(self.net_blocked_addrs)
            if blocked:
                self.net_blocked_addrs |= addresses
            else:
                self.net_blocked_addrs -= addresses

        missing = [address for address in addresses
                   if not self.send_net_to(address, blocked)]
        if missing:
            messagebox.showinfo(
                "Інтернет",
                f"Немає підключеної служби керування інтернетом (netguard) "
                f"для {len(missing)} комп'ютер(ів).\n"
                "Запустіть netguard на цих комп'ютерах — тоді блокування "
                "працюватиме.")

    def on_net_state_addr(self, address, payload):
        try:
            report = json.loads(payload.decode("utf-8"))
            state = str(report["state"])
            mode = str(report.get("mode", ""))
            error = str(report.get("error", ""))[:200]
            initial = bool(report.get("initial"))
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        with self.lock:
            self.net_state[address] = {"state": state, "mode": mode, "error": error}
            if mode == "unsupported" or state == "error":
                self.net_blocked_addrs.discard(address)
                return
            desired = address in self.net_blocked_addrs
            if state == "blocked":
                self.net_blocked_addrs.add(address)
        if not initial:
            return
        # узгоджуємо реальний стан фаєрвола з наміром учителя
        if state == "blocked" and not desired:
            self.send_net_to(address, False)
        elif state != "blocked" and desired:
            self.send_net_to(address, True)

    @staticmethod
    def net_badge(state, desired, has_client):
        """Повертає (текст, колір, назва_файлу_іконки)."""
        if state and state["state"] == "blocked":
            return "Інтернет заблоковано", PALETTE["danger"], "lock_web.png"
        if state and state["state"] == "working":
            return "Застосовується…", PALETTE["warning"], "globe.png"
        if desired and not has_client:
            return ("Немає netguard", PALETTE["warning"], "warning.png")
        if desired:
            return "Блокування інтернету…", PALETTE["danger"], "lock_web.png"
        if state and (state["mode"] == "unsupported" or state["state"] == "error"):
            return ("Блокування недоступне", PALETTE["warning"], "warning.png")
        return "", PALETTE["text"], None

    # ------------------------------------------------------------------
    # Вимкнення комп'ютерів учнів
    # ------------------------------------------------------------------
    def open_shutdown_dialog(self, mode, single_cid=None):
        ids = self.resolve_targets(mode, single_cid)
        if not ids:
            messagebox.showinfo("Вимкнення", "Немає адресатів.")
            return
        with self.lock:
            names = sorted(self.clients[cid]["name"] for cid in ids
                           if cid in self.clients)
        win = tk.Toplevel(self.root)
        win.title("Вимкнення комп'ютерів")
        win.resizable(False, False)
        win.configure(bg=PALETTE["surface"])
        body = ttk.Frame(win, padding=18)
        body.pack(fill="both", expand=True)

        everyone = mode == "all"
        head = (f"Буде вимкнено ВСІ ({len(ids)})" if everyone
                else f"Буде вимкнено: {len(ids)}")
        ttk.Label(body, text=head, image=self._icons["power"], compound="left",
                  font=("Segoe UI", 13, "bold"),
                  foreground=PALETTE["danger"]).pack(anchor="w")
        shown = ", ".join(names[:8]) + (
            f" та ще {len(names) - 8}" if len(names) > 8 else "")
        ttk.Label(body, text=shown, wraplength=460, justify="left").pack(
            anchor="w", pady=(6, 4))
        ttk.Label(body, wraplength=460, justify="left",
                  foreground=PALETTE["muted"],
                  text="Незбережені дані буде втрачено. Під час відліку на "
                       "екрані учня показується попередження; вимкнення можна скасувати.").pack(
            anchor="w", pady=(4, 10))

        row = ttk.Frame(body)
        row.pack(anchor="w")
        ttk.Label(row, text="Відлік перед вимкненням, с:").pack(side="left")
        delay_var = tk.StringVar(value=str(SHUTDOWN_DELAY))
        ttk.Spinbox(row, from_=SHUTDOWN_MIN, to=600, width=6,
                    textvariable=delay_var).pack(side="left", padx=6)

        def go():
            try:
                delay = int(delay_var.get())
            except ValueError:
                messagebox.showwarning("Вимкнення",
                                       "Вкажіть відлік цілим числом секунд.",
                                       parent=win)
                return
            delay = min(600, max(SHUTDOWN_MIN, delay))
            win.destroy()
            self.send_shutdown(ids, delay)

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Скасувати",
                   command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="Вимкнути", style="Danger.TButton",
                   image=self._icons["power"], compound="left",
                   command=go).pack(side="right", padx=6)
        win.grab_set()

    def send_shutdown(self, client_ids, delay):
        packet = MSG_POWER + json.dumps(
            {"action": "shutdown", "delay": delay}).encode("utf-8")
        with self.lock:
            for cid in client_ids:
                client = self.clients.get(cid)
                if client:
                    client["outbox"].append(packet)
                    client["wake"].set()
                    client["power"] = {"state": "pending",
                                       "deadline": time.time() + delay,
                                       "error": ""}

    def cancel_shutdown(self, mode, single_cid=None):
        ids = self.resolve_targets(mode, single_cid)
        packet = MSG_POWER + json.dumps({"action": "cancel"}).encode("utf-8")
        with self.lock:
            for cid in ids:
                client = self.clients.get(cid)
                if client:
                    client["outbox"].append(packet)
                    client["wake"].set()

    def on_power_state(self, client_id, payload):
        try:
            report = json.loads(payload.decode("utf-8"))
            state = str(report["state"])
            remaining = int(report.get("remaining", 0))
            error = str(report.get("error", ""))[:200]
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        with self.lock:
            client = self.clients.get(client_id)
            if client:
                deadline = time.time() + remaining if state == "pending" else None
                client["power"] = {"state": state, "deadline": deadline,
                                   "error": error}

    @staticmethod
    def power_badge(item):
        """Повертає (текст, колір, назва_файлу_іконки)."""
        power = item["power"]
        state = power["state"]
        if state == "pending":
            left = max(0, int(power["deadline"] - time.time())) if power["deadline"] else 0
            return (f"Вимкнення через {left // 60:02d}:{left % 60:02d}",
                    PALETTE["danger"], "shutdown.png")
        if state == "executing":
            return "Вимикається…", PALETTE["danger"], "shutdown.png"
        if state == "cancelled":
            return "Вимкнення скасовано", PALETTE["muted"], "undo.png"
        if state == "failed":
            return ("Не вдалося вимкнути: " + (
                    power["error"] or "помилка"), PALETTE["warning"], "warning.png")
        return "", PALETTE["text"], None

    # ------------------------------------------------------------------
    # Вибір отримувачів і демонстрація екрана
    # ------------------------------------------------------------------
    def toggle(self, address, var):
        with self.lock:
            if var.get():
                self.selected.add(address)
            else:
                self.selected.discard(address)

    def select_all(self):
        with self.lock:
            self.selected = {c["address"] for c in self.clients.values()}
        self.sync_checkboxes()

    def select_none(self):
        with self.lock:
            self.selected.clear()
        self.sync_checkboxes()

    def sync_checkboxes(self):
        with self.lock:
            selected = set(self.selected)
            for card in self.cards.values():
                card["var"].set(card["address"] in selected)

    def start_demo(self):
        if self.demo_active:
            return
        with self.lock:
            count = sum(1 for c in self.clients.values()
                        if c["address"] in self.selected)
        if not count:
            messagebox.showinfo("Демонстрація",
                                "Спершу позначте комп'ютери учнів, "
                                "яким потрібно показати ваш екран.")
            return
        self.demo_active = True
        self.demo_stop = threading.Event()
        threading.Thread(target=self.demo_loop, args=(self.demo_stop,),
                         daemon=True).start()
        self.show_demo_panel()
        self.root.withdraw()

    def show_demo_panel(self):
        panel = tk.Toplevel(self.root)
        panel.title("Демонстрація активна")
        panel.attributes("-topmost", True)
        panel.configure(bg=PALETTE["surface"])
        body = ttk.Frame(panel, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Трансляція вашого екрана",
                  image=self._load_icon("live.png", 22), compound="left",
                  font=("Segoe UI", 12, "bold"),
                  foreground=PALETTE["success"]).pack(anchor="w")
        self.demo_label = ttk.Label(body, text="",
                                    font=("Segoe UI", 11),
                                    foreground=PALETTE["muted"],
                                    wraplength=320, justify="left")
        self.demo_label.pack(anchor="w", pady=(4, 12))
        ttk.Button(body, text="Зупинити демонстрацію",
                   image=self._icons["stop"], compound="left",
                   style="Danger.TButton",
                   command=self.stop_demo).pack()
        panel.geometry("360x170")
        panel.resizable(False, False)
        panel.protocol("WM_DELETE_WINDOW", self.stop_demo)
        self.demo_panel = panel

    def stop_demo(self):
        if not self.demo_active:
            return
        self.demo_active = False
        if self.demo_stop:
            self.demo_stop.set()
        if self.demo_panel:
            try:
                self.demo_panel.destroy()
            except tk.TclError:
                pass
        self.demo_panel = None
        self.demo_label = None
        self.root.deiconify()

    def demo_loop(self, stop_event):
        receiving = set()
        error = None
        try:
            with new_mss() as sct:
                while not stop_event.is_set():
                    started = time.time()
                    with self.lock:
                        targets = {cid for cid, c in self.clients.items()
                                   if c["address"] in self.selected}
                    for cid in receiving - targets:
                        self.queue_packet(cid, MSG_STOP)
                    if targets:
                        frame = capture_jpeg(sct, DEMO_MAX_WIDTH,
                                             DEMO_QUALITY, monitor=1)
                        packet = MSG_DEMO + frame
                        for cid in targets:
                            self.queue_packet(cid, packet)
                    receiving = targets
                    stop_event.wait(max(0.05,
                                        DEMO_INTERVAL - (time.time() - started)))
        except Exception as exc:
            error = exc
        finally:
            for cid in receiving:
                self.queue_packet(cid, MSG_STOP)
            if error is not None:
                def report():
                    self.stop_demo()
                    messagebox.showerror("Помилка демонстрації", str(error))
                self.root.after(0, report)

    # ------------------------------------------------------------------
    # Інтерфейс: картки учнів
    # ------------------------------------------------------------------
    def open_large(self, client_id):
        """Велике вікно з екраном учня: масштабується під вікно, оновлюється
        наживо. Повторний клік по тій самій картці піднімає вже відкрите вікно."""
        with self.lock:
            item = self.clients.get(client_id)
            if item is None or item.get("image") is None:
                return
            name = item["name"]
            address = item["address"]

        existing = self.large_windows.get(address)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title(f"Екран учня — {name}")
        win.configure(bg=PALETTE["bg"])
        win.minsize(640, 400)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w, h = max(900, int(sw * 0.85)), max(600, int(sh * 0.85))
        win.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2)}")

        # Панель керування
        bar = ttk.Frame(win, padding=(10, 8))
        bar.pack(side="top", fill="x")
        name_lbl = ttk.Label(bar, text=name, font=("Segoe UI", 12, "bold"))
        name_lbl.pack(side="left")
        info_lbl = ttk.Label(bar, text="", foreground=PALETTE["muted"])
        info_lbl.pack(side="left", padx=(12, 0))

        # Зона зображення
        area = tk.Frame(win, bg="#0f1115")
        area.pack(side="top", fill="both", expand=True)
        canvas = tk.Canvas(area, bg="#0f1115", highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)

        state = {"photo": None, "raw": None, "orig": (0, 0), "size": (0, 0),
                 "last_shown": None, "msg": None, "dirty": True,
                 "closed": False, "after_id": None, "name": name}

        def resolve():
            with self.lock:
                it = self.clients.get(client_id)
                if it is None and address:
                    for c in self.clients.values():
                        if c["address"] == address:
                            it = c
                            break
                if it is None:
                    return None, None, False
                return it["name"], it["image"], True

        def render():
            if state["closed"] or not win.winfo_exists():
                return
            cw, ch = canvas.winfo_width(), canvas.winfo_height()
            if cw <= 1 or ch <= 1:
                return
            name2, data, alive = resolve()
            resized = (cw, ch) != state["size"]
            if data:
                if not (state["dirty"] or resized or data is not state["last_shown"]):
                    return
                try:
                    img = Image.open(BytesIO(data))
                    img.load()
                    ow, oh = img.size
                    fitted = img.copy()
                    fitted.thumbnail((max(1, cw - 12), max(1, ch - 12)),
                                     Image.LANCZOS)
                    state["photo"] = ImageTk.PhotoImage(fitted)
                    state["raw"] = data
                    state["orig"] = (ow, oh)
                    state["last_shown"] = data
                    state["msg"] = None
                    state["dirty"] = False
                    state["size"] = (cw, ch)
                    canvas.delete("all")
                    canvas.create_image(cw // 2, ch // 2,
                                        image=state["photo"], anchor="center")
                    if name2 and name2 != state["name"]:
                        state["name"] = name2
                        name_lbl.configure(text=name2)
                        win.title(f"Екран учня — {name2}")
                    info_lbl.configure(
                        text=f"{ow}×{oh} px   ·   оновлено "
                             f"{time.strftime('%H:%M:%S')}")
                except Exception:
                    pass
            else:
                msg = ("Очікуємо перший кадр…" if alive
                       else "Зв'язок із учнем втрачено…")
                if state["msg"] != msg:
                    state["msg"] = msg
                    state["dirty"] = False
                    state["size"] = (cw, ch)
                    canvas.delete("all")
                    canvas.create_text(cw // 2, ch // 2, fill="#9aa4b2",
                                       font=("Segoe UI", 14), text=msg)

        def tick():
            if state["closed"] or not win.winfo_exists():
                return
            render()
            state["after_id"] = win.after(REFRESH_MS, tick)

        canvas.bind("<Configure>",
                    lambda _e: (state.__setitem__("dirty", True), render()))

        def save_snapshot():
            data = state.get("raw")
            if not data:
                messagebox.showinfo("Зберегти знімок",
                                    "Ще немає кадру для збереження.",
                                    parent=win)
                return
            default = f"{state['name']}_{time.strftime('%Y%m%d-%H%M%S')}.png"
            path = filedialog.asksaveasfilename(
                parent=win, title="Зберегти знімок екрана",
                defaultextension=".png", initialfile=default,
                filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg")])
            if not path:
                return
            try:
                img = Image.open(BytesIO(data)).convert("RGB")
                if path.lower().endswith((".jpg", ".jpeg")):
                    img.save(path, quality=92)
                else:
                    img.save(path)
                messagebox.showinfo("Збережено",
                                    f"Знімок збережено:\n{path}", parent=win)
            except Exception as exc:
                messagebox.showerror("Помилка збереження", str(exc), parent=win)

        def close():
            state["closed"] = True
            if state["after_id"] and win.winfo_exists():
                try:
                    win.after_cancel(state["after_id"])
                except tk.TclError:
                    pass
            self.large_windows.pop(address, None)
            if win.winfo_exists():
                win.destroy()

        ttk.Button(bar, text="Зберегти знімок…",
                   command=save_snapshot).pack(side="right")
        ttk.Button(bar, text="Закрити",
                   command=close).pack(side="right", padx=(0, 8))

        win.bind("<Escape>", lambda _e: close())
        win.protocol("WM_DELETE_WINDOW", close)

        self.large_windows[address] = win
        win.after(60, tick)

    def _bind_card_open(self, widget, client_id, skip):
        """Робить усю картку клікабельною (відкриває великий перегляд),
        крім віджетів зі `skip` (чекбокс вибору та кнопка-мініатюра)."""
        if widget in skip:
            return
        widget.bind("<Button-1>",
                    lambda _e, c=client_id: self.open_large(c), add="+")
        try:
            widget.configure(cursor="hand2")
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self._bind_card_open(child, client_id, skip)

    def make_card(self, client_id, item):
        address = item["address"]
        frame = tk.Frame(self.grid_frame, width=CARD_W, height=CARD_H,
                         bg=PALETTE["surface"], highlightthickness=1,
                         highlightbackground=PALETTE["border"], bd=0)
        frame.grid_propagate(False)
        frame.pack_propagate(False)

        # Верхній рядок: чекбокс у лівому куті, ім'я праворуч, адреса
        header = tk.Frame(frame, bg=PALETTE["surface"])
        header.pack(fill="x", padx=8, pady=(6, 2))
        var = tk.BooleanVar(value=address in self.selected)
        chk_box = tk.Frame(header, bg=PALETTE["surface"], bd=0,
                           highlightthickness=1,
                           highlightbackground=PALETTE["border"],
                           highlightcolor=PALETTE["accent"])
        chk_box.pack(side="left")
        chk = ttk.Checkbutton(chk_box, variable=var,
                              command=lambda a=address, v=var: self.toggle(a, v))
        chk.pack(padx=2, pady=2)
        ttk.Label(header, text=item["name"], font=("Segoe UI", 9, "bold"),
                  anchor="w").pack(side="left", padx=(4, 0))
        ttk.Label(header, text=address, font=("Segoe UI", 8),
                  foreground=PALETTE["muted"]).pack(side="right")

        # Прев'ю (натискання відкриває збільшений перегляд)
        thumb = ttk.Button(frame, text="Очікуємо перший кадр…",
                           command=lambda cid=client_id: self.open_large(cid),
                           cursor="hand2")
        thumb.pack(pady=(2, 4))

        # Повідомлення стану — одне дворядкове поле з перенесенням тексту
        messages = tk.Text(frame, height=2, width=1, wrap="word", bd=0,
                           highlightthickness=0, relief="flat", padx=0, pady=0,
                           bg=PALETTE["surface"], fg=PALETTE["text"],
                           font=("Segoe UI", 9), cursor="arrow", takefocus=0,
                           exportselection=False, state="disabled")
        messages.pack(fill="x", padx=8, pady=(0, 6))
        messages.tag_configure("sep", foreground=PALETTE["muted"])
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            messages.bind(seq, self._forward_wheel)

        # Уся картка (крім чекбокса) відкриває повномасштабний перегляд
        self._bind_card_open(frame, client_id, skip={chk_box, thumb, messages})

        return {"frame": frame, "thumb": thumb, "var": var,
                "address": address, "messages": messages,
                "name": item["name"], "photo": None, "last_image": None,
                "last_messages": None}

    def _forward_wheel(self, event):
        """Прокручує сітку карток, коли коліщатко над полем повідомлень."""
        if event.num == 4:
            self.canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(1, "units")
        else:
            self.canvas.yview_scroll(-1 * (event.delta // 120), "units")
        return "break"

    def _card_tag(self, widget, color):
        """Ім'я тега tk.Text для заданого кольору тексту."""
        tag = "c" + color.lstrip("#")
        if tag not in widget.tag_names():
            widget.tag_configure(tag, foreground=color)
        return tag

    def _draw_card_messages(self, widget, entries):
        """Малює повідомлення стану в одному дворядковому полі (іконка + текст)."""
        try:
            widget.configure(state="normal")
        except tk.TclError:
            return
        widget.delete("1.0", "end")
        first = True
        for text, color, icon in entries:
            if not text:
                continue
            if not first:
                widget.insert("end", "  ·  ", "sep")
            first = False
            if icon:
                photo = self._load_icon(icon, 14)
                if photo is not None:
                    widget.image_create("end", image=photo)
                    widget.insert("end", " ")
            widget.insert("end", text, self._card_tag(widget, color))
        widget.configure(state="disabled")

    def _make_placeholder(self):
        """Порожній прямокутник для неактивного місця у сітці."""
        frame = tk.Frame(self.grid_frame, width=CARD_W, height=CARD_H,
                         bg=PALETTE["surface_alt"], highlightthickness=1,
                         highlightbackground=PALETTE["border"], bd=0)
        frame.grid_propagate(False)
        frame.pack_propagate(False)
        return frame

    def rebuild_grid(self, snapshot):
        """Перебудовує сітку 3×(5+) карток: активні комп'ютери та порожні місця."""
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.cards.clear()

        used = [cid for cid in self.order if cid in snapshot]
        rows = max(ROWS, math.ceil(len(used) / COLUMNS)) if used else ROWS
        total = rows * COLUMNS
        for index in range(total):
            row, col = divmod(index, COLUMNS)
            if index < len(used):
                cid = used[index]
                card = self.make_card(cid, snapshot[cid])
                self.cards[cid] = card
                widget = card["frame"]
            else:
                widget = self._make_placeholder()
            widget.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)

        for col in range(COLUMNS):
            self.grid_frame.grid_columnconfigure(col, weight=1, minsize=CARD_W)
        for row in range(rows):
            self.grid_frame.grid_rowconfigure(row, minsize=CARD_H)
        self.grid_frame.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def update_thumb(self, card, item):
        image = item["image"]
        if image is None or image is card["last_image"]:
            return
        card["last_image"] = image
        try:
            img = Image.open(BytesIO(image))
            img.thumbnail(THUMB_SIZE)
            photo = ImageTk.PhotoImage(img)
            card["photo"] = photo
            card["thumb"].configure(image=photo, text="", compound="none")
        except Exception:
            card["photo"] = None
            card["thumb"].configure(image="", text="Не вдалося декодувати зображення")

    # ------------------------------------------------------------------
    # Головний цикл оновлення UI
    # ------------------------------------------------------------------
    def refresh(self):
        if not self.running:
            return
        with self.lock:
            present = {c["address"] for c in self.clients.values()}
            self.selected &= present
            snapshot = {k: dict(v) for k, v in self.clients.items()}
            selected = set(self.selected)
            locked_addrs = set(self.locked_addrs)
            input_blocked_addrs = set(self.input_blocked_addrs)
            net_blocked_addrs = set(self.net_blocked_addrs)
            net_state = {k: dict(v) for k, v in self.net_state.items()}
            net_clients = set(self.net_clients)

        self.status.configure(
            text=f"Підключено: {len(snapshot)}   ·   TCP {PORT}   ·   "
                 f"UDP {DISCOVERY_PORT} кожні {ANNOUNCE_INTERVAL:g} с")

        if self.demo_active and self.demo_label:
            receivers = sum(1 for v in snapshot.values()
                            if v["address"] in selected)
            self.demo_label.configure(
                text=f"Ваш екран бачать: {receivers} комп'ютерів")
            self.root.after(REFRESH_MS, self.refresh)
            return

        ids = list(snapshot)
        self.order = [cid for cid in self.order if cid in snapshot]
        for cid in ids:
            if cid not in self.order:
                self.order.append(cid)
        signature = tuple(self.order)
        if signature != self._grid_sig:
            self._grid_sig = signature
            self.rebuild_grid(snapshot)

        for cid, item in snapshot.items():
            card = self.cards.get(cid)
            if card is None:
                continue
            try:
                self.update_thumb(card, item)

                # Повідомлення стану (одне дворядкове поле з перенесенням)
                desired = item["address"] in locked_addrs
                input_desired = item["address"] in input_blocked_addrs
                net_desired = item["address"] in net_blocked_addrs
                entries = [
                    self.lock_badge(item, desired),
                    self.input_badge(item, input_desired),
                    self.power_badge(item),
                    self.net_badge(net_state.get(item["address"]), net_desired,
                                   item["address"] in net_clients),
                ]
                sig = tuple((text, icon) for text, _color, icon in entries)
                if sig != card.get("last_messages"):
                    self._draw_card_messages(card["messages"], entries)
                    card["last_messages"] = sig
            except tk.TclError:
                pass

        self.root.after(REFRESH_MS, self.refresh)

    # ------------------------------------------------------------------
    # Закриття
    # ------------------------------------------------------------------
    def close(self):
        with self.lock:
            locked_now = sum(1 for c in self.clients.values()
                             if c["locked"] or c["address"] in self.locked_addrs)
            net_locked_now = len(self.net_blocked_addrs | {
                a for a, s in self.net_state.items()
                if s.get("state") in ("blocked", "working")})

        if net_locked_now and not locked_now:
            answer = messagebox.askyesnocancel(
                "Заблокований інтернет",
                f"Комп'ютерів із заблокованим інтернетом: {net_locked_now}.\n\n"
                "Так — розблокувати інтернет і вийти\n"
                "Ні — вийти, залишивши інтернет заблокованим\n"
                "Скасувати — не закривати програму")
            if answer is None:
                return
            if answer:
                self.apply_net("all", False)
                time.sleep(0.7)

        if locked_now:
            answer = messagebox.askyesnocancel(
                "Заблоковані комп'ютери",
                f"Заблокованих комп'ютерів: {locked_now}.\n\n"
                "Так — розблокувати їх і вийти\n"
                "Ні — вийти, залишивши їх заблокованими\n"
                "Скасувати — не закривати програму")
            if answer is None:
                return
            if answer:
                self.apply_lock("all", False)
                time.sleep(0.7)

        with self.lock:
            net_locked_now = len(self.net_blocked_addrs | {
                a for a, s in self.net_state.items()
                if s.get("state") in ("blocked", "working")})
        if net_locked_now:
            net_answer = messagebox.askyesnocancel(
                "Заблокований інтернет",
                f"Комп'ютерів із заблокованим інтернетом: {net_locked_now}.\n\n"
                "Так — розблокувати інтернет і вийти\n"
                "Ні — вийти, залишивши інтернет заблокованим\n"
                "Скасувати — не закривати програму")
            if net_answer is None:
                return
            if net_answer:
                self.apply_net("all", False)
                time.sleep(0.7)

        self.running = False
        self.stop_event.set()
        if self.demo_stop:
            self.demo_stop.set()
        try:
            if self.server_socket:
                self.server_socket.close()
        except OSError:
            pass
        self.root.destroy()


SETUP_PROGRAM = "skkk_setup"


def _setup_needed():
    """Чи потрібно перед стартом запустити майстер налаштування середовища."""
    token = load_token()
    if token is None or len(token) < MIN_TOKEN_LEN:
        return True
    return not setup_complete()


def _run_setup(root):
    """Запускає майстер налаштування (skkk_setup.py з вихідного коду або
    skkk_setup.exe поруч для зібраного застосунку) і чекає на завершення."""
    target = program_path(SETUP_PROGRAM)
    if not target.exists():
        messagebox.showwarning(
            "Налаштування середовища",
            f"Не знайдено майстер налаштування:\n{target}",
            parent=root)
        return
    root.withdraw()
    try:
        subprocess.run(program_command(SETUP_PROGRAM, "--no-launch"),
                       cwd=str(app_dir()), check=False)
    except OSError as exc:
        messagebox.showerror(
            "Налаштування середовища",
            f"Не вдалося запустити майстер налаштування:\n{exc}", parent=root)
    finally:
        if root.winfo_exists():
            root.deiconify()
            root.update_idletasks()


def _set_window_icon(root):
    """Встановлює іконку вікна з icon.png (якщо файл доступний)."""
    path = app_icon_path()
    if path is None:
        return
    try:
        photo = tk.PhotoImage(file=str(path))
        root.iconphoto(True, photo)
        root._app_icon = photo  # тримаємо посилання, щоб не зібрало GC
    except Exception as exc:
        if _ICON_DEBUG:
            print(f"[icons] window icon {path} FAILED: {exc!r}", file=sys.stderr)


def main():
    root = tk.Tk()
    root.withdraw()
    _configure_theme()
    _set_window_icon(root)
    if _setup_needed():
        _run_setup(root)
    token = load_token()
    if token is None or len(token) < MIN_TOKEN_LEN:
        messagebox.showerror(
            "Токен доступу",
            "Токен доступу не знайдено або він закороткий.\n\n"
            f"Файл: {token_file_path()}\n\n"
            "Запустіть skkk_setup.py для налаштування середовища.",
            parent=root)
        root.destroy()
        return
    root.deiconify()
    ScreenViewer(root, token)
    root.mainloop()


if __name__ == "__main__":
    main()

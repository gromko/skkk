#!/usr/bin/env python3
"""SKKK — перевірка та автоматичне налаштування середовища.

Графічний майстер, який:
  * перевіряє наявність потрібних бібліотек (tkinter, Pillow, ImageTk, mss),
    доступність локальної мережі та вільність портів;
  * автоматично встановлює відсутні компоненти (apt/pip);
  * за потреби працює від root — з'являється вікно із запитом пароля;
  * відкриває вхідні порти у фаєрволі та вмикає синхронізацію часу;
  * створює токен доступу (якщо його немає);
  * створює classroom_config.json (за потреби);
  * показує журнал дій: успішні кроки — зелена галочка, невдалі — червоний знак;
  * у кінці пропонує запустити teacher.py.

teacher.py більше не виконує ці налаштування самостійно — він викликає цей
майстер, коли середовище ще не налаштоване.
"""
import json
import os
import platform
import queue
import shlex
import socket
import subprocess
import sys
import threading

import common  # легкий модуль без залежностей від mss/Pillow

# Тека застосунку: для зібраного PyInstaller — поряд із .exe, інакше — зі скриптами.
BASE_DIR = common.app_dir()
CONFIG_FILE = BASE_DIR / "classroom_config.json"

try:
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import messagebox, ttk
    HAVE_TK = True
except Exception:                      # tkinter відсутній
    HAVE_TK = False


# --- Мови/кольори інтерфейсу ---
BG = "#f4f6fa"
SURFACE = "#ffffff"
BORDER = "#d8dee8"
TEXT = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#2563eb"
GREEN = "#059669"
RED = "#dc2626"
ORANGE = "#d97706"

ICONS = {"pending": "•", "running": "…", "ok": "\u2714", "fail": "\u2718",
         "warn": "!", "skip": "–"}
ICON_COLORS = {"pending": MUTED, "running": ACCENT, "ok": GREEN, "fail": RED,
               "warn": ORANGE, "skip": MUTED}
TERMINAL = ("ok", "fail", "warn", "skip")

# Залежності: (id, модуль для import, пакети apt, пакети pip)
LIBS = [
    ("tkinter", "tkinter", ["python3-tk"], []),
    ("pillow", "PIL", ["python3-pil"], ["Pillow"]),
    ("imagetk", "PIL.ImageTk", ["python3-pil.imagetk"], []),
    ("mss", "mss", ["python3-mss"], ["mss"]),
]

STEPS = [
    ("python", "Версія Python (3.10+)"),
    ("tkinter", "Модуль tkinter"),
    ("pillow", "Бібліотека Pillow (PIL)"),
    ("imagetk", "Підтримка ImageTk"),
    ("mss", "Бібліотека mss"),
    ("network", "Локальна мережа"),
    ("port_tcp", "Вільний TCP-порт"),
    ("port_udp", "Вільний UDP-порт"),
    ("firewall", "Дозвіл портів у фаєрволі"),
    ("timesync", "Синхронізація часу"),
    ("token", "Токен доступу"),
    ("config", "Файл налаштувань"),
]

CRITICAL = {"python", "tkinter", "pillow", "mss", "token"}


def module_available(name):
    """Чи можна імпортувати модуль (див. common.module_available — враховує
    як запуск зі скриптів, так і зібраний PyInstaller застосунок)."""
    return common.module_available(name)


def port_free(port, proto):
    """(True, "") якщо порт вільний, інакше (False, причина)."""
    kind = socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM
    sock = socket.socket(socket.AF_INET, kind)
    try:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        sock.bind(("", port))
        return True, ""
    except OSError as exc:
        return False, (exc.strerror or str(exc))
    finally:
        sock.close()


def test_broadcast():
    """Перевіряє доступність локальної мережі: чи є IPv4-інтерфейс і чи можна
    надсилати широкомовні пакети. Повертає (ok, опис, [адреси])."""
    ifaces = common._local_ipv4_interfaces()          # noqa: SLF001 (внутрішня)
    ips = [ip for _name, ip, _mask in ifaces]
    if not ips:
        return False, "не знайдено активних IPv4-інтерфейсів", []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(1.0)
        finally:
            sock.close()
    except OSError as exc:
        return False, f"широкомовлення недоступне: {exc}", ips
    return True, ", ".join(ips), ips


class SetupApp:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.rows = {}
        self.completed = 0
        self.total = len(STEPS)
        self.running = False
        self.no_launch = False
        self.family = tkfont.nametofont("TkDefaultFont").actual("family")
        cfg = common.load_config()
        self.tcp_port = int(cfg["tcp_port"])
        self.udp_port = int(cfg["discovery_port"])
        self._update_port_titles()

    # ---------- інтерфейс ----------
    def _update_port_titles(self):
        self.step_titles = {sid: title for sid, title in STEPS}
        self.step_titles["port_tcp"] = f"Вільний TCP-порт {self.tcp_port}"
        self.step_titles["port_udp"] = f"Вільний UDP-порт {self.udp_port}"

    def build(self):
        self.root.title("SKKK — налаштування середовища")
        self.root.configure(bg=BG)
        self.root.geometry("780x680")
        self.root.minsize(660, 560)

        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(header, text="Налаштування середовища SKKK", bg=BG, fg=TEXT,
                 font=(self.family, 16, "bold")).pack(anchor="w")
        tk.Label(header, text="Перевірка бібліотек, мережі, портів і автоматичне "
                              "налаштування фаєрвола, часу та токена.",
                 bg=BG, fg=MUTED, font=(self.family, 10)).pack(anchor="w")

        # Фрейм журналу дій (зі смугою прокрутки)
        steps_wrap = tk.Frame(self.root, bg=SURFACE, highlightthickness=1,
                              highlightbackground=BORDER)
        steps_wrap.pack(fill="both", expand=True, padx=16, pady=(6, 8))
        canvas = tk.Canvas(steps_wrap, bg=SURFACE, highlightthickness=0)
        scroll = ttk.Scrollbar(steps_wrap, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=SURFACE)
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        for sid, _title in STEPS:
            row = tk.Frame(inner, bg=SURFACE)
            row.pack(fill="x", padx=10, pady=2)
            icon = tk.Label(row, text=ICONS["pending"], bg=SURFACE,
                            fg=ICON_COLORS["pending"], width=2,
                            font=(self.family, 13, "bold"))
            icon.pack(side="left")
            name = tk.Label(row, text=self.step_titles[sid], bg=SURFACE, fg=TEXT,
                            font=(self.family, 10, "bold"), anchor="w", width=26)
            name.pack(side="left")
            detail = tk.Label(row, text="", bg=SURFACE, fg=MUTED,
                              font=(self.family, 9), anchor="w", justify="left",
                              wraplength=380)
            detail.pack(side="left", fill="x", expand=True)
            self.rows[sid] = (icon, detail)

        # Детальний журнал
        tk.Label(self.root, text="Журнал:", bg=BG, fg=TEXT,
                 font=(self.family, 10, "bold")).pack(anchor="w", padx=16)
        log_wrap = tk.Frame(self.root, bg=SURFACE, highlightthickness=1,
                            highlightbackground=BORDER)
        log_wrap.pack(fill="both", expand=False, padx=16, pady=(2, 8))
        self.log_text = tk.Text(log_wrap, height=9, bg=SURFACE, fg=TEXT,
                                relief="flat", wrap="word", font=(self.family, 9))
        log_scroll = ttk.Scrollbar(log_wrap, orient="vertical",
                                   command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        log_scroll.pack(side="right", fill="y")

        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill="x", padx=16, pady=(0, 14))
        self.progress = ttk.Progressbar(bottom, mode="determinate",
                                        maximum=self.total)
        self.progress.pack(fill="x", pady=(0, 8))
        btns = tk.Frame(bottom, bg=BG)
        btns.pack(fill="x")
        self.start_btn = ttk.Button(btns, text="Перевірити та налаштувати",
                                    command=self.start)
        self.start_btn.pack(side="left")
        ttk.Button(btns, text="Закрити", command=self.root.destroy).pack(side="right")

        self.root.after(300, self.start)      # автозапуск майстра

    # ---------- потік виконання ----------
    def start(self):
        if self.running:
            return
        self.running = True
        self.start_btn.configure(state="disabled")
        threading.Thread(target=self.worker, daemon=True).start()
        self.poll()

    def poll(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def handle(self, msg):
        kind = msg[0]
        if kind == "step":
            status = msg[2]
            self.set_step(*msg[1:])
            if status in TERMINAL:
                self.completed += 1
                self.progress.configure(value=self.completed)
        elif kind == "log":
            self._append_log(msg[1])
        elif kind == "password":
            self.show_password_dialog(msg[1], msg[2], msg[3])
        elif kind == "done":
            self.on_done(msg[1])

    def set_step(self, sid, status, detail=""):
        icon, lbl = self.rows[sid]
        icon.configure(text=ICONS.get(status, "•"),
                       fg=ICON_COLORS.get(status, MUTED))
        if detail:
            color = {"ok": GREEN, "fail": RED, "warn": ORANGE}.get(status, MUTED)
            lbl.configure(text=detail, fg=color)

    def step(self, sid, status, detail=""):
        """Надсилає оновлення кроку з робочого потоку до GUI (через чергу)."""
        self.q.put(("step", sid, status, detail))

    def log(self, text):
        """Надсилає рядок журналу з робочого потоку до GUI (через чергу)."""
        self.q.put(("log", text))

    def _append_log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------- запит пароля root ----------
    def ask_password(self, reason):
        holder = {"value": None}
        event = threading.Event()
        self.q.put(("password", reason, holder, event))
        event.wait()
        return holder["value"]

    def show_password_dialog(self, reason, holder, event):
        win = tk.Toplevel(self.root)
        win.title("Права адміністратора")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self.root)
        try:
            win.grab_set()
        except tk.TclError:
            pass
        body = tk.Frame(win, bg=BG, padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="Потрібні права адміністратора (root)", bg=BG, fg=TEXT,
                 font=(self.family, 12, "bold")).pack(anchor="w")
        tk.Label(body, text=reason, bg=BG, fg=MUTED, wraplength=420,
                 justify="left", font=(self.family, 10)).pack(anchor="w", pady=(6, 10))
        tk.Label(body, text="Пароль:", bg=BG, fg=TEXT,
                 font=(self.family, 10)).pack(anchor="w")
        entry = ttk.Entry(body, show="*", width=34)
        entry.pack(fill="x", pady=(2, 12))
        entry.focus_set()

        def confirm():
            holder["value"] = entry.get()
            event.set()
            win.destroy()

        def cancel():
            holder["value"] = None
            event.set()
            win.destroy()

        row = tk.Frame(body, bg=BG)
        row.pack(fill="x")
        ttk.Button(row, text="Підтвердити", command=confirm).pack(side="right")
        ttk.Button(row, text="Скасувати", command=cancel).pack(side="right", padx=(0, 8))
        win.bind("<Return>", lambda _e: confirm())
        win.bind("<Escape>", lambda _e: cancel())
        win.protocol("WM_DELETE_WINDOW", cancel)
        win.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # ---------- запуск зовнішніх команд ----------
    def run_plain(self, cmd, input=None, timeout=None):
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  input=input, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log(f"Помилка виконання {cmd[0]}: {exc}")
            return None

    def is_root(self):
        if platform.system() == "Windows":
            return common._fw_is_admin_windows()      # noqa: SLF001
        return getattr(os, "geteuid", lambda: 1)() == 0

    def run_privileged(self, script, reason):
        """Виконує shell-команду від root. На Linux за потреби показує вікно із
        запитом пароля (sudo -S); на Windows — запит UAC."""
        system = platform.system()
        if self.is_root():
            if system == "Windows":
                return self.run_plain(["cmd.exe", "/c", script])
            return self.run_plain(["sh", "-c", script])
        if system == "Windows":
            import ctypes
            self.log(reason)
            class _Win:
                returncode = 0
                stdout = ""
                stderr = ""
            try:
                code = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", "cmd.exe", f"/c {script}", None, 0)
            except Exception as exc:
                self.log(f"Запит UAC не вдався: {exc}")
                return None
            _Win.returncode = 0 if code > 32 else 1
            return _Win()
        sudo = common._which("sudo")
        if sudo:
            last = None
            for _attempt in range(3):
                password = self.ask_password(reason)
                if password is None:
                    self.log("Запит пароля скасовано користувачем.")
                    return None
                result = self.run_plain([sudo, "-S", "-p", "", "sh", "-c", script],
                                        input=password + "\n")
                if result is None:
                    return None
                if result.returncode == 0:
                    return result
                err = (result.stderr or "").lower()
                last = result
                if "incorrect password" in err or "sorry" in err or "try again" in err:
                    continue
                return result
            return last
        pkexec = common._which("pkexec")
        if pkexec:
            self.log(reason)
            return self.run_plain([pkexec, "sh", "-c", script])
        self.log("Не знайдено sudo або pkexec — підвищення прав неможливе.")
        return None

    # ---------- встановлення залежностей ----------
    def install_python_pkg(self, package):
        base = [sys.executable, "-m", "pip", "install"]
        for extra in ([], ["--user"], ["--break-system-packages"]):
            result = self.run_plain(base + extra + [package], timeout=600)
            if result is not None and result.returncode == 0:
                return True
        for extra in (["--break-system-packages"], ["--user", "--break-system-packages"]):
            command = " ".join(shlex.quote(x) for x in base + extra + [package])
            result = self.run_privileged(
                command, "Для встановлення Python-пакета потрібні права root.")
            if result is not None and result.returncode == 0:
                return True
        return False

    def install(self, apt_pkgs, pip_pkgs):
        if common.is_frozen():
            self.log("Застосунок зібрано — встановлення пакетів не потрібне.")
            return False
        ok = False
        if platform.system() == "Linux" and apt_pkgs and common._which("apt-get"):
            command = "apt-get install -y " + " ".join(apt_pkgs)
            self.log(f"apt-get: {' '.join(apt_pkgs)}")
            result = self.run_privileged(
                command, "Для встановлення системних пакетів потрібні права root.")
            if result is None or result.returncode != 0:
                self.run_privileged("apt-get update",
                                    "Оновлення списку пакетів перед встановленням.")
                result = self.run_privileged(
                    command, "Повторна спроба встановлення системних пакетів.")
            ok = result is not None and result.returncode == 0
        if not ok and pip_pkgs:
            for package in pip_pkgs:
                if self.install_python_pkg(package):
                    ok = True
        return ok

    # ---------- окремі кроки ----------
    def setup_firewall(self):
        results = [self._fw_port(self.tcp_port, "tcp"),
                   self._fw_port(self.udp_port, "udp")]
        if all(results):
            self.step("firewall", "ok",
                      f"порти {self.tcp_port}/tcp і {self.udp_port}/udp дозволено")
            return True
        if any(results):
            self.step("firewall", "warn", "частину портів відкрито — перевірте журнал")
        else:
            self.step("firewall", "warn", "не вдалося відкрити порти — див. журнал")
        return False

    def _fw_port(self, port, proto):
        system = platform.system()
        if system == "Windows":
            try:
                result = common.ensure_firewall_port(port, proto=proto,
                                                     on_elevate=self.log)
            except Exception as exc:                  # noqa: BLE001
                self.log(f"Фаєрвол: {exc}")
                return False
            self.log(result.get("message", ""))
            return result.get("status") in ("ok", "added")
        if system != "Linux":
            return False
        mode, command = common._fw_linux_plan(port, proto)     # noqa: SLF001
        if mode is None:
            self.log("Не знайдено утиліт фаєрвола (ufw, firewall-cmd, iptables).")
            return False
        state = common._fw_linux_rule_state(port, mode, proto)  # noqa: SLF001
        if state is True:
            self.log(f"Вхідний {proto.upper()}-порт {port} уже дозволено ({mode}).")
            return True
        result = self.run_privileged(
            command, f"Відкриття вхідного {proto.upper()}-порту {port} у фаєрволі.")
        if result is not None and result.returncode == 0:
            common._fw_record_marker(mode, f"{port}/{proto}")   # noqa: SLF001
            self.log(f"Вхідний {proto.upper()}-порт {port} дозволено ({mode}).")
            return True
        self.log(f"Не вдалося дозволити {proto.upper()}-порт {port}.")
        if result is not None and (result.stderr or "").strip():
            self.log((result.stderr or "").strip())
        return False

    def setup_timesync(self):
        system = platform.system()
        if system == "Windows":
            try:
                result = common.ensure_time_sync(on_elevate=self.log)
            except Exception as exc:                  # noqa: BLE001
                self.log(f"Синхронізація часу: {exc}")
                self.step("timesync", "warn", "не вдалося налаштувати")
                return False
            status = result.get("status")
            self.log(result.get("message", ""))
            self.step("timesync", "ok" if status in ("ok", "added") else "warn",
                      result.get("message", ""))
            return status in ("ok", "added")
        if system != "Linux":
            self.step("timesync", "skip", "не підтримується на цій системі")
            return False
        try:
            if common._ts_linux_synced() is True:     # noqa: SLF001
                self.step("timesync", "ok", "синхронізація часу вже активна")
                return True
            mode, command = common._ts_linux_plan()   # noqa: SLF001
            if mode is None:
                self.step("timesync", "warn", "не знайдено служби синхронізації часу")
                return False
            result = self.run_privileged(
                command, "Увімкнення автоматичної синхронізації часу (NTP).")
            if result is not None and result.returncode == 0:
                common._ts_record_marker(mode)        # noqa: SLF001
                self.step("timesync", "ok", f"синхронізацію часу увімкнено ({mode})")
                return True
            self.step("timesync", "warn", "не вдалося увімкнути синхронізацію часу")
            return False
        except Exception as exc:                      # noqa: BLE001
            self.step("timesync", "warn", f"помилка: {exc}")
            return False

    def setup_token(self):
        token = common.load_token()
        if token and len(token) >= common.MIN_TOKEN_LEN:
            self.step("token", "ok",
                      f"токен уже існує ({common.token_file_path()})")
            return True
        token = common.generate_token()
        try:
            path = common.save_token(token)
        except OSError as exc:
            self.step("token", "fail", f"не вдалося зберегти токен: {exc}")
            return False
        self.log(f"Створено токен доступу: {token}")
        self.log(f"Збережено у файлі {path} — скопіюйте його на комп'ютери учнів.")
        self.step("token", "ok", "новий токен створено")
        return True

    def setup_config(self):
        if CONFIG_FILE.exists():
            self.step("config", "ok", f"{CONFIG_FILE.name} уже існує")
            return True
        data = {}
        try:
            for target in common.local_broadcast_targets():
                if target != "255.255.255.255":
                    data["broadcast_address"] = target
                    break
        except Exception:                             # noqa: BLE001
            pass
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.step("config", "warn", f"не вдалося створити файл: {exc}")
            return False
        detail = f"створено {CONFIG_FILE.name}"
        if data.get("broadcast_address"):
            detail += f" (broadcast {data['broadcast_address']})"
        self.step("config", "ok", detail)
        return True

    # ---------- головний робітник ----------
    def worker(self):
        critical_ok = True

        version = sys.version_info
        if version >= (3, 10):
            self.step("python", "ok",
                      f"Python {version.major}.{version.minor}.{version.micro}")
        else:
            self.step("python", "fail",
                      f"Python {version.major}.{version.minor} — потрібно 3.10+")
            critical_ok = False

        for sid, module, apt_pkgs, pip_pkgs in LIBS:
            self.step(sid, "running", "перевірка…")
            if module_available(module):
                self.step(sid, "ok", f"{module} доступний")
                continue
            if common.is_frozen():
                # У зібраному застосунку бібліотеки вбудовані у .exe —
                # встановлювати нічого не можна й не потрібно.
                self.step(sid, "skip", f"{module} не вбудовано у застосунок")
                continue
            self.log(f"Модуль {module} відсутній — намагаюся встановити…")
            self.install(apt_pkgs, pip_pkgs)
            if module_available(module):
                self.step(sid, "ok", f"{module} встановлено")
            else:
                self.step(sid, "fail", f"не вдалося встановити {module}")
                if sid in CRITICAL:
                    critical_ok = False

        self.step("network", "running", "перевірка…")
        ok, detail, _ips = test_broadcast()
        if ok:
            self.step("network", "ok", detail)
        else:
            self.step("network", "fail", detail)
            critical_ok = False

        for sid, port, proto in (("port_tcp", self.tcp_port, "tcp"),
                                 ("port_udp", self.udp_port, "udp")):
            self.step(sid, "running", "перевірка…")
            free, why = port_free(port, proto)
            if free:
                self.step(sid, "ok", f"{proto.upper()} {port} вільний")
            else:
                self.step(sid, "warn",
                          f"{proto.upper()} {port} зайнятий ({why})")

        self.step("firewall", "running", "налаштування…")
        self.setup_firewall()

        self.step("timesync", "running", "налаштування…")
        self.setup_timesync()

        self.step("token", "running", "перевірка…")
        if not self.setup_token():
            critical_ok = False

        self.step("config", "running", "перевірка…")
        self.setup_config()

        if critical_ok:
            try:
                common.mark_setup_complete({"critical_ok": True})
            except Exception:                         # noqa: BLE001
                pass
        self.q.put(("done", critical_ok))

    # ---------- завершення ----------
    def on_done(self, ok):
        if self.no_launch:
            if ok:
                messagebox.showinfo("Налаштування завершено",
                                    "Середовище успішно налаштовано.",
                                    parent=self.root)
            else:
                messagebox.showwarning("Налаштування з попередженнями",
                                       "Не всі перевірки пройдено — "
                                       "перегляньте журнал.", parent=self.root)
            self.root.destroy()
            return
        if ok:
            title = "Налаштування завершено"
            text = ("Середовище успішно налаштовано.\n\n"
                    "Запустити програму вчителя зараз?")
        else:
            title = "Налаштування з попередженнями"
            text = ("Не всі перевірки пройдено — перегляньте журнал вище.\n\n"
                    "Запустити програму вчителя попри це?")
        if messagebox.askyesno(title, text, parent=self.root):
            self.launch_teacher()
        self.root.destroy()

    def launch_teacher(self):
        try:
            subprocess.Popen(common.program_command("teacher"),
                             cwd=str(BASE_DIR))
        except OSError as exc:
            messagebox.showerror("Помилка",
                                 f"Не вдалося запустити програму вчителя:\n{exc}",
                                 parent=self.root)


def console_main():
    """Запасний варіант, якщо tkinter відсутній (без графічного інтерфейсу)."""
    print("Увага: модуль tkinter не знайдено — графічний майстер недоступний.")
    if platform.system() == "Linux" and common._which("apt-get"):   # noqa: SLF001
        print("Спроба встановити python3-tk та python3-pil.imagetk…")
        elev = "sudo" if common._which("sudo") else ("pkexec" if common._which("pkexec") else None)
        if elev:
            subprocess.run([elev, "apt-get", "install", "-y",
                            "python3-tk", "python3-pil.imagetk"])
            print("Готово. Перезапустіть skkk_setup.py.")
            return 0
    print("Встановіть вручну:  sudo apt install python3-tk python3-pil.imagetk")
    return 1


def main():
    if not HAVE_TK:
        return console_main()
    root = tk.Tk()
    _icon = common.app_icon_path()
    if _icon is not None:
        try:
            root._app_icon = tk.PhotoImage(file=str(_icon))
            root.iconphoto(True, root._app_icon)
        except Exception:
            pass
    app = SetupApp(root)
    # --no-launch використовує teacher.py: після налаштування він стартує сам,
    # тому пропонувати запуск teacher.py повторно не потрібно.
    app.no_launch = "--no-launch" in sys.argv[1:]
    app.build()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

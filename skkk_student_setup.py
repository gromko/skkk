#!/usr/bin/env python3
"""SKKK — меню налаштування учнівського комп'ютера.

Графічне меню з п'ятьма діями для комп'ютера учня:
  * «Налаштування системи» — перевіряє/встановлює потрібні компоненти
    (tkinter, Pillow, ImageTk, mss), перевіряє локальну мережу, вмикає
    синхронізацію часу, відкриває вхідний UDP-порт у фаєрволі, дозволяє
    вимкнення по мережі (sudoers), імпортує токен доступу з комп'ютера
    вчителя та створює файл налаштувань;
  * «Додати в Автозавантаження» — додає агента учня (student_agent) та
    привілейований netguard до автозавантаження при вході в систему;
  * «Видалити з Автозавантаження» — прибирає обидва з автозавантаження;
  * «Запустити систему» — запускає netguard з правами адміністратора та
    агента учня, якщо вони ще не працюють;
  * «Перевірити параметри» — показує стан токена, налаштувань, мережі,
    синхронізації часу, фаєрвола, автозавантаження та процесів.

student_agent.py викликає це меню, якщо учнівський комп'ютер ще не
налаштований.
"""
import json
import os
import platform
import queue
import re
import shlex
import socket
import subprocess
import sys
import threading
from pathlib import Path

import common  # легкий модуль без залежностей від mss/Pillow

BASE_DIR = common.app_dir()
CONFIG_FILE = BASE_DIR / "classroom_config.json"
AGENT_NAME = "student_agent"
NETGUARD_NAME = "netguard"

try:
    import tkinter as tk
    from tkinter import filedialog
    from tkinter import font as tkfont
    from tkinter import messagebox, ttk
    HAVE_TK = True
except Exception:                      # tkinter відсутній
    HAVE_TK = False


# --- Кольори інтерфейсу ---
BG = "#f4f6fa"
SURFACE = "#ffffff"
BORDER = "#d8dee8"
TEXT = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#2563eb"
GREEN = "#059669"
RED = "#dc2626"
ORANGE = "#d97706"

# Залежності: (id, модуль для import, пакети apt, пакети pip)
LIBS = [
    ("tkinter", "tkinter", ["python3-tk"], []),
    ("pillow", "PIL", ["python3-pil"], ["Pillow"]),
    ("imagetk", "PIL.ImageTk", ["python3-pil.imagetk"], []),
    ("mss", "mss", ["python3-mss"], ["mss"]),
]

# Без чого агент не працює повноцінно. tkinter/ImageTk лише вмикають показ
# демонстрації та блокування — про їх відсутність попереджаємо, але не робимо
# критичними.
CRITICAL = {"pillow", "mss", "token"}

AGENT_AUTOSTART_BASENAME = "student_agent.desktop"
AGENT_WIN_BAT = "skkk_student_agent.bat"
NETGUARD_TASK_NAME = "ClassroomNetguard"
NETGUARD_DESKTOP = "netguard.desktop"
NETGUARD_CRON_LOG = "/var/log/netguard.log"

# Файл sudoers, який дозволяє користувачу учня вимикати комп'ютер без пароля,
# щоб агент учня виконав команду вчителя «вимкнути» через sudo.
SUDOERS_PATH = common.SUDOERS_POWER_FILE
SUDOERS_TMP = "/etc/sudoers.d/.classroom-poweroff.tmp"
# Застаріле polkit-правило (раніші версії); прибираємо під час налаштування.
POLKIT_RULES_PATH = Path("/etc/polkit-1/rules.d/49-classroom-poweroff.rules")

STATE_DIR = Path.home() / ".config" / "classroom_screen_monitor"
NETGUARD_AUTOSTART_MARKER = STATE_DIR / "netguard_autostart.json"


def module_available(name):
    """Чи можна імпортувати модуль (у зібраному застосунку — з вбудованих)."""
    return common.module_available(name)


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


def windows_pythonw():
    """Шлях до pythonw.exe, щоб автозапуск не відкривав вікно консолі."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def _ps_quote(value):
    """Обгортає рядок у лапки PowerShell (одинарні лапки подвоюються)."""
    return "'" + str(value).replace("'", "''") + "'"


def _ps_encoded(script):
    """Кодує PowerShell-скрипт у base64 (UTF-16LE) для -EncodedCommand.

    Так уникаємо проблем із вкладеними лапками під час передавання команди
    через cmd.exe із підвищенням прав (UAC)."""
    import base64
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


class SetupApp:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.buttons = []
        self.family = tkfont.nametofont("TkDefaultFont").actual("family")
        cfg = common.load_config()
        self.tcp_port = int(cfg["tcp_port"])
        self.udp_port = int(cfg["discovery_port"])

    # ---------- інтерфейс ----------
    def build(self):
        self.root.title("SKKK — налаштування учнівського комп'ютера")
        self.root.configure(bg=BG)
        self.root.geometry("780x620")
        self.root.minsize(660, 520)

        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(header, text="Налаштування учнівського комп'ютера", bg=BG, fg=TEXT,
                 font=(self.family, 16, "bold")).pack(anchor="w")
        tk.Label(header, text="Виберіть дію. Перебіг виконання видно в журналі "
                              "внизу вікна.",
                 bg=BG, fg=MUTED, font=(self.family, 10)).pack(anchor="w")

        menu = tk.Frame(self.root, bg=BG)
        menu.pack(fill="x", padx=16, pady=(4, 8))
        self._menu_button(menu, "Налаштування системи",
                          "автоматичне налаштування: компоненти, мережа, "
                          "файрвол, токен",
                          self.on_setup)
        self._menu_button(menu, "Додати в Автозавантаження",
                          "netguard і агент учня запускатимуться при вході в систему",
                          self.on_autostart_add)
        self._menu_button(menu, "Видалити з Автозавантаження",
                          "прибрати netguard і агента учня з автозавантаження",
                          self.on_autostart_remove)
        self._menu_button(menu, "Запустити систему",
                          "netguard з правами адміністратора та агент учня",
                          self.on_launch)
        self._menu_button(menu, "Перевірити параметри",
                          "стан токена, налаштувань, файрвола, автозавантаження",
                          self.on_check)

        tk.Label(self.root, text="Журнал:", bg=BG, fg=TEXT,
                 font=(self.family, 10, "bold")).pack(anchor="w", padx=16)
        log_wrap = tk.Frame(self.root, bg=SURFACE, highlightthickness=1,
                            highlightbackground=BORDER)
        log_wrap.pack(fill="both", expand=True, padx=16, pady=(2, 8))
        self.log_text = tk.Text(log_wrap, height=12, bg=SURFACE, fg=TEXT,
                                relief="flat", wrap="word", font=(self.family, 9))
        log_scroll = ttk.Scrollbar(log_wrap, orient="vertical",
                                   command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        log_scroll.pack(side="right", fill="y")

        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill="x", padx=16, pady=(0, 14))
        ttk.Button(bottom, text="Закрити", command=self.root.destroy).pack(side="right")

        self.root.after(200, self.poll)

    def _menu_button(self, parent, text, desc, command):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=3)
        button = ttk.Button(row, text=text, command=command, width=30)
        button.pack(side="left")
        tk.Label(row, text=desc, bg=BG, fg=MUTED, font=(self.family, 9),
                 anchor="w", justify="left", wraplength=380).pack(
                     side="left", padx=(12, 0))
        self.buttons.append(button)

    # ---------- черга повідомлень ----------
    def poll(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def handle(self, msg):
        kind = msg[0]
        if kind == "log":
            self._append_log(msg[1])
        elif kind == "ask":
            _, ask_kind, kwargs, holder, event = msg
            self._handle_ask(ask_kind, kwargs, holder, event)
        elif kind == "done":
            _, ok, title, message = msg
            self.busy = False
            self._set_buttons("normal")
            if ok:
                messagebox.showinfo(title, message, parent=self.root)
            else:
                messagebox.showwarning(title, message, parent=self.root)

    # ---------- журнал ----------
    def log(self, text):
        """Надсилає рядок журналу з робочого потоку до GUI (через чергу)."""
        self.q.put(("log", text))

    def log_ok(self, text):
        self.log("\u2714 " + text)

    def log_fail(self, text):
        self.log("\u2718 " + text)

    def log_warn(self, text):
        self.log("! " + text)

    def _append_log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------- запуск завдань як кнопок ----------
    def _set_buttons(self, state):
        for button in self.buttons:
            button.configure(state=state)

    def _start_task(self, func, title):
        if self.busy:
            return
        self.busy = True
        self._set_buttons("disabled")
        self.log("")
        self.log(f"=== {title} ===")
        threading.Thread(target=self._run_task, args=(func, title),
                         daemon=True).start()

    def _run_task(self, func, title):
        try:
            ok, message = func()
        except Exception as exc:                      # noqa: BLE001
            ok, message = False, f"Помилка: {exc}"
        self.q.put(("done", bool(ok), title, message))

    def on_setup(self):
        self._start_task(self.task_setup, "Налаштування системи")

    def on_autostart_add(self):
        self._start_task(self.task_autostart_add, "Додати в Автозавантаження")

    def on_autostart_remove(self):
        self._start_task(self.task_autostart_remove, "Видалити з Автозавантаження")

    def on_launch(self):
        self._start_task(self.task_launch, "Запустити систему")

    def on_check(self):
        self._start_task(self.task_check, "Перевірити параметри")

    # ---------- запити до користувача (з робочого потоку) ----------
    def _ask(self, kind, **kwargs):
        holder = {"value": None}
        event = threading.Event()
        self.q.put(("ask", kind, kwargs, holder, event))
        event.wait()
        return holder["value"]

    def ask_password(self, reason):
        return self._ask("password", reason=reason)

    def ask_yesno(self, title, question):
        return bool(self._ask("yesno", title=title, question=question))

    def ask_open_file(self, title, filetypes=None, initialdir=None):
        return self._ask("openfile", title=title, filetypes=filetypes,
                         initialdir=initialdir)

    def _handle_ask(self, kind, kwargs, holder, event):
        if kind == "password":
            self.show_password_dialog(kwargs["reason"], holder, event)
        elif kind == "yesno":
            holder["value"] = messagebox.askyesno(
                kwargs["title"], kwargs["question"], parent=self.root)
            event.set()
        elif kind == "openfile":
            path = filedialog.askopenfilename(
                parent=self.root, title=kwargs.get("title", ""),
                filetypes=kwargs.get("filetypes") or [("Усі файли", "*.*")],
                initialdir=kwargs.get("initialdir") or str(Path.home()))
            holder["value"] = path or None
            event.set()
        else:
            event.set()

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
                                  input=input, timeout=timeout,
                                  env=common.clean_env())
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
            except Exception as exc:                  # noqa: BLE001
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

    def _log_result(self, result):
        if result is None:
            return
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if out:
            self.log(out)
        if err:
            self.log(err)

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

    # ---------- окремі кроки налаштування ----------
    def setup_timesync(self):
        system = platform.system()
        if system == "Windows":
            try:
                result = common.ensure_time_sync(on_elevate=self.log)
            except Exception as exc:                  # noqa: BLE001
                self.log(f"Синхронізація часу: {exc}")
                self.log_warn("Синхронізація часу: не вдалося налаштувати")
                return False
            status = result.get("status")
            if result.get("message"):
                self.log(result["message"])
            if status in ("ok", "added"):
                self.log_ok("Синхронізацію часу увімкнено")
                return True
            self.log_warn(f"Синхронізація часу: {result.get('message', 'невідомо')}")
            return False
        if system != "Linux":
            self.log("Синхронізація часу не підтримується на цій системі")
            return False
        try:
            if common._ts_linux_synced() is True:     # noqa: SLF001
                self.log_ok("Синхронізація часу вже активна")
                return True
            mode, command = common._ts_linux_plan()   # noqa: SLF001
            if mode is None:
                self.log_warn("Не знайдено служби синхронізації часу")
                return False
            result = self.run_privileged(
                command, "Увімкнення автоматичної синхронізації часу (NTP).")
            if result is not None and result.returncode == 0:
                common._ts_record_marker(mode)        # noqa: SLF001
                self.log_ok(f"Синхронізацію часу увімкнено ({mode})")
                return True
            self.log_warn("Не вдалося увімкнути синхронізацію часу")
            return False
        except Exception as exc:                      # noqa: BLE001
            self.log_warn(f"Синхронізація часу: помилка: {exc}")
            return False

    def setup_firewall(self):
        """Відкриває вхідний UDP-порт виявлення, щоб учень отримував оголошення
        та відповіді вчителя. Помилки не критичні для роботи агента."""
        if self._fw_port(self.udp_port, "udp"):
            self.log_ok(f"Вхідний UDP-порт {self.udp_port} дозволено у фаєрволі")
            return True
        self.log_warn(f"Не вдалося відкрити UDP-порт {self.udp_port} — див. журнал")
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
            if result.get("message"):
                self.log(result["message"])
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

    POWER_MARKER = STATE_DIR / "power_permission.json"

    @classmethod
    def power_permission_present(cls):
        """Чи встановлено дозвіл на вимкнення по мережі (sudoers)."""
        try:
            return "poweroff" in SUDOERS_PATH.read_text(encoding="utf-8")
        except OSError:
            pass
        # Звичайний користувач не може прочитати /etc/sudoers.d — покладаємось
        # на маркер, записаний після успішного встановлення.
        return cls.POWER_MARKER.exists()

    @classmethod
    def _write_power_marker(cls, installed):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            if installed:
                cls.POWER_MARKER.write_text(
                    json.dumps({"mechanism": "sudoers"}), encoding="utf-8")
            else:
                cls.POWER_MARKER.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _sudoers_content(user, paths):
        line = f"{user} ALL=(root) NOPASSWD: {', '.join(paths)}"
        return ("# SKKK: безпарольне вимкнення комп'ютера учня за командою "
                "вчителя\n" + line + "\n")

    def setup_power_permission(self):
        """Дозволяє користувачу учня вимикати комп'ютер без пароля, щоб агент
        (не root) виконав команду вчителя «вимкнути» через sudo. Створює файл
        /etc/sudoers.d/classroom-poweroff. Помилки не критичні для агента."""
        if platform.system() != "Linux":
            return False
        if self.power_permission_present():
            self.log_ok("Дозвіл на вимкнення по мережі вже встановлено")
            return True
        user = common.invoking_user()
        if not user or not re.fullmatch(r"[A-Za-z0-9_.@-]+", user):
            self.log_warn("Не вдалося визначити користувача для дозволу вимкнення.")
            return False
        paths = common.power_bin_paths()
        if not paths:
            self.log_warn("Не знайдено утиліт poweroff/shutdown — вимкнення "
                          "по мережі може не працювати.")
            return False
        if common._which("sudo") is None:  # noqa: SLF001
            self.log_warn("Утиліту sudo не знайдено — агент не зможе скористатися "
                          "цим дозволом (встановіть sudo).")
        content = self._sudoers_content(user, paths)
        script = (
            "set -e; "
            "install -d -m 0750 /etc/sudoers.d; "
            f"tmp={shlex.quote(SUDOERS_TMP)}; "
            "cat > \"$tmp\" <<'SKKK_SUDOERS_EOF'\n" + content +
            "SKKK_SUDOERS_EOF\n"
            "chmod 0440 \"$tmp\"; "
            "if command -v visudo >/dev/null 2>&1; then "
            "visudo -cf \"$tmp\" || { rm -f \"$tmp\"; exit 1; }; fi; "
            f"install -m 0440 -o root -g root \"$tmp\" {shlex.quote(str(SUDOERS_PATH))}; "
            "rm -f \"$tmp\"; "
            f"rm -f {shlex.quote(str(POLKIT_RULES_PATH))}"
        )
        result = self.run_privileged(
            script, "Для дозволу вимкнення по мережі потрібні права root.")
        if result is not None and result.returncode == 0:
            self._write_power_marker(True)
            self.log_ok(f"Дозвіл на вимкнення встановлено для «{user}» (sudoers)")
            return True
        if result is not None and (result.stderr or "").strip():
            self.log((result.stderr or "").strip())
        self.log_warn("Не вдалося встановити дозвіл на вимкнення по мережі")
        return False

    def setup_token(self):
        """Токен учня має збігатися з токеном учителя, тому тут він не
        генерується, а імпортується з готового файлу."""
        token = common.load_token()
        if token and len(token) >= common.MIN_TOKEN_LEN:
            self.log_ok(f"Токен знайдено ({common.token_file_path()})")
            return True
        self.log("Токен доступу не знайдено або він закороткий.")
        self.log(f"Очікується файл: {common.token_file_path()}")
        self.log("Токен створюється на комп'ютері вчителя і копіюється сюди.")
        proceed = self.ask_yesno(
            "Токен доступу",
            "Токен доступу не знайдено.\n\n"
            "Він має бути таким самим, як на комп'ютері вчителя.\n"
            "Вибрати файл «classroom_token.txt», скопійований з комп'ютера "
            "вчителя?")
        if not proceed:
            self.log_fail("Токен не знайдено — скопіюйте classroom_token.txt "
                          "з комп'ютера вчителя")
            return False
        path = self.ask_open_file(
            "Виберіть файл classroom_token.txt з комп'ютера вчителя",
            filetypes=[("Текст", "*.txt"), ("Усі файли", "*.*")],
            initialdir=str(Path.home() / "Downloads"))
        if not path:
            self.log_fail("Файл токена не вибрано")
            return False
        try:
            candidate = ""
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        candidate = line.strip()
                        break
            if len(candidate) < common.MIN_TOKEN_LEN:
                self.log_fail("У вибраному файлі немає коректного токена")
                return False
            dest = common.save_token(candidate)
        except OSError as exc:
            self.log_fail(f"Не вдалося зберегти токен: {exc}")
            return False
        self.log_ok("Токен імпортовано з файлу вчителя")
        self.log(f"Токен збережено у файлі {dest}.")
        return True

    def setup_config(self):
        if CONFIG_FILE.exists():
            self.log_ok(f"{CONFIG_FILE.name} уже існує")
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
            self.log_warn(f"Не вдалося створити файл налаштувань: {exc}")
            return False
        detail = f"створено {CONFIG_FILE.name}"
        if data.get("broadcast_address"):
            detail += f" (broadcast {data['broadcast_address']})"
        self.log_ok(detail)
        return True

    # ---------- автозавантаження агента учня ----------
    def _agent_desktop_path(self):
        return Path.home() / ".config" / "autostart" / AGENT_AUTOSTART_BASENAME

    def _win_startup_dir(self):
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "Startup")

    def agent_autostart_installed(self):
        system = platform.system()
        if system == "Windows":
            return (self._win_startup_dir() / AGENT_WIN_BAT).exists()
        if system == "Linux":
            if self._agent_desktop_path().exists():
                return True
            startup = Path.home() / ".desktop-session" / "startup"
            if startup.exists():
                try:
                    return "student_agent" in startup.read_text(
                        encoding="utf-8", errors="ignore")
                except OSError:
                    return False
        return False

    def install_agent_autostart(self):
        system = platform.system()
        if system == "Windows":
            return self._install_agent_windows()
        if system == "Linux":
            return self._install_agent_linux()
        self.log("Автозавантаження агента не підтримується на цій системі.")
        return False

    def remove_agent_autostart(self):
        system = platform.system()
        removed = False
        if system == "Windows":
            bat = self._win_startup_dir() / AGENT_WIN_BAT
            if bat.exists():
                try:
                    bat.unlink()
                    removed = True
                    self.log(f"Видалено {bat}.")
                except OSError as exc:
                    self.log_fail(f"Не вдалося видалити {bat}: {exc}")
                    return False
        elif system == "Linux":
            desktop = self._agent_desktop_path()
            if desktop.exists():
                try:
                    desktop.unlink()
                    removed = True
                    self.log(f"Видалено {desktop}.")
                except OSError as exc:
                    self.log_fail(f"Не вдалося видалити {desktop}: {exc}")
                    return False
            startup = Path.home() / ".desktop-session" / "startup"
            if startup.exists():
                try:
                    lines = startup.read_text(encoding="utf-8").splitlines()
                    kept = [ln for ln in lines
                            if "student_agent" not in ln
                            and "автозапуск агента учня" not in ln]
                    if len(kept) != len(lines):
                        startup.write_text("\n".join(kept) + "\n", encoding="utf-8")
                        removed = True
                        self.log(f"Очищено {startup}.")
                except OSError as exc:
                    self.log_fail(f"Не вдалося оновити {startup}: {exc}")
                    return False
        if not removed:
            self.log("Автозавантаження агента не знайдено.")
        return True

    def _agent_command_string(self):
        """Команда запуску агента (для .desktop/startup/.bat). У зібраному
        застосунку — сусідній student_agent, для вихідного коду — python
        student_agent.py."""
        return " ".join(shlex.quote(str(part))
                        for part in common.program_command(AGENT_NAME))

    def _install_agent_linux(self):
        base = str(BASE_DIR)
        autostart = Path.home() / ".config" / "autostart"
        autostart.mkdir(parents=True, exist_ok=True)
        desktop = self._agent_desktop_path()
        cmd = self._agent_command_string()
        exec_line = ('sh -c "cd {} && nohup {} '
                     '> student_a.log 2>&1 &"').format(
                         shlex.quote(base), cmd)
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Version=1.0\n"
            "Name=Classroom Student Agent\n"
            "Name[uk]=Агент учня SKKK\n"
            "Comment=Автозапуск агента учня (student_agent)\n"
            f"Exec={exec_line}\n"
            f"Path={base}\n"
            "Icon=utilities-terminal\n"
            "Terminal=false\n"
            "StartupNotify=false\n"
            "X-GNOME-Autostart-enabled=true\n"
            "X-MATE-Autostart-enabled=true\n"
        )
        try:
            desktop.write_text(content, encoding="utf-8")
            desktop.chmod(0o755)
        except OSError as exc:
            self.log(f"Не вдалося створити {desktop}: {exc}")
            return False
        self.log(f"Автозавантаження агента: {desktop}")
        # antiX / IceWM / Fluxbox (desktop-session) не завжди читають XDG-autostart
        ds_dir = Path.home() / ".desktop-session"
        if ds_dir.is_dir():
            startup = ds_dir / "startup"
            line = ("( sleep 5; cd {} && nohup {} "
                    ">student_a.log 2>&1 & )").format(
                        shlex.quote(base), cmd)
            try:
                existing = startup.read_text(encoding="utf-8") if startup.exists() else ""
                if "student_agent" not in existing:
                    with open(startup, "a", encoding="utf-8") as handle:
                        handle.write("\n# SKKK — автозапуск агента учня\n")
                        handle.write(line + "\n")
                    self.log(f"Автозавантаження додано до {startup} (antiX).")
            except OSError as exc:
                self.log(f"Не вдалося оновити {startup}: {exc}")
        return True

    def _install_agent_windows(self):
        try:
            startup = self._win_startup_dir()
            startup.mkdir(parents=True, exist_ok=True)
            bat = startup / AGENT_WIN_BAT
            if common.is_frozen():
                cmd = common.program_command(AGENT_NAME)
            else:
                cmd = [windows_pythonw(), str(common.program_path(AGENT_NAME))]
            args = " ".join(f'"{part}"' for part in cmd)
            content = ("@echo off\r\n"
                       f'cd /d "{BASE_DIR}"\r\n'
                       f'start "" {args}\r\n')
            bat.write_text(content, encoding="utf-8")
        except OSError as exc:
            self.log(f"Не вдалося створити автозавантаження: {exc}")
            return False
        self.log(f"Автозавантаження агента: {bat}")
        return True

    # ---------- автозавантаження netguard ----------
    def netguard_autostart_installed(self):
        if NETGUARD_AUTOSTART_MARKER.exists():
            return True
        system = platform.system()
        if system == "Windows":
            result = self.run_plain(["schtasks", "/query", "/tn", NETGUARD_TASK_NAME],
                                    timeout=30)
            return result is not None and result.returncode == 0
        if system == "Linux":
            result = self.run_plain(["crontab", "-l"], timeout=20)
            if result is not None and result.returncode == 0 \
                    and "netguard" in (result.stdout or ""):
                return True
            autostart = Path.home() / ".config" / "autostart" / NETGUARD_DESKTOP
            if autostart.exists():
                return True
            try:
                root_cron = Path("/var/spool/cron/crontabs/root")
                if "netguard" in root_cron.read_text(encoding="utf-8",
                                                     errors="ignore"):
                    return True
            except OSError:
                pass
        return False

    def _record_netguard_autostart(self):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            NETGUARD_AUTOSTART_MARKER.write_text(
                json.dumps({"installed": True, "platform": platform.platform()},
                           ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _clear_netguard_marker(self):
        try:
            NETGUARD_AUTOSTART_MARKER.unlink()
        except OSError:
            pass

    def install_netguard_autostart(self):
        system = platform.system()
        if system == "Windows":
            ok = self._install_netguard_windows()
        elif system == "Linux":
            ok = self._install_netguard_linux()
        else:
            self.log("Автозавантаження netguard не підтримується на цій системі.")
            return False
        if ok:
            self._record_netguard_autostart()
        return ok

    def _install_netguard_linux(self):
        run_cmd = " ".join(shlex.quote(str(part))
                           for part in common.program_command(NETGUARD_NAME))
        line = (f"@reboot cd {shlex.quote(str(BASE_DIR))} && {run_cmd} "
                f">> {NETGUARD_CRON_LOG} 2>&1")
        script = (
            'tmp="$(mktemp)"\n'
            '{ crontab -l 2>/dev/null | grep -v netguard || true; } > "$tmp"\n'
            f"printf '%s\\n' {shlex.quote(line)} >> \"$tmp\"\n"
            'crontab "$tmp"\n'
            'rm -f "$tmp"\n'
        )
        result = self.run_privileged(
            script,
            "Для автозапуску netguard з правами root (керування фаєрволом) "
            "потрібні права адміністратора.")
        self._log_result(result)
        return result is not None and result.returncode == 0

    def _install_netguard_windows(self):
        run = common.program_command(NETGUARD_NAME)
        exe = str(run[0])
        args = " ".join(f'"{part}"' for part in run[1:])
        ps = (
            f"$a = New-ScheduledTaskAction -Execute {_ps_quote(exe)} "
            f"-Argument {_ps_quote(args)} -WorkingDirectory {_ps_quote(str(BASE_DIR))}; "
            "$t = New-ScheduledTaskTrigger -AtLogOn; "
            "$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' "
            "-LogonType ServiceAccount -RunLevel Highest; "
            "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -StartWhenAvailable "
            "-ExecutionTimeLimit ([TimeSpan]::Zero); "
            f"Unregister-ScheduledTask -TaskName {_ps_quote(NETGUARD_TASK_NAME)} "
            "-Confirm:$false -ErrorAction SilentlyContinue; "
            f"Register-ScheduledTask -TaskName {_ps_quote(NETGUARD_TASK_NAME)} "
            "-Action $a -Trigger $t -Principal $p -Settings $s | Out-Null"
        )
        script = ("powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
                  + _ps_encoded(ps))
        result = self.run_privileged(
            script,
            "Для автозапуску netguard (заплановане завдання з правами "
            "адміністратора) потрібні права адміністратора.")
        self._log_result(result)
        return result is not None and result.returncode == 0

    def remove_netguard_autostart(self):
        system = platform.system()
        legacy = Path.home() / ".config" / "autostart" / NETGUARD_DESKTOP
        if legacy.exists():
            try:
                legacy.unlink()
                self.log(f"Видалено {legacy}.")
            except OSError as exc:
                self.log_fail(f"Не вдалося видалити {legacy}: {exc}")
        if not self.netguard_autostart_installed():
            self.log("Автозавантаження netguard не знайдено.")
            self._clear_netguard_marker()
            return True
        result = None
        if system == "Windows":
            ps = (f"Unregister-ScheduledTask -TaskName {_ps_quote(NETGUARD_TASK_NAME)} "
                  "-Confirm:$false -ErrorAction SilentlyContinue")
            script = ("powershell -NoProfile -ExecutionPolicy Bypass "
                      "-EncodedCommand " + _ps_encoded(ps))
            result = self.run_privileged(
                script, "Для видалення автозапуску netguard потрібні права "
                        "адміністратора.")
        elif system == "Linux":
            script = (
                'tmp="$(mktemp)"\n'
                '{ crontab -l 2>/dev/null | grep -v netguard || true; } > "$tmp"\n'
                'crontab "$tmp"\n'
                'rm -f "$tmp"\n'
            )
            result = self.run_privileged(
                script, "Для видалення автозапуску netguard потрібні права "
                        "адміністратора.")
        self._log_result(result)
        ok = result is not None and result.returncode == 0
        if ok:
            self._clear_netguard_marker()
            self.log_ok("Автозавантаження netguard видалено.")
        else:
            self.log_warn("Не вдалося видалити автозавантаження netguard.")
        return ok

    # ---------- визначення запущених процесів ----------
    def _is_running(self, name):
        system = platform.system()
        if system == "Windows":
            if common.is_frozen():
                image = name + ".exe"
                result = self.run_plain(
                    ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"], timeout=20)
                return (result is not None
                        and image.lower() in (result.stdout or "").lower())
            ps = ("Get-CimInstance Win32_Process | "
                  "Select-Object -ExpandProperty CommandLine")
            result = self.run_plain(["powershell", "-NoProfile", "-Command", ps],
                                    timeout=30)
            return result is not None and (name + ".py") in (result.stdout or "")
        pattern = name if common.is_frozen() else name + ".py"
        result = self.run_plain(["pgrep", "-f", pattern], timeout=20)
        return result is not None and result.returncode == 0

    # ---------- завдання: налаштування системи ----------
    def task_setup(self):
        critical_ok = True

        if common.is_frozen():
            self.log("Python: вбудований інтерпретатор")
        else:
            version = sys.version_info
            if version >= (3, 10):
                self.log_ok(f"Python {version.major}.{version.minor}.{version.micro}")
            else:
                self.log_fail(f"Python {version.major}.{version.minor} — потрібно 3.10+")
                critical_ok = False

        for sid, module, apt_pkgs, pip_pkgs in LIBS:
            if common.is_frozen():
                self.log(f"{module}: вбудовано у застосунок")
                continue
            if module_available(module):
                self.log_ok(f"{module}: доступний")
                continue
            self.log(f"{module}: відсутній — намагаюся встановити…")
            self.install(apt_pkgs, pip_pkgs)
            if module_available(module):
                self.log_ok(f"{module}: встановлено")
            else:
                self.log_fail(f"{module}: не вдалося встановити")
                if sid in CRITICAL:
                    critical_ok = False

        ok, detail, _ips = test_broadcast()
        if ok:
            self.log_ok(f"Локальна мережа: {detail}")
        else:
            self.log_fail(f"Локальна мережа: {detail}")
            critical_ok = False

        self.log("Синхронізація часу…")
        self.setup_timesync()

        self.log("Фаєрвол…")
        self.setup_firewall()

        self.log("Дозвіл на вимкнення по мережі…")
        self.setup_power_permission()

        self.log("Токен доступу…")
        if not self.setup_token():
            critical_ok = False

        self.log("Файл налаштувань…")
        self.setup_config()

        if critical_ok:
            try:
                common.mark_student_setup_complete({"critical_ok": True})
            except Exception:                         # noqa: BLE001
                pass
            return True, ("Учнівський комп'ютер налаштовано.\n\n"
                          "За потреби скористайтеся кнопками автозавантаження.")
        return False, ("Не всі критичні перевірки пройдено — "
                       "перегляньте журнал у вікні.")

    # ---------- завдання: автозавантаження ----------
    def task_autostart_add(self):
        results = []
        if self.agent_autostart_installed():
            self.log_ok("Автозавантаження агента вже ввімкнено.")
            results.append(True)
        elif self.install_agent_autostart():
            self.log_ok("Агента учня додано до автозавантаження.")
            results.append(True)
        else:
            self.log_fail("Не вдалося додати агента учня до автозавантаження.")
            results.append(False)

        if self.netguard_autostart_installed():
            self.log_ok("Автозавантаження netguard вже ввімкнено.")
            results.append(True)
        elif self.install_netguard_autostart():
            self.log_ok("netguard додано до автозавантаження.")
            results.append(True)
        else:
            self.log_fail("Не вдалося додати netguard до автозавантаження.")
            results.append(False)

        if all(results):
            return True, "Автозавантаження налаштовано."
        return False, ("Частину пунктів автозавантаження не вдалося "
                       "налаштувати — див. журнал.")

    def task_autostart_remove(self):
        results = []
        if self.remove_agent_autostart():
            results.append(True)
        else:
            results.append(False)
        if self.remove_netguard_autostart():
            results.append(True)
        else:
            results.append(False)
        if all(results):
            return True, "Автозавантаження очищено."
        return False, ("Частину пунктів не вдалося видалити — див. журнал.")

    # ---------- завдання: запуск системи ----------
    def task_launch(self):
        if common.program_exists(NETGUARD_NAME):
            if self._is_running(NETGUARD_NAME):
                self.log_ok("netguard уже працює.")
            elif self.launch_netguard():
                self.log_ok("netguard запущено з правами адміністратора.")
            else:
                self.log_fail("Не вдалося запустити netguard.")
        else:
            self.log_warn(f"Не знайдено застосунок {NETGUARD_NAME} поряд із цією "
                          "програмою.")

        if common.program_exists(AGENT_NAME):
            if self._is_running(AGENT_NAME):
                self.log_ok("Агент учня уже працює.")
            else:
                try:
                    subprocess.Popen(common.program_command(AGENT_NAME),
                                     cwd=str(BASE_DIR), env=common.clean_env())
                    self.log_ok("Агента учня запущено.")
                except OSError as exc:
                    self.log_fail(f"Не вдалося запустити агента учня: {exc}")
        else:
            self.log_warn(f"Не знайдено застосунок {AGENT_NAME} поряд із цією "
                          "програмою.")

        return True, "Запуск виконано — див. журнал."

    def launch_netguard(self):
        system = platform.system()
        command = common.program_command(NETGUARD_NAME)
        if system == "Windows":
            import ctypes
            exe, *args = command
            try:
                code = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", str(exe),
                    subprocess.list2cmdline([str(a) for a in args]) if args else "",
                    str(BASE_DIR), 1)
            except Exception as exc:                  # noqa: BLE001
                self.log(f"Запит UAC не вдався: {exc}")
                return False
            if code > 32:
                return True
            self.log(f"UAC відхилено або помилка запуску (код {code}).")
            return False
        if system == "Linux":
            cmd = " ".join(shlex.quote(str(part)) for part in command)
            script = (f"cd {shlex.quote(str(BASE_DIR))} && "
                      f"nohup {cmd} >> {NETGUARD_CRON_LOG} 2>&1 &")
            result = self.run_privileged(
                script,
                "Для запуску netguard з правами адміністратора потрібен пароль.")
            return result is not None and result.returncode == 0
        self.log("Запуск netguard не підтримується на цій системі.")
        return False

    # ---------- завдання: перевірка параметрів ----------
    def task_check(self):
        self.log("— Перевірка параметрів —")

        if common.is_frozen():
            self.log("Інтерпретатор: вбудований (зібраний застосунок)")
        else:
            version = sys.version_info
            self.log(f"Інтерпретатор: Python "
                     f"{version.major}.{version.minor}.{version.micro}")

        token = common.load_token()
        if token and len(token) >= common.MIN_TOKEN_LEN:
            self.log_ok(f"Токен: знайдено ({common.token_file_path()})")
        else:
            self.log_fail("Токен: не знайдено або закороткий")

        if CONFIG_FILE.exists():
            extra = ""
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if data.get("broadcast_address"):
                    extra = f", broadcast {data['broadcast_address']}"
            except (OSError, ValueError):
                pass
            self.log_ok(f"Файл налаштувань: {CONFIG_FILE.name}{extra}")
        else:
            self.log_warn("Файл налаштувань не створено (типові значення)")
        self.log(f"Порти: TCP {self.tcp_port}, UDP(виявлення) {self.udp_port}")

        ok, detail, _ips = test_broadcast()
        if ok:
            self.log_ok(f"Локальна мережа: {detail}")
        else:
            self.log_warn(f"Локальна мережа: {detail}")

        self._report_timesync()
        self._report_firewall()
        self._report_power_permission()

        if self.agent_autostart_installed():
            self.log_ok("Автозавантаження агента: увімкнено")
        else:
            self.log_warn("Автозавантаження агента: вимкнено")
        if self.netguard_autostart_installed():
            self.log_ok("Автозавантаження netguard: увімкнено")
        else:
            self.log_warn("Автозавантаження netguard: вимкнено")

        for label, name in (("netguard", NETGUARD_NAME),
                            ("агент учня", AGENT_NAME)):
            if self._is_running(name):
                self.log_ok(f"Процес {label}: працює")
            else:
                self.log(f"Процес {label}: не запущено")

        if common.student_setup_complete():
            self.log_ok("Позначка завершеного налаштування: є")
        else:
            self.log_warn("Позначка завершеного налаштування: відсутня")

        return True, "Перевірку завершено — див. журнал."

    def _report_timesync(self):
        state = None
        if platform.system() == "Linux":
            try:
                state = common._ts_linux_synced()     # noqa: SLF001
            except Exception:                         # noqa: BLE001
                state = None
        if state is True:
            self.log_ok("Синхронізація часу: активна")
        elif state is False:
            self.log_warn("Синхронізація часу: не активна")
        elif platform.system() == "Linux":
            self.log("Синхронізація часу: стан невідомий")
        else:
            self.log("Синхронізація часу: стан не перевіряється на цій системі")

    def _report_firewall(self):
        system = platform.system()
        if system == "Windows":
            try:
                state = common._fw_windows_rule_state(common.FIREWALL_RULE_NAME)
            except Exception:                         # noqa: BLE001
                state = "unsupported"
            if state == "present":
                self.log_ok("Фаєрвол: правило для вхідних портів наявне")
            elif state == "missing":
                self.log_warn("Фаєрвол: правило для вхідних портів відсутнє")
            else:
                self.log("Фаєрвол: стан невідомий")
            return
        if system != "Linux":
            return
        try:
            marker = common._fw_load_marker()         # noqa: SLF001
        except Exception:                             # noqa: BLE001
            marker = {}
        saved = [f"{port} ({mode})" for mode, ports in marker.items()
                 for port in ports]
        if saved:
            self.log_ok("Фаєрвол (збережені правила): " + ", ".join(saved))
        try:
            mode, _cmd = common._fw_linux_plan(self.udp_port, "udp")  # noqa: SLF001
        except Exception:                             # noqa: BLE001
            mode = None
        if not mode:
            self.log_warn("Фаєрвол: не знайдено утиліт (ufw, firewall-cmd, iptables)")
            return
        try:
            state = common._fw_linux_rule_state(self.udp_port, mode, "udp")  # noqa: SLF001
        except Exception:                             # noqa: BLE001
            state = None
        if state is True:
            self.log_ok(f"Фаєрвол: вхідний UDP-порт {self.udp_port} "
                        f"дозволено ({mode})")
        elif state is False:
            self.log_warn(f"Фаєрвол: вхідний UDP-порт {self.udp_port} "
                          f"не дозволено ({mode})")
        else:
            self.log("Фаєрвол: стан правила невідомий (можливо, потрібні права root)")

    def _report_power_permission(self):
        if platform.system() != "Linux":
            return
        if self.power_permission_present():
            self.log_ok("Вимкнення по мережі: дозвіл встановлено (sudoers)")
        else:
            self.log_warn("Вимкнення по мережі: дозвіл не встановлено "
                          "(запустіть «Налаштування системи»)")


def console_main():
    """Запасний варіант, якщо tkinter відсутній (без графічного інтерфейсу)."""
    print("Увага: модуль tkinter не знайдено — графічне меню недоступне.")
    if platform.system() == "Linux" and common._which("apt-get"):   # noqa: SLF001
        print("Спроба встановити python3-tk та python3-pil.imagetk…")
        elev = "sudo" if common._which("sudo") else ("pkexec" if common._which("pkexec") else None)
        if elev:
            subprocess.run([elev, "apt-get", "install", "-y",
                            "python3-tk", "python3-pil.imagetk"],
                           env=common.clean_env())
            print("Готово. Перезапустіть skkk_student_setup.py.")
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
        except Exception:                         # noqa: BLE001
            pass
    app = SetupApp(root)
    app.build()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

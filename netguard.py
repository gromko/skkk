#!/usr/bin/env python3
"""Керування доступом учнівського комп'ютера до інтернету.

Окремий привілейований клієнт (роль "net"), відокремлений від student_agent.py.
Не показує екран і не потребує вікон. Запускається з правами адміністратора
(root на Linux, адміністратор на Windows), зазвичай як служба/автозапуск, і
підключається до вчителя тим самим протоколом, але з роллю ROLE_NET.

Отримуючи MSG_NET, застосовує або знімає правила фаєрвола, що блокують вихід в
інтернет, залишаючи локальну мережу (і завжди — комп'ютер учителя) доступними,
і повідомляє вчителю MSG_NET_STATE.

Linux:   окремий ланцюжок iptables/ip6tables у OUTPUT: приватні діапазони
         (RFC1918), link-local, multicast, broadcast і IP вчителя явно
         пропускаються (RETURN), усе інше — DROP.
Windows: одне блокувальне правило фаєрвола на вихідний трафік із
         remoteip=Internet — цей вбудований діапазон Windows навмисно не
         включає локальну підмережу, тож LAN-трафік ним не зачіпається.
"""
import argparse
import ctypes
import json
import os
import platform
import select
import signal
import socket
import subprocess
import sys
import threading
import time

from common import (MSG_NET, MSG_NET_STATE, ROLE_NET, _which, clean_env,
                    client_handshake, discover_teacher, load_config, load_token,
                    recv_packet, send_packet, token_file_path)


class NetworkGuard:
    """Застосовує/знімає блокування виходу в інтернет. Потребує прав root
    (Linux) або адміністратора (Windows); без них повертає mode="unsupported".
    Виконує команди синхронно — викликається з основного циклу netguard."""

    RULE_NAME = "ClassroomAgentBlockInternet"
    CHAIN4 = "CLASSROOM_NET"
    CHAIN6 = "CLASSROOM_NET6"
    PRIVATE_V4 = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                  "169.254.0.0/16", "224.0.0.0/4", "255.255.255.255/32"]
    PRIVATE_V6 = ["::1/128", "fc00::/7", "fe80::/10", "ff00::/8"]

    def __init__(self):
        self.teacher_ip = None
        self.mode = ""

    # ---------- платформозалежна реалізація ----------
    def apply(self, want_block):
        """Повертає (ok, mode, error)."""
        system = platform.system()
        try:
            if system == "Windows":
                return self._block_windows() if want_block else self._unblock_windows()
            if system == "Linux":
                return self._block_linux() if want_block else self._unblock_linux()
            return False, "unsupported", f"Непідтримувана ОС: {system}"
        except Exception as exc:
            return False, self.mode or "unsupported", str(exc)[:200]

    def current_state(self):
        """Повертає (state, mode) для поточного стану фаєрвола: "blocked" або
        "none". Використовується для початкового звіту вчителю."""
        system = platform.system()
        if system == "Windows":
            if _which("netsh") is None:
                return "none", "unsupported"
            result = self._run_cmd(["netsh", "advfirewall", "firewall", "show", "rule",
                                    f"name={self.RULE_NAME}"])
            if result is not None and result.returncode == 0:
                return "blocked", "windows_firewall"
            return "none", "windows_firewall"
        if system == "Linux":
            if _which("iptables") is None:
                return "none", "unsupported"
            result = self._run_cmd(["iptables", "-C", "OUTPUT", "-j", self.CHAIN4])
            if result is not None and result.returncode == 0:
                return "blocked", "linux_iptables"
            return "none", "linux_iptables"
        return "none", "unsupported"

    @staticmethod
    def _run_cmd(cmd, timeout=15):
        flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
        cmd = list(cmd)
        env = None
        if os.name == "posix":
            if cmd and "/" not in cmd[0]:
                resolved = _which(cmd[0])
                if resolved:
                    cmd[0] = resolved
            env = clean_env()
            env["PATH"] = (env.get("PATH", "") + ":" +
                           "/usr/sbin:/sbin:/usr/local/sbin").strip(":")
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  creationflags=flags, env=env)
        except FileNotFoundError:
            return None
        except subprocess.SubprocessError as exc:
            class _Fail:
                returncode = 1
                stdout = ""
                stderr = str(exc)
            return _Fail()

    # ---------- Windows ----------
    @staticmethod
    def _is_admin_windows():
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def _block_windows(self):
        if not self._is_admin_windows():
            return False, "unsupported", ("Потрібні права адміністратора. Запустіть "
                                          "netguard від імені адміністратора (служба).")
        self._run_cmd(["netsh", "advfirewall", "firewall", "delete", "rule",
                      f"name={self.RULE_NAME}"])   # прибираємо дублікати від попередніх спроб
        result = self._run_cmd(["netsh", "advfirewall", "firewall", "add", "rule",
                               f"name={self.RULE_NAME}", "dir=out", "action=block",
                               "remoteip=Internet", "profile=any", "protocol=any", "enable=yes"])
        if result is None:
            return False, "unsupported", "Команду netsh не знайдено"
        if result.returncode != 0:
            return False, "windows_firewall", (result.stderr or result.stdout).strip()[:200]
        self.mode = "windows_firewall"
        return True, "windows_firewall", ""

    def _unblock_windows(self):
        if _which("netsh") is None:
            return True, "windows_firewall", ""   # netsh відсутній — блокувати теж не могли
        for _ in range(5):
            result = self._run_cmd(["netsh", "advfirewall", "firewall", "delete", "rule",
                                   f"name={self.RULE_NAME}"])
            if result is None:
                return True, "windows_firewall", ""
            if result.returncode != 0:
                break
        self.mode = "windows_firewall"
        return True, "windows_firewall", ""

    # ---------- Linux ----------
    @staticmethod
    def _geteuid():
        return getattr(os, "geteuid", lambda: 1000)()

    def _block_linux(self):
        if self._geteuid() != 0:
            return False, "unsupported", ("Потрібні права root. Запустіть netguard через sudo "
                                          "або як системну службу, щоб керувати фаєрволом.")
        if _which("iptables") is None:
            return False, "unsupported", "Команду iptables не знайдено"
        ok4 = self._apply_chain_linux("iptables", self.CHAIN4, self.PRIVATE_V4, include_teacher=True)
        if _which("ip6tables") is not None:
            self._apply_chain_linux("ip6tables", self.CHAIN6, self.PRIVATE_V6, include_teacher=False)
        if not ok4:
            return False, "linux_iptables", "Не вдалося застосувати правила iptables"
        self.mode = "linux_iptables"
        return True, "linux_iptables", ""

    def _apply_chain_linux(self, binary, chain, private_ranges, include_teacher):
        self._run_cmd([binary, "-N", chain])            # існує — помилку ігноруємо
        self._run_cmd([binary, "-F", chain])
        for net in private_ranges:
            self._run_cmd([binary, "-A", chain, "-d", net, "-j", "RETURN"])
        if include_teacher and self.teacher_ip:
            self._run_cmd([binary, "-A", chain, "-d", f"{self.teacher_ip}/32", "-j", "RETURN"])
        self._run_cmd([binary, "-A", chain, "-j", "DROP"])
        check = self._run_cmd([binary, "-C", "OUTPUT", "-j", chain])
        if check and check.returncode != 0:
            self._run_cmd([binary, "-I", "OUTPUT", "1", "-j", chain])
        check = self._run_cmd([binary, "-C", "OUTPUT", "-j", chain])
        return bool(check) and check.returncode == 0

    def _unblock_linux(self):
        for binary, chain in (("iptables", self.CHAIN4), ("ip6tables", self.CHAIN6)):
            if _which(binary) is None:
                continue
            for _ in range(5):
                result = self._run_cmd([binary, "-D", "OUTPUT", "-j", chain])
                if result is None or result.returncode != 0:
                    break
            self._run_cmd([binary, "-F", chain])
            self._run_cmd([binary, "-X", chain])
        self.mode = "linux_iptables"
        return True, "linux_iptables", ""


def send_state(sock, state, mode, error="", initial=False):
    send_packet(sock, MSG_NET_STATE + json.dumps(
        {"state": state, "mode": mode, "initial": initial, "error": error},
        ensure_ascii=False).encode("utf-8"))


def session(ip, port, token, name, guard, stop_event):
    """Одна сесія з учителем. Повертає False, якщо автентифікація не пройшла."""
    with socket.create_connection((ip, port), timeout=10) as sock:
        sock.settimeout(1.0)
        if not client_handshake(sock, token, name, role=ROLE_NET):
            return False
        print(f"Підключено до вчителя {ip}:{port}. Керую доступом до інтернету.")
        guard.teacher_ip = ip
        state, mode = guard.current_state()
        send_state(sock, state, mode, initial=True)
        applied_at = time.time() if state == "blocked" else 0.0
        while not stop_event.is_set():
            try:
                ready, _, _ = select.select([sock], [], [], 1.0)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                packet = recv_packet(sock)
            except (OSError, ValueError, ConnectionError):
                break
            if packet[:1] != MSG_NET:
                continue
            want_block = packet[1:2] == b"\x01"
            send_state(sock, "working", guard.mode)
            ok, mode, error = guard.apply(want_block)
            if ok:
                if want_block:
                    applied_at = time.time()
                send_state(sock, "blocked" if want_block else "none", mode, "")
                print("Інтернет заблоковано." if want_block else "Інтернет розблоковано.")
            else:
                send_state(sock, "error", mode, error or "Помилка")
                print(f"Не вдалося змінити блокування: {error}", file=sys.stderr)
        # зв'язок із учителем обірвався невдовзі після блокування — знімаємо як
        # запобіжник, щоб правило не залишило комп'ютер без інтернету назавжди
        if applied_at and time.time() - applied_at < 20:
            print("Зв'язок із учителем втрачено одразу після блокування — "
                  "знімаю блокування як запобіжний захід.", file=sys.stderr)
            guard.apply(False)
            applied_at = 0.0
        return True


def run_client(args, token, guard, stop_event):
    """Основний цикл: пошук вчителя → сесія → при обриві пошук знову."""
    while not stop_event.is_set():
        try:
            if args.teacher_ip:
                ip, port = args.teacher_ip, args.port
            else:
                print(f"Пошук комп'ютера вчителя (UDP {args.discovery_port})…")
                found = discover_teacher(token, args.discovery_port, stop_event, args.port)
                if found is None:
                    return 0
                ip, port, tname = found
                print(f"Знайдено вчителя «{tname}» за адресою {ip}")
            if not session(ip, port, token, args.name, guard, stop_event):
                print("Автентифікація не пройдена (перевірте токен).", file=sys.stderr)
                if args.teacher_ip:
                    return 3
                stop_event.wait(3)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"З'єднання перервано: {exc}. Повтор через 3 с…", file=sys.stderr)
            stop_event.wait(3)
    return 0


def main():
    cfg = load_config()
    parser = argparse.ArgumentParser(
        description="Привілейований клієнт блокування доступу до інтернету (netguard).")
    parser.add_argument("teacher_ip", nargs="?",
                        help="(необов'язково) IP вчителя; без нього — автоматичне виявлення")
    parser.add_argument("--port", type=int, default=cfg["tcp_port"], help="TCP-порт учителя")
    parser.add_argument("--discovery-port", type=int, default=cfg["discovery_port"],
                        help="UDP-порт виявлення")
    parser.add_argument("--token", help="спільний токен (необов'язково; інакше — з файлу)")
    parser.add_argument("--token-file", help="шлях до файлу з токеном "
                        "(за замовчуванням — classroom_token.txt поряд з netguard)")
    parser.add_argument("--name", default=platform.node() or "netguard",
                        help="ім'я, яке бачить учитель")
    args = parser.parse_args()

    token = args.token or load_token(args.token_file)
    if not token:
        print("Токен доступу не знайдено.\n"
              "Покладіть файл classroom_token.txt поряд з netguard.py. Очікується шлях:\n"
              f"  {token_file_path(args.token_file)}\n"
              "або передайте токен аргументом --token.", file=sys.stderr)
        return 2

    guard = NetworkGuard()
    if platform.system() == "Windows" and not NetworkGuard._is_admin_windows():
        print("Увага: netguard запущено без прав адміністратора — блокування "
              "інтернету буде недоступним.", file=sys.stderr)
    elif platform.system() == "Linux" and NetworkGuard._geteuid() != 0:
        print("Увага: netguard запущено без прав root — блокування інтернету "
              "буде недоступним. Запустіть через sudo або як службу.", file=sys.stderr)

    stop_event = threading.Event()

    def _stop(_signum, _frame):
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    try:
        return run_client(args, token, guard, stop_event)
    except KeyboardInterrupt:
        return 0
    finally:
        # на чистому завершенні (зупинка служби) знімаємо блокування, щоб не
        # залишити комп'ютер без інтернету поза контролем учителя
        if guard.current_state()[0] == "blocked":
            print("Зупинка netguard — знімаю блокування інтернету.")
            guard.apply(False)


if __name__ == "__main__":
    raise SystemExit(main())

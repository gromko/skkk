"""Спільний код для teacher.py та student_agent.py. Python 3.10+."""
import ctypes
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse


# ---------- Шляхи для запуску з вихідного коду та зібраного PyInstaller ----------

def is_frozen():
    """True, якщо код зібрано PyInstaller (або іншим «заморожувачем»)."""
    return bool(getattr(sys, "frozen", False))


def app_dir():
    """Тека для зовнішніх файлів (classroom_config.json, classroom_token.txt,
    сусідні програми). Для зібраного застосунку — тека поряд із виконуваним
    файлом; для запуску з вихідного коду — тека зі скриптами (common.py)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir():
    """Тека з вбудованими ресурсами (теки даних, напр. img/). У PyInstaller
    onefile це тимчасова тека розпакування (sys._MEIPASS); у onedir — теж
    _MEIPASS; без збірки — тека зі скриптами."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return app_dir()


def app_icon_path():
    """Шлях до іконки застосунку (icon.png): поряд із виконуваним файлом
    (app_dir) або серед вбудованих ресурсів (bundle_dir). Повертає Path або None."""
    for base in (bundle_dir(), app_dir()):
        candidate = Path(base) / "icon.png"
        if candidate.exists():
            return candidate
    return None


def program_path(name):
    """Шлях до сусідньої програми SKKK (name без розширення): сусідній .exe
    для зібраного застосунку або .py-скрипт поряд."""
    if is_frozen():
        ext = ".exe" if os.name == "nt" else ""
        return app_dir() / (name + ext)
    return app_dir() / (name + ".py")


def program_command(name, *args):
    """Команда для запуску сусідньої програми. Для зібраного застосунку —
    прямий запуск .exe; для вихідного коду — через поточний інтерпретатор."""
    if is_frozen():
        return [str(program_path(name)), *args]
    return [sys.executable, str(program_path(name)), *args]


def program_exists(name):
    """Чи існує файл сусідньої програми, який потрібно запускати."""
    return program_path(name).exists()


def module_available(name):
    """Чи доступний модуль для імпорту.

    У зібраному застосунку окремий процес ``sys.executable`` запустити
    неможливо (це той самий застосунок), тому перевіряємо через importlib.
    Поза збіркою запускаємо окремий інтерпретатор, щоб «побачити» щойно
    встановлені пакети (вони потрапляють у sys.path лише нового процесу)."""
    if is_frozen():
        try:
            import importlib
            import importlib.util
            importlib.invalidate_caches()
            return importlib.util.find_spec(name) is not None
        except (ImportError, AttributeError, ValueError):
            return False
    try:
        result = subprocess.run([sys.executable, "-c", f"import {name}"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


SERVICE = "classroom-screen-viewer"
PROTO_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024

# Роль клієнта в рукостисканні. "agent" — звичайний агент учня (екран, файли,
# блокування екрана/введення, вимкнення). "net" — окремий привілейований
# клієнт netguard, що керує лише блокуванням виходу в інтернет.
ROLE_AGENT = "agent"
ROLE_NET = "net"

# Тип повідомлення = перший байт кожного пакета після рукостискання
MSG_FRAME = b"F"   # учень -> вчитель: кадр екрана учня
MSG_DEMO = b"D"    # вчитель -> учень: кадр екрана вчителя (демонстрація)
MSG_STOP = b"S"    # вчитель -> учень: демонстрацію завершено
MSG_TEXT = b"M"    # вчитель -> учень: текстове повідомлення (UTF-8)
MAX_MESSAGE_CHARS = 2000

# Передавання файлів (вчитель -> учень); результат (учень -> вчитель)
MSG_FILE_START = b"A"    # JSON: id, name, size, sha256
MSG_FILE_DATA = b"B"     # 4 байти id + шматок даних
MSG_FILE_END = b"E"      # 4 байти id + 1 байт статусу (1 — успіх, 0 — скасовано)
MSG_FILE_RESULT = b"R"   # учень -> вчитель: JSON {id, ok, error}
FILE_CHUNK = 256 * 1024

# Блокування виходу в інтернет (локальна мережа залишається доступною)
MSG_NET = b"N"           # вчитель -> учень: 1 байт (1 — заблокувати, 0 — розблокувати)
MSG_NET_STATE = b"V"     # учень -> вчитель: JSON {state, mode, initial, error}
# state: "none" | "working" | "blocked" | "error"
# mode: "windows_firewall" | "linux_iptables" | "unsupported"

# Вимкнення комп'ютера учня
MSG_POWER = b"P"         # вчитель -> учень: JSON {"action": "shutdown", "delay": секунди} або {"action": "cancel"}
MSG_POWER_STATE = b"Q"   # учень -> вчитель: JSON {state, remaining, error}
# state: "none" | "pending" (іде відлік) | "executing" | "cancelled" | "failed"

# Блокування комп'ютера учня
MSG_LOCK = b"L"          # вчитель -> учень: 1 байт (1 — заблокувати, 0 — розблокувати)
MSG_LOCK_STATE = b"K"    # учень -> вчитель: JSON {locked, mode, initial}
# mode: "full" — введення заблоковано; "visual" — лише перекрито екран;
#       "unsupported" — на цьому агенті блокування недоступне

# Блокування лише введення (миша/клавіатура) без перекриття екрана
MSG_INPUT = b"I"         # вчитель -> учень: 1 байт (1 — блокувати введення, 0 — розблокувати)
MSG_INPUT_STATE = b"J"   # учень -> вчитель: JSON {blocked, mode, initial}
# mode: "full" — введення заблоковано; "unsupported" — недоступно; "none" — вимкнено
MAX_FILES_PER_SEND = 20

# Перегляд відео (вчитель -> учень). Відеофайл передається тими самими пакетами,
# що й звичайні файли (MSG_FILE_START/DATA/END), але з полем "mode": "video":
# учень зберігає його в тимчасовій теці, відтворює штатним плеєром і видаляє.
MSG_VIDEO_STOP = b"X"    # вчитель -> учень: зупинити відтворення й видалити файл
MSG_YOUTUBE = b"Z"       # вчитель -> учень: JSON {"url": посилання на YouTube}
MSG_VIDEO_RESULT = b"Y"  # учень -> вчитель: JSON {id, phase, ok, error}
# phase: "playing" | "done" | "error"

# Відео з YouTube дозволяємо відкривати лише на цих доменах
YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
})

# Розширення файлів, які дозволено відтворювати у режимі перегляду відео
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".mpg",
    ".mpeg", ".mpg2", ".m2ts", ".ts", ".vob", ".ogv", ".ogm", ".3gp", ".3g2",
    ".mts", ".m2v", ".divx", ".rmvb",
})

# Типи файлів, які можуть бути запущені подвійним кліком (блокуються за замовчуванням)
RISKY_EXTENSIONS = frozenset({
    ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".pif", ".cpl", ".dll",
    ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".jar",
    ".lnk", ".reg", ".appx", ".msix",
})
_RESERVED_NAMES = {"con", "prn", "aux", "nul",
                   *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}

DEFAULTS = {
    "tcp_port": 8765,               # передавання зображень
    "discovery_port": 8766,         # UDP-оголошення вчителя
    "announce_interval": 3.0,       # секунд між оголошеннями
    "broadcast_address": "255.255.255.255",  # можна вказати напр. 192.168.1.255
    "demo_interval": 1.0,           # секунд між кадрами демонстрації екрана вчителя
    "demo_max_width": 1280,         # ширина кадра демонстрації, px
    "demo_quality": 60,             # JPEG-якість демонстрації (20..95)
    "max_file_mb": 200,             # максимальний розмір одного файлу, МБ
    "allow_executable_files": False,  # дозволити .exe/.bat/.js тощо (не рекомендується)
    "save_dir": "",                 # (учень) куди зберігати файли; порожньо — за замовчуванням
    "shutdown_delay_seconds": 30,       # відлік перед вимкненням за замовчуванням, с
    "shutdown_min_delay_seconds": 10,   # (учень) мінімальний відлік; учитель не може задати менший
    "lock_failsafe_minutes": 0,     # (учень) розблокувати, якщо зв'язок із учителем втрачено на N хв; 0 — ніколи
}


def load_config(path=None):
    """Читає classroom_config.json (необов'язковий) поверх значень за замовчуванням.

    Порядок пошуку файла: явний аргумент, змінна середовища CLASSROOM_CONFIG,
    тека застосунку (поряд із .exe для зібраного PyInstaller), поточна тека."""
    cfg = dict(DEFAULTS)
    if path is None:
        path = os.environ.get("CLASSROOM_CONFIG")
    if not path:
        for candidate in (app_dir() / "classroom_config.json",
                          Path.cwd() / "classroom_config.json"):
            if candidate.exists():
                path = str(candidate)
                break
        else:
            path = str(app_dir() / "classroom_config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    for key in ("tcp_port", "discovery_port"):
        cfg[key] = int(cfg[key])
        if not 1 <= cfg[key] <= 65535:
            raise ValueError(f"{key} must be 1..65535")
    cfg["announce_interval"] = max(1.0, float(cfg["announce_interval"]))
    cfg["demo_interval"] = max(0.3, float(cfg["demo_interval"]))
    cfg["demo_max_width"] = min(3840, max(640, int(cfg["demo_max_width"])))
    cfg["demo_quality"] = min(95, max(20, int(cfg["demo_quality"])))
    cfg["max_file_mb"] = min(4096, max(1, int(cfg["max_file_mb"])))
    cfg["allow_executable_files"] = bool(cfg["allow_executable_files"])
    cfg["save_dir"] = str(cfg["save_dir"] or "")
    cfg["lock_failsafe_minutes"] = max(0, int(cfg["lock_failsafe_minutes"]))
    cfg["shutdown_min_delay_seconds"] = min(600, max(0, int(cfg["shutdown_min_delay_seconds"])))
    cfg["shutdown_delay_seconds"] = min(600, max(cfg["shutdown_min_delay_seconds"],
                                                  int(cfg["shutdown_delay_seconds"])))
    return cfg


# ---------- Токен доступу (читається з файлу, без запиту) ----------

TOKEN_FILE_NAME = "classroom_token.txt"
MIN_TOKEN_LEN = 12
_TOKEN_CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def token_file_path(path=None):
    """Шлях до файлу з токеном. Пріоритет: явний аргумент, змінна середовища
    CLASSROOM_TOKEN_FILE, інакше — файл classroom_token.txt поряд із програмою."""
    if path:
        return str(path)
    env = os.environ.get("CLASSROOM_TOKEN_FILE")
    if env:
        return env
    return str(app_dir() / TOKEN_FILE_NAME)


def load_token(path=None):
    """Читає токен із файлу (перший непорожній рядок).
    Повертає None, якщо файл відсутній або порожній."""
    try:
        with open(token_file_path(path), encoding="utf-8") as handle:
            for line in handle:
                token = line.strip()
                if token:
                    return token
    except OSError:
        pass
    return None


def save_token(token, path=None):
    """Записує токен у файл із правами доступу лише для власника (0600)."""
    target = token_file_path(path)
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token.strip() + "\n")
    return target


def generate_token(length=24):
    return "".join(secrets.choice(_TOKEN_CHARSET) for _ in range(length))


# ---------- Позначка завершеного налаштування середовища ----------
#
# skkk_setup.py після успішної перевірки/налаштування створює цей файл, а
# teacher.py за його наявністю вирішує, чи потрібно запускати майстер
# налаштування перед стартом.

SETUP_MARKER = Path.home() / ".config" / "classroom_screen_monitor" / "setup.json"


def mark_setup_complete(extra=None):
    data = {"completed": True, "time": time.time(),
            "python": sys.version.split()[0], "platform": platform.platform()}
    if extra:
        data.update(extra)
    try:
        SETUP_MARKER.parent.mkdir(parents=True, exist_ok=True)
        with open(SETUP_MARKER, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return SETUP_MARKER


def setup_complete():
    """True, якщо майстер налаштування середовища вже успішно завершувався."""
    try:
        with open(SETUP_MARKER, encoding="utf-8") as handle:
            data = json.load(handle)
        return bool(data.get("completed"))
    except (OSError, ValueError):
        return False


# Позначка завершеного налаштування учнівського комп'ютера (skkk_student_setup.py).
# Окремий файл, щоб налаштування вчителя й учня не перекривали одне одного.

STUDENT_SETUP_MARKER = (Path.home() / ".config" / "classroom_screen_monitor"
                        / "student_setup.json")


def mark_student_setup_complete(extra=None):
    data = {"completed": True, "time": time.time(),
            "python": sys.version.split()[0], "platform": platform.platform()}
    if extra:
        data.update(extra)
    try:
        STUDENT_SETUP_MARKER.parent.mkdir(parents=True, exist_ok=True)
        with open(STUDENT_SETUP_MARKER, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return STUDENT_SETUP_MARKER


def student_setup_complete():
    """True, якщо майстер налаштування учнівського комп'ютера вже завершувався."""
    try:
        with open(STUDENT_SETUP_MARKER, encoding="utf-8") as handle:
            data = json.load(handle)
        return bool(data.get("completed"))
    except (OSError, ValueError):
        return False



# ---------- TCP-фреймінг ----------

def recv_exact(sock, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("Connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_packet(sock, max_size=MAX_FRAME_BYTES):
    size = struct.unpack("!I", recv_exact(sock, 4))[0]
    if size <= 0 or size > max_size:
        raise ValueError("Invalid packet size")
    return recv_exact(sock, size)


def send_packet(sock, payload):
    sock.sendall(struct.pack("!I", len(payload)) + payload)


# ---------- HMAC ----------

def mac(token, *parts):
    h = hmac.new(token.encode("utf-8"), digestmod=hashlib.sha256)
    for part in parts:
        data = part if isinstance(part, bytes) else str(part).encode("utf-8")
        h.update(struct.pack("!I", len(data)))   # довжина — щоб частини не "зливалися"
        h.update(data)
    return h.hexdigest()


# ---------- UDP-оголошення ----------

def build_announcement(token, tcp_port, name):
    body = json.dumps({
        "service": SERVICE,
        "v": PROTO_VERSION,
        "tcp_port": tcp_port,
        "name": str(name)[:80],
        "ts": int(time.time()),
        "nonce": secrets.token_hex(8),
    }, separators=(",", ":"))
    return json.dumps({"body": body, "mac": mac(token, "announce", body)}).encode("utf-8")


def parse_announcement(token, data):
    """Повертає dict з оголошенням або None, якщо воно недійсне."""
    try:
        if len(data) > 2048:
            return None
        outer = json.loads(data.decode("utf-8"))
        body_str = outer["body"]
        if not hmac.compare_digest(str(outer["mac"]), mac(token, "announce", body_str)):
            return None
        body = json.loads(body_str)
        if body.get("service") != SERVICE or body.get("v") != PROTO_VERSION:
            return None
        # Позначку часу (ts) не відкидаємо: автентичність забезпечує HMAC, а
        # жорстка перевірка часу ламала пошук за розбіжності годинників.
        port = int(body["tcp_port"])
        if not 1 <= port <= 65535:
            return None
        return {"tcp_port": port, "name": str(body.get("name", ""))}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def build_query(token):
    """Запит учня «де вчитель?». Підписаний спільним токеном, тож учитель
    відповідає лише на запити з правильним токеном."""
    body = json.dumps({
        "service": SERVICE,
        "v": PROTO_VERSION,
        "q": "who",
        "ts": int(time.time()),
        "nonce": secrets.token_hex(8),
    }, separators=(",", ":"))
    return json.dumps({"body": body, "mac": mac(token, "query", body)}).encode("utf-8")


def parse_query(token, data):
    """Повертає True, якщо це дійсний запит «де вчитель?» із правильним токеном."""
    try:
        if len(data) > 2048:
            return False
        outer = json.loads(data.decode("utf-8"))
        body_str = outer["body"]
        if not hmac.compare_digest(str(outer["mac"]), mac(token, "query", body_str)):
            return False
        body = json.loads(body_str)
        if body.get("service") != SERVICE or body.get("v") != PROTO_VERSION:
            return False
        if body.get("q") != "who":
            return False
        return True
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def _fallback_ipv4_interfaces():
    """Для Windows/macOS (і коли ioctl не спрацював): локальні IPv4-адреси без
    точних масок. Припускаємо /24 — типова шкільна мережа."""
    ips = []
    try:        # UDP connect нічого не надсилає, лише обирає вихідний інтерфейс
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            ips.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    result, seen = [], set()
    for ip in ips:
        if ip in seen or ip.startswith(("127.", "169.254.", "0.")):
            continue
        seen.add(ip)
        result.append(("?", ip, "255.255.255.0"))
    return result


def _local_ipv4_interfaces():
    """[(ім'я, ip, маска)] для локальних інтерфейсів IPv4."""
    result = _linux_ipv4_interfaces() if sys.platform.startswith("linux") else []
    return result or _fallback_ipv4_interfaces()


def _linux_ipv4_interfaces():
    result = []
    try:
        import fcntl
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _idx, name in socket.if_nameindex():
                if name == "lo":
                    continue
                try:
                    packed = struct.pack("256s", name[:15].encode("utf-8"))
                    ip = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), 0x8915, packed)[20:24])
                    mask = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), 0x891b, packed)[20:24])
                except OSError:
                    continue
                result.append((name, ip, mask))
        finally:
            sock.close()
    except Exception:
        pass
    return result


def local_broadcast_targets():
    """Адреси для розсилки UDP-оголошень: глобальна 255.255.255.255 та
    directed-broadcast кожної локальної мережі IPv4 (Linux). Якщо визначити
    не вдалося — лише глобальна."""
    targets = {"255.255.255.255"}
    for _name, ip, mask in _local_ipv4_interfaces():
        try:
            ip_i = struct.unpack("!I", socket.inet_aton(ip))[0]
            mask_i = struct.unpack("!I", socket.inet_aton(mask))[0]
        except OSError:
            continue
        if mask_i == 0:
            continue
        targets.add(socket.inet_ntoa(struct.pack("!I", ip_i | (~mask_i & 0xFFFFFFFF))))
    return sorted(targets)


def local_subnet_hosts():
    """IPv4-адреси інших хостів у локальній підмережі (по одному /24 навколо
    кожної нашої адреси) — для активного пошуку, коли широкомовлення недоступне."""
    hosts = []
    seen = set()
    for _name, ip, _mask in _local_ipv4_interfaces():
        try:
            ip_i = struct.unpack("!I", socket.inet_aton(ip))[0]
        except OSError:
            continue
        base24 = ip_i & 0xFFFFFF00
        for off in range(1, 255):
            cand = base24 | off
            if cand == ip_i:
                continue
            addr = socket.inet_ntoa(struct.pack("!I", cand))
            if addr in seen:
                continue
            seen.add(addr)
            hosts.append(addr)
    return hosts


# ---------- Пошук вчителя через TCP (не потребує вхідного UDP) ----------

def probe_teacher(ip, port, token, timeout=0.6):
    """Чи працює на ip:port вчитель з нашим токеном? Виконуємо лише першу
    половину рукостискання (перевіряємо підпис сервера) і одразу закриваємо
    з'єднання — у вчителя учень при цьому не реєструється."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.settimeout(max(timeout, 2.0))
            cnonce = secrets.token_hex(16)
            send_packet(sock, json.dumps({"name": "probe", "cnonce": cnonce}).encode("utf-8"))
            resp = json.loads(recv_packet(sock, 4096).decode("utf-8"))
            snonce = str(resp["snonce"])
            return hmac.compare_digest(str(resp.get("proof", "")),
                                       mac(token, "srv", cnonce, snonce))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def find_teacher_tcp(token, port, hosts, stop_event=None, workers=64, timeout=0.6):
    """Перебирає hosts, повертає IP першого, де відповів вчитель із нашим токеном."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if not hosts:
        return None
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(probe_teacher, h, port, token, timeout): h for h in hosts}
        for fut in as_completed(futures):
            if stop_event is not None and stop_event.is_set():
                return None
            if fut.result():
                return futures[fut]
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def discover_teacher(token, discovery_port, stop_event, tcp_port=None):
    """Шукає вчителя: слухає його UDP-оголошення, надсилає підписаний запит
    «де вчитель?» (широкомовленням) і, якщо це не дає відповіді, перебирає
    адреси підмережі напряму (коли широкомовлення заблоковане мережею).
    Повертає (ip, tcp_port, name) після перевірки підпису; IP береться з
    адреси відправника пакета, а не з вмісту оголошення."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", discovery_port))
        sock.settimeout(1.0)
        if sys.platform == "win32":
            try:    # без цього Windows кидає ConnectionResetError після ICMP «port unreachable»
                sock.ioctl(socket.SIO_UDP_CONNRESET, False)
            except (AttributeError, OSError, ValueError):
                pass
        targets = local_broadcast_targets()
        sweep_hosts = local_subnet_hosts()
        query = build_query(token)
        started = time.time()
        last_hint = started
        last_query = 0.0
        next_sweep = started + 4.0
        next_tcp = started + 6.0
        tcp_announced = False
        sweep_announced = False
        rejected = 0
        while not stop_event.is_set():
            now = time.time()
            if now - last_query >= 2.0:       # широкомовний запит
                last_query = now
                for target in targets:
                    try:
                        sock.sendto(query, (target, discovery_port))
                    except OSError:
                        pass
            # широкомовлення не дало відповіді — шукаємо перебором адрес
            if sweep_hosts and now >= next_sweep:
                next_sweep = now + 10.0
                if not sweep_announced:
                    sweep_announced = True
                    print("Широкомовлення не дало відповіді — шукаю вчителя "
                          "перебором адрес у підмережі…", file=sys.stderr)
                for host in sweep_hosts:
                    try:
                        sock.sendto(query, (host, discovery_port))
                    except OSError:
                        pass
            # UDP-відповіді не доходять (типово — вхідний UDP закрито фаєрволом) —
            # шукаємо вчителя по TCP: вихідні TCP-з'єднання працюють завжди
            if tcp_port and sweep_hosts and now >= next_tcp:
                next_tcp = now + 10.0
                if not tcp_announced:
                    tcp_announced = True
                    print("UDP-відповіді не надходять — пробую знайти вчителя "
                          f"через TCP-порт {tcp_port}…", file=sys.stderr)
                found_ip = find_teacher_tcp(token, tcp_port, sweep_hosts, stop_event)
                if found_ip:
                    return found_ip, tcp_port, found_ip
            try:
                data, addr = sock.recvfrom(2048)
            except ConnectionResetError:         # Windows: ICMP від хоста без слухача
                continue
            except socket.timeout:
                if now - last_hint > 15:
                    last_hint = now
                    hint = (f"Вчителя ще не знайдено ({int(now - started)} с). "
                            f"Перевірте токен, мережу та UDP-порт {discovery_port} "
                            f"у брандмауері.")
                    if rejected:
                        hint += (f" Отримано пакетів, які не пройшли перевірку: {rejected}.")
                    print(hint, file=sys.stderr)
                continue
            ann = parse_announcement(token, data)
            if ann:
                return addr[0], ann["tcp_port"], ann["name"]
            if parse_query(token, data):
                continue                     # власний запит повернувся розсилкою
            rejected += 1
        return None
    finally:
        sock.close()


# ---------- Взаємна автентифікація по TCP (токен по мережі не передається) ----------

def server_handshake(conn, token):
    """Повертає (ім'я, роль) при успіху, інакше None. Роль за замовчуванням
    ROLE_AGENT — старі агенти поля role не надсилають."""
    hello = json.loads(recv_packet(conn, 4096).decode("utf-8"))
    cnonce = str(hello["cnonce"])
    name = str(hello.get("name", ""))[:80]
    role = str(hello.get("role", ROLE_AGENT))[:16] or ROLE_AGENT
    if not 8 <= len(cnonce) <= 64:
        raise ValueError("Bad client nonce")
    snonce = secrets.token_hex(16)
    send_packet(conn, json.dumps({
        "snonce": snonce,
        "proof": mac(token, "srv", cnonce, snonce),
    }).encode("utf-8"))
    reply = json.loads(recv_packet(conn, 4096).decode("utf-8"))
    ok = hmac.compare_digest(str(reply.get("proof", "")), mac(token, "cli", snonce, cnonce))
    conn.sendall(b"OK" if ok else b"NO")
    return (name, role) if ok else None


def client_handshake(sock, token, name, role=ROLE_AGENT):
    """True, якщо вчитель підтвердив знання токена і прийняв нас."""
    cnonce = secrets.token_hex(16)
    send_packet(sock, json.dumps(
        {"name": name, "role": role, "cnonce": cnonce}).encode("utf-8"))
    resp = json.loads(recv_packet(sock, 4096).decode("utf-8"))
    snonce = str(resp["snonce"])
    if not hmac.compare_digest(str(resp.get("proof", "")), mac(token, "srv", cnonce, snonce)):
        return False   # сервер не знає токена — не довіряємо
    send_packet(sock, json.dumps({"proof": mac(token, "cli", snonce, cnonce)}).encode("utf-8"))
    return recv_exact(sock, 2) == b"OK"


# ---------- Фаєрвол: дозвіл вхідного TCP-порту ----------

FIREWALL_RULE_NAME = "ClassroomScreenViewer"
_FW_MARKER = Path.home() / ".config" / "classroom_screen_monitor" / "firewall.json"


# Каталоги, де на POSIX-системах лежать утиліти адміністрування (ufw, iptables,
# chronyc тощо). GUI-сесії (запуск із .desktop) часто мають PATH без /sbin та
# /usr/sbin, тому шукаємо утиліти і там, не покладаючись лише на PATH.
_PATH_FALLBACK_DIRS = ("/usr/sbin", "/sbin", "/usr/local/sbin",
                       "/usr/bin", "/bin", "/usr/local/bin")


def _which(tool):
    """Як shutil.which, але додатково шукає у типових каталогах sbin/bin.

    Потрібно, бо під час запуску із графічного середовища (ярлик .desktop)
    змінна PATH може не містити /sbin та /usr/sbin, де розташовані ufw,
    iptables, chronyc тощо."""
    found = shutil.which(tool)
    if found:
        return found
    if os.name == "posix" and "/" not in tool:
        for directory in _PATH_FALLBACK_DIRS:
            candidate = os.path.join(directory, tool)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _fw_run(cmd, timeout=120):
    """Тихий запуск команди; None, якщо утиліту не знайдено або сталася помилка."""
    flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
    cmd = list(cmd)
    env = None
    if os.name == "posix":
        if cmd and "/" not in cmd[0]:
            resolved = _which(cmd[0])
            if resolved:
                cmd[0] = resolved
        # Розширюємо PATH і для дочірніх процесів (напр. sh -c "ufw allow ...").
        env = dict(os.environ)
        extra = ":".join(_PATH_FALLBACK_DIRS)
        env["PATH"] = (env.get("PATH", "") + ":" + extra).strip(":")
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=flags, env=env)
    except (OSError, subprocess.SubprocessError):
        return None


def _fw_is_root():
    return getattr(os, "geteuid", lambda: 1)() == 0


def _fw_is_admin_windows():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _fw_load_marker():
    try:
        with open(_FW_MARKER, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _fw_record_marker(mode, port):
    """Запам'ятовує, що прави́ло для порту вже додано цим застосунком, щоб
    не питати права повторно за кожного запуску (перевірку без root зробити
    на Linux неможливо)."""
    data = _fw_load_marker()
    ports = data.setdefault(mode, [])
    if port not in ports:
        ports.append(port)
    try:
        _FW_MARKER.parent.mkdir(parents=True, exist_ok=True)
        with open(_FW_MARKER, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _fw_result(status, mode="", message="", command=""):
    return {"status": status, "mode": mode, "message": message, "command": command}


def _elevation_note(on_elevate, why):
    """Повідомляє користувача, НАВІЩО зараз буде запит прав адміністратора.
    Викликається безпосередньо перед підвищенням прав (UAC/pkexec/sudo)."""
    if on_elevate is None:
        return
    try:
        on_elevate(why)
    except Exception:
        pass


def _elevation_possible_linux():
    return bool(_which("pkexec") or _which("sudo"))


# ----- Windows -----

def _fw_windows_rule_state(rule):
    result = _fw_run(["netsh", "advfirewall", "firewall", "show", "rule",
                      f"name={rule}"], timeout=20)
    if result is None:
        return "unsupported"
    out = result.stdout or ""
    if result.returncode != 0 or "Rule Name" not in out:
        return "missing"
    if re.search(r"Enabled:\s*No", out, re.IGNORECASE):
        return "missing"
    return "present"


def _fw_windows_add(port, rule, proto, on_elevate=None):
    proto_up = proto.upper()
    if _fw_is_admin_windows():
        result = _fw_run(["netsh", "advfirewall", "firewall", "add", "rule",
                          f"name={rule}", "dir=in", "action=allow", f"protocol={proto_up}",
                          f"localport={port}", "profile=any", "enable=yes"])
        return result is not None and result.returncode == 0
    netsh = (f'netsh advfirewall firewall add rule name="{rule}" dir=in '
             f'action=allow protocol={proto_up} localport={port} profile=any enable=yes')
    _elevation_note(
        on_elevate,
        f"Щоб дозволити вхідний {proto_up}-порт {port} у фаєрволі Windows, "
        f"потрібні права адміністратора.\n\n"
        f"Без цього комп'ютери учнів/учитель не зможуть підключитися автоматично.\n"
        f"Зараз з'явиться системний запит контролю облікових записів (UAC).")
    try:      # запит UAC; користувач може скасувати
        code = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe",
                                                   f"/c {netsh}", None, 0)
        return code > 32
    except Exception:
        return False


def _fw_ensure_windows(port, rule, proto="tcp", on_elevate=None):
    proto_up = proto.upper()
    state = _fw_windows_rule_state(rule)
    manual = (f'netsh advfirewall firewall add rule name="{rule}" dir=in '
              f'action=allow protocol={proto_up} localport={port} profile=any enable=yes')
    if state == "present":
        return _fw_result("ok", "windows_firewall",
                          f"Вхідний {proto_up}-порт {port} уже дозволено у фаєрволі Windows.")
    if state == "unsupported":
        return _fw_result("unsupported", "windows_firewall",
                          "Утиліту netsh не знайдено — керування фаєрволом недоступне.",
                          manual)
    if _fw_windows_add(port, rule, proto, on_elevate) and _fw_windows_rule_state(rule) == "present":
        return _fw_result("added", "windows_firewall",
                          f"Вхідний {proto_up}-порт {port} відкрито у фаєрволі Windows.")
    return _fw_result("failed", "windows_firewall",
                      "Не вдалося додати прави́ло для фаєрвола Windows "
                      "(запит прав адміністратора скасовано?).", manual)


# ----- Linux -----

def _fw_ufw_enabled():
    try:
        with open("/etc/ufw/ufw.conf", encoding="utf-8") as f:
            for line in f:
                if line.strip().upper().startswith("ENABLED="):
                    return line.split("=", 1)[1].strip().lower().startswith("yes")
    except OSError:
        pass
    return None


def _fw_linux_plan(port, proto):
    spec = f"{port}/{proto}"
    if _which("ufw") and _fw_ufw_enabled():
        return "ufw", f"ufw allow {spec}"
    if _which("firewall-cmd"):
        return "firewalld", (f"firewall-cmd --permanent --add-port={spec} && "
                             f"firewall-cmd --reload")
    if _which("iptables"):
        return "iptables", (f"iptables -C INPUT -p {proto} --dport {port} -j ACCEPT "
                            f"2>/dev/null || iptables -I INPUT -p {proto} --dport {port} "
                            f"-j ACCEPT")
    if _which("ufw"):
        return "ufw", f"ufw allow {spec}"
    return None, None


def _fw_linux_rule_state(port, mode, proto):
    spec = f"{port}/{proto}"
    if mode == "firewalld":
        result = _fw_run(["firewall-cmd", "--query-port", spec], timeout=20)
        if result is not None and result.returncode in (0, 1):
            return result.returncode == 0
        return None
    if not _fw_is_root():
        return None            # без root правила не прочитати
    if mode == "ufw":
        result = _fw_run(["ufw", "status"], timeout=20)
        if result is not None:
            return re.search(rf"\b{port}/{proto}\b", result.stdout or "") is not None
    elif mode == "iptables":
        result = _fw_run(["iptables", "-C", "INPUT", "-p", proto,
                          "--dport", str(port), "-j", "ACCEPT"], timeout=20)
        if result is not None:
            return result.returncode == 0
    return None


def _elevated_apply_linux(command):
    """Виконує shell-команду від root: напряму, через pkexec (з діалогом) або
    sudo без пароля. Повертає CompletedProcess або None."""
    if _fw_is_root():
        return _fw_run(["sh", "-c", command])
    pkexec = _which("pkexec")
    if pkexec:
        result = _fw_run([pkexec, "sh", "-c", command])
        if result is not None and result.returncode == 0:
            return result
    sudo = _which("sudo")
    if sudo:
        result = _fw_run([sudo, "-n", "sh", "-c", command], timeout=30)
        if result is not None and result.returncode == 0:
            return result
    return None


def _fw_linux_apply(command):
    return _elevated_apply_linux(command)


def _fw_ensure_linux(port, proto="tcp", on_elevate=None):
    proto_up = proto.upper()
    mode, command = _fw_linux_plan(port, proto)
    if mode is None:
        return _fw_result("unsupported", "",
                          "Не знайдено утиліт керування фаєрволом "
                          "(ufw, firewall-cmd або iptables).")
    state = _fw_linux_rule_state(port, mode, proto)
    if state is True:
        return _fw_result("ok", mode,
                          f"Вхідний {proto_up}-порт {port} уже дозволено ({mode}).")
    marker = _fw_load_marker().get(mode, [])
    key = f"{port}/{proto}"
    if state is None and (key in marker or (proto == "tcp" and port in marker)):
        return _fw_result("ok", mode,
                          f"Вхідний {proto_up}-порт {port} уже відкривався раніше ({mode}).")
    if not _fw_is_root() and _elevation_possible_linux():
        _elevation_note(
            on_elevate,
            f"Щоб відкрити вхідний {proto_up}-порт {port} у фаєрволі ({mode}), "
            f"потрібні права адміністратора (root).\n\n"
            f"Без цього комп'ютери учнів/учитель не зможуть підключитися автоматично.\n"
            f"Зараз з'явиться системний запит пароля.")
    result = _fw_linux_apply(command)
    if result is not None and result.returncode == 0:
        _fw_record_marker(mode, key)
        return _fw_result("added", mode,
                          f"Вхідний {proto_up}-порт {port} відкрито у фаєрволі ({mode}).")
    return _fw_result("failed", mode,
                      "Не вдалося відкрити порт у фаєрволі (потрібні права root; "
                      "запит прав, імовірно, скасовано).",
                      f"sudo sh -c '{command}'")


def ensure_firewall_port(port, rule_name=FIREWALL_RULE_NAME, proto="tcp", on_elevate=None):
    """Перевіряє, чи дозволено вхідний порт (TCP або UDP), і за потреби
    відкриває його у локальному фаєрволі (Windows Firewall / ufw / firewalld /
    iptables). За необхідності підвищує права (UAC або pkexec/sudo).

    on_elevate — необов'язковий виклик(текст), який повідомляє користувача,
    навіщо потрібні права адміністратора, безпосередньо перед запитом пароля.

    Повертає dict: {"status": ..., "mode": ..., "message": ..., "command": ...},
    де status — "ok" | "added" | "failed" | "unsupported".
    """
    proto = str(proto).lower()
    if proto not in ("tcp", "udp"):
        raise ValueError("proto must be 'tcp' or 'udp'")
    system = platform.system()
    if system == "Windows":
        rule = rule_name if proto == "tcp" else f"{rule_name} {proto.upper()}"
        return _fw_ensure_windows(port, rule, proto, on_elevate)
    if system == "Linux":
        return _fw_ensure_linux(port, proto, on_elevate)
    return _fw_result("unsupported", "",
                      f"Автоматичне керування фаєрволом для {system} не підтримується.")


# ---------- Автоматична синхронізація часу (NTP / w32time) ----------
#
# Розбіжність годинників між комп'ютерами ускладнює автовизначення вчителя,
# тому на старті вмикаємо системну синхронізацію часу (за потреби — з
# підвищенням прав, супроводжуючи запит поясненням).

_TS_MARKER = Path.home() / ".config" / "classroom_screen_monitor" / "time_sync.json"


def _ts_result(status, mode="", message="", command=""):
    return {"status": status, "mode": mode, "message": message, "command": command}


def _ts_load_marker():
    try:
        with open(_TS_MARKER, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ts_record_marker(mode):
    data = _ts_load_marker()
    if data.get(mode) is not True:
        data[mode] = True
        try:
            _TS_MARKER.parent.mkdir(parents=True, exist_ok=True)
            with open(_TS_MARKER, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError:
            pass


def _ts_linux_synced():
    """True/False/None — чи активна синхронізація часу (None — не вдалося з'ясувати)."""
    if _which("chronyc"):
        r = _fw_run(["chronyc", "tracking"], timeout=5)
        if r is not None and r.returncode == 0:
            m = re.search(r"Stratum\s*:\s*(\d+)", r.stdout or "")
            if m:
                return int(m.group(1)) > 0
    if os.path.isdir("/run/systemd/system") and _which("timedatectl"):
        r = _fw_run(["timedatectl", "show", "-p", "NTP", "--value"], timeout=5)
        if r is not None and r.returncode == 0:
            return r.stdout.strip().lower() in ("yes", "true", "1")
    if _which("ntpq"):
        r = _fw_run(["ntpq", "-p"], timeout=5)
        if r is not None and r.returncode == 0:
            return re.search(r"^\*", r.stdout or "", re.M) is not None
    return None


def _ts_linux_plan():
    """(mode, command) для ввімкнення синхронізації часу на Linux або (None, None)."""
    if os.path.isdir("/run/systemd/system") and _which("timedatectl"):
        return "systemd", "timedatectl set-ntp true"
    # sysvinit / OpenRC: вмикаємо init-скрипт служби синхронізації
    svc = None
    for name in ("chrony", "chronyd", "ntp", "ntpd", "openntpd", "timesyncd"):
        if os.path.exists(f"/etc/init.d/{name}"):
            svc = name
            break
    if svc is not None:
        enable = (f"update-rc.d {svc} defaults 2>/dev/null || "
                  f"rc-update add {svc} default 2>/dev/null || "
                  f"systemctl enable {svc} 2>/dev/null || true")
        start = (f"service {svc} start 2>/dev/null || "
                 f"/etc/init.d/{svc} start 2>/dev/null || "
                 f"rc-service {svc} start 2>/dev/null || "
                 f"systemctl start {svc} 2>/dev/null || true")
        command = f"{enable}; {start}"
        if _which("chronyc"):
            command += "; chronyc makestep 2>/dev/null || true"
        elif _which("ntpdate"):
            command += "; ntpdate -u pool.ntp.org 2>/dev/null || true"
        elif _which("sntp"):
            command += "; sntp -sS pool.ntp.org 2>/dev/null || true"
        return svc, command
    if _which("chronyc"):                # демон уже працює, лише підлаштувати час
        return "chrony", "chronyc makestep 2>/dev/null || true"
    if _which("sntp"):
        return "sntp", "sntp -sS pool.ntp.org"
    if _which("ntpdate"):
        return "ntpdate", "ntpdate -u pool.ntp.org"
    return None, None


_TS_WIN_CMD = (
    'sc config w32time start= auto & '
    'net start w32time & '
    'w32tm /config /manualpeerlist:"time.windows.com,0x8 pool.ntp.org,0x8" '
    '/syncfromflags:manual /reliable:yes /update & '
    'w32tm /resync /force'
)


def _ts_ensure_windows(on_elevate=None):
    if _fw_is_admin_windows():
        result = _fw_run(["cmd.exe", "/c", _TS_WIN_CMD], timeout=120)
        if result is not None and result.returncode == 0:
            _ts_record_marker("w32time")
            return _ts_result("added", "w32time",
                              "Синхронізацію часу Windows увімкнено (служба w32time).")
        return _ts_result("failed", "w32time",
                          "Не вдалося налаштувати синхронізацію часу Windows.",
                          _TS_WIN_CMD)
    if _ts_load_marker().get("w32time"):
        return _ts_result("ok", "w32time",
                          "Синхронізацію часу Windows уже налаштовано раніше.")
    _elevation_note(
        on_elevate,
        "Щоб увімкнути автоматичну синхронізацію часу (служба w32time), потрібні "
        "права адміністратора.\n\nРозбіжність часу між комп'ютерами ускладнює "
        "автоматичне з'єднання.\nЗараз з'явиться системний запит UAC.")
    try:
        code = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe",
                                                   f"/c {_TS_WIN_CMD}", None, 0)
    except Exception:
        code = 0
    if code > 32:
        _ts_record_marker("w32time")
        return _ts_result("added", "w32time",
                          "Запит на ввімкнення синхронізації часу надіслано (UAC).")
    return _ts_result("failed", "w32time",
                      "Запит прав адміністратора скасовано — синхронізацію часу "
                      "не увімкнено.", _TS_WIN_CMD)


def ensure_time_sync(on_elevate=None):
    """Вмикає автоматичну синхронізацію часу (NTP на Linux, w32time на Windows),
    за потреби підвищуючи права. on_elevate(текст) викликається безпосередньо
    перед запитом прав адміністратора, пояснюючи, навіщо це потрібно.

    Повертає dict: {"status": ..., "mode": ..., "message": ..., "command": ...},
    де status — "ok" | "added" | "failed" | "unsupported"."""
    system = platform.system()
    if system == "Windows":
        return _ts_ensure_windows(on_elevate)
    if system != "Linux":
        return _ts_result("unsupported", "",
                          f"Синхронізація часу для {system} не підтримується.")
    synced = _ts_linux_synced()
    if synced is True:
        return _ts_result("ok", "ntp", "Синхронізація часу вже активна.")
    mode, command = _ts_linux_plan()
    if mode is None:
        return _ts_result("unsupported", "",
                          "Не знайдено служби синхронізації часу "
                          "(chrony, ntp, timedatectl).")
    if synced is None and _ts_load_marker().get(mode):
        return _ts_result("ok", mode, "Синхронізацію часу вже вмикали раніше.")
    if not _fw_is_root() and _elevation_possible_linux():
        _elevation_note(
            on_elevate,
            "Щоб увімкнути автоматичну синхронізацію часу (NTP), потрібні права "
            "адміністратора (root).\n\nРозбіжність часу між комп'ютерами ускладнює "
            "автоматичне з'єднання.\nЗараз з'явиться системний запит пароля.")
    result = _elevated_apply_linux(command)
    if result is not None and result.returncode == 0:
        _ts_record_marker(mode)
        return _ts_result("added", mode,
                          f"Автоматичну синхронізацію часу увімкнено ({mode}).")
    return _ts_result("failed", mode,
                      "Не вдалося увімкнути синхронізацію часу (потрібні права root; "
                      "запит прав, імовірно, скасовано).",
                      f"sudo sh -c '{command}'")


# ---------- Захоплення екрана ----------

def new_mss():
    """Створює екземпляр mss, сумісний з різними версіями бібліотеки.

    У mss >= 10.2 клас ``mss.mss`` оголошено застарілим (DeprecationWarning)
    на користь ``mss.MSS``. Повертаємо той варіант, який доступний.
    """
    import mss
    factory = getattr(mss, "MSS", None) or getattr(mss, "mss", None)
    return factory()


def capture_jpeg(sct, max_width=960, quality=55, monitor=0):
    """monitor=0 — об'єднаний робочий стіл, 1 — основний монітор."""
    from PIL import Image
    monitors = sct.monitors
    shot = sct.grab(monitors[monitor if monitor < len(monitors) else 0])
    image = Image.frombytes("RGB", shot.size, shot.rgb)
    if image.width > max_width:
        height = max(1, int(image.height * max_width / image.width))
        image = image.resize((max_width, height), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


# ---------- Допоміжне для файлів ----------

def safe_filename(name):
    """Лишає тільки ім'я файлу: без шляхів, керуючих і заборонених символів."""
    name = str(name).replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip(" .")
    if not name:
        name = "file"
    if name.split(".")[0].lower() in _RESERVED_NAMES:
        name = "_" + name
    if len(name) > 150:
        stem, ext = os.path.splitext(name)
        name = stem[:140] + ext[:10]
    return name


def is_risky_file(name):
    return os.path.splitext(str(name).lower())[1] in RISKY_EXTENSIONS


def is_video_file(name):
    """Чи є файл відео, яке можна відтворити штатним плеєром (за розширенням)."""
    return os.path.splitext(str(name).lower())[1] in VIDEO_EXTENSIONS


def is_youtube_url(url):
    """Чи є посилання http(s)-адресою на один із дозволених доменів YouTube."""
    try:
        parsed = urlparse(str(url).strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.hostname or "").lower() in YOUTUBE_HOSTS


def default_save_dir():
    home = Path.home()
    base = home / "Downloads"
    return str((base if base.is_dir() else home) / "Файли від вчителя")


def format_size(n):
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{int(n)} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024

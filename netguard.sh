#!/bin/sh
# Скрипт запуску й автозапуску привілейованого клієнта netguard
# Потребує прав root для керування фаєрволом.
#
# Якщо поряд лежить зібраний PyInstaller виконуваний файл netguard, він
# використовується замість запуску netguard.py через python3.

set -e

DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || echo python3)"
LOG="/var/log/netguard.log"
USER_LOG="${HOME}/.local/state/netguard/netguard.log"
SERVICE_NAME="netguard"

# Запуск: зібраний бінарник має пріоритет над скриптом.
if [ -x "$DIR/netguard" ]; then
    RUN_CMD="'$DIR/netguard'"
    DESKTOP_EXEC="\"$DIR/netguard\""
else
    RUN_CMD="'$PYTHON' '$DIR/netguard.py'"
    DESKTOP_EXEC="\"$PYTHON\" \"$DIR/netguard.py\""
fi

# Шаблон для pgrep/pkill: шлях до бінарника або ім'я скрипта.
if [ -x "$DIR/netguard" ]; then
    PROC_PATTERN="$DIR/netguard"
else
    PROC_PATTERN="netguard.py"
fi

usage() {
    cat <<'USAGE'
Використання: ./netguard.sh <команда>

Команди:
  run        Запустити netguard у поточному терміналі (foreground)
  start      Запустити netguard у фоновому режимі (nohup)
  stop       Зупинити запущений netguard
  status     Показати, чи працює netguard
  install    Установити автозапуск (root: @reboot у crontab; без root: автозапуск користувача)
  uninstall  Видалити автозапуск netguard
  check      Перевірити, чи потрібні права root
USAGE
}

check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        return 1
    fi
    return 0
}

ensure_user_log() {
    if check_root; then
        mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
        return 0
    fi
    mkdir -p "$(dirname "$USER_LOG")" 2>/dev/null || true
}

run_foreground() {
    cd "$DIR"
    ensure_user_log
    if [ -x "$DIR/netguard" ]; then
        exec "$DIR/netguard"
    fi
    exec "$PYTHON" "$DIR/netguard.py"
}

start_bg() {
    cd "$DIR"
    ensure_user_log
    LOGTARGET="$USER_LOG"
    if check_root; then
        LOGTARGET="$LOG"
    fi
    if [ -x "$DIR/netguard" ]; then
        nohup "$DIR/netguard" >> "$LOGTARGET" 2>&1 &
    else
        nohup "$PYTHON" "$DIR/netguard.py" >> "$LOGTARGET" 2>&1 &
    fi
    echo "netguard запущено в фоні (PID $!)."
}

stop_bg() {
    pkill -f "$PROC_PATTERN" 2>/dev/null || true
    echo "Спроба зупинити netguard завершена."
}

status_bg() {
    if pgrep -f "$PROC_PATTERN" >/dev/null 2>&1; then
        echo "netguard працює."
    else
        echo "netguard не працює."
    fi
}

install_autostart() {
    if check_root; then
        CRON="/var/spool/cron/crontabs/root"
        if [ ! -f "$CRON" ] && [ ! -d "$(dirname "$CRON")" ]; then
            mkdir -p "$(dirname "$CRON")" 2>/dev/null || true
        fi
        line="@reboot cd '$DIR' && $RUN_CMD >> '$LOG' 2>&1"
        if crontab -l 2>/dev/null | grep -q "netguard"; then
            (crontab -l 2>/dev/null | grep -v "netguard"; echo "$line") | crontab -
        else
            (crontab -l 2>/dev/null 2>&1; echo "$line") | crontab -
        fi
        echo "Автозапуск netguard (root) установлено (crontab @reboot)."
        echo "Перевірте лог: $LOG"
    else
        AUTOSTART="${HOME}/.config/autostart"
        mkdir -p "$AUTOSTART"
        DESKTOP="$AUTOSTART/netguard.desktop"
        cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Classroom Netguard
Exec=$DESKTOP_EXEC
X-GNOME-Autostart-enabled=true
X-MATE-Autostart-enabled=true
EOF
        chmod +x "$DESKTOP"
        echo "Автозапуск netguard (користувач) установлено: $DESKTOP"
        echo "Для блокування інтернету потрібні права root. Запустіть netguard від імені адміністратора."
    fi
}

uninstall_autostart() {
    if check_root; then
        if crontab -l 2>/dev/null | grep -q "netguard"; then
            (crontab -l 2>/dev/null | grep -v "netguard") | crontab -
            echo "Автозапуск netguard (root) видалено."
        else
            echo "Автозапуск netguard (root) не знайдено."
        fi
    fi
    AUTOSTART="${HOME}/.config/autostart/netguard.desktop"
    if [ -f "$AUTOSTART" ]; then
        rm -f "$AUTOSTART"
        echo "Видалено $AUTOSTART."
    fi
}

check_privs() {
    if check_root; then
        echo "Працює як root — блокування інтернету доступне."
    else
        echo "Не root — для блокування потрібні права адміністратора (sudo/pkexec)."
    fi
}

case "${1:-}" in
    run)
        run_foreground
        ;;
    start)
        start_bg
        ;;
    stop)
        stop_bg
        ;;
    status)
        status_bg
        ;;
    install)
        install_autostart
        ;;
    uninstall)
        uninstall_autostart
        ;;
    check)
        check_privs
        ;;
    *)
        usage
        exit 1
        ;;
esac
exit 0

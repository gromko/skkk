#!/bin/sh
# Запуск програми вчителя (teacher.py) у фоновому режимі з журналом teacher.log.
# Скрипт сам переходить у свою теку, тому його можна запускати з будь-якого місця
# (подвійним кліком у ROX-Filer/SpaceFM, з меню IceWM/Fluxbox або з термінала).
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
cd "$DIR" || exit 1

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

nohup "$PY" teacher.py > teacher.log 2>&1 &
echo "Програму вчителя запущено (PID $!). Журнал: $DIR/teacher.log"

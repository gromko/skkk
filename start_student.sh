#!/bin/sh
# Запуск агента учня (student_agent.py) у фоновому режимі з журналом student_a.log.
# Скрипт сам переходить у свою теку, тому його можна запускати з будь-якого місця
# (подвійним кліком у ROX-Filer/SpaceFM, з меню IceWM/Fluxbox або з термінала).
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
cd "$DIR" || exit 1

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

nohup "$PY" student_agent.py > student_a.log 2>&1 &
echo "Агента учня запущено (PID $!). Журнал: $DIR/student_a.log"

#!/bin/sh
# Зупинка агента учня (student_agent.py).
if pkill -f student_agent.py; then
    echo "Агента учня зупинено."
else
    echo "Агент учня не запущений."
fi

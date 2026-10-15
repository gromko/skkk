#!/bin/sh
# Зупинка програми вчителя (teacher.py).
if pkill -f teacher.py; then
    echo "Програму вчителя зупинено."
else
    echo "Програму вчителя не запущено."
fi

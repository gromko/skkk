@echo off
rem Запуск програми вчителя (teacher.py) без вікна консолі.
rem Використовується pythonw (графічний запуск без консолі); якщо його немає
rem в PATH — використовується звичайний python (тоді вікно консолі з'явиться).
cd /d "%~dp0"
where pythonw >nul 2>&1 && (start "" pythonw teacher.py) || (start "" python teacher.py)

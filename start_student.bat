@echo off
rem Запуск агента учня у фоновому режимі з журналом student_a.log.
rem Вікно консолі згортається (агент працює далі). Щоб приховати вікно повністю,
rem замініть "python" на "pythonw" — але тоді student_a.log писатися не буде.
cd /d "%~dp0"
start "Student Agent" /min cmd /c "python student_agent.py > student_a.log 2>&1"

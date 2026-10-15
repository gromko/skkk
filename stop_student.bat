@echo off
rem Зупинка агента учня (аналог pkill -f student_agent.py)
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*student_agent.py*' -and $_.Name -ne 'powershell.exe' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Student agent stopped.

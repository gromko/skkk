@echo off
chcp 65001 >nul
rem Збірка програм учнівського комп'ютера у режимі One Dir (PyInstaller).
rem
rem Усі три застосунки мають однаковий набір залежностей, тому використовують
rem ОДНУ СПІЛЬНУ теку бібліотек shared_libs\ (замість трьох однакових).
rem
rem Результат — окрема тека dist\student\:
rem   student_agent.exe, skkk_student_setup.exe, netguard.exe   - виконувані файли
rem   shared_libs\                                              - спільні залежності (одна копія)
rem   icon.png                                                  - іконка застосунків
rem
rem Розповсюджуйте/копіюйте всю теку dist\student\ цілком.
cd /d "%~dp0"

where pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Не знайдено pyinstaller. Встановіть: pip install pyinstaller
    exit /b 1
)

set OUT=dist\student
set LIBS=shared_libs
if exist build_student rmdir /s /q build_student
if exist %OUT% rmdir /s /q %OUT%
mkdir build_student
mkdir %OUT%

pyinstaller --noconfirm --clean --onedir --windowed ^
    --contents-directory %LIBS% ^
    --specpath build_student ^
    --distpath %OUT% ^
    --name student_agent ^
    --icon "%CD%\icon.png" ^
    --add-data "%CD%\icon.png;." ^
    --hidden-import mss ^
    --hidden-import PIL.PngImagePlugin ^
    --hidden-import PIL._imagingtk ^
    --hidden-import PIL._tkinter_finder ^
    student_agent.py

pyinstaller --noconfirm --clean --onedir --windowed ^
    --contents-directory %LIBS% ^
    --specpath build_student ^
    --distpath %OUT% ^
    --name skkk_student_setup ^
    --icon "%CD%\icon.png" ^
    --add-data "%CD%\icon.png;." ^
    --hidden-import PIL.PngImagePlugin ^
    --hidden-import PIL._imagingtk ^
    --hidden-import PIL._tkinter_finder ^
    skkk_student_setup.py

pyinstaller --noconfirm --clean --onedir ^
    --contents-directory %LIBS% ^
    --specpath build_student ^
    --distpath %OUT% ^
    --name netguard ^
    netguard.py

rem Спільна тека бібліотек: зливаємо набори всіх застосунків в одну shared_libs\.
mkdir "%OUT%\%LIBS%"
xcopy /e /i /y /q "%OUT%\student_agent\%LIBS%" "%OUT%\%LIBS%" >nul
xcopy /e /i /y /q "%OUT%\skkk_student_setup\%LIBS%" "%OUT%\%LIBS%" >nul
xcopy /e /i /y /q "%OUT%\netguard\%LIBS%" "%OUT%\%LIBS%" >nul

rem Виконувані файли — безпосередньо в dist\student\, поряд зі shared_libs\.
move /y "%OUT%\student_agent\student_agent.exe" "%OUT%\student_agent.exe" >nul
move /y "%OUT%\skkk_student_setup\skkk_student_setup.exe" "%OUT%\skkk_student_setup.exe" >nul
move /y "%OUT%\netguard\netguard.exe" "%OUT%\netguard.exe" >nul
rmdir /s /q "%OUT%\student_agent" "%OUT%\skkk_student_setup" "%OUT%\netguard"

copy /y icon.png "%OUT%\" >nul

echo.
echo Готово. Тека %OUT%\ містить:
echo   student_agent.exe, skkk_student_setup.exe, netguard.exe
echo   shared_libs\   - спільні бібліотеки (одна копія)
echo   icon.png
echo.
echo Розповсюджуйте всю теку %OUT%\ цілком.

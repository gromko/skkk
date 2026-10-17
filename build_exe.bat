@echo off
chcp 65001 >nul
rem Збірка teacher та skkk_setup у режимі One Dir (PyInstaller).
rem
rem Обидва застосунки складаються в ОДНУ теку dist\ і використовують ОДНУ СПІЛЬНУ
rem теку бібліотек shared_libs\ (замість двох однакових teacher_libs\, setup_libs\).
rem Завдяки цьому teacher бачить сусідній skkk_setup, а classroom_token.txt і
rem classroom_config.json створюються/читаються в спільній теці dist\ (поряд із .exe).
rem
rem Структура dist\:
rem   teacher.exe, skkk_setup.exe   - виконувані файли
rem   shared_libs\                  - спільні бібліотеки та залежності (одна копія;
rem                                   іконки img\ та icon.png також усередині)
rem
rem Розповсюджуйте/копіюйте всю теку dist\ цілком.
cd /d "%~dp0"

where pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Не знайдено pyinstaller. Встановіть: pip install pyinstaller
    exit /b 1
)

set LIBS=shared_libs
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
mkdir build

pyinstaller --noconfirm --clean --onedir --windowed ^
    --contents-directory %LIBS% ^
    --specpath build ^
    --name teacher ^
    --icon "%CD%\icon.png" ^
    --add-data "%CD%\img;img" ^
    --add-data "%CD%\icon.png;." ^
    --hidden-import mss ^
    --hidden-import PIL.PngImagePlugin ^
    --hidden-import PIL._imagingtk ^
    --hidden-import PIL._tkinter_finder ^
    teacher.py

pyinstaller --noconfirm --clean --onedir --windowed ^
    --contents-directory %LIBS% ^
    --specpath build ^
    --name skkk_setup ^
    --icon "%CD%\icon.png" ^
    --add-data "%CD%\icon.png;." ^
    --hidden-import PIL.PngImagePlugin ^
    --hidden-import PIL._imagingtk ^
    --hidden-import PIL._tkinter_finder ^
    skkk_setup.py

rem Спільна тека бібліотек: зливаємо набори обох застосунків в одну shared_libs\.
mkdir "dist\%LIBS%"
xcopy /e /i /y /q "dist\teacher\%LIBS%" "dist\%LIBS%" >nul
xcopy /e /i /y /q "dist\skkk_setup\%LIBS%" "dist\%LIBS%" >nul

rem Виконувані файли — безпосередньо в dist\, поряд зі shared_libs\.
move /y "dist\teacher\teacher.exe" "dist\teacher.exe" >nul
move /y "dist\skkk_setup\skkk_setup.exe" "dist\skkk_setup.exe" >nul
rmdir /s /q "dist\teacher" "dist\skkk_setup"

echo.
echo Готово. Тека dist\ містить:
echo   teacher.exe, skkk_setup.exe   - виконувані файли
echo   shared_libs\                  - спільні бібліотеки (одна копія)
echo.
echo Розповсюджуйте всю теку dist\ цілком.

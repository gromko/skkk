#!/usr/bin/env bash
# Збірка teacher та skkk_setup у режимі One Dir (PyInstaller).
#
# Обидва застосунки складаються в ОДНУ теку dist/ і використовують ОДНУ СПІЛЬНУ
# теку бібліотек shared_libs/ (замість двох однакових teacher_libs/, setup_libs/).
# Завдяки цьому teacher бачить сусідній skkk_setup, а classroom_token.txt і
# classroom_config.json створюються/читаються в спільній теці dist/
# (поряд із виконуваними файлами).
#
# Структура dist/:
#   teacher, skkk_setup       — виконувані файли
#   shared_libs/              — спільні бібліотеки та залежності (одна копія;
#                               іконки img/ та icon.png також усередині)
#
# Кожен exe шукає бібліотеки в теці shared_libs/ поряд із собою.
#
# Розповсюджуйте/копіюйте всю теку dist/ цілком.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v pyinstaller >/dev/null 2>&1; then
    echo "Не знайдено pyinstaller. Встановіть: pip install pyinstaller" >&2
    exit 1
fi

rm -rf build dist
mkdir -p build

LIBS="shared_libs"
APPS=(teacher skkk_setup)

# teacher: One Dir; підтримувальні файли — у спільній shared_libs/, іконки img/.
# Для діагностики приберіть --windowed. mss імпортується ліниво -> hidden-import.
pyinstaller --noconfirm --clean --onedir --windowed \
    --contents-directory "$LIBS" \
    --specpath build \
    --name teacher \
    --icon "$PWD/icon.png" \
    --add-data "$PWD/img:img" \
    --add-data "$PWD/icon.png:." \
    --hidden-import mss \
    --hidden-import PIL.PngImagePlugin \
    --hidden-import PIL._imagingtk \
    --hidden-import PIL._tkinter_finder \
    teacher.py

# skkk_setup: One Dir; підтримувальні файли — у спільній shared_libs/.
pyinstaller --noconfirm --clean --onedir --windowed \
    --contents-directory "$LIBS" \
    --specpath build \
    --name skkk_setup \
    --icon "$PWD/icon.png" \
    --add-data "$PWD/icon.png:." \
    --hidden-import PIL.PngImagePlugin \
    --hidden-import PIL._imagingtk \
    --hidden-import PIL._tkinter_finder \
    skkk_setup.py

# Збираємо спільну теку shared_libs/: зливаємо набори обох застосунків в одну.
mkdir -p "dist/$LIBS"
for app in "${APPS[@]}"; do
    cp -a "dist/$app/$LIBS/." "dist/$LIBS/"
done

# Ім'я libpython, яке очікує кожен exe, вшите в нього на етапі збірки і може
# бути як libpython3.11.so, так і libpython3.11.so.1.0. Гарантуємо наявність
# обох імен (друге — символьне посилання на справжній файл).
real_lib=""
for f in "dist/$LIBS"/libpython*.so*; do
    if [ -e "$f" ] && [ ! -L "$f" ]; then real_lib="$f"; break; fi
done
if [ -n "$real_lib" ]; then
    lib_base="$(basename "$real_lib")"
    case "$lib_base" in
        *.so.1.0) ln -sf "$lib_base" "dist/$LIBS/${lib_base%.1.0}" ;;
        *.so)     ln -sf "$lib_base" "dist/$LIBS/$lib_base.1.0" ;;
    esac
fi

# Виконувані файли — безпосередньо в dist/, поряд зі shared_libs/.
for app in "${APPS[@]}"; do
    mv "dist/$app/$app" "dist/.${app}_tmp"
done
rm -rf "${APPS[@]/#/dist/}"
for app in "${APPS[@]}"; do
    mv "dist/.${app}_tmp" "dist/$app"
done

echo
echo "Готово. Тека dist/ містить:"
echo "  teacher, skkk_setup   — виконувані файли"
echo "  shared_libs/          — спільні бібліотеки (одна копія)"
echo
echo "Розповсюджуйте всю теку dist/ цілком."

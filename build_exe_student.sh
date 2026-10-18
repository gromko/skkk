#!/usr/bin/env bash
# Збірка програм учнівського комп'ютера у режимі One Dir (PyInstaller).
#
# Усі три застосунки мають однаковий набір залежностей, тому використовують
# ОДНУ СПІЛЬНУ теку бібліотек shared_libs/ (≈96 МБ) замість трьох однакових.
#
# Результат — окрема тека dist/student/:
#   student_agent, skkk_student_setup, netguard   — виконувані файли
#   shared_libs/                                  — спільні залежності (одна копія)
#   icon.png                                      — іконка застосунків
#
# Кожен виконуваний файл шукає бібліотеки в теці shared_libs/ поряд із собою
# (уся тека лежить у dist/student/, тому шлях спільний для всіх).
#
# Розповсюджуйте/копіюйте всю теку dist/student/ цілком.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v pyinstaller >/dev/null 2>&1; then
    echo "Не знайдено pyinstaller. Встановіть: pip install pyinstaller" >&2
    exit 1
fi

OUT="dist/student"
LIBS="shared_libs"
APPS=(student_agent skkk_student_setup netguard)
rm -rf build_student "$OUT"
mkdir -p build_student "$OUT"

# Агент учня: One Dir; підтримувальні файли — у спільній shared_libs/.
# mss імпортується ліниво -> hidden-import. ImageTk потребує PIL._tkinter_finder.
pyinstaller --noconfirm --clean --onedir --windowed \
    --contents-directory "$LIBS" \
    --specpath build_student \
    --distpath "$OUT" \
    --name student_agent \
    --icon "$PWD/icon.png" \
    --add-data "$PWD/icon.png:." \
    --hidden-import mss \
    --hidden-import PIL.PngImagePlugin \
    --hidden-import PIL._imagingtk \
    --hidden-import PIL._tkinter_finder \
    student_agent.py

# Майстер налаштування учня: One Dir; підтримувальні файли — у спільній shared_libs/.
pyinstaller --noconfirm --clean --onedir --windowed \
    --contents-directory "$LIBS" \
    --specpath build_student \
    --distpath "$OUT" \
    --name skkk_student_setup \
    --icon "$PWD/icon.png" \
    --add-data "$PWD/icon.png:." \
    --hidden-import PIL.PngImagePlugin \
    --hidden-import PIL._imagingtk \
    --hidden-import PIL._tkinter_finder \
    skkk_student_setup.py

# netguard: консольний застосунок (запускається як root/SYSTEM); One Dir.
pyinstaller --noconfirm --clean --onedir \
    --contents-directory "$LIBS" \
    --specpath build_student \
    --distpath "$OUT" \
    --name netguard \
    netguard.py

# Збираємо спільну теку shared_libs/: зливаємо набори всіх застосунків в одну.
mkdir -p "$OUT/$LIBS"
for app in "${APPS[@]}"; do
    cp -a "$OUT/$app/$LIBS/." "$OUT/$LIBS/"
done

# Ім'я libpython, яке очікує кожен exe, вшите в нього на етапі збірки і може
# бути як libpython3.11.so, так і libpython3.11.so.1.0. Гарантуємо наявність
# обох імен (друге — символьне посилання на справжній файл).
real_lib=""
for f in "$OUT/$LIBS"/libpython*.so*; do
    if [ -e "$f" ] && [ ! -L "$f" ]; then real_lib="$f"; break; fi
done
if [ -n "$real_lib" ]; then
    lib_base="$(basename "$real_lib")"
    case "$lib_base" in
        *.so.1.0) ln -sf "$lib_base" "$OUT/$LIBS/${lib_base%.1.0}" ;;
        *.so)     ln -sf "$lib_base" "$OUT/$LIBS/$lib_base.1.0" ;;
    esac
fi

# Виконувані файли — безпосередньо в dist/student/, поряд зі shared_libs/.
for app in "${APPS[@]}"; do
    mv "$OUT/$app/$app" "$OUT/.${app}_tmp"
done
rm -rf "${APPS[@]/#/$OUT/}"
for app in "${APPS[@]}"; do
    mv "$OUT/.${app}_tmp" "$OUT/$app"
done

# Іконка застосунків.
cp icon.png "$OUT/"
chmod +x "$OUT/student_agent" "$OUT/skkk_student_setup" "$OUT/netguard"

echo
echo "Готово. Тека dist/student/ містить:"
echo "  student_agent, skkk_student_setup, netguard   — виконувані файли"
echo "  shared_libs/                                  — спільні бібліотеки (одна копія)"
echo "  icon.png                                      — іконка"
echo
echo "Розповсюджуйте всю теку dist/student/ цілком."

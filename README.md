# PhotoOrganizer v2.0

Инструмент для сортировки семейного фотоархива по папкам **Год → Месяц → Фото/Видео**.

Поддерживает Google Takeout (ZIP-архивы с JSON-метаданными), Live Photo с iPhone,
определение дат из EXIF, и автоматический поиск скриншотов.

---

## Возможности

| Функция | Описание |
|---|---|
| **Статистика** | Сканирует папку и ZIP-архивы, выводит количество и размер по каждому формату |
| **Сортировка** | Раскладывает фото и видео по папкам `Год/Месяц/Фото` и `Год/Месяц/Видео` |
| **Google Takeout** | Читает точную дату съёмки из `.json` сайдкаров (`photoTakenTime`) |
| **Live Photo** | Видео без JSON берёт дату из парного HEIC/JPG |
| **Скриншоты** | Находит скриншоты в отсортированном архиве и перемещает в `Скриншоты/` |
| **Темы** | Тёмная и светлая тема, переключаются кнопкой |
| **Многопоток** | Настраиваемое количество потоков, пауза и продолжение |

---

## Структура результата

```
Цель/
├── 2015/
│   ├── 01 - Январь/
│   │   ├── Фото/
│   │   ├── Видео/
│   │   └── Скриншоты/      ← после работы вкладки «Скриншоты»
│   └── 06 - Июнь/
│       ├── Фото/
│       └── Видео/
├── 2023/
│   └── ...
└── _Без даты/
    ├── Фото/               ← файлы без определимой даты
    └── Видео/
```

---

## Определение даты (приоритет)

1. **Google Takeout JSON** — `photoTakenTime.timestamp` из `.json` сайдкара (самый точный)
2. **Live Photo** — для `.MOV` без JSON берётся дата парного `.HEIC` / `.JPG`
3. **EXIF exifread** — `DateTimeOriginal` / `DateTimeDigitized` (для RAW, HEIC, JPEG)
4. **EXIF Pillow** — резервный способ для JPEG/TIFF
5. **MP4 mvhd atom** — для видео без JSON и без парного фото
6. **ZIP metadata** — дата из метаданных ZIP-записи (последний резерв)
7. **`_Без даты/`** — если дату определить не удалось

---

## Определение скриншотов

Файл считается скриншотом если:

- Имя начинается с: `Screenshot_`, `Screen Shot`, `Снимок экрана_`, `Скриншот_`,
  `Bildschirmfoto`, `Captura`, `Schermata`, `Capture`
- **ИЛИ** файл `.png` с разрешением, совпадающим с экраном телефона
  (iPhone, Android — более 20 популярных разрешений)

---

## Поддерживаемые форматы

**Фото:** `.jpg` `.jpeg` `.png` `.gif` `.bmp` `.tiff` `.tif` `.heic` `.heif`
`.webp` `.raw` `.cr2` `.nef` `.arw` `.dng` `.orf` `.rw2` `.pef` `.srw`

**Видео:** `.mp4` `.mov` `.avi` `.mkv` `.m4v` `.3gp` `.wmv` `.flv` `.mpg` `.mpeg`

**Архивы:** `.zip` (Google Takeout и другие)

---

## Установка и запуск

### Требования

- Python 3.10 или новее
- Windows / macOS / Linux

### 1. Установка зависимостей

```bat
pip install -r requirements.txt
```

### 2. Запуск

```bat
python photo_organizer_v2.py
```

---

## Сборка .exe для Windows

### Установка PyInstaller

```bat
pip install pyinstaller
```

### Сборка (один файл, без консоли)

```bat
pyinstaller --onefile --windowed --name PhotoOrganizer photo_organizer_v2.py
```

Готовый `.exe` появится в папке `dist/`.

### Сборка с иконкой

```bat
pyinstaller --onefile --windowed --name PhotoOrganizer --icon icon.ico photo_organizer_v2.py
```

### Если что-то не работает в EXE

Иногда PyInstaller не подхватывает скрытые зависимости. Полная команда с явным указанием:

```bat
pyinstaller ^
  --onefile ^
  --windowed ^
  --name PhotoOrganizer ^
  --hidden-import=PIL._tkinter_finder ^
  --hidden-import=exifread ^
  --hidden-import=mutagen.mp4 ^
  --collect-all exifread ^
  --collect-all mutagen ^
  photo_organizer_v2.py
```

### Альтернатива: папка вместо одного файла

Запускается быстрее (нет распаковки при старте):

```bat
pyinstaller --onedir --windowed --name PhotoOrganizer photo_organizer_v2.py
```

Результат в `dist/PhotoOrganizer/` — копируй всю папку целиком.

---

## Зависимости

| Библиотека | Назначение | Обязательна |
|---|---|---|
| `exifread` | EXIF из HEIC, RAW, JPEG | Нет, но без неё RAW/HEIC без даты |
| `Pillow` | EXIF из JPEG/TIFF, размер PNG для скриншотов | Нет, но желательна |
| `mutagen` | Метаданные из MP4/MOV | Нет, есть резервный способ (mvhd) |

Программа работает и без всех трёх библиотек — просто меньше файлов получат дату.

---

## Часто задаваемые вопросы

**В журнале много `[нет даты]` — почему?**

Вероятно файлы без EXIF и без Google JSON. Проверь что галочка «Метаданные Google Takeout»
включена и JSON-файлы лежат рядом с медиафайлами.

**Видео попадают в дату загрузки а не съёмки**

Для Live Photo с iPhone JSON создаётся только для фото (.HEIC), не для видео (.MOV).
Убедись что HEIC и MOV с одинаковым именем лежат в одной папке в ZIP — тогда программа
найдёт дату автоматически.

**Файл оказался в `_Без даты/` — как исправить?**

Открой файл в любом просмотрщике с поддержкой EXIF (FastStone, XnView) и посмотри
дату вручную. Если дата есть визуально но не в EXIF — возможно метаданные повреждены.

**EXE не запускается — ошибка про DLL**

Установи [Microsoft Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe).

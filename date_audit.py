"""
date_audit.py — аудит дат всех медиафайлов в папке
Выводит CSV с колонками:
  путь, имя, расширение,
  exif_DateTimeOriginal, exif_DateTimeDigitized, exif_DateTime,
  pil_DateTimeOriginal, pil_DateTimeDigitized, pil_DateTime,
  mp4_mvhd, mutagen,
  google_json, google_json_path,
  filename_parsed,
  file_mtime, file_ctime,
  итог (лучшая дата), источник

Запуск:
  python date_audit.py "D:\Фото архив" [output.csv]
"""

import os, sys, csv, json, re, struct, zipfile
from datetime import datetime, timedelta
from pathlib import Path

# ── Опциональные библиотеки ───────────────────────────────────────────────────
try:
    import exifread
    EXIFREAD_OK = True
except ImportError:
    EXIFREAD_OK = False
    print("! exifread не установлен — часть EXIF не будет прочитана")

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False
    print("! Pillow не установлен")

try:
    from mutagen.mp4 import MP4 as MutagenMP4
    MUTAGEN_OK = True
except ImportError:
    MUTAGEN_OK = False

# ── Константы ─────────────────────────────────────────────────────────────────
PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".heic", ".heif", ".webp", ".raw", ".cr2", ".nef", ".arw",
    ".dng", ".orf", ".rw2", ".pef", ".srw",
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp",
    ".wmv", ".flv", ".mpg", ".mpeg",
}
VIDEO_META_EXTS = {".mp4", ".mov", ".m4v", ".3gp"}
ALL_MEDIA = PHOTO_EXTS | VIDEO_EXTS

EXIF_FMT   = "%Y:%m:%d %H:%M:%S"
MP4_EPOCH  = datetime(1904, 1, 1)

EXIF_TAGS_ER = {
    "exif_DateTimeOriginal":  "EXIF DateTimeOriginal",
    "exif_DateTimeDigitized": "EXIF DateTimeDigitized",
    "exif_DateTime":          "Image DateTime",
}
EXIF_TAGS_PIL = {
    "pil_DateTimeOriginal":  36867,
    "pil_DateTimeDigitized": 36868,
    "pil_DateTime":          306,
}

# Паттерны для парсинга даты из имени файла
FILENAME_PATTERNS = [
    r'(\d{4})(\d{2})(\d{2})[_\-T](\d{2})(\d{2})(\d{2})',  # 20230615_123456
    r'(\d{4})[_\-](\d{2})[_\-](\d{2})[_\- T](\d{2})[_\-:\.](\d{2})[_\-:\.](\d{2})',  # 2023-06-15_12-34-56
    r'(\d{4})(\d{2})(\d{2})',   # 20230615
    r'(\d{4})[_\-](\d{2})[_\-](\d{2})',  # 2023-06-15
]

EMPTY = ""


# ═══════════════════════════════════════════════════════════════════════════════
#  ФУНКЦИИ ЧТЕНИЯ ДАТ
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_exif_dt(value):
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii", errors="ignore")
        except Exception:
            pass
    s = str(value).strip().strip("\x00").strip()  # убираем нулевые байты
    if not s or s.startswith("0000") or s == "None":
        return EMPTY
    for fmt in (EXIF_FMT, EXIF_FMT[:16]):
        try:
            dt = datetime.strptime(s[:len(fmt)], fmt)
            if dt.year < 1990 or dt.year > datetime.now().year + 1:
                return f"[подозрительная: {dt.strftime('%Y-%m-%d %H:%M:%S')}]"
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return f"[не распознана: {s[:30]}]"


def read_exifread(path):
    """Читает EXIF через exifread. Возвращает dict с тегами."""
    result = {k: EMPTY for k in EXIF_TAGS_ER}
    if not EXIFREAD_OK:
        return result
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
        for col, tag in EXIF_TAGS_ER.items():
            if tag in tags:
                result[col] = _parse_exif_dt(tags[tag])
    except Exception as e:
        result["exif_DateTimeOriginal"] = f"[ошибка: {e}]"
    return result


def read_pil(path):
    """Читает EXIF через Pillow."""
    result = {k: EMPTY for k in EXIF_TAGS_PIL}
    if not PIL_OK:
        return result
    try:
        img = Image.open(path)
        exif = img.getexif() or {}
        raw  = getattr(img, "_getexif", lambda: None)() or {}
        for col, tid in EXIF_TAGS_PIL.items():
            val = exif.get(tid) or raw.get(tid)
            if val:
                result[col] = _parse_exif_dt(val)
    except Exception:
        pass
    return result


def read_mp4_mvhd(path):
    """Читает дату из атома mvhd в MP4/MOV."""
    try:
        with open(path, "rb") as f:
            data = f.read(min(4 << 20, os.path.getsize(path)))
        idx = data.find(b"mvhd")
        if idx == -1:
            return EMPTY
        box = data[idx + 4:]
        version = box[0]
        if version == 0:
            creation_time = struct.unpack(">I", box[4:8])[0]
        elif version == 1:
            creation_time = struct.unpack(">Q", box[4:12])[0]
        else:
            return EMPTY
        if creation_time == 0:
            return EMPTY
        import calendar
        dt = datetime.fromtimestamp(
            calendar.timegm((MP4_EPOCH + timedelta(seconds=creation_time)).timetuple()))
        if dt.year < 1990 or dt.year > datetime.now().year + 1:
            return f"[подозрительная: {dt}]"
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return EMPTY


def read_mutagen(path):
    """Читает метаданные через mutagen."""
    if not MUTAGEN_OK:
        return EMPTY
    try:
        tags = MutagenMP4(path)
        for key in ("©day", "com.apple.quicktime.creationdate"):
            if key in tags:
                val = str(tags[key][0])[:19]
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y"):
                    try:
                        dt = datetime.strptime(val[:len(fmt)], fmt[:len(val)])
                        return dt.strftime("%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
    except Exception:
        pass
    return EMPTY


def read_google_json(path):
    """Ищет и читает Google Takeout JSON сайдкар."""
    p      = Path(path)
    parent = p.parent
    name   = p.name
    stem   = p.stem

    candidates = [
        parent / f"{name}.json",
        parent / f"{stem}.json",
        parent / f"{name}.supplemental-metadata.json",
        parent / f"{stem}.supplemental-metadata.json",
    ]
    # Усечения до 46 символов
    for base in (name, stem):
        if len(base) > 40:
            for cut in range(40, 48):
                candidates.append(parent / f"{base[:cut]}.json")

    for c in candidates:
        if c.exists():
            try:
                with open(c, encoding="utf-8") as f:
                    data = json.load(f)
                for key in ("photoTakenTime", "creationTime"):
                    block = data.get(key)
                    if block:
                        ts = int(block.get("timestamp", 0))
                        if ts > 0:
                            dt = datetime.fromtimestamp(ts)
                            return dt.strftime("%Y-%m-%d %H:%M:%S"), str(c)
            except Exception:
                pass
    return EMPTY, EMPTY


def parse_from_filename(path):
    """Парсит дату из имени файла."""
    name = Path(path).stem
    for pattern in FILENAME_PATTERNS:
        m = re.search(pattern, name)
        if not m:
            continue
        g = m.groups()
        try:
            year, month, day = int(g[0]), int(g[1]), int(g[2])
            if not (1990 <= year <= datetime.now().year): continue
            if not (1 <= month <= 12):                    continue
            if not (1 <= day <= 31):                      continue
            if len(g) >= 6:
                h, mn, sec = int(g[3]), int(g[4]), int(g[5])
                dt = datetime(year, month, day, min(h, 23), min(mn, 59), min(sec, 59))
            else:
                dt = datetime(year, month, day)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return EMPTY


def read_file_times(path):
    """Читает mtime и ctime из файловой системы."""
    try:
        stat  = os.stat(path)
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        ctime = datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M:%S")
        return mtime, ctime
    except Exception:
        return EMPTY, EMPTY


def best_date(row):
    """Определяет лучшую дату из всех источников (приоритет как в основной программе)."""
    priority = [
        ("google_json",            row.get("google_json",            EMPTY)),
        ("exif_DateTimeOriginal",  row.get("exif_DateTimeOriginal",  EMPTY)),
        ("exif_DateTimeDigitized", row.get("exif_DateTimeDigitized", EMPTY)),
        ("pil_DateTimeOriginal",   row.get("pil_DateTimeOriginal",   EMPTY)),
        ("pil_DateTimeDigitized",  row.get("pil_DateTimeDigitized",  EMPTY)),
        ("exif_DateTime",          row.get("exif_DateTime",          EMPTY)),
        ("pil_DateTime",           row.get("pil_DateTime",           EMPTY)),
        ("mp4_mvhd",               row.get("mp4_mvhd",               EMPTY)),
        ("mutagen",                row.get("mutagen",                EMPTY)),
        ("filename_parsed",        row.get("filename_parsed",        EMPTY)),
        ("file_mtime",             row.get("file_mtime",             EMPTY)),
    ]
    for src, val in priority:
        if val and not val.startswith("["):
            return val, src
    return EMPTY, "нет даты"


# ═══════════════════════════════════════════════════════════════════════════════
#  СКАНИРОВАНИЕ
# ═══════════════════════════════════════════════════════════════════════════════

def audit_file(path):
    """Собирает все даты для одного файла."""
    ext = Path(path).suffix.lower()
    row = {
        "path":                  str(Path(path).parent),
        "filename":              Path(path).name,
        "ext":                   ext,
        "exif_DateTimeOriginal":  EMPTY,
        "exif_DateTimeDigitized": EMPTY,
        "exif_DateTime":          EMPTY,
        "pil_DateTimeOriginal":   EMPTY,
        "pil_DateTimeDigitized":  EMPTY,
        "pil_DateTime":           EMPTY,
        "mp4_mvhd":               EMPTY,
        "mutagen":                EMPTY,
        "google_json":            EMPTY,
        "google_json_path":       EMPTY,
        "filename_parsed":        EMPTY,
        "file_mtime":             EMPTY,
        "file_ctime":             EMPTY,
        "best_date":              EMPTY,
        "best_source":            EMPTY,
    }

    # Google JSON (всегда)
    row["google_json"], row["google_json_path"] = read_google_json(path)

    # Даты файловой системы (всегда)
    row["file_mtime"], row["file_ctime"] = read_file_times(path)

    # Парсинг из имени
    row["filename_parsed"] = parse_from_filename(path)

    if ext in PHOTO_EXTS:
        exif = read_exifread(path)
        row.update(exif)
        pil = read_pil(path)
        row.update(pil)

    elif ext in VIDEO_EXTS:
        if ext in VIDEO_META_EXTS:
            row["mp4_mvhd"] = read_mp4_mvhd(path)
            row["mutagen"]  = read_mutagen(path)
        # exifread иногда читает видео
        exif = read_exifread(path)
        row.update(exif)

    row["best_date"], row["best_source"] = best_date(row)
    return row

def _fix_zip_encoding(zf: zipfile.ZipFile) -> zipfile.ZipFile:
    """Исправляет кодировку CP866 в именах файлов старых ZIP-архивов."""
    for info in zf.infolist():
        if not (info.flag_bits & 0x800):
            try:
                info.filename = info.filename.encode("cp437").decode("cp866")
            except Exception:
                pass
    return zf

def scan_folder(root, include_zip=True):
    """Сканирует папку рекурсивно, возвращает генератор строк."""
    for dirpath, _, fnames in os.walk(root):
        for fname in fnames:
            fpath = os.path.join(dirpath, fname)
            ext   = Path(fname).suffix.lower()

            if ext in ALL_MEDIA:
                yield audit_file(fpath)

            elif include_zip and ext == ".zip":
                # Файлы внутри ZIP
                try:
                    with zipfile.ZipFile(fpath, "r") as zf:
                        for info in zf.infolist():
                            if not (info.flag_bits & 0x800):
                                try:
                                    info.filename = info.filename.encode("cp437").decode("cp866")
                                except Exception:
                                    pass
                        for info in zf.infolist():
                            if info.is_dir():
                                continue
                            ie = Path(info.filename).suffix.lower()
                            if ie not in ALL_MEDIA:
                                continue
                            # Для ZIP — только то что можно определить без извлечения
                            dt_zip = EMPTY
                            if info.date_time and info.date_time[0] >= 1990:
                                try:
                                    dt_zip = datetime(*info.date_time[:6]).strftime("%Y-%m-%d %H:%M:%S")
                                except Exception:
                                    pass
                            fname_parsed = parse_from_filename(info.filename)
                            bd = dt_zip or fname_parsed or EMPTY
                            bs = ("zip-metadata" if dt_zip else
                                  "filename_parsed" if fname_parsed else "нет даты")
                            yield {
                                "path":                  f"[ZIP] {fpath}",
                                "filename":              os.path.basename(info.filename),
                                "ext":                   ie,
                                "exif_DateTimeOriginal":  EMPTY,
                                "exif_DateTimeDigitized": EMPTY,
                                "exif_DateTime":          EMPTY,
                                "pil_DateTimeOriginal":   EMPTY,
                                "pil_DateTimeDigitized":  EMPTY,
                                "pil_DateTime":           EMPTY,
                                "mp4_mvhd":               EMPTY,
                                "mutagen":                EMPTY,
                                "google_json":            EMPTY,
                                "google_json_path":       EMPTY,
                                "filename_parsed":        fname_parsed,
                                "file_mtime":             dt_zip,
                                "file_ctime":             EMPTY,
                                "best_date":              bd,
                                "best_source":            bs,
                            }
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

COLUMNS = [
    "path", "filename", "ext",
    "exif_DateTimeOriginal", "exif_DateTimeDigitized", "exif_DateTime",
    "pil_DateTimeOriginal",  "pil_DateTimeDigitized",  "pil_DateTime",
    "mp4_mvhd", "mutagen",
    "google_json", "google_json_path",
    "filename_parsed",
    "file_mtime", "file_ctime",
    "best_date", "best_source",
]

def main():
    if len(sys.argv) < 2:
        root = input("Путь к папке: ").strip().strip('"')
    else:
        root = sys.argv[1].strip('"')

    if not os.path.isdir(root):
        print(f"Папка не найдена: {root}")
        sys.exit(1)

    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"date_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )

    print(f"Сканирую: {root}")
    print(f"Вывод:    {out_path}\n")

    total = ok = no_date = 0
    sources = {}

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, delimiter=";")
        writer.writeheader()

        for row in scan_folder(root):
            writer.writerow(row)
            total += 1
            if row["best_source"] == "нет даты":
                no_date += 1
            else:
                ok += 1
                src = row["best_source"]
                sources[src] = sources.get(src, 0) + 1

            if total % 100 == 0:
                print(f"  {total} файлов обработано...", end="\r")

    print(f"\nГотово! Всего файлов: {total}")
    print(f"  С датой:    {ok}")
    print(f"  Без даты:   {no_date}")
    print(f"\nИсточники дат:")
    for src, cnt in sorted(sources.items(), key=lambda x: -x[1]):
        print(f"  {src:<30} {cnt}")
    print(f"\nCSV сохранён: {out_path}")
    print("Открой в Excel: Данные → Из текста/CSV, разделитель точка с запятой, кодировка UTF-8")


if __name__ == "__main__":
    main()
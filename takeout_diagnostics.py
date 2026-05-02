"""
Диагностика Google Takeout архива.
Запуск: python3 takeout_diagnostics.py /путь/к/папке/с/архивом
"""

import os, sys, json, zipfile
from pathlib import Path
from datetime import datetime
from collections import defaultdict

PHOTO_EXTS = {".jpg",".jpeg",".png",".gif",".bmp",".tiff",".tif",
              ".heic",".heif",".webp",".raw",".cr2",".nef",".arw",
              ".dng",".orf",".rw2",".pef",".srw"}
VIDEO_EXTS = {".mp4",".mov",".avi",".mkv",".m4v",".3gp",".wmv",".flv",".mpg",".mpeg"}
ALL_MEDIA  = PHOTO_EXTS | VIDEO_EXTS


def ts_to_dt(ts):
    try:
        t = int(ts)
        return datetime.fromtimestamp(t) if t > 0 else None
    except Exception:
        return None


def parse_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        taken  = ts_to_dt(d.get("photoTakenTime",  {}).get("timestamp", 0))
        create = ts_to_dt(d.get("creationTime",     {}).get("timestamp", 0))
        title  = d.get("title", "")
        return taken, create, title
    except Exception:
        return None, None, ""


def scan_dir(root):
    """Сканирует папку на диске."""
    media  = {}  # path → ext
    jsons  = {}  # lower_basename → path

    for dirpath, _, files in os.walk(root):
        for fn in files:
            fpath = os.path.join(dirpath, fn)
            ext   = Path(fn).suffix.lower()
            if ext in ALL_MEDIA:
                media[fpath] = ext
            elif ext == ".json":
                jsons[fn.lower()] = fpath

    return media, jsons


def scan_zip(zpath):
    """Сканирует ZIP-архив."""
    media = {}
    jsons = {}
    try:
        with zipfile.ZipFile(zpath) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                fn  = info.filename
                bn  = os.path.basename(fn)
                ext = Path(bn).suffix.lower()
                if ext in ALL_MEDIA:
                    media[fn] = ext
                elif ext == ".json":
                    jsons[bn.lower()] = fn
    except Exception as e:
        print(f"  [ZIP ошибка] {e}")
    return media, jsons


def find_json_for_media(media_name, jsons):
    """Перебирает варианты имён JSON для медиафайла."""
    p      = Path(media_name)
    stem   = p.stem
    suffix = p.suffix

    candidates = [
        f"{media_name}.json",
        f"{stem}.json",
        f"{media_name}.supplemental-metadata.json",
        f"{stem}.supplemental-metadata.json",
    ]

    # Дубликаты: IMG(1).HEIC → IMG.HEIC.json
    import re
    m = re.match(r'^(.*?)(\(\d+\))$', stem)
    if m:
        bs, num = m.group(1), m.group(2)
        candidates += [
            f"{bs}{suffix}.json",
            f"{bs}.json",
            f"{bs}{suffix}{num}.json",
        ]

    # Усечения до 46 символов
    for base in [media_name, stem]:
        if len(base) > 40:
            for cut in range(40, 48):
                candidates.append(f"{base[:cut]}.json")
                candidates.append(f"{base[:cut]}.supplemental-metadata.json"[:50] + ".json")

    # Отредактированные
    for sfx in ("-edited", "-modifié", "-modified"):
        if stem.endswith(sfx):
            orig = stem[:-len(sfx)]
            candidates += [f"{orig}{suffix}.json", f"{orig}.json"]

    for c in candidates:
        if c.lower() in jsons:
            return jsons[c.lower()], c
    return None, None


def run_diagnostics(root):
    print(f"\n{'='*60}")
    print(f"  ДИАГНОСТИКА GOOGLE TAKEOUT")
    print(f"  Путь: {root}")
    print(f"{'='*60}\n")

    # Определяем: папка с ZIP или распакованная папка
    zips   = list(Path(root).glob("*.zip"))
    is_zip = len(zips) > 0

    all_media = {}  # name → ext
    all_jsons = {}  # lower_basename → path/zippath

    if is_zip:
        print(f"Найдено ZIP-архивов: {len(zips)}")
        for zp in zips:
            print(f"  Сканирую {zp.name} ...")
            m, j = scan_zip(str(zp))
            all_media.update(m)
            all_jsons.update(j)
    else:
        print("ZIP не найдены, сканирую папку рекурсивно...")
        all_media, all_jsons = scan_dir(root)

    total_media  = len(all_media)
    total_jsons  = len(all_jsons)
    total_photos = sum(1 for e in all_media.values() if e in PHOTO_EXTS)
    total_videos = sum(1 for e in all_media.values() if e in VIDEO_EXTS)

    print(f"\n{'─'*50}")
    print(f"  Медиафайлов всего : {total_media}")
    print(f"    Фото            : {total_photos}")
    print(f"    Видео           : {total_videos}")
    print(f"  JSON-файлов       : {total_jsons}")
    print(f"{'─'*50}\n")

    # ── Анализ сопоставления ──────────────────────────────────────────────
    matched_photo   = 0
    matched_video   = 0
    no_json_photo   = []
    no_json_video   = []

    # Статистика дат
    stats = defaultdict(int)   # "photo_taken", "creation_only", "no_date"
    diff_big   = []            # файлы где taken и upload расходятся > 7 дней
    examples_no_json = {"photo": [], "video": []}

    for mpath, ext in all_media.items():
        mname = os.path.basename(mpath)
        json_path, matched_pattern = find_json_for_media(mname, all_jsons)
        is_video = ext in VIDEO_EXTS

        if json_path is None:
            if is_video:
                no_json_video.append(mname)
                examples_no_json["video"].append(mname)
            else:
                no_json_photo.append(mname)
                examples_no_json["photo"].append(mname)
            stats["no_json"] += 1
            continue

        # Читаем дату (только из уже просканированных файлов на диске)
        if not is_zip:
            taken, create, title = parse_json(json_path)
        else:
            taken, create, title = None, None, ""  # в ZIP не читаем содержимое в диагностике

        if is_video:
            matched_video += 1
        else:
            matched_photo += 1

        if taken:
            stats["photo_taken"] += 1
            if create and abs((create - taken).days) > 7:
                diff_big.append((mname, taken, create))
        elif create:
            stats["creation_only"] += 1
        else:
            stats["no_date"] += 1

    print(f"{'─'*50}")
    print(f"  СОПОСТАВЛЕНИЕ С JSON")
    print(f"{'─'*50}")
    print(f"  Фото с JSON     : {matched_photo}/{total_photos}")
    print(f"  Видео с JSON    : {matched_video}/{total_videos}")
    print(f"  Фото БЕЗ JSON   : {len(no_json_photo)}")
    print(f"  Видео БЕЗ JSON  : {len(no_json_video)}")

    # Анализ видео без JSON — скорее всего Live Photo
    if no_json_video:
        live_photo_candidates = 0
        truly_no_json = []
        for vname in no_json_video:
            vstem = Path(vname).stem
            # Есть ли рядом фото с таким же именем?
            found_sibling = False
            for pext in PHOTO_EXTS:
                sibling = f"{vstem}{pext}"
                if sibling.lower() in {os.path.basename(k).lower() for k in all_media}:
                    live_photo_candidates += 1
                    found_sibling = True
                    break
            if not found_sibling:
                truly_no_json.append(vname)

        print(f"\n  Из видео без JSON:")
        print(f"    Вероятно Live Photo (есть парное фото) : {live_photo_candidates}")
        print(f"    Обычные видео без JSON                 : {len(truly_no_json)}")
        if truly_no_json[:5]:
            print(f"\n    Примеры обычных видео без JSON:")
            for v in truly_no_json[:5]:
                print(f"      {v}")

    # ── Статистика дат (только для распакованных папок) ───────────────────
    if not is_zip and (stats["photo_taken"] + stats["creation_only"] + stats["no_date"]) > 0:
        print(f"\n{'─'*50}")
        print(f"  СТАТИСТИКА ДАТ (из JSON)")
        print(f"{'─'*50}")
        total_with_json = stats["photo_taken"] + stats["creation_only"] + stats["no_date"]
        print(f"  photoTakenTime (дата съёмки) : {stats['photo_taken']} ({stats['photo_taken']*100//total_with_json}%)")
        print(f"  Только creationTime (загрузка): {stats['creation_only']} ({stats['creation_only']*100//total_with_json}%)")
        print(f"  Вообще без даты в JSON        : {stats['no_date']}")

        if diff_big:
            print(f"\n  Файлы где дата съёмки и загрузки расходятся > 7 дней: {len(diff_big)}")
            print(f"  (первые 5 примеров)")
            for name, taken, create in diff_big[:5]:
                delta = abs((create - taken).days)
                print(f"    {name}")
                print(f"      съёмка:  {taken.strftime('%Y-%m-%d %H:%M')}")
                print(f"      загрузка:{create.strftime('%Y-%m-%d %H:%M')}  (разница {delta} дн.)")

    # ── Примеры файлов без JSON ───────────────────────────────────────────
    if examples_no_json["photo"]:
        print(f"\n  Примеры фото без JSON (первые 10):")
        for n in examples_no_json["photo"][:10]:
            print(f"    {n}")

    # ── Анализ форматов JSON-имён ────────────────────────────────────────
    print(f"\n{'─'*50}")
    print(f"  ФОРМАТЫ JSON-ИМЁН В АРХИВЕ")
    print(f"{'─'*50}")
    fmt_counts = defaultdict(int)
    for jname in all_jsons:
        if ".supplemental-metad" in jname:
            fmt_counts["supplemental-metadata (усечённый)"] += 1
        elif ".supplemental-metadata.json" in jname:
            fmt_counts["supplemental-metadata"] += 1
        elif jname.count(".") >= 2:
            fmt_counts["filename.EXT.json (стандарт)"] += 1
        else:
            fmt_counts["filename.json (без расширения медиа)"] += 1
    for k, v in sorted(fmt_counts.items(), key=lambda x: -x[1]):
        print(f"  {k:45s}: {v}")

    print(f"\n{'='*60}")
    print(f"  ИТОГ")
    print(f"{'='*60}")
    coverage = (matched_photo + matched_video) * 100 // max(total_media, 1)
    print(f"  Покрытие JSON: {coverage}% медиафайлов")
    if len(no_json_video) > matched_video:
        print(f"\n  ⚠️  Много видео без JSON — нормально для Live Photo.")
        print(f"     Для них дата берётся из парного фото (HEIC/JPG).")
        print(f"     Убедитесь что в коде Live Photo поиск идёт ДО mp4/mvhd!")
    print()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else input("Путь к папке с архивом: ").strip()
    if not os.path.isdir(path):
        print("Папка не найдена!")
        sys.exit(1)
    run_diagnostics(path)

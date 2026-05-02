"""
Статистика файлов по расширениям во всех ZIP-архивах.
Запуск: python zip_stats.py "D:\ФОТО АРХИВ\Google исходники"
"""

import os, sys, zipfile
from pathlib import Path
from collections import defaultdict

PHOTO_EXTS = {".jpg",".jpeg",".png",".gif",".bmp",".tiff",".tif",
              ".heic",".heif",".webp",".raw",".cr2",".nef",".arw",
              ".dng",".orf",".rw2",".pef",".srw"}
VIDEO_EXTS = {".mp4",".mov",".avi",".mkv",".m4v",".3gp",".wmv",".flv",".mpg",".mpeg"}

def run(root):
    zips = sorted(Path(root).glob("*.zip"))
    if not zips:
        print("ZIP-архивы не найдены!")
        return

    counts   = defaultdict(int)  # ext → count
    sizes    = defaultdict(int)  # ext → bytes
    zip_info = []                # (zip_name, total_files, total_bytes)

    for zpath in zips:
        zname     = zpath.name
        z_files   = 0
        z_bytes   = 0
        try:
            with zipfile.ZipFile(zpath) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    ext = Path(info.filename).suffix.lower()
                    counts[ext] += 1
                    sizes[ext]  += info.file_size
                    z_files     += 1
                    z_bytes     += info.file_size
        except Exception as e:
            print(f"  [ошибка] {zname}: {e}")
            continue
        zip_info.append((zname, z_files, z_bytes))

    def fmt_size(b):
        if b >= 1_073_741_824: return f"{b/1_073_741_824:.1f} ГБ"
        if b >= 1_048_576:     return f"{b/1_048_576:.1f} МБ"
        return f"{b/1024:.1f} КБ"

    # ── По архивам ───────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  АРХИВЫ ({len(zips)} шт.)")
    print(f"{'='*60}")
    total_all_files = 0
    total_all_bytes = 0
    for zname, zf, zb in zip_info:
        print(f"  {zname:<45}  {zf:>5} файлов  {fmt_size(zb):>8}")
        total_all_files += zf
        total_all_bytes += zb
    print(f"  {'ИТОГО':<45}  {total_all_files:>5} файлов  {fmt_size(total_all_bytes):>8}")

    # ── По расширениям ───────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  ВСЕ РАСШИРЕНИЯ")
    print(f"{'='*60}")
    for ext, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        tag = ""
        if ext in PHOTO_EXTS: tag = "← фото"
        elif ext in VIDEO_EXTS: tag = "← видео"
        elif ext == ".json": tag = "← метаданные"
        print(f"  {ext or '(без расш.)':<12}  {cnt:>6}  {fmt_size(sizes[ext]):>9}  {tag}")

    # ── Сводка фото/видео/прочее ─────────────────────────────────────────
    photo_count = sum(counts[e] for e in counts if e in PHOTO_EXTS)
    video_count = sum(counts[e] for e in counts if e in VIDEO_EXTS)
    json_count  = counts.get(".json", 0)
    other_count = total_all_files - photo_count - video_count - json_count

    photo_bytes = sum(sizes[e] for e in sizes if e in PHOTO_EXTS)
    video_bytes = sum(sizes[e] for e in sizes if e in VIDEO_EXTS)

    print(f"\n{'='*60}")
    print(f"  СВОДКА")
    print(f"{'='*60}")
    print(f"  Фото              : {photo_count:>6}  ({fmt_size(photo_bytes)})")
    print(f"  Видео             : {video_count:>6}  ({fmt_size(video_bytes)})")
    print(f"  JSON-метаданные   : {json_count:>6}")
    print(f"  Прочее            : {other_count:>6}")
    print(f"  {'─'*40}")
    print(f"  Всего файлов      : {total_all_files:>6}  ({fmt_size(total_all_bytes)})")
    print(f"  Медиа всего       : {photo_count+video_count:>6}")
    print()

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else input("Путь к папке с ZIP: ").strip().strip('"')
    if not os.path.isdir(path):
        print("Папка не найдена!")
        sys.exit(1)
    run(path)
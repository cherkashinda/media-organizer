"""
PhotoOrganizer — сортировщик фотографий по папкам Год → Месяц
Поддерживает: JPG, PNG, HEIC, RAW, MP4, MOV + ZIP + Google Takeout JSON
Многопоточная обработка, пауза/продолжение, перезапись/пропуск/копия.
Видеофайлы: читает дату из MP4/MOV атома mvhd.
Файлы без определённой даты — в отдельную папку.

ИСПРАВЛЕНО (2025-05):
  - _find_google_json: полная поддержка всех форматов именования Google Takeout
    * стандарт: IMG.HEIC.json
    * без расширения: IMG.json
    * усечение на 46 символах
    * supplemental-metadata с усечением до 46 символов
    * дубликаты: IMG(1).HEIC → ищем IMG.HEIC.json и IMG.HEIC(1).json
    * отредактированные: IMG-edited.HEIC → ищем IMG.HEIC.json
  - ZIP: строим индекс всех JSON в архиве для надёжного сопоставления
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os, shutil, threading, zipfile, tempfile, json, re, struct
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import exifread
    EXIFREAD_AVAILABLE = True
except ImportError:
    EXIFREAD_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    from mutagen.mp4 import MP4 as MutagenMP4
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False

# ── Расширения ────────────────────────────────────────────────────────────────
PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".heic", ".heif", ".webp", ".raw", ".cr2", ".nef", ".arw",
    ".dng", ".orf", ".rw2", ".pef", ".srw",
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".wmv",
    ".flv", ".mpg", ".mpeg",
}
VIDEO_META_EXTS = {".mp4", ".mov", ".m4v", ".3gp"}

MONTH_NAMES = {
    1: "01 - Январь",  2: "02 - Февраль", 3: "03 - Март",
    4: "04 - Апрель",  5: "05 - Май",      6: "06 - Июнь",
    7: "07 - Июль",    8: "08 - Август",   9: "09 - Сентябрь",
    10: "10 - Октябрь",11: "11 - Ноябрь", 12: "12 - Декабрь",
}

UNKNOWN_FOLDER = "_Без даты"

EXIF_DATE_TAGS_ER  = ["EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"]
EXIF_DATE_TAGS_PIL = {36867: "DateTimeOriginal", 36868: "DateTimeDigitized", 306: "DateTime"}
EXIF_FMT           = "%Y:%m:%d %H:%M:%S"
MP4_EPOCH          = datetime(1904, 1, 1)

_fs_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
#  ОПРЕДЕЛЕНИЕ ДАТЫ
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_exif_dt(value):
    s = str(value).strip()
    if not s or s.startswith("0000"):
        return None
    try:
        return datetime.strptime(s, EXIF_FMT)
    except ValueError:
        try:
            return datetime.strptime(s[:19], EXIF_FMT)
        except ValueError:
            return None


def _exif_exifread(path):
    if not EXIFREAD_AVAILABLE:
        return None, None
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False)
        for t in EXIF_DATE_TAGS_ER:
            if t in tags:
                dt = _parse_exif_dt(tags[t])
                if dt:
                    return dt, t
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
        for t in EXIF_DATE_TAGS_ER:
            if t in tags:
                dt = _parse_exif_dt(tags[t])
                if dt:
                    return dt, t
    except Exception:
        pass
    return None, None


def _exif_pil(path):
    if not PIL_AVAILABLE:
        return None, None
    try:
        img = Image.open(path)
        exif = img.getexif()
        if exif:
            for tid, tn in EXIF_DATE_TAGS_PIL.items():
                if tid in exif:
                    dt = _parse_exif_dt(exif[tid])
                    if dt:
                        return dt, f"PIL:{tn}"
        raw = getattr(img, "_getexif", lambda: None)()
        if raw:
            for tid, tn in EXIF_DATE_TAGS_PIL.items():
                if tid in raw:
                    dt = _parse_exif_dt(raw[tid])
                    if dt:
                        return dt, f"PIL:{tn}"
    except Exception:
        pass
    return None, None


def _read_mp4_mvhd(path):
    try:
        file_size = os.path.getsize(path)
        read_size = min(4 * 1024 * 1024, file_size)
        with open(path, "rb") as f:
            data = f.read(read_size)
        idx = data.find(b"mvhd")
        if idx == -1:
            return None
        box = data[idx + 4:]
        if len(box) < 8:
            return None
        version = box[0]
        if version == 0:
            creation_time = struct.unpack(">I", box[4:8])[0]
        elif version == 1:
            if len(box) < 16:
                return None
            creation_time = struct.unpack(">Q", box[4:12])[0]
        else:
            return None
        if creation_time == 0:
            return None
        dt_utc = MP4_EPOCH + timedelta(seconds=creation_time)
        import calendar
        utc_ts = calendar.timegm(dt_utc.timetuple())
        return datetime.fromtimestamp(utc_ts)
    except Exception:
        return None


def _read_video_date_mutagen(path):
    if not MUTAGEN_AVAILABLE:
        return None, None
    try:
        tags = MutagenMP4(path)
        for key in ("©day", "com.apple.quicktime.creationdate"):
            if key in tags:
                val = str(tags[key][0])[:19]
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y"):
                    try:
                        return datetime.strptime(val[:len(fmt)], fmt[:len(val)]), f"mutagen:{key}"
                    except ValueError:
                        continue
    except Exception:
        pass
    return None, None


def _read_video_date(path):
    ext = Path(path).suffix.lower()
    if ext in VIDEO_META_EXTS:
        dt = _read_mp4_mvhd(path)
        if dt:
            return dt, "MP4/mvhd"
        dt, src = _read_video_date_mutagen(path)
        if dt:
            return dt, src
    dt, src = _exif_exifread(path)
    if dt:
        return dt, src
    return None, None


# ── Google Takeout JSON ───────────────────────────────────────────────────────

# Локализованные суффиксы "-edited" в разных языках Google Photos
_EDITED_SUFFIXES = ("-edited", "-modifié", "-modified", "-bearbeitet",
                    "-bewerkt", "-editada", "-editado", "-modificato")

# Предельная длина имени файла ДО добавления ".json" в Google Takeout
_GOOGLE_MAX_NAME_LEN = 46


def _google_json_candidates(name: str) -> list:
    """
    Возвращает список строк-кандидатов (без родительской папки и без .json),
    которые Google мог использовать как имя JSON-сайдкара для медиафайла `name`.

    Порядок: от наиболее вероятного к наименее вероятному.
    """
    p = Path(name)
    stem   = p.stem    # "IMG_2414" / "IMG_2414(1)" / "IMG_2414-edited"
    suffix = p.suffix  # ".HEIC"

    bases = []   # кандидаты для части «до .json»

    def _with_supplemental(base):
        """Добавляет base и base.supplemental-metadata (с усечением)."""
        bases.append(base)
        sm = f"{base}.supplemental-metadata"
        # Google усекает имя до _GOOGLE_MAX_NAME_LEN символов
        if len(sm) <= _GOOGLE_MAX_NAME_LEN:
            bases.append(sm)
        else:
            # Пробуем все разумные длины усечения вокруг лимита
            for cut in range(max(35, _GOOGLE_MAX_NAME_LEN - 5),
                             _GOOGLE_MAX_NAME_LEN + 2):
                bases.append(sm[:cut])

    # 1. Стандарт: IMG_2414.HEIC
    _with_supplemental(name)

    # 2. Без расширения: IMG_2414
    _with_supplemental(stem)

    # 3. Усечение полного имени файла до 46 символов
    if len(name) > _GOOGLE_MAX_NAME_LEN:
        for cut in range(_GOOGLE_MAX_NAME_LEN - 1, _GOOGLE_MAX_NAME_LEN + 2):
            _with_supplemental(name[:cut])

    # 4. Дубликаты: IMG_2414(1).HEIC  →  ищем JSON от оригинала IMG_2414.HEIC
    m = re.match(r'^(.*?)(\(\d+\))$', stem)
    if m:
        base_stem, num = m.group(1), m.group(2)
        orig_name = f"{base_stem}{suffix}"
        _with_supplemental(orig_name)            # IMG_2414.HEIC → JSON оригинала
        _with_supplemental(base_stem)            # IMG_2414
        _with_supplemental(f"{orig_name}{num}")  # IMG_2414.HEIC(1) — альтернативный Google формат
        _with_supplemental(f"{base_stem}{num}{suffix}")  # IMG_2414(1).HEIC — ещё один вариант

    # 5. Отредактированные: IMG_2414-edited.HEIC  →  ищем IMG_2414.HEIC.json
    for edited_sfx in _EDITED_SUFFIXES:
        if stem.endswith(edited_sfx):
            orig_stem = stem[:-len(edited_sfx)]
            orig_name = f"{orig_stem}{suffix}"
            _with_supplemental(orig_name)        # IMG_2414.HEIC
            _with_supplemental(orig_stem)        # IMG_2414
            break

    # Убираем дубли, сохраняем порядок
    seen = set()
    result = []
    for b in bases:
        if b not in seen:
            seen.add(b)
            result.append(b)
    return result


def _find_google_json(path: str) -> str | None:
    """
    Ищет JSON-сайдкар Google Takeout для медиафайла на диске.
    Возвращает путь к JSON или None.
    """
    p      = Path(path)
    parent = p.parent
    name   = p.name

    for base in _google_json_candidates(name):
        candidate = parent / f"{base}.json"
        if candidate.exists():
            return str(candidate)
    return None


def _find_google_json_in_index(media_name: str,
                                json_index: dict[str, str]) -> str | None:
    """
    Ищет JSON-сайдкар в индексе ZIP-файла.

    json_index: dict { lowercase_json_basename → full_path_in_zip }
    Возвращает full_path_in_zip или None.
    """
    for base in _google_json_candidates(media_name):
        key = f"{base}.json".lower()
        if key in json_index:
            return json_index[key]
    return None


def _google_json_date(json_path: str) -> datetime | None:
    """
    Читает дату съёмки из JSON-сайдкара Google Takeout.
    Приоритет: photoTakenTime > creationTime.
    Возвращает datetime (локальное время) или None.
    """
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        # photoTakenTime — дата съёмки (то, что нам нужно)
        # creationTime  — дата загрузки в Google Photos (запасной вариант)
        for key in ("photoTakenTime", "creationTime"):
            block = data.get(key)
            if block:
                ts = int(block.get("timestamp", 0))
                if ts > 0:
                    return datetime.fromtimestamp(ts)
    except Exception:
        pass
    return None


def get_best_date(path, use_google=True):
    """
    Возвращает (datetime | None, source_str).
    None → файл пойдёт в _Без даты.
    """
    # 1. Google Takeout JSON (наивысший приоритет)
    if use_google:
        jp = _find_google_json(path)
        if jp:
            dt = _google_json_date(jp)
            if dt:
                return dt, "Google JSON"

    ext      = Path(path).suffix.lower()
    is_video = ext in VIDEO_EXTS

    if is_video:
        dt, src = _read_video_date(path)
        if dt:
            return dt, src
    else:
        dt, src = _exif_exifread(path)
        if dt:
            return dt, src
        dt, src = _exif_pil(path)
        if dt:
            return dt, src

    # Для видео без даты — ищем Live Photo (парное фото)
    if is_video:
        # Сначала Live Photo — парное фото рядом на диске (или извлечённое из ZIP)
        parent = Path(path).parent
        stem   = Path(path).stem
        for photo_ext in PHOTO_EXTS:
            sibling = parent / (stem + photo_ext)
            if sibling.exists():
                dt, src = get_best_date(str(sibling), use_google=use_google)
                if dt:
                    return dt, f"Live Photo ({sibling.name})"

        # Только если парного фото нет — читаем mvhd
        dt, src = _read_video_date(path)
        if dt:
            return dt, src

    return None, "no-date"


def _zip_entry_date(info):
    if info.date_time and info.date_time[0] >= 1980:
        try:
            return datetime(*info.date_time[:6]), "zip-metadata"
        except Exception:
            pass
    return None, "no-date"


def _build_zip_json_index(zf: zipfile.ZipFile) -> dict[str, str]:
    """
    Строит индекс JSON-файлов в ZIP.
    Ключ: basename в нижнем регистре, значение: полный путь внутри ZIP.
    Это позволяет надёжно сопоставлять JSON-сайдкары при любом именовании.
    """
    index = {}
    for info in zf.infolist():
        fn = info.filename
        if fn.lower().endswith(".json") and not info.is_dir():
            bn = os.path.basename(fn).lower()
            if bn:
                index[bn] = fn
    return index


# ═══════════════════════════════════════════════════════════════════════════════
#  РАЗМЕЩЕНИЕ ФАЙЛА
# ═══════════════════════════════════════════════════════════════════════════════

def place_file(src_path, dst_root, dt, mode, conflict):
    fname = os.path.basename(src_path)
    base, ext = os.path.splitext(fname)
    media_sub = "Видео" if ext.lower() in VIDEO_EXTS else "Фото"

    if dt is None:
        target_dir = os.path.join(dst_root, UNKNOWN_FOLDER, media_sub)
    else:
        target_dir = os.path.join(dst_root, str(dt.year), MONTH_NAMES[dt.month], media_sub)

    with _fs_lock:
        os.makedirs(target_dir, exist_ok=True)
        dest_path = os.path.join(target_dir, fname)

        if os.path.exists(dest_path):
            if conflict == "skip":
                return os.path.relpath(dest_path, dst_root), "skip"
            elif conflict == "copy":
                n = 1
                while True:
                    candidate = os.path.join(target_dir, f"{base}_copy_{n}{ext}")
                    if not os.path.exists(candidate):
                        dest_path = candidate
                        break
                    n += 1

        open(dest_path, "ab").close()

    try:
        if mode == "move":
            shutil.copy2(src_path, dest_path)
            if os.path.getsize(dest_path) == os.path.getsize(src_path):
                os.remove(src_path)
            else:
                raise IOError("Размер файла после копирования не совпадает")
        else:
            shutil.copy2(src_path, dest_path)
    except Exception:
        try:
            os.remove(dest_path)
        except Exception:
            pass
        raise

    return os.path.relpath(dest_path, dst_root), "ok"


# ═══════════════════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════════════════
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Сортировщик фотографий")
        self.resizable(True, True)
        self.configure(bg="#1e1e2e")
        self._running  = False
        self._paused   = False
        self._pause_ev = threading.Event()
        self._pause_ev.set()
        self._build_ui()
        self._center()

    def _center(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(780, sw - 40)
        h = min(660, sh - 60)
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        self.minsize(620, 520)

    def _build_ui(self):
        BG    = "#1e1e2e"; CARD  = "#2a2a3e"; ACC   = "#7c5cfc"
        FG    = "#e0e0f0"; MUTED = "#888aaa"; GREEN = "#3ddc97"
        YELLOW= "#f4c430"; BLUE  = "#5bc8f5"; RED   = "#ff6b6b"
        ORANGE= "#ffaa44"
        self._C = dict(BG=BG,CARD=CARD,ACC=ACC,FG=FG,MUTED=MUTED,
                       GREEN=GREEN,YELLOW=YELLOW,BLUE=BLUE,RED=RED,ORANGE=ORANGE)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame",         background=BG)
        style.configure("Card.TFrame",    background=CARD)
        style.configure("TLabel",         background=BG,   foreground=FG,   font=("Segoe UI", 9))
        style.configure("Card.TLabel",    background=CARD, foreground=FG,   font=("Segoe UI", 9))
        style.configure("Muted.TLabel",   background=CARD, foreground=MUTED,font=("Segoe UI", 8))
        style.configure("TEntry",         fieldbackground="#13132a", foreground=FG,
                        insertcolor=FG, font=("Segoe UI", 9))
        style.configure("Accent.TButton", background=ACC, foreground="white",
                        font=("Segoe UI", 10,"bold"), borderwidth=0, relief="flat", padding=8)
        style.map("Accent.TButton",
                  background=[("active","#9370ff"),("disabled","#444466")])
        style.configure("Pause.TButton",  background="#444466", foreground="white",
                        font=("Segoe UI", 10,"bold"), borderwidth=0, relief="flat", padding=8)
        style.map("Pause.TButton",
                  background=[("active","#6666aa"),("disabled","#333344")])
        style.configure("TProgressbar",   troughcolor=CARD, background=ACC, borderwidth=0)

        outer  = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vscroll= ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        canvas.pack(side="left",  fill="both", expand=True)
        inner  = tk.Frame(canvas, bg=BG)
        wid    = canvas.create_window((0,0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(wid, width=e.width))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1*(e.delta/120)),"units"))
        canvas.bind_all("<Button-4>",   lambda e: canvas.yview_scroll(-1,"units"))
        canvas.bind_all("<Button-5>",   lambda e: canvas.yview_scroll(1,"units"))

        root = tk.Frame(inner, bg=BG, padx=16, pady=12)
        root.pack(fill="both", expand=True)

        tk.Label(root, text="Сортировщик фотографий",
                 bg=BG, fg=FG, font=("Segoe UI",13,"bold")).pack(anchor="w")
        tk.Label(root, text="Медиафайлы и ZIP по папкам  Год → Месяц",
                 bg=BG, fg=MUTED, font=("Segoe UI",8)).pack(anchor="w", pady=(1,6))

        lf = tk.Frame(root, bg=BG); lf.pack(anchor="w", pady=(0,8))
        for lib, ok, note in [
            ("exifread", EXIFREAD_AVAILABLE, "HEIC/RAW/JPEG"),
            ("Pillow",   PIL_AVAILABLE,      "JPEG/TIFF"),
            ("mutagen",  MUTAGEN_AVAILABLE,  "MP4/MOV"),
        ]:
            tk.Label(lf, text=f"{'✓' if ok else '✗'} {lib} ({note})",
                     bg=BG, fg=GREEN if ok else YELLOW,
                     font=("Segoe UI",8)).pack(side="left", padx=(0,12))

        self.src_var = tk.StringVar()
        self.dst_var = tk.StringVar()
        self._path_row(root, "Источник", self.src_var)
        self._path_row(root, "Цель",     self.dst_var)

        opts = tk.Frame(root, bg=CARD, padx=12, pady=10)
        opts.pack(fill="x", pady=(0,8))

        def cb(p, text, var, fg=FG, bold=False):
            return tk.Checkbutton(p, text=text, variable=var,
                                  bg=CARD, fg=fg, selectcolor="#13132a",
                                  activebackground=CARD, activeforeground=fg,
                                  font=("Segoe UI",9,"bold") if bold else ("Segoe UI",9))

        def rb(p, text, var, val, fg=FG):
            return tk.Radiobutton(p, text=text, variable=var, value=val,
                                  bg=CARD, fg=fg, selectcolor="#13132a",
                                  activebackground=CARD, activeforeground=fg,
                                  font=("Segoe UI",9))

        r0 = tk.Frame(opts, bg=CARD); r0.pack(fill="x", pady=(0,4))
        tk.Label(r0, text="Режим:", bg=CARD, fg=FG, font=("Segoe UI",9)).pack(side="left", padx=(0,6))
        self.mode_var = tk.StringVar(value="copy")
        rb(r0,"Копировать",  self.mode_var,"copy").pack(side="left",padx=(0,12))
        rb(r0,"Перемещать",  self.mode_var,"move").pack(side="left",padx=(0,20))
        self.video_var = tk.BooleanVar(value=True)
        self.zip_var   = tk.BooleanVar(value=True)
        cb(r0,"Видео",self.video_var).pack(side="left",padx=(0,10))
        cb(r0,"ZIP",  self.zip_var  ).pack(side="left")

        r1 = tk.Frame(opts, bg=CARD); r1.pack(fill="x", pady=(4,4))
        tk.Label(r1, text="Если файл уже есть:", bg=CARD, fg=FG,
                 font=("Segoe UI",9)).pack(side="left",padx=(0,8))
        self.conflict_var = tk.StringVar(value="skip")
        rb(r1,"Пропустить",         self.conflict_var,"skip"     ).pack(side="left",padx=(0,12))
        rb(r1,"Перезаписать",       self.conflict_var,"overwrite").pack(side="left",padx=(0,12))
        rb(r1,"Создать копию (_copy_N)", self.conflict_var,"copy").pack(side="left")

        r2 = tk.Frame(opts, bg=CARD); r2.pack(fill="x", pady=(4,4))
        tk.Label(r2, text="Потоков:", bg=CARD, fg=FG,
                 font=("Segoe UI",9)).pack(side="left",padx=(0,6))
        cpu = os.cpu_count() or 4
        self.threads_var = tk.IntVar(value=min(cpu,8))
        tk.Spinbox(r2, from_=1, to=cpu, textvariable=self.threads_var,
                   width=4, bg="#13132a", fg=FG, buttonbackground=CARD,
                   insertbackground=FG, font=("Segoe UI",9), relief="flat"
                   ).pack(side="left")
        tk.Label(r2, text=f"(макс. {cpu})",
                 bg=CARD, fg=MUTED, font=("Segoe UI",8)).pack(side="left",padx=(4,16))
        self.show_src_var   = tk.BooleanVar(value=True)
        self.zip_delete_var = tk.BooleanVar(value=False)
        cb(r2,"Источник даты", self.show_src_var  ).pack(side="left",padx=(0,10))
        cb(r2,"Удалять ZIP",   self.zip_delete_var).pack(side="left")

        r3 = tk.Frame(opts, bg=CARD); r3.pack(fill="x", pady=(6,0))
        self.google_var = tk.BooleanVar(value=True)
        cb(r3,"Метаданные Google Takeout  (photoTakenTime из .json)",
           self.google_var, fg=BLUE, bold=True).pack(side="left")

        pf = tk.Frame(root, bg=CARD, padx=12, pady=8)
        pf.pack(fill="x", pady=(0,8))
        top = tk.Frame(pf, bg=CARD); top.pack(fill="x")
        self.prog_label  = tk.Label(top, text="Готов к работе",
                                    bg=CARD, fg=FG, font=("Segoe UI",9))
        self.prog_label.pack(side="left")
        self.stats_label = tk.Label(top, text="",
                                    bg=CARD, fg=MUTED, font=("Segoe UI",8))
        self.stats_label.pack(side="right")
        self.progress = ttk.Progressbar(pf, mode="determinate", style="TProgressbar")
        self.progress.pack(fill="x", pady=(4,0))

        lf2 = tk.Frame(root, bg=CARD, padx=12, pady=8)
        lf2.pack(fill="both", expand=True, pady=(0,8))
        tk.Label(lf2, text="Журнал", bg=CARD, fg=MUTED,
                 font=("Segoe UI",8)).pack(anchor="w")
        self.log_widget = tk.Text(
            lf2, height=7, bg="#13132a", fg="#b0b2d0",
            font=("Consolas",8), bd=0, padx=4, pady=4,
            state="disabled", wrap="word")
        self.log_widget.tag_configure("warn",    foreground=YELLOW)
        self.log_widget.tag_configure("ok",      foreground=GREEN)
        self.log_widget.tag_configure("skip",    foreground=MUTED)
        self.log_widget.tag_configure("nodate",  foreground=ORANGE)
        self.log_widget.tag_configure("google",  foreground=BLUE)
        self.log_widget.tag_configure("err",     foreground=RED)
        self.log_widget.tag_configure("head",    foreground="#a0a8d0",
                                                 font=("Consolas",8,"bold"))
        self.log_widget.tag_configure("pause",   foreground=YELLOW,
                                                 font=("Consolas",8,"bold"))
        sl = ttk.Scrollbar(lf2, command=self.log_widget.yview)
        self.log_widget.configure(yscrollcommand=sl.set)
        sl.pack(side="right", fill="y")
        self.log_widget.pack(fill="both", expand=True)

        br = tk.Frame(root, bg=BG); br.pack(fill="x", pady=(0,4))
        self.btn_start = ttk.Button(br, text="▶  Запустить",
                                    style="Accent.TButton", command=self._start)
        self.btn_start.pack(side="left", fill="x", expand=True, ipady=2)
        self.btn_pause = ttk.Button(br, text="⏸  Пауза",
                                    style="Pause.TButton", command=self._toggle_pause,
                                    state="disabled")
        self.btn_pause.pack(side="left", fill="x", expand=True, ipady=2, padx=(6,0))

    def _path_row(self, parent, label, var):
        C = self._C
        f = tk.Frame(parent, bg=C["CARD"], padx=12, pady=8)
        f.pack(fill="x", pady=(0,6))
        tk.Label(f, text=label, bg=C["CARD"], fg=C["FG"],
                 font=("Segoe UI",9)).pack(anchor="w", pady=(0,4))
        row = tk.Frame(f, bg=C["CARD"]); row.pack(fill="x")
        ttk.Entry(row, textvariable=var, style="TEntry").pack(
            side="left", fill="x", expand=True, ipady=4)
        tk.Button(row, text="Обзор...", bg=C["ACC"], fg="white", relief="flat",
                  font=("Segoe UI",8), padx=8,
                  activebackground="#9370ff", activeforeground="white",
                  command=lambda v=var: self._browse(v)).pack(
            side="left", padx=(6,0), ipady=4)

    def _browse(self, var):
        path = filedialog.askdirectory(title="Выберите папку")
        if path:
            var.set(path)

    def _log(self, msg, tag=""):
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", msg+"\n", tag)
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _log_clear(self):
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0","end")
        self.log_widget.configure(state="disabled")

    def _ui(self, fn):
        self.after(0, fn)

    def _start(self):
        if self._running:
            return
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("Ошибка","Выберите корректную папку-источник.")
            return
        if not dst:
            messagebox.showerror("Ошибка","Выберите папку назначения.")
            return
        if os.path.abspath(src) == os.path.abspath(dst):
            messagebox.showerror("Ошибка","Источник и цель не могут совпадать.")
            return
        self._log_clear()
        self._running = True
        self._paused  = False
        self._pause_ev.set()
        self.btn_start.configure(state="disabled")
        self.btn_pause.configure(state="normal", text="⏸  Пауза")
        threading.Thread(target=self._worker, args=(src,dst), daemon=True).start()

    def _toggle_pause(self):
        if not self._running:
            return
        if self._paused:
            self._paused = False
            self._pause_ev.set()
            self.btn_pause.configure(text="⏸  Пауза")
            self._ui(lambda: self._log("▶  Продолжение...","pause"))
        else:
            self._paused = True
            self._pause_ev.clear()
            self.btn_pause.configure(text="▶  Продолжить")
            self._ui(lambda: self._log("⏸  Пауза...","pause"))

    def _done(self):
        def _():
            self._running = False
            self._paused  = False
            self._pause_ev.set()
            self.btn_start.configure(state="normal")
            self.btn_pause.configure(state="disabled", text="⏸  Пауза")
        self.after(0, _)

    # ═══════════════════════════════════════════════════════════════════════════
    #  РАБОЧИЙ ПОТОК
    # ═══════════════════════════════════════════════════════════════════════════
    def _worker(self, src, dst):
        mode       = self.mode_var.get()
        inc_video  = self.video_var.get()
        inc_zip    = self.zip_var.get()
        del_zip    = self.zip_delete_var.get()
        show_src   = self.show_src_var.get()
        use_google = self.google_var.get()
        n_threads  = max(1, self.threads_var.get())
        conflict   = self.conflict_var.get()

        extensions = PHOTO_EXTS | (VIDEO_EXTS if inc_video else set())

        self._ui(lambda: self._log("Сканирование...","head"))

        media_files = []
        zip_tasks   = []

        for root_dir, _, fnames in os.walk(src):
            for fname in fnames:
                fpath = os.path.join(root_dir, fname)
                ext   = Path(fname).suffix.lower()
                if ext in extensions:
                    media_files.append(fpath)
                elif inc_zip and ext == ".zip":
                    try:
                        with zipfile.ZipFile(fpath, "r") as zf:
                            entries = [
                                info for info in zf.infolist()
                                if not info.is_dir()
                                and Path(info.filename).suffix.lower() in extensions
                            ]
                        if entries:
                            zip_tasks.append((fpath, entries))
                    except Exception:
                        pass

        total_zip_files = sum(len(e) for _,e in zip_tasks)
        total = len(media_files) + total_zip_files

        if total == 0:
            self._ui(lambda: self._log("Файлы не найдены.","warn"))
            self._done()
            return

        self._ui(lambda: self._log(
            f"Файлов: {len(media_files)} на диске + {total_zip_files} в {len(zip_tasks)} ZIP"
            f"  |  Итого: {total}  |  Потоков: {n_threads}\n","head"))

        lock = threading.Lock()
        stats = {"ok":0,"skip":0,"err":0,"nodate":0,"google":0,"done":0}

        def _inc(**kw):
            with lock:
                for k,v in kw.items():
                    stats[k] += v

        def _tick():
            with lock:
                done=stats["done"]; ok=stats["ok"]
                sk=stats["skip"];   err=stats["err"]
                nd=stats["nodate"]
            pct = int(done/total*100)
            self._ui(lambda p=pct,d=done,o=ok,s=sk,e=err,n=nd: (
                self.progress.configure(value=p),
                self.prog_label.configure(text=f"Обработано: {d}/{total}  ({p}%)"),
                self.stats_label.configure(
                    text=f"ok:{o}  пропущено:{s}  без даты:{n}  ошибок:{e}")
            ))

        def process(fpath, get_date_fn, op_mode):
            self._pause_ev.wait()
            fname = os.path.basename(fpath)
            try:
                dt, date_src = get_date_fn()
                is_google    = (date_src == "Google JSON")
                no_date      = (dt is None)

                rel, result  = place_file(fpath, dst, dt, op_mode, conflict)

                if result == "skip":
                    _inc(skip=1, done=1)
                    _tick()
                    if show_src:
                        self._ui(lambda r=rel:
                                 self._log(f"  --  {r}  [пропущен]","skip"))
                    return "skip"

                if no_date:
                    _inc(nodate=1, ok=1, done=1)
                    _tick()
                    self._ui(lambda r=rel, s=date_src:
                             self._log(f"  ??  {r}  [нет даты]","nodate"))
                    return "ok"

                _inc(ok=1, done=1, google=1 if is_google else 0)
                _tick()

                if show_src:
                    tag = "google" if is_google else "ok"
                    self._ui(lambda r=rel, s=date_src, t=tag:
                             self._log(f"  ok  {r}  [{s}]", t))
                else:
                    self._ui(lambda r=rel: self._log(f"  ok  {r}","ok"))
                return "ok"

            except Exception as e:
                _inc(err=1, done=1)
                _tick()
                self._ui(lambda n=fname, ex=e:
                         self._log(f"  ERR {n}: {ex}","err"))
                return "err"

        # ── Медиафайлы ───────────────────────────────────────────────────────
        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futures = {
                pool.submit(
                    process,
                    fp,
                    lambda p=fp: get_best_date(p, use_google=use_google),
                    mode
                ): fp
                for fp in media_files
            }
            for _ in as_completed(futures):
                pass

        # ── ZIP-архивы ───────────────────────────────────────────────────────
        for zpath, entries in zip_tasks:
            zname = os.path.basename(zpath)
            self._ui(lambda n=zname, c=len(entries):
                     self._log(f"\n[ZIP] {n}  ({c} файлов)","head"))
            zip_errs = [0]

            with zipfile.ZipFile(zpath, "r") as zf:
                # Строим индекс всех JSON в архиве — ключевое улучшение
                json_index = _build_zip_json_index(zf) if use_google else {}

                def make_task(info, json_idx=json_index):
                    def _task():
                        self._pause_ev.wait()
                        fname = os.path.basename(info.filename) or info.filename
                        try:
                            with tempfile.TemporaryDirectory() as tmpdir:
                                with _fs_lock:
                                    extracted = zf.extract(info, tmpdir)

                                    # JSON для этого файла
                                    media_basename = os.path.basename(info.filename)
                                    json_zip_path  = _find_google_json_in_index(
                                        media_basename, json_idx)
                                    if json_zip_path:
                                        try: zf.extract(json_zip_path, tmpdir)
                                        except Exception: pass

                                    # Live Photo: если это видео без JSON — извлекаем парное фото
                                    ext = Path(media_basename).suffix.lower()
                                    if ext in VIDEO_EXTS and json_zip_path is None:
                                        stem = Path(media_basename).stem
                                        for photo_ext in PHOTO_EXTS:
                                            sibling_name = stem + photo_ext
                                            # Ищем парное фото в индексе медиафайлов ZIP
                                            sibling_zip = next(
                                                (k for k in zf.namelist()
                                                if os.path.basename(k).lower() == sibling_name.lower()),
                                                None
                                            )
                                            if sibling_zip:
                                                try:
                                                    zf.extract(sibling_zip, tmpdir)
                                                    # И его JSON тоже
                                                    sib_json = _find_google_json_in_index(
                                                        sibling_name, json_idx)
                                                    if sib_json:
                                                        zf.extract(sib_json, tmpdir)
                                                except Exception:
                                                    pass
                                                break

                                def _gd():
                                    dt, ds = get_best_date(extracted, use_google=use_google)
                                    if dt is None and ds == "no-date":
                                        dt2, ds2 = _zip_entry_date(info)
                                        if dt2:
                                            return dt2, ds2
                                    return dt, ds

                                return process(extracted, _gd, "copy")
                        except Exception as e:
                            _inc(err=1, done=1)
                            _tick()
                            self._ui(lambda n=fname, ex=e:
                                    self._log(f"      ERR {n}: {ex}","err"))
                            return "err"
                    return _task

                with ThreadPoolExecutor(max_workers=n_threads) as pool:
                    results = list(pool.map(lambda i: make_task(i)(), entries))
                zip_errs[0] = results.count("err")

            if del_zip and zip_errs[0] == 0:
                try:
                    os.remove(zpath)
                    self._ui(lambda n=zname: self._log(f"      удалён: {n}"))
                except Exception as e:
                    self._ui(lambda ex=e: self._log(f"      не удалось удалить: {ex}","warn"))

        s = stats
        parts = [f"ok: {s['ok']}"]
        if s["skip"]:   parts.append(f"пропущено: {s['skip']}")
        if s["nodate"]: parts.append(f"без даты → {UNKNOWN_FOLDER}: {s['nodate']}")
        if s["err"]:    parts.append(f"ошибок: {s['err']}")
        if s["google"]: parts.append(f"Google JSON: {s['google']}")
        tag = "err" if s["err"] else ("nodate" if s["nodate"] else "ok")
        self._ui(lambda p="   ".join(parts), t=tag:
                 self._log(f"\n✓ Готово!  {p}", t))
        self._done()


if __name__ == "__main__":
    app = App()
    app.mainloop()
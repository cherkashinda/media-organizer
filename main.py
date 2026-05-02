"""
PhotoOrganizer v2.0 — семейный фотоархив
Вкладки: Статистика | Сортировка | Скриншоты
Темы: тёмная / светлая

Новое в v2:
  - Переключение тёмной/светлой темы
  - Вкладка «Статистика»: сканирование папки/ZIP, сводка по всем форматам
  - Вкладка «Скриншоты»: поиск и перемещение скриншотов в папку «Скриншоты»
  - Опция «Пропускать скриншоты» в сортировке
  - Live Photo: парное фото ищется до mvhd (правильный порядок)
  - Подавление лишних сообщений exifread в консоли
"""

import logging
logging.getLogger("exifread").setLevel(logging.CRITICAL)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os, shutil, threading, zipfile, tempfile, json, re, struct
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
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

# ═══════════════════════════════════════════════════════════════════════════════
#  КОНСТАНТЫ
# ═══════════════════════════════════════════════════════════════════════════════

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
ALL_MEDIA       = PHOTO_EXTS | VIDEO_EXTS

MONTH_NAMES = {
    1:"01 - Январь",   2:"02 - Февраль",  3:"03 - Март",
    4:"04 - Апрель",   5:"05 - Май",       6:"06 - Июнь",
    7:"07 - Июль",     8:"08 - Август",    9:"09 - Сентябрь",
    10:"10 - Октябрь", 11:"11 - Ноябрь",  12:"12 - Декабрь",
}

UNKNOWN_FOLDER    = "_Без даты"
SCREENSHOT_FOLDER = "Скриншоты"

EXIF_DATE_TAGS_ER  = ["EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"]
EXIF_DATE_TAGS_PIL = {36867: "DateTimeOriginal", 36868: "DateTimeDigitized", 306: "DateTime"}
EXIF_FMT  = "%Y:%m:%d %H:%M:%S"
MP4_EPOCH = datetime(1904, 1, 1)
_fs_lock  = threading.Lock()

_GOOGLE_MAX_NAME_LEN = 46
_EDITED_SUFFIXES = ("-edited", "-modifié", "-modified", "-bearbeitet",
                    "-bewerkt", "-editada", "-editado", "-modificato")

# ── Темы ─────────────────────────────────────────────────────────────────────
THEMES = {
    "dark": dict(
        BG="#1e1e2e", CARD="#2a2a3e", ACC="#7c5cfc",
        FG="#e0e0f0", MUTED="#888aaa", GREEN="#3ddc97",
        YELLOW="#f4c430", BLUE="#5bc8f5", RED="#ff6b6b",
        ORANGE="#ffaa44", ENTRY="#13132a", SEL="#13132a",
        ACC_ACT="#9370ff", LOG_BG="#13132a", LOG_FG="#b0b2d0",
        TREE_BG="#1e1e2e", TREE_FG="#e0e0f0",
        TREE_SEL="#3a3a5e", TREE_HEAD="#2a2a3e",
    ),
    "light": dict(
        BG="#f0f2f8", CARD="#ffffff", ACC="#5a3edc",
        FG="#1a1a2e", MUTED="#888899", GREEN="#1a8050",
        YELLOW="#806000", BLUE="#1a60aa", RED="#cc2222",
        ORANGE="#bb5500", ENTRY="#f8f8ff", SEL="#e0e0ff",
        ACC_ACT="#7050d0", LOG_BG="#f8f8ff", LOG_FG="#2a2a3e",
        TREE_BG="#ffffff", TREE_FG="#1a1a2e",
        TREE_SEL="#d0d0f0", TREE_HEAD="#eeeef8",
    ),
}

# ── Паттерны скриншотов ───────────────────────────────────────────────────────
_SCREENSHOT_PATTERNS = [
    re.compile(r'^screenshot[\s_\-]', re.I),
    re.compile(r'^screenshot\.', re.I),
    re.compile(r'^screen[\s_\-]shot', re.I),
    re.compile(r'^снимок[\s_\-]экрана', re.I),
    re.compile(r'^скриншот[\s_\-]', re.I),
    re.compile(r'^bildschirmfoto', re.I),
    re.compile(r'^captura', re.I),
    re.compile(r'^schermata', re.I),
    re.compile(r'^capture', re.I),
]

# Разрешения экранов телефонов в портретной ориентации (px)
_SCREEN_RESOLUTIONS = {
    (750, 1334), (1125, 2436), (1242, 2688), (828, 1792),
    (1170, 2532), (1284, 2778), (1179, 2556), (1290, 2796),
    (1080, 1920), (1080, 2340), (1080, 2400), (1440, 2960),
    (1440, 3040), (1080, 2312), (1080, 2220), (720, 1280),
    (1080, 2160), (1440, 3120), (1080, 2376), (1284, 2802),
}


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
        read_size = min(4 * 1024 * 1024, os.path.getsize(path))
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
        return datetime.fromtimestamp(calendar.timegm(dt_utc.timetuple()))
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

def _google_json_candidates(name: str) -> list:
    p = Path(name)
    stem, suffix = p.stem, p.suffix
    bases = []

    def _ws(base):
        bases.append(base)
        sm = f"{base}.supplemental-metadata"
        if len(sm) <= _GOOGLE_MAX_NAME_LEN:
            bases.append(sm)
        else:
            for cut in range(max(35, _GOOGLE_MAX_NAME_LEN - 5), _GOOGLE_MAX_NAME_LEN + 2):
                bases.append(sm[:cut])

    _ws(name)
    _ws(stem)
    if len(name) > _GOOGLE_MAX_NAME_LEN:
        for cut in range(_GOOGLE_MAX_NAME_LEN - 1, _GOOGLE_MAX_NAME_LEN + 2):
            _ws(name[:cut])
    m = re.match(r'^(.*?)(\(\d+\))$', stem)
    if m:
        bs, num = m.group(1), m.group(2)
        orig = f"{bs}{suffix}"
        _ws(orig); _ws(bs)
        _ws(f"{orig}{num}"); _ws(f"{bs}{num}{suffix}")
    for sfx in _EDITED_SUFFIXES:
        if stem.endswith(sfx):
            os_ = stem[:-len(sfx)]
            _ws(f"{os_}{suffix}"); _ws(os_)
            break

    seen, result = set(), []
    for b in bases:
        if b not in seen:
            seen.add(b)
            result.append(b)
    return result


def _find_google_json(path: str) -> str | None:
    parent = Path(path).parent
    name   = Path(path).name
    for base in _google_json_candidates(name):
        c = parent / f"{base}.json"
        if c.exists():
            return str(c)
    return None


def _find_google_json_in_index(media_name: str, json_index: dict) -> str | None:
    for base in _google_json_candidates(media_name):
        key = f"{base}.json".lower()
        if key in json_index:
            return json_index[key]
    return None


def _google_json_date(json_path: str) -> datetime | None:
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
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
    """Возвращает (datetime | None, source_str)."""
    if use_google:
        jp = _find_google_json(path)
        if jp:
            dt = _google_json_date(jp)
            if dt:
                return dt, "Google JSON"

    ext      = Path(path).suffix.lower()
    is_video = ext in VIDEO_EXTS

    if is_video:
        # Live Photo: сначала ищем парное фото (до mvhd!)
        parent = Path(path).parent
        stem   = Path(path).stem
        for photo_ext in PHOTO_EXTS:
            sibling = parent / (stem + photo_ext)
            if sibling.exists():
                dt, src = get_best_date(str(sibling), use_google=use_google)
                if dt:
                    return dt, f"Live Photo ({sibling.name})"
        # Только потом mvhd
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

    return None, "no-date"


def _zip_entry_date(info):
    if info.date_time and info.date_time[0] >= 1980:
        try:
            return datetime(*info.date_time[:6]), "zip-metadata"
        except Exception:
            pass
    return None, "no-date"


def _build_zip_json_index(zf: zipfile.ZipFile) -> dict:
    index = {}
    for info in zf.infolist():
        fn = info.filename
        if fn.lower().endswith(".json") and not info.is_dir():
            bn = os.path.basename(fn).lower()
            if bn:
                index[bn] = fn
    return index


# ═══════════════════════════════════════════════════════════════════════════════
#  СКРИНШОТЫ
# ═══════════════════════════════════════════════════════════════════════════════

def is_screenshot(path: str) -> bool:
    """
    Определяет, является ли файл скриншотом.
    Критерий 1: имя файла соответствует паттерну (Screenshot_*, Снимок экрана_*, ...)
    Критерий 2: PNG-файл с разрешением, совпадающим с экраном телефона
    """
    fname = Path(path).name
    ext   = Path(path).suffix.lower()

    for pattern in _SCREENSHOT_PATTERNS:
        if pattern.match(fname):
            return True

    if ext == ".png" and PIL_AVAILABLE:
        try:
            with Image.open(path) as img:
                w, h = img.size
                if (w, h) in _SCREEN_RESOLUTIONS or (h, w) in _SCREEN_RESOLUTIONS:
                    return True
        except Exception:
            pass

    return False


# ═══════════════════════════════════════════════════════════════════════════════
#  СТАТИСТИКА
# ═══════════════════════════════════════════════════════════════════════════════

def scan_stats(root: str, progress_cb=None) -> dict:
    """
    Сканирует папку и ZIP-архивы в ней.
    Возвращает dict: ext -> {"count": N, "size": bytes}
    """
    counts = defaultdict(lambda: {"count": 0, "size": 0})

    def add(ext, size):
        e = ext.lower() if ext else "(без расш.)"
        counts[e]["count"] += 1
        counts[e]["size"]  += size

    # Собираем все файлы
    all_files = []
    for dirpath, _, fnames in os.walk(root):
        for fn in fnames:
            all_files.append(os.path.join(dirpath, fn))

    total = len(all_files)

    for i, fpath in enumerate(all_files):
        ext  = Path(fpath).suffix.lower()
        size = 0
        try:
            size = os.path.getsize(fpath)
        except Exception:
            pass

        if ext == ".zip":
            # Раскрываем ZIP и считаем содержимое
            try:
                with zipfile.ZipFile(fpath, "r") as zf:
                    for info in zf.infolist():
                        if not info.is_dir():
                            ze = Path(info.filename).suffix.lower()
                            add(ze, info.file_size)
            except Exception:
                add(ext, size)
        else:
            add(ext, size)

        if progress_cb and total:
            progress_cb(i + 1, total)

    return dict(counts)


# ═══════════════════════════════════════════════════════════════════════════════
#  РАЗМЕЩЕНИЕ ФАЙЛА
# ═══════════════════════════════════════════════════════════════════════════════

def place_file(src_path, dst_root, dt, mode, conflict, subdir_override=None):
    """
    Копирует или перемещает файл в Год/Месяц/[Фото|Видео|subdir_override]/
    Возвращает (rel_path, "ok"|"skip")
    """
    fname = os.path.basename(src_path)
    base, ext = os.path.splitext(fname)

    if subdir_override:
        media_sub = subdir_override
    elif ext.lower() in VIDEO_EXTS:
        media_sub = "Видео"
    else:
        media_sub = "Фото"

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
        open(dest_path, "ab").close()  # резервируем имя

    try:
        if mode == "move":
            shutil.copy2(src_path, dest_path)
            if os.path.getsize(dest_path) == os.path.getsize(src_path):
                os.remove(src_path)
            else:
                raise IOError("Размер не совпадает после копирования")
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
#  ПРИЛОЖЕНИЕ
# ═══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PhotoOrganizer v2")
        self.resizable(True, True)
        self.theme = "dark"

        # ── Переменные состояния (переживают смену темы) ──────────────────────
        # Статистика
        self.stats_src_var   = tk.StringVar()
        # Сортировка
        self.src_var         = tk.StringVar()
        self.dst_var         = tk.StringVar()
        self.mode_var        = tk.StringVar(value="copy")
        self.video_var       = tk.BooleanVar(value=True)
        self.zip_var         = tk.BooleanVar(value=True)
        self.conflict_var    = tk.StringVar(value="skip")
        self.threads_var     = tk.IntVar(value=min(os.cpu_count() or 4, 8))
        self.show_src_var    = tk.BooleanVar(value=True)
        self.zip_delete_var  = tk.BooleanVar(value=False)
        self.google_var      = tk.BooleanVar(value=True)
        self.skip_scr_var    = tk.BooleanVar(value=False)
        # Скриншоты
        self.scr_root_var    = tk.StringVar()
        self.scr_mode_var    = tk.StringVar(value="move")

        # ── Рантайм ──────────────────────────────────────────────────────────
        self._running     = False
        self._paused      = False
        self._pause_ev    = threading.Event()
        self._pause_ev.set()
        self._scr_running = False
        self._stats_running = False

        self._build_ui()
        self._center()

    @property
    def C(self):
        return THEMES[self.theme]

    def _center(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(840, sw - 40)
        h = min(740, sh - 60)
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        self.minsize(680, 580)

    def _toggle_theme(self):
        if self._running or self._scr_running or self._stats_running:
            messagebox.showinfo("", "Дождитесь завершения операции.")
            return
        try:
            active_tab = self._notebook.index("current")
        except Exception:
            active_tab = 0
        self.theme = "light" if self.theme == "dark" else "dark"
        for child in self.winfo_children():
            child.destroy()
        self._build_ui()
        try:
            self._notebook.select(active_tab)
        except Exception:
            pass

    def _ui(self, fn):
        self.after(0, fn)

    def _browse(self, var):
        path = filedialog.askdirectory(title="Выберите папку")
        if path:
            var.set(path)

    # ── Стили ────────────────────────────────────────────────────────────────

    def _apply_styles(self):
        C = self.C
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TFrame",        background=C["BG"])
        s.configure("Card.TFrame",   background=C["CARD"])
        s.configure("TLabel",        background=C["BG"],   foreground=C["FG"],  font=("Segoe UI", 9))
        s.configure("Card.TLabel",   background=C["CARD"], foreground=C["FG"],  font=("Segoe UI", 9))
        s.configure("Muted.TLabel",  background=C["CARD"], foreground=C["MUTED"], font=("Segoe UI", 8))
        s.configure("TEntry",        fieldbackground=C["ENTRY"], foreground=C["FG"],
                    insertcolor=C["FG"], font=("Segoe UI", 9))
        s.configure("TProgressbar",  troughcolor=C["CARD"], background=C["ACC"], borderwidth=0)
        s.configure("Accent.TButton", background=C["ACC"], foreground="white",
                    font=("Segoe UI", 10, "bold"), borderwidth=0, relief="flat", padding=8)
        s.map("Accent.TButton",
              background=[("active", C["ACC_ACT"]), ("disabled", "#444466")])
        s.configure("Pause.TButton", background="#444466", foreground="white",
                    font=("Segoe UI", 10, "bold"), borderwidth=0, relief="flat", padding=8)
        s.map("Pause.TButton",
              background=[("active", "#6666aa"), ("disabled", "#333344")])
        s.configure("Theme.TButton", background=C["CARD"], foreground=C["FG"],
                    font=("Segoe UI", 9), borderwidth=0, relief="flat", padding=6)
        s.map("Theme.TButton", background=[("active", C["ACC"]),],
              foreground=[("active", "white")])
        # Notebook
        s.configure("TNotebook",     background=C["BG"], borderwidth=0)
        s.configure("TNotebook.Tab", background=C["CARD"], foreground=C["MUTED"],
                    font=("Segoe UI", 9), padding=[14, 7])
        s.map("TNotebook.Tab",
              background=[("selected", C["ACC"]), ("active", C["CARD"])],
              foreground=[("selected", "white"), ("active", C["FG"])])
        # Treeview
        s.configure("Treeview",         background=C["TREE_BG"], foreground=C["TREE_FG"],
                    fieldbackground=C["TREE_BG"], borderwidth=0, font=("Segoe UI", 9),
                    rowheight=22)
        s.configure("Treeview.Heading", background=C["TREE_HEAD"], foreground=C["FG"],
                    font=("Segoe UI", 9, "bold"), borderwidth=0, relief="flat")
        s.map("Treeview", background=[("selected", C["TREE_SEL"])])

    # ── Главный UI ───────────────────────────────────────────────────────────

    def _build_ui(self):
        C = self.C
        self.configure(bg=C["BG"])
        self._apply_styles()

        # Шапка
        header = tk.Frame(self, bg=C["BG"], padx=16, pady=10)
        header.pack(fill="x")

        tk.Label(header, text="📷  PhotoOrganizer",
                 bg=C["BG"], fg=C["FG"],
                 font=("Segoe UI", 14, "bold")).pack(side="left")
        tk.Label(header, text="v2.0  |  Семейный архив",
                 bg=C["BG"], fg=C["MUTED"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(10, 0), pady=(5, 0))

        # Кнопка темы
        moon = "☀  Светлая" if self.theme == "dark" else "🌙  Тёмная"
        ttk.Button(header, text=moon, style="Theme.TButton",
                   command=self._toggle_theme).pack(side="right")

        # Статус библиотек
        lf = tk.Frame(header, bg=C["BG"])
        lf.pack(side="right", padx=(0, 16))
        for lib, ok, note in [
            ("exifread", EXIFREAD_AVAILABLE, "HEIC/RAW"),
            ("Pillow",   PIL_AVAILABLE,      "JPEG/TIFF"),
            ("mutagen",  MUTAGEN_AVAILABLE,  "MP4"),
        ]:
            tk.Label(lf, text=f"{'✓' if ok else '✗'} {lib}",
                     bg=C["BG"], fg=C["GREEN"] if ok else C["YELLOW"],
                     font=("Segoe UI", 8)).pack(side="left", padx=(0, 10))

        # Notebook
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        tab1 = tk.Frame(self._notebook, bg=C["BG"])
        self._notebook.add(tab1, text="📊  Статистика")
        self._build_stats_tab(tab1)

        tab2 = tk.Frame(self._notebook, bg=C["BG"])
        self._notebook.add(tab2, text="🗂  Сортировка")
        self._build_sort_tab(tab2)

        tab3 = tk.Frame(self._notebook, bg=C["BG"])
        self._notebook.add(tab3, text="📱  Скриншоты")
        self._build_screenshots_tab(tab3)

    # ─────────────────────────────────────────────────────────────────────────
    #  ВКЛАДКА 1: СТАТИСТИКА
    # ─────────────────────────────────────────────────────────────────────────

    def _build_stats_tab(self, parent):
        C = self.C
        root = tk.Frame(parent, bg=C["BG"], padx=12, pady=10)
        root.pack(fill="both", expand=True)

        tk.Label(root, text="Сканирует папку (в том числе ZIP-архивы) и выводит статистику по форматам файлов.",
                 bg=C["BG"], fg=C["MUTED"], font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 8))

        self._path_row(root, "Папка для анализа", self.stats_src_var)

        # Кнопка + статус
        bf = tk.Frame(root, bg=C["BG"]); bf.pack(fill="x", pady=(4, 8))
        self._stats_btn = ttk.Button(bf, text="🔍  Сканировать",
                                     style="Accent.TButton",
                                     command=self._start_stats)
        self._stats_btn.pack(side="left", ipady=2)
        self._stats_status = tk.Label(bf, text="", bg=C["BG"], fg=C["MUTED"],
                                      font=("Segoe UI", 8))
        self._stats_status.pack(side="left", padx=(12, 0))

        self._stats_progress = ttk.Progressbar(root, mode="determinate")
        self._stats_progress.pack(fill="x", pady=(0, 8))

        # Таблица
        tree_frame = tk.Frame(root, bg=C["BG"])
        tree_frame.pack(fill="both", expand=True)

        self._stats_tree = ttk.Treeview(tree_frame,
                                         columns=("ext", "count", "size", "category"),
                                         show="headings", selectmode="browse")
        for col, label, w, anchor in [
            ("ext",      "Расширение", 110, "w"),
            ("count",    "Файлов",      80, "e"),
            ("size",     "Размер",      110, "e"),
            ("category", "Категория",   120, "w"),
        ]:
            self._stats_tree.heading(col, text=label,
                                     command=lambda c=col: self._sort_stats_tree(c))
            self._stats_tree.column(col, width=w, anchor=anchor, minwidth=60)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._stats_tree.yview)
        self._stats_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._stats_tree.pack(fill="both", expand=True)

        self._stats_summary = tk.Label(root, text="",
                                        bg=C["BG"], fg=C["FG"],
                                        font=("Segoe UI", 9, "bold"), wraplength=700, justify="left")
        self._stats_summary.pack(anchor="w", pady=(8, 0))

        self._stats_sort_col = "count"
        self._stats_sort_rev = True

    def _sort_stats_tree(self, col):
        if self._stats_sort_col == col:
            self._stats_sort_rev = not self._stats_sort_rev
        else:
            self._stats_sort_col = col
            self._stats_sort_rev = col in ("count", "size")
        items = [(self._stats_tree.set(i, col), i)
                 for i in self._stats_tree.get_children()]

        def key(x):
            try:
                return int(x[0].replace(",", "").replace(" ", ""))
            except Exception:
                return x[0].lower()
        items.sort(key=key, reverse=self._stats_sort_rev)
        for idx, (_, iid) in enumerate(items):
            self._stats_tree.move(iid, "", idx)

    def _start_stats(self):
        src = self.stats_src_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("Ошибка", "Выберите корректную папку для анализа.")
            return
        self._stats_btn.configure(state="disabled")
        self._stats_tree.delete(*self._stats_tree.get_children())
        self._stats_summary.configure(text="")
        self._stats_progress.configure(value=0)
        self._stats_running = True
        threading.Thread(target=self._worker_stats, args=(src,), daemon=True).start()

    def _worker_stats(self, src):
        def on_progress(done, total):
            pct = int(done / total * 100) if total else 0
            self._ui(lambda p=pct, d=done, t=total: (
                self._stats_progress.configure(value=p),
                self._stats_status.configure(text=f"Файлов: {d}/{t} ({p}%)")
            ))

        result = scan_stats(src, progress_cb=on_progress)

        def fmt_size(b):
            if b >= 1_073_741_824: return f"{b / 1_073_741_824:.1f} ГБ"
            if b >= 1_048_576:     return f"{b / 1_048_576:.1f} МБ"
            if b >= 1024:          return f"{b / 1024:.1f} КБ"
            return f"{b} Б"

        def category(ext):
            if ext in PHOTO_EXTS: return "📷 Фото"
            if ext in VIDEO_EXTS: return "🎬 Видео"
            if ext == ".json":    return "📋 Метаданные"
            if ext == ".zip":     return "🗜 Архив"
            return "📄 Прочее"

        def update():
            photo_c = video_c = photo_s = video_s = total_c = total_s = 0
            self._stats_tree.delete(*self._stats_tree.get_children())
            for ext, d in sorted(result.items(), key=lambda x: -x[1]["count"]):
                self._stats_tree.insert("", "end", values=(
                    ext,
                    f"{d['count']:,}",
                    fmt_size(d["size"]),
                    category(ext),
                ))
                total_c += d["count"]
                total_s += d["size"]
                if ext in PHOTO_EXTS:
                    photo_c += d["count"]; photo_s += d["size"]
                elif ext in VIDEO_EXTS:
                    video_c += d["count"]; video_s += d["size"]

            summary = (
                f"Всего файлов: {total_c:,}  ({fmt_size(total_s)})     "
                f"Фото: {photo_c:,} ({fmt_size(photo_s)})     "
                f"Видео: {video_c:,} ({fmt_size(video_s)})     "
                f"Медиа итого: {photo_c + video_c:,}"
            )
            self._stats_summary.configure(text=summary)
            self._stats_status.configure(text="✓ Готово")
            self._stats_progress.configure(value=100)
            self._stats_btn.configure(state="normal")
            self._stats_running = False

        self._ui(update)

    # ─────────────────────────────────────────────────────────────────────────
    #  ВКЛАДКА 2: СОРТИРОВКА
    # ─────────────────────────────────────────────────────────────────────────

    def _build_sort_tab(self, parent):
        C = self.C

        outer  = tk.Frame(parent, bg=C["BG"])
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=C["BG"], highlightthickness=0)
        vsb    = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=C["BG"])
        wid   = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(wid, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

        root = tk.Frame(inner, bg=C["BG"], padx=12, pady=10)
        root.pack(fill="both", expand=True)

        self._path_row(root, "Источник", self.src_var)
        self._path_row(root, "Цель",     self.dst_var)

        opts = tk.Frame(root, bg=C["CARD"], padx=12, pady=10)
        opts.pack(fill="x", pady=(0, 8))

        def cb(p, text, var, fg=None, bold=False):
            return tk.Checkbutton(p, text=text, variable=var,
                                  bg=C["CARD"], fg=fg or C["FG"],
                                  selectcolor=C["SEL"],
                                  activebackground=C["CARD"],
                                  activeforeground=fg or C["FG"],
                                  font=("Segoe UI", 9, "bold") if bold else ("Segoe UI", 9))

        def rb(p, text, var, val):
            return tk.Radiobutton(p, text=text, variable=var, value=val,
                                  bg=C["CARD"], fg=C["FG"], selectcolor=C["SEL"],
                                  activebackground=C["CARD"], activeforeground=C["FG"],
                                  font=("Segoe UI", 9))

        # Режим + медиа
        r0 = tk.Frame(opts, bg=C["CARD"]); r0.pack(fill="x", pady=(0, 4))
        tk.Label(r0, text="Режим:", bg=C["CARD"], fg=C["FG"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        rb(r0, "Копировать", self.mode_var, "copy").pack(side="left", padx=(0, 12))
        rb(r0, "Перемещать", self.mode_var, "move").pack(side="left", padx=(0, 24))
        cb(r0, "Видео", self.video_var).pack(side="left", padx=(0, 10))
        cb(r0, "ZIP",   self.zip_var  ).pack(side="left")

        # Дубликаты
        r1 = tk.Frame(opts, bg=C["CARD"]); r1.pack(fill="x", pady=(4, 4))
        tk.Label(r1, text="Если файл уже есть:", bg=C["CARD"], fg=C["FG"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))
        rb(r1, "Пропустить",      self.conflict_var, "skip"     ).pack(side="left", padx=(0, 12))
        rb(r1, "Перезаписать",    self.conflict_var, "overwrite").pack(side="left", padx=(0, 12))
        rb(r1, "Создать копию",   self.conflict_var, "copy"     ).pack(side="left")

        # Потоки + прочее
        r2 = tk.Frame(opts, bg=C["CARD"]); r2.pack(fill="x", pady=(4, 4))
        tk.Label(r2, text="Потоков:", bg=C["CARD"], fg=C["FG"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        cpu = os.cpu_count() or 4
        tk.Spinbox(r2, from_=1, to=cpu, textvariable=self.threads_var,
                   width=4, bg=C["ENTRY"], fg=C["FG"], buttonbackground=C["CARD"],
                   insertbackground=C["FG"], font=("Segoe UI", 9), relief="flat"
                   ).pack(side="left")
        tk.Label(r2, text=f"(макс. {cpu})", bg=C["CARD"], fg=C["MUTED"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(4, 16))
        cb(r2, "Источник даты", self.show_src_var  ).pack(side="left", padx=(0, 10))
        cb(r2, "Удалять ZIP",   self.zip_delete_var).pack(side="left")

        # Google JSON
        r3 = tk.Frame(opts, bg=C["CARD"]); r3.pack(fill="x", pady=(4, 4))
        cb(r3, "Метаданные Google Takeout  (photoTakenTime из .json)",
           self.google_var, fg=C["BLUE"], bold=True).pack(side="left")

        # Скриншоты
        r4 = tk.Frame(opts, bg=C["CARD"]); r4.pack(fill="x", pady=(4, 0))
        cb(r4, "Пропускать скриншоты", self.skip_scr_var, fg=C["ORANGE"]).pack(side="left")
        tk.Label(r4, text="(Screenshot_*, Снимок экрана_*, PNG с разрешением экрана телефона)",
                 bg=C["CARD"], fg=C["MUTED"], font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))

        # Прогресс
        pf = tk.Frame(root, bg=C["CARD"], padx=12, pady=8)
        pf.pack(fill="x", pady=(0, 8))
        top = tk.Frame(pf, bg=C["CARD"]); top.pack(fill="x")
        self.prog_label  = tk.Label(top, text="Готов к работе",
                                    bg=C["CARD"], fg=C["FG"], font=("Segoe UI", 9))
        self.prog_label.pack(side="left")
        self.stats_label = tk.Label(top, text="",
                                    bg=C["CARD"], fg=C["MUTED"], font=("Segoe UI", 8))
        self.stats_label.pack(side="right")
        self.progress = ttk.Progressbar(pf, mode="determinate")
        self.progress.pack(fill="x", pady=(4, 0))

        # Журнал
        lf = tk.Frame(root, bg=C["CARD"], padx=12, pady=8)
        lf.pack(fill="both", expand=True, pady=(0, 8))
        tk.Label(lf, text="Журнал", bg=C["CARD"], fg=C["MUTED"],
                 font=("Segoe UI", 8)).pack(anchor="w")
        self.log_widget = tk.Text(lf, height=8,
                                  bg=C["LOG_BG"], fg=C["LOG_FG"],
                                  font=("Consolas", 8), bd=0,
                                  padx=4, pady=4, state="disabled", wrap="word")
        for tag, fg in [("warn",   C["YELLOW"]), ("ok",     C["GREEN"]),
                        ("skip",   C["MUTED"]),  ("nodate", C["ORANGE"]),
                        ("google", C["BLUE"]),   ("err",    C["RED"]),
                        ("scr",    C["ORANGE"])]:
            self.log_widget.tag_configure(tag, foreground=fg)
        self.log_widget.tag_configure("head",  foreground=C["MUTED"],
                                               font=("Consolas", 8, "bold"))
        self.log_widget.tag_configure("pause", foreground=C["YELLOW"],
                                               font=("Consolas", 8, "bold"))
        sl = ttk.Scrollbar(lf, command=self.log_widget.yview)
        self.log_widget.configure(yscrollcommand=sl.set)
        sl.pack(side="right", fill="y")
        self.log_widget.pack(fill="both", expand=True)

        # Кнопки
        br = tk.Frame(root, bg=C["BG"]); br.pack(fill="x", pady=(0, 4))
        self.btn_start = ttk.Button(br, text="▶  Запустить",
                                    style="Accent.TButton", command=self._start_sort)
        self.btn_start.pack(side="left", fill="x", expand=True, ipady=2)
        self.btn_pause = ttk.Button(br, text="⏸  Пауза",
                                    style="Pause.TButton", command=self._toggle_pause,
                                    state="disabled")
        self.btn_pause.pack(side="left", fill="x", expand=True, ipady=2, padx=(6, 0))

    # ─────────────────────────────────────────────────────────────────────────
    #  ВКЛАДКА 3: СКРИНШОТЫ
    # ─────────────────────────────────────────────────────────────────────────

    def _build_screenshots_tab(self, parent):
        C = self.C
        root = tk.Frame(parent, bg=C["BG"], padx=12, pady=10)
        root.pack(fill="both", expand=True)

        tk.Label(root,
                 text="Ищет скриншоты в уже отсортированном архиве (папки Фото и Видео) "
                      "и перемещает их в папку «Скриншоты» рядом с «Фото» и «Видео».",
                 bg=C["BG"], fg=C["MUTED"], font=("Segoe UI", 8),
                 wraplength=700, justify="left").pack(anchor="w", pady=(0, 8))

        self._path_row(root, "Корень архива (папка с подпапками 2015, 2016, ...)",
                       self.scr_root_var)

        opts = tk.Frame(root, bg=C["CARD"], padx=12, pady=10)
        opts.pack(fill="x", pady=(0, 8))

        r0 = tk.Frame(opts, bg=C["CARD"]); r0.pack(fill="x", pady=(0, 6))
        tk.Label(r0, text="Режим:", bg=C["CARD"], fg=C["FG"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))
        for text, val in [("Переместить", "move"), ("Копировать", "copy")]:
            tk.Radiobutton(r0, text=text, variable=self.scr_mode_var, value=val,
                           bg=C["CARD"], fg=C["FG"], selectcolor=C["SEL"],
                           activebackground=C["CARD"], font=("Segoe UI", 9)
                           ).pack(side="left", padx=(0, 12))

        tk.Label(opts,
                 text="Критерии скриншота:  имя начинается с Screenshot_ / Снимок экрана_ / Скриншот_  "
                      "или PNG с разрешением экрана телефона (определяется через Pillow)",
                 bg=C["CARD"], fg=C["MUTED"], font=("Segoe UI", 8),
                 wraplength=680, justify="left").pack(anchor="w", pady=(4, 0))

        # Прогресс
        pf = tk.Frame(root, bg=C["CARD"], padx=12, pady=8)
        pf.pack(fill="x", pady=(0, 8))
        top = tk.Frame(pf, bg=C["CARD"]); top.pack(fill="x")
        self.scr_prog_label  = tk.Label(top, text="Готов к работе",
                                        bg=C["CARD"], fg=C["FG"], font=("Segoe UI", 9))
        self.scr_prog_label.pack(side="left")
        self.scr_stats_label = tk.Label(top, text="",
                                        bg=C["CARD"], fg=C["MUTED"], font=("Segoe UI", 8))
        self.scr_stats_label.pack(side="right")
        self.scr_progress = ttk.Progressbar(pf, mode="determinate")
        self.scr_progress.pack(fill="x", pady=(4, 0))

        # Журнал
        lf = tk.Frame(root, bg=C["CARD"], padx=12, pady=8)
        lf.pack(fill="both", expand=True, pady=(0, 8))
        tk.Label(lf, text="Журнал", bg=C["CARD"], fg=C["MUTED"],
                 font=("Segoe UI", 8)).pack(anchor="w")
        self.scr_log_widget = tk.Text(lf, height=10,
                                      bg=C["LOG_BG"], fg=C["LOG_FG"],
                                      font=("Consolas", 8), bd=0,
                                      padx=4, pady=4, state="disabled", wrap="word")
        for tag, fg in [("ok",   C["GREEN"]), ("skip", C["MUTED"]),
                        ("err",  C["RED"]),   ("head", C["MUTED"])]:
            self.scr_log_widget.tag_configure(tag, foreground=fg)
        self.scr_log_widget.tag_configure("head", font=("Consolas", 8, "bold"))
        sl2 = ttk.Scrollbar(lf, command=self.scr_log_widget.yview)
        self.scr_log_widget.configure(yscrollcommand=sl2.set)
        sl2.pack(side="right", fill="y")
        self.scr_log_widget.pack(fill="both", expand=True)

        br = tk.Frame(root, bg=C["BG"]); br.pack(fill="x", pady=(0, 4))
        self.btn_scr_start = ttk.Button(br, text="🔍  Найти и переместить скриншоты",
                                        style="Accent.TButton",
                                        command=self._start_screenshots)
        self.btn_scr_start.pack(fill="x", ipady=2)

    # ── Общие хелперы ────────────────────────────────────────────────────────

    def _path_row(self, parent, label, var):
        C = self.C
        f = tk.Frame(parent, bg=C["CARD"], padx=12, pady=8)
        f.pack(fill="x", pady=(0, 6))
        tk.Label(f, text=label, bg=C["CARD"], fg=C["FG"],
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 4))
        row = tk.Frame(f, bg=C["CARD"]); row.pack(fill="x")
        ttk.Entry(row, textvariable=var, style="TEntry").pack(
            side="left", fill="x", expand=True, ipady=4)
        tk.Button(row, text="Обзор...", bg=C["ACC"], fg="white", relief="flat",
                  font=("Segoe UI", 8), padx=8,
                  activebackground=C["ACC_ACT"], activeforeground="white",
                  command=lambda v=var: self._browse(v)).pack(
            side="left", padx=(6, 0), ipady=4)

    def _log(self, msg, tag=""):
        try:
            w = self.log_widget
            w.configure(state="normal")
            w.insert("end", msg + "\n", tag)
            w.see("end")
            w.configure(state="disabled")
        except Exception:
            pass

    def _log_clear(self):
        try:
            w = self.log_widget
            w.configure(state="normal")
            w.delete("1.0", "end")
            w.configure(state="disabled")
        except Exception:
            pass

    def _scr_log(self, msg, tag=""):
        try:
            w = self.scr_log_widget
            w.configure(state="normal")
            w.insert("end", msg + "\n", tag)
            w.see("end")
            w.configure(state="disabled")
        except Exception:
            pass

    def _scr_log_clear(self):
        try:
            w = self.scr_log_widget
            w.configure(state="normal")
            w.delete("1.0", "end")
            w.configure(state="disabled")
        except Exception:
            pass

    # ── Управление сортировкой ───────────────────────────────────────────────

    def _start_sort(self):
        if self._running:
            return
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("Ошибка", "Выберите корректную папку-источник.")
            return
        if not dst:
            messagebox.showerror("Ошибка", "Выберите папку назначения.")
            return
        if os.path.abspath(src) == os.path.abspath(dst):
            messagebox.showerror("Ошибка", "Источник и цель не могут совпадать.")
            return
        self._log_clear()
        self._running = True
        self._paused  = False
        self._pause_ev.set()
        self.btn_start.configure(state="disabled")
        self.btn_pause.configure(state="normal", text="⏸  Пауза")
        threading.Thread(target=self._worker_sort, args=(src, dst), daemon=True).start()

    def _toggle_pause(self):
        if not self._running:
            return
        if self._paused:
            self._paused = False
            self._pause_ev.set()
            self.btn_pause.configure(text="⏸  Пауза")
            self._ui(lambda: self._log("▶  Продолжение...", "pause"))
        else:
            self._paused = True
            self._pause_ev.clear()
            self.btn_pause.configure(text="▶  Продолжить")
            self._ui(lambda: self._log("⏸  Пауза...", "pause"))

    def _sort_done(self):
        def _():
            self._running = False
            self._paused  = False
            self._pause_ev.set()
            self.btn_start.configure(state="normal")
            self.btn_pause.configure(state="disabled", text="⏸  Пауза")
        self.after(0, _)

    # ── Управление скриншотами ───────────────────────────────────────────────

    def _start_screenshots(self):
        if self._scr_running:
            return
        src = self.scr_root_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("Ошибка", "Выберите корректную папку архива.")
            return
        self._scr_log_clear()
        self._scr_running = True
        self.btn_scr_start.configure(state="disabled")
        threading.Thread(target=self._worker_screenshots, args=(src,), daemon=True).start()

    def _scr_done(self):
        def _():
            self._scr_running = False
            self.btn_scr_start.configure(state="normal")
        self.after(0, _)

    # ═══════════════════════════════════════════════════════════════════════════
    #  РАБОЧИЙ ПОТОК: СОРТИРОВКА
    # ═══════════════════════════════════════════════════════════════════════════

    def _worker_sort(self, src, dst):
        mode       = self.mode_var.get()
        inc_video  = self.video_var.get()
        inc_zip    = self.zip_var.get()
        del_zip    = self.zip_delete_var.get()
        show_src   = self.show_src_var.get()
        use_google = self.google_var.get()
        n_threads  = max(1, self.threads_var.get())
        conflict   = self.conflict_var.get()
        skip_scr   = self.skip_scr_var.get()

        extensions = PHOTO_EXTS | (VIDEO_EXTS if inc_video else set())

        self._ui(lambda: self._log("Сканирование...", "head"))

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

        total_zip = sum(len(e) for _, e in zip_tasks)
        total = len(media_files) + total_zip

        if total == 0:
            self._ui(lambda: self._log("Файлы не найдены.", "warn"))
            self._sort_done()
            return

        self._ui(lambda: self._log(
            f"Файлов: {len(media_files)} на диске + {total_zip} в {len(zip_tasks)} ZIP"
            f"  |  Итого: {total}  |  Потоков: {n_threads}\n", "head"))

        lock  = threading.Lock()
        stats = {"ok": 0, "skip": 0, "err": 0, "nodate": 0, "google": 0, "scr": 0, "done": 0}

        def _inc(**kw):
            with lock:
                for k, v in kw.items():
                    stats[k] += v

        def _tick():
            with lock:
                done = stats["done"]; ok = stats["ok"]
                sk   = stats["skip"]; err = stats["err"]
                nd   = stats["nodate"]; sc = stats["scr"]
            pct = int(done / total * 100)
            self._ui(lambda p=pct, d=done, o=ok, s=sk, e=err, n=nd, sc=sc: (
                self.progress.configure(value=p),
                self.prog_label.configure(text=f"Обработано: {d}/{total}  ({p}%)"),
                self.stats_label.configure(
                    text=f"ok:{o}  пропущено:{s}  скринш.:{sc}  нет даты:{n}  ошибок:{e}")
            ))

        def process(fpath, get_date_fn, op_mode):
            self._pause_ev.wait()
            fname = os.path.basename(fpath)
            try:
                # Пропуск скриншотов
                if skip_scr and is_screenshot(fpath):
                    _inc(scr=1, done=1)
                    _tick()
                    self._ui(lambda n=fname:
                             self._log(f"  📱  {n}  [скриншот — пропущен]", "scr"))
                    return "skip_scr"

                dt, date_src = get_date_fn()
                is_google    = (date_src == "Google JSON")
                no_date      = (dt is None)

                rel, result = place_file(fpath, dst, dt, op_mode, conflict)

                if result == "skip":
                    _inc(skip=1, done=1)
                    _tick()
                    if show_src:
                        self._ui(lambda r=rel: self._log(f"  --  {r}  [пропущен]", "skip"))
                    return "skip"

                if no_date:
                    _inc(nodate=1, ok=1, done=1)
                    _tick()
                    self._ui(lambda r=rel: self._log(f"  ??  {r}  [нет даты]", "nodate"))
                    return "ok"

                _inc(ok=1, done=1, google=1 if is_google else 0)
                _tick()
                if show_src:
                    tag = "google" if is_google else "ok"
                    self._ui(lambda r=rel, s=date_src, t=tag:
                             self._log(f"  ok  {r}  [{s}]", t))
                else:
                    self._ui(lambda r=rel: self._log(f"  ok  {r}", "ok"))
                return "ok"

            except Exception as e:
                _inc(err=1, done=1)
                _tick()
                self._ui(lambda n=fname, ex=e:
                         self._log(f"  ERR {n}: {ex}", "err"))
                return "err"

        # Медиафайлы
        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futures = {
                pool.submit(
                    process, fp,
                    lambda p=fp: get_best_date(p, use_google=use_google),
                    mode
                ): fp for fp in media_files
            }
            for _ in as_completed(futures):
                pass

        # ZIP-архивы
        for zpath, entries in zip_tasks:
            zname = os.path.basename(zpath)
            self._ui(lambda n=zname, c=len(entries):
                     self._log(f"\n[ZIP] {n}  ({c} файлов)", "head"))
            zip_errs = [0]

            with zipfile.ZipFile(zpath, "r") as zf:
                json_index = _build_zip_json_index(zf) if use_google else {}

                def make_task(info, json_idx=json_index):
                    def _task():
                        self._pause_ev.wait()
                        fname = os.path.basename(info.filename) or info.filename
                        try:
                            with tempfile.TemporaryDirectory() as tmpdir:
                                with _fs_lock:
                                    extracted = zf.extract(info, tmpdir)
                                    media_basename = os.path.basename(info.filename)
                                    json_zip_path  = _find_google_json_in_index(
                                        media_basename, json_idx)
                                    if json_zip_path:
                                        try: zf.extract(json_zip_path, tmpdir)
                                        except Exception: pass
                                    ext_ = Path(media_basename).suffix.lower()
                                    if ext_ in VIDEO_EXTS and json_zip_path is None:
                                        stem_ = Path(media_basename).stem
                                        for pext in PHOTO_EXTS:
                                            sib_name = stem_ + pext
                                            sib_zip  = next(
                                                (k for k in zf.namelist()
                                                 if os.path.basename(k).lower() == sib_name.lower()),
                                                None)
                                            if sib_zip:
                                                try:
                                                    zf.extract(sib_zip, tmpdir)
                                                    sj = _find_google_json_in_index(
                                                        sib_name, json_idx)
                                                    if sj: zf.extract(sj, tmpdir)
                                                except Exception:
                                                    pass
                                                break

                                def _gd():
                                    dt, ds = get_best_date(extracted, use_google=use_google)
                                    if dt is None and ds == "no-date":
                                        dt2, ds2 = _zip_entry_date(info)
                                        if dt2: return dt2, ds2
                                    return dt, ds

                                return process(extracted, _gd, "copy")
                        except Exception as e:
                            _inc(err=1, done=1)
                            _tick()
                            self._ui(lambda n=fname, ex=e:
                                     self._log(f"      ERR {n}: {ex}", "err"))
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
                    self._ui(lambda ex=e: self._log(f"      не удалось удалить: {ex}", "warn"))

        s = stats
        parts = [f"ok: {s['ok']}"]
        if s["skip"]:   parts.append(f"пропущено: {s['skip']}")
        if s["scr"]:    parts.append(f"скриншотов пропущено: {s['scr']}")
        if s["nodate"]: parts.append(f"без даты → {UNKNOWN_FOLDER}: {s['nodate']}")
        if s["err"]:    parts.append(f"ошибок: {s['err']}")
        if s["google"]: parts.append(f"Google JSON: {s['google']}")
        tag = "err" if s["err"] else ("nodate" if s["nodate"] else "ok")
        self._ui(lambda p="   ".join(parts), t=tag:
                 self._log(f"\n✓ Готово!  {p}", t))
        self._sort_done()

    # ═══════════════════════════════════════════════════════════════════════════
    #  РАБОЧИЙ ПОТОК: СКРИНШОТЫ
    # ═══════════════════════════════════════════════════════════════════════════

    def _worker_screenshots(self, archive_root):
        """
        Ищет скриншоты в папках Фото и Видео отсортированного архива,
        перемещает в Скриншоты/ рядом с ними.
        """
        mode = self.scr_mode_var.get()
        self._ui(lambda: self._scr_log("Сканирование архива...", "head"))

        # Собираем медиафайлы из папок Фото и Видео
        candidates = []
        for dirpath, _, fnames in os.walk(archive_root):
            if os.path.basename(dirpath) in ("Фото", "Видео"):
                for fname in fnames:
                    ext = Path(fname).suffix.lower()
                    if ext in ALL_MEDIA:
                        candidates.append(os.path.join(dirpath, fname))

        if not candidates:
            self._ui(lambda: self._scr_log(
                "Медиафайлы не найдены. Проверь путь — нужна корневая папка "
                "с годами (2015, 2016, ...), внутри которых есть папки Фото/Видео.", "warn"))
            self._scr_done()
            return

        self._ui(lambda c=len(candidates):
                 self._scr_log(f"Найдено {c} медиафайлов. Проверяем...\n", "head"))

        found = 0; done_count = 0; errors = 0
        action_word = "перемещено" if mode == "move" else "скопировано"

        for i, fpath in enumerate(candidates):
            pct = int((i + 1) / len(candidates) * 100)
            self._ui(lambda p=pct, ii=i, t=len(candidates): (
                self.scr_progress.configure(value=p),
                self.scr_prog_label.configure(text=f"Проверено: {ii+1}/{t}  ({p}%)")
            ))

            if not is_screenshot(fpath):
                continue

            found += 1
            fname      = os.path.basename(fpath)
            photo_dir  = os.path.dirname(fpath)        # .../2015/01 - Январь/Фото
            month_dir  = os.path.dirname(photo_dir)    # .../2015/01 - Январь
            scr_dir    = os.path.join(month_dir, SCREENSHOT_FOLDER)

            try:
                os.makedirs(scr_dir, exist_ok=True)
                dst_path = os.path.join(scr_dir, fname)
                if os.path.exists(dst_path):
                    base, ext = os.path.splitext(fname)
                    n = 1
                    while os.path.exists(dst_path):
                        dst_path = os.path.join(scr_dir, f"{base}_copy_{n}{ext}")
                        n += 1

                if mode == "move":
                    shutil.move(fpath, dst_path)
                else:
                    shutil.copy2(fpath, dst_path)

                rel = os.path.relpath(fpath, archive_root)
                done_count += 1
                arrow = "→" if mode == "move" else "📋"
                self._ui(lambda r=rel, d=os.path.relpath(dst_path, archive_root), a=arrow:
                         self._scr_log(f"  {a}  {r}", "ok"))
            except Exception as e:
                errors += 1
                self._ui(lambda n=fname, ex=e:
                         self._scr_log(f"  ERR  {n}: {ex}", "err"))

        tag = "err" if errors else "ok"
        self._ui(lambda f=found, dc=done_count, e=errors, aw=action_word, t=tag: (
            self.scr_stats_label.configure(
                text=f"найдено: {f}  {aw}: {dc}  ошибок: {e}"),
            self.scr_progress.configure(value=100),
            self._scr_log(
                f"\n✓ Готово!  Скриншотов найдено: {f}  {aw}: {dc}"
                + (f"  ошибок: {e}" if e else ""), t)
        ))
        self._scr_done()


if __name__ == "__main__":
    app = App()
    app.mainloop()

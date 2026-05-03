"""
PhotoOrganizer v3.1
Вкладки: Статистика | Сортировка | Карта архива | Скриншоты | Дубликаты | Запись EXIF

Изменения v3.1:
  - Батчинг лога: сообщения пишутся пачками раз в 100мс — интерфейс не зависает
  - Исправлен open_zip_with_encoding (лишний аргумент)
  - Исправлено поле Источник пропадало при скролле
"""

import logging
logging.getLogger("exifread").setLevel(logging.CRITICAL)

import hashlib
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

try:
    import piexif
    PIEXIF_AVAILABLE = True
except ImportError:
    PIEXIF_AVAILABLE = False

VERSION = "3.1"

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

_SCREENSHOT_PATTERNS = [
    re.compile(r'^screenshot[\s_\-]', re.I), re.compile(r'^screenshot\.', re.I),
    re.compile(r'^screen[\s_\-]shot', re.I), re.compile(r'^снимок[\s_\-]экрана', re.I),
    re.compile(r'^скриншот[\s_\-]', re.I),   re.compile(r'^bildschirmfoto', re.I),
    re.compile(r'^captura', re.I),            re.compile(r'^schermata', re.I),
    re.compile(r'^capture', re.I),
]
_SCREEN_RESOLUTIONS = {
    (750,1334),(1125,2436),(1242,2688),(828,1792),(1170,2532),
    (1284,2778),(1179,2556),(1290,2796),(1080,1920),(1080,2340),
    (1080,2400),(1440,2960),(1440,3040),(1080,2312),(1080,2220),
    (720,1280),(1080,2160),(1440,3120),(1284,2802),(390,844),(428,926),
}


# ═══════════════════════════════════════════════════════════════════════════════
#  ОПРЕДЕЛЕНИЕ ДАТЫ
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_exif_dt(value):
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    s = str(value).strip().strip("\x00").strip()
    if not s or s.startswith("0000") or s == "None":
        return None
    for fmt in (EXIF_FMT, EXIF_FMT[:16]):
        try:
            return datetime.strptime(s[:len(fmt)], fmt)
        except ValueError:
            pass
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
                if dt: return dt, t
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
        for t in EXIF_DATE_TAGS_ER:
            if t in tags:
                dt = _parse_exif_dt(tags[t])
                if dt: return dt, t
    except Exception:
        pass
    return None, None

def _exif_pil(path):
    if not PIL_AVAILABLE:
        return None, None
    try:
        img = Image.open(path)
        for exif in (img.getexif(), getattr(img, "_getexif", lambda: None)()):
            if not exif: continue
            for tid, tn in EXIF_DATE_TAGS_PIL.items():
                if tid in exif:
                    dt = _parse_exif_dt(exif[tid])
                    if dt: return dt, f"PIL:{tn}"
    except Exception:
        pass
    return None, None

def _read_mp4_mvhd(path):
    try:
        with open(path, "rb") as f:
            data = f.read(min(4 << 20, os.path.getsize(path)))
        idx = data.find(b"mvhd")
        if idx == -1: return None
        box = data[idx + 4:]
        if len(box) < 8: return None
        version = box[0]
        if version == 0:
            creation_time = struct.unpack(">I", box[4:8])[0]
        elif version == 1:
            if len(box) < 16: return None
            creation_time = struct.unpack(">Q", box[4:12])[0]
        else:
            return None
        if creation_time == 0: return None
        import calendar
        return datetime.fromtimestamp(calendar.timegm((MP4_EPOCH + timedelta(seconds=creation_time)).timetuple()))
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
        if dt: return dt, "MP4/mvhd"
        dt, src = _read_video_date_mutagen(path)
        if dt: return dt, src
    dt, src = _exif_exifread(path)
    if dt: return dt, src
    return None, None

def _google_json_candidates(name):
    p = Path(name); stem, suffix = p.stem, p.suffix
    bases = []
    def _ws(base):
        bases.append(base)
        sm = f"{base}.supplemental-metadata"
        if len(sm) <= _GOOGLE_MAX_NAME_LEN:
            bases.append(sm)
        else:
            for cut in range(max(35, _GOOGLE_MAX_NAME_LEN - 5), _GOOGLE_MAX_NAME_LEN + 2):
                bases.append(sm[:cut])
    _ws(name); _ws(stem)
    if len(name) > _GOOGLE_MAX_NAME_LEN:
        for cut in range(_GOOGLE_MAX_NAME_LEN - 1, _GOOGLE_MAX_NAME_LEN + 2):
            _ws(name[:cut])
    m = re.match(r'^(.*?)(\(\d+\))$', stem)
    if m:
        bs, num = m.group(1), m.group(2); orig = f"{bs}{suffix}"
        _ws(orig); _ws(bs); _ws(f"{orig}{num}"); _ws(f"{bs}{num}{suffix}")
    for sfx in _EDITED_SUFFIXES:
        if stem.endswith(sfx):
            os_ = stem[:-len(sfx)]; _ws(f"{os_}{suffix}"); _ws(os_); break
    seen, result = set(), []
    for b in bases:
        if b not in seen: seen.add(b); result.append(b)
    return result

def _find_google_json(path):
    parent, name = Path(path).parent, Path(path).name
    for base in _google_json_candidates(name):
        c = parent / f"{base}.json"
        if c.exists(): return str(c)
    return None

def _find_google_json_in_index(media_name, json_index):
    for base in _google_json_candidates(media_name):
        key = f"{base}.json".lower()
        if key in json_index: return json_index[key]
    return None

def _google_json_date(json_path):
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        for key in ("photoTakenTime", "creationTime"):
            block = data.get(key)
            if block:
                ts = int(block.get("timestamp", 0))
                if ts > 0: return datetime.fromtimestamp(ts)
    except Exception:
        pass
    return None

def _date_from_filename(path):
    name = Path(path).stem
    patterns = [
        r'(\d{4})(\d{2})(\d{2})[_\-T](\d{2})(\d{2})(\d{2})',
        r'(\d{4})[_\-](\d{2})[_\-](\d{2})[_\- T](\d{2})[_\-:\.](\d{2})[_\-:\.](\d{2})',
        r'(\d{4})(\d{2})(\d{2})',
        r'(\d{4})[_\-](\d{2})[_\-](\d{2})',
    ]
    for pattern in patterns:
        m = re.search(pattern, name)
        if not m: continue
        g = m.groups()
        try:
            y, mo, d = int(g[0]), int(g[1]), int(g[2])
            if not (1990 <= y <= datetime.now().year): continue
            if not (1 <= mo <= 12): continue
            if not (1 <= d <= 31): continue
            if len(g) >= 6:
                return datetime(y, mo, d, min(int(g[3]),23), min(int(g[4]),59), min(int(g[5]),59))
            return datetime(y, mo, d)
        except ValueError:
            continue
    return None

def get_best_date(path, use_google=True, use_mtime=False):
    if use_google:
        jp = _find_google_json(path)
        if jp:
            dt = _google_json_date(jp)
            if dt: return dt, "Google JSON"
    ext      = Path(path).suffix.lower()
    is_video = ext in VIDEO_EXTS
    if is_video:
        parent, stem = Path(path).parent, Path(path).stem
        for photo_ext in PHOTO_EXTS:
            sibling = parent / (stem + photo_ext)
            if sibling.exists():
                dt, src = get_best_date(str(sibling), use_google=use_google, use_mtime=use_mtime)
                if dt: return dt, f"Live Photo ({sibling.name})"
        dt, src = _read_video_date(path)
        if dt: return dt, src
    else:
        dt, src = _exif_exifread(path)
        if dt: return dt, src
        dt, src = _exif_pil(path)
        if dt: return dt, src
    dt = _date_from_filename(path)
    if dt: return dt, "filename"
    if use_mtime:
        try:
            mtime = os.path.getmtime(path)
            dt = datetime.fromtimestamp(mtime)
            if datetime(1995, 1, 1) <= dt <= datetime.now():
                return dt, "file mtime"
        except Exception:
            pass
    return None, "no-date"

def _zip_entry_date(info):
    if info.date_time and info.date_time[0] >= 1980:
        try: return datetime(*info.date_time[:6]), "zip-metadata"
        except Exception: pass
    return None, "no-date"

def _build_zip_json_index(zf):
    index = {}
    for info in zf.infolist():
        fn = info.filename
        if fn.lower().endswith(".json") and not info.is_dir():
            bn = os.path.basename(fn).lower()
            if bn: index[bn] = fn
    return index

def open_zip_with_encoding(zpath):
    """Открывает ZIP с исправлением кодировки CP866 для старых русских архивов."""
    zf = zipfile.ZipFile(zpath, "r")
    for info in zf.infolist():
        if info.flag_bits & 0x800:
            return zf  # UTF-8 флаг установлен — всё ок
        try:
            info.filename = info.filename.encode("cp437").decode("cp866")
        except Exception:
            pass
    return zf


# ═══════════════════════════════════════════════════════════════════════════════
#  СКРИНШОТЫ
# ═══════════════════════════════════════════════════════════════════════════════

def is_screenshot(path):
    fname = Path(path).name; ext = Path(path).suffix.lower()
    for pattern in _SCREENSHOT_PATTERNS:
        if pattern.match(fname): return True
    if ext == ".png" and PIL_AVAILABLE:
        try:
            with Image.open(path) as img:
                w, h = img.size
                if (w,h) in _SCREEN_RESOLUTIONS or (h,w) in _SCREEN_RESOLUTIONS:
                    return True
        except Exception:
            pass
    return False


# ═══════════════════════════════════════════════════════════════════════════════
#  СТАТИСТИКА
# ═══════════════════════════════════════════════════════════════════════════════

def scan_stats(root, progress_cb=None):
    counts = defaultdict(lambda: {"count": 0, "size": 0})
    def add(ext, size):
        e = ext.lower() if ext else "(без расш.)"
        counts[e]["count"] += 1; counts[e]["size"] += size
    all_files = []
    for dirpath, _, fnames in os.walk(root):
        for fn in fnames: all_files.append(os.path.join(dirpath, fn))
    for i, fpath in enumerate(all_files):
        ext = Path(fpath).suffix.lower(); size = 0
        try: size = os.path.getsize(fpath)
        except Exception: pass
        if ext == ".zip":
            try:
                with open_zip_with_encoding(fpath) as zf:
                    for info in zf.infolist():
                        if not info.is_dir():
                            add(Path(info.filename).suffix.lower(), info.file_size)
            except Exception:
                add(ext, size)
        else:
            add(ext, size)
        if progress_cb and all_files:
            progress_cb(i + 1, len(all_files))
    return dict(counts)


# ═══════════════════════════════════════════════════════════════════════════════
#  КАРТА АРХИВА
# ═══════════════════════════════════════════════════════════════════════════════

def scan_archive_map(root, progress_cb=None):
    result = {}
    year_dirs = []
    try:
        for entry in os.scandir(root):
            if entry.is_dir() and re.match(r'^\d{4}$', entry.name):
                year_dirs.append((int(entry.name), entry.path))
    except Exception:
        pass
    year_dirs.sort()
    for i, (year, year_path) in enumerate(year_dirs):
        months = defaultdict(lambda: {"count": 0, "size": 0})
        for dirpath, _, fnames in os.walk(year_path):
            for fname in fnames:
                if Path(fname).suffix.lower() not in ALL_MEDIA: continue
                rel   = os.path.relpath(dirpath, year_path)
                month = rel.split(os.sep)[0] if rel != "." else "—"
                size  = 0
                try: size = os.path.getsize(os.path.join(dirpath, fname))
                except Exception: pass
                months[month]["count"] += 1
                months[month]["size"]  += size
        result[year] = dict(months)
        if progress_cb and year_dirs:
            progress_cb(i + 1, len(year_dirs))
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  ДУБЛИКАТЫ
# ═══════════════════════════════════════════════════════════════════════════════

def file_md5(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while buf := f.read(chunk):
            h.update(buf)
    return h.hexdigest()

def scan_duplicates(root, extensions=None, progress_cb=None):
    size_map = defaultdict(list)
    for dirpath, _, fnames in os.walk(root):
        for fn in fnames:
            fpath = os.path.join(dirpath, fn)
            ext = Path(fn).suffix.lower()
            if extensions and ext not in extensions: continue
            try:
                size = os.path.getsize(fpath)
                if size > 0: size_map[size].append(fpath)
            except Exception:
                pass
    candidates = [f for files in size_map.values() if len(files) > 1 for f in files]
    total = len(candidates)
    hash_map = defaultdict(list)
    for i, fpath in enumerate(candidates):
        try: hash_map[file_md5(fpath)].append(fpath)
        except Exception: pass
        if progress_cb and total: progress_cb(i + 1, total)
    return {k: v for k, v in hash_map.items() if len(v) > 1}


# ═══════════════════════════════════════════════════════════════════════════════
#  ЗАПИСЬ EXIF
# ═══════════════════════════════════════════════════════════════════════════════

def write_date_to_exif(path, dt):
    if not PIEXIF_AVAILABLE:
        raise ImportError("piexif не установлен")
    if Path(path).suffix.lower() not in (".jpg", ".jpeg"):
        raise ValueError(f"Только JPEG: {Path(path).name}")
    dt_str = dt.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
    try: exif_dict = piexif.load(path)
    except Exception: exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "Interop": {}}
    exif_dict.setdefault("Exif", {}); exif_dict.setdefault("0th", {})
    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal]  = dt_str
    exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = dt_str
    exif_dict["0th"][piexif.ImageIFD.DateTime]          = dt_str
    piexif.insert(piexif.dump(exif_dict), path)


# ═══════════════════════════════════════════════════════════════════════════════
#  РАЗМЕЩЕНИЕ ФАЙЛА
# ═══════════════════════════════════════════════════════════════════════════════

def place_file(src_path, dst_root, dt, mode, conflict,
               subdir_override=None, dry_run=False, rename_by_date=False):
    fname = os.path.basename(src_path)
    base, ext = os.path.splitext(fname)
    if rename_by_date and dt:
        base = dt.strftime("%Y-%m-%d_%H-%M-%S"); fname = base + ext
    media_sub = (subdir_override if subdir_override
                 else ("Видео" if ext.lower() in VIDEO_EXTS else "Фото"))
    target_dir = (os.path.join(dst_root, UNKNOWN_FOLDER, media_sub) if dt is None
                  else os.path.join(dst_root, str(dt.year), MONTH_NAMES[dt.month], media_sub))
    dest_path = os.path.join(target_dir, fname)
    if dry_run:
        if os.path.exists(dest_path) and conflict == "skip":
            return os.path.relpath(dest_path, dst_root), "skip"
        return os.path.relpath(dest_path, dst_root), "ok"
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
                    if not os.path.exists(candidate): dest_path = candidate; break
                    n += 1
        open(dest_path, "ab").close()
    try:
        if mode == "move":
            shutil.copy2(src_path, dest_path)
            if os.path.getsize(dest_path) == os.path.getsize(src_path):
                os.remove(src_path)
            else:
                raise IOError("Размер не совпадает")
        else:
            shutil.copy2(src_path, dest_path)
    except Exception:
        try: os.remove(dest_path)
        except Exception: pass
        raise
    return os.path.relpath(dest_path, dst_root), "ok"


def fmt_size(b):
    if b >= 1 << 30: return f"{b/(1<<30):.1f} ГБ"
    if b >= 1 << 20: return f"{b/(1<<20):.1f} МБ"
    if b >= 1 << 10: return f"{b/(1<<10):.1f} КБ"
    return f"{b} Б"

def category_label(ext):
    if ext in PHOTO_EXTS: return "📷 Фото"
    if ext in VIDEO_EXTS: return "🎬 Видео"
    if ext == ".json":    return "📋 Метаданные"
    if ext == ".zip":     return "🗜 Архив"
    return "📄 Прочее"


# ═══════════════════════════════════════════════════════════════════════════════
#  ПРИЛОЖЕНИЕ
# ═══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"PhotoOrganizer v{VERSION}")
        self.resizable(True, True)
        self.theme = "dark"

        now_year = datetime.now().year
        self.stats_src_var   = tk.StringVar()
        self.src_var         = tk.StringVar()
        self.dst_var         = tk.StringVar()
        self.mode_var        = tk.StringVar(value="copy")
        self.video_var       = tk.BooleanVar(value=True)
        self.zip_var         = tk.BooleanVar(value=True)
        self.conflict_var    = tk.StringVar(value="skip")
        self.threads_var     = tk.IntVar(value=min(os.cpu_count() or 4, 8))
        self.show_src_var    = tk.BooleanVar(value=True)
        self.zip_delete_var  = tk.BooleanVar(value=False)
        self.google_var      = tk.BooleanVar(value=False)
        self.use_mtime_var   = tk.BooleanVar(value=False)
        self.skip_scr_var    = tk.BooleanVar(value=False)
        self.dry_run_var     = tk.BooleanVar(value=False)
        self.rename_var      = tk.BooleanVar(value=False)
        self.year_from_var   = tk.IntVar(value=1980)
        self.year_to_var     = tk.IntVar(value=now_year)
        self.year_filter_var = tk.BooleanVar(value=False)
        self.map_src_var     = tk.StringVar()
        self.scr_root_var    = tk.StringVar()
        self.scr_mode_var    = tk.StringVar(value="move")
        self.dup_src_var     = tk.StringVar()
        self.dup_ext_var     = tk.StringVar(value="media")
        self.exif_src_var    = tk.StringVar()
        self.exif_backup_var = tk.BooleanVar(value=True)

        self._running       = False
        self._paused        = False
        self._pause_ev      = threading.Event(); self._pause_ev.set()
        self._scr_running   = False
        self._stats_running = False
        self._map_running   = False
        self._dup_running   = False
        self._exif_running  = False
        self._dup_data      = {}

        # ── Батчинг лога ─────────────────────────────────────────────────────
        self._log_queue = []
        self._log_lock  = threading.Lock()
        self._start_log_flusher()

        self._build_ui()
        self._center()

    # ── Батчинг лога ─────────────────────────────────────────────────────────

    def _start_log_flusher(self):
        """Сбрасывает накопленные сообщения в лог-виджет раз в 100мс."""
        def flush():
            with self._log_lock:
                msgs = self._log_queue[:]
                self._log_queue.clear()
            if msgs:
                try:
                    w = self.log_widget
                    w.configure(state="normal")
                    for msg, tag in msgs:
                        w.insert("end", msg + "\n", tag)
                    w.see("end")
                    w.configure(state="disabled")
                except Exception:
                    pass
            self.after(100, flush)
        self.after(100, flush)

    def _log_async(self, msg, tag=""):
        """Добавляет сообщение в очередь — не блокирует рабочий поток."""
        with self._log_lock:
            self._log_queue.append((msg, tag))

    # ── Прочие хелперы ───────────────────────────────────────────────────────

    @property
    def C(self):
        return THEMES[self.theme]

    def _center(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(900, sw - 40); h = min(780, sh - 60)
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        self.minsize(720, 600)

    def _toggle_theme(self):
        if any([self._running, self._scr_running, self._stats_running,
                self._map_running, self._dup_running, self._exif_running]):
            messagebox.showinfo("", "Дождитесь завершения операции."); return
        try: tab = self._notebook.index("current")
        except Exception: tab = 0
        self.theme = "light" if self.theme == "dark" else "dark"
        for child in self.winfo_children(): child.destroy()
        self._build_ui()
        try: self._notebook.select(tab)
        except Exception: pass

    def _ui(self, fn):
        self.after(0, fn)

    def _browse(self, var):
        path = filedialog.askdirectory(title="Выберите папку")
        if path: var.set(path)

    def _apply_styles(self):
        C = self.C; s = ttk.Style(self); s.theme_use("clam")
        s.configure("TFrame",         background=C["BG"])
        s.configure("Card.TFrame",    background=C["CARD"])
        s.configure("TLabel",         background=C["BG"],   foreground=C["FG"],   font=("Segoe UI", 9))
        s.configure("Card.TLabel",    background=C["CARD"], foreground=C["FG"],   font=("Segoe UI", 9))
        s.configure("TEntry",         fieldbackground=C["ENTRY"], foreground=C["FG"],
                    insertcolor=C["FG"], font=("Segoe UI", 9))
        s.configure("TProgressbar",   troughcolor=C["CARD"], background=C["ACC"], borderwidth=0)
        s.configure("Accent.TButton", background=C["ACC"],   foreground="white",
                    font=("Segoe UI", 10, "bold"), borderwidth=0, relief="flat", padding=8)
        s.map("Accent.TButton", background=[("active", C["ACC_ACT"]), ("disabled", "#444466")])
        s.configure("Pause.TButton",  background="#444466", foreground="white",
                    font=("Segoe UI", 10, "bold"), borderwidth=0, relief="flat", padding=8)
        s.map("Pause.TButton", background=[("active", "#6666aa"), ("disabled", "#333344")])
        s.configure("Danger.TButton", background=C["RED"],  foreground="white",
                    font=("Segoe UI", 10, "bold"), borderwidth=0, relief="flat", padding=8)
        s.map("Danger.TButton", background=[("active", "#cc3333")])
        s.configure("Neutral.TButton", background=C["CARD"], foreground=C["FG"],
                    font=("Segoe UI", 9), borderwidth=0, relief="flat", padding=6)
        s.map("Neutral.TButton", background=[("active", C["ACC"])],
              foreground=[("active", "white")])
        s.configure("TNotebook",     background=C["BG"], borderwidth=0)
        s.configure("TNotebook.Tab", background=C["CARD"], foreground=C["MUTED"],
                    font=("Segoe UI", 9), padding=[12, 6])
        s.map("TNotebook.Tab",
              background=[("selected", C["ACC"]), ("active", C["CARD"])],
              foreground=[("selected", "white"), ("active", C["FG"])],
              padding=[("selected", [12, 6]), ("active", [12, 6])],
              expand=[("selected", [0, 0, 0, 0])])
        s.configure("Treeview",         background=C["TREE_BG"], foreground=C["TREE_FG"],
                    fieldbackground=C["TREE_BG"], borderwidth=0, font=("Segoe UI", 9), rowheight=22)
        s.configure("Treeview.Heading", background=C["TREE_HEAD"], foreground=C["FG"],
                    font=("Segoe UI", 9, "bold"), borderwidth=0, relief="flat")
        s.map("Treeview.Heading",
              background=[("active", C["ACC"])],
              foreground=[("active", "white")])
        s.map("Treeview", background=[("selected", C["TREE_SEL"])])

    def _build_ui(self):
        C = self.C
        self.configure(bg=C["BG"])
        self._apply_styles()
        hdr = tk.Frame(self, bg=C["BG"], padx=16, pady=8); hdr.pack(fill="x")
        tk.Label(hdr, text="📷  PhotoOrganizer",
                 bg=C["BG"], fg=C["FG"], font=("Segoe UI", 13, "bold")).pack(side="left")
        tk.Label(hdr, text=f"v{VERSION}",
                 bg=C["BG"], fg=C["MUTED"], font=("Segoe UI", 8)).pack(side="left", padx=(8,0), pady=(4,0))
        moon = "☀  Светлая" if self.theme == "dark" else "🌙  Тёмная"
        ttk.Button(hdr, text=moon, style="Neutral.TButton",
                   command=self._toggle_theme).pack(side="right")
        lf = tk.Frame(hdr, bg=C["BG"]); lf.pack(side="right", padx=(0,16))
        for lib, ok in [("exifread", EXIFREAD_AVAILABLE), ("Pillow", PIL_AVAILABLE),
                         ("mutagen", MUTAGEN_AVAILABLE),  ("piexif", PIEXIF_AVAILABLE)]:
            tk.Label(lf, text=f"{'✓' if ok else '✗'} {lib}",
                     bg=C["BG"], fg=C["GREEN"] if ok else C["YELLOW"],
                     font=("Segoe UI", 8)).pack(side="left", padx=(0,8))
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True, padx=8, pady=(0,8))
        for label, builder in [
            ("📊 Статистика",   self._build_stats_tab),
            ("🗂 Сортировка",   self._build_sort_tab),
            ("📅 Карта архива", self._build_map_tab),
            ("📱 Скриншоты",    self._build_screenshots_tab),
            ("🔍 Дубликаты",    self._build_duplicates_tab),
            ("✍ Запись EXIF",  self._build_exif_tab),
        ]:
            frame = tk.Frame(self._notebook, bg=C["BG"])
            self._notebook.add(frame, text=label)
            builder(frame)

    # ── Общие UI хелперы ─────────────────────────────────────────────────────

    def _path_row(self, parent, label, var):
        C = self.C
        f = tk.Frame(parent, bg=C["CARD"], padx=12, pady=8); f.pack(fill="x", pady=(0,6))
        tk.Label(f, text=label, bg=C["CARD"], fg=C["FG"], font=("Segoe UI",9)).pack(anchor="w", pady=(0,4))
        row = tk.Frame(f, bg=C["CARD"]); row.pack(fill="x")
        ttk.Entry(row, textvariable=var, style="TEntry").pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(row, text="Обзор...", bg=C["ACC"], fg="white", relief="flat",
                  font=("Segoe UI",8), padx=8, activebackground=C["ACC_ACT"], activeforeground="white",
                  command=lambda v=var: self._browse(v)).pack(side="left", padx=(6,0), ipady=4)

    def _make_log(self, parent, extra_tags=None):
        C = self.C
        tk.Label(parent, text="Журнал", bg=C["CARD"], fg=C["MUTED"], font=("Segoe UI",8)).pack(anchor="w")
        w = tk.Text(parent, height=7, bg=C["LOG_BG"], fg=C["LOG_FG"],
                    font=("Consolas",8), bd=0, padx=4, pady=4, state="disabled", wrap="word")
        w.tag_configure("head",  foreground=C["MUTED"],   font=("Consolas",8,"bold"))
        w.tag_configure("pause", foreground=C["YELLOW"],  font=("Consolas",8,"bold"))
        w.tag_configure("ok",    foreground=C["GREEN"])
        w.tag_configure("err",   foreground=C["RED"])
        w.tag_configure("skip",  foreground=C["MUTED"])
        if extra_tags:
            for tag, fg in extra_tags.items(): w.tag_configure(tag, foreground=fg)
        sl = ttk.Scrollbar(parent, command=w.yview)
        w.configure(yscrollcommand=sl.set)
        sl.pack(side="right", fill="y"); w.pack(fill="both", expand=True)
        return w

    def _write_log(self, widget, msg, tag=""):
        try:
            widget.configure(state="normal"); widget.insert("end", msg+"\n", tag)
            widget.see("end"); widget.configure(state="disabled")
        except Exception: pass

    def _clear_log(self, widget):
        try:
            widget.configure(state="normal"); widget.delete("1.0","end")
            widget.configure(state="disabled")
        except Exception: pass

    def _log(self,      msg, tag=""): self._write_log(self.log_widget,      msg, tag)
    def _scr_log(self,  msg, tag=""): self._write_log(self.scr_log_widget,  msg, tag)
    def _exif_log(self, msg, tag=""): self._write_log(self.exif_log_widget, msg, tag)

    def _make_sortable(self, tree, columns):
        tree._sort_col = None; tree._sort_rev = False
        tree._col_labels = {col: label for col, label, *_ in columns}
        for col, label, width, anchor in columns:
            tree.heading(col, text=label, command=lambda c=col, t=tree: self._sort_tree(t, c))
            tree.column(col, width=width, anchor=anchor, minwidth=60)

    def _sort_tree(self, tree, col):
        rev = (not tree._sort_rev) if tree._sort_col == col else False
        tree._sort_col = col; tree._sort_rev = rev
        for c, label in tree._col_labels.items():
            arrow = ("  ▼" if rev else "  ▲") if c == col else ""
            tree.heading(c, text=label + arrow, command=lambda tc=c, tt=tree: self._sort_tree(tt, tc))
        items = [(tree.set(i, col), i) for i in tree.get_children()]
        def key(x):
            val = x[0].strip()
            m = re.match(r"([\d.,]+)\s*(ГБ|МБ|КБ|Б)?", val.replace(",","."))
            if m:
                num = float(m.group(1)); unit = m.group(2) or ""
                return num * {"ГБ":1<<30,"МБ":1<<20,"КБ":1<<10,"Б":1}.get(unit, 1)
            clean = re.sub(r"[^\d.]", "", val.replace(",",""))
            try: return float(clean)
            except Exception: return val.lower()
        items.sort(key=key, reverse=rev)
        for idx, (_, iid) in enumerate(items): tree.move(iid, "", idx)

    def _export_sort_log(self):
        try: content = self.log_widget.get("1.0","end")
        except Exception: messagebox.showinfo("","Сначала запустите сортировку."); return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", filetypes=[("Text files","*.txt"),("All files","*.*")],
            initialfile=f"sort_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
        if path:
            with open(path,"w",encoding="utf-8") as f: f.write(content)
            messagebox.showinfo("","Журнал сохранён.")

    # ─────────────────────────────────────────────────────────────────────────
    #  ВКЛАДКА 1: СТАТИСТИКА
    # ─────────────────────────────────────────────────────────────────────────

    def _build_stats_tab(self, parent):
        C = self.C
        root = tk.Frame(parent, bg=C["BG"], padx=12, pady=10); root.pack(fill="both", expand=True)
        tk.Label(root, text="Сканирует папку (включая ZIP-архивы) и показывает статистику по всем форматам.",
                 bg=C["BG"], fg=C["MUTED"], font=("Segoe UI",8)).pack(anchor="w", pady=(0,8))
        self._path_row(root, "Папка для анализа", self.stats_src_var)
        bf = tk.Frame(root, bg=C["BG"]); bf.pack(fill="x", pady=(4,8))
        self._stats_btn = ttk.Button(bf, text="🔍  Сканировать", style="Accent.TButton",
                                     command=self._start_stats)
        self._stats_btn.pack(side="left", ipady=2)
        self._stats_status = tk.Label(bf, text="", bg=C["BG"], fg=C["MUTED"], font=("Segoe UI",8))
        self._stats_status.pack(side="left", padx=(12,0))
        self._stats_progress = ttk.Progressbar(root, mode="determinate")
        self._stats_progress.pack(fill="x", pady=(0,8))
        tf = tk.Frame(root, bg=C["BG"]); tf.pack(fill="both", expand=True)
        self._stats_tree = ttk.Treeview(tf, columns=("ext","count","size","cat"), show="headings")
        self._make_sortable(self._stats_tree, [
            ("ext","Расширение",110,"w"), ("count","Файлов",80,"e"),
            ("size","Размер",110,"e"),    ("cat","Категория",120,"w"),
        ])
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self._stats_tree.yview)
        self._stats_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y"); self._stats_tree.pack(fill="both", expand=True)
        self._stats_summary = tk.Label(root, text="", bg=C["BG"], fg=C["FG"],
                                        font=("Segoe UI",9,"bold"), wraplength=750, justify="left")
        self._stats_summary.pack(anchor="w", pady=(8,0))

    def _start_stats(self):
        src = self.stats_src_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("Ошибка","Выберите корректную папку."); return
        self._stats_btn.configure(state="disabled")
        self._stats_tree.delete(*self._stats_tree.get_children())
        self._stats_summary.configure(text=""); self._stats_progress.configure(value=0)
        self._stats_running = True
        threading.Thread(target=self._worker_stats, args=(src,), daemon=True).start()

    def _worker_stats(self, src):
        def on_prog(d, t):
            pct = int(d/t*100) if t else 0
            self._ui(lambda p=pct,d=d,t=t: (
                self._stats_progress.configure(value=p),
                self._stats_status.configure(text=f"{d}/{t} ({p}%)")))
        result = scan_stats(src, on_prog)
        def upd():
            self._stats_tree.delete(*self._stats_tree.get_children())
            pc=vc=ps=vs=tc=ts=0
            for ext, d in sorted(result.items(), key=lambda x: -x[1]["count"]):
                self._stats_tree.insert("","end",values=(ext,f"{d['count']:,}",fmt_size(d["size"]),category_label(ext)))
                tc+=d["count"]; ts+=d["size"]
                if ext in PHOTO_EXTS: pc+=d["count"]; ps+=d["size"]
                elif ext in VIDEO_EXTS: vc+=d["count"]; vs+=d["size"]
            self._stats_summary.configure(
                text=f"Всего: {tc:,} ({fmt_size(ts)})     Фото: {pc:,} ({fmt_size(ps)})     "
                     f"Видео: {vc:,} ({fmt_size(vs)})     Медиа: {pc+vc:,}")
            self._stats_status.configure(text="✓ Готово")
            self._stats_progress.configure(value=100)
            self._stats_btn.configure(state="normal")
            self._stats_running = False
        self._ui(upd)

    # ─────────────────────────────────────────────────────────────────────────
    #  ВКЛАДКА 2: СОРТИРОВКА
    # ─────────────────────────────────────────────────────────────────────────

    def _build_sort_tab(self, parent):
        C = self.C
        outer = tk.Frame(parent, bg=C["BG"]); outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=C["BG"], highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y"); canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=C["BG"])
        wid = canvas.create_window((0,0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(wid, width=e.width))
        canvas.bind("<Map>", lambda e: canvas.yview_moveto(0))  # сброс позиции скролла
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1*(e.delta/120)),"units"))
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1,"units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1,"units"))
        root = tk.Frame(inner, bg=C["BG"], padx=12, pady=10); root.pack(fill="both", expand=True)

        self._path_row(root, "Источник", self.src_var)
        self._path_row(root, "Цель",     self.dst_var)

        def card():
            f = tk.Frame(root, bg=C["CARD"], padx=12, pady=8); f.pack(fill="x", pady=(0,6)); return f
        def cb(p, text, var, fg=None, bold=False):
            return tk.Checkbutton(p, text=text, variable=var, bg=C["CARD"], fg=fg or C["FG"],
                                  selectcolor=C["SEL"], activebackground=C["CARD"],
                                  activeforeground=fg or C["FG"],
                                  font=("Segoe UI",9,"bold") if bold else ("Segoe UI",9))
        def rb(p, text, var, val):
            return tk.Radiobutton(p, text=text, variable=var, value=val, bg=C["CARD"],
                                  fg=C["FG"], selectcolor=C["SEL"], activebackground=C["CARD"],
                                  activeforeground=C["FG"], font=("Segoe UI",9))

        # Режим
        f = card()
        r = tk.Frame(f, bg=C["CARD"]); r.pack(fill="x", pady=(0,4))
        tk.Label(r, text="Режим:", bg=C["CARD"], fg=C["FG"], font=("Segoe UI",9)).pack(side="left", padx=(0,6))
        rb(r,"Копировать",self.mode_var,"copy").pack(side="left",padx=(0,12))
        rb(r,"Перемещать",self.mode_var,"move").pack(side="left",padx=(0,24))
        cb(r,"Видео",self.video_var).pack(side="left",padx=(0,10))
        cb(r,"ZIP",  self.zip_var  ).pack(side="left",padx=(0,10))
        cb(r,"Удалять ZIP",self.zip_delete_var,fg=C["RED"]).pack(side="left")
        r2 = tk.Frame(f, bg=C["CARD"]); r2.pack(fill="x", pady=(4,0))
        cb(r2,"🧪 Dry Run (тестовый прогон — файлы не перемещаются)",
           self.dry_run_var, fg=C["YELLOW"], bold=True).pack(side="left")

        # Конфликты
        f = card()
        r = tk.Frame(f, bg=C["CARD"]); r.pack(fill="x")
        tk.Label(r, text="Если файл уже есть:", bg=C["CARD"], fg=C["FG"],
                 font=("Segoe UI",9)).pack(side="left",padx=(0,8))
        rb(r,"Пропустить",   self.conflict_var,"skip"     ).pack(side="left",padx=(0,12))
        rb(r,"Перезаписать", self.conflict_var,"overwrite").pack(side="left",padx=(0,12))
        rb(r,"Создать копию",self.conflict_var,"copy"     ).pack(side="left")

        # Потоки + переименование
        f = card()
        r = tk.Frame(f, bg=C["CARD"]); r.pack(fill="x", pady=(0,4))
        tk.Label(r, text="Потоков:", bg=C["CARD"], fg=C["FG"], font=("Segoe UI",9)).pack(side="left",padx=(0,6))
        cpu = os.cpu_count() or 4
        tk.Spinbox(r, from_=1, to=cpu, textvariable=self.threads_var, width=4,
                   bg=C["ENTRY"], fg=C["FG"], buttonbackground=C["CARD"],
                   insertbackground=C["FG"], font=("Segoe UI",9), relief="flat").pack(side="left")
        tk.Label(r, text=f"(макс.{cpu})", bg=C["CARD"], fg=C["MUTED"],
                 font=("Segoe UI",8)).pack(side="left",padx=(4,16))
        cb(r,"Источник даты",self.show_src_var).pack(side="left",padx=(0,10))
        r2 = tk.Frame(f, bg=C["CARD"]); r2.pack(fill="x", pady=(4,0))
        cb(r2,"Переименовать по дате  (IMG_1234.jpg → 2023-06-09_16-36-52.jpg)",
           self.rename_var, fg=C["BLUE"]).pack(side="left")

        # Фильтр по годам
        f = card()
        r = tk.Frame(f, bg=C["CARD"]); r.pack(fill="x")
        cb(r,"Фильтр по годам:", self.year_filter_var, fg=C["ORANGE"]).pack(side="left",padx=(0,8))
        tk.Label(r,text="с",bg=C["CARD"],fg=C["FG"],font=("Segoe UI",9)).pack(side="left",padx=(0,4))
        tk.Spinbox(r,from_=1900,to=2100,textvariable=self.year_from_var,width=6,
                   bg=C["ENTRY"],fg=C["FG"],buttonbackground=C["CARD"],
                   insertbackground=C["FG"],font=("Segoe UI",9),relief="flat").pack(side="left")
        tk.Label(r,text="по",bg=C["CARD"],fg=C["FG"],font=("Segoe UI",9)).pack(side="left",padx=(4,4))
        tk.Spinbox(r,from_=1900,to=2100,textvariable=self.year_to_var,width=6,
                   bg=C["ENTRY"],fg=C["FG"],buttonbackground=C["CARD"],
                   insertbackground=C["FG"],font=("Segoe UI",9),relief="flat").pack(side="left")

        # Google + прочие опции
        f = card()
        cb(f,"Метаданные Google Takeout  (photoTakenTime из .json)",
           self.google_var, fg=C["BLUE"], bold=True).pack(anchor="w")
        cb(f,"Пропускать скриншоты при сортировке",
           self.skip_scr_var, fg=C["ORANGE"]).pack(anchor="w", pady=(4,0))
        cb(f,"Дата файла как резерв если ничего не найдено (file mtime, ненадёжно)",
           self.use_mtime_var, fg=C["MUTED"]).pack(anchor="w", pady=(4,0))

        # Прогресс
        pf = tk.Frame(root, bg=C["CARD"], padx=12, pady=8); pf.pack(fill="x", pady=(0,8))
        top = tk.Frame(pf, bg=C["CARD"]); top.pack(fill="x")
        self.prog_label  = tk.Label(top, text="Готов к работе", bg=C["CARD"], fg=C["FG"], font=("Segoe UI",9))
        self.prog_label.pack(side="left")
        self.stats_label = tk.Label(top, text="", bg=C["CARD"], fg=C["MUTED"], font=("Segoe UI",8))
        self.stats_label.pack(side="right")
        self.progress = ttk.Progressbar(pf, mode="determinate")
        self.progress.pack(fill="x", pady=(4,0))

        # Журнал
        lf = tk.Frame(root, bg=C["CARD"], padx=12, pady=8); lf.pack(fill="both", expand=True, pady=(0,4))
        self.log_widget = self._make_log(lf, extra_tags={
            "warn":C["YELLOW"],"ok":C["GREEN"],"skip":C["MUTED"],
            "nodate":C["ORANGE"],"google":C["BLUE"],"err":C["RED"],
            "scr":C["ORANGE"],"dry":C["YELLOW"],"year":C["MUTED"]})

        # Кнопки
        br = tk.Frame(root, bg=C["BG"]); br.pack(fill="x", pady=(0,4))
        self.btn_start = ttk.Button(br, text="▶  Запустить", style="Accent.TButton",
                                    command=self._start_sort)
        self.btn_start.pack(side="left", fill="x", expand=True, ipady=2)
        self.btn_pause = ttk.Button(br, text="⏸  Пауза", style="Pause.TButton",
                                    command=self._toggle_pause, state="disabled")
        self.btn_pause.pack(side="left", fill="x", expand=True, ipady=2, padx=(6,0))
        ttk.Button(br, text="💾 Экспорт лога", style="Neutral.TButton",
                   command=self._export_sort_log).pack(side="left", padx=(6,0), ipady=2)

    # ─────────────────────────────────────────────────────────────────────────
    #  ВКЛАДКА 3: КАРТА АРХИВА
    # ─────────────────────────────────────────────────────────────────────────

    def _build_map_tab(self, parent):
        C = self.C
        root = tk.Frame(parent, bg=C["BG"], padx=12, pady=10); root.pack(fill="both", expand=True)
        tk.Label(root, text="Показывает количество файлов и объём по годам и месяцам в отсортированном архиве.",
                 bg=C["BG"], fg=C["MUTED"], font=("Segoe UI",8)).pack(anchor="w", pady=(0,8))
        self._path_row(root, "Корень архива (папки 2015, 2016, ...)", self.map_src_var)
        bf = tk.Frame(root, bg=C["BG"]); bf.pack(fill="x", pady=(4,8))
        self._map_btn = ttk.Button(bf, text="📅  Построить карту", style="Accent.TButton",
                                   command=self._start_map)
        self._map_btn.pack(side="left", ipady=2)
        self._map_status = tk.Label(bf, text="", bg=C["BG"], fg=C["MUTED"], font=("Segoe UI",8))
        self._map_status.pack(side="left", padx=(12,0))
        self._map_progress = ttk.Progressbar(root, mode="determinate")
        self._map_progress.pack(fill="x", pady=(0,8))
        tf = tk.Frame(root, bg=C["BG"]); tf.pack(fill="both", expand=True)
        self._map_tree = ttk.Treeview(tf, columns=("period","count","size"), show="headings")
        self._make_sortable(self._map_tree, [
            ("period","Период",200,"w"), ("count","Файлов",80,"e"), ("size","Размер",110,"e"),
        ])
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self._map_tree.yview)
        self._map_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y"); self._map_tree.pack(fill="both", expand=True)
        self._map_summary = tk.Label(root, text="", bg=C["BG"], fg=C["FG"], font=("Segoe UI",9,"bold"))
        self._map_summary.pack(anchor="w", pady=(8,0))

    def _start_map(self):
        src = self.map_src_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("Ошибка","Выберите корректную папку."); return
        self._map_btn.configure(state="disabled")
        self._map_tree.delete(*self._map_tree.get_children())
        self._map_summary.configure(text=""); self._map_progress.configure(value=0)
        self._map_running = True
        threading.Thread(target=self._worker_map, args=(src,), daemon=True).start()

    def _worker_map(self, src):
        def on_prog(d,t):
            pct = int(d/t*100) if t else 0
            self._ui(lambda p=pct: self._map_progress.configure(value=p))
        result = scan_archive_map(src, on_prog)
        def upd():
            self._map_tree.delete(*self._map_tree.get_children())
            tc=ts=0
            for year in sorted(result.keys()):
                months = result[year]
                yc = sum(m["count"] for m in months.values())
                ys = sum(m["size"]  for m in months.values())
                tc+=yc; ts+=ys
                yid = self._map_tree.insert("","end", values=(f"📅 {year}",f"{yc:,}",fmt_size(ys)))
                for month in sorted(months.keys()):
                    d = months[month]
                    self._map_tree.insert(yid,"end", values=(f"    {month}",f"{d['count']:,}",fmt_size(d["size"])))
            self._map_summary.configure(text=f"Итого: {len(result)} лет   {tc:,} файлов   {fmt_size(ts)}")
            self._map_status.configure(text="✓ Готово")
            self._map_progress.configure(value=100)
            self._map_btn.configure(state="normal")
            self._map_running = False
        self._ui(upd)

    # ─────────────────────────────────────────────────────────────────────────
    #  ВКЛАДКА 4: СКРИНШОТЫ
    # ─────────────────────────────────────────────────────────────────────────

    def _build_screenshots_tab(self, parent):
        C = self.C
        root = tk.Frame(parent, bg=C["BG"], padx=12, pady=10); root.pack(fill="both", expand=True)
        tk.Label(root, text="Ищет скриншоты в папках Фото/Видео архива и перемещает в «Скриншоты».",
                 bg=C["BG"], fg=C["MUTED"], font=("Segoe UI",8)).pack(anchor="w", pady=(0,8))
        self._path_row(root, "Корень архива (папки 2015, 2016, ...)", self.scr_root_var)
        opts = tk.Frame(root, bg=C["CARD"], padx=12, pady=10); opts.pack(fill="x", pady=(0,8))
        r = tk.Frame(opts, bg=C["CARD"]); r.pack(fill="x", pady=(0,4))
        tk.Label(r, text="Режим:", bg=C["CARD"], fg=C["FG"], font=("Segoe UI",9)).pack(side="left",padx=(0,8))
        for text, val in [("Переместить","move"),("Копировать","copy")]:
            tk.Radiobutton(r, text=text, variable=self.scr_mode_var, value=val,
                           bg=C["CARD"], fg=C["FG"], selectcolor=C["SEL"],
                           activebackground=C["CARD"], font=("Segoe UI",9)).pack(side="left",padx=(0,12))
        tk.Label(opts, text="Критерии: Screenshot_*, Снимок экрана_*, или PNG с разрешением экрана телефона",
                 bg=C["CARD"], fg=C["MUTED"], font=("Segoe UI",8)).pack(anchor="w", pady=(4,0))
        pf = tk.Frame(root, bg=C["CARD"], padx=12, pady=8); pf.pack(fill="x", pady=(0,8))
        top = tk.Frame(pf, bg=C["CARD"]); top.pack(fill="x")
        self.scr_prog_label  = tk.Label(top, text="Готов к работе", bg=C["CARD"], fg=C["FG"], font=("Segoe UI",9))
        self.scr_prog_label.pack(side="left")
        self.scr_stats_label = tk.Label(top, text="", bg=C["CARD"], fg=C["MUTED"], font=("Segoe UI",8))
        self.scr_stats_label.pack(side="right")
        self.scr_progress = ttk.Progressbar(pf, mode="determinate")
        self.scr_progress.pack(fill="x", pady=(4,0))
        lf = tk.Frame(root, bg=C["CARD"], padx=12, pady=8); lf.pack(fill="both", expand=True, pady=(0,8))
        self.scr_log_widget = self._make_log(lf)
        br = tk.Frame(root, bg=C["BG"]); br.pack(fill="x", pady=(0,4))
        self.btn_scr_start = ttk.Button(br, text="🔍  Найти и переместить скриншоты",
                                        style="Accent.TButton", command=self._start_screenshots)
        self.btn_scr_start.pack(fill="x", ipady=2)

    # ─────────────────────────────────────────────────────────────────────────
    #  ВКЛАДКА 5: ДУБЛИКАТЫ
    # ─────────────────────────────────────────────────────────────────────────

    def _build_duplicates_tab(self, parent):
        C = self.C
        root = tk.Frame(parent, bg=C["BG"], padx=12, pady=10); root.pack(fill="both", expand=True)
        tk.Label(root, text="Поиск дубликатов по MD5. Сначала группировка по размеру, MD5 только для совпадающих.",
                 bg=C["BG"], fg=C["MUTED"], font=("Segoe UI",8), wraplength=700, justify="left").pack(anchor="w",pady=(0,8))
        self._path_row(root, "Папка для поиска дубликатов", self.dup_src_var)
        opts = tk.Frame(root, bg=C["CARD"], padx=12, pady=10); opts.pack(fill="x", pady=(0,8))
        r = tk.Frame(opts, bg=C["CARD"]); r.pack(fill="x")
        tk.Label(r, text="Формат:", bg=C["CARD"], fg=C["FG"], font=("Segoe UI",9)).pack(side="left",padx=(0,8))
        for text, val in [("Только медиа","media"),("Только фото","photo"),("Все файлы","all")]:
            tk.Radiobutton(r, text=text, variable=self.dup_ext_var, value=val,
                           bg=C["CARD"], fg=C["FG"], selectcolor=C["SEL"],
                           activebackground=C["CARD"], font=("Segoe UI",9)).pack(side="left",padx=(0,12))
        self._dup_progress = ttk.Progressbar(root, mode="determinate")
        self._dup_progress.pack(fill="x", pady=(0,8))
        tf = tk.Frame(root, bg=C["BG"]); tf.pack(fill="both", expand=True)
        self._dup_tree = ttk.Treeview(tf, columns=("path","size"), show="tree headings",
                                       selectmode="extended")
        self._dup_tree.heading("path",text="Файл"); self._dup_tree.heading("size",text="Размер")
        self._dup_tree.column("#0",width=30,minwidth=20,stretch=False)
        self._dup_tree.column("path",width=520,anchor="w"); self._dup_tree.column("size",width=90,anchor="e")
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self._dup_tree.yview)
        self._dup_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right",fill="y"); self._dup_tree.pack(fill="both",expand=True)
        self._dup_status = tk.Label(root, text="", bg=C["BG"], fg=C["FG"], font=("Segoe UI",9,"bold"))
        self._dup_status.pack(anchor="w", pady=(6,6))
        br = tk.Frame(root, bg=C["BG"]); br.pack(fill="x", pady=(0,4))
        self._dup_scan_btn = ttk.Button(br, text="🔍  Найти дубликаты", style="Accent.TButton",
                                        command=self._start_duplicates)
        self._dup_scan_btn.pack(side="left", ipady=2)
        ttk.Button(br, text="🗑  Оставить первый, удалить остальные", style="Danger.TButton",
                   command=self._delete_duplicates_auto).pack(side="left", padx=(8,0), ipady=2)
        ttk.Button(br, text="🗑  Удалить выбранные", style="Neutral.TButton",
                   command=self._delete_selected_duplicates).pack(side="left", padx=(6,0), ipady=2)

    # ─────────────────────────────────────────────────────────────────────────
    #  ВКЛАДКА 6: ЗАПИСЬ EXIF
    # ─────────────────────────────────────────────────────────────────────────

    def _build_exif_tab(self, parent):
        C = self.C
        root = tk.Frame(parent, bg=C["BG"], padx=12, pady=10); root.pack(fill="both", expand=True)
        avail_text  = "piexif установлен ✓" if PIEXIF_AVAILABLE else "piexif НЕ установлен ✗ — pip install piexif"
        avail_color = C["GREEN"] if PIEXIF_AVAILABLE else C["RED"]
        tk.Label(root, text=avail_text, bg=C["BG"], fg=avail_color,
                 font=("Segoe UI",9,"bold")).pack(anchor="w", pady=(0,4))
        tk.Label(root, text="Берёт дату из Google JSON (photoTakenTime) и записывает в EXIF JPEG файла.",
                 bg=C["BG"], fg=C["MUTED"], font=("Segoe UI",8), wraplength=700).pack(anchor="w", pady=(0,8))
        self._path_row(root, "Папка с JPEG файлами (и рядом лежащими .json)", self.exif_src_var)
        opts = tk.Frame(root, bg=C["CARD"], padx=12, pady=10); opts.pack(fill="x", pady=(0,8))
        tk.Checkbutton(opts, text="Создавать резервную копию перед записью (.bak)",
                       variable=self.exif_backup_var, bg=C["CARD"], fg=C["FG"],
                       selectcolor=C["SEL"], activebackground=C["CARD"],
                       font=("Segoe UI",9)).pack(anchor="w")
        pf = tk.Frame(root, bg=C["CARD"], padx=12, pady=8); pf.pack(fill="x", pady=(0,8))
        top = tk.Frame(pf, bg=C["CARD"]); top.pack(fill="x")
        self.exif_prog_label  = tk.Label(top, text="Готов к работе", bg=C["CARD"], fg=C["FG"], font=("Segoe UI",9))
        self.exif_prog_label.pack(side="left")
        self.exif_stats_label = tk.Label(top, text="", bg=C["CARD"], fg=C["MUTED"], font=("Segoe UI",8))
        self.exif_stats_label.pack(side="right")
        self.exif_progress = ttk.Progressbar(pf, mode="determinate")
        self.exif_progress.pack(fill="x", pady=(4,0))
        lf = tk.Frame(root, bg=C["CARD"], padx=12, pady=8); lf.pack(fill="both", expand=True, pady=(0,8))
        self.exif_log_widget = self._make_log(lf, extra_tags={
            "ok":C["GREEN"],"skip":C["MUTED"],"err":C["RED"],"nodate":C["ORANGE"]})
        br = tk.Frame(root, bg=C["BG"]); br.pack(fill="x", pady=(0,4))
        self._exif_btn = ttk.Button(br, text="✍  Записать даты в EXIF", style="Accent.TButton",
                                    command=self._start_exif,
                                    state="normal" if PIEXIF_AVAILABLE else "disabled")
        self._exif_btn.pack(fill="x", ipady=2)

    # ── Управление сортировкой ───────────────────────────────────────────────

    def _start_sort(self):
        if self._running: return
        src = self.src_var.get().strip(); dst = self.dst_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("Ошибка","Выберите корректную папку-источник."); return
        if not dst:
            messagebox.showerror("Ошибка","Выберите папку назначения."); return
        if os.path.abspath(src) == os.path.abspath(dst):
            messagebox.showerror("Ошибка","Источник и цель не могут совпадать."); return
        self._clear_log(self.log_widget)
        with self._log_lock: self._log_queue.clear()
        if self.dry_run_var.get():
            self._log("🧪 ТЕСТОВЫЙ ПРОГОН — файлы не перемещаются","dry")
        self._running = True; self._paused = False; self._pause_ev.set()
        self.btn_start.configure(state="disabled")
        self.btn_pause.configure(state="normal", text="⏸  Пауза")
        threading.Thread(target=self._worker_sort, args=(src,dst), daemon=True).start()

    def _toggle_pause(self):
        if not self._running: return
        if self._paused:
            self._paused = False; self._pause_ev.set()
            self.btn_pause.configure(text="⏸  Пауза")
            self._log_async("▶  Продолжение...", "pause")
        else:
            self._paused = True; self._pause_ev.clear()
            self.btn_pause.configure(text="▶  Продолжить")
            self._log_async("⏸  Пауза...", "pause")

    def _sort_done(self):
        def _():
            self._running = False; self._paused = False; self._pause_ev.set()
            self.btn_start.configure(state="normal")
            self.btn_pause.configure(state="disabled", text="⏸  Пауза")
        self.after(0, _)

    def _start_screenshots(self):
        if self._scr_running: return
        src = self.scr_root_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("Ошибка","Выберите корректную папку."); return
        self._clear_log(self.scr_log_widget); self._scr_running = True
        self.btn_scr_start.configure(state="disabled")
        threading.Thread(target=self._worker_screenshots, args=(src,), daemon=True).start()

    def _start_duplicates(self):
        if self._dup_running: return
        src = self.dup_src_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("Ошибка","Выберите корректную папку."); return
        self._dup_tree.delete(*self._dup_tree.get_children())
        self._dup_status.configure(text=""); self._dup_progress.configure(value=0)
        self._dup_data = {}; self._dup_running = True
        self._dup_scan_btn.configure(state="disabled")
        ext_mode = self.dup_ext_var.get()
        exts = (ALL_MEDIA if ext_mode=="media" else PHOTO_EXTS if ext_mode=="photo" else None)
        threading.Thread(target=self._worker_duplicates, args=(src,exts), daemon=True).start()

    def _delete_duplicates_auto(self):
        if not self._dup_data: messagebox.showinfo("","Сначала выполните поиск."); return
        to_delete = []
        for md5, paths in self._dup_data.items():
            to_delete.extend(sorted(paths)[1:])
        if not to_delete: messagebox.showinfo("","Нечего удалять."); return
        ans = messagebox.askyesno("Подтверждение",
            f"Будет удалено {len(to_delete)} файлов. Продолжить?")
        if not ans: return
        errors = 0
        for path in to_delete:
            try: os.remove(path)
            except Exception: errors += 1
        messagebox.showinfo("Готово", f"Удалено: {len(to_delete)-errors}  Ошибок: {errors}")
        self._start_duplicates()

    def _delete_selected_duplicates(self):
        selected = self._dup_tree.selection(); paths_to_delete = []
        for iid in selected:
            if self._dup_tree.parent(iid):
                path = self._dup_tree.set(iid,"path")
                if path and os.path.exists(path): paths_to_delete.append(path)
        if not paths_to_delete: messagebox.showinfo("","Выберите файлы для удаления."); return
        ans = messagebox.askyesno("Подтверждение", f"Удалить {len(paths_to_delete)} файлов?")
        if not ans: return
        errors = 0
        for path in paths_to_delete:
            try: os.remove(path)
            except Exception: errors += 1
        messagebox.showinfo("Готово", f"Удалено: {len(paths_to_delete)-errors}  Ошибок: {errors}")
        self._start_duplicates()

    def _start_exif(self):
        if self._exif_running: return
        src = self.exif_src_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("Ошибка","Выберите корректную папку."); return
        self._clear_log(self.exif_log_widget); self._exif_running = True
        self._exif_btn.configure(state="disabled")
        threading.Thread(target=self._worker_exif, args=(src,), daemon=True).start()

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
        dry_run    = self.dry_run_var.get()
        rename     = self.rename_var.get()
        year_flt   = self.year_filter_var.get()
        year_from  = self.year_from_var.get()
        year_to    = self.year_to_var.get()
        use_mtime  = self.use_mtime_var.get()

        extensions = PHOTO_EXTS | (VIDEO_EXTS if inc_video else set())
        self._log_async("Сканирование...", "head")

        media_files = []; zip_tasks = []
        for root_dir, _, fnames in os.walk(src):
            for fname in fnames:
                fpath = os.path.join(root_dir, fname)
                ext   = Path(fname).suffix.lower()
                if ext in extensions:
                    media_files.append(fpath)
                elif inc_zip and ext == ".zip":
                    try:
                        with open_zip_with_encoding(fpath) as zf:
                            entries = [i for i in zf.infolist()
                                       if not i.is_dir() and Path(i.filename).suffix.lower() in extensions]
                        if entries: zip_tasks.append((fpath, entries))
                    except Exception as e:
                        self._log_async(f"  ERR ZIP scan {fname}: {e}", "err")

        total_zip = sum(len(e) for _,e in zip_tasks)
        total = len(media_files) + total_zip
        if total == 0:
            self._log_async("Файлы не найдены.", "warn")
            self._sort_done(); return

        self._log_async(
            f"Файлов: {len(media_files)} на диске + {total_zip} в {len(zip_tasks)} ZIP"
            f"  |  Итого: {total}  |  Потоков: {n_threads}"
            + ("  |  DRY RUN" if dry_run else "") + "\n", "head")

        lock  = threading.Lock()
        stats = {k:0 for k in ("ok","skip","err","nodate","google","scr","year","done")}

        def _inc(**kw):
            with lock:
                for k,v in kw.items(): stats[k] += v

        def _tick():
            with lock:
                d,o,s,e,n,sc,yr = (stats["done"],stats["ok"],stats["skip"],
                                    stats["err"],stats["nodate"],stats["scr"],stats["year"])
            pct = int(d/total*100)
            self._ui(lambda p=pct,d=d,o=o,s=s,e=e,n=n,sc=sc,yr=yr: (
                self.progress.configure(value=p),
                self.prog_label.configure(text=f"Обработано: {d}/{total}  ({p}%)"),
                self.stats_label.configure(
                    text=f"ok:{o}  пропущено:{s}  скр:{sc}  год:{yr}  нет даты:{n}  ошибок:{e}")))

        def process(fpath, get_date_fn, op_mode):
            self._pause_ev.wait()
            fname = os.path.basename(fpath)
            try:
                if skip_scr and is_screenshot(fpath):
                    _inc(scr=1, done=1); _tick()
                    self._log_async(f"  📱  {fname}  [скриншот — пропущен]", "scr")
                    return "skip_scr"

                dt, date_src = get_date_fn()
                is_google    = (date_src == "Google JSON")

                if year_flt and dt and not (year_from <= dt.year <= year_to):
                    _inc(year=1, done=1); _tick()
                    if show_src:
                        self._log_async(f"  --  {fname}  [год {dt.year} вне диапазона]", "year")
                    return "skip_year"

                no_date = (dt is None)
                rel, result = place_file(fpath, dst, dt, op_mode, conflict,
                                         dry_run=dry_run, rename_by_date=rename)

                if result == "skip":
                    _inc(skip=1, done=1); _tick()
                    if show_src:
                        self._log_async(f"  --  {rel}  [пропущен]", "skip")
                    return "skip"

                if no_date:
                    _inc(nodate=1, ok=1, done=1); _tick()
                    self._log_async(f"  ??  {rel}  [нет даты]  ←  {fpath}", "nodate")
                    return "ok"

                _inc(ok=1, done=1, google=1 if is_google else 0); _tick()
                if show_src:
                    tag = "dry" if dry_run else ("google" if is_google else "ok")
                    pfx = "  →  " if dry_run else "  ok  "
                    self._log_async(f"{pfx}{rel}  [{date_src}]  ←  {fpath}", tag)
                return "ok"

            except Exception as e:
                _inc(err=1, done=1); _tick()
                self._log_async(f"  ERR {fname}: {e}", "err")
                return "err"

        # Медиафайлы
        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futures = {
                pool.submit(process, fp,
                            lambda p=fp: get_best_date(p, use_google=use_google, use_mtime=use_mtime),
                            mode): fp
                for fp in media_files
            }
            for _ in as_completed(futures): pass

        # ZIP-архивы
        for zpath, entries in zip_tasks:
            zname = os.path.basename(zpath)
            self._log_async(f"\n[ZIP] {zname}  ({len(entries)} файлов)", "head")
            zip_errs = [0]
            with open_zip_with_encoding(zpath) as zf:
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
                                    jp = _find_google_json_in_index(media_basename, json_idx)
                                    if jp:
                                        try: zf.extract(jp, tmpdir)
                                        except Exception: pass
                                    if Path(media_basename).suffix.lower() in VIDEO_EXTS and not jp:
                                        stem_ = Path(media_basename).stem
                                        for pext in PHOTO_EXTS:
                                            sib = next((k for k in zf.namelist()
                                                        if os.path.basename(k).lower()==(stem_+pext).lower()), None)
                                            if sib:
                                                try:
                                                    zf.extract(sib, tmpdir)
                                                    sj = _find_google_json_in_index(stem_+pext, json_idx)
                                                    if sj: zf.extract(sj, tmpdir)
                                                except Exception: pass
                                                break
                                def _gd():
                                    dt, ds = get_best_date(extracted, use_google=use_google, use_mtime=use_mtime)
                                    if dt is None and ds == "no-date":
                                        dt2, ds2 = _zip_entry_date(info)
                                        if dt2: return dt2, ds2
                                    return dt, ds
                                return process(extracted, _gd, "copy")
                        except Exception as e:
                            _inc(err=1, done=1); _tick()
                            self._log_async(f"      ERR {fname}: {e}", "err")
                            return "err"
                    return _task

                with ThreadPoolExecutor(max_workers=n_threads) as pool:
                    results = list(pool.map(lambda i: make_task(i)(), entries))
                zip_errs[0] = results.count("err")

            if del_zip and not dry_run and zip_errs[0] == 0:
                try:
                    os.remove(zpath)
                    self._log_async(f"      удалён: {zname}")
                except Exception as e:
                    self._log_async(f"      не удалось удалить: {e}", "warn")

        s = stats
        pfx = "🧪 DRY RUN итог:  " if dry_run else "\n✓ Готово!  "
        parts = [f"ok: {s['ok']}"]
        if s["skip"]:   parts.append(f"пропущено: {s['skip']}")
        if s["scr"]:    parts.append(f"скриншотов: {s['scr']}")
        if s["year"]:   parts.append(f"вне диапазона: {s['year']}")
        if s["nodate"]: parts.append(f"без даты: {s['nodate']}")
        if s["err"]:    parts.append(f"ошибок: {s['err']}")
        if s["google"]: parts.append(f"Google JSON: {s['google']}")
        tag = "dry" if dry_run else ("err" if s["err"] else ("nodate" if s["nodate"] else "ok"))
        self._log_async(f"{pfx}{'   '.join(parts)}", tag)
        self._sort_done()

    # ═══════════════════════════════════════════════════════════════════════════
    #  РАБОЧИЙ ПОТОК: СКРИНШОТЫ
    # ═══════════════════════════════════════════════════════════════════════════

    def _worker_screenshots(self, archive_root):
        mode = self.scr_mode_var.get()
        self._write_log(self.scr_log_widget, "Сканирование архива...", "head")
        candidates = []
        for dirpath, _, fnames in os.walk(archive_root):
            if os.path.basename(dirpath) in ("Фото","Видео"):
                for fname in fnames:
                    if Path(fname).suffix.lower() in ALL_MEDIA:
                        candidates.append(os.path.join(dirpath, fname))
        if not candidates:
            self._write_log(self.scr_log_widget, "Медиафайлы не найдены. Проверь путь.", "warn")
            self._ui(lambda: self.btn_scr_start.configure(state="normal"))
            self._scr_running = False; return
        self._write_log(self.scr_log_widget, f"Найдено {len(candidates)} медиафайлов...\n", "head")
        found=moved=errors=0
        for i, fpath in enumerate(candidates):
            pct = int((i+1)/len(candidates)*100)
            self._ui(lambda p=pct,ii=i,t=len(candidates): (
                self.scr_progress.configure(value=p),
                self.scr_prog_label.configure(text=f"Проверено: {ii+1}/{t}  ({p}%)")))
            if not is_screenshot(fpath): continue
            found += 1; fname = os.path.basename(fpath)
            scr_dir = os.path.join(os.path.dirname(os.path.dirname(fpath)), SCREENSHOT_FOLDER)
            try:
                os.makedirs(scr_dir, exist_ok=True)
                dst = os.path.join(scr_dir, fname)
                if os.path.exists(dst):
                    b, e = os.path.splitext(fname); n = 1
                    while os.path.exists(dst): dst = os.path.join(scr_dir, f"{b}_copy_{n}{e}"); n += 1
                (shutil.move if mode=="move" else shutil.copy2)(fpath, dst)
                rel = os.path.relpath(fpath, archive_root); moved += 1
                self._write_log(self.scr_log_widget, f"  {'→' if mode=='move' else '📋'}  {rel}", "ok")
            except Exception as ex:
                errors += 1
                self._write_log(self.scr_log_widget, f"  ERR  {fname}: {ex}", "err")
        aw = "перемещено" if mode=="move" else "скопировано"
        self._ui(lambda f=found,m=moved,e=errors,aw=aw: (
            self.scr_stats_label.configure(text=f"найдено:{f}  {aw}:{m}  ошибок:{e}"),
            self.scr_progress.configure(value=100),
            self._write_log(self.scr_log_widget,
                f"\n✓ Готово!  найдено: {f}  {aw}: {m}" + (f"  ошибок: {e}" if e else ""),
                "ok" if not e else "err"),
            self.btn_scr_start.configure(state="normal")))
        self._scr_running = False

    # ═══════════════════════════════════════════════════════════════════════════
    #  РАБОЧИЙ ПОТОК: ДУБЛИКАТЫ
    # ═══════════════════════════════════════════════════════════════════════════

    def _worker_duplicates(self, src, exts):
        def on_prog(d,t):
            pct = int(d/t*100) if t else 0
            self._ui(lambda p=pct,d=d,t=t: (
                self._dup_progress.configure(value=p),
                self._dup_status.configure(text=f"Хеширование: {d}/{t} ({p}%)")))
        result = scan_duplicates(src, extensions=exts, progress_cb=on_prog)
        self._dup_data = result
        def upd():
            self._dup_tree.delete(*self._dup_tree.get_children())
            total_waste = 0
            for md5, paths in sorted(result.items(), key=lambda x: -len(x[1])):
                try: sz = os.path.getsize(paths[0])
                except Exception: sz = 0
                waste = sz * (len(paths)-1); total_waste += waste
                gid = self._dup_tree.insert("","end", text="📁",
                    values=(f"Группа {len(paths)} файлов  ·  {fmt_size(sz)} каждый  ·  теряется {fmt_size(waste)}",""))
                for path in sorted(paths):
                    try: fsz = os.path.getsize(path)
                    except Exception: fsz = 0
                    self._dup_tree.insert(gid,"end",text="",values=(path,fmt_size(fsz)))
            total_groups = len(result); total_files = sum(len(v) for v in result.values())
            self._dup_status.configure(
                text=f"Групп: {total_groups}   Копий: {total_files}   Можно освободить: {fmt_size(total_waste)}")
            self._dup_progress.configure(value=100)
            self._dup_scan_btn.configure(state="normal")
            self._dup_running = False
        self._ui(upd)

    # ═══════════════════════════════════════════════════════════════════════════
    #  РАБОЧИЙ ПОТОК: ЗАПИСЬ EXIF
    # ═══════════════════════════════════════════════════════════════════════════

    def _worker_exif(self, src):
        backup = self.exif_backup_var.get()
        self._write_log(self.exif_log_widget, "Поиск JPEG файлов...", "head")
        candidates = []
        for dirpath, _, fnames in os.walk(src):
            for fname in fnames:
                if Path(fname).suffix.lower() in (".jpg",".jpeg"):
                    candidates.append(os.path.join(dirpath, fname))
        if not candidates:
            self._write_log(self.exif_log_widget, "JPEG файлы не найдены.", "err")
            self._ui(lambda: self._exif_btn.configure(state="normal"))
            self._exif_running = False; return
        self._write_log(self.exif_log_widget, f"JPEG файлов: {len(candidates)}\n", "head")
        ok_count=skip_count=err_count=nodate_count=0
        for i, fpath in enumerate(candidates):
            pct = int((i+1)/len(candidates)*100); fname = os.path.basename(fpath)
            self._ui(lambda p=pct,ii=i,t=len(candidates): (
                self.exif_progress.configure(value=p),
                self.exif_prog_label.configure(text=f"Обработано: {ii+1}/{t}  ({p}%)")))
            jp = _find_google_json(fpath)
            if not jp:
                skip_count += 1
                self._write_log(self.exif_log_widget, f"  --  {fname}  [нет JSON]", "skip"); continue
            dt = _google_json_date(jp)
            if not dt:
                nodate_count += 1
                self._write_log(self.exif_log_widget, f"  ??  {fname}  [нет даты в JSON]", "nodate"); continue
            try:
                if backup:
                    bak = fpath + ".bak"
                    if not os.path.exists(bak): shutil.copy2(fpath, bak)
                write_date_to_exif(fpath, dt); ok_count += 1
                self._write_log(self.exif_log_widget,
                    f"  ✓  {fname}  [{dt.strftime('%Y-%m-%d %H:%M:%S')}]", "ok")
            except Exception as e:
                err_count += 1
                self._write_log(self.exif_log_widget, f"  ERR  {fname}: {e}", "err")
        self._ui(lambda o=ok_count,s=skip_count,e=err_count,n=nodate_count: (
            self.exif_stats_label.configure(text=f"записано:{o}  без JSON:{s}  нет даты:{n}  ошибок:{e}"),
            self.exif_progress.configure(value=100),
            self._write_log(self.exif_log_widget,
                f"\n✓ Готово!  Обновлено: {o}   Без JSON: {s}   Нет даты: {n}   Ошибок: {e}",
                "ok" if not e else "err"),
            self._exif_btn.configure(state="normal")))
        self._exif_running = False


if __name__ == "__main__":
    app = App()
    app.mainloop()
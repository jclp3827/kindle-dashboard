"""电子相册：从 NAS 读照片，返回 PNG。"""
import os, glob, subprocess, threading, time
from io import BytesIO
from PIL import Image, ImageOps

PHOTO_ROOT = "/Volumes/摄影图片"
SKIP_DIRS = {"_重复待确认", "#recycle", ".DS_Store"}
EXTS = {".jpg", ".jpeg", ".png", ".nef"}

_state = {"files": [], "idx": 0, "png": None, "last_refresh": 0}
_lock = threading.Lock()

def _scan():
    files = []
    for root, dirs, names in os.walk(PHOTO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for n in names:
            if os.path.splitext(n)[1].lower() in EXTS:
                files.append(os.path.join(root, n))
    files.sort()
    return files

def _make_png(src):
    if src.lower().endswith(".nef"):
        tmpjpg = "/tmp/kindle_nef_tmp.jpg"
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80",
                        "--resampleHeightWidthMax", "2048", src, "--out", tmpjpg],
                       check=True, capture_output=True)
        src = tmpjpg
    im = Image.open(src)
    im = ImageOps.exif_transpose(im)
    im = im.convert("L")
    tw, th = 1024, 758
    sr = im.width / im.height
    tr = tw / th
    if sr > tr:
        nw, nh = tw, round(im.height * tw / im.width)
    else:
        nh, nw = th, round(im.width * th / im.height)
    im = im.resize((nw, nh), Image.LANCZOS)
    im = ImageOps.autocontrast(im)
    canvas = Image.new("L", (tw, th), 255)
    canvas.paste(im, ((tw - nw) // 2, (th - nh) // 2))
    buf = BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()

def get_photo(advance=True):
    with _lock:
        if not os.path.ismount(PHOTO_ROOT):
            return None
        now = time.time()
        if not _state["files"] or now - _state["last_refresh"] > 3600:
            _state["files"] = _scan()
            _state["last_refresh"] = now
        if not _state["files"]:
            return None
        if advance:
            _state["idx"] = (_state["idx"] + 1) % len(_state["files"])
        for _ in range(5):
            try:
                src = _state["files"][_state["idx"] % len(_state["files"])]
                png = _make_png(src)
                _state["png"] = png
                _state["idx"] += 1
                return png
            except Exception:
                _state["idx"] += 1
        return None

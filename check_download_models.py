"""
check_and_download_models.py  —  Cross-platform Piper model manager
====================================================================
Works on Windows CMD, PowerShell, Linux, macOS.

Usage:
    python check_and_download_models.py              # check only
    python check_and_download_models.py --download   # check + download missing/corrupt
    python check_and_download_models.py --download --dir C:/my/models

CHANGE vs previous version:
    Each voice now has its own MIN_BYTES threshold based on the real
    file size on HuggingFace.  A flat 5 MB guard let corrupt partial
    downloads (e.g. ryan-high at 33 MB instead of 63 MB) pass silently,
    causing ONNXRuntime "Protobuf parsing fail" at runtime.
"""

import argparse
import os
import sys
import time
import urllib.request
from pathlib import Path

# ── Voice catalog ─────────────────────────────────────────────────────────────
# Each entry:  voice_key -> (hf_onnx_path, hf_json_path, min_onnx_bytes)
#
# min_onnx_bytes = 90 % of the real file size on HuggingFace @ v1.0.0.
# Real sizes (measured):
#   lessac-high       113 MB   → min 100 MB
#   ryan-high          63 MB   → min  57 MB
#   lessac-medium      63 MB   → min  57 MB
#   ryan-medium        63 MB   → min  57 MB
#   alan-medium        28 MB   → min  25 MB
#   jenny_dioco-medium 63 MB   → min  57 MB

HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

VOICE_CATALOG = {
    "en_US_female_high": (
        "en/en_US/lessac/high/en_US-lessac-high.onnx",
        "en/en_US/lessac/high/en_US-lessac-high.onnx.json",
        100_000_000,   # min 100 MB  (real: ~113 MB)
    ),
    "en_US_male_high": (
        "en/en_US/ryan/high/en_US-ryan-high.onnx",
        "en/en_US/ryan/high/en_US-ryan-high.onnx.json",
        57_000_000,    # min  57 MB  (real:  ~63 MB)
    ),
    "en_US_female_medium": (
        "en/en_US/lessac/medium/en_US-lessac-medium.onnx",
        "en/en_US/lessac/medium/en_US-lessac-medium.onnx.json",
        57_000_000,    # min  57 MB  (real:  ~63 MB)
    ),
    "en_US_male_medium": (
        "en/en_US/ryan/medium/en_US-ryan-medium.onnx",
        "en/en_US/ryan/medium/en_US-ryan-medium.onnx.json",
        57_000_000,    # min  57 MB  (real:  ~63 MB)
    ),
    "en_GB_male_medium": (
        "en/en_GB/alan/medium/en_GB-alan-medium.onnx",
        "en/en_GB/alan/medium/en_GB-alan-medium.onnx.json",
        25_000_000,    # min  25 MB  (real:  ~28 MB)
    ),
    "en_GB_female_medium": (
        "en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium.onnx",
        "en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium.onnx.json",
        57_000_000,    # min  57 MB  (real:  ~63 MB)
    ),
}

MIN_JSON_BYTES = 100   # JSON configs are a few KB — anything less is corrupt

# ── Colour output ─────────────────────────────────────────────────────────────

RESET  = "\033[0m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"

def _supports_colour():
    if sys.platform == "win32":
        return (
            os.environ.get("WT_SESSION") is not None
            or os.environ.get("TERM_PROGRAM") is not None
            or "ANSICON" in os.environ
        )
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

USE_COLOUR = _supports_colour()

def col(colour, text):
    return f"{colour}{text}{RESET}" if USE_COLOUR else text

def ok(msg):   print(col(GREEN,  f"  [OK]      {msg}"))
def miss(msg): print(col(RED,    f"  [MISSING] {msg}"))
def warn(msg): print(col(YELLOW, f"  [WARN]    {msg}"))
def info(msg): print(col(CYAN,   f"  [INFO]    {msg}"))
def head(msg): print(col(BOLD,   msg))

# ── Progress bar ──────────────────────────────────────────────────────────────

class _Progress:
    def __init__(self, filename):
        self.filename = filename
        self._last = -1

    def __call__(self, block, block_size, total):
        if total <= 0:
            return
        done = block * block_size
        pct  = min(int(done * 100 / total), 100)
        if pct != self._last:
            bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
            print(
                f"\r  [{bar}] {pct:3d}%"
                f"  {done/1e6:.1f}/{total/1e6:.1f} MB"
                f"  {self.filename}",
                end="", flush=True,
            )
            self._last = pct
        if pct == 100:
            print()

# ── Download ──────────────────────────────────────────────────────────────────

def download_file(url, dest, min_bytes=1, retries=3):
    """Download url → dest with progress + retry. Returns True on success."""
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    for attempt in range(1, retries + 1):
        try:
            urllib.request.urlretrieve(
                f"{url}?download=true", str(tmp),
                reporthook=_Progress(dest.name),
            )
            size = tmp.stat().st_size
            if size >= min_bytes:
                tmp.replace(dest)
                return True
            # File is too small — treat as corrupt
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"Incomplete download: got {size/1e6:.1f} MB, "
                f"expected >= {min_bytes/1e6:.1f} MB"
            )
        except Exception as exc:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            if attempt < retries:
                wait = 2 ** attempt
                warn(f"Attempt {attempt}/{retries} failed: {exc}  — retry in {wait}s")
                time.sleep(wait)
            else:
                print(col(RED, f"\n  [FAIL] {dest.name}: {exc}"))
                return False
    return False

# ── File status ───────────────────────────────────────────────────────────────

def file_status(path, min_bytes):
    """Returns ('ok' | 'missing' | 'corrupt', actual_size)."""
    if not path.exists():
        return "missing", 0
    size = path.stat().st_size
    if size < min_bytes:
        return "corrupt", size
    return "ok", size

# ── Core logic ────────────────────────────────────────────────────────────────

def _print_table(models_dir):
    """Print status table. Returns list of (voice_key, dest, url, min_bytes) to fix."""
    to_fix = []
    print(
        f"\n  {'VOICE KEY':<28}  {'ONNX':<22}  {'JSON':<10}  "
        f"{'ACTUAL':>10}  {'REQUIRED':>10}"
    )
    print(
        f"  {'─'*28}  {'─'*22}  {'─'*10}  "
        f"{'─'*10}  {'─'*10}"
    )

    for voice_key, (onnx_rel, json_rel, min_onnx) in VOICE_CATALOG.items():
        onnx_path = models_dir / f"{voice_key}.onnx"
        json_path = models_dir / f"{voice_key}.onnx.json"

        onnx_st, onnx_sz = file_status(onnx_path, min_onnx)
        json_st, _       = file_status(json_path, MIN_JSON_BYTES)

        onnx_label = {
            "ok":      col(GREEN,  "OK"),
            "missing": col(RED,    "MISSING"),
            "corrupt": col(YELLOW, f"CORRUPT ({onnx_sz/1e6:.0f} MB)"),
        }[onnx_st]

        json_label = {
            "ok":      col(GREEN,  "OK"),
            "missing": col(RED,    "MISSING"),
            "corrupt": col(YELLOW, "CORRUPT"),
        }[json_st]

        actual_str   = f"{onnx_sz/1e6:.0f} MB" if onnx_sz else "—"
        required_str = f"{min_onnx/1e6:.0f} MB"

        print(
            f"  {voice_key:<28}  {onnx_label:<31}  {json_label:<19}  "
            f"{actual_str:>10}  {required_str:>10}"
        )

        if onnx_st != "ok":
            if onnx_path.exists():
                onnx_path.unlink()   # remove corrupt file before re-download
            to_fix.append((voice_key, onnx_path, f"{HF_BASE}/{onnx_rel}", min_onnx))
        if json_st != "ok":
            if json_path.exists():
                json_path.unlink()
            to_fix.append((voice_key, json_path, f"{HF_BASE}/{json_rel}", MIN_JSON_BYTES))

    return to_fix


def check_and_download(models_dir, do_download):
    models_dir.mkdir(parents=True, exist_ok=True)

    head("\n" + "=" * 70)
    head("  Piper TTS — Model Status" + (" + Download" if do_download else ""))
    head("=" * 70)
    print(f"\n  Models dir : {models_dir.resolve()}")

    to_fix = _print_table(models_dir)

    total_sz = sum(
        (models_dir / f"{k}.onnx").stat().st_size
        for k in VOICE_CATALOG
        if (models_dir / f"{k}.onnx").exists()
    )
    print(f"\n  Total on disk : {total_sz/1e6:.0f} MB")

    if not to_fix:
        print(col(GREEN, "\n  All 6 models present and healthy. ✅"))
        print("\n  You can start the stack:")
        print(col(CYAN, "    docker-compose -f docker-compose.prod.yml up -d\n"))
        return True

    print(col(RED, f"\n  {len(to_fix)} file(s) missing or corrupt."))

    if not do_download:
        print(col(YELLOW, "\n  Re-run with --download to fix:"))
        print(col(CYAN,   "    python check_and_download_models.py --download\n"))
        return False

    # ── Download / re-download ────────────────────────────────
    head("\n" + "=" * 70)
    head("  Downloading…")
    head("=" * 70 + "\n")

    failed = []
    for voice_key, dest, url, min_bytes in to_fix:
        info(f"Fetching {dest.name}  (min {min_bytes/1e6:.0f} MB)…")
        if download_file(url, dest, min_bytes=min_bytes):
            ok(f"{dest.name}  →  {dest.stat().st_size/1e6:.1f} MB")
        else:
            failed.append(dest.name)

    # ── Final table ───────────────────────────────────────────
    head("\n" + "=" * 70)
    head("  Final status")
    head("=" * 70)
    _print_table(models_dir)   # recheck — to_fix result ignored

    print()
    if failed:
        print(col(RED, f"  {len(failed)} file(s) still failed: {', '.join(failed)}"))
        print(col(YELLOW, "  Check your connection and re-run.\n"))
        return False

    print(col(GREEN, "  All models ready. ✅"))
    print("\n  Rebuild workers to pick up new models:")
    print(col(CYAN,
        "    docker-compose -f docker-compose.prod.yml "
        "up -d --build worker-1 worker-2\n"
    ))
    return True


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if sys.platform == "win32":
        os.system("")   # enable ANSI on Windows CMD

    p = argparse.ArgumentParser(
        description="Check and optionally download all 6 Piper TTS voice models."
    )
    p.add_argument(
        "--download", "-d", action="store_true",
        help="Download missing or corrupt models (default: check only)",
    )
    p.add_argument(
        "--dir", default="./piper_models",
        help="Models directory (default: ./piper_models)",
    )
    args = p.parse_args()

    ok_result = check_and_download(
        models_dir=Path(args.dir),
        do_download=args.download,
    )
    sys.exit(0 if ok_result else 1)
"""Translate every raw (ja/zh/ko) chapter in the library, while the PC is idle.

    .venv\\Scripts\\python.exe web\\mt_batch.py            # run until everything is done
    .venv\\Scripts\\python.exe web\\mt_batch.py --once     # one pass, then exit

A standalone worker (it never imports api.py, which would start a second copy of
the server's import queue). Order: chapters that arrived through imports first
(the ones being read), then the library's Japanese, then Chinese/Korean series.
Chapters already translated by the current pipeline version are skipped; files
from an older version are redone. Waits for idle time (no keyboard/mouse for
--idle minutes, GPU free) and steps aside while the server translates something
itself. Progress: ~/.mangashelf/mt_batch.log
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

import translate as mt                      # noqa: E402
from database import Database               # noqa: E402
from scanner import get_series_chapters     # noqa: E402

LOG = Path.home() / ".mangashelf" / "mt_batch.log"
TOKEN = Path.home() / ".mangashelf" / "web_token.txt"


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def server_busy() -> bool:
    """True while the MangaShelf server is running a translation of its own."""
    try:
        req = urllib.request.Request("http://127.0.0.1:8000/api/translate/status",
                                     headers={"X-Mangashelf-Token": TOKEN.read_text().strip()})
        with urllib.request.urlopen(req, timeout=10) as r:
            return any(j.get("status") == "running" for j in json.loads(r.read()).get("jobs", []))
    except Exception:
        return False


FAILED = "translation.failed.json"   # one bad chapter must not block the queue forever


def done(ch_dir: Path) -> bool:
    if (ch_dir / FAILED).exists():
        return True
    f = ch_dir / mt.TRANSLATION_FILE
    try:
        return int(json.loads(f.read_text(encoding="utf-8")).get("v") or 0) >= mt.PIPELINE_VERSION
    except Exception:
        return False


def todo(db: Database) -> list[tuple[int, str, Path, str]]:
    """(priority, title, chapter dir, lang) for every raw chapter not yet done."""
    out = []
    for s in db.get_series_list():
        folder = Path(str(s.get("folder_path") or ""))
        if not folder.is_dir():
            continue
        series_lang = mt.series_language(str(s.get("title") or folder.name), str(s.get("language") or ""))
        try:
            chapters = get_series_chapters(str(folder))
        except Exception:
            continue
        for ch in chapters:
            d = Path(ch.path)
            if not d.is_dir():
                continue                       # PDFs/archives: nothing to overlay
            lang = mt.chapter_language(d) or series_lang
            if lang not in mt.RAW_LANGS or done(d):
                continue
            prio = 0 if (d / mt.CHAPTER_INFO).exists() else (1 if lang == "ja" else 2)
            out.append((prio, str(s.get("title") or folder.name), d, lang))
    out.sort(key=lambda t: (t[0], t[1].lower(), str(t[2])))
    return out


class LlamaServer:
    """llama.cpp's llama-server for a big MoE model (experts in system RAM via
    --n-cpu-moe). It holds ~10-15 GB of RAM, so it runs only while the PC is
    idle: started before work, stopped the moment the owner is back."""
    EXE = r"D:\llamacpp\bin\llama-server.exe"

    def __init__(self, gguf: str, n_cpu_moe: int):
        self.gguf, self.n_cpu_moe, self.proc = gguf, n_cpu_moe, None

    def up(self) -> bool:
        try:
            with urllib.request.urlopen(f"{mt.LLAMACPP}/health", timeout=5) as r:
                return b"ok" in r.read()
        except Exception:
            return False

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        import subprocess
        log(f"starting llama-server ({Path(self.gguf).name})")
        port = mt.LLAMACPP.rsplit(":", 1)[1]
        self.proc = subprocess.Popen(
            [self.EXE, "-m", self.gguf, "-ngl", "99", "--n-cpu-moe", str(self.n_cpu_moe),
             "-c", "8192", "-fa", "on", "-ub", "2048", "-b", "2048", "-t", "6", "-np", "1",
             # Load mode "none" (no mmap): read the GGUF (on a hard drive) into RAM
             # once, instead of paging pieces in mid-translation (cold start took 9 min).
             "--load-mode", "none", "--jinja", "--host", "127.0.0.1", "--port", port],
            stdout=subprocess.DEVNULL, stderr=open(LOG.with_name("llama-server.log"), "w"),
            creationflags=0x08000000)   # CREATE_NO_WINDOW
        for _ in range(240):            # loading ~20 GB from disk takes a while
            if self.up():
                return
            if self.proc.poll() is not None:
                raise RuntimeError("llama-server exited while loading (see llama-server.log)")
            time.sleep(5)
        raise RuntimeError("llama-server did not come up")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            log("stopping llama-server (PC in use)")
            self.proc.terminate()
            self.proc.wait(timeout=60)
        self.proc = None


LLAMA: LlamaServer | None = None


def wait_idle(idle_min: int) -> None:
    announced = False
    while True:
        idle = mt.idle_seconds()
        if idle >= idle_min * 60 and not mt.gpu_busy() and not server_busy():
            if LLAMA:
                LLAMA.start()
            return
        if LLAMA:
            LLAMA.stop()                   # give the RAM back while the owner uses the PC
        if not announced:
            log(f"waiting (idle {int(idle)}s, need {idle_min * 60}s; GPU/server busy is checked too)")
            announced = True
        time.sleep(30)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--idle", type=int, default=10, help="minutes without input before working")
    ap.add_argument("--model", default=mt.DEFAULT_MODEL)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--gguf", help="run this MoE GGUF in llama-server instead of the Ollama --model")
    ap.add_argument("--n-cpu-moe", type=int, default=99, help="layers whose experts stay in RAM")
    a = ap.parse_args()
    global LLAMA
    if a.gguf:
        LLAMA = LlamaServer(a.gguf, a.n_cpu_moe)
        a.model = "llamacpp:" + Path(a.gguf).stem
    # Runs hidden: everything (incl. the translator's per-batch timings and any
    # rejected model replies) goes to the log file.
    LOG.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)
    db = Database()
    while True:
        jobs = todo(db)                        # one library scan per pass (3k+ series)
        log(f"{len(jobs)} raw chapters to translate")
        if not jobs:
            if a.once:
                return
            time.sleep(600)                    # new raws arrive with imports; look again later
            continue
        for prio, title, d, lang in jobs:
            if done(d) or not d.is_dir():
                continue
            wait_idle(a.idle)
            t0 = time.time()
            try:
                n = mt.translate_chapter(d, lang, a.model, wait=lambda: wait_idle(a.idle),
                                         work=title, progress=lambda m: None)
                log(f"OK  {title[:70]} / {d.name}: {n} lines in {time.time() - t0:.0f}s")
            except Exception as exc:  # noqa: BLE001
                log(f"ERR {title[:70]} / {d.name}: {exc}")
                try:
                    (d / FAILED).write_text(json.dumps({"error": str(exc)[:300]}), encoding="utf-8")
                except OSError:
                    pass
        if a.once:
            return


if __name__ == "__main__":
    main()

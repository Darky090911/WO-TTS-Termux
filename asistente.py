#!/usr/bin/env python3
"""
WO-TTS v2.2 — Asistente de voz + chat con Ollama (Termux).

Cambios v2.2:
  - FIX: al escribir (tecla 'e') se ve el texto mientras escribes
  - FIX: los menus interactivos ya no "tragan" las teclas

Cambios v2.1:
  - FIX: TTS ya no mata el proceso principal
  - FIX: timestamp del usuario ya no se sale de linea

Teclas: g grabar, e escribir, f manos libres, t TTS, d pensamiento,
        m modelo, o opciones, v historial, c compacto, a animaciones,
        z verboso, l limpiar, i estado, s guardar, r reiniciar,
        h ayuda, q salir.

Requiere: Termux + termux-api + whisper + ollama + sshkeyboard
"""

import os
import re
import sys
import time
import json
import queue
import shutil
import signal
import argparse
import subprocess
import threading
from datetime import datetime
from pathlib import Path

# readline puede empeorar el eco en algunos builds de Termux.
# Si los menus no muestran lo que escribes, deja esto en None.
try:
    import readline  # noqa: F401
except ImportError:
    readline = None

try:
    from sshkeyboard import listen_keyboard, stop_listening
except ImportError:
    print("[ERROR] pip install sshkeyboard")
    sys.exit(1)

try:
    from ollama import chat, ResponseError, RequestError
except ImportError:
    print("[ERROR] pip install ollama")
    sys.exit(1)


# ============================================================
#  CONFIG
# ============================================================
VERSION = "2.2"
DEFAULT_MODEL = "gpt-oss:120b-cloud"
DEFAULT_SYSTEM = (
    "Eres un asistente util, preciso y amable. "
    "Respondes en el mismo idioma en el que se te habla, de forma clara y concisa."
)
DEFAULT_TEMP = 0.7
DEFAULT_CTX = 4096

HISTORY_DIR = Path.home() / ".ollama_chats"
AUDIO_DIR = Path.home() / "storage" / "music" / "Grabaciones"
AUDIO_FILE = AUDIO_DIR / "audio_transcript.m4a"
TRANSCRIPT_FILE = AUDIO_DIR / "audio_transcript_TRANSCRIPT.txt"
AUDIO_BACKUP_DIR = HISTORY_DIR / "audios"
CONFIG_FILE = HISTORY_DIR / "config.json"
LOG_FILE = HISTORY_DIR / "log.jsonl"
TMPDIR = Path(os.environ.get("TMPDIR", "/tmp"))
TMP_HIST = TMPDIR / f"asistente_hist_{os.getpid()}.txt"
WORKSPACE = Path(__file__).parent.resolve()

AUTOSUMARIZE_RATIO = 0.75
OLLAMA_CACHE_TTL = 30


# ============================================================
#  PALETA
# ============================================================
class C:
    TXT   = "\033[38;5;252m"
    DIM   = "\033[38;5;245m"
    MUTE  = "\033[38;5;240m"
    FAINT = "\033[38;5;237m"
    ACC   = "\033[38;5;111m"
    ACC2  = "\033[38;5;183m"
    OK    = "\033[38;5;114m"
    WARN  = "\033[38;5;222m"
    ERR   = "\033[38;5;210m"
    BOLD  = "\033[1m"
    ITAL  = "\033[3m"
    RESET = "\033[0m"


BANNER_GRADIENT = [
    "\033[38;5;51m", "\033[38;5;45m", "\033[38;5;39m", "\033[38;5;33m",
    "\033[38;5;63m", "\033[38;5;99m", "\033[38;5;141m", "\033[38;5;177m",
    "\033[38;5;213m",
]


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
ANIMATIONS = True


def vlen(s):
    return len(_ANSI_RE.sub("", s))


def color(t, c):
    return f"{c}{t}{C.RESET}"


def truncate_ansi(text, max_visible):
    out = []
    visible = 0
    i = 0
    n = len(text)
    while i < n and visible < max_visible:
        if text[i] == "\033" and i + 1 < n and text[i + 1] == "[":
            j = i + 2
            while j < n and text[j] not in "mK":
                j += 1
            if j < n:
                out.append(text[i:j + 1])
                i = j + 1
                continue
        out.append(text[i])
        visible += 1
        i += 1
    out.append(C.RESET)
    return "".join(out)


# ============================================================
#  STDOUT CON LOCK
# ============================================================
_OUT_LOCK = threading.RLock()


class LockedStream:
    def __init__(self, stream):
        self._stream = stream

    def write(self, s):
        with _OUT_LOCK:
            return self._stream.write(s)

    def flush(self):
        with _OUT_LOCK:
            return self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


_RAW_STDOUT = None


def raw_write(s):
    with _OUT_LOCK:
        _RAW_STDOUT.write(s)
        _RAW_STDOUT.flush()


# ============================================================
#  COLA DE TAREAS + LISTENER CONTROLABLE
# ============================================================
TASK_QUEUE = queue.Queue()
_listener_lock = threading.Lock()
_listener_thread = None


def push_key(tecla):
    TASK_QUEUE.put(tecla)


def _listener_runner():
    try:
        listen_keyboard(on_press=push_key, until=None, sequential=False)
    except Exception:
        pass


def start_listener():
    global _listener_thread
    with _listener_lock:
        if _listener_thread and _listener_thread.is_alive():
            return
        _listener_thread = threading.Thread(target=_listener_runner, daemon=True)
        _listener_thread.start()


def stop_listener():
    """Detiene el listener y espera a que suelte stdin."""
    global _listener_thread
    with _listener_lock:
        try:
            stop_listening()
        except Exception:
            pass
        if _listener_thread:
            try:
                _listener_thread.join(timeout=1.5)
            except Exception:
                pass
        _listener_thread = None
    # Dar tiempo a termios para restaurar modo normal (ICANON + ECHO)
    time.sleep(0.15)


def drain_queue():
    while True:
        try:
            TASK_QUEUE.get_nowait()
        except queue.Empty:
            return


# ============================================================
#  PANTALLA
# ============================================================
class Screen:
    STATUS_LINES = 3

    def __init__(self):
        self.rows = 24
        self.cols = 80
        self.enabled = False
        self._running = False
        self._thread = None
        self._provider = None
        self._toast_msg = ""
        self._toast_until = 0.0
        self._toast_queue = []
        self._lock = threading.RLock()

    def _size(self):
        try:
            s = shutil.get_terminal_size()
            return s.lines, max(40, s.columns)
        except Exception:
            return 24, 80

    def start(self):
        self.rows, self.cols = self._size()
        bot = self.rows - self.STATUS_LINES
        if bot < 6:
            return False
        raw_write("\033[2J\033[H")
        raw_write(f"\033[1;{bot}r")
        raw_write(f"\033[{bot};1H")
        self.enabled = True
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if not self.enabled:
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)
        raw_write("\033[r")
        raw_write(f"\033[{self.rows};1H\n")
        self.enabled = False

    def set_provider(self, fn):
        self._provider = fn

    def toast(self, msg, duration=2.0):
        with self._lock:
            if self._toast_msg and time.time() < self._toast_until:
                if len(self._toast_queue) < 5:
                    self._toast_queue.append((msg, duration))
            else:
                self._toast_msg = msg
                self._toast_until = time.time() + duration

    def resize(self):
        self.rows, self.cols = self._size()
        bot = self.rows - self.STATUS_LINES
        if bot < 6:
            return
        with _OUT_LOCK:
            _RAW_STDOUT.write(f"\033[1;{bot}r")
            _RAW_STDOUT.flush()

    def clear(self):
        with _OUT_LOCK:
            if not self.enabled:
                _RAW_STDOUT.write("\033[2J\033[H")
                _RAW_STDOUT.flush()
                return
            bot = self.rows - self.STATUS_LINES
            for row in range(1, bot + 1):
                _RAW_STDOUT.write(f"\033[{row};1H\033[2K")
            _RAW_STDOUT.write("\033[1;1H")
            _RAW_STDOUT.flush()

    def _loop(self):
        while self._running:
            try:
                lines = self._provider() if self._provider else [""] * self.STATUS_LINES
            except Exception:
                lines = [""] * self.STATUS_LINES
            lines = list(lines)
            with self._lock:
                if self._toast_msg:
                    if time.time() < self._toast_until:
                        if len(lines) > 1:
                            lines[1] = self._toast_msg
                    else:
                        self._toast_msg = ""
                        if self._toast_queue:
                            msg, dur = self._toast_queue.pop(0)
                            self._toast_msg = msg
                            self._toast_until = time.time() + dur
                            if len(lines) > 1:
                                lines[1] = self._toast_msg
            self._redraw(lines)
            time.sleep(0.1)

    def _redraw(self, lines):
        rows = self.rows
        cols = self.cols
        top = rows - self.STATUS_LINES + 1
        parts = ["\0337"]
        for i in range(self.STATUS_LINES):
            row = top + i
            text = lines[i] if i < len(lines) else ""
            text = truncate_ansi(text, cols)
            parts.append(f"\033[{row};1H\033[2K")
            parts.append(text)
        parts.append("\0338")
        with _OUT_LOCK:
            _RAW_STDOUT.write("".join(parts))
            _RAW_STDOUT.flush()


SCREEN = Screen()


# ============================================================
#  HELPERS UI
# ============================================================
def hline(n=None, c=None):
    if n is None:
        n = SCREEN.cols
    if c is None:
        c = C.FAINT
    print(color("─" * n, c))


def fit_plain(text, width):
    if len(text) > width:
        return "…" + text[-(width - 1):]
    return text + " " * (width - len(text))


def section_header(title, glyph="◈", glyph_color=None):
    if glyph_color is None:
        glyph_color = C.ACC2
    cols = SCREEN.cols if SCREEN.cols else 80
    print()
    print("  " + color(glyph, glyph_color + C.BOLD) + " " + color(title, C.TXT + C.BOLD))
    print("  " + color("─" * (cols - 4), C.FAINT))
    print()


def _apply_md(line):
    if line.startswith("### "):
        return color(line[4:], C.BOLD + C.TXT)
    if line.startswith("## "):
        return color(line[3:], C.BOLD + C.TXT)
    if line.startswith("# "):
        return color(line[2:], C.BOLD + C.ACC)
    if line.startswith("> "):
        return color("│ ", C.MUTE) + color(line[2:], C.DIM + C.ITAL)
    stripped = line.lstrip()
    indent = line[:len(line) - len(stripped)]
    if stripped.startswith("- ") or stripped.startswith("* "):
        line = indent + "• " + stripped[2:]
    out = []
    i = 0
    n = len(line)
    while i < n:
        if line[i:i+2] == "**":
            end = line.find("**", i + 2)
            if end > 0:
                out.append(C.BOLD + line[i+2:end] + C.RESET)
                i = end + 2
                continue
        if line[i] == "`":
            end = line.find("`", i + 1)
            if end > 0:
                out.append(C.OK + line[i+1:end] + C.RESET)
                i = end + 1
                continue
        if line[i] == "*" and i > 0 and line[i-1] not in " \t":
            end = line.find("*", i + 1)
            if end > 0:
                out.append(C.ITAL + line[i+1:end] + C.RESET)
                i = end + 1
                continue
        out.append(line[i])
        i += 1
    return "".join(out)


class StreamRenderer:
    def __init__(self):
        self.buffer = ""
        self.in_code = False
        self.code_lang = ""

    def feed(self, text):
        self.buffer += text
        out = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            out.append(self._render(line))
        return out

    def flush(self):
        if self.buffer:
            line = self._render(self.buffer)
            self.buffer = ""
            return [line]
        return []

    def _render(self, line):
        if line.startswith("```"):
            if self.in_code:
                self.in_code = False
                self.code_lang = ""
                return color("    ╰" + "─" * 40, C.MUTE)
            else:
                self.in_code = True
                self.code_lang = line[3:].strip()
                label = self.code_lang or "code"
                return color("    ╭─ ", C.MUTE) + color(label, C.DIM)
        if self.in_code:
            return color("    │ ", C.MUTE) + color(line, C.OK)
        return "  " + _apply_md(line)


# ============================================================
#  UTILIDADES
# ============================================================
def detectar_idioma(texto):
    t = texto.lower()
    if re.search(r"[ñáéíóúü¿¡]", t):
        return "es"
    es_words = {"que", "de", "la", "el", "en", "y", "es", "un", "para", "con",
                "por", "no", "se", "los", "las", "una", "pero", "como", "más"}
    en_words = {"the", "and", "is", "of", "to", "in", "a", "for", "with",
                "that", "this", "you", "have", "are", "but", "not", "from"}
    words = set(re.findall(r"\b[a-záéíóúñ]+\b", t))
    es_hits = len(words & es_words)
    en_hits = len(words & en_words)
    if es_hits > en_hits:
        return "es"
    if en_hits > es_hits:
        return "en"
    return None


def copiar_portapapeles(texto):
    if not shutil.which("termux-clipboard-set"):
        return
    try:
        subprocess.Popen(
            ["termux-clipboard-set", texto],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def notificar(sonido=True, vibracion=True, mensaje="Respuesta lista"):
    if sonido:
        try:
            sys.stdout.write("\a"); sys.stdout.flush()
        except Exception:
            pass
    if vibracion and shutil.which("termux-vibrate"):
        try:
            subprocess.Popen(
                ["termux-vibrate", "-d", "150", "-f"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass
    if shutil.which("termux-notification"):
        try:
            subprocess.Popen(
                ["termux-notification",
                 "--title", "WO-TTS",
                 "--content", mensaje,
                 "--id", "wo-tts"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass


def limpiar_procesos_colgados():
    try:
        subprocess.run(
            ["pkill", "-f", "termux-microphone-record"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except Exception:
        pass


def cargar_archivo_texto(ruta):
    p = Path(ruta).expanduser()
    if not p.exists() or not p.is_file():
        return None
    ext = p.suffix.lower()
    if ext == ".pdf":
        if not shutil.which("pdftotext"):
            return None
        try:
            r = subprocess.run(
                ["pdftotext", str(p), "-"],
                capture_output=True, text=True, timeout=30,
            )
            return r.stdout if r.returncode == 0 else None
        except Exception:
            return None
    if ext in (".txt", ".md", ".py", ".json", ".csv", ".log", ".rst", ".html"):
        try:
            return p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None
    return None


def programar_recordatorio(delay_seg, texto):
    def _run():
        try:
            time.sleep(delay_seg)
            if shutil.which("termux-notification"):
                subprocess.Popen(
                    ["termux-notification",
                     "--title", "Recordatorio",
                     "--content", texto],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            if shutil.which("termux-tts-speak"):
                subprocess.Popen(
                    ["termux-tts-speak", "Recordatorio: " + texto],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            try:
                sys.stdout.write("\a"); sys.stdout.flush()
            except Exception:
                pass
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def parsear_duracion(s):
    s = s.strip().lower()
    if not s:
        return None
    m = re.match(r"^(\d+)\s*([smhd]?)$", s)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2) or "m"
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 60)
    return val * mult


# ============================================================
#  CONFIG PERSISTENCE
# ============================================================
def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(chatbot):
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        cfg = {
            "model": chatbot.model,
            "temperature": chatbot.temperature,
            "num_ctx": chatbot.num_ctx,
            "system_prompt": chatbot.system_prompt,
            "tts_on": chatbot.tts_on,
            "compact": chatbot.compact,
            "think": chatbot.think,
            "saved_at": datetime.now().isoformat(),
        }
        CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except Exception:
        pass


# ============================================================
#  DEPENDENCIAS
# ============================================================
DEPS_REQUIRED = [
    ("ollama",                   "pkg install ollama  (o: curl ollama.com/install.sh | sh)"),
    ("termux-microphone-record", "pkg install termux-api"),
    ("termux-tts-speak",         "pkg install termux-api"),
    ("whisper",                  "pip install openai-whisper"),
]

DEPS_OPTIONAL = [
    ("termux-vibrate",       "pkg install termux-api"),
    ("termux-notification",  "pkg install termux-api"),
    ("termux-clipboard-set", "pkg install termux-api"),
    ("pdftotext",            "pkg install poppler"),
    ("less",                 "pkg install less"),
]


def check_dependencies():
    missing_req = []
    for cmd, hint in DEPS_REQUIRED:
        if not shutil.which(cmd):
            missing_req.append((cmd, hint))
    missing_opt = []
    for cmd, hint in DEPS_OPTIONAL:
        if not shutil.which(cmd):
            missing_opt.append((cmd, hint))
    return missing_req, missing_opt


# ============================================================
#  LOG DE SESION
# ============================================================
def log_turn(model, latencia_s, chars_in, chars_out, user_input, respuesta):
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().isoformat(),
            "model": model,
            "latencia_s": round(latencia_s, 2),
            "chars_in": chars_in,
            "chars_out": chars_out,
            "user": user_input[:300],
            "assistant": respuesta[:500],
        }
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ============================================================
#  TTS CON COLA + STREAMING + IDIOMA
# ============================================================
class TTSManager:
    def __init__(self):
        self.activo = False
        self.proceso = None
        self.disponible = shutil.which("termux-tts-speak") is not None
        self._queue = []
        self._q_lock = threading.Lock()
        self._worker_stop = threading.Event()
        self._worker = None

    def _ensure_worker(self):
        if self._worker and self._worker.is_alive():
            return
        self._worker_stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def _worker_loop(self):
        while not self._worker_stop.is_set():
            item = None
            with self._q_lock:
                if self._queue:
                    item = self._queue.pop(0)
            if item is None:
                time.sleep(0.05)
                continue
            texto, lang = item
            self._speak_one(texto, lang)

    def _speak_one(self, texto, lang):
        if not texto.strip() or not self.disponible:
            return
        cmd = ["termux-tts-speak", texto]
        if lang:
            cmd += ["-l", lang]
        try:
            self.proceso = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.activo = True
            self.proceso.wait()
        except Exception:
            pass
        finally:
            self.activo = False
            self.proceso = None

    def hablar(self, texto, lang=None):
        if not texto or not texto.strip() or not self.disponible:
            return
        if lang is None:
            lang = detectar_idioma(texto)
        self.encolar(texto, lang)

    def encolar(self, frase, lang=None):
        if not frase or not frase.strip() or not self.disponible:
            return
        with self._q_lock:
            self._queue.append((frase, lang))
        self._ensure_worker()

    def flush_queue(self):
        with self._q_lock:
            self._queue.clear()

    def cancelar(self):
        self.flush_queue()
        if self.proceso is None:
            self.activo = False
            return False
        try:
            if self.proceso.poll() is None:
                try:
                    self.proceso.terminate()
                    self.proceso.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.proceso.kill()
                    try:
                        self.proceso.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:
            pass
        finally:
            self.proceso = None
            self.activo = False
        return True

    def estado(self):
        if not self.disponible:
            return "NO disponible"
        if self.activo and self.proceso and self.proceso.poll() is None:
            return "hablando"
        with self._q_lock:
            if self._queue:
                return "en cola"
        if self.proceso and self.proceso.poll() is not None:
            self.activo = False
            self.proceso = None
        return "inactivo"

    def shutdown(self):
        self._worker_stop.set()
        self.flush_queue()
        self.cancelar()


# ============================================================
#  ESTADO COMPARTIDO
# ============================================================
STATE = {
    "sending": False,
    "transcribing": False,
    "recording": False,
    "rec_start": 0.0,
    "send_start": 0.0,
    "stream_chars": 0,
    "free_hands": False,
    "verbose": False,
}


# ============================================================
#  CACHE OLLAMA LIST
# ============================================================
_OLLAMA_LIST_CACHE = {"ts": 0.0, "data": []}


def ollama_list_cached(force=False):
    now = time.time()
    if not force and now - _OLLAMA_LIST_CACHE["ts"] < OLLAMA_CACHE_TTL:
        return _OLLAMA_LIST_CACHE["data"]
    try:
        r = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return _OLLAMA_LIST_CACHE["data"]
    if r.returncode != 0:
        return _OLLAMA_LIST_CACHE["data"]
    modelos = []
    for ln in r.stdout.strip().splitlines()[1:]:
        ln = ln.strip()
        if ln:
            modelos.append(ln.split()[0])
    _OLLAMA_LIST_CACHE["ts"] = now
    _OLLAMA_LIST_CACHE["data"] = modelos
    return modelos


# ============================================================
#  ASCII ART — WO-TTS
# ============================================================
WO_TTS_ART = r"""
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⣾⠀⠀⠀⣀⡀⠀⠀       ⢸⣿⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⡏⠀⠀⠀⢀⣴⣾⣿⣿⣷⣦⣀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⣤⣶⣿⣿⣿⣶⠄
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⣾⣿⣿⠀⠀⠀⠉⠛⢷⣄⠀⠀⠀⠀⠀⠀ ⠈⣿⡇⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⢰⣿⠃⠀⠀⣴⣿⠋⠀⠀⠀⠀⠙⣿⣷⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⣾⣿⠁⠀⠀⠀⠈⠀
⠀⠀⠀⠀⠀⣀⣀⣀⣀⣀⣠⣾⣿⣿⣿⣿⠀⠀⠘⢶⣤⠀⠙⢷⡀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⢰⣿⣿⣧⠀⠀⠀⣾⣿⠀⠀⢰⣿⠃⠀⠀⠀⠀⠀⠀⠘⣿⣇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⣿⣿⣀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠠⣄⠀⠙⣷⠀⠈⣷⠀⠀   ⠀⢸⣿⡀⠀⠀⣿⠇⠘⣿⡀⠀⠀⣿⡏⠀⠀⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⠀⠻⣿⣿⣶⣄⠀⠀
⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⢸⠆⠀⣿⠆⠀⣿⠀⠀⠀⠀ ⠀⠀⣿⣇⠀⣼⣿⠀⠀⢿⣷⠀⢸⣿⠀⠀⠀⢿⣿⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠿⠿⠿⠿⠇⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⢿⣿⣆
⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠐⠋⠀⣠⡿⠀⢀⡿⠀⠀   ⠀⠀⢻⣿⢀⣿⠃⠀⠀⠈⣿⡄⣿⡿⠀⠀⠀⠘⣿⣧⠀⠀⠀⠀⠀⠀⣼⣿⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿
⠀⠀⠀⠀⠀⠉⠉⠉⠉⠉⠙⢿⣿⣿⣿⣿⠀⠀⢠⠾⠛⠀⣠⡾⠁⠀⠀⠀⠀⠀ ⠀⠈⣿⣿⡟⠀⠀⠀⠀⢹⣿⣿⠁⠀⠀⠀⠀⠈⢿⣷⣤⣀⣀⣤⣾⣿⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⠀⠀⠀⠀⠀⣶⣤⣀⣀⣀⣴⣿⠟
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⢿⣿⣿⠀⠀⠀⣀⣤⡾⠋⠀⠀⠀⠀⠀⠀⠀ ⠀⠀⠛⠛⠀⠀⠀⠀⠀⠀⠛⠛⠀⠀⠀⠀⠀⠀⠀⠈⠙⠛⠛⠋⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠛⠛⠀⠀⠀⠀⠀⠀⠀⠀⠛⠛⠀⠀⠀⠀⠀⠈⠉⠛⠛⠛⠉⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⢿⠀⠀⠀⠉⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀          ⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
""".strip("\n").split("\n")


def logo():
    n = len(WO_TTS_ART)
    out = []
    for i, ln in enumerate(WO_TTS_ART):
        idx = int(round(i * (len(BANNER_GRADIENT) - 1) / max(1, n - 1)))
        col = BANNER_GRADIENT[idx]
        out.append(col + ln + C.RESET)
    return out


# ============================================================
#  BANNER
# ============================================================
def banner(model, tts_ok):
    art_width = max(vlen(ln) for ln in WO_TTS_ART) if WO_TTS_ART else 0
    cols = SCREEN.cols if SCREEN.cols else 80
    pad = max(0, (cols - art_width) // 2)
    print()
    for lg in logo():
        print(" " * pad + lg)
    print()
    tts_txt = color("TTS disponible", C.OK) if tts_ok else color("TTS no disponible", C.ERR)
    line = (
        "  " + color("WO-TTS", C.TXT + C.BOLD)
        + "  " + color("·", C.MUTE)
        + "  " + color("v" + VERSION, C.DIM)
        + "  " + color("·", C.MUTE)
        + "  " + color(model, C.ACC)
        + "  " + color("·", C.MUTE)
        + "  " + tts_txt
    )
    print(line)
    print("  " + color("Asistente de voz + Ollama  ·  Termux", C.MUTE))
    print()


def panel_ayuda():
    section_header("ATAJOS DE TECLADO")
    items = [
        ("g", "Grabar / detener grabacion"),
        ("e", "Escribir texto manualmente"),
        ("f", "Modo manos libres on/off"),
        ("t", "TTS on/off"),
        ("d", "Modo pensamiento on/off"),
        ("m", "Cambiar de modelo"),
        ("o", "Opciones"),
        ("v", "Ver historial"),
        ("c", "Modo compacto on/off"),
        ("a", "Animaciones on/off"),
        ("z", "Modo verboso on/off"),
        ("l", "Limpiar interfaz"),
        ("i", "Estado de la sesion"),
        ("s", "Guardar conversacion"),
        ("r", "Reiniciar contexto"),
        ("h", "Mostrar esta ayuda"),
        ("q", "Salir"),
    ]
    for k, desc in items:
        print("    " + color(k, C.ACC + C.BOLD) + "    " + color(desc, C.DIM))
    print()
    section_header("COMANDOS SLASH (en modo 'e')", glyph="◆", glyph_color=C.ACC)
    cmds = [
        ("/model <n>",     "cambiar modelo"),
        ("/temp <n>",      "temperatura (0.0-2.0)"),
        ("/ctx <n>",       "num_ctx"),
        ("/file <ruta>",   "cargar archivo al contexto"),
        ("/clear",         "limpiar contexto"),
        ("/save",          "guardar conversacion"),
        ("/tts",           "toggle TTS"),
        ("/recordar 10m X","recordatorio"),
        ("/obsidian",      "exportar a Obsidian"),
    ]
    for k, desc in cmds:
        print("    " + color(k.ljust(18), C.ACC + C.BOLD) + color(desc, C.DIM))
    print()


# ============================================================
#  CHAT
# ============================================================
class OllamaChat:
    def __init__(self, model, system_prompt, temperature, num_ctx, tts, tts_on=True):
        self.model = model
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.system_prompt = system_prompt
        self.messages = []
        self.turn_count = 0
        self.think = False
        self.tts_on = tts_on
        self.tts = tts
        self.compact = False
        self._reset()

    def _ok(self, txt):
        SCREEN.toast("  " + color("✓", C.OK + C.BOLD) + "  " + color(txt, C.DIM), 2.0)

    def _off(self, txt):
        SCREEN.toast("  " + color("○", C.MUTE) + "  " + color(txt, C.MUTE), 1.6)

    def _warn(self, txt):
        print("  " + color("⚠", C.WARN) + "  " + color(txt, C.WARN))

    def _err(self, txt):
        print("  " + color("✗", C.ERR) + "  " + color(txt, C.ERR))

    def _with_paused(self, func):
        """Para el listener y ejecuta func con stdin libre."""
        stop_listener()
        try:
            drain_queue()
            func()
        finally:
            start_listener()

    def _reset(self):
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.turn_count = 0

    def reset(self):
        self._reset()
        self._ok("Contexto reiniciado.")

    def set_system(self, p):
        self.system_prompt = p
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0]["content"] = p
        else:
            self.messages.insert(0, {"role": "system", "content": p})
        self._ok("Prompt de sistema actualizado.")

    def toggle_think(self):
        self.think = not self.think
        self._ok("Pensamiento ON") if self.think else self._off("Pensamiento OFF")

    def toggle_tts(self):
        self.tts_on = not self.tts_on
        if self.tts_on:
            self._ok("TTS ON")
        else:
            self.tts.cancelar()
            self._off("TTS OFF")

    def toggle_compact(self):
        self.compact = not self.compact
        self._ok("Modo compacto ON") if self.compact else self._off("Modo compacto OFF")

    # ---------- modelos ----------
    def _pick_model(self):
        modelos = ollama_list_cached()
        if not modelos:
            self._err("No hay modelos disponibles.")
            return
        actual = self.model
        section_header("MODELOS DISPONIBLES", glyph="◇", glyph_color=C.ACC)
        for i, m in enumerate(modelos, 1):
            marker = color("  ●", C.OK) if m == actual else ""
            print("    " + color(f"{i:>2}", C.ACC + C.BOLD) + "   " + color(m, C.TXT) + marker)
        print()
        try:
            choice = input("  " + color(f"Modelo [1-{len(modelos)}] (Enter cancela): ", C.DIM)).strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if not choice:
            self._off("Cambio cancelado.")
            return
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(modelos):
                nuevo = modelos[idx - 1]
            else:
                self._err(f"Numero fuera de rango: {idx}")
                return
        else:
            nuevo = choice
        if nuevo == actual:
            self._off(f"Ya estas usando '{nuevo}'.")
            return
        self.model = nuevo
        self._ok("Modelo: " + nuevo)
        save_config(self)

    def cambiar_modelo(self):
        self.tts.cancelar()
        self._with_paused(self._pick_model)

    # ---------- opciones ----------
    def _edit_temperature(self):
        print()
        print("  " + color("Temperatura actual: ", C.DIM) + color(str(self.temperature), C.TXT))
        try:
            v = input("  " + color("Nueva temperatura (0.0-2.0, Enter cancela): ", C.DIM)).strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if not v:
            self._off("Sin cambios."); return
        try:
            nuevo = float(v)
        except ValueError:
            self._err(f"Valor no numerico: {v}"); return
        if not (0.0 <= nuevo <= 2.0):
            self._warn("Fuera de rango (0.0-2.0). Se aplicara igual.")
        self.temperature = nuevo
        self._ok(f"Temperatura: {nuevo}")
        save_config(self)

    def _edit_num_ctx(self):
        print()
        print("  " + color("num_ctx actual: ", C.DIM) + color(str(self.num_ctx), C.TXT))
        try:
            v = input("  " + color("Nuevo num_ctx (Enter cancela): ", C.DIM)).strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if not v:
            self._off("Sin cambios."); return
        try:
            nuevo = int(v)
        except ValueError:
            self._err(f"Valor no entero: {v}"); return
        if nuevo < 256:
            self._warn("Valores muy bajos pueden truncar el contexto.")
        self.num_ctx = nuevo
        self._ok(f"num_ctx: {nuevo}")
        save_config(self)

    def _edit_system(self):
        print()
        print("  " + color("Prompt actual:", C.DIM))
        for ln in self.system_prompt.splitlines() or [""]:
            print("    " + color(ln, C.MUTE))
        print()
        print("  " + color("Escribe el nuevo prompt en una linea (Enter cancela).", C.DIM))
        print("  " + color("Usa \\\\n para saltos.", C.MUTE))
        try:
            v = input("  " + color("> ", C.ACC))
        except (EOFError, KeyboardInterrupt):
            print(); return
        if not v.strip():
            self._off("Sin cambios."); return
        self.set_system(v.replace("\\n", "\n"))
        save_config(self)

    def _menu_opciones_loop(self):
        while True:
            section_header("OPCIONES")
            filas = [
                ("1", "Modelo",             self.model),
                ("2", "Temperatura",        str(self.temperature)),
                ("3", "num_ctx",            str(self.num_ctx)),
                ("4", "Prompt de sistema",  "(ver / editar)"),
                ("5", "Reiniciar contexto", ""),
                ("6", "Guardar config",     ""),
                ("0", "Volver",             ""),
            ]
            for k, label, val in filas:
                line = "    " + color(k, C.ACC + C.BOLD) + "   " + color(label.ljust(20), C.DIM)
                if val:
                    line += color(val, C.TXT)
                print(line)
            print()
            try:
                opt = input("  " + color("Opcion: ", C.DIM)).strip()
            except (EOFError, KeyboardInterrupt):
                print(); return
            if opt in ("", "0", "q"):
                return
            if opt == "1":
                self._pick_model()
            elif opt == "2":
                self._edit_temperature()
            elif opt == "3":
                self._edit_num_ctx()
            elif opt == "4":
                self._edit_system()
            elif opt == "5":
                self.reset()
            elif opt == "6":
                save_config(self)
                self._ok("Config guardada.")
            else:
                self._warn(f"Opcion desconocida: {opt}")

    def menu_opciones(self):
        self.tts.cancelar()
        self._with_paused(self._menu_opciones_loop)

    # ---------- historial ----------
    def _dump_historial(self):
        no_system = [m for m in self.messages if m["role"] != "system"]
        if not no_system:
            self._off("No hay mensajes todavia."); return
        lines = [""]
        lines.append(
            "  " + color("◈", C.ACC2 + C.BOLD)
            + " " + color("HISTORIAL", C.TXT + C.BOLD)
            + color(f"   ·   {len(no_system)} mensajes   ·   {self.turn_count} turnos", C.MUTE)
        )
        lines.append("  " + color("─" * (SCREEN.cols - 4), C.FAINT))
        lines.append("")
        for m in no_system:
            if m["role"] == "user":
                lines.append("  " + color("> ", C.ACC + C.BOLD) + color(m["content"], C.TXT))
            else:
                for ln in (m["content"].splitlines() or [""]):
                    lines.append("  " + _apply_md(ln))
            lines.append("")
        lines.append("  " + color("fin del historial   ·   pulsa 'q' para volver", C.MUTE))
        lines.append("")
        try:
            TMP_HIST.write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            self._err(f"No se pudo escribir el historial: {e}"); return
        menos = shutil.which("less") or shutil.which("more")
        if not menos:
            for ln in lines:
                print(ln)
            return
        try:
            subprocess.run([menos, "-R", "-X", "-F", str(TMP_HIST)])
        except Exception as e:
            self._err(f"Error abriendo paginador: {e}")

    def ver_historial(self):
        self._with_paused(self._dump_historial)

    # ---------- persistencia ----------
    def save(self, path=None):
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        if path is None:
            path = HISTORY_DIR / f"chat_{datetime.now():%Y%m%d_%H%M%S}.json"
        else:
            path = Path(path)
        data = {
            "model": self.model, "system_prompt": self.system_prompt,
            "temperature": self.temperature, "num_ctx": self.num_ctx,
            "think": self.think, "tts_on": self.tts_on,
            "compact": self.compact,
            "messages": self.messages, "saved_at": datetime.now().isoformat(),
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._ok("Guardado en " + str(path))

    def load(self, path):
        path = Path(path)
        if not path.exists():
            self._err(f"No existe: {path}"); return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            self._err(f"JSON invalido: {e}"); return
        self.model = data.get("model", self.model)
        self.system_prompt = data.get("system_prompt", self.system_prompt)
        self.temperature = data.get("temperature", self.temperature)
        self.num_ctx = data.get("num_ctx", self.num_ctx)
        self.think = data.get("think", self.think)
        self.tts_on = data.get("tts_on", self.tts_on)
        self.compact = data.get("compact", self.compact)
        self.messages = data.get("messages", [])
        self.turn_count = sum(1 for m in self.messages if m["role"] == "user")
        self._ok(f"Cargado ({self.turn_count} turnos)")

    def _tokens(self):
        return sum(len(m["content"]) for m in self.messages) // 4

    def stats(self):
        section_header("ESTADO DE LA SESION")
        rows = [
            ("Modelo",       self.model),
            ("Temperatura",  str(self.temperature)),
            ("num_ctx",      str(self.num_ctx)),
            ("Pensamiento",  "ON" if self.think else "OFF"),
            ("TTS",          f"{'ON' if self.tts_on else 'OFF'}  ({self.tts.estado()})"),
            ("Compacto",     "ON" if self.compact else "OFF"),
            ("Animaciones",  "ON" if ANIMATIONS else "OFF"),
            ("Manos libres", "ON" if STATE["free_hands"] else "OFF"),
            ("Verboso",      "ON" if STATE["verbose"] else "OFF"),
            ("Turnos",       str(self.turn_count)),
            ("Mensajes",     str(len(self.messages))),
            ("Tokens~",      str(self._tokens())),
        ]
        for k, v in rows:
            print("    " + color("·", C.FAINT) + " " + color(k.ljust(14), C.DIM) + color(v, C.TXT))
        print()

    def exportar_obsidian(self):
        obsidian_dir = Path.home() / "storage" / "shared" / "Obsidian" / "WO-TTS"
        try:
            obsidian_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            f = obsidian_dir / f"chat_{ts}.md"
            lines = [f"# WO-TTS — {datetime.now():%Y-%m-%d %H:%M}", "",
                     f"- **Modelo**: {self.model}",
                     f"- **Temp**: {self.temperature}",
                     f"- **Turnos**: {self.turn_count}", ""]
            for m in self.messages:
                if m["role"] == "system":
                    continue
                role = "**Tú**" if m["role"] == "user" else "**Asistente**"
                lines.append(f"### {role}")
                lines.append("")
                lines.append(m["content"])
                lines.append("")
            f.write_text("\n".join(lines), encoding="utf-8")
            self._ok(f"Exportado a Obsidian/{f.name}")
        except Exception as e:
            self._err(f"Obsidian: {e}")

    def _auto_resumir_si_necesario(self):
        if len(self.messages) < 6:
            return
        tokens_aprox = self._tokens()
        limite = int(self.num_ctx * AUTOSUMARIZE_RATIO)
        if tokens_aprox < limite:
            return
        SCREEN.toast("  " + color("⋯", C.ACC2) + "  " + color("Resumiendo contexto...", C.DIM), 3.0)
        try:
            no_system = [m for m in self.messages if m["role"] != "system"]
            resumen_prompt = (
                "Resume la siguiente conversacion en un parrafo conciso, "
                "conservando la informacion esencial, decisiones y datos concretos.\n\n"
                + "\n".join(f"{m['role']}: {m['content'][:600]}" for m in no_system[-20:])
            )
            r = chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": "Eres un asistente que resume conversaciones."},
                    {"role": "user", "content": resumen_prompt},
                ],
                stream=False,
                options={"temperature": 0.3, "num_ctx": self.num_ctx},
            )
            resumen = ""
            if isinstance(r, dict):
                resumen = r.get("message", {}).get("content", "")
            if resumen:
                self.messages = [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "system", "content": "Resumen de conversacion previa: " + resumen},
                ]
                self.turn_count = 0
                self._ok("Contexto resumido.")
        except Exception as e:
            self._warn(f"Auto-resumen fallo: {e}")

    # ---------- render ----------
    def print_user(self, texto, ts=None):
        print()
        lines = texto.splitlines() or [""]
        try:
            cols = shutil.get_terminal_size().columns
        except Exception:
            cols = SCREEN.cols or 80

        bar = color("▎", C.ACC)
        for idx, ln in enumerate(lines):
            if idx == 0:
                prefix = "  " + bar + " " + color(">", C.ACC + C.BOLD) + " "
                prefix_vis = 6
            else:
                prefix = "  " + bar + "   "
                prefix_vis = 5

            if idx == 0 and ts:
                room = cols - prefix_vis - len(ln) - len(ts) - 2
                if room >= 1:
                    print(prefix + color(ln, C.TXT) + " " * room + color(ts, C.FAINT))
                    continue
            print(prefix + color(ln, C.TXT))

    def _print_assistant_end(self, segundos, chars):
        print()
        meta = f"{segundos:.1f}s"
        if chars:
            meta += f"  ·  {chars} ch"
        print("  " + color("╰─ ", C.MUTE) + color(meta, C.FAINT))

    def _print_verbose_pre(self, user_input):
        if not STATE["verbose"]:
            return
        print()
        print("  " + color("· verbose ·", C.MUTE))
        print("    " + color("model  ", C.DIM) + color(self.model, C.OK))
        print("    " + color("temp   ", C.DIM) + color(str(self.temperature), C.OK))
        print("    " + color("ctx    ", C.DIM) + color(str(self.num_ctx), C.OK))
        print("    " + color("tokens ", C.DIM) + color("~" + str(self._tokens()), C.OK))
        print("    " + color("msgs   ", C.DIM) + color(str(len(self.messages)), C.OK))

    # ---------- envio ----------
    def send(self, user_input):
        self._auto_resumir_si_necesario()
        self.messages.append({"role": "user", "content": user_input})
        self._print_verbose_pre(user_input)
        print()

        respuesta = ""
        inicio = time.time()
        STATE["sending"] = True
        STATE["send_start"] = inicio
        STATE["stream_chars"] = 0
        got_first = False
        renderer = StreamRenderer()
        tts_buf = ""

        try:
            stream = chat(
                model=self.model,
                messages=self.messages,
                stream=True,
                think=self.think,
                options={"temperature": self.temperature, "num_ctx": self.num_ctx},
            )
            for chunk in stream:
                if not got_first:
                    STATE["sending"] = False
                    got_first = True
                msg = chunk.get("message", {})
                if msg.get("thinking"):
                    th = msg["thinking"]
                    for ln in th.splitlines() or [""]:
                        print("  " + color("⋯ ", C.ACC2 + C.MUTE) + color(ln, C.ACC2 + C.ITAL))
                content = msg.get("content", "")
                if content:
                    respuesta += content
                    STATE["stream_chars"] = len(respuesta)
                    for rendered_line in renderer.feed(content):
                        print(rendered_line)
                    sys.stdout.flush()
                    if self.tts_on:
                        tts_buf += content
                        while True:
                            m = re.search(r"[.!?¡¿][\s\n]|\n{2,}", tts_buf)
                            if not m:
                                break
                            end = m.end()
                            frase = tts_buf[:end].strip()
                            tts_buf = tts_buf[end:]
                            if len(frase) >= 4:
                                self.tts.encolar(frase, detectar_idioma(frase))

            for rendered_line in renderer.flush():
                print(rendered_line)

            if self.tts_on and tts_buf.strip():
                self.tts.encolar(tts_buf.strip(), detectar_idioma(tts_buf))

            STATE["sending"] = False
            duracion = time.time() - inicio
            self._print_assistant_end(duracion, len(respuesta))
            self.messages.append({"role": "assistant", "content": respuesta})
            self.turn_count += 1

            log_turn(self.model, duracion, len(user_input), len(respuesta), user_input, respuesta)
            copiar_portapapeles(respuesta)
            notificar(mensaje=f"Respuesta lista ({len(respuesta)} ch)")

        except ResponseError as e:
            STATE["sending"] = False
            err = str(e).lower()
            if "think" in err or "thinking" in err or "does not support" in err:
                self._warn("El modelo no soporta modo pensamiento. Desactivado.")
                self.think = False
            else:
                self._err(f"Modelo: {e.error}")
            self.messages.pop()
            print()
        except RequestError as e:
            STATE["sending"] = False
            self._err(f"Conexion: {e}")
            print("    " + color("·  Esta Ollama corriendo? Prueba: ollama serve", C.MUTE))
            self.messages.pop()
            print()
        except KeyboardInterrupt:
            STATE["sending"] = False
            print()
            print("  " + color("■ Generacion interrumpida.", C.WARN))
            if respuesta:
                self.messages.append({"role": "assistant", "content": respuesta})
            else:
                self.messages.pop()
            print()
        except Exception as e:
            STATE["sending"] = False
            self._err(f"Inesperado: {e}")
            self.messages.pop()
            print()

    # ---------- slash commands ----------
    def procesar_slash(self, texto):
        if not texto.startswith("/"):
            return False
        parts = texto.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/help":
            panel_ayuda()
        elif cmd == "/model":
            if not arg:
                self._err("Uso: /model <nombre>"); return True
            self.model = arg
            self._ok("Modelo: " + arg)
            save_config(self)
        elif cmd == "/temp":
            try:
                self.temperature = float(arg)
                self._ok(f"Temperatura: {arg}")
                save_config(self)
            except ValueError:
                self._err("Uso: /temp 0.7")
        elif cmd == "/ctx":
            try:
                self.num_ctx = int(arg)
                self._ok(f"num_ctx: {arg}")
                save_config(self)
            except ValueError:
                self._err("Uso: /ctx 4096")
        elif cmd == "/clear":
            self.reset()
        elif cmd == "/save":
            self.save()
        elif cmd == "/tts":
            self.toggle_tts()
        elif cmd == "/file":
            if not arg:
                self._err("Uso: /file ruta/al/archivo.pdf"); return True
            contenido = cargar_archivo_texto(arg)
            if not contenido:
                self._err(f"No se pudo leer: {arg}"); return True
            if len(contenido) > 20000:
                contenido = contenido[:20000] + "\n[...truncado]"
            self.messages.append({
                "role": "system",
                "content": f"Contenido del archivo '{arg}':\n\n{contenido}",
            })
            self._ok(f"Archivo cargado ({len(contenido)} ch)")
        elif cmd == "/recordar":
            if not arg:
                self._err("Uso: /recordar 10m sacar la pizza"); return True
            partes = arg.split(maxsplit=1)
            if len(partes) < 2:
                self._err("Uso: /recordar 10m sacar la pizza"); return True
            seg = parsear_duracion(partes[0])
            if not seg:
                self._err("Formato de tiempo invalido (10s, 5m, 2h, 1d)"); return True
            programar_recordatorio(seg, partes[1])
            self._ok(f"Recordatorio en {partes[0]}: {partes[1]}")
        elif cmd == "/obsidian":
            self.exportar_obsidian()
        else:
            self._err(f"Comando desconocido: {cmd}")
        return True


# ============================================================
#  GRABADORA
# ============================================================
class Grabador:
    def __init__(self):
        self.grabando = False
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        try:
            AUDIO_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def iniciar(self):
        limpiar_procesos_colgados()
        os.system(f"rm -f {AUDIO_FILE} {TRANSCRIPT_FILE} 2>/dev/null")
        os.system(f"termux-microphone-record -f {AUDIO_FILE} > /dev/null 2>&1")
        self.grabando = True
        STATE["recording"] = True
        STATE["rec_start"] = time.time()

    def detener_y_transcribir(self):
        self.grabando = False
        STATE["recording"] = False
        try:
            os.system("termux-microphone-record -q > /dev/null 2>&1")
        except Exception:
            pass

        backup_path = None
        try:
            if AUDIO_FILE.exists():
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = AUDIO_BACKUP_DIR / f"rec_{ts}.m4a"
                shutil.copy2(AUDIO_FILE, backup_path)
        except Exception:
            backup_path = None

        STATE["transcribing"] = True
        try:
            time.sleep(0.5)
            os.system(
                f"echo '4' | whisper '{AUDIO_FILE}' "
                f"--model small --no-subs > /dev/null 2>&1"
            )
        except KeyboardInterrupt:
            SCREEN.toast("  " + color("■", C.WARN) + "  "
                         + color("Transcripcion cancelada", C.WARN), 2.5)
            return ""
        finally:
            STATE["transcribing"] = False

        if not TRANSCRIPT_FILE.exists():
            msg = "No se genero transcripcion."
            if backup_path:
                msg += f"  (audio en {backup_path.name})"
            SCREEN.toast("  " + color("✗", C.ERR) + "  " + color(msg, C.ERR), 3.5)
            return ""
        try:
            return TRANSCRIPT_FILE.read_text(encoding="utf-8").strip()
        except Exception as e:
            SCREEN.toast("  " + color("✗", C.ERR) + "  "
                         + color(f"Error: {e}", C.ERR), 3.0)
            return ""


# ============================================================
#  BARRA INFERIOR
# ============================================================
def make_status_provider(chatbot, grabador):
    DOTS = ["●", "◉", "◎", "○", "◎", "◉"]
    WAVES = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    EDOTS = [".  ", ".. ", "...", " ..", "  .", "   "]
    counter = {"i": 0}

    def provider():
        i = counter["i"]
        counter["i"] += 1
        cols = SCREEN.cols

        l1 = color("  ◈ ", C.ACC2) + color("─" * (cols - 4), C.FAINT)

        hint = "? atajos"
        hint_visible = len(hint)

        if STATE["recording"]:
            elapsed = time.time() - STATE["rec_start"]
            mm, ss = divmod(int(elapsed), 60)
            dot = DOTS[i % len(DOTS)]
            wv = "".join(WAVES[(i + k) % len(WAVES)] for k in range(8))
            fh = color("  FH", C.OK) if STATE["free_hands"] else ""
            prompt = (
                color("> ", C.DIM)
                + color(dot, C.ERR + C.BOLD) + " "
                + color(f"REC {mm:02d}:{ss:02d}", C.ERR + C.BOLD) + "  "
                + color(wv, C.ERR) + fh
            )
        elif STATE["transcribing"]:
            fr = SPIN[i % len(SPIN)]
            prompt = (
                color("> ", C.DIM)
                + color(fr, C.WARN) + " "
                + color("Transcribiendo con Whisper...", C.WARN)
            )
        elif STATE["sending"]:
            fr = SPIN[i % len(SPIN)]
            chars = STATE.get("stream_chars", 0)
            if chars == 0:
                prompt = (
                    color("> ", C.DIM)
                    + color(fr, C.ACC2) + " "
                    + color("Pensando" + EDOTS[i % len(EDOTS)], C.ACC2 + C.ITAL)
                )
            else:
                elapsed = time.time() - STATE["send_start"]
                prompt = (
                    color("> ", C.DIM)
                    + color(fr, C.ACC2) + " "
                    + color(f"↓ {chars} ch", C.ACC2)
                    + color(f"   {elapsed:.1f}s", C.MUTE)
                )
        elif chatbot.tts.estado() == "hablando":
            prompt = (
                color("> ", C.DIM)
                + color("♪", C.OK) + " "
                + color("Leyendo respuesta", C.OK)
                + "  " + color("· 'g' para cancelar", C.MUTE)
            )
        else:
            extra = ""
            if STATE["free_hands"]:
                extra = color("  · FH", C.OK)
            elif STATE["verbose"]:
                extra = color("  · verbose", C.ACC2)
            prompt = (
                color("> ", C.DIM)
                + color("▸", C.FAINT) + " "
                + color("Pulsa ", C.MUTE)
                + color("g", C.TXT + C.BOLD)
                + color(" para grabar", C.MUTE)
                + extra
            )

        pad = cols - vlen(prompt) - hint_visible - 2
        if pad < 1:
            pad = 1
        l2 = prompt + " " * pad + color(hint, C.MUTE)

        col1_w = int(cols * 0.42)
        col2_w = int(cols * 0.20)
        col3_w = max(10, cols - col1_w - col2_w - 4)

        ws = str(WORKSPACE).replace(str(Path.home()), "~")
        glyph = "\033[38;5;213m◈\033[0m "
        ws_avail = col1_w - 3
        sep = color("│", C.FAINT)

        c1 = glyph + color(fit_plain(ws, ws_avail), C.DIM)

        voz_txt = "voz:on" if chatbot.tts_on else "voz:off"
        voz_col = C.OK if chatbot.tts_on else C.MUTE
        c2 = color(fit_plain(voz_txt, col2_w - 2) + " ", voz_col)

        c3 = color(fit_plain(chatbot.model, col3_w - 1), C.ACC)

        l3 = " " + c1 + " " + sep + " " + c2 + " " + sep + " " + c3

        return [l1, l2, l3]

    return provider


# ============================================================
#  PREHEAT
# ============================================================
def precalentar(chatbot):
    def _warm():
        try:
            chat(
                model=chatbot.model,
                messages=[{"role": "user", "content": "."}],
                stream=False,
                options={"num_predict": 1, "temperature": 0},
            )
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()


# ============================================================
#  PROMPT MANUAL (con listener parado -> se ve lo que escribes)
# ============================================================
def prompt_manual():
    stop_listener()
    try:
        drain_queue()
        # Nos aseguramos de dejar la terminal en modo "normal"
        try:
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()
        except Exception:
            pass
        print()
        try:
            txt = input("  " + color("> ", C.ACC))
        except (EOFError, KeyboardInterrupt):
            print()
            return ""
        result = txt.strip()
        if result and readline is not None:
            try:
                readline.add_history(result)
            except Exception:
                pass
        return result
    finally:
        start_listener()


# ============================================================
#  MAIN
# ============================================================
def main():
    global ANIMATIONS, _RAW_STDOUT

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", "-m", default=None)
    parser.add_argument("--system", "-s", default=None)
    parser.add_argument("--temp", "-t", type=float, default=None)
    parser.add_argument("--ctx", "-c", type=int, default=None)
    parser.add_argument("--load", "-l", default=None)
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--no-tts", action="store_true")
    parser.add_argument("--no-anim", action="store_true")
    parser.add_argument("--no-config", action="store_true")
    args = parser.parse_args()

    if args.no_anim:
        ANIMATIONS = False

    miss_req, miss_opt = check_dependencies()
    if miss_req:
        print()
        print("  " + color("✗ Faltan dependencias obligatorias:", C.ERR + C.BOLD))
        for cmd, hint in miss_req:
            print("    " + color("·", C.ERR) + " " + color(cmd, C.TXT)
                  + color("   →  ", C.MUTE) + color(hint, C.DIM))
        print()
        if input("  ¿Continuar de todos modos? (s/N): ").strip().lower() != "s":
            sys.exit(1)

    _RAW_STDOUT = sys.stdout
    sys.stdout = LockedStream(_RAW_STDOUT)

    SCREEN.start()

    cfg = {} if args.no_config else load_config()
    model = args.model or cfg.get("model", DEFAULT_MODEL)
    system = args.system or cfg.get("system_prompt", DEFAULT_SYSTEM)
    temp = args.temp if args.temp is not None else cfg.get("temperature", DEFAULT_TEMP)
    ctx = args.ctx if args.ctx is not None else cfg.get("num_ctx", DEFAULT_CTX)

    tts = TTSManager()
    banner(model=model, tts_ok=tts.disponible)

    if miss_opt:
        print("  " + color("·", C.MUTE) + " "
              + color("Opcionales no disponibles: ", C.MUTE)
              + color(", ".join(c for c, _ in miss_opt), C.FAINT))
        print()

    chatbot = OllamaChat(
        model, system, temp, ctx, tts=tts,
        tts_on=(tts.disponible and not args.no_tts and cfg.get("tts_on", True)),
    )
    chatbot.compact = args.compact or cfg.get("compact", False)
    chatbot.think = cfg.get("think", False)
    if args.load:
        chatbot.load(args.load)
    grabador = Grabador()

    SCREEN.set_provider(make_status_provider(chatbot, grabador))
    precalentar(chatbot)

    def _on_resize(signum, frame):
        SCREEN.resize()
    try:
        signal.signal(signal.SIGWINCH, _on_resize)
    except Exception:
        pass

    # Arrancamos el listener en su hilo
    start_listener()

    def _procesar_texto(texto):
        if chatbot.procesar_slash(texto):
            return
        ts = datetime.now().strftime("%H:%M:%S")
        chatbot.print_user(texto, ts)
        chatbot.send(texto)

    def handle_key(tecla):
        global ANIMATIONS

        if grabador.grabando and tecla not in ('g', 'f'):
            return

        if tecla == 'g':
            if not grabador.grabando:
                tts.cancelar()
                grabador.iniciar()
            else:
                texto = grabador.detener_y_transcribir()
                if texto:
                    _procesar_texto(texto)
                    if STATE["free_hands"] and not grabador.grabando:
                        tts.cancelar()
                        time.sleep(0.3)
                        grabador.iniciar()
        elif tecla == 'e':
            texto = prompt_manual()
            if texto:
                _procesar_texto(texto)
        elif tecla == 'f':
            STATE["free_hands"] = not STATE["free_hands"]
            if STATE["free_hands"]:
                chatbot._ok("Manos libres ON")
                if not grabador.grabando:
                    tts.cancelar()
                    grabador.iniciar()
            else:
                chatbot._off("Manos libres OFF")
                if grabador.grabando:
                    grabador.grabando = False
                    STATE["recording"] = False
                    os.system("termux-microphone-record -q > /dev/null 2>&1")
        elif tecla == 't':
            chatbot.toggle_tts(); save_config(chatbot)
        elif tecla == 'd':
            chatbot.toggle_think(); save_config(chatbot)
        elif tecla == 'm':
            chatbot.cambiar_modelo()
        elif tecla == 'o':
            chatbot.menu_opciones()
        elif tecla == 'v':
            chatbot.ver_historial()
        elif tecla == 'c':
            chatbot.toggle_compact(); save_config(chatbot)
        elif tecla == 'a':
            ANIMATIONS = not ANIMATIONS
            (chatbot._ok if ANIMATIONS else chatbot._off)(
                "Animaciones " + ("ON" if ANIMATIONS else "OFF"))
        elif tecla == 'z':
            STATE["verbose"] = not STATE["verbose"]
            (chatbot._ok if STATE["verbose"] else chatbot._off)(
                "Verboso " + ("ON" if STATE["verbose"] else "OFF"))
        elif tecla == 'l':
            SCREEN.clear()
            banner(chatbot.model, tts.disponible)
            SCREEN.toast("  " + color("✓", C.OK + C.BOLD) + "  "
                         + color("Interfaz limpia", C.DIM), 1.5)
        elif tecla == 'i':
            chatbot.stats()
        elif tecla == 'h':
            panel_ayuda()
        elif tecla == 's':
            chatbot.save()
        elif tecla == 'r':
            chatbot.reset()

    # ----- Loop principal -----
    try:
        while True:
            try:
                tecla = TASK_QUEUE.get(timeout=0.2)
            except queue.Empty:
                continue
            if tecla == 'q':
                break
            try:
                handle_key(tecla)
            except KeyboardInterrupt:
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop_listener()
        try:
            tts.shutdown()
        except Exception:
            pass
        SCREEN.stop()
        print()
        hline()
        save_config(chatbot)
        try:
            if chatbot.turn_count > 0:
                g = input("  " + color("¿Guardar conversacion completa? (s/N): ", C.DIM)).strip().lower()
                if g == "s":
                    chatbot.save()
        except (EOFError, KeyboardInterrupt):
            pass
        print()
        print("  " + color("■", C.MUTE) + "  " + color("Adios.", C.DIM))
        hline()
        print()
        sys.stdout = _RAW_STDOUT


if __name__ == "__main__":
    main()

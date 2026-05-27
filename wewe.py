#!/usr/bin/env python3
"""
WEW Ads + Claim merged runner.

Flow:
1. Checkin jika belum.
2. Spin jika masih ada.
3. Watch ads sampai task reward/interstitial selesai.
4. Claim task yang sudah bisa diklaim.

File yang dipakai:
- akun.txt  : user_id|native_user_id|native_user_pwd|initial_reward_token|nama_opsional
- proxy.txt : satu proxy per baris, opsional
"""

from __future__ import annotations

import hashlib
import ctypes
import json
import os
import random
import re
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

from github_sync import sync_file_to_github


SCRIPT_DIR = Path(__file__).resolve().parent
ACCOUNT_FILE = "akun.txt"
PROXY_FILE = "proxy.txt"

LANG = "en"
GAME_ID = "06MergeBigWatermelon"

DEBUG_HTTP = False

REQUEST_TIMEOUT = 5
REQUEST_RETRIES = 3
REQUEST_RETRY_DELAY = 1
SLEEP_BETWEEN_ADS = 2
SLEEP_BETWEEN_ROUNDS = 2
ADS_PER_ROUND = 1
MAX_ROUNDS = 1000
SKIP_GAMEPLAY_SIMULATION = True
MAX_WORKERS = 73
SLEEP_LOGIN_RETRY = 1
DELAY_CLAIM = 2
MAX_LOGIN_RETRY = 3
MAX_PROXY_SWAP_PER_AD = 2        # berapa kali ganti proxy per-ad saat cooldown
ALLOW_DIRECT_REWARD_FALLBACK = False

# Cooldown per task type, hasil pengamatan via test_cooldown.py.
# token: jeda minimum antar attempt pada akun yang sama (server batasi token).
# proxy: jeda minimum sebelum proxy yang baru cooldown bisa dipakai lagi.
TOKEN_COOLDOWN = {
    "reward": 0,           # reward ad: tidak ada cooldown akun-level signifikan
    "interstitial": 35,    # interstitial: ~30s cooldown akun + buffer 5s
}
PROXY_COOLDOWN = {
    "reward": 45,          # IP cooldown reward ~36s + buffer
    "interstitial": 130,   # IP cooldown interstitial ~100-140s + buffer
}
PROXY_COOLDOWN_DEFAULT = 90      # fallback kalau task type tidak dikenal
STAGGER_PER_ACCOUNT_SECONDS = 1.5  # delay start tiap akun supaya tidak rebutan proxy bersamaan
LOG_MODE = "quiet"         # quiet | railway | summary | verbose | off/silent
SUMMARY_INTERVAL = 300     # interval log progress dalam detik
RUN_MAX_SECONDS = 3600     # restart ulang proses setelah 1 jam
ACCOUNT_SYNC_INTERVAL_SECONDS = 0     # 0 = sync akun.txt hanya saat restart 1 jam, -1 = matikan sync

LOG_MODE = os.environ.get("LOG_MODE", LOG_MODE).strip().lower()
DEBUG_HTTP = os.environ.get("DEBUG_HTTP", str(DEBUG_HTTP)).lower() in ("1", "true", "yes", "y")
SUMMARY_INTERVAL = int(os.environ.get("SUMMARY_INTERVAL", str(SUMMARY_INTERVAL)))
RUN_MAX_SECONDS = int(os.environ.get("RUN_MAX_SECONDS", str(RUN_MAX_SECONDS)))
ACCOUNT_SYNC_INTERVAL_SECONDS = int(os.environ.get("ACCOUNT_SYNC_INTERVAL_SECONDS", str(ACCOUNT_SYNC_INTERVAL_SECONDS)))
LOG_ENABLED = LOG_MODE not in ("0", "false", "no", "off", "silent")

REWARD_APP_ID = "hi5reward"
REWARD_SIGN_SALT = "rwdCgrdxrCrgBgeikLpboaki4R2zz9bbb"
REWARD_API_URL = "https://www.hi5games.top/reward/api"

TASK_CONFIG = {
    "reward": {"task_id": 29, "event_type": "ad_reward", "ad_type": "Reward"},
    "interstitial": {"task_id": 30, "event_type": "ad_interstitial", "ad_type": "Interstitial"},
}


class Warna:
    HIJAU = "\033[92m"
    MERAH = "\033[91m"
    KUNING = "\033[93m"
    BIRU = "\033[96m"
    UNGU = "\033[95m"
    ABU = "\033[90m"
    TEBAL = "\033[1m"
    RESET = "\033[0m"


if os.environ.get("NO_COLOR") or os.environ.get("RAILWAY_ENVIRONMENT") or not sys.stdout.isatty():
    Warna.HIJAU = ""
    Warna.MERAH = ""
    Warna.KUNING = ""
    Warna.BIRU = ""
    Warna.UNGU = ""
    Warna.ABU = ""
    Warna.TEBAL = ""
    Warna.RESET = ""


_print_lock = threading.Lock()
_console_ctrl_handler_ref = None
ACCOUNT_LOG_WIDTH = 33
_account_sync_lock = threading.Lock()
_account_sync_dirty = False
_account_sync_last = 0.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def visible_len(text: str) -> int:
    return len(strip_ansi(text))


def pad_visible(text: str, width: int) -> str:
    pad = width - visible_len(text)
    if pad <= 0:
        return text
    return text + " " * pad


def stop_sekarang(signum=None, frame=None) -> None:
    try:
        print("\nDihentikan paksa oleh Ctrl+C.", flush=True)
    finally:
        os._exit(130)


def pasang_ctrl_c_handler() -> None:
    global _console_ctrl_handler_ref

    signal.signal(signal.SIGINT, stop_sekarang)
    signal.signal(signal.SIGTERM, stop_sekarang)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop_sekarang)

    if os.name != "nt":
        return

    handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

    @handler_type
    def console_ctrl_handler(ctrl_type):
        # CTRL_C_EVENT=0, CTRL_BREAK_EVENT=1, CTRL_CLOSE_EVENT=2,
        # CTRL_LOGOFF_EVENT=5, CTRL_SHUTDOWN_EVENT=6.
        if ctrl_type in (0, 1, 2, 5, 6):
            stop_sekarang()
            return True
        return False

    _console_ctrl_handler_ref = console_ctrl_handler
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_ctrl_handler_ref, True)


pasang_ctrl_c_handler()


def mark_account_sync_dirty() -> None:
    global _account_sync_dirty
    with _account_sync_lock:
        _account_sync_dirty = True


def maybe_sync_account_file(force: bool = False) -> None:
    global _account_sync_dirty, _account_sync_last
    if ACCOUNT_SYNC_INTERVAL_SECONDS < 0:
        return
    with _account_sync_lock:
        if not _account_sync_dirty:
            return
        now = time.time()
        if not force and ACCOUNT_SYNC_INTERVAL_SECONDS == 0:
            return
        if not force and ACCOUNT_SYNC_INTERVAL_SECONDS > 0 and now - _account_sync_last < ACCOUNT_SYNC_INTERVAL_SECONDS:
            return
        _account_sync_dirty = False
        _account_sync_last = now

    filepath = path_dari_folder_skrip(ACCOUNT_FILE)
    ok = sync_file_to_github(filepath, ACCOUNT_FILE, message=f"Update {ACCOUNT_FILE} token batch from Railway")
    if not ok:
        with _account_sync_lock:
            _account_sync_dirty = True


def restart_process() -> None:
    with _print_lock:
        print(f"[{time.strftime('%H:%M:%S')}][INF] Restart 1 jam tercapai, run ulang proses...", flush=True)
    maybe_sync_account_file(force=True)
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as exc:
        with _print_lock:
            print(f"[{time.strftime('%H:%M:%S')}][ERR] Restart gagal: {exc}", flush=True)
        os._exit(75)


def start_restart_watchdog() -> None:
    if RUN_MAX_SECONDS <= 0:
        return

    def watchdog() -> None:
        time.sleep(RUN_MAX_SECONDS)
        restart_process()

    thread = threading.Thread(target=watchdog, name="restart-watchdog", daemon=True)
    thread.start()


def tprint(*args, **kwargs):
    if not LOG_ENABLED:
        return
    with _print_lock:
        print(*args, **kwargs)


def vprint(*args, **kwargs):
    if LOG_ENABLED and LOG_MODE == "verbose":
        tprint(*args, **kwargs)


def log(level: str, msg: str) -> None:
    if not LOG_ENABLED:
        return
    ts = time.strftime("%H:%M:%S")
    with _print_lock:
        print(f"[{ts}][{level}] {msg}", flush=True)


def log_info(msg: str) -> None:
    log("INF", msg)


def log_ok(msg: str) -> None:
    log("OK ", msg)


def log_warn(msg: str) -> None:
    log("WRN", msg)


def log_err(msg: str) -> None:
    log("ERR", msg)


def log_step(level: str, phase: str, account: str, detail: str = "") -> None:
    if not LOG_ENABLED:
        return
    if LOG_MODE in ("quiet", "railway"):
        important_warn = level == "WARN" and ("account disabled" in detail.lower() or "failed" in detail.lower())
        if level not in ("ERR", "ERROR") and not (level == "INFO" and phase == "CONFIG") and not important_warn:
            return
    ts = time.strftime("%H:%M:%S")
    detail_text = detail if detail else "-"
    level_color = {
        "OK": Warna.HIJAU,
        "DONE": Warna.HIJAU,
        "WARN": Warna.KUNING,
        "ERR": Warna.MERAH,
        "ERROR": Warna.MERAH,
        "START": Warna.UNGU,
        "INFO": Warna.BIRU,
    }.get(level, Warna.RESET)
    phase_color = {
        "ACCOUNT": Warna.UNGU,
        "CONFIG": Warna.BIRU,
        "REWARD": Warna.BIRU,
        "CHECKIN": Warna.HIJAU,
        "SPIN": Warna.UNGU,
        "WATCH": Warna.KUNING,
        "CLAIM": Warna.HIJAU,
        "PROGRESS": Warna.BIRU,
    }.get(phase, Warna.RESET)
    ts_text = f"{Warna.ABU}[{ts}]{Warna.RESET}"
    level_text = f"{level_color}{level:<5}{Warna.RESET}"
    phase_text = f"{phase_color}{phase:<9}{Warna.RESET}"
    account_padded = pad_visible(account, ACCOUNT_LOG_WIDTH)
    account_text = f"{Warna.TEBAL}{account_padded}{Warna.RESET}"
    with _print_lock:
        if account:
            if detail:
                print(f"{ts_text} {level_text} {phase_text} {account_text} | {detail_text}", flush=True)
            else:
                print(f"{ts_text} {level_text} {phase_text} {account_text}", flush=True)
        else:
            print(f"{ts_text} {level_text} {phase_text} {detail_text}", flush=True)


def dprint(msg: str) -> None:
    if DEBUG_HTTP:
        tprint(f"[DBG] {msg}")


def path_dari_folder_skrip(filename: str) -> Path:
    p = Path(filename)
    return p if p.is_absolute() else SCRIPT_DIR / p


def jam_sekarang() -> str:
    return time.strftime("%H:%M:%S")


def garis(judul: str) -> None:
    tprint(f"\n{Warna.UNGU}{'=' * 50}{Warna.RESET}")
    tprint(f"{Warna.UNGU}{judul}{Warna.RESET}")
    tprint(f"{Warna.UNGU}{'=' * 50}{Warna.RESET}")


def to_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def default_task_stats() -> dict[str, int]:
    return {"attempted": 0, "success": 0, "cooldown": 0, "failed": 0}


def mask_proxy(proxy: str) -> str:
    if not proxy:
        return "no-proxy"
    if "@" not in proxy:
        return proxy
    scheme, rest = ("", proxy)
    if "://" in proxy:
        scheme, rest = proxy.split("://", 1)
        scheme += "://"
    host_part = rest.rsplit("@", 1)[-1]
    return f"{scheme}****:****@{host_part}"


def compact_proxy(proxy: str) -> str:
    if not proxy:
        return "direct"
    scheme = ""
    rest = proxy
    if "://" in proxy:
        scheme, rest = proxy.split("://", 1)
        scheme += "://"
    host_part = rest.rsplit("@", 1)[-1]
    return f"{scheme}{host_part}"


def format_progress(progress: dict | None) -> str:
    if not progress:
        return "err"
    today = f"{progress.get('today_count', 0)}/{progress.get('today_limit', 0)}"
    cycle = f"{progress.get('progress_count', 0)}/{progress.get('require_count', 0)}"
    return f"today {today}, cycle {cycle}"


def format_progress_short(progress: dict | None) -> str:
    if not progress:
        return "err"
    today = f"{progress.get('today_count', 0)}/{progress.get('today_limit', 0)}"
    cycle = f"{progress.get('progress_count', 0)}/{progress.get('require_count', 0)}"
    return f"today={today} cycle={cycle}"


def format_task_compact(progress: dict | None) -> tuple[str, str]:
    if not progress:
        return "err", "err"
    today_count = to_int(progress.get("today_count"), 0)
    today_limit = to_int(progress.get("today_limit"), 0)
    progress_count = to_int(progress.get("progress_count"), 0)
    require_count = to_int(progress.get("require_count"), 0)
    today = f"{today_count}/{today_limit}"
    cycle = f"{progress_count}/{require_count}"
    if today_limit > 0 and today_count >= today_limit:
        today = f"{Warna.HIJAU}{today}{Warna.RESET}"
    if require_count > 0 and progress_count >= require_count:
        cycle = f"{Warna.HIJAU}{cycle}{Warna.RESET}"
    return today, cycle


def task_selesai_hari_ini(progress: dict | None) -> bool:
    if not progress:
        return False
    today_count = to_int(progress.get("today_count"), 0)
    today_limit = to_int(progress.get("today_limit"), 0)
    return today_limit > 0 and today_count >= today_limit


def load_lines(filename: str) -> list[str]:
    filepath = path_dari_folder_skrip(filename)
    if not filepath.exists():
        return []
    return [
        line.strip()
        for line in filepath.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _looks_like_init_data(s: str) -> bool:
    if not s:
        return False
    sl = s.lower()
    return ("auth_date=" in sl) or sl.startswith("user=") or sl.startswith("query_id=")


def load_accounts(filename: str = ACCOUNT_FILE) -> list[dict[str, Any]]:
    filepath = path_dari_folder_skrip(filename)
    accounts: list[dict[str, Any]] = []
    if not filepath.exists():
        return accounts

    for line_no, line in enumerate(filepath.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 4:
            tprint(f"{Warna.KUNING}[SKIP] akun.txt baris {line_no}: format tidak lengkap.{Warna.RESET}")
            continue
        token = parts[3].strip()
        if not token:
            tprint(f"{Warna.KUNING}[SKIP] akun.txt baris {line_no}: initial_reward_token kosong.{Warna.RESET}")
            continue

        # Deteksi cerdas: init_data bisa di kolom 5 atau 6, display_name di yang lain.
        col5 = parts[4].strip() if len(parts) >= 5 else ""
        col6 = parts[5].strip() if len(parts) >= 6 else ""

        init_data = ""
        display_name = ""
        if _looks_like_init_data(col5):
            init_data = col5
            display_name = col6
        elif _looks_like_init_data(col6):
            init_data = col6
            display_name = col5
        else:
            display_name = col5

        accounts.append(
            {
                "user_id": parts[0].strip(),
                "native_user_id": parts[1].strip(),
                "native_user_pwd": parts[2].strip(),
                "initial_reward_token": token,
                "display_name": display_name,
                "init_data": init_data,
                "line_no": line_no,
            }
        )
    return accounts


DEFAULT_PROXY_SCHEME = "socks5"


def normalize_proxy_line(line: str) -> str:
    """Konversi berbagai format proxy ke bentuk scheme://user:pass@host:port.

    Diterima:
    - scheme://user:pass@host:port
    - scheme://host:port
    - user:pass@host:port
    - host:port:user:pass    (default scheme socks5)
    - host:port              (default scheme socks5)
    """
    s = line.strip()
    if not s:
        return ""

    # 1) Sudah ada scheme://
    for scheme in ("socks5://", "socks4://", "http://", "https://"):
        if s.lower().startswith(scheme):
            return s

    # 2) Bentuk user:pass@host:port (tanpa scheme)
    if "@" in s:
        return f"{DEFAULT_PROXY_SCHEME}://{s}"

    parts = s.split(":")
    # 3) host:port
    if len(parts) == 2:
        host, port = parts
        return f"{DEFAULT_PROXY_SCHEME}://{host}:{port}"
    # 4) host:port:user:pass
    if len(parts) == 4:
        host, port, user, passwd = parts
        return f"{DEFAULT_PROXY_SCHEME}://{user}:{passwd}@{host}:{port}"
    # 5) Tidak dikenali, fallback http:// di depan supaya tidak crash
    return f"http://{s}"


def load_proxy_strings(filename: str = PROXY_FILE) -> list[str]:
    proxies: list[str] = []
    for line in load_lines(filename):
        normalized = normalize_proxy_line(line)
        if normalized:
            proxies.append(normalized)
    return proxies


def parse_claim_proxy(proxy_str: str) -> dict[str, Any] | None:
    try:
        s = normalize_proxy_line(proxy_str)
        proxy_scheme = "http"
        for scheme in ("socks5://", "socks4://", "http://", "https://"):
            if s.lower().startswith(scheme):
                proxy_scheme = scheme[:-3].lower()
                s = s[len(scheme):]
                break
        if "@" in s:
            auth, hostport = s.rsplit("@", 1)
            user, passwd = auth.split(":", 1)
        else:
            user = passwd = ""
            hostport = s
        host, port = hostport.rsplit(":", 1)
        return {
            "scheme": proxy_scheme,
            "host": host.strip(),
            "port": int(port.strip()),
            "user": user.strip(),
            "pass": passwd.strip(),
        }
    except Exception:
        return None


def load_claim_proxies(filename: str = PROXY_FILE) -> list[dict[str, Any]]:
    return [p for p in (parse_claim_proxy(line) for line in load_lines(filename)) if p]


def format_claim_proxy_url(proxy: dict[str, Any]) -> str:
    auth = ""
    if proxy.get("user"):
        auth = f"{proxy['user']}:{proxy.get('pass', '')}@"
    return f"{proxy.get('scheme', 'http')}://{auth}{proxy['host']}:{proxy['port']}"


def mask_claim_proxy(proxy: dict[str, Any] | None) -> str:
    if not proxy:
        return "direct"
    auth = "****:****@" if proxy.get("user") else ""
    return f"{proxy.get('scheme', 'http')}://{auth}{proxy['host']}:{proxy['port']}"


def ringkas_resp(resp: dict | None) -> str:
    if not isinstance(resp, dict):
        return "no-response"
    msg = resp.get("msg") or resp.get("message") or resp.get("error") or ""
    status = resp.get("status")
    if msg:
        return f"status={status} msg={msg}"
    return f"status={status} keys={','.join(map(str, list(resp.keys())[:6]))}"


def ringkas_json(value: Any, limit: int = 500) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = str(value)
    if len(text) > limit:
        return text[:limit] + "...(cut)"
    return text


def display_reward_username(username: str) -> str:
    if username.startswith("tg_") and len(username) > 3:
        return username[3:]
    return username


def green_positive(text: str) -> str:
    return f"{Warna.HIJAU}{text}{Warna.RESET}" if text.startswith("+") else text


def format_account_label(idx: int, total: int, username: str, balance: float | None = None) -> str:
    idx_w = len(str(total))
    idx_part = f"[{idx:0{idx_w}d}/{total}]"
    user_part = f"{username:<12}"
    if balance is None:
        return f"{idx_part} {user_part}"
    bal_part = f"[{Warna.HIJAU}{int(balance):^4} WEW{Warna.RESET}]"
    return f"{idx_part} {user_part} {bal_part}"


def nilai_can_claim(value: Any) -> bool:
    if value is True:
        return True
    if value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() == "true":
        return True
    return False


def debug_task_list(username: str, tasks: list[dict[str, Any]], source: str) -> None:
    if not DEBUG_HTTP:
        return
    for task in tasks:
        progress = task.get("progress") or {}
        dprint(
            f"TASK {source} user={username} id={task.get('id')} event={task.get('event_type')} "
            f"can_claim={progress.get('can_claim')!r} "
            f"today={progress.get('today_count')}/{progress.get('today_limit')} "
            f"cycle={progress.get('progress_count')}/{progress.get('require_count')} "
            f"reward={task.get('point_reward')}"
        )


# ============================================================
# Reward API: checkin, spin, claim
# ============================================================


class Socks5Connection:
    __slots__ = ("sock",)

    def __init__(self, proxy: dict[str, Any], target_host: str, target_port: int, timeout: int = 20):
        self.sock = socket.create_connection((proxy["host"], proxy["port"]), timeout=timeout)
        self._handshake(proxy, target_host, target_port)

    def _handshake(self, proxy: dict[str, Any], target_host: str, target_port: int) -> None:
        user = proxy.get("user", "")
        passwd = proxy.get("pass", "")
        self.sock.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
        chosen = self.sock.recv(2)
        if len(chosen) < 2 or chosen[0] != 5:
            raise OSError("SOCKS5: invalid greeting")
        if chosen[1] == 0x02:
            cred = bytes([0x01, len(user)]) + user.encode() + bytes([len(passwd)]) + passwd.encode()
            self.sock.sendall(cred)
            if self.sock.recv(2)[1] != 0:
                raise OSError("SOCKS5: auth failed")
        elif chosen[1] != 0x00:
            raise OSError("SOCKS5: no auth method")

        host_enc = target_host.encode()
        req = b"\x05\x01\x00\x03" + bytes([len(host_enc)]) + host_enc + target_port.to_bytes(2, "big")
        self.sock.sendall(req)
        resp = self.sock.recv(10)
        if len(resp) < 2 or resp[1] != 0:
            code = resp[1] if len(resp) >= 2 else "short"
            raise OSError(f"SOCKS5: connect failed code={code}")

    def wrap_ssl(self, server_hostname: str) -> None:
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(self.sock, server_hostname=server_hostname)

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read_all(self) -> bytes:
        chunks = []
        while True:
            chunk = self.sock.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


def socks5_post(url: str, body: bytes, headers: dict[str, str], proxy: dict[str, Any], timeout: int = 20) -> dict:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"

    conn = Socks5Connection(proxy, host, port, timeout)
    if parsed.scheme == "https":
        conn.wrap_ssl(host)

    all_headers = {"Host": host, "Content-Length": str(len(body)), "Connection": "close", **headers}
    req_lines = f"POST {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in all_headers.items()) + "\r\n"
    try:
        conn.send(req_lines.encode() + body)
        raw = conn.read_all()
    finally:
        conn.close()

    resp_body = raw.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in raw else raw
    return json.loads(resp_body.decode("utf-8", errors="replace"))


def reward_build_sign(action: str, data: dict[str, Any], timestamp: int) -> str:
    data_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    raw = f"appId={REWARD_APP_ID}&action={action}&data={data_str}&timestamp={timestamp}{REWARD_SIGN_SALT}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _tg_extract_start_param(initdata: str) -> str:
    try:
        from urllib.parse import parse_qs, unquote
        params = parse_qs(unquote(initdata))
        return params.get("start_param", [""])[0]
    except Exception:
        return ""


def tg_login_refresh(akun: "AkunState") -> bool:
    """Re-fetch reward token via TGLogin menggunakan init_data Telegram.
    Update file akun.txt agar token baru persist. Return True jika sukses.
    """
    initdata = (akun.init_data or "").strip()
    if not initdata:
        return False
    start_param = _tg_extract_start_param(initdata)
    payload = {
        "init_data": initdata,
        "start_param": start_param,
    }
    resp = reward_call_api("TGLogin", "", payload, proxy=None)
    if resp.get("status") != 200:
        vprint(f"[TGLOGIN FAIL] {akun.user_id} | {ringkas_resp(resp)}")
        return False
    inner = resp.get("data") or {}
    if inner.get("code") != 0:
        vprint(f"[TGLOGIN FAIL] {akun.user_id} | code={inner.get('code')} msg={inner.get('msg')}")
        return False
    new_token = inner.get("token", "")
    if not new_token:
        return False
    old_token = akun.initial_reward_token
    if new_token == old_token:
        return True
    akun.initial_reward_token = new_token
    akun.reward_token = new_token
    if akun._account is not None:
        akun._account["initial_reward_token"] = new_token
    _persist_account_token(akun.user_id, new_token)
    log_step("OK", "REWARD", akun.label(), "TGLogin Refresh")
    return True


def _persist_account_token(user_id: str, new_token: str) -> None:
    """Tulis ulang akun.txt dengan token baru untuk user_id tertentu."""
    filepath = path_dari_folder_skrip(ACCOUNT_FILE)
    if not filepath.exists():
        return
    try:
        lines = filepath.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        log_warn(f"Persist token gagal baca akun.txt: {exc}")
        return
    changed = False
    new_lines = []
    for line in lines:
        s = line.rstrip("\r")
        if not s.strip() or s.strip().startswith("#"):
            new_lines.append(s)
            continue
        parts = s.split("|")
        if len(parts) >= 4 and parts[0].strip() == str(user_id):
            parts[3] = new_token
            new_lines.append("|".join(parts))
            changed = True
        else:
            new_lines.append(s)
    if changed:
        try:
            filepath.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            mark_account_sync_dirty()
            maybe_sync_account_file()
        except Exception as exc:
            log_warn(f"Persist token gagal tulis akun.txt: {exc}")


def reward_call_api(
    action: str,
    token: str,
    data: dict[str, Any] | None = None,
    proxy: dict[str, Any] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    if data is None:
        data = {}

    payload_data = {
        "lang": LANG,
        "device_type": "OTHER",
        "app_type": "pc",
        "st": int(time.time()),
        "_ssid": "",
        **data,
    }
    timestamp = int(time.time() * 1000)
    payload = {
        "appId": REWARD_APP_ID,
        "action": action,
        "token": token,
        "lang": LANG,
        "timestamp": timestamp,
        "data": json.dumps(payload_data, ensure_ascii=False, separators=(",", ":")),
        "sign": reward_build_sign(action, payload_data, timestamp),
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0",
    }
    dprint(
        f"REWARD REQ action={action} token={token[:6]}... proxy={mask_claim_proxy(proxy)} "
        f"data={ringkas_json(payload_data)}"
    )

    try:
        if proxy:
            scheme = proxy.get("scheme", "http")
            if scheme == "socks5":
                result = socks5_post(REWARD_API_URL, body, headers, proxy, timeout)
                dprint(f"REWARD RES action={action} {ringkas_resp(result)} body={ringkas_json(result)}")
                return result
            if scheme in ("http", "https"):
                proxy_url = format_claim_proxy_url(proxy)
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                )
                req = urllib.request.Request(REWARD_API_URL, data=body, headers=headers, method="POST")
                with opener.open(req, timeout=timeout) as resp:
                    result = json.loads(resp.read().decode("utf-8", errors="replace"))
                    dprint(f"REWARD RES action={action} {ringkas_resp(result)} body={ringkas_json(result)}")
                    return result

            return {"status": -1, "msg": f"unsupported proxy scheme: {scheme}"}

        req = urllib.request.Request(REWARD_API_URL, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8", errors="replace"))
            dprint(f"REWARD RES action={action} {ringkas_resp(result)} body={ringkas_json(result)}")
            return result
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8", errors="replace"))
            dprint(f"REWARD HTTPERR action={action} code={exc.code} body={ringkas_json(result)}")
            return result
        except Exception:
            dprint(f"REWARD HTTPERR action={action} code={exc.code} msg={exc}")
            return {"status": exc.code, "msg": str(exc)}
    except Exception as exc:
        dprint(f"REWARD EXC action={action} proxy={mask_claim_proxy(proxy)} msg={exc}")
        return {"status": -1, "msg": str(exc)}


def reward_resolve_proxy(token: str, proxies: list[dict[str, Any]], start_idx: int) -> tuple[dict | None, dict | None]:
    tried = 0
    for offset in range(len(proxies)):
        if tried >= MAX_LOGIN_RETRY:
            break
        proxy = proxies[(start_idx + offset) % len(proxies)]
        vprint(f"[REWARD LOGIN] token={token[:6]}... coba {mask_claim_proxy(proxy)}")
        resp = reward_call_api("GetUserInfo", token, proxy=proxy)
        tried += 1
        if resp.get("status") == 200:
            vprint(f"[REWARD LOGIN OK] token={token[:6]}... via {mask_claim_proxy(proxy)}")
            return resp, proxy
        vprint(f"[REWARD LOGIN FAIL] token={token[:6]}... via {mask_claim_proxy(proxy)} | {ringkas_resp(resp)}")

    if ALLOW_DIRECT_REWARD_FALLBACK:
        vprint(f"[REWARD LOGIN] token={token[:6]}... coba direct")
        resp = reward_call_api("GetUserInfo", token, proxy=None)
        if resp.get("status") == 200:
            vprint(f"[REWARD LOGIN OK] token={token[:6]}... via direct")
            return resp, None
        vprint(f"[REWARD LOGIN FAIL] token={token[:6]}... via direct | {ringkas_resp(resp)}")
    return None, None


def reward_login_account(
    idx: int,
    total: int,
    account: dict[str, Any],
    proxies: list[dict[str, Any]],
) -> dict[str, Any] | None:
    token = account["initial_reward_token"]
    label = account.get("display_name") or account.get("user_id") or token[:6]
    start_idx = (idx - 1) % len(proxies) if proxies else 0
    short_tok = token[:6] + "..."

    if proxies:
        resp, proxy_used = reward_resolve_proxy(token, proxies, start_idx)
    else:
        vprint(f"[REWARD LOGIN] token={token[:6]}... coba direct")
        resp = reward_call_api("GetUserInfo", token, proxy=None)
        proxy_used = None
        if resp.get("status") == 200:
            vprint(f"[REWARD LOGIN OK] token={token[:6]}... via direct")
        else:
            vprint(f"[REWARD LOGIN FAIL] token={token[:6]}... via direct | {ringkas_resp(resp)}")

    if resp is None or resp.get("status") != 200:
        # Coba refresh lewat TGLogin kalau ada init_data, lalu retry sekali.
        initdata = (account.get("init_data") or "").strip()
        refreshed = False
        if initdata:
            tg_payload = {
                "init_data": initdata,
                "start_param": _tg_extract_start_param(initdata),
            }
            tg_resp = reward_call_api("TGLogin", "", tg_payload, proxy=None)
            if tg_resp.get("status") == 200:
                inner = tg_resp.get("data") or {}
                if inner.get("code") == 0 and inner.get("token"):
                    new_token = inner["token"]
                    account["initial_reward_token"] = new_token
                    token = new_token
                    _persist_account_token(account.get("user_id", ""), new_token)
                    log_step("OK", "REWARD", f"[{idx}/{total}] {short_tok}", "TGLogin Refresh")
                    short_tok = new_token[:6] + "..."
                    refreshed = True
                else:
                    log_step(
                        "WARN", "REWARD", f"[{idx}/{total}] {short_tok}",
                        f"TGLogin Failed | code={inner.get('code')} msg={inner.get('msg')}",
                    )
            else:
                log_step(
                    "WARN", "REWARD", f"[{idx}/{total}] {short_tok}",
                    f"TGLogin Failed | {ringkas_resp(tg_resp)}",
                )
        else:
            log_step(
                "WARN", "REWARD", f"[{idx}/{total}] {short_tok}",
                "No init_data, cannot refresh",
            )

        if refreshed and proxies:
            resp, proxy_used = reward_resolve_proxy(token, proxies, start_idx)
        elif refreshed:
            resp = reward_call_api("GetUserInfo", token, proxy=None)
            proxy_used = None

        if resp is None or resp.get("status") != 200:
            fallback_text = " + direct" if ALLOW_DIRECT_REWARD_FALLBACK else ""
            log_step("ERR", "REWARD", f"[{idx}/{total}] {short_tok}", f"Login Failed After {MAX_LOGIN_RETRY} Proxy{fallback_text}")
            return None

    info = resp.get("data") or {}
    username = info.get("username") or label or "?"
    username = display_reward_username(str(username))
    balance = float(info.get("balance_free", 0) or 0)
    via = f"Proxy: {proxy_used['host']}" if proxy_used else "Direct"
    reward_label = format_account_label(idx, total, username, balance)
    log_step("OK", "REWARD", reward_label, via)
    return {"token": token, "username": username, "balance": balance, "proxy": proxy_used, "account": account}


def reward_claim_available(
    idx: int,
    total: int,
    token: str,
    username: str,
    proxy_used: dict[str, Any] | None,
    *,
    quiet_no_claim: bool = True,
    reward_login: dict[str, Any] | None = None,
) -> int:
    rt = reward_call_api("GetTaskList", token, proxy=proxy_used)
    if rt.get("status") != 200:
        if not quiet_no_claim:
            log_warn(f"[{idx}/{total}] {username} gagal ambil tasklist")
        return 0

    tasks = (rt.get("data") or {}).get("tasks", [])
    debug_task_list(username, tasks, "CLAIM")
    claimable = [t for t in tasks if nilai_can_claim((t.get("progress") or {}).get("can_claim"))]
    dprint(f"CLAIM CHECK [{idx}/{total}] {username} tasks={len(tasks)} claimable={len(claimable)}")
    claim_label = format_account_label(idx, total, username, float(reward_login["balance"])) if reward_login else format_account_label(idx, total, username)
    if not claimable:
        if not quiet_no_claim:
            maxed = [
                t for t in tasks
                if (t.get("progress") or {}).get("today_count", 0) >= (t.get("progress") or {}).get("today_limit", 1)
            ]
            if len(maxed) == len(tasks):
                log_step("DONE", "CLAIM", claim_label, "Tasks Max")
        return 0

    success = 0
    total_pts = 0.0
    for task in claimable:
        tid = task.get("id")
        reward = float(task.get("point_reward", 0) or 0)
        dprint(f"CLAIM TRY [{idx}/{total}] {username} task={tid} reward={reward}")
        r = reward_call_api("ClaimTask", token, {"task_id": str(tid)}, proxy=proxy_used)
        ok = r.get("status") == 200 and (r.get("data") or {}).get("code") == 0
        if ok:
            success += 1
            total_pts += reward
        else:
            vprint(f"[CLAIM FAIL] [{idx}/{total}] {username} task={tid} | {ringkas_resp(r)}")
        if len(claimable) > 1:
            time.sleep(DELAY_CLAIM)

    if success:
        if reward_login is not None:
            reward_login["balance"] = float(reward_login.get("balance", 0) or 0) + total_pts
            account = reward_login.get("account")
            if account is not None:
                account["log_label"] = format_account_label(idx, total, username, float(reward_login["balance"]))
            claim_label = format_account_label(idx, total, username, float(reward_login["balance"]))
        log_step("OK", "CLAIM", claim_label, f"{success}/{len(claimable)} | {green_positive(f'+{total_pts:.0f} WEW')}")
    else:
        log_step("WARN", "CLAIM", claim_label, "Claim Failed")
    return success


def reward_process_account(
    idx: int,
    total: int,
    account: dict[str, Any],
    proxies: list[dict[str, Any]],
    *,
    do_checkin: bool,
    do_spin: bool,
    do_claim: bool,
) -> None:
    login = reward_login_account(idx, total, account, proxies)
    if not login:
        return
    token = login["token"]
    username = login["username"]
    proxy_used = login["proxy"]
    balance = login.get("balance", 0)
    acc_label = format_account_label(idx, total, username, float(balance))

    if do_checkin:
        rc = reward_call_api("GetCheckinStatus", token, proxy=proxy_used)
        if rc.get("status") == 200:
            cd = rc.get("data") or {}
            if cd.get("code") == 0 and not cd.get("signed_today"):
                rd = reward_call_api("DoCheckin", token, proxy=proxy_used)
                if rd.get("status") == 200 and (rd.get("data") or {}).get("code") == 0:
                    pts = (rd.get("data") or {}).get("points_earned", 0)
                    log_step("OK", "CHECKIN", acc_label, green_positive(f"+{pts} pts"))

    if do_spin:
        rs = reward_call_api("GetFreespinInfo", token, proxy=proxy_used)
        if rs.get("status") == 200:
            sd = rs.get("data") or {}
            remaining = int(sd.get("remaining", 0) or 0)
            if remaining > 0:
                spin_ok = 0
                for _ in range(remaining):
                    rd = reward_call_api("DoFreespin", token, proxy=proxy_used)
                    if rd.get("status") == 200 and (rd.get("data") or {}).get("code") == 0:
                        spin_ok += 1
                    time.sleep(DELAY_CLAIM)
                log_step("OK", "SPIN", acc_label, f"{spin_ok}/{remaining}")

    if not do_claim:
        return

    reward_claim_available(idx, total, token, username, proxy_used, quiet_no_claim=False)


def run_reward_phase(
    accounts: list[dict[str, Any]],
    phase_name: str,
    *,
    do_checkin: bool,
    do_spin: bool,
    do_claim: bool,
) -> None:
    proxies = load_claim_proxies()
    total = len(accounts)
    log_info(f"=== {phase_name} | {total} akun | {len(proxies)} proxy | {MAX_WORKERS} thread ===")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                reward_process_account,
                i,
                total,
                account,
                proxies,
                do_checkin=do_checkin,
                do_spin=do_spin,
                do_claim=do_claim,
            ): i
            for i, account in enumerate(accounts, 1)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                future.result()
            except Exception as exc:
                log_err(f"Akun ke-{i} exception: {exc}")

    log_info(f"=== {phase_name} selesai ===")


# ============================================================
# Watch ads
# ============================================================


class AkunState:
    def __init__(self, acc: dict[str, Any], proxy: str):
        self.user_id = acc["user_id"]
        self.native_user_id = acc["native_user_id"]
        self.native_user_pwd = acc["native_user_pwd"]
        self.initial_reward_token = acc["initial_reward_token"]
        self.display_name = acc.get("display_name", "")
        self.init_data = acc.get("init_data", "")
        self._account = acc

        self.reward_token = ""
        self.game_token = ""
        self.reward_user_token = ""
        self.sesi_ssid = ""
        self.waktu_st = 0
        self.nama_akun = ""
        self.log_label = acc.get("log_label") or acc.get("display_name") or self.user_id
        self.current_proxy = proxy
        self.valid = False
        self.done = False
        self.stats = {"reward": default_task_stats(), "interstitial": default_task_stats()}
        # Timestamp epoch kapan task type tertentu boleh di-attempt lagi (token cooldown).
        self.token_next_allowed: dict[str, float] = {"reward": 0.0, "interstitial": 0.0}
        self._last_summary_time = 0
        self._round_count = 0
        self._last_token_refresh = 0.0

        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def update_proxy(self, proxy: str) -> None:
        self.current_proxy = proxy
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        else:
            self.session.proxies = {}

    def label(self) -> str:
        return self.log_label

    def should_print_summary(self) -> bool:
        now = time.time()
        if now - self._last_summary_time >= SUMMARY_INTERVAL:
            self._last_summary_time = now
            return True
        return False

    def summary_stats(self) -> str:
        r = self.stats["reward"]
        i = self.stats["interstitial"]
        return f"R ok={r['success']} cd={r['cooldown']} err={r['failed']} | I ok={i['success']} cd={i['cooldown']} err={i['failed']}"


def _proxy_ip(proxy: str) -> str:
    """Ambil host/IP dari proxy string. Cooldown server berdasarkan IP, port tidak peduli."""
    if not proxy:
        return ""
    s = proxy
    if "://" in s:
        s = s.split("://", 1)[1]
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    if ":" in s:
        s = s.rsplit(":", 1)[0]
    return s.strip()


class AdsProxyManager:
    def __init__(self):
        self.proxies: list[str] = []
        self._lock = threading.Lock()
        self.last_proxy: dict[str, str] = {}
        # Map IP -> map task_type -> epoch expiry. Cooldown berdasarkan IP.
        self.cooldown: dict[str, dict[str, float]] = {}
        self.current_round = 0
        # Statistik untuk log start.
        self.unique_ips: int = 0

    def load(self, proxies: list[str]) -> None:
        # Dedup per IP agar tidak rotasi ke port yang IP-nya sama dengan yang baru cooldown.
        seen_ips: set[str] = set()
        unique: list[str] = []
        for p in proxies:
            ip = _proxy_ip(p)
            if not ip or ip in seen_ips:
                continue
            seen_ips.add(ip)
            unique.append(p)
        self.proxies = unique
        self.unique_ips = len(unique)

    def mulai_round_baru(self, round_num: int) -> None:
        with self._lock:
            self.current_round = round_num

    def _is_cooldown(self, proxy: str, task_type: str) -> bool:
        ip = _proxy_ip(proxy)
        info = self.cooldown.get(ip)
        if not info:
            return False
        exp = info.get(task_type, 0.0)
        if exp <= time.time():
            info.pop(task_type, None)
            if not info:
                self.cooldown.pop(ip, None)
            return False
        return True

    def _kandidat(self, account_key: str, task_type: str, exclude: set[str] | None = None) -> list[str]:
        last = self.last_proxy.get(account_key, "")
        ex = exclude or set()
        kandidat = [
            p for p in self.proxies
            if p not in ex and p != last and not self._is_cooldown(p, task_type)
        ]
        if kandidat:
            return kandidat
        kandidat = [p for p in self.proxies if p not in ex and not self._is_cooldown(p, task_type)]
        return kandidat

    def ambil_proxy(self, account_key: str, task_type: str = "reward") -> str:
        if not self.proxies:
            return ""
        with self._lock:
            kandidat = self._kandidat(account_key, task_type)
            if not kandidat:
                last = self.last_proxy.get(account_key, "")
                kandidat = [p for p in self.proxies if p != last] or list(self.proxies)
            proxy = random.choice(kandidat)
            self.last_proxy[account_key] = proxy
            return proxy

    def ambil_proxy_swap(self, account_key: str, proxy_gagal: str, task_type: str = "reward") -> str:
        with self._lock:
            self._set_cooldown_locked(proxy_gagal, task_type)
            kandidat = self._kandidat(account_key, task_type, exclude={proxy_gagal})
            if not kandidat:
                return ""
            proxy = random.choice(kandidat)
            self.last_proxy[account_key] = proxy
            return proxy

    def _set_cooldown_locked(self, proxy: str, task_type: str) -> None:
        ttl = PROXY_COOLDOWN.get(task_type, PROXY_COOLDOWN_DEFAULT)
        if ttl <= 0:
            return
        ip = _proxy_ip(proxy)
        if not ip:
            return
        info = self.cooldown.setdefault(ip, {})
        info[task_type] = time.time() + ttl

    def laporkan_cooldown(self, proxy: str, task_type: str = "reward") -> None:
        with self._lock:
            self._set_cooldown_locked(proxy, task_type)


ads_proxy_manager = AdsProxyManager()


def buat_sign(app_id_hash: str, action: str, data_dict: dict[str, Any], timestamp: int) -> tuple[str, str]:
    salt = "rwdCgrdxrCrgBgeikLpboaki4R2zz9bbb" if app_id_hash == "hi5reward" else "wbCgrdxrCrgAgeikLpboaki4R2zz9aaa"
    data_str = json.dumps(data_dict, separators=(",", ":"))
    raw_str = f"appId={app_id_hash}&action={action}&data={data_str}&timestamp={timestamp}{salt}"
    return hashlib.md5(raw_str.encode("utf-8")).hexdigest().upper(), data_str


def kirim_request(akun: AkunState, url: str, tipe_endpoint: str, action: str, token: str, data_dict: dict[str, Any]) -> dict:
    timestamp = int(time.time() * 1000)
    app_id_hash = "hi5reward" if tipe_endpoint == "reward" else "hi5games"
    sign, data_str = buat_sign(app_id_hash, action, data_dict, timestamp)

    if tipe_endpoint == "v1":
        payload = {"gameId": GAME_ID, "action": action, "token": token, "lang": "en", "plat": "hi5", "timestamp": timestamp, "data": data_str, "sign": sign}
    elif tipe_endpoint == "home":
        payload = {"appId": "hi5games", "action": action, "token": token, "lang": "en", "timestamp": timestamp, "data": data_str, "sign": sign}
    elif tipe_endpoint == "reward":
        payload = {"appId": "hi5reward", "action": action, "data": data_str, "timestamp": timestamp, "sign": sign}
        if token:
            payload["token"] = token
            payload["lang"] = "en"
    else:
        return {"status": None}

    headers = {
        "Content-Type": "application/json",
        "Connection": "keep-alive",
        "User-Agent": "Mozilla/5.0 (Linux; Android 9; SM-S9210 Build/PQ3A.190705.05211459; wv) AppleWebKit/537.36",
    }
    req_proxies = {"http": akun.current_proxy, "https": akun.current_proxy} if akun.current_proxy else None
    dprint(
        f"WATCH REQ akun={akun.label()} endpoint={tipe_endpoint} action={action} "
        f"token={(token[:6] + '...') if token else 'empty'} proxy={mask_proxy(akun.current_proxy)} "
        f"data={ringkas_json(data_dict)}"
    )

    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = akun.session.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT, proxies=req_proxies)
            try:
                response.raise_for_status()
                result = response.json()
                dprint(f"WATCH RES akun={akun.label()} action={action} attempt={attempt} {ringkas_resp(result)} body={ringkas_json(result)}")
                return result
            finally:
                response.close()
        except requests.RequestException as exc:
            if DEBUG_HTTP:
                dprint(f"WATCH EXC akun={akun.label()} action={action} attempt={attempt} proxy={mask_proxy(akun.current_proxy)} msg={exc}")
        except ValueError as exc:
            if DEBUG_HTTP:
                dprint(f"WATCH INVALID_JSON akun={akun.label()} action={action} attempt={attempt} msg={exc}")
            return {"status": None, "error": "invalid_json"}

        if attempt < REQUEST_RETRIES:
            time.sleep(REQUEST_RETRY_DELAY)
    return {"status": None, "error": "request_failed"}


def inisialisasi_sesi(akun: AkunState) -> bool:
    if not akun.reward_token:
        akun.reward_token = akun.initial_reward_token

    data_init = {"device_type": "ANDROID", "app_type": "mo", "st": 0, "_ssid": ""}
    res_init = kirim_request(akun, "https://www.hi5games.top/home/api", "home", "InitAppData", "", data_init)
    try:
        akun.waktu_st = res_init.get("st", int(time.time()))
        akun.sesi_ssid = res_init["data"]["InitAppData"]["AppSettings"]["_ssid"]
    except (KeyError, TypeError):
        vprint(f"{Warna.MERAH}[GAGAL] Login {akun.user_id}: tidak dapat ssid.{Warna.RESET}")
        return False

    data_login = {
        "userId": akun.native_user_id,
        "userPwd": akun.native_user_pwd,
        "apptype": "GoogleApk",
        "device_type": "ANDROID",
        "app_type": "mo",
        "st": akun.waktu_st,
        "_ssid": akun.sesi_ssid,
    }
    res_login = kirim_request(akun, "https://www.hi5games.top/home/api", "home", "Login_Native", "", data_login)
    try:
        data_user = res_login["data"]["UserInfo"]
        akun.game_token = res_login["data"]["LoginEnd"]["token"]
        akun.nama_akun = data_user.get("userNick", "unknown")
        if not akun.display_name:
            akun.display_name = akun.nama_akun
    except (KeyError, TypeError):
        vprint(f"{Warna.MERAH}[GAGAL] Login {akun.user_id}: ditolak server.{Warna.RESET}")
        return False

    data_launch = {"game_id": 1, "lang": "en", "device_type": "ANDROID", "app_type": "mo", "st": int(time.time()), "_ssid": ""}
    res_launch = kirim_request(akun, "https://www.hi5games.top/reward/api", "reward", "GameLaunch", akun.reward_token, data_launch)
    if res_launch.get("status") == 200:
        try:
            akun.reward_user_token = res_launch["data"]["launch_token"]
        except (KeyError, TypeError):
            akun.reward_user_token = akun.reward_token
    else:
        akun.reward_user_token = akun.reward_token

    log_step("OK", "WATCH", akun.label(), "Login")
    return True


def get_action_data(response: dict, action: str) -> dict:
    if not isinstance(response, dict):
        return {}
    data = response.get("data", {})
    if not isinstance(data, dict):
        return {}
    action_data = data.get(action, {})
    return action_data if isinstance(action_data, dict) else {}


def get_task_items(data: dict) -> list[dict]:
    if not isinstance(data, dict):
        return []
    candidates = []
    for action_key in ("GetTaskList", "GetEarnPageData"):
        nested = data.get(action_key)
        if isinstance(nested, dict):
            candidates.extend(get_task_items(nested))
    for key in ("tasks", "task_list"):
        value = data.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    groups = data.get("task_groups")
    if isinstance(groups, dict):
        groups = list(groups.values())
    if not isinstance(groups, list):
        groups = []
    for group in groups:
        if isinstance(group, dict):
            value = group.get("tasks") or group.get("task_list")
            if isinstance(value, list):
                candidates.extend(value)
    return candidates


def _try_get_task_progress_once(akun: AkunState, task_id: int, tokens: list[str], profiles: list[dict[str, str]]) -> dict | None:
    """Sekali jalan ambil progress task tertentu. Return progress dict atau None."""
    for task_token in tokens:
        for profile in profiles:
            data = {
                "lang": "en",
                "device_type": profile["device_type"],
                "app_type": profile["app_type"],
                "st": akun.waktu_st or int(time.time()),
                "_ssid": "",
            }
            response = kirim_request(akun, "https://www.hi5games.top/reward/api", "reward", "GetTaskList", task_token, data)
            if isinstance(response, dict) and response.get("st"):
                akun.waktu_st = to_int(response.get("st"), akun.waktu_st)
            if response.get("status") != 200:
                continue
            for task in get_task_items(response.get("data", {})):
                if isinstance(task, dict) and str(task.get("id")) == str(task_id):
                    progress = task.get("progress", {})
                    return progress if isinstance(progress, dict) else {}
    return None


def get_task_progress(akun: AkunState, task_id: int = 29, account_key: str | None = None) -> dict | None:
    tokens = [t for t in (akun.reward_token, akun.reward_user_token) if t]
    tokens = list(dict.fromkeys(tokens))  # dedup, preserve order
    profiles = [{"device_type": "OTHER", "app_type": "pc"}, {"device_type": "ANDROID", "app_type": "mo"}]

    # Attempt 1: normal
    result = _try_get_task_progress_once(akun, task_id, tokens, profiles)
    if result is not None:
        return result

    # Attempt 2: rotasi proxy
    if account_key:
        new_proxy = ads_proxy_manager.ambil_proxy(account_key)
        if new_proxy and new_proxy != akun.current_proxy:
            akun.update_proxy(new_proxy)
            time.sleep(0.5)
            result = _try_get_task_progress_once(akun, task_id, tokens, profiles)
            if result is not None:
                return result

    # Attempt 3: kemungkinan token expired -> coba TGLogin refresh, lalu re-login native (debounced 60s).
    now = time.time()
    last_refresh = getattr(akun, "_last_token_refresh", 0.0)
    if now - last_refresh < 60:
        return None
    akun._last_token_refresh = now

    # 3a) Coba TGLogin dulu kalau ada init_data: refresh initial_reward_token.
    if akun.init_data:
        if tg_login_refresh(akun):
            result = _try_get_task_progress_once(akun, task_id, list(dict.fromkeys([t for t in (akun.reward_token, akun.reward_user_token) if t])), profiles)
            if result is not None:
                return result

    # 3b) Re-login native (InitAppData + Login_Native + GameLaunch).
    log_step("WARN", "WATCH", akun.label(), "Token Refresh")
    if not inisialisasi_sesi(akun):
        return None

    # Pakai token baru
    tokens = list(dict.fromkeys([t for t in (akun.reward_token, akun.reward_user_token) if t]))
    return _try_get_task_progress_once(akun, task_id, tokens, profiles)


def jalankan_single_event(akun: AkunState, ad_type: str, event_type: str, skip_gameplay: bool = False) -> dict:
    st_time = int(time.time()) - random.randint(10, 20)
    if not skip_gameplay:
        kirim_request(
            akun,
            "https://www.hi5games.top/api/v1",
            "v1",
            "Game_SetItem",
            akun.game_token,
            {"item_key": "score", "item_value": "0", "device_type": "ANDROID", "app_type": "mo", "st": st_time, "_ssid": akun.sesi_ssid},
        )
        skor_acak = random.randint(20, 150)
        kirim_request(
            akun,
            "https://www.hi5games.top/api/v1",
            "v1",
            "Game_SubmitScore",
            akun.game_token,
            {"score": skor_acak, "device_type": "ANDROID", "app_type": "mo", "st": st_time, "_ssid": akun.sesi_ssid},
        )

    ad_id = "interstitial_lose" if event_type == "ad_interstitial" else "reward_Refresh"
    res_klik = kirim_request(
        akun,
        "https://www.hi5games.top/home/api",
        "home",
        "AdClick_Submit",
        akun.game_token,
        {"gameId": GAME_ID, "adType": ad_type, "adId": ad_id, "device_type": "ANDROID", "app_type": "mo", "st": st_time, "_ssid": akun.sesi_ssid},
    )

    if not res_klik or res_klik.get("status") != 200:
        return {"status": res_klik.get("status", 500), "reason": res_klik.get("error", "request_failed")}
    action_data = get_action_data(res_klik, "AdClick_Submit")
    reward_info = action_data.get("reward", {})
    reward_msg = ""
    if isinstance(reward_info, dict):
        reward_msg = str(reward_info.get("msg", "")).lower()
    if reward_msg in ("ip_cooldown", "cooldown"):
        return {"status": 429, "reason": reward_msg}
    if action_data and action_data.get("result") not in (None, 1, "1", True):
        return {"status": 400, "reason": "ad_rejected"}
    return {"status": 200}


def process_ads_for_task(akun: AkunState, task_type: str, num_ads: int, account_key: str) -> int:
    config = TASK_CONFIG[task_type]
    event_type = config["event_type"]
    ad_type = config["ad_type"]
    stats = akun.stats
    success_delta = 0
    token_ttl = TOKEN_COOLDOWN.get(task_type, 0)

    # Skip kalau token akun masih cooldown untuk task type ini.
    now = time.time()
    if akun.token_next_allowed.get(task_type, 0.0) > now:
        sisa = int(akun.token_next_allowed[task_type] - now)
        dprint(f"WATCH EVENT SKIP_TOKEN_CD akun={akun.label()} task={task_type} sisa={sisa}s")
        return 0

    for _ in range(num_ads):
        stats[task_type]["attempted"] += 1
        dprint(f"WATCH EVENT TRY akun={akun.label()} task={task_type} proxy={mask_proxy(akun.current_proxy)}")
        res = jalankan_single_event(akun, ad_type, event_type, skip_gameplay=SKIP_GAMEPLAY_SIMULATION)

        if res.get("status") == 200:
            stats[task_type]["success"] += 1
            success_delta += 1
            if token_ttl > 0:
                akun.token_next_allowed[task_type] = time.time() + token_ttl
            # Proxy yang sukses dipakai sekarang masuk cooldown IP.
            # Tandai blacklist dan rotasi ke proxy lain sebelum ad berikutnya.
            ads_proxy_manager.laporkan_cooldown(akun.current_proxy, task_type)
            proxy_next = ads_proxy_manager.ambil_proxy(account_key, task_type)
            if proxy_next:
                akun.update_proxy(proxy_next)
            dprint(f"WATCH EVENT OK akun={akun.label()} task={task_type} success={stats[task_type]['success']}")
            time.sleep(SLEEP_BETWEEN_ADS)
            continue

        if res.get("reason") not in ("ip_cooldown", "cooldown"):
            stats[task_type]["failed"] += 1
            stats[task_type]["last_error"] = res.get("reason", res.get("status", "unknown"))
            dprint(f"WATCH EVENT FAIL akun={akun.label()} task={task_type} reason={stats[task_type]['last_error']}")
            time.sleep(SLEEP_BETWEEN_ADS)
            continue

        # Cooldown: blacklist proxy untuk task type ini, plus token cooldown.
        if token_ttl > 0:
            akun.token_next_allowed[task_type] = time.time() + token_ttl

        swap_success = False
        for swap_attempt in range(1, MAX_PROXY_SWAP_PER_AD + 1):
            proxy_lama = akun.current_proxy
            dprint(
                f"WATCH EVENT COOLDOWN akun={akun.label()} task={task_type} "
                f"swap={swap_attempt}/{MAX_PROXY_SWAP_PER_AD} proxy={mask_proxy(proxy_lama)}"
            )
            proxy_baru = ads_proxy_manager.ambil_proxy_swap(account_key, proxy_lama, task_type)
            if not proxy_baru:
                ads_proxy_manager.laporkan_cooldown(proxy_lama, task_type)
                dprint(f"WATCH EVENT NO_PROXY_SWAP akun={akun.label()} task={task_type}")
                break

            akun.update_proxy(proxy_baru)
            log_step(
                "WARN",
                "WATCH",
                akun.label(),
                f"Cooldown {swap_attempt}/{MAX_PROXY_SWAP_PER_AD} | Next Proxy: {compact_proxy(proxy_baru)}",
            )
            res2 = jalankan_single_event(akun, ad_type, event_type, skip_gameplay=SKIP_GAMEPLAY_SIMULATION)

            if res2.get("status") == 200:
                stats[task_type]["success"] += 1
                success_delta += 1
                if token_ttl > 0:
                    akun.token_next_allowed[task_type] = time.time() + token_ttl
                # Proxy yang baru sukses cooldown IP juga.
                ads_proxy_manager.laporkan_cooldown(akun.current_proxy, task_type)
                proxy_next = ads_proxy_manager.ambil_proxy(account_key, task_type)
                if proxy_next:
                    akun.update_proxy(proxy_next)
                dprint(
                    f"WATCH EVENT OK_AFTER_SWAP akun={akun.label()} task={task_type} "
                    f"swap={swap_attempt} success={stats[task_type]['success']}"
                )
                swap_success = True
                break

            if res2.get("reason") in ("ip_cooldown", "cooldown"):
                ads_proxy_manager.laporkan_cooldown(proxy_baru, task_type)
                dprint(
                    f"WATCH EVENT COOLDOWN_AFTER_SWAP akun={akun.label()} task={task_type} "
                    f"swap={swap_attempt} proxy={mask_proxy(proxy_baru)}"
                )
                continue

            stats[task_type]["failed"] += 1
            stats[task_type]["last_error"] = res2.get("reason", res2.get("status", "unknown"))
            dprint(
                f"WATCH EVENT FAIL_AFTER_SWAP akun={akun.label()} task={task_type} "
                f"swap={swap_attempt} reason={stats[task_type]['last_error']}"
            )
            break
        else:
            stats[task_type]["cooldown"] += 1

        if not swap_success:
            # token cooldown sudah di-set di atas; keluar dari iterasi ad ini
            time.sleep(SLEEP_BETWEEN_ADS)
            # Tidak melanjutkan ad selanjutnya untuk task ini di round ini
            # karena token sudah cooldown (jika token_ttl > 0).
            if token_ttl > 0:
                break
            continue

        time.sleep(SLEEP_BETWEEN_ADS)

    return success_delta


def worker_akun(akun: AkunState, account_key: str, idx: int, total: int, after_round=None) -> bool:
    if not akun.valid or akun.done:
        return False

    printed_done = False
    for round_num in range(1, MAX_ROUNDS + 1):
        if akun.done:
            break
        new_proxy = ads_proxy_manager.ambil_proxy(account_key)
        akun.update_proxy(new_proxy)
        akun._round_count = round_num

        pr_awal = get_task_progress(akun, 29, account_key=account_key)
        pi_awal = get_task_progress(akun, 30, account_key=account_key)
        if task_selesai_hari_ini(pr_awal) and task_selesai_hari_ini(pi_awal):
            akun.done = True
            log_step("DONE", "WATCH", akun.label(), "Tasks Max")
            printed_done = True
            break

        vprint(f"[{jam_sekarang()}] WATCH      {akun.label()} | Round: {round_num} | Proxy: {mask_proxy(new_proxy)}")
        reward_delta = 0
        interstitial_delta = 0
        if task_selesai_hari_ini(pr_awal):
            vprint(f"[SKIP] {akun.label()} Watch sudah max")
        else:
            reward_delta = process_ads_for_task(akun, "reward", ADS_PER_ROUND, account_key)

        if task_selesai_hari_ini(pi_awal):
            vprint(f"[SKIP] {akun.label()} View sudah max")
        else:
            interstitial_delta = process_ads_for_task(akun, "interstitial", ADS_PER_ROUND, account_key)

        pr = get_task_progress(akun, 29, account_key=account_key)
        pi = get_task_progress(akun, 30, account_key=account_key)

        if after_round:
            after_round(round_num, pr, pi)

        if task_selesai_hari_ini(pr) and task_selesai_hari_ini(pi):
            akun.done = True
            log_step("DONE", "WATCH", akun.label(), f"Round: {round_num} | {akun.summary_stats()}")
            printed_done = True
            break

        if akun.should_print_summary():
            r_today, r_cycle = format_task_compact(pr)
            i_today, i_cycle = format_task_compact(pi)
            r_mark = green_positive(f"+{reward_delta}") if reward_delta else "0"
            i_mark = green_positive(f"+{interstitial_delta}") if interstitial_delta else "0"
            round_text = f"{round_num:>4}"
            r_today_p = pad_visible(r_today, 5)
            i_today_p = pad_visible(i_today, 5)
            r_cycle_p = pad_visible(r_cycle, 3)
            i_cycle_p = pad_visible(i_cycle, 3)
            r_mark_p = pad_visible(r_mark, 2)
            i_mark_p = pad_visible(i_mark, 2)
            log_step(
                "INFO",
                "PROGRESS",
                akun.label(),
                f"Round: {round_text} | Watch: {r_today_p} Cycle: {r_cycle_p} {r_mark_p} | View: {i_today_p} Cycle: {i_cycle_p} {i_mark_p}",
            )

        time.sleep(SLEEP_BETWEEN_ROUNDS)

    return printed_done


def run_watch_phase(accounts_raw: list[dict[str, Any]]) -> None:
    proxies = load_proxy_strings()
    ads_proxy_manager.load(proxies)
    ads_proxy_manager.mulai_round_baru(0)

    total = len(accounts_raw)
    garis(f"WATCH ADS | {total} akun | Mode log: {LOG_MODE.upper()} | Summary tiap {SUMMARY_INTERVAL}s")
    tprint(f"Akun: {total} | Proxy: {len(proxies)} | Start: {jam_sekarang()}")

    akun_list: list[AkunState] = []
    account_keys: list[str] = []
    for idx, acc in enumerate(accounts_raw):
        uid = acc["user_id"]
        account_key = f"{idx}:{uid}"
        proxy = ads_proxy_manager.ambil_proxy(account_key)
        akun_list.append(AkunState(acc, proxy))
        account_keys.append(account_key)

    def login_akun(args):
        akun, account_key, idx = args
        vprint(f"[{jam_sekarang()}] Login {idx + 1}/{total} ({akun.user_id})")
        if inisialisasi_sesi(akun):
            akun.valid = True
        return akun, account_key, idx

    failed = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(login_akun, (akun, account_keys[i], i)): i for i, akun in enumerate(akun_list)}
        for future in as_completed(futures):
            akun, account_key, idx = future.result()
            if not akun.valid:
                tprint(f"{Warna.KUNING}[RETRY] {akun.user_id} gagal login.{Warna.RESET}")
                failed.append((akun, account_key, idx))

    if failed:
        tprint(f"\n{Warna.KUNING}Retry {len(failed)} akun gagal...{Warna.RESET}")
        while failed:
            still_failed = []
            for akun, account_key, idx in failed:
                new_proxy = random.choice(proxies) if proxies else ""
                akun.update_proxy(new_proxy)
                if inisialisasi_sesi(akun):
                    akun.valid = True
                    log_step("OK", "WATCH", akun.user_id, "Login Retry")
                else:
                    still_failed.append((akun, account_key, idx))
                time.sleep(SLEEP_LOGIN_RETRY)
            failed = still_failed
            if failed:
                sisa = ", ".join(a.user_id for a, _, _ in failed)
                tprint(f"{Warna.KUNING}Masih {len(failed)} gagal: {sisa} - retry...{Warna.RESET}")
        tprint(f"{Warna.HIJAU}Semua akun berhasil login.{Warna.RESET}")

    valid_count = sum(1 for a in akun_list if a.valid)
    tprint(f"\n{Warna.BIRU}Mulai watch ads: {valid_count}/{total} akun valid{Warna.RESET}")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(worker_akun, akun, account_keys[i], i, total) for i, akun in enumerate(akun_list) if akun.valid]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                tprint(f"{Warna.MERAH}[ERROR] Thread error: {exc}{Warna.RESET}")

    garis("RINGKASAN WATCH ADS")
    for akun in akun_list:
        if not akun.valid:
            tprint(f"  {akun.label()}: login gagal")
            continue
        r = akun.stats["reward"]
        i = akun.stats["interstitial"]
        tprint(
            f"  {akun.label()} | Round {akun._round_count} | "
            f"Reward ok={r['success']} cd={r['cooldown']} err={r['failed']} | "
            f"Intrstl ok={i['success']} cd={i['cooldown']} err={i['failed']}"
        )
    tprint(f"\n{Warna.HIJAU}Watch ads selesai. {jam_sekarang()}{Warna.RESET}")


def run_watch_for_account(
    account: dict[str, Any],
    idx: int,
    total: int,
    reward_login: dict[str, Any] | None = None,
) -> None:
    account_key = f"{idx - 1}:{account['user_id']}"
    proxy = ads_proxy_manager.ambil_proxy(account_key)
    akun = AkunState(account, proxy)

    vprint(f"[{jam_sekarang()}] Login watch {idx}/{total} ({akun.user_id})")
    retry_count = 0
    while not akun.valid:
        if inisialisasi_sesi(akun):
            akun.valid = True
            break

        retry_count += 1
        # Hanya log saat retry tertentu agar tidak bising di pool besar.
        if retry_count == 1 or retry_count % 5 == 0:
            log_step("WARN", "WATCH", akun.user_id, f"Login Retry {retry_count}")
        new_proxy = ads_proxy_manager.ambil_proxy(account_key)
        akun.update_proxy(new_proxy)
        time.sleep(SLEEP_LOGIN_RETRY)

    def claim_after_round(round_num, pr, pi):
        if not reward_login:
            return
        reward_claim_available(
            idx,
            total,
            reward_login["token"],
            reward_login["username"],
            reward_login["proxy"],
            quiet_no_claim=True,
            reward_login=reward_login,
        )

    printed_done = worker_akun(akun, account_key, idx - 1, total, after_round=claim_after_round)

    if not printed_done:
        r = akun.stats["reward"]
        i = akun.stats["interstitial"]
        log_step(
            "DONE",
            "WATCH",
            akun.label(),
            f"Round: {akun._round_count} | Watch OK: {r['success']} CD: {r['cooldown']} Err: {r['failed']} | "
            f"Interstitial OK: {i['success']} CD: {i['cooldown']} Err: {i['failed']}",
        )


def run_account_flow(idx: int, total: int, account: dict[str, Any], claim_proxies: list[dict[str, Any]], start_delay: float = 0.0) -> None:
    if start_delay > 0:
        time.sleep(start_delay)
    label = account.get("display_name") or account.get("user_id") or f"akun-{idx}"

    reward_login = reward_login_account(idx, total, account, claim_proxies)
    if reward_login:
        token = reward_login["token"]
        username = reward_login["username"]
        balance = reward_login["balance"]
        proxy_used = reward_login["proxy"]
        account["log_label"] = format_account_label(idx, total, username, float(balance))
        log_step("START", "ACCOUNT", account["log_label"])

        rc = reward_call_api("GetCheckinStatus", token, proxy=proxy_used)
        if rc.get("status") == 200:
            cd = rc.get("data") or {}
            if cd.get("code") == 0 and not cd.get("signed_today"):
                rd = reward_call_api("DoCheckin", token, proxy=proxy_used)
                if rd.get("status") == 200 and (rd.get("data") or {}).get("code") == 0:
                    pts = (rd.get("data") or {}).get("points_earned", 0)
                    log_step("OK", "CHECKIN", account["log_label"], green_positive(f"+{pts} pts"))

        rs = reward_call_api("GetFreespinInfo", token, proxy=proxy_used)
        if rs.get("status") == 200:
            sd = rs.get("data") or {}
            remaining = int(sd.get("remaining", 0) or 0)
            if remaining > 0:
                spin_ok = 0
                for _ in range(remaining):
                    rd = reward_call_api("DoFreespin", token, proxy=proxy_used)
                    if rd.get("status") == 200 and (rd.get("data") or {}).get("code") == 0:
                        spin_ok += 1
                    time.sleep(DELAY_CLAIM)
                log_step("OK", "SPIN", account["log_label"], f"{spin_ok}/{remaining}")
    else:
        account["log_label"] = format_account_label(idx, total, label)
        log_step("START", "ACCOUNT", account["log_label"], "Reward Login Failed")

    run_watch_for_account(account, idx, total, reward_login=reward_login)

    if reward_login:
        reward_claim_available(
            idx,
            total,
            reward_login["token"],
            reward_login["username"],
            reward_login["proxy"],
            quiet_no_claim=False,
            reward_login=reward_login,
        )

    final_label = account.get("log_label") or label
    log_step("DONE", "ACCOUNT", final_label)


# ============================================================
# Main flow
# ============================================================


def main() -> int:
    start_restart_watchdog()
    accounts = load_accounts()
    if not accounts:
        print(f"{Warna.MERAH}File akun.txt tidak ditemukan atau format salah.{Warna.RESET}")
        print("Format: user_id|native_user_id|native_user_pwd|initial_reward_token|nama_opsional")
        return 1

    ads_proxies = load_proxy_strings()
    claim_proxies = load_claim_proxies()
    ads_proxy_manager.load(ads_proxies)
    ads_proxy_manager.mulai_round_baru(0)

    total = len(accounts)
    garis("WEWADS RUNNER")
    log_step(
        "INFO",
        "CONFIG",
        "",
        f"Accounts: {total} | Workers: {MAX_WORKERS} | Watch Proxies: {len(ads_proxies)} (unique IP: {ads_proxy_manager.unique_ips}) | Reward Proxies: {len(claim_proxies)} | Log: {LOG_MODE.upper()} | Debug: {DEBUG_HTTP}",
    )

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(run_account_flow, idx, total, account, claim_proxies, (idx - 1) * STAGGER_PER_ACCOUNT_SECONDS): idx
            for idx, account in enumerate(accounts, 1)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                future.result()
            except KeyboardInterrupt:
                stop_sekarang()
            except Exception as exc:
                tprint(f"{Warna.MERAH}[ERROR] Akun ke-{idx} exception: {exc}{Warna.RESET}")

    tprint(f"\n{Warna.HIJAU}Semua akun selesai. {jam_sekarang()}{Warna.RESET}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        stop_sekarang()

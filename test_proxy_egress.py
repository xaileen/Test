#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from github_sync import sync_file_to_github


BASE_DIR = Path(__file__).resolve().parent
PROXY_FILE = BASE_DIR / os.environ.get("PROXY_FILE", "proxy.txt")
ALIVE_FILE = BASE_DIR / os.environ.get("PROXY_ALIVE_FILE", "proxy_alive.txt")

DEFAULT_PROXY_SCHEME = os.environ.get("DEFAULT_PROXY_SCHEME", "socks5")
TEST_URL = os.environ.get("PROXY_TEST_URL", "https://api.ipify.org?format=json")
TARGET_URL = os.environ.get("PROXY_TEST_TARGET_URL", "https://www.hi5games.top/")
TIMEOUT = float(os.environ.get("PROXY_TEST_TIMEOUT", "6"))
WORKERS = int(os.environ.get("PROXY_TEST_WORKERS", "200"))
LIMIT = int(os.environ.get("PROXY_TEST_LIMIT", "0"))
PRINT_ALIVE_FULL = os.environ.get("PRINT_ALIVE_FULL", "").lower() in ("1", "true", "yes", "y")
FAIL_IF_NONE = os.environ.get("FAIL_IF_NO_PROXY", "").lower() in ("1", "true", "yes", "y")
LOG_OK_SAMPLES = int(os.environ.get("PROXY_TEST_LOG_OK_SAMPLES", "10"))
LOG_FAIL_SAMPLES = int(os.environ.get("PROXY_TEST_LOG_FAIL_SAMPLES", "5"))
LOG_EVERY = int(os.environ.get("PROXY_TEST_LOG_EVERY", "250"))
SAVE_TOP_N = int(os.environ.get("PROXY_TEST_SAVE_TOP_N", "0"))
PRINT_ALIVE_BLOCK = os.environ.get("PRINT_ALIVE_BLOCK", "").lower() in ("1", "true", "yes", "y")
PRINT_ALIVE_BLOCK_LIMIT = int(os.environ.get("PRINT_ALIVE_BLOCK_LIMIT", "200"))
SERVE_ALIVE_FILE = os.environ.get("SERVE_ALIVE_FILE", "").lower() in ("1", "true", "yes", "y")
SERVE_HOST = os.environ.get("HOST", "0.0.0.0")
SERVE_PORT = int(os.environ.get("PORT", "8080"))


def normalize_proxy_line(line: str) -> str:
    s = line.strip()
    if not s or s.startswith("#"):
        return ""

    lowered = s.lower()
    for scheme in ("socks5://", "socks4://", "http://", "https://"):
        if lowered.startswith(scheme):
            return s

    if "@" in s:
        return f"{DEFAULT_PROXY_SCHEME}://{s}"

    parts = s.split(":")
    if len(parts) == 2:
        host, port = parts
        return f"{DEFAULT_PROXY_SCHEME}://{host}:{port}"
    if len(parts) == 4:
        host, port, user, passwd = parts
        return f"{DEFAULT_PROXY_SCHEME}://{user}:{passwd}@{host}:{port}"
    return f"http://{s}"


def mask_proxy(proxy: str) -> str:
    parsed = urlsplit(proxy)
    if "@" not in parsed.netloc:
        return proxy

    host_part = parsed.netloc.rsplit("@", 1)[1]
    return urlunsplit((parsed.scheme, f"****:****@{host_part}", parsed.path, parsed.query, parsed.fragment))


def load_proxies() -> list[str]:
    if not PROXY_FILE.exists():
        return []

    proxies = []
    seen = set()
    for raw in PROXY_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        proxy = normalize_proxy_line(raw)
        if proxy and proxy not in seen:
            proxies.append(proxy)
            seen.add(proxy)
    if LIMIT > 0:
        return proxies[:LIMIT]
    return proxies


def short_error(exc: Exception) -> str:
    msg = str(exc).replace("\r", " ").replace("\n", " ")
    return msg[:180]


def request_via_proxy(url: str, proxy: str) -> requests.Response:
    return requests.get(
        url,
        proxies={"http": proxy, "https": proxy},
        timeout=TIMEOUT,
        headers={"User-Agent": "proxy-egress-check/1.0"},
    )


def check_proxy(proxy: str) -> dict[str, object]:
    started = time.perf_counter()
    try:
        ip_resp = request_via_proxy(TEST_URL, proxy)
        ip_resp.raise_for_status()
        ip_text = ip_resp.text.strip()

        target_resp = request_via_proxy(TARGET_URL, proxy)
        elapsed = time.perf_counter() - started
        return {
            "ok": True,
            "proxy": proxy,
            "masked": mask_proxy(proxy),
            "ip": ip_text[:80],
            "target_status": target_resp.status_code,
            "elapsed": elapsed,
            "error": "",
        }
    except Exception as exc:
        elapsed = time.perf_counter() - started
        return {
            "ok": False,
            "proxy": proxy,
            "masked": mask_proxy(proxy),
            "ip": "",
            "target_status": "",
            "elapsed": elapsed,
            "error": short_error(exc),
        }


def print_direct_egress() -> None:
    print("== Direct cloud egress ==", flush=True)
    try:
        resp = requests.get(TEST_URL, timeout=TIMEOUT, headers={"User-Agent": "proxy-egress-check/1.0"})
        print(f"direct ok | status={resp.status_code} | body={resp.text.strip()[:100]}", flush=True)
    except Exception as exc:
        print(f"direct fail | {short_error(exc)}", flush=True)


def serve_alive_file(alive_count: int, failed_count: int, saved_count: int) -> None:
    class AliveFileHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def send_text(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                body = (
                    "<!doctype html><html><head><meta charset='utf-8'><title>proxy_alive.txt</title></head>"
                    "<body>"
                    "<h1>Proxy test selesai</h1>"
                    f"<p>alive={alive_count} failed={failed_count} saved={saved_count}</p>"
                    "<p><a href='/proxy_alive.txt' download>Download proxy_alive.txt</a></p>"
                    "</body></html>"
                )
                self.send_text(200, body, "text/html; charset=utf-8")
                return

            if self.path == "/proxy_alive.txt":
                if not ALIVE_FILE.exists():
                    self.send_text(404, "proxy_alive.txt belum ada\n")
                    return
                data = ALIVE_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="proxy_alive.txt"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if self.path == "/summary.txt":
                self.send_text(200, f"alive={alive_count}\nfailed={failed_count}\nsaved={saved_count}\nfile={ALIVE_FILE.name}\n")
                return

            self.send_text(404, "not found\n")

    print(f"\n== Download server ==", flush=True)
    print(f"Serving {ALIVE_FILE.name} on 0.0.0.0:{SERVE_PORT}", flush=True)
    print("Open Railway public URL, then download /proxy_alive.txt", flush=True)
    ThreadingHTTPServer((SERVE_HOST, SERVE_PORT), AliveFileHandler).serve_forever()


def main() -> int:
    proxies = load_proxies()
    print_direct_egress()
    print(f"\n== Proxy test ==", flush=True)
    print(f"file={PROXY_FILE.name} | total={len(proxies)} | workers={WORKERS} | timeout={TIMEOUT}s", flush=True)
    print(f"ip_url={TEST_URL}", flush=True)
    print(f"target_url={TARGET_URL}", flush=True)
    print(
        f"log=first {LOG_OK_SAMPLES} OK, first {LOG_FAIL_SAMPLES} FAIL, progress every {LOG_EVERY} checked",
        flush=True,
    )

    if not proxies:
        ALIVE_FILE.write_text("", encoding="utf-8")
        print("No proxies to test.", flush=True)
        return 1 if FAIL_IF_NONE else 0

    alive: list[dict[str, object]] = []
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, min(WORKERS, len(proxies)))) as pool:
        futures = [pool.submit(check_proxy, proxy) for proxy in proxies]
        for index, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result["ok"]:
                alive.append(result)
                if len(alive) <= LOG_OK_SAMPLES:
                    shown_proxy = str(result["proxy"]) if PRINT_ALIVE_FULL else str(result["masked"])
                    print(
                        f"[OK {len(alive):03d}/{index:03d}] {shown_proxy} | ip={result['ip']} "
                        f"| target={result['target_status']} | {float(result['elapsed']):.2f}s",
                        flush=True,
                    )
            else:
                failed += 1
                if failed <= LOG_FAIL_SAMPLES:
                    print(
                        f"[FAIL {failed:03d}/{index:03d}] {result['masked']} | {result['error']} "
                        f"| {float(result['elapsed']):.2f}s",
                        flush=True,
                    )
            if LOG_EVERY > 0 and index % LOG_EVERY == 0:
                print(f"[PROGRESS] checked={index}/{len(proxies)} | alive={len(alive)} | failed={failed}", flush=True)

    alive_sorted = sorted(alive, key=lambda item: float(item["elapsed"]))
    saved_alive = alive_sorted[:SAVE_TOP_N] if SAVE_TOP_N > 0 else alive_sorted
    alive_lines = [str(item["proxy"]) for item in saved_alive]
    ALIVE_FILE.write_text("\n".join(alive_lines) + ("\n" if alive_lines else ""), encoding="utf-8")
    sync_file_to_github(
        ALIVE_FILE,
        "proxy_alive.txt",
        message=f"Update proxy_alive.txt from Railway proxy test ({len(alive_lines)} alive)",
    )
    print("\n== Summary ==", flush=True)
    print(f"alive={len(alive)} | failed={failed} | saved={len(alive_lines)} | written={ALIVE_FILE.name}", flush=True)
    if SAVE_TOP_N > 0:
        print(f"saved only fastest {SAVE_TOP_N} alive proxies by elapsed time", flush=True)
    if alive and not PRINT_ALIVE_FULL:
        print("Proxy credentials are masked in logs. Set PRINT_ALIVE_FULL=1 only if you really want full alive lines in Railway logs.", flush=True)
    if PRINT_ALIVE_BLOCK:
        block_lines = alive_lines[:PRINT_ALIVE_BLOCK_LIMIT]
        print(f"\n=== PROXY_ALIVE_BEGIN total={len(alive_lines)} printed={len(block_lines)} ===", flush=True)
        for line in block_lines:
            print(line, flush=True)
        print("=== PROXY_ALIVE_END ===", flush=True)
    if SERVE_ALIVE_FILE:
        serve_alive_file(len(alive), failed, len(alive_lines))
    return 1 if FAIL_IF_NONE and not alive else 0


if __name__ == "__main__":
    raise SystemExit(main())

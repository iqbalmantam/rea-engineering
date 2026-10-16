"""Pendukung mode APK dan Website untuk REA Web UI (bebas Streamlit, bisa diuji).

- APK: menyiapkan JAR jadx-headless-mcp (diunduh sekali, diverifikasi SHA-256
  sesuai metadata REA) dan memeriksa JDK.
- Website: memvalidasi URL (anti-SSRF dasar), meluncurkan Chromium headless
  dengan CDP hanya di loopback, dan mencari ID target halaman.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

# ---------------------------------------------------------------- JADX / APK
JADX_VERSION = "0.7.1"
JADX_JAR_NAME = f"jadx-headless-mcp-{JADX_VERSION}-all.jar"
JADX_URL = (
    "https://github.com/1013503897/jadx-headless-mcp/releases/download/"
    f"v{JADX_VERSION}/{JADX_JAR_NAME}"
)
# Digest yang dipin oleh REA (src/android/JadxRelease.ts, docs/android-analysis.md).
JADX_SHA256 = "6e5eacf500b64292bfb73c49797c1958f6ee44646e43e868039ae7feb573ff75"
JADX_MAX_BYTES = 300 * 1024 * 1024


class SetupError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_jadx_jar(
    cache_dir: Path | None = None,
    *,
    url: str = JADX_URL,
    expected_sha256: str = JADX_SHA256,
    timeout: int = 300,
) -> Path:
    """Kembalikan path JAR yang sudah terverifikasi; unduh bila belum ada."""
    cache = cache_dir or Path(tempfile.gettempdir()) / "rea_jadx"
    cache.mkdir(parents=True, exist_ok=True)
    jar = cache / JADX_JAR_NAME
    if jar.is_file() and sha256_file(jar) == expected_sha256:
        return jar
    part = cache / (JADX_JAR_NAME + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rea-web-ui"})
        total = 0
        with urllib.request.urlopen(req, timeout=timeout) as resp, part.open("wb") as out:  # noqa: S310
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > JADX_MAX_BYTES:
                    raise SetupError("Unduhan JADX melebihi batas ukuran.")
                out.write(chunk)
    except (urllib.error.URLError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise SetupError(f"Gagal mengunduh JADX: {exc}") from exc
    if sha256_file(part) != expected_sha256:
        part.unlink(missing_ok=True)
        raise SetupError("SHA-256 JADX tidak cocok dengan metadata REA; berkas dihapus.")
    part.replace(jar)
    return jar


def find_java() -> str | None:
    home = os.environ.get("JAVA_HOME", "").strip()
    if home:
        cand = Path(home) / "bin" / "java"
        if cand.is_file():
            return str(cand)
    return shutil.which("java")


def java_version(java: str | None = None) -> str | None:
    java = java or find_java()
    if not java:
        return None
    try:
        out = subprocess.run(
            [java, "-version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = (out.stderr or out.stdout).strip().splitlines()
    return first[0] if first else None


def android_env(jar: Path) -> dict[str, str]:
    """Variabel lingkungan tambahan untuk perintah Android REA."""
    env = {"REA_JADX_MCP_JAR": str(jar)}
    java = find_java()
    if java and not os.environ.get("JAVA_HOME"):
        # .../jdk/bin/java -> .../jdk (resolusi symlink agar JAVA_HOME valid)
        env["JAVA_HOME"] = str(Path(java).resolve().parent.parent)
    return env


# ---------------------------------------------------------------- URL aman
ALLOWED_PORTS = {80, 443, 8080, 8443}
Resolver = Callable[[str, int], list[str]]


def _default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


class UnsafeUrl(ValueError):
    pass


def validate_public_url(url: str, resolver: Resolver = _default_resolver) -> str:
    """Terima hanya http(s) ke alamat IP publik. Kembalikan URL ternormalisasi.

    Mencegah app dipakai untuk menjangkau jaringan internal (SSRF dasar).
    Catatan: ini tidak menghentikan redirect ke alamat internal setelah halaman
    dimuat; batasan itu didokumentasikan di README.
    """
    url = (url or "").strip()
    if not url:
        raise UnsafeUrl("URL kosong.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl("Hanya http:// atau https:// yang diizinkan.")
    if not parts.hostname:
        raise UnsafeUrl("URL tidak memiliki host.")
    if parts.username or parts.password:
        raise UnsafeUrl("URL dengan username/password tidak diizinkan.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeUrl("Port tidak valid.") from exc
    if port not in ALLOWED_PORTS:
        raise UnsafeUrl(f"Port {port} tidak diizinkan (boleh: {sorted(ALLOWED_PORTS)}).")
    host = parts.hostname
    try:
        addrs = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addrs = resolver(host, port)
        except OSError as exc:
            raise UnsafeUrl(f"Host tidak dapat di-resolve: {exc}") from exc
    if not addrs:
        raise UnsafeUrl("Host tidak menghasilkan alamat IP.")
    for addr in addrs:
        ip = ipaddress.ip_address(addr)
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise UnsafeUrl(f"Host mengarah ke alamat non-publik ({addr}); ditolak.")
    return url


# ---------------------------------------------------------------- Chromium
_CHROMIUM_NAMES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome")


def find_chromium() -> str | None:
    override = os.environ.get("REA_BROWSER_EXECUTABLE", "").strip()
    if override and Path(override).is_file():
        return override
    for name in _CHROMIUM_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def chromium_args(exe: str, port: int, profile: Path, url: str) -> list[str]:
    return [
        exe,
        "--headless=new",
        "--no-sandbox",  # kontainer Streamlit Cloud tidak mengizinkan sandbox
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        url,
    ]


def _http_json(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - loopback saja
        return json.loads(resp.read().decode("utf-8", "replace"))


def pick_page_target(targets: list[dict]) -> dict | None:
    """Pilih target halaman terbaik dari daftar /json/list."""
    pages = [t for t in targets if t.get("type") == "page" and t.get("id")]
    real = [t for t in pages if not str(t.get("url", "")).startswith(("about:", "chrome:", "devtools:"))]
    return (real or pages or [None])[0]


@dataclass
class BrowserSession:
    process: subprocess.Popen
    port: int
    profile: Path
    target_id: str
    target_url: str
    target_title: str

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    self.process.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


def open_page(
    url: str,
    *,
    exe: str | None = None,
    settle_seconds: float = 6.0,
    start_timeout: float = 30.0,
    load_timeout: float = 45.0,
) -> BrowserSession:
    """Luncurkan Chromium headless ke `url`, tunggu halaman dimuat, kembalikan sesi."""
    exe = exe or find_chromium()
    if not exe:
        raise SetupError("Chromium tidak ditemukan. Tambahkan `chromium` ke packages.txt.")
    port = pick_free_port()
    profile = Path(tempfile.mkdtemp(prefix="rea_chrome_"))
    proc = subprocess.Popen(  # noqa: S603 - tanpa shell
        chromium_args(exe, port, profile, url),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    session = BrowserSession(proc, port, profile, "", "", "")
    try:
        deadline = time.monotonic() + start_timeout
        while True:
            if proc.poll() is not None:
                raise SetupError(f"Chromium berhenti saat start (exit {proc.returncode}).")
            try:
                _http_json(f"{session.endpoint}/json/version")
                break
            except (urllib.error.URLError, OSError, ValueError):
                if time.monotonic() > deadline:
                    raise SetupError("Chromium tidak membuka port CDP tepat waktu.") from None
                time.sleep(0.3)
        deadline = time.monotonic() + load_timeout
        target = None
        while time.monotonic() < deadline:
            try:
                target = pick_page_target(_http_json(f"{session.endpoint}/json/list"))
            except (urllib.error.URLError, OSError, ValueError):
                target = None
            if target and not str(target.get("url", "")).startswith("about:"):
                break
            time.sleep(0.5)
        if not target:
            raise SetupError("Halaman tidak muncul sebagai target CDP.")
        time.sleep(settle_seconds)  # beri waktu skrip dan jaringan selesai
        # Ambil ulang: URL/judul bisa berubah setelah redirect.
        try:
            refreshed = pick_page_target(_http_json(f"{session.endpoint}/json/list"))
            target = refreshed or target
        except (urllib.error.URLError, OSError, ValueError):
            pass
        session.target_id = str(target["id"])
        session.target_url = str(target.get("url", ""))
        session.target_title = str(target.get("title", ""))
        return session
    except BaseException:
        session.close()
        raise

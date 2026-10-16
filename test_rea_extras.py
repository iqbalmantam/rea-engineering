"""Uji rea_extras dan pembangun argumen baru. Tanpa jaringan. Jalankan: python test_rea_extras.py"""

import hashlib
import socket
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import rea_extras as rx
import rea_runner as rr

FAKE_CHROMIUM = """#!{python}
import http.server, json, sys, threading, time
args = sys.argv[1:]
port = int([a for a in args if a.startswith("--remote-debugging-port=")][0].split("=")[1])
url = [a for a in args if a.startswith("http")][0]
born = time.time()
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/json/version":
            body = {{"Browser": "FakeChrome/1"}}
        elif self.path == "/json/list":
            # 1 dtk pertama masih about:blank, lalu halaman sebenarnya
            real = time.time() - born > 1.0
            body = [{{"id": "WORKER", "type": "service_worker", "url": url}},
                    {{"id": "PAGE123", "type": "page",
                      "url": url if real else "about:blank", "title": "Fake Page"}}]
        else:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rea_extras_test_"))

    # 1. Validasi URL
    public = lambda host, port: ["93.184.216.34"]  # noqa: E731
    assert rx.validate_public_url("https://example.com/path?q=1", public) == "https://example.com/path?q=1"
    bad = {
        "http://localhost/": lambda h, p: ["127.0.0.1"],
        "http://127.0.0.1/": public,
        "http://10.0.0.5/": public,
        "http://192.168.1.1:8080/": public,
        "http://169.254.169.254/latest/meta-data": public,
        "http://[::1]/": public,
        "http://[::ffff:127.0.0.1]/": public,
        "file:///etc/passwd": public,
        "ftp://example.com/": public,
        "https://user:pw@example.com/": public,
        "https://example.com:22/": public,
        "https://example.com:99999/": public,
        "": public,
        "https:///nohost": public,
        "https://internal.example/": lambda h, p: ["93.184.216.34", "10.1.2.3"],  # campuran
        "https://nx.example/": lambda h, p: (_ for _ in ()).throw(OSError("NXDOMAIN")),
        "https://empty.example/": lambda h, p: [],
    }
    for u, resolver in bad.items():
        try:
            rx.validate_public_url(u, resolver)
        except rx.UnsafeUrl:
            continue
        raise AssertionError(f"URL lolos padahal tidak aman: {u!r}")
    assert rx.validate_public_url("http://93.184.216.34:8080/") == "http://93.184.216.34:8080/"

    # 2. Unduh + verifikasi JADX (pakai file:// sebagai sumber)
    src = tmp / "src.jar"
    src.write_bytes(b"PK-fake-jar-content")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    cache = tmp / "cache"
    jar = rx.ensure_jadx_jar(cache, url=src.as_uri(), expected_sha256=digest)
    assert jar.read_bytes() == b"PK-fake-jar-content" and jar.name == rx.JADX_JAR_NAME
    src.unlink()  # bukti: panggilan kedua memakai cache, tanpa unduh
    assert rx.ensure_jadx_jar(cache, url=src.as_uri(), expected_sha256=digest) == jar
    bad_cache = tmp / "bad"
    src.write_bytes(b"tampered")
    try:
        rx.ensure_jadx_jar(bad_cache, url=src.as_uri(), expected_sha256=digest)
        raise AssertionError("digest salah lolos")
    except rx.SetupError:
        assert not list(bad_cache.glob("*.jar")) and not list(bad_cache.glob("*.part"))
    try:
        rx.ensure_jadx_jar(tmp / "none", url=(tmp / "missing.jar").as_uri(), expected_sha256=digest)
        raise AssertionError("sumber hilang lolos")
    except rx.SetupError:
        pass
    assert rx.JADX_SHA256 == "6e5eacf500b64292bfb73c49797c1958f6ee44646e43e868039ae7feb573ff75"
    env = rx.android_env(jar)
    assert env["REA_JADX_MCP_JAR"] == str(jar)

    # 3. Pemilihan target halaman
    t = rx.pick_page_target
    assert t([]) is None
    assert t([{"id": "w", "type": "service_worker", "url": "https://a"}]) is None
    assert t([{"id": "a", "type": "page", "url": "about:blank"}])["id"] == "a"
    assert t([{"id": "a", "type": "page", "url": "about:blank"},
              {"id": "b", "type": "page", "url": "https://x.test"}])["id"] == "b"

    # 4. Argumen Chromium: CDP hanya di loopback
    a = rx.chromium_args("/bin/chromium", 9333, Path("/p"), "https://x.test")
    assert "--remote-debugging-address=127.0.0.1" in a and "--remote-debugging-port=9333" in a
    assert a[-1] == "https://x.test" and "--headless=new" in a

    # 5. Peluncuran Chromium tiruan end-to-end
    fake = tmp / "fake_chromium"
    fake.write_text(FAKE_CHROMIUM.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    t0 = time.monotonic()
    sess = rx.open_page("https://example.test/", exe=str(fake), settle_seconds=0.2)
    try:
        assert sess.target_id == "PAGE123", sess.target_id
        assert sess.target_url == "https://example.test/" and sess.target_title == "Fake Page"
        assert sess.endpoint.startswith("http://127.0.0.1:")
        assert port_open(sess.port)
        assert time.monotonic() - t0 >= 1.0  # menunggu halaman benar-benar dimuat
    finally:
        sess.close()
    time.sleep(0.5)
    assert not port_open(sess.port) and not sess.profile.exists()

    crash = tmp / "crash_chromium"
    crash.write_text("#!/bin/sh\nexit 3\n")
    crash.chmod(0o755)
    try:
        rx.open_page("https://example.test/", exe=str(crash), settle_seconds=0)
        raise AssertionError("Chromium crash tidak terdeteksi")
    except rx.SetupError as exc:
        assert "exit 3" in str(exc)
    try:
        rx.open_page("https://example.test/", exe=None) if rx.find_chromium() is None else (_ for _ in ()).throw(rx.SetupError("skip"))
    except rx.SetupError:
        pass

    # 6. Pembangun argumen CLI
    assert rr.apk_args("package", Path("/a.apk")) == ["inspect-android-package", "/a.apk", "--json"]
    assert rr.apk_args("search", Path("/a.apk"), query="Main") == ["search-android-classes", "/a.apk", "Main", "--json"]
    assert rr.apk_args("class", Path("/a.apk"), class_name="x.Main") == ["inspect-android-class", "/a.apk", "x.Main", "--json"]
    assert rr.apk_args("method", Path("/a.apk"), class_name="x.Main", method_name="onCreate", overload_index=1) == [
        "inspect-android-method", "/a.apk", "x.Main", "onCreate", "--json", "--overload-index", "1"]
    assert "--overload-index" not in rr.apk_args("method", Path("/a.apk"), class_name="x.M", method_name="m")
    w = rr.web_args("inspect", "http://127.0.0.1:9", "ID1", observation_ms=2000, include_script_sources=True)
    assert w == ["inspect-web-page", "http://127.0.0.1:9", "ID1", "--json", "--observation-ms", "2000",
                 "--include-script-sources"], w
    wb = rr.web_args("bundle", "http://127.0.0.1:9", "ID1", observation_ms=1000, fetch_source_maps=True)
    assert wb[-1] == "--fetch-source-maps" and wb[0] == "analyze-web-bundle"
    assert rr.MODES["apk"].types == ["apk"] and rr.MODES["web"].accepts == "url"

    # 7. extra_env diteruskan ke subproses
    script = tmp / "printenv.py"
    script.write_text("import os; print(os.environ.get('REA_JADX_MCP_JAR', 'KOSONG'))")
    r = rr.run_cli([sys.executable], [str(script)], tmp, out_name="e.txt",
                   extra_env={"REA_JADX_MCP_JAR": "/x/y.jar"})
    assert r.stdout_path.read_text().strip() == "/x/y.jar"

    rr.cleanup(tmp)
    print("SEMUA UJI REA_EXTRAS LULUS")


if __name__ == "__main__":
    main()

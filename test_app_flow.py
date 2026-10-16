"""Uji alur app.py dengan Streamlit, CLI REA, dan Chromium tiruan (tanpa jaringan).

Jalankan: python test_app_flow.py
Ini menjalankan kode app.py sungguhan; yang ditiru hanya antarmuka Streamlit.
"""

import contextlib
import json
import runpy
import stat
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import rea_extras as rx  # noqa: E402
import rea_runner as rr  # noqa: E402
from test_rea_extras import FAKE_CHROMIUM  # noqa: E402


class StopApp(Exception):
    pass


class Upload:
    def __init__(self, name, data):
        self.name, self._data = name, data

    def getvalue(self):
        return self._data


class FakeSt(types.ModuleType):
    """Antarmuka Streamlit minimal yang mencatat pemanggilan."""

    def __init__(self):
        super().__init__("streamlit")
        self.session_state = {}
        self.mode = "js"
        self.clicked: set[str] = set()
        self.texts: dict[str, str] = {}
        self.checks: dict[str, bool] = {}
        self.upload = None
        self.errors: list[str] = []
        self.jsons: list = []
        self.sidebar = self

    # konteks
    @contextlib.contextmanager
    def _ctx(self, *a, **k):
        yield self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    expander = spinner = _ctx

    # widget
    def selectbox(self, label, options, **kw):
        return self.mode

    def button(self, label, **kw):
        return label in self.clicked and not kw.get("disabled", False)

    def text_input(self, label, **kw):
        return self.texts.get(label, "")

    def checkbox(self, label, **kw):
        return self.checks.get(label, False)

    def file_uploader(self, *a, **kw):
        return self.upload

    def slider(self, label, mn, mx, value=None, **kw):
        return mn if value is None else value

    def number_input(self, label, mn=0, mx=0, value=0, **kw):
        return value

    def radio(self, label, options, **kw):
        return options[0]

    def columns(self, n):
        return [self] * (n if isinstance(n, int) else len(n))

    # keluaran
    def error(self, msg, *a, **k):
        self.errors.append(str(msg))

    def json(self, data, **k):
        self.jsons.append(data)

    def stop(self):
        raise StopApp

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


FAKE_REA = """#!{python}
import json, os, sys
print(json.dumps({{"argv": sys.argv[1:], "jar": os.environ.get("REA_JADX_MCP_JAR")}}))
"""


def run_app(st: FakeSt) -> None:
    sys.modules["streamlit"] = st
    try:
        runpy.run_path(str(HERE / "app.py"), run_name="__main__")
    except StopApp:
        pass


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rea_flow_"))
    fake_rea = tmp / "fake_rea.py"
    fake_rea.write_text(FAKE_REA.format(python=sys.executable))
    fake_rea.chmod(fake_rea.stat().st_mode | stat.S_IXUSR)
    fake_chrome = tmp / "fake_chromium"
    fake_chrome.write_text(FAKE_CHROMIUM.format(python=sys.executable))
    fake_chrome.chmod(fake_chrome.stat().st_mode | stat.S_IXUSR)

    import os

    os.environ["REA_COMMAND"] = f"{sys.executable} {fake_rea}"
    os.environ["REA_BROWSER_EXECUTABLE"] = str(fake_chrome)
    rx.validate_public_url = lambda u, *a, **k: u  # tanpa DNS di sandbox ini

    # 1. Semua mode tampil tanpa error saat tidak ada tombol yang ditekan
    for mode in rr.MODES:
        st = FakeSt()
        st.mode = mode
        run_app(st)
        assert not st.errors or mode == "web", (mode, st.errors)

    # 2. Alur Website: URL ditolak tanpa pernyataan hak
    st = FakeSt()
    st.mode = "web"
    st.clicked = {"🌐 Buka & periksa"}
    st.texts = {"URL situs": "https://x.test/"}
    run_app(st)
    assert any("pernyataan" in e for e in st.errors), st.errors
    assert "webjob" not in st.session_state

    # 3. Alur Website lengkap: Chromium tiruan + CLI tiruan, inspect + bundle
    st = FakeSt()
    st.mode = "web"
    st.clicked = {"🌐 Buka & periksa"}
    st.texts = {"URL situs": "https://x.test/"}
    st.checks = {"Saya memiliki situs ini atau berhak memeriksanya.": True,
                 "Analisis bundle JS": True, "Sumber skrip": True}
    run_app(st)
    assert not st.errors, st.errors
    wj = st.session_state["webjob"]
    assert [r[0] for r in wj["results"]] == ["inspect-web-page", "analyze-web-bundle"]
    for _label, res, _f in wj["results"]:
        assert res.ok, res.stderr
        data = json.loads(res.stdout_path.read_text())
        assert data["argv"][1].startswith("http://127.0.0.1:") and data["argv"][2] == "PAGE123"
    first = json.loads(wj["results"][0][1].stdout_path.read_text())["argv"]
    assert first[0] == "inspect-web-page" and "--include-script-sources" in first
    assert wj["url"] == "https://x.test/" and wj["title"] == "Fake Page"
    assert len([j for j in st.jsons if "argv" in j]) == 2  # selain panel Diagnostik

    # 4. Alur Website: URL ditolak validator asli
    st = FakeSt()
    st.mode = "web"
    st.clicked = {"🌐 Buka & periksa"}
    st.texts = {"URL situs": "http://127.0.0.1/"}
    st.checks = {"Saya memiliki situs ini atau berhak memeriksanya.": True}
    real_validate = rx.__dict__["validate_public_url"]
    import importlib

    importlib.reload(rx)  # kembalikan validator asli
    run_app(st)
    assert any("URL ditolak" in e for e in st.errors), st.errors
    assert "webjob" not in st.session_state
    rx.validate_public_url = lambda u, *a, **k: u

    # 5. Alur APK: unggah, analisis paket, lalu dekompilasi method
    jar = tmp / "jadx.jar"
    jar.write_bytes(b"x")
    rx.find_java = lambda: "/usr/lib/jvm/x/bin/java"
    rx.ensure_jadx_jar = lambda *a, **k: jar
    rx.android_env = lambda j: {"REA_JADX_MCP_JAR": str(j)}
    st = FakeSt()
    st.mode = "apk"
    st.upload = Upload("../../evil name.apk", b"PK\x03\x04fake")
    st.clicked = {"▶️ Jalankan analisis"}
    run_app(st)
    assert not st.errors, st.errors
    job = st.session_state["job"]
    out = json.loads(job["res"].stdout_path.read_text())
    assert out["argv"][0] == "inspect-android-package" and out["jar"] == str(jar)
    assert Path(job["target"]).name == "evil_name.apk"  # nama aman, tanpa path traversal

    st.clicked = {"Dekompilasi method"}
    st.texts = {"Nama kelas persis": "com.x.Main", "Nama method persis": "onCreate"}
    st.checks = {"Pakai indeks overload": True}
    before = len([j for j in st.jsons if "argv" in j])
    run_app(st)
    shown = [j for j in st.jsons if "argv" in j]
    assert len(shown) == before + 2  # hasil utama dirender ulang + hasil method
    m = shown[-1]
    assert m["argv"][:4] == ["inspect-android-method", job["target"], "com.x.Main", "onCreate"], m
    assert "--overload-index" in m["argv"] and m["jar"] == str(jar)

    # 6. Alur APK tanpa JDK: pesan jelas, tanpa job
    rx.find_java = lambda: None
    st2 = FakeSt()
    st2.mode = "apk"
    st2.upload = Upload("a.apk", b"x")
    st2.clicked = {"▶️ Jalankan analisis"}
    run_app(st2)
    assert any("JDK" in e for e in st2.errors) and "job" not in st2.session_state

    for key in ("job", "webjob"):
        for s in (st, st2):
            if key in s.session_state:
                rr.cleanup(Path(s.session_state[key]["workdir"]))
    rr.cleanup(tmp)
    print("SEMUA UJI ALUR APP LULUS")


if __name__ == "__main__":
    main()

"""Logika pembungkus CLI REA (rea-agents) untuk antarmuka web.

Modul ini sengaja bebas dari Streamlit supaya bisa diuji tersendiri.
Semua perintah dijalankan tanpa shell (daftar argumen), dengan timeout,
dan keluaran besar ditulis ke berkas, bukan ditahan di memori.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

NPX_PACKAGE = "rea-agents@latest"
MAX_INMEMORY_JSON_BYTES = 64 * 1024 * 1024  # batas memuat JSON ke memori UI
MAX_ZIP_ENTRIES = 50_000
MAX_ZIP_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------
# Menemukan CLI
# --------------------------------------------------------------------------
def _wheel_root() -> Path | None:
    """Folder paket `nodejs_wheel` (dari nodejs-wheel-binaries), bila terpasang."""
    try:
        import importlib.util

        spec = importlib.util.find_spec("nodejs_wheel")
        if spec and spec.submodule_search_locations:
            return Path(list(spec.submodule_search_locations)[0])
        if spec and spec.origin:
            return Path(spec.origin).parent
    except (ImportError, ValueError):
        pass
    for entry in sys.path:
        cand = Path(entry) / "nodejs_wheel"
        if cand.is_dir():
            return cand
    return None


def bundled_node() -> tuple[Path | None, Path | None]:
    """(node, npx-cli.js) dari paket pip, atau (None, None).

    nodejs-wheel-binaries tidak memasang `node`/`npx` ke bin/ venv; binernya ada
    di folder paketnya sendiri, dan npx dijalankan lewat npx-cli.js milik npm.
    """
    root = _wheel_root()
    if root is None:
        return None, None
    node = next(
        (p for p in [root / "bin" / "node", root / "node", root / "bin" / "node.exe"] if p.is_file()),
        None,
    )
    if node is None:
        node = next((p for p in root.rglob("node") if p.is_file() and os.access(p, os.X_OK)), None)
    npx_cli = next(iter(root.rglob("npx-cli.js")), None)
    return node, npx_cli


def tool_env() -> dict[str, str]:
    """Lingkungan untuk subproses.

    PATH diawali folder `node` dari paket pip (skrip bin `rea` memakai
    `#!/usr/bin/env node`) dan folder Python aktif. Cache npm diarahkan ke folder
    sementara yang pasti bisa ditulis.
    """
    env = dict(os.environ)
    extra = [str(Path(sys.executable).parent)]
    node, _ = bundled_node()
    if node is not None:
        extra.insert(0, str(node.parent))
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    env.setdefault("npm_config_cache", str(Path(tempfile.gettempdir()) / "rea_npm_cache"))
    env.setdefault("npm_config_update_notifier", "false")
    env.setdefault("npm_config_fund", "false")
    return env


def find_rea_command() -> list[str] | None:
    """Awalan perintah REA: `rea` jika ada, lalu npx dari paket pip, lalu npx sistem."""
    override = os.environ.get("REA_COMMAND", "").strip()
    if override:
        return override.split()
    pkg = os.environ.get("REA_PACKAGE", NPX_PACKAGE)
    path = tool_env()["PATH"]
    if shutil.which("rea", path=path):
        return ["rea"]
    node, npx_cli = bundled_node()
    if node is not None and npx_cli is not None:
        return [str(node), str(npx_cli), "-y", pkg]
    if shutil.which("npx", path=path):
        return ["npx", "-y", pkg]
    return None


def node_version() -> str | None:
    node_path, _ = bundled_node()
    node = str(node_path) if node_path else shutil.which("node", path=tool_env()["PATH"])
    if not node:
        return None
    try:
        out = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def diagnostics() -> dict:
    """Info untuk panel diagnostik saat Node/REA tidak ditemukan."""
    try:
        from importlib import metadata

        dists = sorted(
            f"{d.metadata['Name']}=={d.version}"
            for d in metadata.distributions()
            if d.metadata["Name"] and "node" in d.metadata["Name"].lower()
        )
    except Exception as exc:  # noqa: BLE001
        dists = [f"(gagal membaca metadata: {exc})"]
    root = _wheel_root()
    node, npx_cli = bundled_node()
    listing: list[str] = []
    if root is not None:
        try:
            listing = sorted(p.name for p in root.iterdir())[:30]
        except OSError:
            pass
    return {
        "python": sys.executable,
        "paket_node_terpasang": dists,
        "folder_nodejs_wheel": str(root) if root else None,
        "isi_folder": listing,
        "node": str(node) if node else None,
        "npx_cli": str(npx_cli) if npx_cli else None,
        "node_di_PATH": shutil.which("node"),
        "npx_di_PATH": shutil.which("npx"),
    }


def node_version_ok(version: str | None) -> bool:
    """REA butuh Node 22.x (>=22.19), 24.x (>=24.11), atau >=26."""
    if not version:
        return False
    m = re.match(r"v?(\d+)\.(\d+)", version)
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2))
    if major == 22:
        return minor >= 19
    if major == 24:
        return minor >= 11
    return major >= 26


# --------------------------------------------------------------------------
# Menjalankan perintah
# --------------------------------------------------------------------------
@dataclass
class RunResult:
    argv: list[str]
    returncode: int | None
    stdout_path: Path
    stderr: str
    elapsed: float
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.error is None

    @property
    def stdout_size(self) -> int:
        try:
            return self.stdout_path.stat().st_size
        except OSError:
            return 0


def run_cli(
    prefix: list[str],
    args: list[str],
    workdir: Path,
    *,
    timeout: int = 600,
    out_name: str = "out.json",
    extra_env: dict[str, str] | None = None,
) -> RunResult:
    """Jalankan `prefix + args`; stdout ke berkas, stderr dipotong 64 KiB."""
    argv = [*prefix, *args]
    out_path = workdir / out_name
    env = tool_env()
    if extra_env:
        env.update(extra_env)
    start = time.monotonic()
    try:
        with out_path.open("wb") as fh:
            proc = subprocess.run(  # noqa: S603 - tanpa shell, argumen berupa daftar
                argv,
                stdout=fh,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                cwd=str(workdir),
                env=env,
            )
        stderr = proc.stderr[:65536].decode("utf-8", "replace")
        return RunResult(argv, proc.returncode, out_path, stderr, time.monotonic() - start)
    except subprocess.TimeoutExpired as exc:
        err = (exc.stderr or b"")[:65536]
        return RunResult(
            argv,
            None,
            out_path,
            err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err),
            time.monotonic() - start,
            timed_out=True,
        )
    except OSError as exc:
        return RunResult(argv, None, out_path, "", time.monotonic() - start, error=str(exc))


def load_json(path: Path, limit: int = MAX_INMEMORY_JSON_BYTES):
    """Muat JSON bila ukurannya wajar; kembalikan (data, catatan)."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, f"Berkas keluaran tidak terbaca: {exc}"
    if size == 0:
        return None, "Keluaran kosong."
    if size > limit:
        return None, f"Keluaran {size / 1e6:.1f} MB melebihi batas tampilan {limit / 1e6:.0f} MB."
    try:
        return json.loads(path.read_text("utf-8", errors="replace")), None
    except json.JSONDecodeError as exc:
        return None, f"Keluaran bukan JSON valid: {exc}"


# --------------------------------------------------------------------------
# Berkas unggahan
# --------------------------------------------------------------------------
def safe_filename(name: str, default: str = "target") -> str:
    base = Path(name.replace("\\", "/")).name
    cleaned = _SAFE_NAME.sub("_", base).strip("._")
    return cleaned or default


def save_upload(data: bytes, filename: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / safe_filename(filename)
    dest.write_bytes(data)
    return dest


class UnsafeArchive(ValueError):
    pass


def extract_zip_safe(zip_path: Path, dest: Path) -> Path:
    """Ekstrak ZIP dengan perlindungan zip-slip, jumlah entri, dan ukuran."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise UnsafeArchive(f"Terlalu banyak entri ({len(infos)}).")
        if sum(i.file_size for i in infos) > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise UnsafeArchive("Ukuran hasil ekstrak melebihi batas 2 GiB.")
        for info in infos:
            target = (root / info.filename).resolve()
            if root != target and root not in target.parents:
                raise UnsafeArchive(f"Entri keluar dari folder tujuan: {info.filename}")
            mode = info.external_attr >> 16
            if (mode & 0o170000) == 0o120000:
                raise UnsafeArchive(f"Symlink tidak diizinkan: {info.filename}")
        zf.extractall(root)
    # Jika ZIP hanya berisi satu folder teratas, pakai folder itu sebagai target.
    entries = [p for p in root.iterdir() if p.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return root


# --------------------------------------------------------------------------
# Tampilan analisis (inspect-analysis-view) untuk Evidence besar
# --------------------------------------------------------------------------
def write_view_request(evidence_path: Path, view: dict, out_path: Path) -> Path:
    """Tulis {"source":{"kind":"inline","evidence":<berkas>},"view":<view>} secara streaming."""
    with out_path.open("wb") as out, evidence_path.open("rb") as ev:
        out.write(b'{"source":{"kind":"inline","evidence":')
        shutil.copyfileobj(ev, out, 1024 * 1024)
        out.write(b'},"view":')
        out.write(json.dumps(view).encode("utf-8"))
        out.write(b"}")
    return out_path


# --------------------------------------------------------------------------
# Definisi mode analisis
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    description: str
    accepts: str  # "file", "dir_or_file"
    types: list[str] = field(default_factory=list)  # ekstensi unggahan (tanpa titik)
    needs_engine: str = "Tidak ada (analisis statis)"


MODES: dict[str, Mode] = {
    "js": Mode(
        "js",
        "JavaScript / Electron",
        "Modul, impor, batas Electron, rute, dan IPC dari ASAR atau folder aplikasi (unggah .asar atau .zip).",
        "dir_or_file",
        ["asar", "zip"],
    ),
    "dotnet": Mode(
        "dotnet",
        ".NET assembly",
        "Identitas, metadata, dan anggota tipe dari PE/CLI (.dll/.exe).",
        "file",
        ["dll", "exe"],
    ),
    "sqlite": Mode(
        "sqlite",
        "Database SQLite",
        "Tabel, kolom, indeks, view, trigger, dan contoh baris tabel biasa.",
        "file",
        ["db", "sqlite", "sqlite3", "db3"],
        "Node.js 24.15+ atau 26+ (batas SQLite native)",
    ),
    "go": Mode(
        "go",
        "Biner Go",
        "Versi compiler, modul, replacement, dan build settings yang tertanam.",
        "file",
        [],
    ),
    "rust": Mode(
        "rust",
        "Biner Rust",
        "Crate cargo-auditable yang tertanam: nama, versi, sumber, dan edge dependensi.",
        "file",
        [],
    ),
    "apk": Mode(
        "apk",
        "Android APK",
        "Manifest dan izin, daftar kelas, serta dekompilasi method dari APK (statis, tanpa menjalankan aplikasi).",
        "file",
        ["apk"],
        "JDK 17+ (dipasang lewat packages.txt) dan JADX 0.7.1 (diunduh & diverifikasi otomatis)",
    ),
    "web": Mode(
        "web",
        "Website (dibuka langsung)",
        "App membuka URL di Chromium headless milik server, lalu REA mengamati halaman: struktur DOM, skrip, request jaringan, dan bundle.",
        "url",
        [],
        "Chromium (dipasang lewat packages.txt)",
    ),
    "artifact": Mode(
        "artifact",
        "Inventaris paket / aplikasi",
        "Inventaris berkas, digest, plist, dan anatomi bundle dari paket atau folder aplikasi.",
        "dir_or_file",
        [],
    ),
}


def build_args(mode_key: str, target: Path, opts: dict) -> list[str]:
    """Susun argumen CLI untuk mode tertentu (tanpa awalan perintah)."""
    t = str(target)
    if mode_key == "js":
        return ["analyze-javascript-application", t, "--json"]
    if mode_key == "dotnet":
        return ["inspect-managed-artifact", t, "--json"]
    if mode_key == "sqlite":
        args = ["inspect-sqlite-database", t, "--json"]
        if opts.get("table"):
            args += ["--table", str(opts["table"])]
            args += ["--row-limit", str(int(opts.get("row_limit", 20)))]
        return args
    if mode_key == "go":
        return ["inspect-go-binary", t, "--json"]
    if mode_key == "rust":
        return ["inspect-rust-binary", t, "--json"]
    if mode_key == "artifact":
        return ["inspect-artifact", t, "--json"]
    if mode_key == "apk":
        return apk_args("package", target)
    raise KeyError(mode_key)


def apk_args(kind: str, target: Path, **kw) -> list[str]:
    """Argumen untuk perintah Android: package | search | class | method."""
    t = str(target)
    if kind == "package":
        return ["inspect-android-package", t, "--json"]
    if kind == "search":
        return ["search-android-classes", t, kw.get("query", ""), "--json"]
    if kind == "class":
        return ["inspect-android-class", t, kw["class_name"], "--json"]
    if kind == "method":
        args = ["inspect-android-method", t, kw["class_name"], kw["method_name"], "--json"]
        if kw.get("overload_index") is not None:
            args += ["--overload-index", str(int(kw["overload_index"]))]
        return args
    raise KeyError(kind)


def web_args(kind: str, endpoint: str, target_id: str, **kw) -> list[str]:
    """Argumen untuk perintah website: inspect | bundle."""
    ms = str(int(kw.get("observation_ms", 3000)))
    if kind == "inspect":
        args = ["inspect-web-page", endpoint, target_id, "--json", "--observation-ms", ms]
        for flag in ("include_json_body_shapes", "include_script_sources", "include_storage_keys"):
            if kw.get(flag):
                args.append("--" + flag.replace("_", "-"))
        return args
    if kind == "bundle":
        args = ["analyze-web-bundle", endpoint, target_id, "--json", "--observation-ms", ms]
        if kw.get("fetch_source_maps"):
            args.append("--fetch-source-maps")
        return args
    raise KeyError(kind)


def build_members_args(target: Path, types: list[str]) -> list[str]:
    args = ["inspect-managed-members", str(target), "--json"]
    for name in types:
        if name.strip():
            args += ["--type", name.strip()]
    return args


def new_workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="rea_webui_"))


def cleanup(workdir: Path) -> None:
    shutil.rmtree(workdir, ignore_errors=True)

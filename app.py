"""Antarmuka web Streamlit untuk REA (Reverse Engineer Anything).

Membungkus CLI `rea-agents` untuk analisis statis: JavaScript/Electron, .NET,
SQLite, biner Go/Rust, dan inventaris paket. Tidak ada kode target yang dijalankan.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

import rea_runner as rr

st.set_page_config(page_title="REA Web UI", page_icon="🔬", layout="wide")

ALLOW_LOCAL_PATHS = os.environ.get("REA_ALLOW_LOCAL_PATHS", "") == "1"
DOWNLOAD_LIMIT = 200 * 1024 * 1024


# ---------------------------------------------------------------- helpers
def reset_job() -> None:
    old = st.session_state.pop("job", None)
    if old:
        rr.cleanup(Path(old["workdir"]))


def show_run(res: rr.RunResult, label: str) -> None:
    c1, c2, c3 = st.columns(3)
    c1.metric("Status", "Berhasil" if res.ok else "Gagal", f"exit {res.returncode}")
    c2.metric("Durasi", f"{res.elapsed:.1f} dtk")
    c3.metric("Ukuran keluaran", f"{res.stdout_size / 1e6:.2f} MB")
    if res.timed_out:
        st.error("Perintah melewati batas waktu. Naikkan timeout di sidebar.")
    if res.error:
        st.error(f"Gagal memulai perintah: {res.error}")
    with st.expander(f"Perintah & log — {label}"):
        st.code(" ".join(res.argv), language="bash")
        st.text(res.stderr or "(stderr kosong)")


def show_output(res: rr.RunResult, name: str, key: str) -> None:
    data, note = rr.load_json(res.stdout_path)
    if data is not None:
        st.json(data, expanded=1)
    elif note:
        st.warning(note)
        if res.stdout_size and res.stdout_size <= 2_000_000:
            st.code(res.stdout_path.read_text("utf-8", errors="replace"))
    if 0 < res.stdout_size <= DOWNLOAD_LIMIT:
        st.download_button(
            "⬇️ Unduh keluaran JSON",
            res.stdout_path.read_bytes(),
            file_name=name,
            mime="application/json",
            key=f"dl_{key}",
        )
    elif res.stdout_size:
        st.info("Keluaran terlalu besar untuk diunduh dari browser (>200 MB).")


def resolve_target(mode: rr.Mode, workdir: Path, source: str, uploaded, local_path: str):
    """Kembalikan (Path target, pesan_error)."""
    if source == "path":
        p = Path(local_path).expanduser()
        if not p.is_absolute() or not p.exists():
            return None, "Isi path absolut yang ada di mesin ini."
        return p, None
    if uploaded is None:
        return None, "Unggah berkas target terlebih dahulu."
    saved = rr.save_upload(uploaded.getvalue(), uploaded.name, workdir / "input")
    if saved.suffix.lower() == ".zip" and mode.accepts == "dir_or_file":
        try:
            return rr.extract_zip_safe(saved, workdir / "extracted"), None
        except (rr.UnsafeArchive, OSError, Exception) as exc:  # noqa: BLE001
            return None, f"ZIP ditolak: {exc}"
    return saved, None


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Lingkungan")
    prefix = rr.find_rea_command()
    nv = rr.node_version()
    st.write(f"**Node.js:** `{nv or 'tidak ditemukan'}`")
    if nv and not rr.node_version_ok(nv):
        st.warning("REA butuh Node 22.19+, 24.11+, atau 26+.")
    if prefix:
        st.write("**Perintah:** `" + " ".join(prefix) + "`")
    else:
        st.error("Tidak ada `rea` maupun `npx`. Pasang Node.js dan npm.")
    if prefix and st.button("Uji koneksi REA"):
        wd = rr.new_workdir()
        with st.spinner("Mengunduh/menjalankan rea-agents…"):
            chk = rr.run_cli(prefix, ["--version"], wd, timeout=300, out_name="v.txt")
        if chk.ok:
            st.success("REA siap: " + chk.stdout_path.read_text().strip()[:80])
        else:
            st.error("REA gagal dijalankan. Lihat log di bawah.")
            st.code((chk.stderr or chk.error or "(tanpa pesan)")[:2000])
        rr.cleanup(wd)
    timeout = st.slider("Timeout per perintah (detik)", 30, 1800, 600, step=30)
    st.divider()
    st.caption(
        "Gunakan hanya pada target yang sah: riset keamanan, interoperabilitas, "
        "debugging, atau perangkat lunak yang Anda miliki/berhak periksa. "
        "Lihat DISCLAIMER REA untuk batasannya."
    )

# ---------------------------------------------------------------- main
st.title("🔬 REA Web UI")
st.caption("Antarmuka untuk CLI REA — analisis statis tanpa menjalankan kode target.")

mode_key = st.selectbox(
    "Jenis analisis",
    list(rr.MODES),
    format_func=lambda k: rr.MODES[k].label,
    on_change=reset_job,
)
mode = rr.MODES[mode_key]
st.info(f"{mode.description}\n\n**Kebutuhan:** {mode.needs_engine}")

source = "upload"
if ALLOW_LOCAL_PATHS:
    source = st.radio(
        "Sumber target",
        ["upload", "path"],
        format_func=lambda s: "Unggah berkas" if s == "upload" else "Path lokal",
        horizontal=True,
    )

uploaded, local_path = None, ""
if source == "upload":
    uploaded = st.file_uploader(
        "Berkas target",
        type=mode.types or None,
        help="Folder aplikasi: kompres menjadi .zip lalu unggah." if mode.accepts == "dir_or_file" else None,
    )
else:
    local_path = st.text_input("Path absolut target", placeholder="/path/ke/target")

opts: dict = {}
if mode_key == "sqlite":
    c1, c2 = st.columns(2)
    opts["table"] = c1.text_input("Tabel (opsional)", help="Nama tabel biasa untuk contoh baris")
    opts["row_limit"] = c2.number_input("Batas baris", 1, 1000, 20)

run_clicked = st.button("▶️ Jalankan analisis", type="primary", disabled=prefix is None)

if run_clicked and prefix:
    reset_job()
    workdir = rr.new_workdir()
    target, err = resolve_target(mode, workdir, source, uploaded, local_path)
    if err:
        st.error(err)
        rr.cleanup(workdir)
    else:
        with st.spinner("REA sedang menganalisis… (unduhan npx pertama bisa memakan waktu)"):
            res = rr.run_cli(
                prefix,
                rr.build_args(mode_key, target, opts),
                workdir,
                timeout=timeout,
                out_name="result.json",
            )
        st.session_state["job"] = {
            "mode": mode_key,
            "workdir": str(workdir),
            "target": str(target),
            "res": res,
        }

job = st.session_state.get("job")
if job and job["mode"] == mode_key and prefix:
    workdir = Path(job["workdir"])
    target = Path(job["target"])
    res: rr.RunResult = job["res"]
    st.subheader("Hasil")
    show_run(res, "analisis utama")

    if res.ok and mode_key == "js":
        st.markdown("**Evidence aplikasi disimpan; pilih tampilan terproyeksi** (aman untuk Evidence ratusan MB).")
        view_kind = st.radio("Tampilan", ["Ringkasan", "Halaman modul"], horizontal=True)
        view: dict = {"kind": "summary"}
        if view_kind == "Halaman modul":
            c1, c2 = st.columns(2)
            offset = c1.number_input("Offset", 0, 10_000_000, 0, step=32)
            limit = c2.number_input("Limit", 1, 500, 32)
            view = {"kind": "page", "collection": "modules", "offset": int(offset), "limit": int(limit)}
        if st.button("Tampilkan"):
            req = rr.write_view_request(res.stdout_path, view, workdir / "view-request.json")
            vres = rr.run_cli(prefix, ["inspect-analysis-view", str(req), "--json"], workdir,
                              timeout=timeout, out_name="view-result.json")
            show_run(vres, "inspect-analysis-view")
            show_output(vres, "analysis-view.json", "view")
        if 0 < res.stdout_size <= DOWNLOAD_LIMIT:
            st.download_button("⬇️ Unduh Evidence lengkap", res.stdout_path.read_bytes(),
                               file_name="app-evidence.json", mime="application/json", key="dl_full")
        elif res.stdout_size:
            st.caption(f"Evidence lengkap {res.stdout_size / 1e6:.0f} MB tersimpan di server selama sesi.")
    else:
        show_output(res, f"rea-{mode_key}.json", "main")

    if res.ok and mode_key == "dotnet":
        st.divider()
        st.markdown("**Anggota tipe (metadata & CIL)**")
        types_text = st.text_input("Filter tipe (opsional, pisahkan koma)", placeholder="Example.Parser, Example.Reader")
        if st.button("Periksa anggota"):
            names = [s for s in types_text.split(",") if s.strip()]
            mres = rr.run_cli(prefix, rr.build_members_args(target, names), workdir,
                              timeout=timeout, out_name="members.json")
            show_run(mres, "inspect-managed-members")
            show_output(mres, "managed-members.json", "members")

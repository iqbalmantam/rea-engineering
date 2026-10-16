"""Antarmuka web Streamlit untuk REA (Reverse Engineer Anything).

Membungkus CLI `rea-agents`: JavaScript/Electron, .NET, SQLite, biner Go/Rust,
inventaris paket, APK Android (statis), dan Website (halaman dibuka di Chromium
milik server sehingga skrip situs ikut berjalan di sana).
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

import rea_extras as rx
import rea_runner as rr

st.set_page_config(page_title="REA Web UI", page_icon="🔬", layout="wide")

ALLOW_LOCAL_PATHS = os.environ.get("REA_ALLOW_LOCAL_PATHS", "") == "1"
DOWNLOAD_LIMIT = 200 * 1024 * 1024


# ---------------------------------------------------------------- helpers
def reset_job() -> None:
    for key in ("job", "webjob"):
        old = st.session_state.pop(key, None)
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


def web_ui(prefix, timeout: int) -> None:
    """Mode Website: buka URL di Chromium headless milik server, lalu amati lewat REA."""
    st.warning(
        "Mode ini membuka situs di Chromium milik server, jadi skrip situs ikut berjalan di sana. "
        "Gunakan hanya pada situs yang Anda miliki atau berhak periksa."
    )
    exe = rx.find_chromium()
    if not exe:
        st.error("Chromium tidak ditemukan di server. Pastikan `chromium` ada di packages.txt.")
        return
    url = st.text_input("URL situs", placeholder="https://contoh.com/halaman")
    c1, c2 = st.columns(2)
    observe_s = c1.slider("Durasi pengamatan (detik)", 1, 30, 4)
    settle_s = c2.slider("Tunggu halaman selesai dimuat (detik)", 0, 20, 6)
    o1, o2, o3, o4 = st.columns(4)
    include_json = o1.checkbox("Bentuk body JSON", help="Struktur (bukan isi) respons JSON")
    include_scripts = o2.checkbox("Sumber skrip", help="Menyertakan teks JavaScript; keluaran bisa besar")
    include_storage = o3.checkbox("Nama kunci storage", help="Hanya nama kunci, tanpa nilai")
    do_bundle = o4.checkbox("Analisis bundle JS", help="Rute dan endpoint dari skrip yang dimuat")
    auth = st.checkbox("Saya memiliki situs ini atau berhak memeriksanya.")

    if st.button("🌐 Buka & periksa", type="primary", disabled=prefix is None):
        reset_job()
        if not auth:
            st.error("Centang pernyataan hak memeriksa terlebih dahulu.")
        else:
            try:
                safe_url = rx.validate_public_url(url)
            except rx.UnsafeUrl as exc:
                st.error(f"URL ditolak: {exc}")
            else:
                workdir = rr.new_workdir()
                results: list = []
                try:
                    with st.spinner("Membuka situs di Chromium dan menjalankan REA…"):
                        sess = rx.open_page(safe_url, exe=exe, settle_seconds=float(settle_s))
                        try:
                            kw = dict(observation_ms=observe_s * 1000)
                            results.append((
                                "inspect-web-page",
                                rr.run_cli(prefix, rr.web_args(
                                    "inspect", sess.endpoint, sess.target_id,
                                    include_json_body_shapes=include_json,
                                    include_script_sources=include_scripts,
                                    include_storage_keys=include_storage, **kw),
                                    workdir, timeout=timeout, out_name="web-inspect.json"),
                                "web-inspect.json",
                            ))
                            if do_bundle:
                                results.append((
                                    "analyze-web-bundle",
                                    rr.run_cli(prefix, rr.web_args(
                                        "bundle", sess.endpoint, sess.target_id, **kw),
                                        workdir, timeout=timeout, out_name="web-bundle.json"),
                                    "web-bundle.json",
                                ))
                        finally:
                            info = (sess.target_url, sess.target_title)
                            sess.close()
                except rx.SetupError as exc:
                    st.error(f"Gagal membuka situs: {exc}")
                    rr.cleanup(workdir)
                else:
                    st.session_state["webjob"] = {
                        "workdir": str(workdir), "url": info[0], "title": info[1], "results": results,
                    }

    wj = st.session_state.get("webjob")
    if wj:
        st.subheader("Hasil")
        st.caption(f"Halaman: {wj['title'] or '(tanpa judul)'} — {wj['url']}")
        for label, res, fname in wj["results"]:
            st.markdown(f"**{label}**")
            show_run(res, label)
            show_output(res, fname, f"web_{label}")


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
        st.error("Node.js/npx tidak ditemukan. Buka *Diagnostik* di bawah.")
    with st.expander("Diagnostik", expanded=prefix is None):
        st.json(rr.diagnostics())
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
    jv = rx.java_version()
    st.write(f"**Java:** `{jv or 'tidak ditemukan'}` (mode APK)")
    st.write(f"**Chromium:** `{'ada' if rx.find_chromium() else 'tidak ditemukan'}` (mode Website)")
    timeout = st.slider("Timeout per perintah (detik)", 30, 1800, 600, step=30)
    st.divider()
    st.caption(
        "Gunakan hanya pada target yang sah: riset keamanan, interoperabilitas, "
        "debugging, atau perangkat lunak yang Anda miliki/berhak periksa. "
        "Lihat DISCLAIMER REA untuk batasannya."
    )

# ---------------------------------------------------------------- main
st.title("🔬 REA Web UI")
st.caption(
    "Antarmuka untuk CLI REA. Analisis berkas bersifat statis (kode target tidak dijalankan); "
    "mode Website membuka halaman di Chromium server, jadi skrip situs berjalan di sana."
)

mode_key = st.selectbox(
    "Jenis analisis",
    list(rr.MODES),
    format_func=lambda k: rr.MODES[k].label,
    on_change=reset_job,
)
mode = rr.MODES[mode_key]
st.info(f"{mode.description}\n\n**Kebutuhan:** {mode.needs_engine}")

if mode_key == "web":
    web_ui(prefix, timeout)
    st.stop()

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
        extra_env = None
        if mode_key == "apk":
            if not rx.find_java():
                st.error("JDK tidak ditemukan di server. Pastikan `default-jdk-headless` ada di packages.txt.")
                rr.cleanup(workdir)
                st.stop()
            try:
                with st.spinner("Menyiapkan JADX (unduhan pertama, diverifikasi SHA-256)…"):
                    extra_env = rx.android_env(rx.ensure_jadx_jar())
            except rx.SetupError as exc:
                st.error(str(exc))
                rr.cleanup(workdir)
                st.stop()
        with st.spinner("REA sedang menganalisis… (unduhan npx pertama bisa memakan waktu)"):
            res = rr.run_cli(
                prefix,
                rr.build_args(mode_key, target, opts),
                workdir,
                timeout=timeout,
                out_name="result.json",
                extra_env=extra_env,
            )
        st.session_state["job"] = {
            "mode": mode_key,
            "workdir": str(workdir),
            "target": str(target),
            "res": res,
            "extra_env": extra_env,
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

    if res.ok and mode_key == "apk":
        env = job.get("extra_env")
        st.divider()
        st.markdown("**Jelajahi kelas dan dekompilasi method**")
        q = st.text_input("Cari kelas (potongan nama, peka huruf besar-kecil; kosong = semua)", key="apk_q")
        if st.button("Cari kelas"):
            sres = rr.run_cli(prefix, rr.apk_args("search", target, query=q), workdir,
                              timeout=timeout, out_name="apk-search.json", extra_env=env)
            show_run(sres, "search-android-classes")
            show_output(sres, "apk-classes.json", "apk_search")
        cname = st.text_input("Nama kelas persis", placeholder="com.contoh.MainActivity", key="apk_class")
        c1, c2 = st.columns(2)
        if c1.button("Lihat anggota kelas") and cname.strip():
            cres = rr.run_cli(prefix, rr.apk_args("class", target, class_name=cname.strip()), workdir,
                              timeout=timeout, out_name="apk-class.json", extra_env=env)
            show_run(cres, "inspect-android-class")
            show_output(cres, "apk-class.json", "apk_class")
        mname = c2.text_input("Nama method persis", placeholder="onCreate", key="apk_method")
        overload = st.number_input("Indeks overload (isi jika ada beberapa method bernama sama)",
                                   0, 100, 0, key="apk_overload")
        use_overload = st.checkbox("Pakai indeks overload", key="apk_use_overload")
        if st.button("Dekompilasi method") and cname.strip() and mname.strip():
            mres = rr.run_cli(prefix, rr.apk_args(
                "method", target, class_name=cname.strip(), method_name=mname.strip(),
                overload_index=int(overload) if use_overload else None), workdir,
                timeout=timeout, out_name="apk-method.json", extra_env=env)
            show_run(mres, "inspect-android-method")
            show_output(mres, "apk-method.json", "apk_method")

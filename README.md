# REA Web UI

Antarmuka Streamlit untuk CLI [REA](https://github.com/morluto/rea) (`rea-agents`).
Hanya analisis statis: tidak ada kode target yang dijalankan.

## Mode

| Mode | Perintah REA | Berkas target |
| --- | --- | --- |
| JavaScript / Electron | `analyze-javascript-application`, lalu `inspect-analysis-view` | `.asar` atau `.zip` folder aplikasi |
| .NET assembly | `inspect-managed-artifact`, `inspect-managed-members` | `.dll` / `.exe` |
| SQLite | `inspect-sqlite-database` | `.db` / `.sqlite` |
| Biner Go | `inspect-go-binary` | ELF / PE / Mach-O |
| Biner Rust | `inspect-rust-binary` | ELF / PE / Mach-O |
| Inventaris paket | `inspect-artifact` | berkas atau `.zip` folder |
| Android APK | `inspect-android-package`, `search-android-classes`, `inspect-android-class`, `inspect-android-method` | `.apk` |
| Website (dibuka langsung) | `inspect-web-page`, `analyze-web-bundle` | URL `http(s)` |

Evidence JavaScript bisa ratusan MB, jadi UI menyimpannya di server dan hanya
menampilkan ringkasan atau satu halaman modul lewat `inspect-analysis-view`.

## Deploy: GitHub + Streamlit Community Cloud (tanpa lokal)

1. Buat repositori GitHub baru dan unggah isi folder ini lewat web (Add file ->
   Upload files) sehingga `app.py`, `rea_runner.py`, `requirements.txt` dan
   `packages.txt` berada di **root** repositori.
2. Di [share.streamlit.io](https://share.streamlit.io) pilih *Create app*,
   arahkan ke repositori itu, branch `main`, main file `app.py`, lalu *Deploy*.
3. Setelah app terbuka, klik **Uji koneksi REA** di sidebar. Unduhan pertama
   `rea-agents` lewat `npx` bisa makan beberapa menit.

Cara kerjanya: `requirements.txt` memasang Node.js modern lewat paket PyPI
`nodejs-wheel-binaries` (REA butuh Node 22.19+; Node dari apt biasanya lebih
lama). `packages.txt` memasang `procps` (dibutuhkan REA di Linux), `chromium`
(mode Website), dan `default-jdk-headless` (mode APK). Sidebar menampilkan versi
Node, Java, dan ada tidaknya Chromium.

Batasan Community Cloud: memori terbatas (Evidence JavaScript besar bisa
gagal), unggahan maksimal 200 MB, dan filesystem hilang saat app di-restart.
Aplikasi yang tidur akan memulai ulang dan mengunduh `rea-agents` lagi.

Opsional lewat *Secrets*/environment: `REA_PACKAGE` untuk mengunci versi,
misalnya `rea-agents@6.5.0`.

## Mode Android APK

Unggah `.apk`, app menjalankan `inspect-android-package` (izin, manifest,
deklarasi), lalu Anda bisa mencari kelas, melihat anggota kelas, dan mendekompilasi
satu method. REA memakai JADX headless: app mengunduh `jadx-headless-mcp 0.7.1`
dari GitHub sekali per sesi server dan memverifikasi SHA-256 yang dipin oleh REA
sebelum dipakai. Dokumentasi REA menyatakan bridge ini baru diverifikasi nyata di
macOS dengan JDK 21; Linux belum, jadi hasil di Streamlit Cloud belum terjamin.
JADX memakan memori; APK besar bisa gagal.

## Mode Website (dibuka langsung)

App membuka URL di Chromium headless milik server (CDP hanya di loopback), menunggu
halaman dimuat, lalu menjalankan `inspect-web-page` dan opsional `analyze-web-bundle`.
**Skrip situs berjalan di Chromium server**, berbeda dari mode berkas yang statis.

Pengamanan yang ada:
- Hanya `http://` dan `https://`, tanpa username/password, port 80/443/8080/8443.
- Host di-resolve dan ditolak bila mengarah ke alamat non-publik (localhost,
  jaringan privat, link-local/metadata).
- Pengguna harus menyatakan berhak memeriksa situs.

Batasan yang perlu diketahui:
- Validasi dilakukan sebelum halaman dibuka. Redirect atau subrequest situs ke alamat
  internal setelah itu tidak dicegah, dan DNS bisa berubah di antara validasi dan
  pemuatan. Karena itu **jangan membuka app ini untuk publik**: di Streamlit Cloud
  atur *Share* menjadi hanya orang tertentu.
- Halaman diamati setelah dimuat; request yang terjadi sebelum REA terhubung bisa
  tidak tercatat. Situs yang butuh login, CAPTCHA, atau interaksi tidak didukung.
- Chromium dari apt dan Streamlit Cloud belum saya uji bersama REA.

## Menjalankan lokal (opsional)

```bash
pip install -r requirements.txt
streamlit run app.py
```

Untuk memeriksa target lewat path di mesin yang sama, jalankan dengan
`REA_ALLOW_LOCAL_PATHS=1`. Jangan aktifkan ini pada deployment publik.

## Pengujian

```bash
python test_rea_runner.py   # logika pembungkus CLI
python test_rea_extras.py   # validasi URL, unduh+verifikasi JADX, peluncuran Chromium
python test_app_flow.py     # alur app.py dengan Streamlit/CLI/Chromium tiruan
```

Semua memakai tiruan tanpa jaringan (CLI REA, Chromium, dan Streamlit palsu).
Yang belum teruji: tampilan Streamlit sungguhan, JADX asli, dan Chromium asli.
Tampilan Streamlit (`app.py`) belum diuji end-to-end.

## Batasan

- Native analysis (Hopper, Ghidra, IDA) tidak dibungkus; itu butuh engine terpasang.
- Mode Website tidak mendukung situs yang butuh login atau interaksi.
- Folder aplikasi harus dikompres ke `.zip` untuk diunggah.
- Gunakan hanya pada target yang sah (lihat `DISCLAIMER.md` REA).

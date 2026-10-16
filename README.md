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
lama), dan `packages.txt` memasang `procps` yang dibutuhkan REA di Linux.
Sidebar menampilkan versi Node yang terdeteksi.

Batasan Community Cloud: memori terbatas (Evidence JavaScript besar bisa
gagal), unggahan maksimal 200 MB, dan filesystem hilang saat app di-restart.
Aplikasi yang tidur akan memulai ulang dan mengunduh `rea-agents` lagi.

Opsional lewat *Secrets*/environment: `REA_PACKAGE` untuk mengunci versi,
misalnya `rea-agents@6.5.0`.

## Menjalankan lokal (opsional)

```bash
pip install -r requirements.txt
streamlit run app.py
```

Untuk memeriksa target lewat path di mesin yang sama, jalankan dengan
`REA_ALLOW_LOCAL_PATHS=1`. Jangan aktifkan ini pada deployment publik.

## Pengujian

```bash
python test_rea_runner.py
```

Menguji logika pembungkus dengan CLI tiruan (tanpa jaringan): streaming Evidence,
timeout, kegagalan, penolakan zip-slip, dan pengecekan versi Node.
Tampilan Streamlit (`app.py`) belum diuji end-to-end.

## Batasan

- Native analysis (Hopper, Ghidra, IDA) tidak dibungkus; itu butuh engine terpasang.
- Folder aplikasi harus dikompres ke `.zip` untuk diunggah.
- Gunakan hanya pada target yang sah (lihat `DISCLAIMER.md` REA).

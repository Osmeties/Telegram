# Bot Broadcast + Deep-Link Media

Bot Telegram sederhana dengan dua fitur utama:

1. **Broadcast** — kirim satu pesan ke banyak channel/grup sekaligus.
2. **Deep-link media** — user klik link `https://t.me/NamaBot?start=get_KODE`,
   bot otomatis kirim media (foto/video/dokumen/gif) yang sudah disimpan
   dengan kode itu. Ini pola yang sama seperti banyak bot "file store" di
   Telegram (link → auto-kirim konten ke chat pribadi).

## 1. Persiapan

```bash
pip install -r requirements.txt
```

1. Buat bot baru lewat [@BotFather](https://t.me/BotFather), salin tokennya.
2. **Siapkan database Postgres di Railway:**
   - Di project Railway kamu, klik **New → Database → Add PostgreSQL**.
   - Railway otomatis bikin variable `DATABASE_URL` dan menyambungkannya
     ke service bot kamu (kalau service bot & Postgres ada di project
     yang sama, tinggal reference variable-nya — Railway biasanya
     menawarkan ini otomatis lewat "Add Variable Reference").
   - Bot ini (`db.py`) otomatis baca `DATABASE_URL` dari environment,
     jadi **tidak perlu edit kode apa pun** untuk connect ke Postgres.
   - Tabel `media` dibuat otomatis saat bot pertama kali start
     (lihat `init_db()` di `db.py`).
3. Isi variable lain di tab **Variables** Railway (atau di `config.py`
   kalau jalan lokal):
   - `BOT_TOKEN` — token dari BotFather.
   - `ADMIN_IDS` — user_id kamu (cek lewat @userinfobot), pisahkan koma
     kalau lebih dari satu, contoh: `123456789,987654321`.
   - `TARGET_CHATS` — chat_id channel/grup tujuan broadcast, pisahkan
     koma. Bot harus jadi admin di sana dulu.
   - `REQUIRED_CHATS` — channel/grup yang WAJIB di-join sebelum user bisa
     ambil konten dari deep link. Bot harus jadi **admin** di setiap
     channel/grup ini (supaya bisa cek status member lewat
     `getChatMember`). Cara cepat dapat `chat_id`: forward pesan apa saja
     dari channel/grup tsb ke [@userinfobot](https://t.me/userinfobot),
     atau add bot [@RawDataBot](https://t.me/RawDataBot) sementara ke
     situ. Kalau lewat Railway env var, isi sebagai JSON string (lihat
     contoh format di dalam `config.py`).

## 2. Jalankan bot

**Lokal:**
```bash
python bot.py
```

**Di Railway:** cukup push/deploy — Railway otomatis jalankan
`python bot.py` (atau sesuai Procfile/Start Command yang kamu set).
Bot akan connect ke Postgres pakai `DATABASE_URL` yang sudah tersambung.

## 3. Cara pakai

### Menyimpan media & membuat link
1. Kirim/forward media (foto/video/dokumen/gif) ke bot lewat chat pribadi.
2. **Reply** media itu dengan:
   ```
   /store PROMO1
   ```
3. Bot akan balas dengan link siap-share, contoh:
   ```
   https://t.me/NamaBot?start=get_PROMO1
   ```
4. Bagikan link itu di channel/caption postingan kamu (seperti contoh
   tombol link di gambar yang kamu kirim). Siapa pun yang klik akan
   otomatis menerima media itu di chat pribadi mereka.

Kalau lupa link-nya, tinggal ketik `/link PROMO1` untuk dapat ulang tanpa
perlu simpan ulang medianya.

### Broadcast ke channel/grup
1. Reply pesan yang ingin disebar (boleh teks atau media) dengan:
   ```
   /broadcast
   ```
2. Bot akan mengirim salinan pesan itu ke semua `chat_id` yang ada di
   `TARGET_CHATS`, dan melaporkan berapa yang berhasil/gagal.

### Alur wajib-join
1. User klik link `?start=get_KODE`.
2. Bot cek: user sudah join semua chat di `REQUIRED_CHATS`?
   - **Belum** → bot balas dengan sapaan pakai nama depan user ("Hai
     [Nama]!"), penjelasan singkat, lalu tombol-tombol join tersusun
     2-per-baris + tombol **"🔄 COBA LAGI"** di bawahnya — persis pola
     yang lazim dipakai bot-bot serupa.
   - **Sudah** → bot langsung kirim medianya.
3. User klik "🔄 COBA LAGI" → bot verifikasi ulang lewat `getChatMember`.
   Kalau semua sudah lolos, bot edit pesan jadi "✅ Terverifikasi, [Nama]!
   Mengirim video..." lalu kirim medianya. Kalau masih ada yang belum
   join, muncul notifikasi kecil (alert) dan tombolnya tetap tampil untuk
   dicoba lagi.

Catatan: fitur ini butuh bot jadi **admin** di channel/grup yang wajib
di-join (bot biasa tidak bisa lihat status member orang lain).

## 4. Ide pengembangan lanjutan
- **Tombol menarik**: tambahkan `InlineKeyboardMarkup` di pesan broadcast
  supaya ada tombol "🔥 Ambil Sekarang" yang langsung mengarah ke deep link.
- **Auto-hapus**: pakai `context.job_queue` untuk hapus pesan media di chat
  user setelah beberapa menit (fitur "self-destruct").
- **Statistik klik**: tambah kolom `click_count` di tabel `media` supaya
  admin tahu link mana yang paling laris.
- **Multi-file per kode**: satu kode bisa terhubung ke beberapa file_id,
  bot kirim semuanya sekaligus (album).
- **Hosting**: bisa deploy ke Railway/Render seperti bot Telegram kamu
  yang lain, tinggal set environment variable untuk token & admin id
  supaya tidak hardcode di config.py.

## 5. Catatan penting
- `file_id` Telegram terikat ke bot yang mengunggahnya pertama kali —
  jika token bot diganti, media perlu di-upload ulang.
- Data media sekarang disimpan di **Postgres**, bukan file lokal —
  jadi aman kalau Railway redeploy/restart service (data tidak hilang
  seperti SQLite di filesystem sementara).
- Pastikan konten yang dibagikan sesuai dengan Ketentuan Layanan Telegram.

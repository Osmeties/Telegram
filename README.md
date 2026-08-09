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
   /genlink PROMO1
   ```
   (atau `/store PROMO1` — dua-duanya sama persis, `/genlink` cuma alias
   yang lebih deskriptif)
3. Bot akan balas dengan link siap-share, contoh:
   ```
   https://t.me/NamaBot?start=get_PROMO1
   ```
4. Bagikan link itu di channel/caption postingan kamu. Siapa pun yang
   klik akan otomatis menerima media itu di chat pribadi mereka
   (setelah lolos cek wajib-join, kalau diaktifkan).

Kalau lupa link-nya, tinggal ketik `/link PROMO1` untuk dapat ulang tanpa
perlu simpan ulang medianya.

### Menyimpan banyak media sekaligus (album/batch) dalam 1 kode
Kalau kamu kirim **beberapa foto/video sebagai 1 album** (dipilih sekaligus
lalu dikirim bareng), bot otomatis merekam semua anggota album itu. Reply
`/genlink KODE` ke **salah satu** foto di album tersebut — tidak perlu reply
ke semuanya satu-satu:
```
/genlink LIBURAN1
```
Bot akan simpan semua foto/video di album itu di bawah 1 kode yang sama, dan
saat user klik link-nya, semuanya dikirim sekaligus sebagai 1 album juga
(lewat `send_media_group`). Kalau kamu reply ke foto/video tunggal (bukan
bagian dari album), perilakunya sama seperti sebelumnya — cuma 1 media yang
tersimpan.

Catatan: buffer album ini cuma tersimpan di memori bot selama 5 menit sejak
pesan pertama masuk, jadi jalankan `/genlink` tidak lama setelah kirim
albumnya. Kalau bot baru saja restart tepat setelah kamu kirim album (misal
karena redeploy), buffer-nya ikut hilang — kirim ulang albumnya kalau itu
terjadi.

### Menghapus media
```
/delmedia PROMO1
```
Menghapus media dengan kode itu dari database secara permanen. Setelah
dihapus, link deep-link `?start=get_PROMO1` yang sudah pernah dibagikan
tidak akan berfungsi lagi (user akan dapat pesan "konten tidak ditemukan").
Khusus admin.

### Menjelajah & cari media ("perpustakaan")
Kalau sudah banyak kode yang tersimpan dan susah diingat-ingat:

```
/listmedia
```
Menampilkan daftar semua media (kode, jumlah item, cuplikan caption, link,
tanggal upload), 10 per halaman, dengan tombol ⬅️/➡️ buat pindah halaman.
Urutannya terbaru dulu.

```
/cari promo
```
Cari media yang kodenya **atau** caption-nya mengandung kata kunci itu
(tidak case-sensitive), maksimal 20 hasil ditampilkan sekaligus. Berguna
kalau lupa kode persisnya tapi ingat sepotong isi caption-nya.

Keduanya khusus admin.

### Alur "upload video → posting link ke channel" (1 langkah)
Ini alur yang biasanya kamu mau: upload video sekali ke bot, lalu
**link-nya** (bukan videonya) yang muncul di channel, dibungkus tombol —
persis seperti contoh gambar "LENDIR GENZ" yang kamu kirim di awal.

Reply video/foto yang mau kamu posting dengan:
```
/postlink PROMO1 🔥 Tonton Sekarang
```

Bot akan otomatis:
1. Simpan videonya dengan kode `PROMO1` (sama seperti `/genlink`)
2. Bikin deep link `https://t.me/NamaBot?start=get_PROMO1`
3. Kirim pesan ke **semua channel/grup di `TARGET_CHATS`** berupa teks
   (pakai caption video kalau ada, atau teks default) + **tombol**
   `🔥 Tonton Sekarang` yang mengarah ke link itu

Jadi yang tampil di channel cuma teks + tombol — bukan videonya. Orang
yang klik tombol baru diarahkan ke bot, dicek wajib-join (kalau
`REQUIRED_CHATS` diisi), baru videonya dikirim ke chat pribadi mereka.

Kalau kamu mau posting ulang link yang **sama** ke channel lain tanpa
upload ulang videonya, tinggal jalankan `/postlink PROMO1 🔥 Tonton
Sekarang` lagi tanpa reply ke media apa pun — bot otomatis pakai video
yang sudah tersimpan dengan kode itu.

### Mengatur TARGET_CHATS / REQUIRED_CHATS langsung dari chat
Gak perlu lagi bolak-balik ke Railway Variables setiap mau ganti channel
tujuan broadcast atau channel wajib-join — admin bisa atur langsung:

- **`/setvars <KEY> <value>`** — set/ganti nilai. Contoh:
  ```
  /setvars TARGET_CHATS -1001111111111,-1002222222222
  ```
  ```
  /setvars REQUIRED_CHATS [{"chat_id": -1001111111111, "username": "namachannel", "invite_link": null, "label": "📢 Join Channel Utama"}]
  ```
- **`/delvars <KEY>`** — hapus nilai custom, balik pakai default dari
  Railway Variables. Contoh: `/delvars TARGET_CHATS`
- **`/getvars`** — lihat nilai yang sedang aktif sekarang (custom atau
  default).

Nilai dari `/setvars` disimpan di database dan **menimpa** nilai dari
Railway Variables selama belum di-`/delvars`. Cocok buat ganti-ganti
channel tanpa perlu akses dashboard Railway tiap saat.

### Cek bot masih hidup
`/ping` — bot balas dengan waktu respon dalam milidetik.

### Menu command yang berbeda untuk admin & member
Saat user ketik `/` di chat bot, menu yang muncul otomatis berbeda:
- **Member biasa** hanya melihat `/start` dan `/ping`.
- **Admin** (sesuai `ADMIN_IDS`) melihat semua command: `/genlink`,
  `/store`, `/link`, `/delmedia`, `/listmedia`, `/cari`, `/broadcast`,
  `/setvars`, `/delvars`, `/getvars`, `/ping`, `/start`.

Ini murni soal tampilan menu supaya rapi — semua command admin **tetap**
dicek lewat `is_admin()` di kode, jadi member yang tahu nama command-nya
dan coba ketik manual tetap akan ditolak.

Catatan: menu khusus admin baru bisa Telegram pasang kalau admin itu
sudah pernah `/start` bot ini minimal sekali (batasan dari Telegram,
bukan dari kode kita).

### Broadcast ke channel/grup
**Mode 1 — copy pesan yang sudah ada** (cocok kalau ada media/video):
```
/broadcast
```
Reply ke pesan yang ingin disebar (teks atau media). Bot mengirim salinannya
ke semua `chat_id` di `TARGET_CHATS`.

**Mode 2 — tulis langsung, tanpa reply** (cocok buat postingan teks promosi):
Ketik `/broadcast` diikuti isi postingan di baris-baris berikutnya — boleh
multi-baris, bold/underline dari toolbar Telegram tetap kepakai. Contoh:
```
/broadcast Judul Film

👉 Cara Menonton 👈
Tekan tombol Putar Video dibawah

Jangan Lupa
Subscribe dan Join Channel Dan Group Kami
Untuk Update Konten Kami Lainnya 🙏

▶️ Putar Video | https://t.me/NamaBot?start=get_KODE
```
Baris **terakhir** kalau formatnya `<teks tombol> | <url>` (URL wajib diawali
`http://`, `https://`, atau `tg://`) otomatis diubah jadi tombol inline, tidak
ikut tampil sebagai teks/link telanjang di postingan.

Catatan: mode 2 tidak bisa membawa media (foto/video) karena tidak ada pesan
yang di-reply — kalau butuh media, pakai Mode 1 lalu tambahkan baris tombol
yang sama di akhir command `/broadcast`-nya (reply + tulis caption baru +
baris tombol sekaligus, caption pesan asli akan ditimpa oleh caption baru
ini).

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

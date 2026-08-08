"""
Konfigurasi bot.

Di Railway, isi lewat tab "Variables" (bukan edit file ini langsung),
supaya token/id tidak ikut ke-commit ke repo. Untuk jalan di laptop,
boleh langsung ganti nilai default di bawah, atau bikin file .env.
"""

import os
import json

# Token dari @BotFather
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ISI_TOKEN_BOT_DI_SINI")

# Daftar user_id Telegram yang boleh menjalankan /store dan /broadcast
# Cara cek user_id sendiri: chat ke @userinfobot
# Di Railway: set variable ADMIN_IDS = "123456789,987654321"
_admin_ids_env = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in _admin_ids_env.split(",") if x.strip()] or [
    123456789,  # ganti dengan user_id admin kamu (kalau tidak pakai env var)
]

# Daftar chat_id channel/grup tujuan broadcast
# Untuk channel biasanya formatnya -100xxxxxxxxxx
# Bot HARUS sudah jadi admin di channel/grup tersebut
# Di Railway: set variable TARGET_CHATS = "-1001234567890,-1009876543210"
_target_chats_env = os.environ.get("TARGET_CHATS", "")
TARGET_CHATS = [int(x) for x in _target_chats_env.split(",") if x.strip()] or [
    -1001234567890,  # ganti dengan chat_id channel/grup kamu
]

# Channel/grup WAJIB di-join sebelum user bisa ambil konten dari deep link.
# Setiap entri butuh:
#   - chat_id  : id channel/grup (untuk cek status member via getChatMember)
#   - username : username publik (tanpa @) untuk bikin tombol "Join",
#                boleh None kalau grup/channel private (pakai invite_link saja)
#   - invite_link : link undangan (dipakai kalau grup private / tidak punya username)
#   - label    : teks tombol yang muncul ke user
#
# Bot HARUS jadi admin di setiap channel/grup ini supaya bisa cek member.
#
# Di Railway, kalau mau isi lewat env var, set REQUIRED_CHATS ke JSON string,
# contoh: [{"chat_id": -1001111111111, "username": "infovipredroom",
#           "invite_link": null, "label": "📢 Join Channel Utama"}]
_required_chats_env = os.environ.get("REQUIRED_CHATS", "")
if _required_chats_env:
    REQUIRED_CHATS = json.loads(_required_chats_env)
else:
    REQUIRED_CHATS = [
        {
            "chat_id": -1001111111111,      # ganti dengan chat_id channel utama
            "username": "infovipredroom",   # tanpa @, isi None kalau tidak ada
            "invite_link": None,            # isi kalau channel private
            "label": "📢 Join Channel Utama",
        },
        {
            "chat_id": -1002222222222,      # ganti dengan chat_id grup utama
            "username": None,
            "invite_link": "https://t.me/+xxxxxxxxxxxx",  # invite link grup private
            "label": "👥 Join Grup Utama",
        },
    ]

# DATABASE_URL dibaca langsung oleh db.py dari environment variable
# yang otomatis dibuat Railway saat kamu attach plugin Postgres.

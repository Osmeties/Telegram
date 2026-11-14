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

# Daftar chat_id channel/grup tujuan broadcast.
# Untuk channel biasanya formatnya -100xxxxxxxxxx
# Bot HARUS sudah jadi admin di channel/grup tersebut.
#
# Tiap entri punya "kind": "channel" atau "group" -- dipakai bot untuk milih
# thumbnail yang beda per tujuan (lihat /thumbch dan /thumbgrp di README).
# Kalau tidak butuh thumbnail beda-beda, isi "kind" apa saja (misal "channel")
# untuk semuanya, atau biarkan default di bawah.
#
# Di Railway, isi lewat env var TARGET_CHATS sebagai JSON string, contoh:
# TARGET_CHATS = [{"chat_id": -1001111111111, "kind": "channel"},
#                  {"chat_id": -1002222222222, "kind": "group"}]
_target_chats_env = os.environ.get("TARGET_CHATS", "")
if _target_chats_env:
    # Terima 2 format: JSON (format baru, dengan "kind") ATAU angka dipisah
    # koma (format lama, mis. "-1001,-1002"). Format lama dibaca apa adanya
    # (tanpa kind) supaya bot tidak crash saat start; bot.py yang
    # menormalkannya jadi dict dengan kind=None.
    try:
        TARGET_CHATS = json.loads(_target_chats_env)
    except json.JSONDecodeError:
        TARGET_CHATS = [int(x) for x in _target_chats_env.split(",") if x.strip()]
else:
    TARGET_CHATS = [
        {"chat_id": -1001234567890, "kind": "channel"},  # ganti dengan chat_id channel kamu
        {"chat_id": -1009876543210, "kind": "group"},    # ganti dengan chat_id grup kamu
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

# Chat_id channel/grup "brankas" -- salinan tiap media yang disimpan lewat
# bot (genlink/store/batchdone/postlink) otomatis di-forward ke sini juga,
# dan /vaultsync bisa backup semua kode lama yang belum ke-forward. Gunanya:
# file_id Telegram terikat ke bot yang upload pertama kali, jadi kalau bot
# ini suatu saat kena ban, file_id di database jadi mati -- tapi salinan di
# channel brankas (yang histori-nya disimpan Telegram, bukan bot) tetap bisa
# diakses manual lewat akun admin, lalu di-upload ulang ke bot baru.
#
# Bot WAJIB jadi admin di channel/grup ini, dan sebaiknya ada akun admin
# manusia juga di situ (bukan cuma bot) supaya tetap bisa diakses walau bot
# kena ban. Kosongkan / isi 0 kalau belum mau pakai fitur ini.
# Di Railway: set variable VAULT_CHAT_ID = "-1001112223334"
_vault_chat_id_env = os.environ.get("VAULT_CHAT_ID", "").strip()
VAULT_CHAT_ID = int(_vault_chat_id_env) if _vault_chat_id_env else None

# DATABASE_URL dibaca langsung oleh db.py dari environment variable
# yang otomatis dibuat Railway saat kamu attach plugin Postgres.

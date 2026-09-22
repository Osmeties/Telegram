"""
Bot Telegram: Broadcast + Kirim Media via Deep Link + Wajib-Join
==================================================================

Fitur:
1. /store atau /genlink -> admin reply ke sebuah media (foto/video/dokumen)
              dengan "/store <kode>" untuk menyimpan media itu dengan kode
              unik, sekaligus dapat deep link siap-pakai.
2. /link   -> admin ketik "/link <kode>" untuk ambil ulang deep link
              tanpa perlu simpan ulang medianya.
3. Saat user klik deep link (payload get_<kode>), bot cek wajib-join
   (REQUIRED_CHATS), lalu kirim media yang tersimpan ke chat pribadi user.
4. /broadcast -> admin reply ke sebuah pesan (teks/media) untuk mengirim
              pesan itu ke semua channel/grup di TARGET_CHATS.
4b. /thumbch, /thumbgrp -> admin reply ke sebuah FOTO untuk set thumbnail
              berbeda ke channel vs grup (sesuai "kind" tiap entri di
              TARGET_CHATS), dipakai otomatis di /postlink atau /broadcast
              berikutnya lalu langsung ke-reset.
5. /setvars, /delvars, /getvars -> admin atur TARGET_CHATS & REQUIRED_CHATS
              langsung dari chat, tanpa perlu ubah Railway Variables.
              Nilai ini disimpan di database dan menimpa nilai default dari
              config.py/Railway Variables selama belum dihapus (/delvars).
6. /ping   -> cek bot masih hidup & seberapa cepat responnya.

Menu command yang muncul saat user ketik "/" DIBEDAKAN:
- User biasa hanya melihat /start dan /ping.
- Admin (ADMIN_IDS) melihat semua command di atas.
Ini murni soal tampilan menu; command admin tetap dicek is_admin() di kode,
jadi tidak bisa "ditembus" walau seseorang tahu nama command-nya.

Semua data (media & settings) disimpan di PostgreSQL (Railway) lewat db.py.
"""

import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from config import BOT_TOKEN, ADMIN_IDS, TARGET_CHATS, REQUIRED_CHATS

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Key yang boleh diatur lewat /setvars, /delvars, /getvars
KNOWN_VAR_KEYS = ["TARGET_CHATS", "REQUIRED_CHATS"]

PUBLIC_COMMANDS = [
    BotCommand("start", "Mulai bot"),
    BotCommand("ping", "Cek kecepatan respon bot"),
    BotCommand("cari", "Cari media berdasarkan kode/caption (maks 3x/hari)"),
]

ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("genlink", "Untuk membuat link fsub / konten"),
    BotCommand("postlink", "Upload media + langsung posting link ke channel"),
    BotCommand("thumbch", "Set thumbnail (reply foto) khusus utk channel, dipakai post berikutnya"),
    BotCommand("thumbgrp", "Set thumbnail (reply foto) khusus utk grup, dipakai post berikutnya"),
    BotCommand("store", "Alias dari /genlink"),
    BotCommand("link", "Ambil ulang link dari kode yang sudah ada"),
    BotCommand("batchstart", "Mulai kumpulkan banyak media (sampai ratusan) ke 1 kode"),
    BotCommand("batchstatus", "Lihat jumlah media yang sudah terkumpul di batch"),
    BotCommand("batchdone", "Selesaikan batch & simpan semua media ke kode"),
    BotCommand("batchcancel", "Batalkan batch yang sedang berjalan"),
    BotCommand("delmedia", "Hapus media tersimpan berdasarkan kode"),
    BotCommand("listmedia", "Lihat daftar semua media tersimpan"),
    BotCommand("setvars", "Untuk mengatur variabel"),
    BotCommand("delvars", "Untuk menghapus variabel"),
    BotCommand("getvars", "Untuk mendapatkan daftar variabel"),
    BotCommand("broadcast", "Untuk mengirimkan pesan ke channel/grup"),
    BotCommand("jadwal", "Jadwalkan broadcast (posting terjadwal)"),
    BotCommand("jadwallist", "Lihat daftar broadcast terjadwal"),
    BotCommand("jadwalbatal", "Batalkan broadcast terjadwal"),
]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------------------------------------------------------------
# Nilai efektif TARGET_CHATS / REQUIRED_CHATS (DB override > config/env)
# ---------------------------------------------------------------------
def _normalize_target_chats(raw: list) -> list[dict]:
    """Terima TARGET_CHATS dalam bentuk apa pun -- list of dict (format baru),
    list of int (format lama / belum di-migrasi), atau kosong -- dan SELALU
    kembalikan list of dict {"chat_id": int, "kind": "channel"/"group"/None}."""
    if not raw:
        return []
    if isinstance(raw[0], dict):
        return raw
    return [{"chat_id": int(c), "kind": None} for c in raw]


async def get_target_chats() -> list[dict]:
    """Kembalikan TARGET_CHATS efektif, selalu sebagai list of dict
    {"chat_id": int, "kind": "channel"/"group"/None}. Mendukung semua bentuk
    value yang mungkin ada -- baik dari config.py/env var (kalau belum pernah
    di-/setvars) MAUPUN dari DB (kalau sudah pernah di-/setvars):
    - JSON list of dict (format baru): [{"chat_id":.., "kind":..}, ...]
    - JSON list of int / list of int polos (format lama / belum di-migrasi)
    - String lama dipisah koma: "-100111,-100222" (kind jadi None utk semua,
      artinya tidak dapat thumbnail custom -- tetap jalan seperti sebelumnya)
    """
    val = await db.get_setting("TARGET_CHATS")
    if not val:
        return _normalize_target_chats(TARGET_CHATS)

    try:
        parsed = json.loads(val)
    except json.JSONDecodeError:
        parsed = None

    if parsed is not None:
        return _normalize_target_chats(parsed)

    return [{"chat_id": int(x), "kind": None} for x in val.split(",") if x.strip()]


async def get_required_chats() -> list[dict]:
    val = await db.get_setting("REQUIRED_CHATS")
    if val:
        return json.loads(val)
    return REQUIRED_CHATS


# ---------------------------------------------------------------------
# Cek wajib-join
# ---------------------------------------------------------------------
async def get_missing_chats(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    """Kembalikan daftar REQUIRED_CHATS (efektif) yang BELUM di-join user."""
    required = await get_required_chats()
    missing = []
    for chat in required:
        try:
            member = await context.bot.get_chat_member(chat["chat_id"], user_id)
            if member.status in ("left", "kicked"):
                missing.append(chat)
        except TelegramError as e:
            # Kalau bot gagal cek (misal belum admin di sana), anggap "belum join"
            # supaya tidak diam-diam meloloskan orang.
            logger.warning("Gagal cek member untuk %s: %s", chat["chat_id"], e)
            missing.append(chat)
    return missing


def build_join_keyboard(missing: list[dict], code: str) -> InlineKeyboardMarkup:
    """Susun tombol 2 per baris (join channel/grup) + 1 baris 'Coba Lagi' di bawah,
    meniru tampilan umum bot-bot serupa."""
    join_buttons = []
    for chat in missing:
        if chat.get("username"):
            url = f"https://t.me/{chat['username']}"
        elif chat.get("invite_link"):
            url = chat["invite_link"]
        else:
            continue
        join_buttons.append(InlineKeyboardButton(chat["label"], url=url))

    rows = [join_buttons[i:i + 2] for i in range(0, len(join_buttons), 2)]
    rows.append([InlineKeyboardButton("🔄 COBA LAGI", callback_data=f"checkjoin_{code}")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------
# Thumbnail per-tujuan (channel vs grup) untuk /postlink & /broadcast.
#
# Alurnya: admin reply sebuah FOTO dengan /thumbch (utk channel) dan/atau
# /thumbgrp (utk grup) SEBELUM jalanin /postlink atau /broadcast. Command
# posting berikutnya otomatis pakai thumbnail itu (dicocokkan lewat "kind"
# tiap entri TARGET_CHATS), lalu langsung di-reset -- jadi harus di-set
# ulang tiap mau posting, sesuai maunya.
#
# Catatan teknis: utk /postlink (post teks+tombol), thumbnail dipakai
# sebagai FOTO utama post itu -- boleh pakai file_id apa adanya. Tapi utk
# /broadcast yang bawa video/dokumen/animasi, thumbnail itu beneran
# "preview frame" dari Bot API, dan Bot API MEWAJIBKAN thumbnail di-upload
# fresh (tidak boleh reuse file_id) -- makanya ada _download_thumb_bytes.
# ---------------------------------------------------------------------
PENDING_THUMB_TTL = 3600  # detik; kalau lupa dipakai, basi setelah 1 jam


def _get_pending_thumb_entry(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict:
    store = context.bot_data.setdefault("pending_thumbs", {})
    entry = store.get(user_id)
    if entry is not None and time.time() - entry["ts"] > PENDING_THUMB_TTL:
        entry = None
    if entry is None:
        entry = {"channel": None, "group": None, "ts": time.time()}
        store[user_id] = entry
    return entry


def pop_pending_thumbs(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict:
    """Ambil & langsung hapus sesi thumbnail admin ini (dipanggil pas
    /postlink atau /broadcast benar-benar mengirim). Basi -> dianggap kosong."""
    store = context.bot_data.setdefault("pending_thumbs", {})
    entry = store.pop(user_id, None)
    if entry is None or time.time() - entry["ts"] > PENDING_THUMB_TTL:
        return {"channel": None, "group": None}
    return entry


async def _set_pending_thumb(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, label: str) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    replied = update.message.reply_to_message
    if replied is None or not replied.photo:
        await update.message.reply_text(
            f"Reply command ini ke sebuah FOTO untuk dijadikan thumbnail {label}."
        )
        return

    entry = _get_pending_thumb_entry(context, user.id)
    entry[kind] = replied.photo[-1].file_id
    entry["ts"] = time.time()

    other_kind = "group" if kind == "channel" else "channel"
    other_label = "grup" if kind == "channel" else "channel"
    other_status = "✅ sudah di-set" if entry[other_kind] else "belum di-set"
    await update.message.reply_text(
        f"✅ Thumbnail {label} disimpan.\nThumbnail {other_label}: {other_status}.\n\n"
        "Keduanya otomatis kepakai di /postlink atau /broadcast berikutnya, "
        "lalu ke-reset (harus di-set ulang tiap mau posting)."
    )


async def thumbch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_pending_thumb(update, context, "channel", "channel")


async def thumbgrp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_pending_thumb(update, context, "group", "grup")


async def _download_thumb_bytes(context: ContextTypes.DEFAULT_TYPE, file_id: str | None) -> bytes | None:
    """Download foto thumbnail jadi bytes. WAJIB dilakukan fresh tiap broadcast
    karena Bot API tidak izinkan reuse file_id langsung sebagai parameter
    'thumbnail' (beda dengan parameter media utama yang boleh reuse file_id)."""
    if not file_id:
        return None
    try:
        tg_file = await context.bot.get_file(file_id)
        return bytes(await tg_file.download_as_bytearray())
    except TelegramError as e:
        logger.warning("Gagal download thumbnail %s: %s", file_id, e)
        return None


PREVIEW_THUMB_MEDIA_TYPES = ("video", "document", "animation")


async def _send_broadcast_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    kind: str | None,
    thumbs: dict,
    *,
    media_type: str | None,
    media_file_id: str | None,
    source_chat_id: int | None,
    source_message_id: int | None,
    caption_md: str | None,
    keyboard: InlineKeyboardMarkup | None,
    thumb_bytes_cache: dict,
) -> None:
    """Kirim 1 pesan broadcast ke 1 chat, terapkan thumbnail per-kind (channel
    vs group) kalau relevan. Dipakai bareng oleh /broadcast (langsung) dan
    run_due_scheduled_broadcasts (/jadwal, tertunda) supaya logikanya sama
    persis, tidak dobel kode.

    - source_message_id None -> mode teks polos, tidak ada pesan sumber sama
      sekali (caption_md WAJIB diisi di kasus ini).
    - media_type "photo" + ada thumbnail utk kind ini -> foto DIGANTI
      sepenuhnya pakai thumbnail itu.
    - media_type video/document/animation + ada thumbnail -> file aslinya
      tetap sama, cuma PREVIEW-nya yang diganti (butuh download bytes fresh).
    - Selain itu (tidak ada thumbnail relevan, atau tipe media tidak
      didukung) -> copy_message apa adanya dari source_chat_id/message_id,
      opsional timpa caption kalau caption_md diisi.

    thumb_bytes_cache: dict mutable yg dipakai LINTAS PANGGILAN (1 broadcast
    ke banyak chat) supaya video/dokumen/animasi cuma didownload sekali per
    kind, bukan re-download tiap chat_id.
    """
    thumb_file_id = thumbs.get(kind) if kind else None

    if source_message_id is None:
        # Mode teks polos: tidak ada apa pun yang bisa di-copy.
        if thumb_file_id:
            await context.bot.send_photo(
                chat_id, photo=thumb_file_id, caption=caption_md or "",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard,
            )
        else:
            await context.bot.send_message(
                chat_id, caption_md or "", parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard, disable_web_page_preview=True,
            )
        return

    if thumb_file_id and media_type == "photo" and media_file_id:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=thumb_file_id,
            caption=caption_md or None,
            parse_mode=ParseMode.MARKDOWN_V2 if caption_md else None,
            reply_markup=keyboard,
        )
        return

    if thumb_file_id and media_type in PREVIEW_THUMB_MEDIA_TYPES and media_file_id:
        if kind not in thumb_bytes_cache:
            thumb_bytes_cache[kind] = await _download_thumb_bytes(context, thumb_file_id)
        thumb_bytes = thumb_bytes_cache[kind]
        if thumb_bytes:
            sender = {
                "video": context.bot.send_video,
                "document": context.bot.send_document,
                "animation": context.bot.send_animation,
            }[media_type]
            await sender(
                chat_id=chat_id,
                **{media_type: media_file_id},
                thumbnail=thumb_bytes,
                caption=caption_md or None,
                parse_mode=ParseMode.MARKDOWN_V2 if caption_md else None,
                reply_markup=keyboard,
            )
            return
        # Gagal download thumbnail -> lanjut ke fallback copy_message di bawah,
        # jangan sampai broadcast gagal total gara-gara thumbnail doang.

    await context.bot.copy_message(
        chat_id=chat_id,
        from_chat_id=source_chat_id,
        message_id=source_message_id,
        caption=caption_md or None,
        parse_mode=ParseMode.MARKDOWN_V2 if caption_md else None,
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------
# Dukungan batch/album: kalau admin kirim beberapa foto/video sekaligus
# sebagai 1 album, Telegram mengirimnya sebagai pesan terpisah yang cuma
# ditandai media_group_id yang sama — reply command cuma nempel ke 1 dari
# pesan itu. Jadi kita "rekam" tiap pesan album yang masuk di sini, supaya
# /store, /genlink, /postlink bisa ambil semua anggotanya sekaligus.
#
# Catatan: Telegram sendiri MEMBATASI 1 album yang dikirim user ke maks 10
# item per pesan album (batasan dari Telegram, bukan dari bot ini). Jadi
# untuk kumpulkan lebih banyak dari itu (misal 200-300 media) di 1 kode,
# dipakai sesi /batchstart di bawah: admin boleh kirim berkali-kali (album
# demi album, atau satuan) sampai kode itu selesai lalu ditutup dengan
# /batchdone.
# ---------------------------------------------------------------------
MEDIA_GROUP_TTL = 300  # detik; buffer lama otomatis dibuang biar tidak numpuk

# Batas jumlah media per kode. Telegram mengirim media group maks 10 per
# pesan, jadi angka besar di sini otomatis dipecah 10-10 saat dikirim
# (lihat deliver_media). Angka ini cuma jaga-jaga supaya 1 kode tidak
# kebablasan jadi ribuan item.
MAX_BATCH_ITEMS = 300
BATCH_SESSION_TTL = 3600  # detik; sesi /batchstart yang lupa ditutup otomatis basi setelah 1 jam


def extract_media(message) -> tuple[str | None, str | None]:
    """Ambil (file_id, media_type) dari 1 Message, atau (None, None) kalau
    tipenya tidak didukung."""
    if message.photo:
        return message.photo[-1].file_id, "photo"
    if message.video:
        return message.video.file_id, "video"
    if message.document:
        return message.document.file_id, "document"
    if message.animation:
        return message.animation.file_id, "animation"
    return None, None


def get_batch_session(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict | None:
    """Ambil sesi /batchstart aktif milik admin ini, atau None kalau tidak ada
    / sudah basi (otomatis dibuang kalau basi)."""
    sessions = context.bot_data.setdefault("batch_sessions", {})
    session = sessions.get(user_id)
    if session is None:
        return None
    if time.time() - session["ts"] > BATCH_SESSION_TTL:
        del sessions[user_id]
        return None
    return session


async def capture_batch_media(message, context: ContextTypes.DEFAULT_TYPE, session: dict) -> None:
    """Tampung 1 pesan media ke sesi /batchstart yang sedang aktif -- ini
    yang bikin 1 kode bisa punya ratusan item walau Telegram sendiri cuma
    izinkan maks 10 item per pesan album."""
    file_id, media_type = extract_media(message)
    if file_id is None:
        return

    if len(session["items"]) >= MAX_BATCH_ITEMS:
        # Cuma kasih tahu sekali biar tidak spam admin kalau dia masih
        # ngirim lebih banyak lagi setelah limit tercapai.
        if not session.get("limit_warned"):
            session["limit_warned"] = True
            await message.reply_text(
                f"⚠️ Batch sudah mencapai batas {MAX_BATCH_ITEMS} media. "
                "Media ini TIDAK ditambahkan. Jalankan /batchdone untuk "
                "menyimpan yang sudah terkumpul."
            )
        return

    session["items"].append({"file_id": file_id, "media_type": media_type})
    session["ts"] = time.time()
    if message.caption and not session["caption"]:
        session["caption"] = message.caption


def capture_album(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rekam tiap pesan media dari admin yang merupakan bagian dari album
    (media_group_id sama) ke buffer sementara di bot_data."""
    if message.media_group_id is None:
        return

    file_id, media_type = extract_media(message)
    if file_id is None:
        return

    groups = context.bot_data.setdefault("media_groups", {})

    now = time.time()
    for gid in [g for g, v in groups.items() if now - v["ts"] > MEDIA_GROUP_TTL]:
        del groups[gid]

    group = groups.setdefault(message.media_group_id, {"items": [], "caption": None, "ts": now})
    group["items"].append({"file_id": file_id, "media_type": media_type})
    group["ts"] = now
    if message.caption:
        group["caption"] = message.caption


async def handle_admin_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Router untuk tiap pesan media yang masuk dari admin: kalau ada sesi
    /batchstart aktif, tampung ke situ; kalau tidak, jalankan buffer album
    biasa (dipakai /genlink, /store, /postlink)."""
    message = update.message
    if message is None:
        return
    user = message.from_user
    if user is None or not is_admin(user.id):
        return

    session = get_batch_session(context, user.id)
    if session is not None:
        await capture_batch_media(message, context, session)
        return

    capture_album(message, context)


# ---------------------------------------------------------------------
# /batchstart, /batchstatus, /batchdone, /batchcancel
# Alur kumpul media dalam jumlah besar (sampai MAX_BATCH_ITEMS) ke 1 kode,
# lintas beberapa kali kirim (boleh campur album & satuan, boleh dari
# beberapa kali forward), baru disimpan sekaligus lewat /batchdone.
# ---------------------------------------------------------------------
async def batchstart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    if not context.args:
        await update.message.reply_text("Format: /batchstart <kode>")
        return

    code = context.args[0]
    sessions = context.bot_data.setdefault("batch_sessions", {})
    if user.id in sessions:
        await update.message.reply_text(
            f"Sudah ada batch aktif untuk kode '{sessions[user.id]['code']}' "
            f"({len(sessions[user.id]['items'])} media terkumpul).\n"
            "Selesaikan dulu dengan /batchdone atau batalkan dengan /batchcancel "
            "sebelum mulai batch baru."
        )
        return

    sessions[user.id] = {"code": code, "items": [], "caption": None, "ts": time.time(), "limit_warned": False}
    await update.message.reply_text(
        f"📦 Batch dimulai untuk kode '{code}'.\n\n"
        "Sekarang kirim/forward media (foto/video/dokumen/gif) ke bot ini "
        "sebanyak yang kamu mau — boleh berkali-kali, boleh dalam bentuk "
        f"album ataupun satuan, sampai maks {MAX_BATCH_ITEMS} media.\n\n"
        "Cek progres dengan /batchstatus.\n"
        "Setelah semua terkirim, tutup dengan /batchdone.\n"
        f"(Batch otomatis basi kalau didiamkan lebih dari {BATCH_SESSION_TTL // 60} menit.)"
    )


async def batchstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    session = get_batch_session(context, user.id)
    if session is None:
        await update.message.reply_text("Tidak ada batch aktif. Mulai dengan /batchstart <kode>.")
        return

    await update.message.reply_text(
        f"📦 Batch '{session['code']}': {len(session['items'])} media terkumpul.\n"
        "Kirim lagi kalau belum selesai, atau /batchdone untuk menyimpan."
    )


async def batchcancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    sessions = context.bot_data.setdefault("batch_sessions", {})
    session = sessions.pop(user.id, None)
    if session is None:
        await update.message.reply_text("Tidak ada batch aktif.")
        return

    await update.message.reply_text(
        f"❌ Batch '{session['code']}' dibatalkan ({len(session['items'])} media dibuang, tidak disimpan)."
    )


async def batchdone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    sessions = context.bot_data.setdefault("batch_sessions", {})
    session = sessions.get(user.id)
    if session is None:
        await update.message.reply_text("Tidak ada batch aktif. Mulai dengan /batchstart <kode>.")
        return

    if not session["items"]:
        del sessions[user.id]
        await update.message.reply_text("Batch dibatalkan otomatis — tidak ada media yang terkumpul.")
        return

    code = session["code"]
    await db.save_media(code, session["items"], session["caption"])
    del sessions[user.id]

    bot_username = (await context.bot.get_me()).username
    deep_link = f"https://t.me/{bot_username}?start=get_{code}"
    await update.message.reply_text(
        f"✅ Batch selesai. Tersimpan {len(session['items'])} media dengan kode: {code}\n"
        f"Link siap pakai:\n{deep_link}\n\n"
        "(Saat dikirim ke user, otomatis dipecah jadi beberapa album 10-10 "
        "karena batasan Telegram.)"
    )


def get_replied_media_items(replied, context: ContextTypes.DEFAULT_TYPE) -> tuple[list[dict], str]:
    """Kalau pesan yang di-reply itu bagian dari album yang sudah tertangkap
    capture_album, kembalikan SEMUA anggota album itu. Kalau bukan album (atau
    belum tertangkap), kembalikan 1 item dari pesan itu sendiri saja.
    Return (items, caption)."""
    if replied.media_group_id:
        groups = context.bot_data.get("media_groups", {})
        group = groups.get(replied.media_group_id)
        if group and group["items"]:
            caption = group["caption"] or (replied.caption or "")
            return group["items"], caption

    file_id, media_type = extract_media(replied)
    if file_id is None:
        return [], ""
    return [{"file_id": file_id, "media_type": media_type}], (replied.caption or "")


# ---------------------------------------------------------------------
# Proteksi konten: media yang dikirim ke user (deliver_media) dipasangi
# protect_content (Telegram menonaktifkan tombol forward & "Simpan ke
# Galeri" di client -- ini fitur resmi Bot API 5.5+, bukan hack) dan
# dijadwalkan otomatis terhapus dari chat setelah AUTO_DELETE_SECONDS.
#
# PENTING - batasannya, biar realistis:
# - protect_content mencegah forward & simpan LEWAT TELEGRAM. Tidak
#   mencegah screenshot atau screen-recording di device penerima.
# - Auto-hapus butuh JobQueue (extra "python-telegram-bot[job-queue]" di
#   requirements.txt). Kalau bot restart (misal redeploy Railway) SAAT
#   masih dalam window 2 jam, jadwal hapus yang belum sempat jalan ikut
#   hilang (media jadi tidak kehapus tepat waktu untuk kasus itu saja --
#   bukan bug fatal, cuma keterbatasan JobQueue yang disimpan di memori).
# ---------------------------------------------------------------------
AUTO_DELETE_SECONDS = 2 * 60 * 60  # 2 jam
PROTECT_CONTENT = True

WIB = timezone(timedelta(hours=7))

# Jendela waktu di mana media dikirim TANPA proteksi (boleh forward/simpan).
# Ditentukan dari jam BOT MENGIRIM medianya, bukan jam user menontonnya --
# sekali terkirim tanpa proteksi karena kebetulan dalam jendela ini,
# statusnya "boleh forward" sampai auto-hapus 2 jam nanti, tidak berubah
# lagi walau jam sudah lewat dari jendela. Auto-hapus 2 jam TETAP berjalan
# biarpun dikirim di jendela ini (proteksi & auto-hapus itu 2 hal terpisah).
UNLOCK_WINDOW_START = dt_time(20, 0)
UNLOCK_WINDOW_END = dt_time(21, 0)


def is_in_unlock_window() -> bool:
    return UNLOCK_WINDOW_START <= datetime.now(WIB).time() < UNLOCK_WINDOW_END


async def _delete_scheduled_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
    except TelegramError as e:
        # Wajar gagal kalau user sudah duluan hapus manual, atau blokir bot
        logger.info("Auto-hapus dilewati utk pesan %s di %s: %s", data["message_id"], data["chat_id"], e)


def schedule_auto_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_ids: list[int]) -> None:
    if not message_ids:
        return
    if context.job_queue is None:
        logger.warning(
            "job_queue tidak aktif -- pastikan 'python-telegram-bot[job-queue]' "
            "terpasang (lihat requirements.txt). Auto-hapus TIDAK berjalan kali ini."
        )
        return
    for mid in message_ids:
        context.job_queue.run_once(
            _delete_scheduled_message,
            when=AUTO_DELETE_SECONDS,
            data={"chat_id": chat_id, "message_id": mid},
            name=f"autodel_{chat_id}_{mid}",
        )


async def deliver_media(chat_id: int, code: str, context: ContextTypes.DEFAULT_TYPE, requester_id: int) -> bool:
    """Kirim media untuk kode tsb. Return True kalau berhasil ketemu & terkirim.
    Media diproteksi (protect_content, tidak bisa di-forward/disimpan) dan
    otomatis dihapus setelah AUTO_DELETE_SECONDS -- KECUALI kalau yang minta
    adalah admin (mis. lagi mendata/cek isi media, medianya tidak boleh
    hilang sendiri)."""
    row = await db.get_media(code)
    if row is None:
        await context.bot.send_message(chat_id, "Maaf, konten tidak ditemukan atau sudah kedaluwarsa.")
        return False

    items, caption = row["items"], row["caption"]
    sent_message_ids: list[int] = []
    # Admin selalu bebas proteksi. Selain admin, proteksi dilewati kalau
    # sedang di jendela waktu bebas (20:00-21:00 WIB) -- tapi auto-hapus
    # 2 jam tetap jalan terlepas dari jendela ini.
    protect = PROTECT_CONTENT and not is_admin(requester_id) and not is_in_unlock_window()
    skip_notice = is_admin(requester_id)

    send_map = {
        "photo": context.bot.send_photo,
        "video": context.bot.send_video,
        "document": context.bot.send_document,
        "animation": context.bot.send_animation,
    }

    # 1 media -> kirim seperti biasa.
    if len(items) == 1:
        media_type = items[0]["media_type"]
        sender = send_map.get(media_type)
        if sender is None:
            await context.bot.send_message(chat_id, "Tipe media tidak didukung.")
            return False
        msg = await sender(
            chat_id=chat_id,
            **{media_type: items[0]["file_id"]},
            caption=caption,
            protect_content=protect,
        )
        sent_message_ids.append(msg.message_id)

    else:
        # Lebih dari 1 media -> kirim sebagai album (media group), dipecah
        # 10-10 karena Telegram membatasi maks 10 item per pesan send_media_group
        # (jadi 200-300 media otomatis jadi 20-30 pesan album berturutan).
        # Catatan: Telegram cuma izinkan photo/video/document dicampur dalam 1
        # media group, "animation" (GIF) tidak bisa ikut di sana.
        media_input_map = {
            "photo": InputMediaPhoto,
            "video": InputMediaVideo,
            "document": InputMediaDocument,
        }
        GROUP_CHUNK_SIZE = 10
        DELAY_BETWEEN_CHUNKS = 1.5  # detik; jaga-jaga supaya tidak kena flood limit Telegram

        if all(item["media_type"] in media_input_map for item in items):
            chunks = [items[i:i + GROUP_CHUNK_SIZE] for i in range(0, len(items), GROUP_CHUNK_SIZE)]
            # send_media_group butuh minimal 2 item per pesan -- kalau potongan
            # terakhir cuma nyisa 1 (misal total 11, 21, dst.), "pinjam" 1 item
            # dari potongan sebelumnya (jadi 9 + 2) supaya tidak ada chunk
            # ber-isi 1 ataupun yang kelebihan dari 10.
            if len(chunks) > 1 and len(chunks[-1]) == 1:
                chunks[-1].insert(0, chunks[-2].pop())

            for chunk_idx, chunk in enumerate(chunks):
                media_group = [
                    media_input_map[item["media_type"]](
                        media=item["file_id"],
                        # Caption cuma boleh nempel di 1 item -- taruh di item
                        # pertama dari chunk pertama saja supaya tidak berulang
                        # di tiap potongan album.
                        caption=caption if (chunk_idx == 0 and i == 0) else None,
                    )
                    for i, item in enumerate(chunk)
                ]
                messages = await send_media_group_with_retry(
                    context, chat_id, media_group, protect_content=protect
                )
                sent_message_ids.extend(m.message_id for m in messages)
                if chunk_idx < len(chunks) - 1:
                    await asyncio.sleep(DELAY_BETWEEN_CHUNKS)

        else:
            # Fallback: ada tipe yang tidak didukung media group (misal ada GIF
            # tercampur) -> kirim satu-satu.
            for i, item in enumerate(items):
                sender = send_map.get(item["media_type"])
                if sender is None:
                    continue
                msg = await sender(
                    chat_id=chat_id,
                    **{item["media_type"]: item["file_id"]},
                    caption=caption if i == 0 else None,
                    protect_content=protect,
                )
                sent_message_ids.append(msg.message_id)

    if sent_message_ids and not skip_notice:
        jam = AUTO_DELETE_SECONDS / 3600
        jam_str = f"{jam:g}"  # "2" bukan "2.0"
        if protect:
            notice_text = (
                f"⏳ Media di atas otomatis terhapus dalam {jam_str} jam dan tidak bisa "
                "di-forward/disimpan. Tonton sekarang selagi masih ada ya."
            )
        else:
            # Sedang di jendela bebas (20:00-21:00 WIB) -> boleh forward/simpan,
            # tapi auto-hapus tetap jalan.
            notice_text = (
                f"⏳ Media di atas otomatis terhapus dalam {jam_str} jam. Lagi jendela "
                "bebas forward/simpan sekarang, jadi silakan disimpan kalau perlu."
            )
        notice = await context.bot.send_message(chat_id, notice_text)
        schedule_auto_delete(context, chat_id, sent_message_ids)
        schedule_auto_delete(context, chat_id, [notice.message_id])  # pesan info ini juga ikut kehapus

    return True


async def send_media_group_with_retry(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, media_group: list, protect_content: bool = False
) -> list:
    """send_media_group, tapi kalau Telegram balas 'Too Many Requests'
    (wajar kalau kirim puluhan album berturutan), tunggu sesuai retry_after
    lalu coba lagi sekali. Return daftar Message yang berhasil terkirim
    (dipakai buat menjadwalkan auto-hapus)."""
    try:
        return await context.bot.send_media_group(
            chat_id=chat_id, media=media_group, protect_content=protect_content
        )
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return await context.bot.send_media_group(
            chat_id=chat_id, media=media_group, protect_content=protect_content
        )


# ---------------------------------------------------------------------
# /start dengan deep-link payload
# ---------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args  # isi setelah "?start="
    user = update.effective_user
    name = user.first_name or "kamu"

    if not args:
        await update.message.reply_text(
            f"🤖 Hai {name}!\n\n"
            "Bot ini aktif. Gunakan link khusus yang dibagikan admin "
            "untuk mengambil konten."
        )
        return

    payload = args[0]

    if not payload.startswith("get_"):
        await update.message.reply_text("Payload tidak dikenali.")
        return

    code = payload[len("get_"):]

    if await db.get_media(code) is None:
        await update.message.reply_text("Maaf, konten tidak ditemukan atau sudah kedaluwarsa.")
        return

    missing = await get_missing_chats(user.id, context)

    if missing:
        await update.message.reply_text(
            f"🤖 Hai {name}!\n\n"
            "💡 Untuk mendapatkan video yang ingin kamu tonton, kamu harus "
            "join ke group/channel di bawah ini terlebih dahulu.\n\n"
            "✅ Setelah bergabung, silahkan klik tombol coba lagi.",
            reply_markup=build_join_keyboard(missing, code),
        )
        return

    await deliver_media(update.effective_chat.id, code, context, user.id)


# ---------------------------------------------------------------------
# Tombol "Saya sudah join, cek lagi"
# ---------------------------------------------------------------------
async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    code = query.data[len("checkjoin_"):]
    user = update.effective_user
    name = user.first_name or "kamu"

    missing = await get_missing_chats(user.id, context)

    if missing:
        await query.answer("Masih ada channel/grup yang belum kamu join 🙏", show_alert=True)
        await query.edit_message_reply_markup(reply_markup=build_join_keyboard(missing, code))
        return

    await query.answer("Verifikasi berhasil ✅")
    await query.edit_message_text(f"✅ Terverifikasi, {name}! Mengirim video...")
    await deliver_media(update.effective_chat.id, code, context, user.id)


# ---------------------------------------------------------------------
# /store <kode>  (reply ke media)
# ---------------------------------------------------------------------
async def store(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    cmd = update.message.text.split()[0].lstrip("/").split("@")[0]  # "store" atau "genlink"

    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    if not context.args:
        await update.message.reply_text(f"Format: /{cmd} <kode>  (reply ke media)")
        return

    code = context.args[0]
    replied = update.message.reply_to_message
    if replied is None:
        await update.message.reply_text("Reply perintah ini ke pesan media yang ingin disimpan.")
        return

    items, caption = get_replied_media_items(replied, context)
    if not items:
        await update.message.reply_text("Tipe media tidak didukung (gunakan foto/video/dokumen/gif).")
        return

    await db.save_media(code, items, caption)

    bot_username = (await context.bot.get_me()).username
    deep_link = f"https://t.me/{bot_username}?start=get_{code}"

    jumlah = f" ({len(items)} media)" if len(items) > 1 else ""
    await update.message.reply_text(
        f"Tersimpan dengan kode: {code}{jumlah}\nLink siap pakai:\n{deep_link}"
    )


# ---------------------------------------------------------------------
# /postlink <kode> <teks tombol>  (reply ke media)
# Upload media + langsung posting ke TARGET_CHATS dalam bentuk pesan
# bertombol yang mengarah ke deep link bot. Ini gabungan /genlink + /broadcast
# supaya alur "upload video -> post link di channel" bisa 1 langkah.
# ---------------------------------------------------------------------
async def postlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    parts = update.message.text.split(None, 2)  # ["/postlink", "kode", "teks tombol..."]
    if len(parts) < 3:
        await update.message.reply_text(
            "Format: /postlink <kode> <teks tombol>  (reply ke media)\n\n"
            "Contoh:\n"
            "/postlink PROMO1 🔥 Tonton Sekarang\n\n"
            "Kalau kode-nya sudah pernah dipakai sebelumnya (medianya sudah "
            "tersimpan), tidak perlu reply media lagi — command ini akan "
            "langsung posting link yang sudah ada."
        )
        return

    code = parts[1]
    button_label = parts[2].strip()
    replied = update.message.reply_to_message
    caption = None

    if replied is not None:
        items, cap = get_replied_media_items(replied, context)
        if items:
            caption = cap
            await db.save_media(code, items, caption)

    if await db.get_media(code) is None:
        await update.message.reply_text(
            "Kode ini belum punya media tersimpan. Reply command ini ke "
            "foto/video/dokumen yang mau diposting, atau simpan dulu lewat "
            "/genlink."
        )
        return

    bot_username = (await context.bot.get_me()).username
    deep_link = f"https://t.me/{bot_username}?start=get_{code}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(button_label, url=deep_link)]])
    post_text = caption or "🎬 Konten baru sudah tersedia! Klik tombol di bawah untuk menonton."

    target_chats = await get_target_chats()
    if not target_chats:
        await update.message.reply_text(
            "⚠️ TARGET_CHATS masih kosong, jadi tidak ada channel/grup tujuan.\n"
            "Isi dulu lewat /setvars TARGET_CHATS (lihat /setvars tanpa argumen "
            "untuk contoh formatnya)."
        )
        return

    thumbs = pop_pending_thumbs(context, user.id)

    sent, failed = 0, 0
    for chat in target_chats:
        chat_id, kind = chat["chat_id"], chat.get("kind")
        thumb_file_id = thumbs.get(kind) if kind else None
        try:
            if thumb_file_id:
                await context.bot.send_photo(
                    chat_id, photo=thumb_file_id, caption=post_text, reply_markup=keyboard
                )
            else:
                await context.bot.send_message(chat_id, post_text, reply_markup=keyboard)
            sent += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Gagal posting ke %s: %s", chat_id, e)
            failed += 1

    thumb_note = ""
    if thumbs.get("channel") or thumbs.get("group"):
        thumb_note = "\n\n(Thumbnail channel/grup yang di-set sudah terpakai & ke-reset.)"
    await update.message.reply_text(
        f"✅ Posting selesai. Sukses: {sent}, Gagal: {failed}\n\nLink: {deep_link}{thumb_note}"
    )


# ---------------------------------------------------------------------
# /link <kode>  -> ambil ulang link tanpa simpan ulang
# ---------------------------------------------------------------------
async def link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Format: /link <kode>")
        return
    code = context.args[0]
    if await db.get_media(code) is None:
        await update.message.reply_text("Kode tidak ditemukan.")
        return
    bot_username = (await context.bot.get_me()).username
    await update.message.reply_text(f"https://t.me/{bot_username}?start=get_{code}")


# ---------------------------------------------------------------------
# /delmedia <kode>  -> admin hapus media tersimpan
# ---------------------------------------------------------------------
async def delmedia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    if not context.args:
        await update.message.reply_text("Format: /delmedia <kode>")
        return

    code = context.args[0]
    deleted = await db.delete_media(code)

    if deleted:
        await update.message.reply_text(f"🗑️ Media dengan kode '{code}' berhasil dihapus.")
    else:
        await update.message.reply_text(f"Kode '{code}' tidak ditemukan (mungkin sudah terhapus).")


# ---------------------------------------------------------------------
# /listmedia [halaman] & /cari <kata kunci>  -> "perpustakaan" media
# ---------------------------------------------------------------------
MEDIA_PAGE_SIZE = 10
DAILY_SEARCH_LIMIT = 3
# WIB sudah didefinisikan di atas (dekat konstanta auto-hapus/proteksi)


def format_media_entry(row: dict, bot_username: str) -> str:
    caption = (row["caption"] or "").strip().replace("\n", " ")
    if len(caption) > 40:
        caption = caption[:40] + "…"
    caption = html.escape(caption)
    code_display = html.escape(row["code"])
    tanggal = row["created_at"].strftime("%d %b %Y")
    jumlah = f"{row['item_count']} media" if row["item_count"] > 1 else "1 media"
    link = f"https://t.me/{bot_username}?start=get_{row['code']}"
    caption_part = f' — "{caption}"' if caption else ""
    return f"🔑 <code>{code_display}</code> — {jumlah}{caption_part} ({tanggal})\n{link}"


async def build_media_page(context: ContextTypes.DEFAULT_TYPE, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    total = await db.count_media()
    if total == 0:
        return "Belum ada media yang tersimpan.", None

    total_pages = max(1, (total + MEDIA_PAGE_SIZE - 1) // MEDIA_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    rows = await db.list_media(offset=page * MEDIA_PAGE_SIZE, limit=MEDIA_PAGE_SIZE)

    bot_username = (await context.bot.get_me()).username
    body = "\n\n".join(format_media_entry(r, bot_username) for r in rows)
    header = f"📚 Daftar media ({total} total) — halaman {page + 1}/{total_pages}\n\n"
    text = header + body

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"listmedia_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Berikutnya ➡️", callback_data=f"listmedia_{page + 1}"))
    keyboard = InlineKeyboardMarkup([nav]) if nav else None
    return text, keyboard


async def listmedia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    text, keyboard = await build_media_page(context, page=0)
    await update.message.reply_text(
        text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def listmedia_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Perintah ini khusus admin.", show_alert=True)
        return
    await query.answer()

    page = int(query.data.split("_", 1)[1])
    text, keyboard = await build_media_page(context, page=page)
    await query.edit_message_text(
        text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cari(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    admin = is_admin(user.id)
    today = datetime.now(WIB).date()

    used = 0
    if not admin:
        used = await db.get_search_count(user.id, today)
        if used >= DAILY_SEARCH_LIMIT:
            await update.message.reply_text(
                f"⚠️ Kuota pencarian hari ini sudah habis ({DAILY_SEARCH_LIMIT}x/hari). "
                "Coba lagi besok setelah jam 00:00 WIB."
            )
            return

    if not context.args:
        await update.message.reply_text("Format: /cari <kata kunci>\nContoh: /cari promo1")
        return

    keyword = " ".join(context.args)
    rows = await db.search_media(keyword, limit=20)

    if not admin:
        await db.increment_search_count(user.id, today)
        sisa = DAILY_SEARCH_LIMIT - (used + 1)
        sisa_line = f"\n\n(sisa kuota pencarian hari ini: {sisa})"
    else:
        sisa_line = ""

    if not rows:
        await update.message.reply_text(f"Tidak ada media yang cocok dengan '{keyword}'.{sisa_line}")
        return

    bot_username = (await context.bot.get_me()).username
    body = "\n\n".join(format_media_entry(r, bot_username) for r in rows)
    suffix = " (maks 20 ditampilkan, persempit kata kuncinya kalau perlu)" if len(rows) == 20 else ""
    header = f"🔍 Hasil untuk '{html.escape(keyword)}' — {len(rows)} ditemukan{suffix}\n\n"

    await update.message.reply_text(
        header + body + sisa_line, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


# ---------------------------------------------------------------------
# /broadcast  (reply ke pesan yang mau disebar ke channel/grup terdaftar)
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# Parsing baris tombol utk /broadcast. Baris di paling bawah teks yang
# formatnya "<label> | <url>" dijadikan tombol. Bisa lebih dari 1 baris
# (jadi beberapa baris tombol bertumpuk), dan 1 baris bisa berisi lebih
# dari 1 tombol sekaligus (dipisah "||", jadi sebaris/side-by-side).
# Berhenti scan begitu ketemu baris yang bukan format tombol.
# ---------------------------------------------------------------------
MAX_BUTTON_ROWS = 10


# Alias warna Indonesia & Inggris -> nilai resmi Bot API 9.4+ (primary/success/danger)
BUTTON_STYLE_ALIASES = {
    "biru": "primary", "blue": "primary", "primary": "primary",
    "hijau": "success", "green": "success", "success": "success",
    "merah": "danger", "red": "danger", "danger": "danger",
}


def parse_button_lines(lines: list[str]) -> tuple[int, list[list[InlineKeyboardButton]]]:
    """Return (jumlah_baris_yang_dipertahankan_sbg_teks, rows_tombol).
    Tiap segmen tombol: "<label> | <url>" atau "<label> | <url> | <warna>"
    (warna: biru/hijau/merah, opsional — butuh Telegram client yang cukup baru,
    kalau tidak didukung tombol tampil normal tanpa warna)."""
    rows: list[list[InlineKeyboardButton]] = []
    idx = len(lines)
    while idx > 0 and len(rows) < MAX_BUTTON_ROWS:
        line = lines[idx - 1]
        if "|" not in line:
            break
        row: list[InlineKeyboardButton] = []
        ok = True
        for seg in (s.strip() for s in line.split("||")):
            parts = [p.strip() for p in seg.split("|")]
            if len(parts) < 2 or len(parts) > 3:
                ok = False
                break
            label, url = parts[0], parts[1]
            if not label or not url.startswith(("http://", "https://", "tg://")):
                ok = False
                break
            style = None
            if len(parts) == 3 and parts[2]:
                style = BUTTON_STYLE_ALIASES.get(parts[2].lower())
                if style is None:
                    ok = False
                    break
            kwargs = {"url": url}
            if style:
                kwargs["style"] = style
            row.append(InlineKeyboardButton(label, **kwargs))
        if not ok or not row:
            break
        rows.insert(0, row)
        idx -= 1
    return idx, rows


def button_rows_to_spec(rows: list[list[InlineKeyboardButton]]) -> list:
    """InlineKeyboardButton (objek, tidak bisa disimpan ke DB) -> list dict polos
    (bisa di-JSON-kan), buat disimpan sbg button_spec di scheduled_broadcasts."""
    return [
        [{"label": b.text, "url": b.url, "style": b.style} for b in row]
        for row in rows
    ]


def spec_to_keyboard(spec: list | None) -> InlineKeyboardMarkup | None:
    """Kebalikan dari button_rows_to_spec -- dipakai saat jadwal dieksekusi."""
    if not spec:
        return None
    rows = []
    for row in spec:
        btn_row = []
        for b in row:
            kwargs = {"url": b["url"]}
            if b.get("style"):
                kwargs["style"] = b["style"]
            btn_row.append(InlineKeyboardButton(b["label"], **kwargs))
        rows.append(btn_row)
    return InlineKeyboardMarkup(rows)


def parse_schedule_time(spec: str) -> tuple[datetime | None, str | None]:
    """Parse 'DD-MM-YYYY HH:MM' atau 'HH:MM' saja (WIB, otomatis hari
    ini/besok). Return (waktu_wib, pesan_error) -- salah satu None."""
    spec = spec.strip()
    now_wib = datetime.now(WIB)

    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})\s+(\d{2}):(\d{2})$", spec)
    if m:
        day, month, year, hour, minute = map(int, m.groups())
        try:
            run_at = datetime(year, month, day, hour, minute, tzinfo=WIB)
        except ValueError:
            return None, "Tanggal/jam tidak valid. Cek lagi angkanya."
    else:
        m2 = re.match(r"^(\d{2}):(\d{2})$", spec)
        if not m2:
            return None, (
                "Format waktu salah. Baris pertama harus salah satu:\n"
                "DD-MM-YYYY HH:MM (contoh: 30-08-2026 20:00)\n"
                "atau HH:MM saja (contoh: 20:00 -> otomatis hari ini, atau besok "
                "kalau jam segitu sudah lewat)"
            )
        hour, minute = map(int, m2.groups())
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None, "Jam tidak valid."
        run_at = now_wib.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run_at <= now_wib:
            run_at += timedelta(days=1)

    if run_at <= now_wib:
        return None, "Waktu itu sudah lewat. Pakai waktu di masa depan."

    return run_at, None


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    replied = update.message.reply_to_message

    # Ambil argumen setelah "/broadcast" dalam 2 bentuk: teks polos (buat cari
    # baris tombol & validasi URL apa adanya) dan versi MarkdownV2 (biar
    # bold/underline/dll dari toolbar Telegram tetap kepakai pas dikirim ulang).
    plain_full = update.message.text or ""
    md_full = update.message.text_markdown_v2 or plain_full

    plain_parts = plain_full.split(None, 1)
    md_parts = md_full.split(None, 1)
    plain_lines = (plain_parts[1] if len(plain_parts) > 1 else "").rstrip().splitlines()
    md_lines = (md_parts[1] if len(md_parts) > 1 else "").rstrip().splitlines()

    keep_idx, button_rows = parse_button_lines(plain_lines)

    if not button_rows and plain_lines and "|" in plain_lines[-1]:
        # baris terakhir ADA "|" (jelas maksudnya mau bikin tombol) tapi
        # format/URL-nya tidak valid -> kasih tahu, jangan diam-diam
        # dianggap teks biasa.
        await update.message.reply_text(
            "Format tombol salah. Pastikan tiap baris tombol:\n"
            "<teks tombol> | <url yang valid, diawali http/https>\n"
            "atau dengan warna: <teks tombol> | <url> | <biru/hijau/merah>\n"
            "(pisahkan dengan || kalau mau beberapa tombol sebaris)"
        )
        return

    removed_count = len(plain_lines) - keep_idx
    keyboard = InlineKeyboardMarkup(button_rows) if button_rows else None
    plain_lines = plain_lines[:keep_idx]
    if len(md_lines) == len(plain_lines) + removed_count:
        md_lines = md_lines[: len(md_lines) - removed_count] if removed_count else md_lines

    custom_text = "\n".join(md_lines).strip()

    if replied is None:
        # Mode compose langsung: isi postingan diketik setelah /broadcast, tidak
        # perlu reply ke pesan lain. Tidak bisa bawa media di mode ini.
        if not custom_text:
            await update.message.reply_text(
                "Reply perintah ini ke pesan yang ingin di-broadcast (kalau ada media), "
                "ATAU tulis langsung isi postingannya setelah /broadcast — boleh "
                "multi-baris & pakai bold/underline dari toolbar Telegram.\n\n"
                "Baris terakhir opsional buat tombol:\n<teks tombol> | <url>\n\n"
                "Contoh:\n/broadcast Judul Film\n\nKlik tombol di bawah buat nonton.\n"
                "▶️ Putar Video | https://t.me/NamaBot?start=get_KODE"
            )
            return

        thumbs = pop_pending_thumbs(context, user.id)
        thumb_bytes_cache: dict[str, bytes | None] = {}

        sent, failed = 0, 0
        target_chats = await get_target_chats()
        for chat in target_chats:
            chat_id, kind = chat["chat_id"], chat.get("kind")
            try:
                await _send_broadcast_message(
                    context, chat_id, kind, thumbs,
                    media_type=None, media_file_id=None,
                    source_chat_id=None, source_message_id=None,
                    caption_md=custom_text,
                    keyboard=keyboard,
                    thumb_bytes_cache=thumb_bytes_cache,
                )
                sent += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("Gagal broadcast ke %s: %s", chat_id, e)
                failed += 1

        await update.message.reply_text(f"Broadcast selesai. Sukses: {sent}, Gagal: {failed}")
        return

    # Mode reply: copy pesan yang di-reply (bawa media kalau ada), opsional
    # timpa caption-nya + pasang tombol dari argumen /broadcast di atas.
    # Logika thumbnail per-kind (channel vs group) ditangani di satu tempat
    # oleh _send_broadcast_message -- dipakai bareng dengan /jadwal juga.
    thumbs = pop_pending_thumbs(context, user.id)
    thumb_bytes_cache: dict[str, bytes | None] = {}

    replied_file_id, replied_media_type = extract_media(replied)
    caption_override_md = custom_text or (replied.caption_markdown_v2 or "")

    sent, failed = 0, 0
    target_chats = await get_target_chats()
    for chat in target_chats:
        chat_id, kind = chat["chat_id"], chat.get("kind")
        try:
            await _send_broadcast_message(
                context, chat_id, kind, thumbs,
                media_type=replied_media_type, media_file_id=replied_file_id,
                source_chat_id=replied.chat_id, source_message_id=replied.message_id,
                caption_md=caption_override_md,
                keyboard=keyboard,
                thumb_bytes_cache=thumb_bytes_cache,
            )
            sent += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Gagal broadcast ke %s: %s", chat_id, e)
            failed += 1

    thumb_note = ""
    if thumbs.get("channel") or thumbs.get("group"):
        thumb_note = "\n(Thumbnail channel/grup yang di-set sudah terpakai & ke-reset.)"
        if replied_media_type not in PREVIEW_THUMB_MEDIA_TYPES and replied_media_type != "photo":
            thumb_note += " (Tidak diterapkan karena tipe media ini tidak didukung.)"
    await update.message.reply_text(f"Broadcast selesai. Sukses: {sent}, Gagal: {failed}{thumb_note}")


# ---------------------------------------------------------------------
# /jadwal, /jadwallist, /jadwalbatal -> broadcast terjadwal (ala posting
# terjadwal Facebook). Disimpan di Postgres (scheduled_broadcasts), jadi
# tetap jalan walau bot sempat restart sebelum waktunya tiba -- job
# run_due_scheduled_broadcasts yang jalan tiap 30 detik yang mengeksekusi.
# ---------------------------------------------------------------------
async def jadwal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    replied = update.message.reply_to_message
    plain_full = update.message.text or ""
    md_full = update.message.text_markdown_v2 or plain_full

    plain_parts = plain_full.split(None, 1)
    if len(plain_parts) < 2:
        await update.message.reply_text(
            "Format:\n/jadwal <DD-MM-YYYY HH:MM atau HH:MM>\n<isi postingan...>\n\n"
            "Waktu WIB, harus di baris PERTAMA sendirian. Contoh:\n"
            "/jadwal 30-08-2026 20:00\nJudul Film\n\n"
            "▶️ Putar Video | https://t.me/NamaBot?start=get_KODE\n\n"
            "Atau reply ke media/pesan yang mau dijadwalkan (isi teks di bawah "
            "waktu jadi caption/override-nya, opsional)."
        )
        return

    plain_lines_all = plain_parts[1].rstrip().splitlines()
    md_parts = md_full.split(None, 1)
    md_lines_all = (md_parts[1] if len(md_parts) > 1 else "").rstrip().splitlines()

    time_line = plain_lines_all[0].strip()
    run_at_wib, err = parse_schedule_time(time_line)
    if err:
        await update.message.reply_text(err)
        return

    plain_lines = plain_lines_all[1:]
    md_lines = md_lines_all[1:] if len(md_lines_all) == len(plain_lines_all) else md_lines_all

    keep_idx, button_rows = parse_button_lines(plain_lines)
    if not button_rows and plain_lines and "|" in plain_lines[-1]:
        await update.message.reply_text(
            "Format tombol salah. Pastikan tiap baris tombol:\n"
            "<teks tombol> | <url yang valid, diawali http/https>\n"
            "atau dengan warna: <teks tombol> | <url> | <biru/hijau/merah>\n"
            "(pisahkan dengan || kalau mau beberapa tombol sebaris)"
        )
        return

    removed_count = len(plain_lines) - keep_idx
    plain_lines = plain_lines[:keep_idx]
    if len(md_lines) == len(plain_lines) + removed_count:
        md_lines = md_lines[: len(md_lines) - removed_count] if removed_count else md_lines

    custom_text = "\n".join(md_lines).strip()
    button_spec = button_rows_to_spec(button_rows) if button_rows else None

    if replied is None and not custom_text:
        await update.message.reply_text(
            "Reply ke pesan/media yang mau dijadwalkan, ATAU tulis isi postingannya "
            "di baris-baris setelah waktu (baris pertama)."
        )
        return

    thumbs = pop_pending_thumbs(context, user.id)
    media_file_id, media_type = extract_media(replied) if replied else (None, None)
    source_caption_md = (replied.caption_markdown_v2 or None) if replied else None

    source_chat_id = replied.chat_id if replied else None
    source_message_id = replied.message_id if replied else None
    run_at_utc = run_at_wib.astimezone(timezone.utc)

    sched_id = await db.create_scheduled_broadcast(
        created_by=user.id,
        run_at=run_at_utc,
        text=custom_text or None,
        button_spec=button_spec,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        thumb_channel_file_id=thumbs.get("channel"),
        thumb_group_file_id=thumbs.get("group"),
        media_file_id=media_file_id,
        media_type=media_type,
        source_caption=source_caption_md,
    )

    thumb_note = ""
    if thumbs.get("channel") or thumbs.get("group"):
        thumb_note = "\n(Thumbnail channel/grup yang di-set ikut tersimpan buat jadwal ini.)"
    await update.message.reply_text(
        f"📅 Terjadwal (#{sched_id}) — {run_at_wib.strftime('%d-%m-%Y %H:%M')} WIB.\n"
        f"Lihat semua: /jadwallist\n"
        f"Batalkan: /jadwalbatal {sched_id}{thumb_note}"
    )


def strip_markdown_v2_escapes(text: str) -> str:
    """Buang backslash escape MarkdownV2 (misal 'GEN\\-Z' -> 'GEN-Z') --
    dipakai buat cuplikan/preview teks polos (bukan buat dikirim ulang
    dengan parse_mode, cuma buat ditampilkan apa adanya)."""
    return re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!])", r"\1", text)


async def jadwallist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    rows = await db.list_pending_scheduled_broadcasts()
    if not rows:
        await update.message.reply_text("Tidak ada broadcast terjadwal saat ini.")
        return

    lines = []
    for r in rows:
        run_at_wib = r["run_at"].astimezone(WIB)
        raw = r["text"] or "(copy pesan/media yang di-reply)"
        snippet = strip_markdown_v2_escapes(raw).replace("\n", " ")
        if len(snippet) > 40:
            snippet = snippet[:40] + "…"
        lines.append(f"#{r['id']} — {run_at_wib.strftime('%d-%m-%Y %H:%M')} WIB — {snippet}")

    await update.message.reply_text(
        "📅 Broadcast terjadwal (belum jalan):\n\n" + "\n".join(lines) +
        "\n\nBatalkan salah satu: /jadwalbatal <id>"
    )


async def jadwalbatal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Format: /jadwalbatal <id>  (lihat id lewat /jadwallist)")
        return

    ok = await db.cancel_scheduled_broadcast(int(context.args[0]))
    if ok:
        await update.message.reply_text("✅ Jadwal dibatalkan.")
    else:
        await update.message.reply_text(
            "Tidak ketemu jadwal dengan id itu (mungkin sudah terkirim/dibatalkan/salah id)."
        )


async def run_due_scheduled_broadcasts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dipanggil berkala oleh JobQueue (lihat post_init). Cari jadwal yang
    waktunya sudah lewat & masih 'pending', lalu kirim persis seperti /broadcast
    (termasuk thumbnail per-kind kalau di-set pas /jadwal dibuat)."""
    due = await db.get_due_scheduled_broadcasts(datetime.now(timezone.utc))
    if not due:
        return

    target_chats = await get_target_chats()
    for item in due:
        keyboard = spec_to_keyboard(item["button_spec"])
        caption_md = item["text"] or item["source_caption"]
        thumbs = {"channel": item["thumb_channel_file_id"], "group": item["thumb_group_file_id"]}
        thumb_bytes_cache: dict[str, bytes | None] = {}

        sent, failed = 0, 0
        for chat in target_chats:
            chat_id, kind = chat["chat_id"], chat.get("kind")
            try:
                await _send_broadcast_message(
                    context, chat_id, kind, thumbs,
                    media_type=item["media_type"], media_file_id=item["media_file_id"],
                    source_chat_id=item["source_chat_id"], source_message_id=item["source_message_id"],
                    caption_md=caption_md,
                    keyboard=keyboard,
                    thumb_bytes_cache=thumb_bytes_cache,
                )
                sent += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("Gagal jalankan broadcast terjadwal #%s ke %s: %s", item["id"], chat_id, e)
                failed += 1

        await db.mark_scheduled_broadcast(item["id"], "sent" if sent > 0 else "failed")

        try:
            await context.bot.send_message(
                item["created_by"],
                f"📅 Broadcast terjadwal #{item['id']} sudah dijalankan. "
                f"Sukses: {sent}, Gagal: {failed}",
            )
        except TelegramError:
            pass  # wajar gagal kalau admin itu belum pernah /start bot ini


# ---------------------------------------------------------------------
# /setvars <KEY> <value>  -> admin atur TARGET_CHATS / REQUIRED_CHATS
# ---------------------------------------------------------------------
async def setvars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    parts = update.message.text.split(None, 2)  # ["/setvars", "KEY", "sisanya..."]
    if len(parts) < 3:
        await update.message.reply_text(
            "Format: /setvars <KEY> <value>\n\n"
            "Key yang didukung:\n\n"
            "• TARGET_CHATS — daftar chat_id tujuan /broadcast & /postlink, "
            "format JSON, tiap entri punya \"kind\": \"channel\" atau \"group\" "
            "(dipakai bot buat milih thumbnail lewat /thumbch & /thumbgrp).\n"
            "  Contoh:\n"
            '  /setvars TARGET_CHATS [{"chat_id": -1001111111111, "kind": '
            '"channel"}, {"chat_id": -1002222222222, "kind": "group"}]\n\n'
            "• REQUIRED_CHATS — daftar channel/grup wajib-join, format JSON\n"
            "  Contoh:\n"
            '  /setvars REQUIRED_CHATS [{"chat_id": -1001111111111, '
            '"username": "namachannel", "invite_link": null, '
            '"label": "📢 Join Channel Utama"}]'
        )
        return

    key = parts[1].upper()
    value = parts[2].strip()

    if key not in KNOWN_VAR_KEYS:
        await update.message.reply_text(
            f"Key '{key}' tidak dikenali. Gunakan salah satu: {', '.join(KNOWN_VAR_KEYS)}"
        )
        return

    try:
        if key == "TARGET_CHATS":
            parsed = json.loads(value)
            if not isinstance(parsed, list) or not parsed:
                raise ValueError("harus berupa list JSON, tidak boleh kosong")
            for entry in parsed:
                if not isinstance(entry, dict) or "chat_id" not in entry:
                    raise ValueError('tiap entri harus dict dengan minimal "chat_id"')
                entry["chat_id"] = int(entry["chat_id"])
                entry.setdefault("kind", None)
                if entry["kind"] not in ("channel", "group", None):
                    raise ValueError('"kind" harus "channel" atau "group"')
        elif key == "REQUIRED_CHATS":
            parsed = json.loads(value)
            if not isinstance(parsed, list):
                raise ValueError("harus berupa list JSON")
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"❌ Format value salah: {e}")
        return

    if key in ("TARGET_CHATS", "REQUIRED_CHATS"):
        value = json.dumps(parsed)
    await db.set_setting(key, value)
    await update.message.reply_text(f"✅ {key} berhasil disimpan.")


# ---------------------------------------------------------------------
# /delvars <KEY>  -> hapus override, balik ke default Railway Variables
# ---------------------------------------------------------------------
async def delvars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    if not context.args:
        await update.message.reply_text(
            f"Format: /delvars <KEY>\nKey yang didukung: {', '.join(KNOWN_VAR_KEYS)}"
        )
        return

    key = context.args[0].upper()
    await db.delete_setting(key)
    await update.message.reply_text(
        f"🗑️ {key} dihapus. Bot akan pakai nilai default dari Railway Variables lagi."
    )


# ---------------------------------------------------------------------
# /getvars  -> lihat nilai efektif TARGET_CHATS & REQUIRED_CHATS saat ini
# ---------------------------------------------------------------------
async def getvars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    settings = await db.get_all_settings()
    lines = ["📋 Variabel aktif saat ini:\n"]
    for key in KNOWN_VAR_KEYS:
        if key in settings:
            lines.append(f"• {key} (diset via /setvars):\n{settings[key]}\n")
        else:
            default_val = TARGET_CHATS if key == "TARGET_CHATS" else REQUIRED_CHATS
            lines.append(f"• {key} (default dari Railway Variables):\n{default_val}\n")

    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------
# /ping  -> cek bot hidup + latency
# ---------------------------------------------------------------------
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t0 = time.monotonic()
    msg = await update.message.reply_text("🏓 Pong...")
    elapsed_ms = (time.monotonic() - t0) * 1000
    await msg.edit_text(f"🏓 Pong! {elapsed_ms:.0f}ms")


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
async def post_init(application: Application) -> None:
    await db.init_db()
    logger.info("Koneksi database Postgres siap.")

    # Menu command default: yang dilihat SEMUA orang (member biasa)
    await application.bot.set_my_commands(
        PUBLIC_COMMANDS, scope=BotCommandScopeDefault()
    )

    # Menu command khusus tiap admin: lihat semua command
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except TelegramError as e:
            # Wajar gagal kalau admin itu belum pernah /start bot ini sama sekali
            logger.warning("Gagal set menu admin untuk %s: %s", admin_id, e)

    logger.info("Menu command terpasang.")

    # Cek jadwal broadcast tiap 30 detik. "first=10" -> mulai 10 detik
    # setelah bot nyala (bukan langsung 0 detik) supaya init_db/menu selesai
    # dulu, dan supaya jadwal yang "kelewat" pas bot mati langsung diproses
    # begitu bot nyala lagi.
    if application.job_queue is not None:
        application.job_queue.run_repeating(
            run_due_scheduled_broadcasts, interval=30, first=10, name="scheduled_broadcasts"
        )
    else:
        logger.warning(
            "job_queue tidak aktif -- /jadwal tidak akan pernah tereksekusi. "
            "Pastikan 'python-telegram-bot[job-queue]' terpasang."
        )


async def post_shutdown(application: Application) -> None:
    await db.close_db()


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Jaring pengaman terakhir: kalau ada error yang lolos dari semua
    try/except di command masing-masing (misal error pas parsing teks
    SEBELUM sempat masuk ke loop kirim), tanpa ini admin cuma diem-diem
    tidak dapat balasan apa pun -- error-nya cuma nyangkut di log server.
    Sekarang selalu dibalas ke chat yang minta, jadi kelihatan."""
    logger.error("Unhandled exception saat proses update: %s", update, exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            err_text = html.escape(str(context.error))[:500]
            await update.effective_message.reply_text(
                f"⚠️ Terjadi error pas proses perintah ini:\n<code>{err_text}</code>\n\n"
                "Cek format perintahnya lagi, atau coba ulang.",
                parse_mode=ParseMode.HTML,
            )
    except Exception:  # noqa: BLE001
        pass  # jangan sampai error handler-nya sendiri ikut crash


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    media_filter = filters.User(user_id=ADMIN_IDS) & (
        filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION
    )
    app.add_handler(MessageHandler(media_filter, handle_admin_media))
    app.add_handler(CommandHandler(["store", "genlink"], store))
    app.add_handler(CommandHandler(["postlink", "pl"], postlink))
    app.add_handler(CommandHandler(["thumbch", "tc"], thumbch))
    app.add_handler(CommandHandler(["thumbgrp", "tg"], thumbgrp))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler(["batchstart", "bs"], batchstart))
    app.add_handler(CommandHandler(["batchstatus", "bt"], batchstatus))
    app.add_handler(CommandHandler(["batchdone", "bd"], batchdone))
    app.add_handler(CommandHandler(["batchcancel", "bc"], batchcancel))
    app.add_handler(CommandHandler(["delmedia", "dm"], delmedia))
    app.add_handler(CommandHandler(["listmedia", "lm"], listmedia))
    app.add_handler(CommandHandler(["cari", "cr"], cari))
    app.add_handler(CommandHandler(["broadcast", "br"], broadcast))
    app.add_handler(CommandHandler(["jadwal", "jd"], jadwal))
    app.add_handler(CommandHandler(["jadwallist", "jl"], jadwallist))
    app.add_handler(CommandHandler(["jadwalbatal", "jb"], jadwalbatal))
    app.add_handler(CommandHandler(["setvars", "sv"], setvars))
    app.add_handler(CommandHandler(["delvars", "dv"], delvars))
    app.add_handler(CommandHandler(["getvars", "gv"], getvars))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern=r"^checkjoin_"))
    app.add_handler(CallbackQueryHandler(listmedia_callback, pattern=r"^listmedia_"))
    app.add_error_handler(global_error_handler)

    logger.info("Bot berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

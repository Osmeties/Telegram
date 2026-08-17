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

import html
import json
import logging
import time
from datetime import datetime, timedelta, timezone

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
from telegram.error import TelegramError
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
    BotCommand("store", "Alias dari /genlink"),
    BotCommand("link", "Ambil ulang link dari kode yang sudah ada"),
    BotCommand("delmedia", "Hapus media tersimpan berdasarkan kode"),
    BotCommand("listmedia", "Lihat daftar semua media tersimpan"),
    BotCommand("setvars", "Untuk mengatur variabel"),
    BotCommand("delvars", "Untuk menghapus variabel"),
    BotCommand("getvars", "Untuk mendapatkan daftar variabel"),
    BotCommand("broadcast", "Untuk mengirimkan pesan ke channel/grup"),
]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------------------------------------------------------------
# Nilai efektif TARGET_CHATS / REQUIRED_CHATS (DB override > config/env)
# ---------------------------------------------------------------------
async def get_target_chats() -> list[int]:
    val = await db.get_setting("TARGET_CHATS")
    if val:
        return [int(x) for x in val.split(",") if x.strip()]
    return TARGET_CHATS


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
# Dukungan batch/album: kalau admin kirim beberapa foto/video sekaligus
# sebagai 1 album, Telegram mengirimnya sebagai pesan terpisah yang cuma
# ditandai media_group_id yang sama — reply command cuma nempel ke 1 dari
# pesan itu. Jadi kita "rekam" tiap pesan album yang masuk di sini, supaya
# /store, /genlink, /postlink bisa ambil semua anggotanya sekaligus.
# ---------------------------------------------------------------------
MEDIA_GROUP_TTL = 300  # detik; buffer lama otomatis dibuang biar tidak numpuk


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


async def capture_album(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rekam tiap pesan media dari admin yang merupakan bagian dari album
    (media_group_id sama) ke buffer sementara di bot_data."""
    message = update.message
    if message is None or message.media_group_id is None:
        return
    if not is_admin(message.from_user.id):
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


async def deliver_media(chat_id: int, code: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Kirim media untuk kode tsb. Return True kalau berhasil ketemu & terkirim."""
    row = await db.get_media(code)
    if row is None:
        await context.bot.send_message(chat_id, "Maaf, konten tidak ditemukan atau sudah kedaluwarsa.")
        return False

    items, caption = row["items"], row["caption"]

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
        await sender(chat_id=chat_id, **{media_type: items[0]["file_id"]}, caption=caption)
        return True

    # Lebih dari 1 media -> coba kirim sebagai 1 album (media group).
    # Catatan: Telegram cuma izinkan photo/video/document dicampur dalam 1
    # media group, "animation" (GIF) tidak bisa ikut di sana.
    media_input_map = {
        "photo": InputMediaPhoto,
        "video": InputMediaVideo,
        "document": InputMediaDocument,
    }
    if all(item["media_type"] in media_input_map for item in items):
        media_group = [
            media_input_map[item["media_type"]](
                media=item["file_id"],
                caption=caption if i == 0 else None,  # caption cuma boleh di item pertama
            )
            for i, item in enumerate(items)
        ]
        await context.bot.send_media_group(chat_id=chat_id, media=media_group)
        return True

    # Fallback: ada tipe yang tidak didukung media group (misal ada GIF
    # tercampur di dalamnya) -> kirim satu-satu.
    for i, item in enumerate(items):
        sender = send_map.get(item["media_type"])
        if sender is None:
            continue
        await sender(
            chat_id=chat_id,
            **{item["media_type"]: item["file_id"]},
            caption=caption if i == 0 else None,
        )
    return True


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

    await deliver_media(update.effective_chat.id, code, context)


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
    await deliver_media(update.effective_chat.id, code, context)


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
            "Isi dulu lewat /setvars TARGET_CHATS <chat_id> sebelum posting."
        )
        return

    sent, failed = 0, 0
    for chat_id in target_chats:
        try:
            await context.bot.send_message(chat_id, post_text, reply_markup=keyboard)
            sent += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Gagal posting ke %s: %s", chat_id, e)
            failed += 1

    await update.message.reply_text(
        f"✅ Posting selesai. Sukses: {sent}, Gagal: {failed}\n\nLink: {deep_link}"
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
WIB = timezone(timedelta(hours=7))  # reset kuota /cari jam 00:00 WIB


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

        sent, failed = 0, 0
        target_chats = await get_target_chats()
        for chat_id in target_chats:
            try:
                await context.bot.send_message(
                    chat_id,
                    custom_text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                sent += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("Gagal broadcast ke %s: %s", chat_id, e)
                failed += 1

        await update.message.reply_text(f"Broadcast selesai. Sukses: {sent}, Gagal: {failed}")
        return

    # Mode reply: copy pesan yang di-reply (bawa media kalau ada), opsional
    # timpa caption-nya + pasang tombol dari argumen /broadcast di atas.
    sent, failed = 0, 0
    target_chats = await get_target_chats()
    for chat_id in target_chats:
        try:
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=replied.chat_id,
                message_id=replied.message_id,
                caption=custom_text or None,
                parse_mode=ParseMode.MARKDOWN_V2 if custom_text else None,
                reply_markup=keyboard,
            )
            sent += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Gagal broadcast ke %s: %s", chat_id, e)
            failed += 1

    await update.message.reply_text(f"Broadcast selesai. Sukses: {sent}, Gagal: {failed}")


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
            "• TARGET_CHATS — daftar chat_id tujuan /broadcast, pisah koma\n"
            "  Contoh:\n"
            "  /setvars TARGET_CHATS -1001111111111,-1002222222222\n\n"
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
            parsed = [int(x) for x in value.split(",") if x.strip()]
            if not parsed:
                raise ValueError("daftar kosong")
        elif key == "REQUIRED_CHATS":
            parsed = json.loads(value)
            if not isinstance(parsed, list):
                raise ValueError("harus berupa list JSON")
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"❌ Format value salah: {e}")
        return

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


async def post_shutdown(application: Application) -> None:
    await db.close_db()


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
    app.add_handler(
        MessageHandler(
            filters.User(user_id=ADMIN_IDS)
            & (filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION),
            capture_album,
        )
    )
    app.add_handler(CommandHandler(["store", "genlink"], store))
    app.add_handler(CommandHandler("postlink", postlink))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler("delmedia", delmedia))
    app.add_handler(CommandHandler("listmedia", listmedia))
    app.add_handler(CommandHandler("cari", cari))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("setvars", setvars))
    app.add_handler(CommandHandler("delvars", delvars))
    app.add_handler(CommandHandler("getvars", getvars))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern=r"^checkjoin_"))
    app.add_handler(CallbackQueryHandler(listmedia_callback, pattern=r"^listmedia_"))

    logger.info("Bot berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

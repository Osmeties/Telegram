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

import json
import logging
import time

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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
]

ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("genlink", "Untuk membuat link fsub / konten"),
    BotCommand("store", "Alias dari /genlink"),
    BotCommand("link", "Ambil ulang link dari kode yang sudah ada"),
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


async def deliver_media(chat_id: int, code: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Kirim media untuk kode tsb. Return True kalau berhasil ketemu & terkirim."""
    row = await db.get_media(code)
    if row is None:
        await context.bot.send_message(chat_id, "Maaf, konten tidak ditemukan atau sudah kedaluwarsa.")
        return False

    file_id, media_type, caption = row
    send_map = {
        "photo": context.bot.send_photo,
        "video": context.bot.send_video,
        "document": context.bot.send_document,
        "animation": context.bot.send_animation,
    }
    sender = send_map.get(media_type)
    if sender is None:
        await context.bot.send_message(chat_id, "Tipe media tidak didukung.")
        return False

    await sender(chat_id=chat_id, **{media_type: file_id}, caption=caption)
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
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    if not context.args:
        await update.message.reply_text("Format: /store <kode>  (reply ke media)")
        return

    code = context.args[0]
    replied = update.message.reply_to_message
    if replied is None:
        await update.message.reply_text("Reply perintah ini ke pesan media yang ingin disimpan.")
        return

    if replied.photo:
        file_id = replied.photo[-1].file_id
        media_type = "photo"
    elif replied.video:
        file_id = replied.video.file_id
        media_type = "video"
    elif replied.document:
        file_id = replied.document.file_id
        media_type = "document"
    elif replied.animation:
        file_id = replied.animation.file_id
        media_type = "animation"
    else:
        await update.message.reply_text("Tipe media tidak didukung (gunakan foto/video/dokumen/gif).")
        return

    caption = replied.caption or ""
    await db.save_media(code, file_id, media_type, caption)

    bot_username = (await context.bot.get_me()).username
    deep_link = f"https://t.me/{bot_username}?start=get_{code}"

    await update.message.reply_text(
        f"Tersimpan dengan kode: {code}\nLink siap pakai:\n{deep_link}"
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
# /broadcast  (reply ke pesan yang mau disebar ke channel/grup terdaftar)
# ---------------------------------------------------------------------
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Perintah ini khusus admin.")
        return

    replied = update.message.reply_to_message
    if replied is None:
        await update.message.reply_text("Reply perintah ini ke pesan yang ingin di-broadcast.")
        return

    sent, failed = 0, 0
    target_chats = await get_target_chats()
    for chat_id in target_chats:
        try:
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=replied.chat_id,
                message_id=replied.message_id,
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
    app.add_handler(CommandHandler(["store", "genlink"], store))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("setvars", setvars))
    app.add_handler(CommandHandler("delvars", delvars))
    app.add_handler(CommandHandler("getvars", getvars))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern=r"^checkjoin_"))

    logger.info("Bot berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

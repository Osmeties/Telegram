"""
Bot Telegram: Broadcast + Kirim Media via Deep Link
=====================================================

Fitur:
1. /store  -> admin reply ke sebuah media (foto/video/dokumen) dengan
              "/store <kode>" untuk menyimpan media itu dengan kode unik.
2. /link   -> admin ketik "/link <kode>" untuk mendapatkan deep link
              siap-pakai, contoh:
              https://t.me/NamaBot?start=get_<kode>
3. Saat user klik link tsb dan membuka bot (trigger command /start
   dengan payload get_<kode>), bot otomatis mengirim media yang
   tersimpan ke chat pribadi user.
4. /broadcast -> admin reply ke sebuah pesan (teks/media) dengan
              "/broadcast" untuk mengirim pesan itu ke semua channel/grup
              yang terdaftar di daftar TARGET_CHATS (lihat config.py).

Semua data media disimpan di PostgreSQL (Railway) lewat modul db.py.
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------------------------------------------------------------
# Cek wajib-join
# ---------------------------------------------------------------------
async def get_missing_chats(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    """Kembalikan daftar REQUIRED_CHATS yang BELUM di-join user."""
    missing = []
    for chat in REQUIRED_CHATS:
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
    for chat_id in TARGET_CHATS:
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
# Main
# ---------------------------------------------------------------------
async def post_init(application: Application) -> None:
    await db.init_db()
    logger.info("Koneksi database Postgres siap.")


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
    app.add_handler(CommandHandler("store", store))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern=r"^checkjoin_"))

    logger.info("Bot berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

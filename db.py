"""
Modul database — pakai PostgreSQL (Railway) lewat asyncpg connection pool.

Railway otomatis kasih environment variable DATABASE_URL kalau kamu
attach plugin "Postgres" ke project. Modul ini baca dari situ.
"""

import json
import os
from typing import Optional

import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool: Optional[asyncpg.Pool] = None


async def init_db() -> None:
    """Buat connection pool + pastikan tabel media & settings sudah ada/terkini."""
    global _pool
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL tidak ditemukan. Di Railway, attach plugin Postgres "
            "ke project ini dulu — variable-nya akan otomatis muncul."
        )

    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with _pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media (
                code TEXT PRIMARY KEY,
                file_id TEXT,
                media_type TEXT,
                items JSONB,
                caption TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # Migrasi utk instalasi lama: kolom "items" (array media, dipakai utk
        # batch/album) belum ada dulu, dan file_id/media_type dulunya NOT NULL.
        await conn.execute("ALTER TABLE media ADD COLUMN IF NOT EXISTS items JSONB")
        await conn.execute("ALTER TABLE media ALTER COLUMN file_id DROP NOT NULL")
        await conn.execute("ALTER TABLE media ALTER COLUMN media_type DROP NOT NULL")
        # Isi "items" utk baris lama yang dibuat sebelum kolom ini ada, supaya
        # kode lama tetap terkirim tanpa perlu upload ulang.
        await conn.execute(
            """
            UPDATE media
            SET items = jsonb_build_array(
                jsonb_build_object('file_id', file_id, 'media_type', media_type)
            )
            WHERE items IS NULL AND file_id IS NOT NULL
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # Kuota harian utk /cari (dihitung per hari kalender WIB, direset di bot.py)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_usage (
                user_id BIGINT NOT NULL,
                day DATE NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            )
            """
        )
        # Broadcast terjadwal ("posting terjadwal" ala Facebook) -- disimpan
        # permanen di sini (bukan cuma di memori) supaya tetap jalan walau
        # bot sempat restart/redeploy sebelum waktunya tiba.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
                id SERIAL PRIMARY KEY,
                created_by BIGINT NOT NULL,
                run_at TIMESTAMPTZ NOT NULL,
                text TEXT,
                button_spec JSONB,
                source_chat_id BIGINT,
                source_message_id BIGINT,
                thumb_channel_file_id TEXT,
                thumb_group_file_id TEXT,
                media_file_id TEXT,
                media_type TEXT,
                source_caption TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                sent_at TIMESTAMPTZ
            )
            """
        )
        # Migrasi utk instalasi lama: kolom thumbnail/media di bawah ini belum
        # ada dulu (fitur thumbnail beda channel/grup utk /jadwal ditambah
        # belakangan).
        await conn.execute("ALTER TABLE scheduled_broadcasts ADD COLUMN IF NOT EXISTS thumb_channel_file_id TEXT")
        await conn.execute("ALTER TABLE scheduled_broadcasts ADD COLUMN IF NOT EXISTS thumb_group_file_id TEXT")
        await conn.execute("ALTER TABLE scheduled_broadcasts ADD COLUMN IF NOT EXISTS media_file_id TEXT")
        await conn.execute("ALTER TABLE scheduled_broadcasts ADD COLUMN IF NOT EXISTS media_type TEXT")
        await conn.execute("ALTER TABLE scheduled_broadcasts ADD COLUMN IF NOT EXISTS source_caption TEXT")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sched_pending_run_at "
            "ON scheduled_broadcasts (run_at) WHERE status = 'pending'"
        )

        # Dukungan /jdtemp, /jdtempch, /jdtempgrp -- versi terjadwal dari
        # /temp, /tempch, /tempgrp. is_temp menandai jadwal ini perlu dicatat
        # ke temp_posts (auto-hapus jam 02:00 WIB) pas dieksekusi nanti;
        # target_kind membatasi ke TARGET_CHATS "channel"/"group" saja (NULL
        # = semua, perilaku /jadwal yang sudah ada).
        await conn.execute("ALTER TABLE scheduled_broadcasts ADD COLUMN IF NOT EXISTS is_temp BOOLEAN NOT NULL DEFAULT FALSE")
        await conn.execute("ALTER TABLE scheduled_broadcasts ADD COLUMN IF NOT EXISTS target_kind TEXT")

        # Pesan hasil /temp -- dicatat di sini supaya bisa dihapus otomatis
        # tiap jam 02:00 WIB (lihat job "cleanup_temp_posts"). Disimpan
        # permanen (bukan cuma di memori) supaya tetap ke-hapus walau bot
        # sempat restart/redeploy sebelum jam 02:00 tiba.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS temp_posts (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def save_media(code: str, items: list[dict], caption: str | None) -> None:
    """items: list of {"file_id":.., "media_type":..} — 1 elemen untuk media
    tunggal, beberapa elemen kalau ini batch/album (beberapa foto/video
    disimpan sekaligus di bawah 1 kode)."""
    first = items[0]
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO media (code, items, file_id, media_type, caption)
            VALUES ($1, $2::jsonb, $3, $4, $5)
            ON CONFLICT (code) DO UPDATE
            SET items = EXCLUDED.items,
                file_id = EXCLUDED.file_id,
                media_type = EXCLUDED.media_type,
                caption = EXCLUDED.caption
            """,
            code, json.dumps(items), first["file_id"], first["media_type"], caption,
        )


async def get_media(code: str):
    """Return dict {"items": [{"file_id":.., "media_type":..}, ...], "caption": ...}
    atau None kalau kode tidak ditemukan. "items" selalu list, walau isinya 1."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT items, file_id, media_type, caption FROM media WHERE code = $1",
            code,
        )
        if row is None:
            return None
        items = row["items"]
        if items:
            if isinstance(items, str):
                items = json.loads(items)
        else:
            # fallback kalau ada baris yang entah kenapa belum ke-backfill
            items = [{"file_id": row["file_id"], "media_type": row["media_type"]}]
        return {"items": items, "caption": row["caption"]}


async def delete_media(code: str) -> bool:
    """Hapus media dengan kode tsb. Return True kalau ada baris yang terhapus."""
    async with _pool.acquire() as conn:
        result = await conn.execute("DELETE FROM media WHERE code = $1", code)
        # asyncpg execute() balikin string seperti "DELETE 1" atau "DELETE 0"
        return result.split()[-1] != "0"


# ---------------------------------------------------------------------
# Browse & cari ("perpustakaan")
# ---------------------------------------------------------------------
_MEDIA_LIST_SELECT = """
    SELECT code, caption, created_at,
           COALESCE(jsonb_array_length(items), 1) AS item_count
    FROM media
"""


async def count_media() -> int:
    async with _pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM media")


async def list_media(offset: int, limit: int) -> list[dict]:
    """Daftar media terbaru dulu, buat /listmedia dengan pagination."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            _MEDIA_LIST_SELECT + " ORDER BY created_at DESC OFFSET $1 LIMIT $2",
            offset, limit,
        )
        return [dict(r) for r in rows]


async def search_media(keyword: str, limit: int = 20) -> list[dict]:
    """Cari media yang kode ATAU caption-nya mengandung kata kunci (case-insensitive)."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            _MEDIA_LIST_SELECT
            + """
            WHERE code ILIKE '%' || $1 || '%' OR caption ILIKE '%' || $1 || '%'
            ORDER BY created_at DESC
            LIMIT $2
            """,
            keyword, limit,
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------
# Kuota harian /cari (member biasa dibatasi, admin tidak)
# ---------------------------------------------------------------------
async def get_search_count(user_id: int, day) -> int:
    async with _pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT count FROM search_usage WHERE user_id = $1 AND day = $2",
            user_id, day,
        )
        return val or 0


async def increment_search_count(user_id: int, day) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO search_usage (user_id, day, count)
            VALUES ($1, $2, 1)
            ON CONFLICT (user_id, day) DO UPDATE
            SET count = search_usage.count + 1
            """,
            user_id, day,
        )


# ---------------------------------------------------------------------
# Settings (variabel yang bisa diatur admin lewat /setvars, /delvars, /getvars)
# ---------------------------------------------------------------------
async def get_setting(key: str) -> str | None:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", key)
        return row["value"] if row else None


async def set_setting(key: str, value: str) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES ($1, $2, now())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = now()
            """,
            key, value,
        )


async def delete_setting(key: str) -> None:
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM settings WHERE key = $1", key)


async def get_all_settings() -> dict:
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM settings ORDER BY key")
        return {r["key"]: r["value"] for r in rows}


# ---------------------------------------------------------------------
# Broadcast terjadwal
# ---------------------------------------------------------------------
async def create_scheduled_broadcast(
    created_by: int,
    run_at,
    text: str | None,
    button_spec: list | None,
    source_chat_id: int | None,
    source_message_id: int | None,
    thumb_channel_file_id: str | None = None,
    thumb_group_file_id: str | None = None,
    media_file_id: str | None = None,
    media_type: str | None = None,
    source_caption: str | None = None,
    is_temp: bool = False,
    target_kind: str | None = None,
) -> int:
    """is_temp: kalau True, pas dieksekusi nanti pesannya dicatat ke
    temp_posts (ikut auto-hapus jam 02:00 WIB, lihat cleanup_temp_posts).
    target_kind: None = semua TARGET_CHATS, "channel"/"group" = cuma yg
    kind-nya itu (persis seperti /tempch, /tempgrp tapi versi terjadwal)."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO scheduled_broadcasts
                (created_by, run_at, text, button_spec, source_chat_id, source_message_id,
                 thumb_channel_file_id, thumb_group_file_id, media_file_id, media_type,
                 source_caption, is_temp, target_kind)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            RETURNING id
            """,
            created_by, run_at, text,
            json.dumps(button_spec) if button_spec else None,
            source_chat_id, source_message_id,
            thumb_channel_file_id, thumb_group_file_id, media_file_id, media_type, source_caption,
            is_temp, target_kind,
        )
        return row["id"]


async def get_due_scheduled_broadcasts(now) -> list[dict]:
    """Ambil semua jadwal yang statusnya masih 'pending' dan waktunya sudah lewat."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, created_by, run_at, text, button_spec, source_chat_id, source_message_id,
                   thumb_channel_file_id, thumb_group_file_id, media_file_id, media_type, source_caption,
                   is_temp, target_kind
            FROM scheduled_broadcasts
            WHERE status = 'pending' AND run_at <= $1
            ORDER BY run_at
            """,
            now,
        )
        result = []
        for r in rows:
            d = dict(r)
            if d["button_spec"] and isinstance(d["button_spec"], str):
                d["button_spec"] = json.loads(d["button_spec"])
            result.append(d)
        return result


async def mark_scheduled_broadcast(id_: int, status: str) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE scheduled_broadcasts
            SET status = $1, sent_at = CASE WHEN $1 = 'sent' THEN now() ELSE sent_at END
            WHERE id = $2
            """,
            status, id_,
        )


async def list_pending_scheduled_broadcasts(limit: int = 20) -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, run_at, text, source_chat_id
            FROM scheduled_broadcasts
            WHERE status = 'pending'
            ORDER BY run_at
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


async def cancel_scheduled_broadcast(id_: int) -> bool:
    """Return True kalau ada jadwal 'pending' dengan id itu yang berhasil dibatalkan."""
    async with _pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE scheduled_broadcasts SET status = 'cancelled' "
            "WHERE id = $1 AND status = 'pending'",
            id_,
        )
        return result.split()[-1] != "0"


# ---------------------------------------------------------------------
# /temp -- broadcast sementara yang auto-hapus tiap jam 02:00 WIB
# ---------------------------------------------------------------------
async def add_temp_post(chat_id: int, message_id: int) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO temp_posts (chat_id, message_id) VALUES ($1, $2)",
            chat_id, message_id,
        )


async def get_all_temp_posts() -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, chat_id, message_id FROM temp_posts ORDER BY id")
        return [dict(r) for r in rows]


async def delete_temp_post_rows(ids: list[int]) -> None:
    """Hapus baris catatan (bukan pesan Telegram-nya -- itu urusan bot.py)
    setelah selesai diproses, sukses ataupun gagal dihapus dari Telegram."""
    if not ids:
        return
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM temp_posts WHERE id = ANY($1::int[])", ids)


async def count_temp_posts() -> int:
    async with _pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM temp_posts")

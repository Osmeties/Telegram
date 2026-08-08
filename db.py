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

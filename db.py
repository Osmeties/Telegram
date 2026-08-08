"""
Modul database — pakai PostgreSQL (Railway) lewat asyncpg connection pool.

Railway otomatis kasih environment variable DATABASE_URL kalau kamu
attach plugin "Postgres" ke project. Modul ini baca dari situ.
"""

import os
from typing import Optional

import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool: Optional[asyncpg.Pool] = None


async def init_db() -> None:
    """Buat connection pool + pastikan tabel media sudah ada."""
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
                file_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                caption TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def save_media(code: str, file_id: str, media_type: str, caption: str | None) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO media (code, file_id, media_type, caption)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (code) DO UPDATE
            SET file_id = EXCLUDED.file_id,
                media_type = EXCLUDED.media_type,
                caption = EXCLUDED.caption
            """,
            code, file_id, media_type, caption,
        )


async def get_media(code: str):
    """Return tuple (file_id, media_type, caption) atau None."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT file_id, media_type, caption FROM media WHERE code = $1",
            code,
        )
        if row is None:
            return None
        return row["file_id"], row["media_type"], row["caption"]

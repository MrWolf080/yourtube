"""Populate an explicitly selected YourTube instance with playable demo data."""

import argparse
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import psycopg

from app.main import MEDIA, passwords, prepare_video

VERSION = "001_demo_data"
ADMIN_VERSION = "002_demo_admin"
ADMIN_HANDLE = "demo_admin"
SEED_LOCK = 910207440
ACCOUNTS = {
    "demo_lab": "user",
    "demo_studio": "user",
    "demo_viewer": "user",
    "demo_moderator": "moderator",
}
# Fixed IDs let a retry reuse files installed before an interrupted commit.
VIDEOS = (
    ("demo_lab", "Цветовая шкала в движении", "Технологии", "testsrc2", 6, False),
    ("demo_studio", "Световой градиент", "Кино", "gradients", 8, False),
    ("demo_lab", "Спектр на экране", "Музыка", "rgbtestsrc", 5, False),
    ("demo_studio", "Черновик монтажа", "Кино", "smptebars", 7, True),
)


def demo_video_id(index):
    return UUID(f"91ca21c3-c92a-496e-a3cc-3b6bccaa{index:04x}")


def install_media(video_id, pattern, seconds):
    destination = MEDIA / str(video_id)
    if (destination / "video.mp4").is_file() and (destination / "preview.jpg").is_file():
        return
    with TemporaryDirectory(prefix=".demo-", dir=MEDIA) as temporary:
        working = Path(temporary)
        source = working / "source.mp4"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
             f"{pattern}=size=640x360:rate=24", "-t", str(seconds),
             "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", str(source)],
            check=True, timeout=60,
        )
        duration = prepare_video(source, working)
        if duration != seconds:
            raise RuntimeError(f"Неожиданная длительность ролика {video_id}: {duration}")
        source.unlink()
        working.rename(destination)


def seed_demo_data(db):
    credentials = {handle: secrets.token_urlsafe(18) for handle in ACCOUNTS}
    users = {}
    for handle, role in ACCOUNTS.items():
        users[handle] = db.execute(
            "INSERT INTO accounts (handle,password_hash,role) VALUES (%s,%s,%s) RETURNING id",
            (handle, passwords.hash(credentials[handle]), role),
        ).fetchone()[0]

    MEDIA.mkdir(parents=True, exist_ok=True)
    video_ids = []
    for index, (owner, title, category, pattern, seconds, private) in enumerate(VIDEOS, 1):
        video_id = demo_video_id(index)
        db.execute("""
            INSERT INTO videos (id, owner_id, title, description, category, private, duration, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,now() - make_interval(days => %s))
        """, (video_id, users[owner], title, "Тестовый ролик, сгенерированный FFmpeg.",
              category, private, seconds, index))
        install_media(video_id, pattern, seconds)
        video_ids.append(video_id)

    viewer, lab, studio = (users[name] for name in ("demo_viewer", "demo_lab", "demo_studio"))
    db.execute("INSERT INTO grants VALUES (%s,%s)", (video_ids[3], viewer))
    db.execute("INSERT INTO likes (video_id,user_id) VALUES (%s,%s),(%s,%s),(%s,%s)",
               (video_ids[0], viewer, video_ids[0], studio, video_ids[1], viewer))
    db.execute("INSERT INTO subscriptions VALUES (%s,%s),(%s,%s)", (lab, viewer, studio, viewer))
    db.execute("""
        INSERT INTO comments (video_id,user_id,body) VALUES (%s,%s,%s),(%s,%s,%s),(%s,%s,%s)
    """, (video_ids[0], viewer, "Перемотка работает на середине ролика.",
          video_ids[0], lab, "Спасибо за проверку!", video_ids[3], viewer, "Черновик открылся."))
    playlist = db.execute("INSERT INTO playlists (owner_id,title) VALUES (%s,%s) RETURNING id",
                          (lab, "Тестовые ролики")).fetchone()[0]
    db.execute("INSERT INTO playlist_items (playlist_id,video_id) VALUES (%s,%s),(%s,%s)",
               (playlist, video_ids[0], playlist, video_ids[2]))
    db.execute("INSERT INTO reports (video_id,reporter_id,reason) VALUES (%s,%s,%s)",
               (video_ids[2], viewer, "Тестовая жалоба для проверки модерации."))
    return credentials


def seed_demo_admin(db):
    handle = ADMIN_HANDLE
    password = secrets.token_urlsafe(18)
    account = db.execute("""
        INSERT INTO accounts (handle,password_hash,role) VALUES (%s,%s,'admin')
        ON CONFLICT (handle) DO NOTHING RETURNING id
    """, (handle, passwords.hash(password))).fetchone()
    if not account:
        raise RuntimeError("Имя demo_admin уже занято. Выберите другое имя для существующего аккаунта.")
    return {handle: password}


def ensure_migration_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS data_migrations (
            version text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
    """)


def apply(db):
    credentials = {}
    for version, migrate in ((VERSION, seed_demo_data), (ADMIN_VERSION, seed_demo_admin)):
        if db.execute("SELECT 1 FROM data_migrations WHERE version=%s", (version,)).fetchone():
            continue
        credentials.update(migrate(db))
        db.execute("INSERT INTO data_migrations (version) VALUES (%s)", (version,))
    return credentials


def rollback(db, video_ids):
    versions = [row[0] for row in db.execute(
        "SELECT version FROM data_migrations WHERE version = ANY(%s)",
        ([VERSION, ADMIN_VERSION],),
    )]
    handles = list(ACCOUNTS) if VERSION in versions else []
    if ADMIN_VERSION in versions:
        handles.append(ADMIN_HANDLE)
    account_ids = [row[0] for row in db.execute(
        "SELECT id FROM accounts WHERE handle = ANY(%s) ORDER BY id FOR UPDATE", (handles,),
    )]
    if VERSION in versions:
        db.execute("DELETE FROM videos WHERE id = ANY(%s)", (video_ids,))
    if account_ids:
        if db.execute("SELECT 1 FROM videos WHERE owner_id = ANY(%s) LIMIT 1", (account_ids,)).fetchone():
            raise RuntimeError("У демо-аккаунтов есть загруженные ролики. Откат отменён.")
        db.execute("DELETE FROM comments WHERE user_id = ANY(%s)", (account_ids,))
        db.execute("DELETE FROM reports WHERE reporter_id = ANY(%s)", (account_ids,))
        db.execute("UPDATE reports SET reviewed_by=NULL WHERE reviewed_by = ANY(%s)", (account_ids,))
        db.execute("DELETE FROM video_view_receipts WHERE viewer = ANY(%s)",
                   ([f"user:{account_id}" for account_id in account_ids],))
        db.execute("DELETE FROM accounts WHERE id = ANY(%s)", (account_ids,))
    db.execute("DELETE FROM data_migrations WHERE version = ANY(%s)", (versions,))


def remove_demo_media(db, video_ids):
    # The DB transaction has committed. A later down can retry interrupted cleanup.
    existing = {row[0] for row in db.execute("SELECT id FROM videos WHERE id = ANY(%s)", (video_ids,))}
    for video_id in video_ids:
        if video_id not in existing:
            try:
                shutil.rmtree(MEDIA / str(video_id))
            except FileNotFoundError:
                pass


def main():
    parser = argparse.ArgumentParser(description="Управление тестовыми данными YourTube")
    parser.add_argument("action", choices=("up", "down"), default="up", nargs="?")
    action = parser.parse_args().action
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as db:
        db.execute("SELECT pg_advisory_lock(%s)", (SEED_LOCK,))
        with db.transaction():
            ensure_migration_table(db)
            if action == "up":
                credentials = apply(db)
            else:
                video_ids = [demo_video_id(index) for index in range(1, len(VIDEOS) + 1)]
                rollback(db, video_ids)
        if action == "down":
            remove_demo_media(db, video_ids)
            print("Тестовые данные удалены.")
            return
    if not credentials:
        print("Миграция уже применена.")
        return
    print("Тестовые данные добавлены. Сохраните пароли, они выводятся один раз.")
    for handle, password in credentials.items():
        print(f"{handle}: {password}")


if __name__ == "__main__":
    main()


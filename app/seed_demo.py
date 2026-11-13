"""Populate an explicitly selected YourTube instance with playable demo data."""

import os
import secrets
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import psycopg

from app.main import MEDIA, passwords, prepare_video

VERSION = "001_demo_data"
ADMIN_VERSION = "002_demo_admin"
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
        video_id = UUID(f"91ca21c3-c92a-496e-a3cc-3b6bccaa{index:04x}")
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
    handle = "demo_admin"
    password = secrets.token_urlsafe(18)
    account = db.execute("""
        INSERT INTO accounts (handle,password_hash,role) VALUES (%s,%s,'admin')
        ON CONFLICT (handle) DO NOTHING RETURNING id
    """, (handle, passwords.hash(password))).fetchone()
    if not account:
        raise RuntimeError("Имя demo_admin уже занято. Выберите другое имя для существующего аккаунта.")
    return {handle: password}


def apply(db):
    db.execute("SELECT pg_advisory_xact_lock(910207440)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS data_migrations (
            version text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    credentials = {}
    for version, migrate in ((VERSION, seed_demo_data), (ADMIN_VERSION, seed_demo_admin)):
        if db.execute("SELECT 1 FROM data_migrations WHERE version=%s", (version,)).fetchone():
            continue
        credentials.update(migrate(db))
        db.execute("INSERT INTO data_migrations (version) VALUES (%s)", (version,))
    return credentials


def main():
    with psycopg.connect(os.environ["DATABASE_URL"]) as db:
        credentials = apply(db)
    if not credentials:
        print("Миграция уже применена.")
        return
    print("Тестовые данные добавлены. Сохраните пароли, они выводятся один раз.")
    for handle, password in credentials.items():
        print(f"{handle}: {password}")


if __name__ == "__main__":
    main()


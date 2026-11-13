"""Durable PostgreSQL video queue. Run with python -m app.video_worker."""

import logging
import os
import selectors
import shutil
import signal
import subprocess
import threading
import time
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from app.main import DATABASE_URL, MEDIA, clean_pending_media, pool, sweep_uploads, video_lock
from app.video_processing import ERROR, prepare_video

LEASE = 30
ATTEMPTS = 3
SLOT_NAMESPACE = 910207443
log = logging.getLogger(__name__)


class JobLost(Exception):
    pass


def claim(db):
    with db.transaction():
        candidates = db.execute("""
            SELECT id, attempts FROM videos
            WHERE status='queued' OR (status='processing' AND lease_until <= now())
            ORDER BY created_at,id LIMIT 32 FOR UPDATE SKIP LOCKED
        """).fetchall()
        for row in candidates:
            if not video_lock(db, row["id"], transaction=False):
                continue
            token = uuid4()
            db.execute("""
                UPDATE videos SET status='processing', progress=0, attempts=attempts+1,
                    lease_token=%s, lease_until=now() + make_interval(secs => %s)
                WHERE id=%s
            """, (token, LEASE, row["id"]))
            return row["id"], token, row["attempts"] + 1
    return None


def heartbeat(db, video_id, token, progress):
    row = db.execute("""
        UPDATE videos SET lease_until=now() + make_interval(secs => %s), progress=%s
        WHERE id=%s AND lease_token=%s AND status='processing' AND lease_until > now()
        RETURNING id
    """, (LEASE, progress, video_id, token)).fetchone()
    if not row:
        raise JobLost


class Pipeline:
    def __init__(self, db, video_id, token):
        self.db = db
        self.id = video_id
        self.token = token
        self.progress = 0

    def run(self, command, timeout, duration=None):
        deadline = time.monotonic() + timeout
        renew_at = 0
        output = bytearray()
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        now = time.monotonic()
                        if now >= deadline:
                            raise subprocess.TimeoutExpired(command, timeout)
                        if now >= renew_at:
                            heartbeat(self.db, self.id, self.token, self.progress)
                            renew_at = now + 2
                        if not selector.select(timeout=0.5):
                            continue
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        output.extend(chunk)
                        if duration is not None:
                            lines = output.split(b"\n")
                            output = bytearray(lines.pop())
                            for line in lines:
                                if line.startswith(b"out_time_us="):
                                    value = line.partition(b"=")[2]
                                    if value.isdigit():
                                        self.progress = max(self.progress, min(99, int(int(value) / (duration * 10000))))
                        elif len(output) > 1024 * 1024:
                            raise subprocess.CalledProcessError(1, command)
                if process.wait(timeout=max(0.1, deadline - time.monotonic())):
                    raise subprocess.CalledProcessError(process.returncode, command)
            finally:
                if process.poll() is None:
                    process.kill()
        return bytes(output)


def finish(db, video_id, token, directory, staging, duration):
    with db.transaction():
        row = db.execute("""
            SELECT id FROM videos WHERE id=%s AND status='processing' AND lease_token=%s
            AND lease_until > now() FOR UPDATE
        """, (video_id, token)).fetchone()
        if not row:
            raise JobLost
        if duration is None:
            db.execute("""
                UPDATE videos SET status='failed', error=%s, lease_token=NULL, lease_until=NULL, cleanup_pending=true
                WHERE id=%s
            """, (ERROR, video_id))
        else:
            for name in ("video.mp4", "preview.jpg"):
                with (staging / name).open("rb") as result:
                    os.fsync(result.fileno())
                (staging / name).replace(directory / name)
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            db.execute("""
                UPDATE videos SET status='ready', progress=100, duration=%s,
                    lease_token=NULL, lease_until=NULL, cleanup_pending=true WHERE id=%s
            """, (duration, video_id))


def process(db, video_id, token, attempt):
    directory = MEDIA / str(video_id)
    staging = directory / ".processing"
    source = directory / "source"
    try:
        if staging.exists():
            shutil.rmtree(staging)
        duration = None
        if attempt <= ATTEMPTS:
            try:
                staging.mkdir()
                duration = prepare_video(source, staging, Pipeline(db, video_id, token).run)
            except JobLost:
                raise
            except Exception:
                log.exception("Video processing failed for %s", video_id)
        finish(db, video_id, token, directory, staging, duration)
    except JobLost:
        log.info("Video job cancelled or lease lost: %s", video_id)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    trim_terminal_files(db, video_id)


def trim_terminal_files(db, video_id):
    directory = MEDIA / str(video_id)
    with db.transaction():
        row = db.execute("""
            SELECT status FROM videos WHERE id=%s AND status IN ('ready','failed') FOR UPDATE
        """, (video_id,)).fetchone()
        if not row:
            return
        (directory / "source").unlink(missing_ok=True)
        if (directory / ".processing").exists():
            shutil.rmtree(directory / ".processing")
        if row["status"] == "failed":
            for name in ("video.mp4", "preview.jpg"):
                (directory / name).unlink(missing_ok=True)
        size = sum(item.stat().st_size for item in directory.iterdir() if item.is_file())
        db.execute("UPDATE videos SET reserved_bytes=%s, cleanup_pending=false WHERE id=%s", (size, video_id))


def worker_loop(stop, maintenance=False):
    while not stop.is_set():
        try:
            with psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row,
                                 connect_timeout=5) as db:
                next_cleanup = 0
                while not stop.is_set():
                    if maintenance and time.monotonic() >= next_cleanup:
                        clean_pending_media()
                        sweep_uploads()
                        terminal = db.execute("""
                            SELECT id FROM videos WHERE status IN ('ready','failed')
                            AND cleanup_pending ORDER BY id LIMIT 40
                        """).fetchall()
                        for row in terminal:
                            with db.transaction():
                                if video_lock(db, row["id"]):
                                    trim_terminal_files(db, row["id"])
                        next_cleanup = time.monotonic() + 30
                    for slot in range(2):
                        if db.execute("SELECT pg_try_advisory_lock(%s,%s) AS locked",
                                      (SLOT_NAMESPACE, slot)).fetchone()["locked"]:
                            break
                    else:
                        stop.wait(1)
                        continue
                    job = None
                    try:
                        job = claim(db)
                        if job:
                            process(db, *job)
                    finally:
                        db.execute("SELECT pg_advisory_unlock_all()")
                    if not job:
                        stop.wait(1)
        except Exception:
            log.exception("Worker iteration failed")
            stop.wait(2)


def main():
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    pool.open(wait=True)
    threads = [threading.Thread(target=worker_loop, args=(stop, index == 0)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    pool.close()


if __name__ == "__main__":
    main()


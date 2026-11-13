"""Queue integration checks against a disposable Compose project."""

import subprocess
from contextlib import contextmanager
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from uuid import uuid4

from smoke import COMPOSE, Client

# The gate holds the real worker between claiming and starting FFmpeg.
GATED_WORKER = """
import time
from pathlib import Path
from app import video_worker

original = video_worker.prepare_video

def gated(source, staging, run):
    gate = Path("/tmp/queue-gate")
    while not gate.exists():
        time.sleep(0.1)
    return original(source, staging, run)

video_worker.prepare_video = gated
video_worker.main()
"""


def compose(*args):
    return subprocess.check_output([*COMPOSE, *args], stdin=subprocess.DEVNULL, text=True).strip()


def sql(query):
    return compose("exec", "-T", "db", "psql", "-U", "yourtube", "-d", "yourtube", "-At",
                   "-v", "ON_ERROR_STOP=1", "-c", query)


def wait(owner, path, expected):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status = owner.request(path + "/status")
        if status["status"] == expected:
            return status
        if status["status"] == "failed":
            raise AssertionError(status)
        time.sleep(0.2)
    raise AssertionError(f"{expected}: {status}")


def upload(owner, content, *, private=False):
    query = urlencode({"title": "Queue integration test", "private": str(private).lower()})
    result = owner.request("/api/videos?" + query, 202, "POST", content)
    assert result["status"] == "queued"
    return "/api/videos/" + result["id"]


LOW_DISK_WORKER = """
from types import SimpleNamespace
from app import video_worker

video_worker.shutil.disk_usage = lambda path: SimpleNamespace(free=0)
video_worker.main()
"""

PARTIAL_PUBLISH_WORKER = """
import os
from pathlib import Path
from app import video_worker

replace = Path.replace

def crash_after_replace(source, target):
    result = replace(source, target)
    if source.name == "video.mp4":
        os._exit(91)
    return result

Path.replace = crash_after_replace
video_worker.main()
"""


@contextmanager
def test_worker(code):
    name = "yourtube-queue-" + uuid4().hex[:12]
    process = subprocess.Popen([
        *COMPOSE, "run", "--rm", "--no-deps", "--name", name,
        "-e", f"PGAPPNAME={name}", "-T", "worker", "python", "-c", code,
    ], stdin=subprocess.DEVNULL)
    try:
        yield name, process
    finally:
        if process.poll() is None:
            subprocess.run(["docker", "kill", name], check=False, stdout=subprocess.DEVNULL)
        process.wait(timeout=20)


def release_gate(name):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = subprocess.run(["docker", "exec", name, "touch", "/tmp/queue-gate"],
                                capture_output=True)
        if result.returncode == 0:
            return
        time.sleep(0.2)
    raise AssertionError(f"Worker gate unavailable: {result.stderr.decode()}")


def upload_again(owner, handle, content):
    sql(f"DELETE FROM upload_attempts WHERE user_id=(SELECT id FROM accounts WHERE handle='{handle}')")
    return upload(owner, content)


def video_id(path):
    return path.rsplit("/", 1)[1]


def wait_for_db(query, expected):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        actual = sql(query)
        if actual == expected:
            return
        time.sleep(0.2)
    raise AssertionError((query, expected, actual))


def check_parallel_limit(accounts, media):
    compose("stop", "-t", "5", "worker")
    jobs = [(owner, upload_again(owner, handle, media)) for owner, handle in accounts]
    with test_worker(GATED_WORKER) as (first, _), test_worker(GATED_WORKER) as (second, _):
        ids = ",".join(f"'{video_id(path)}'" for _, path in jobs)
        query = f"SELECT count(*) FROM videos WHERE id IN ({ids}) AND status='processing'"
        wait_for_db(query, "2")
        time.sleep(2)
        assert sql(query) == "2"
        assert sql(f"SELECT count(*) FROM videos WHERE id IN ({ids}) AND status='queued'") == "1"
        release_gate(first)
        release_gate(second)
        for owner, path in jobs:
            wait(owner, path, "ready")


def check_live_expired_lease(owner, handle, media):
    path = upload_again(owner, handle, media)
    job = video_id(path)
    with test_worker(GATED_WORKER) as (old, old_process):
        wait(owner, path, "processing")
        lease = sql(f"SELECT lease_token FROM videos WHERE id='{job}'")
        assert int(sql(f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{old}'")) > 0
        sql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='{old}'")
        sql(f"UPDATE videos SET lease_until=clock_timestamp() - interval '1 second' WHERE id='{job}'")
        with test_worker(GATED_WORKER) as (new, _):
            wait_for_db(f"SELECT attempts FROM videos WHERE id='{job}'", "2")
            assert sql(f"SELECT lease_token FROM videos WHERE id='{job}'") != lease
            assert old_process.poll() is None
            release_gate(old)
            deadline = time.monotonic() + 15
            while subprocess.run([*COMPOSE, "exec", "-T", "app", "test", "-d",
                                  f"/data/media/{job}/.processing-{lease}"],
                                 stdin=subprocess.DEVNULL).returncode == 0:
                assert time.monotonic() < deadline
                time.sleep(0.2)
            owner.request(path + "/stream", 404)
            release_gate(new)
            wait(owner, path, "ready")


def check_disk_floor(owner, handle, media):
    path = upload_again(owner, handle, media)
    with test_worker(LOW_DISK_WORKER):
        status = wait(owner, path, "failed")
        assert "Недостаточно места" in status["error"], status
        owner.request(path + "/stream", 404)
        job = video_id(path)
        assert sql(f"SELECT attempts FROM videos WHERE id='{job}'") == "3"
        wait_for_db(f"SELECT cleanup_pending FROM videos WHERE id='{job}'", "f")
        assert int(sql(f"SELECT reserved_bytes FROM videos WHERE id='{job}'")) == len(media)
        assert owner.request(path + "/status")["can_retry"]
    owner.request(path + "/retry", 202, "POST")
    with test_worker(GATED_WORKER) as (name, _):
        release_gate(name)
        wait(owner, path, "ready")


def check_partial_publish(owner, handle, media):
    path = upload_again(owner, handle, media)
    job = video_id(path)
    with test_worker(PARTIAL_PUBLISH_WORKER) as (_, process):
        assert process.wait(timeout=30) == 91
    wait(owner, path, "processing")
    compose("exec", "-T", "app", "test", "-s", f"/data/media/{job}/video.mp4")
    owner.request(path + "/stream", 404)
    owner.request(path + "/preview", 404)
    sql(f"UPDATE videos SET lease_until=clock_timestamp() - interval '1 second' WHERE id='{job}'")
    compose("start", "worker")
    wait(owner, path, "ready")
    assert sql(f"SELECT attempts FROM videos WHERE id='{job}'") == "2"
    owner.request(path + "/stream", 206, headers={"Range": "bytes=0-31"})
    owner.request(path + "/preview")
    compose("stop", "-t", "5", "worker")


def check_postgres_recovery(owner, handle, media):
    path = upload_again(owner, handle, media)
    job = video_id(path)
    with test_worker(GATED_WORKER) as (name, process):
        wait(owner, path, "processing")
        compose("stop", "db")
        try:
            release_gate(name)
            deadline = time.monotonic() + 15
            while "Worker iteration failed" not in subprocess.check_output(
                    ["docker", "logs", name], stderr=subprocess.STDOUT, text=True):
                assert time.monotonic() < deadline
                time.sleep(0.2)
        finally:
            compose("up", "-d", "--wait", "--wait-timeout", "60", "app")
        wait(owner, path, "ready")
        assert process.poll() is None
        assert sql(f"SELECT attempts FROM videos WHERE id='{job}'") == "2"


def main():
    owner, guest, other = Client(), Client(), Client()
    handle = owner.register("queue_owner")
    other_handle = other.register("queue_other")
    third = Client()
    third_handle = third.register("queue_third")
    with TemporaryDirectory() as tmp:
        file = Path(tmp) / "test.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=853x480:rate=12",
                        "-t", "2", "-c:v", "libx264rgb", str(file)], check=True)
        media = file.read_bytes()

    compose("stop", "-t", "5", "worker")
    path = upload(owner, media, private=True)
    assert wait(owner, path, "queued")["progress"] == 0
    for client in (guest, owner, other):
        client.request(path + "/stream", 404)
    guest.request(path + "/status", 401)
    other.request(path + "/status", 404)

    first = subprocess.Popen([*COMPOSE, "run", "--rm", "--no-deps", "--name", "yourtube-queue-gated",
                              "-T", "worker", "python", "-c", GATED_WORKER],
                             stdin=subprocess.DEVNULL)
    try:
        wait(owner, path, "processing")
        owner.request(path + "/stream", 404)
        other.request(path + "/status", 404)
        compose("start", "worker")
        video_id = path.rsplit("/", 1)[1]
        time.sleep(2)
        assert sql(f"SELECT attempts FROM videos WHERE id='{video_id}'") == "1"
    finally:
        subprocess.run(["docker", "kill", "yourtube-queue-gated"], check=False, stdout=subprocess.DEVNULL)
        first.wait(timeout=20)

    assert wait(owner, path, "ready")["progress"] == 100
    assert sql(f"SELECT attempts FROM videos WHERE id='{video_id}'") == "2"
    owner.request(path + "/stream", 206, headers={"Range": "bytes=0-31"})
    other.request(path + "/stream", 404)

    sql(f"DELETE FROM upload_attempts WHERE user_id=(SELECT id FROM accounts WHERE handle='{handle}')")
    bad = upload(owner, b"invalid file")
    assert wait(owner, bad, "failed")["error"]
    owner.request(bad + "/stream", 404)

    compose("stop", "-t", "5", "worker")
    sql(f"DELETE FROM upload_attempts WHERE user_id=(SELECT id FROM accounts WHERE handle='{handle}')")
    deleted = upload(owner, media)
    first = subprocess.Popen([*COMPOSE, "run", "--rm", "--no-deps", "--name", "yourtube-queue-gated",
                              "-T", "worker", "python", "-c", GATED_WORKER],
                             stdin=subprocess.DEVNULL)
    try:
        wait(owner, deleted, "processing")
        owner.request(deleted + "/stream", 404)
        owner.request(deleted, 202, "DELETE")
        subprocess.run(["docker", "exec", "yourtube-queue-gated", "touch", "/tmp/queue-gate"], check=True)
        deleted_id = deleted.rsplit("/", 1)[1]
        deadline = time.monotonic() + 40
        while sql(f"SELECT count(*) FROM pending_video_deletions WHERE video_id='{deleted_id}'") != "0":
            assert time.monotonic() < deadline
            time.sleep(0.5)
        owner.request(deleted + "/status", 404)
        guest.request(deleted + "/stream", 404)
    finally:
        subprocess.run(["docker", "kill", "yourtube-queue-gated"], check=False, stdout=subprocess.DEVNULL)
        first.wait(timeout=20)
        compose("start", "worker")
    check_parallel_limit([(owner, handle), (other, other_handle), (third, third_handle)], media)
    check_live_expired_lease(owner, handle, media)
    check_disk_floor(owner, handle, media)
    check_partial_publish(owner, handle, media)
    check_postgres_recovery(owner, handle, media)
    print("Video queue integration checks passed")


if __name__ == "__main__":
    main()


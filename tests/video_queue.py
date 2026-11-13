"""Queue integration checks against a disposable Compose project."""

import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

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


def main():
    owner, guest, other = Client(), Client(), Client()
    handle = owner.register("queue_owner")
    other.register("queue_other")
    with TemporaryDirectory() as tmp:
        file = Path(tmp) / "test.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=853x480:rate=12",
                        "-t", "2", "-c:v", "libx264rgb", str(file)], check=True)
        media = file.read_bytes()

    compose("stop", "worker")
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

    compose("stop", "worker")
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
    print("Video queue integration checks passed")


if __name__ == "__main__":
    main()


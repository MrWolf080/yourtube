
"""End-to-end checks for a disposable YourTube Compose project."""
import http.cookiejar
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

BASE = os.environ.get("YOURTUBE_TEST_URL", "http://127.0.0.1:8000")
PROJECT = os.environ["YOURTUBE_TEST_PROJECT"]
COMPOSE = ["docker", "compose", "-p", PROJECT]
SUFFIX = uuid4().hex[:8]


class Client:
    def __init__(self):
        self.http = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, status=200, method="GET", data=None, headers=None):
        headers = {"Origin": BASE, **(headers or {})}
        if data is not None:
            if isinstance(data, bytes):
                headers["Content-Type"] = "application/octet-stream"
            else:
                headers["Content-Type"] = "application/json"
                data = json.dumps(data).encode()
        try:
            response = self.http.open(Request(BASE + path, data, headers, method=method), timeout=45)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            assert response.status == status, (method, path, response.status, body[:300])
            if body and response.headers.get_content_type() == "application/json":
                return json.loads(body)
            return body

    def register(self, prefix):
        handle = prefix + SUFFIX
        self.request("/api/register", 201, "POST", {"handle": handle, "password": uuid4().hex})
        return handle


def main():
    guest, owner, viewer, moderator, admin = (Client() for _ in range(5))
    guest.request("/docs")
    guest.request("/static/pages.js")
    owner_handle = owner.register("owner")
    viewer_handle = viewer.register("viewer")
    moderator_handle = moderator.register("moderator")
    admin_handle = admin.register("admin")
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "sample.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=320x180:rate=12", "-t", "2", "-c:v", "libx264", str(video)], check=True)
        title = "Видео " + SUFFIX
        query = urlencode({"title": title, "category": "Кино", "private": "true"})
        video_id = owner.request("/api/videos?" + query, 202, "POST", video.read_bytes())["id"]
    path = "/api/videos/" + video_id
    deadline = time.monotonic() + 40
    while True:
        status = owner.request(path + "/status")
        if status["status"] == "ready":
            break
        assert status["status"] != "failed" and time.monotonic() < deadline, status
        time.sleep(0.2)
    for extension in ("", "/stream", "/preview", "/comments"):
        guest.request(path + extension, 404)
        moderator.request(path + extension, 404)
    assert owner.request(path + "/access")["users"] == []
    assert owner.request(path + "/preview").startswith(b"\xff\xd8")
    assert owner.request(path + "/stream", 206, headers={"Range": "bytes=0-31"})[4:8] == b"ftyp"
    owner.request(path + "/access/" + viewer_handle, 204, "PUT")
    assert viewer.request("/api/videos?feed=shared")[0]["id"] == video_id
    viewer.request(path + "/stream", 206, headers={"Range": "bytes=0-31"})
    assert guest.request("/api/videos?" + urlencode({"q": SUFFIX})) == []
    playlist = owner.request("/api/playlists", 201, "POST", {"title":"Мои видео"})["id"]
    playlist_path = "/api/playlists/" + str(playlist)
    owner.request(playlist_path + "/videos/" + video_id, 204, "PUT")
    assert len(viewer.request(playlist_path)["videos"]) == 1
    assert guest.request(playlist_path)["videos"] == []
    owner.request(path + "/access/" + viewer_handle, 204, "DELETE")
    viewer.request(path + "/preview", 404)
    assert viewer.request(playlist_path)["videos"] == []
    owner.request(path, method="PATCH", data={"title":title})
    assert guest.request("/api/videos?" + urlencode({"q": SUFFIX}))[0]["id"] == video_id
    assert guest.request(playlist_path)["videos"][0]["id"] == video_id
    viewer.request(path + "/like", 204, "PUT")
    viewer.request(path + "/like", 204, "PUT")
    viewer.request(path + "/comments", 201, "POST", {"body":"Комментарий"})
    viewer.request(f"/api/channels/{owner_handle}/subscription", 204, "PUT")
    assert viewer.request(path)["likes"] == 1
    assert viewer.request(path + "/comments")[0]["body"] == "Комментарий"
    assert viewer.request("/api/videos?feed=subscriptions")[0]["id"] == video_id
    viewer.request(path + "/reports", 201, "POST", {"body":"Жалоба"})
    viewer.request("/api/moderation/reports", 403)
    subprocess.run(COMPOSE + ["exec", "-T", "db", "psql", "-U", "yourtube", "-d", "yourtube",
                              "-v", "ON_ERROR_STOP=1", "-c",
                              f"UPDATE accounts SET role='admin' WHERE handle='{admin_handle}'"], check=True)
    admin.request("/api/admin/accounts/" + moderator_handle + "/role", 204, "PATCH", {"role":"moderator"})
    report = next(r for r in moderator.request("/api/moderation/reports") if r["video_id"] == video_id)
    moderator.request("/api/moderation/reports/" + str(report["id"]), 204, "PATCH", {"action":"hide"})
    for client in (guest, owner, viewer, moderator, admin):
        client.request(path + "/stream", 404)
    assert guest.request(playlist_path)["videos"] == []
    moderator.request("/api/moderation/videos/" + video_id, 204, "PATCH", {"hidden":False})
    guest.request(path + "/stream", 206, headers={"Range": "bytes=0-31"})
    print("Smoke checks passed")


if __name__ == "__main__":
    main()


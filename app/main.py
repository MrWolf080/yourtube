import hashlib
import json
import os
import secrets
import subprocess
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import APIKeyCookie
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, StringConstraints
from starlette.concurrency import run_in_threadpool

ROOT = Path(__file__).resolve().parent
MEDIA = Path(os.environ.get("MEDIA_DIR", "media")).resolve()
LIMIT = int(os.environ.get("UPLOAD_MAX_BYTES", 512 * 1024**2))
CATEGORIES = ("Кино", "Природа", "Путешествия", "Музыка", "Технологии", "Другое")
COOKIE = "yourtube_session"
cookie = APIKeyCookie(name=COOKIE, auto_error=False)
passwords = PasswordHasher()
pool = ConnectionPool(
    os.environ.get("DATABASE_URL", "postgresql:///yourtube"),
    kwargs={"row_factory": dict_row}, min_size=1, max_size=8, open=False,
)

@asynccontextmanager
async def lifespan(app):
    MEDIA.mkdir(parents=True, exist_ok=True)
    pool.open(wait=True)
    with pool.connection() as db:
        db.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))
    try:
        yield
    finally:
        pool.close()

app = FastAPI(title="YourTube API", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

Handle = Annotated[str, StringConstraints(to_lower=True, strip_whitespace=True, pattern=r"^[a-z0-9_]{3,30}$")]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Body = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]

class Credentials(BaseModel):
    handle: Handle
    password: Annotated[str, StringConstraints(min_length=8, max_length=128)]

class VideoInput(BaseModel):
    title: Title
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)] = ""
    category: Literal["Кино", "Природа", "Путешествия", "Музыка", "Технологии", "Другое"] = "Другое"
    private: bool = False

class TextInput(BaseModel):
    body: Body

class PlaylistInput(BaseModel):
    title: Title

class RoleInput(BaseModel):
    role: Literal["user", "moderator"]

class ReportDecision(BaseModel):
    action: Literal["hide", "dismiss"]

class VisibilityInput(BaseModel):
    hidden: bool

def fail(status, detail):
    raise HTTPException(status, detail)

def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()

def session_user(token: Annotated[str | None, Depends(cookie)]):
    if token and len(token) <= 128:
        with pool.connection() as db:
            return db.execute("""
                SELECT a.id, a.handle, a.role FROM sessions s JOIN accounts a ON a.id = s.user_id
                WHERE s.digest = %s AND s.expires > now()
            """, (digest(token),)).fetchone()
    return None

def signed_in(user=Depends(session_user)):
    if not user:
        fail(401, "Войдите в аккаунт")
    return user

def staff(user=Depends(signed_in)):
    if user["role"] not in ("moderator", "admin"):
        fail(403, "Недостаточно прав")
    return user

def admin(user=Depends(signed_in)):
    if user["role"] != "admin":
        fail(403, "Требуются права администратора")
    return user

@app.middleware("http")
async def same_origin(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        expected = os.environ.get("PUBLIC_ORIGIN") or f"{request.url.scheme}://{request.headers.get('host')}"
        if origin and origin != expected:
            return Response(status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return Response(status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

def login_session(db, response, user):
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM sessions WHERE expires <= now()")
    db.execute("INSERT INTO sessions VALUES (%s,%s,now() + interval '14 days')", (digest(token), user["id"]))
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax",
                        secure=os.environ.get("COOKIE_SECURE") == "1", max_age=14 * 86400)

@app.post("/api/register", tags=["Аккаунт"], status_code=201)
def register(payload: Credentials, response: Response):
    encoded = passwords.hash(payload.password)
    with pool.connection() as db:
        user = db.execute("""
            INSERT INTO accounts (handle,password_hash) VALUES (%s,%s)
            ON CONFLICT DO NOTHING RETURNING id,handle,role
        """, (payload.handle, encoded)).fetchone()
        if not user:
            fail(409, "Имя пользователя уже занято")
        login_session(db, response, user)
    return user

@app.post("/api/login", tags=["Аккаунт"])
def login(payload: Credentials, response: Response):
    with pool.connection() as db:
        user = db.execute("SELECT * FROM accounts WHERE handle=%s", (payload.handle,)).fetchone()
        try:
            if not user or not passwords.verify(user["password_hash"], payload.password):
                fail(401, "Неверное имя пользователя или пароль")
        except VerifyMismatchError:
            fail(401, "Неверное имя пользователя или пароль")
        user.pop("password_hash")
        login_session(db, response, user)
    return user

@app.post("/api/logout", tags=["Аккаунт"], status_code=204)
def logout(response: Response, token: Annotated[str | None, Depends(cookie)]):
    if token:
        with pool.connection() as db:
            db.execute("DELETE FROM sessions WHERE digest=%s", (digest(token),))
    response.delete_cookie(COOKIE)

@app.get("/api/me", tags=["Аккаунт"])
def me(user=Depends(session_user)):
    return user

SELECT_VIDEO = "SELECT v.*, a.handle FROM videos v JOIN accounts a ON a.id = v.owner_id"
VISIBLE = """NOT v.hidden AND (NOT v.private OR v.owner_id = %s OR EXISTS (
    SELECT 1 FROM grants g WHERE g.video_id=v.id AND g.user_id=%s
))"""

def viewer_id(user):
    return user["id"] if user else 0

def video_for(db, video_id, user):
    uid = viewer_id(user)
    video = db.execute(f"{SELECT_VIDEO} WHERE v.id=%s AND {VISIBLE}", (video_id, uid, uid)).fetchone()
    if not video:
        fail(404, "Видео недоступно")
    return video

def owner_video(db, video_id, user):
    video = db.execute("SELECT * FROM videos WHERE id=%s AND owner_id=%s FOR UPDATE",
                       (video_id, user["id"])).fetchone()
    if not video:
        fail(404, "Видео не найдено")
    return video

@app.get("/api/videos", tags=["Видео"])
def videos(q: str = Query("", max_length=120), category: str = Query("", max_length=40),
           feed: Literal["home", "subscriptions", "mine", "shared"] = "home",
           offset: int = Query(0, ge=0), user=Depends(session_user)):
    uid = viewer_id(user)
    if feed != "home" and not user:
        fail(401, "Войдите в аккаунт")
    filters = {
        "home": "NOT v.hidden AND NOT v.private",
        "subscriptions": """NOT v.hidden AND NOT v.private AND EXISTS (
            SELECT 1 FROM subscriptions s WHERE s.channel_id=v.owner_id AND s.user_id=%s)""",
        "mine": "v.owner_id=%s",
        "shared": """NOT v.hidden AND v.private AND EXISTS (
            SELECT 1 FROM grants g WHERE g.video_id=v.id AND g.user_id=%s)""",
    }
    args = [] if feed == "home" else [uid]
    where = filters[feed]
    if q.strip():
        where += " AND strpos(lower(v.title), lower(%s)) > 0"
        args.append(q.strip())
    if category:
        where += " AND v.category=%s"
        args.append(category)
    with pool.connection() as db:
        return db.execute(f"{SELECT_VIDEO} WHERE {where} ORDER BY v.created_at DESC,v.id LIMIT 24 OFFSET %s",
                          (*args, offset)).fetchall()

def prepare_video(source, directory):
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_entries",
             "stream=codec_type,codec_name:stream_disposition=attached_pic:format=duration,format_name",
             "-of", "json", str(source)], capture_output=True, check=True, timeout=20,
        )
        info = json.loads(probe.stdout)
        formats = set(info["format"]["format_name"].split(","))
        if not formats.intersection({"mov", "mp4", "matroska", "webm", "avi", "mpegts", "mpeg", "flv", "ogg"}):
            raise ValueError
        streams = info.get("streams", [])
        index = next(i for i, stream in enumerate(streams) if stream["codec_type"] == "video"
                     and not stream.get("disposition", {}).get("attached_pic"))
        duration = round(float(info["format"]["duration"]))
        if duration < 0:
            raise ValueError
        ffmpeg = ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file", "-threads", "2", "-i", str(source)]
        subprocess.run(ffmpeg + [
            "-map", f"0:{index}", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1",
            "-vf", "scale=w='min(1920,iw)':h=-2", "-pix_fmt", "yuv420p", "-c:v", "libx264",
            "-preset", "veryfast", "-threads", "2", "-c:a", "aac", "-movflags", "+faststart",
            str(directory / "video.mp4"),
        ], capture_output=True, check=True, timeout=900)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(min(2, duration / 4)),
                        "-i", str(directory / "video.mp4"), "-frames:v", "1", "-vf", "scale=640:-2",
                        str(directory / "preview.jpg")], capture_output=True, check=True, timeout=30)
        if not (directory / "preview.jpg").is_file():
            raise ValueError
        return duration
    except (subprocess.SubprocessError, KeyError, StopIteration, ValueError, OverflowError):
        fail(422, "Не удалось обработать видео. Попробуйте другой файл.")

def publish_video(directory, video_id, metadata, duration, user):
    final = MEDIA / str(video_id)
    directory.rename(final)
    try:
        with pool.connection() as db:
            db.execute("""
                INSERT INTO videos (id,owner_id,title,description,category,private,duration)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (video_id, user["id"], metadata.title, metadata.description,
                  metadata.category, metadata.private, duration))
    except Exception:
        final.rename(directory)
        raise

@app.post("/api/videos", tags=["Видео"], status_code=201, openapi_extra={"requestBody": {
    "required": True, "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
}})
async def upload(request: Request, title: Title, description: str = Query("", max_length=5000),
                 category: Literal["Кино", "Природа", "Путешествия", "Музыка", "Технологии", "Другое"] = "Другое",
                 private: bool = False, user=Depends(signed_in)):
    metadata = VideoInput(title=title, description=description, category=category, private=private)
    length = request.headers.get("content-length", "")
    if length.isdecimal() and int(length) > LIMIT:
        fail(413, "Файл слишком большой")
    video_id = uuid4()
    with tempfile.TemporaryDirectory(prefix=".upload-", dir=MEDIA) as tmp:
        directory = Path(tmp)
        source = directory / "source"
        size = 0
        with source.open("wb") as target:
            async for chunk in request.stream():
                size += len(chunk)
                if size > LIMIT:
                    fail(413, "Файл слишком большой")
                if chunk:
                    await run_in_threadpool(target.write, chunk)
        if not size:
            fail(422, "Выберите видеофайл")
        duration = await run_in_threadpool(prepare_video, source, directory)
        source.unlink()
        await run_in_threadpool(publish_video, directory, video_id, metadata, duration, user)
    return {"id": video_id}

@app.get("/api/videos/{video_id}", tags=["Видео"])
def get_video(video_id: UUID, user=Depends(session_user)):
    uid = viewer_id(user)
    with pool.connection() as db:
        video = video_for(db, video_id, user)
        video.update(db.execute("""
            SELECT (SELECT count(*) FROM likes WHERE video_id=%s) AS likes,
            EXISTS(SELECT 1 FROM likes WHERE video_id=%s AND user_id=%s) AS liked,
            (SELECT count(*) FROM subscriptions WHERE channel_id=%s) AS subscribers,
            EXISTS(SELECT 1 FROM subscriptions WHERE channel_id=%s AND user_id=%s) AS subscribed
        """, (video_id, video_id, uid, video["owner_id"], video["owner_id"], uid)).fetchone())
        return video

@app.post("/api/videos/{video_id}/views", tags=["Видео"])
def count_view(video_id: UUID, user=Depends(session_user)):
    uid = viewer_id(user)
    with pool.connection() as db:
        row = db.execute(f"UPDATE videos v SET views=views+1 WHERE v.id=%s AND {VISIBLE} RETURNING views",
                         (video_id, uid, uid)).fetchone()
        if not row:
            fail(404, "Видео недоступно")
        return row

def serve_media(video_id, user, filename, mime):
    with pool.connection() as db:
        video_for(db, video_id, user)
    path = MEDIA / str(video_id) / filename
    if not path.is_file():
        fail(404, "Файл недоступен")
    return FileResponse(path, media_type=mime)

@app.get("/api/videos/{video_id}/stream", tags=["Видео"], response_class=FileResponse)
def stream(video_id: UUID, user=Depends(session_user)):
    return serve_media(video_id, user, "video.mp4", "video/mp4")

@app.get("/api/videos/{video_id}/preview", tags=["Видео"], response_class=FileResponse)
def preview(video_id: UUID, user=Depends(session_user)):
    return serve_media(video_id, user, "preview.jpg", "image/jpeg")

@app.patch("/api/videos/{video_id}", tags=["Видео"])
def edit_video(video_id: UUID, payload: VideoInput, user=Depends(signed_in)):
    with pool.connection() as db:
        owner_video(db, video_id, user)
        db.execute("UPDATE videos SET title=%s,description=%s,category=%s,private=%s WHERE id=%s",
                   (payload.title, payload.description, payload.category, payload.private, video_id))
    return {"id": video_id}

@app.get("/api/videos/{video_id}/access", tags=["Приватность"])
def list_access(video_id: UUID, user=Depends(signed_in)):
    with pool.connection() as db:
        video = owner_video(db, video_id, user)
        users = db.execute("""
            SELECT a.handle FROM grants g JOIN accounts a ON a.id=g.user_id
            WHERE g.video_id=%s ORDER BY a.handle
        """, (video_id,)).fetchall()
        return {"video": video, "users": users}

@app.put("/api/videos/{video_id}/access/{handle}", tags=["Приватность"], status_code=204)
def grant_access(video_id: UUID, handle: Handle, user=Depends(signed_in)):
    with pool.connection() as db:
        owner_video(db, video_id, user)
        account = channel_account(db, handle)
        if account["id"] == user["id"]:
            fail(400, "У владельца уже есть доступ")
        db.execute("INSERT INTO grants VALUES (%s,%s) ON CONFLICT DO NOTHING", (video_id, account["id"]))

@app.delete("/api/videos/{video_id}/access/{handle}", tags=["Приватность"], status_code=204)
def revoke_access(video_id: UUID, handle: Handle, user=Depends(signed_in)):
    with pool.connection() as db:
        owner_video(db, video_id, user)
        db.execute("DELETE FROM grants WHERE video_id=%s AND user_id=(SELECT id FROM accounts WHERE handle=%s)",
                   (video_id, handle))

@app.put("/api/videos/{video_id}/like", tags=["Видео"], status_code=204)
def like(video_id: UUID, user=Depends(signed_in)):
    with pool.connection() as db:
        video_for(db, video_id, user)
        db.execute("INSERT INTO likes VALUES (%s,%s) ON CONFLICT DO NOTHING", (video_id, user["id"]))

@app.delete("/api/videos/{video_id}/like", tags=["Видео"], status_code=204)
def unlike(video_id: UUID, user=Depends(signed_in)):
    with pool.connection() as db:
        video_for(db, video_id, user)
        db.execute("DELETE FROM likes WHERE video_id=%s AND user_id=%s", (video_id, user["id"]))

@app.get("/api/videos/{video_id}/comments", tags=["Видео"])
def comments(video_id: UUID, offset: int = Query(0, ge=0), user=Depends(session_user)):
    with pool.connection() as db:
        video_for(db, video_id, user)
        return db.execute("""
            SELECT c.id,c.body,c.created_at,a.handle FROM comments c JOIN accounts a ON a.id=c.user_id
            WHERE c.video_id=%s ORDER BY c.id DESC LIMIT 50 OFFSET %s
        """, (video_id, offset)).fetchall()

@app.post("/api/videos/{video_id}/comments", tags=["Видео"], status_code=201)
def comment(video_id: UUID, payload: TextInput, user=Depends(signed_in)):
    with pool.connection() as db:
        video_for(db, video_id, user)
        return db.execute("INSERT INTO comments (video_id,user_id,body) VALUES (%s,%s,%s) RETURNING id",
                          (video_id, user["id"], payload.body)).fetchone()

def channel_account(db, handle):
    account = db.execute("SELECT id,handle FROM accounts WHERE handle=%s", (handle,)).fetchone()
    if not account:
        fail(404, "Канал не найден")
    return account

@app.get("/api/channels/{handle}", tags=["Каналы"])
def channel(handle: Handle, offset: int = Query(0, ge=0), user=Depends(session_user)):
    uid = viewer_id(user)
    with pool.connection() as db:
        account = channel_account(db, handle)
        cid = account["id"]
        account.update(db.execute("""
            SELECT count(*) AS subscribers, coalesce(bool_or(user_id=%s),false) AS subscribed
            FROM subscriptions WHERE channel_id=%s
        """, (uid, cid)).fetchone())
        account["videos"] = db.execute(f"""
            {SELECT_VIDEO} WHERE v.owner_id=%s AND {VISIBLE}
            ORDER BY v.created_at DESC,v.id LIMIT 24 OFFSET %s
        """, (cid, uid, uid, offset)).fetchall()
        account["playlists"] = db.execute("SELECT id,title FROM playlists WHERE owner_id=%s ORDER BY id DESC",
                                          (cid,)).fetchall()
        return account

@app.put("/api/channels/{handle}/subscription", tags=["Каналы"], status_code=204)
def subscribe(handle: Handle, user=Depends(signed_in)):
    with pool.connection() as db:
        account = channel_account(db, handle)
        if account["id"] == user["id"]:
            fail(400, "На свой канал подписаться нельзя")
        db.execute("INSERT INTO subscriptions VALUES (%s,%s) ON CONFLICT DO NOTHING",
                   (account["id"], user["id"]))

@app.delete("/api/channels/{handle}/subscription", tags=["Каналы"], status_code=204)
def unsubscribe(handle: Handle, user=Depends(signed_in)):
    with pool.connection() as db:
        account = channel_account(db, handle)
        db.execute("DELETE FROM subscriptions WHERE channel_id=%s AND user_id=%s", (account["id"], user["id"]))

@app.post("/api/playlists", tags=["Плейлисты"], status_code=201)
def new_playlist(payload: PlaylistInput, user=Depends(signed_in)):
    with pool.connection() as db:
        return db.execute("INSERT INTO playlists (owner_id,title) VALUES (%s,%s) RETURNING id",
                          (user["id"], payload.title)).fetchone()

def playlist_for(db, playlist_id):
    playlist = db.execute("""
        SELECT p.*,a.handle FROM playlists p JOIN accounts a ON a.id=p.owner_id WHERE p.id=%s
    """, (playlist_id,)).fetchone()
    if not playlist:
        fail(404, "Плейлист не найден")
    return playlist

@app.get("/api/playlists/{playlist_id}", tags=["Плейлисты"])
def get_playlist(playlist_id: int, offset: int = Query(0, ge=0), user=Depends(session_user)):
    uid = viewer_id(user)
    with pool.connection() as db:
        playlist = playlist_for(db, playlist_id)
        playlist["videos"] = db.execute(f"""
            {SELECT_VIDEO} JOIN playlist_items i ON i.video_id=v.id
            WHERE i.playlist_id=%s AND {VISIBLE} ORDER BY i.added_at,i.video_id LIMIT 24 OFFSET %s
        """, (playlist_id, uid, uid, offset)).fetchall()
        return playlist

@app.put("/api/playlists/{playlist_id}/videos/{video_id}", tags=["Плейлисты"], status_code=204)
def add_to_playlist(playlist_id: int, video_id: UUID, user=Depends(signed_in)):
    with pool.connection() as db:
        if playlist_for(db, playlist_id)["owner_id"] != user["id"]:
            fail(403, "Плейлист принадлежит другому пользователю")
        video_for(db, video_id, user)
        db.execute("INSERT INTO playlist_items (playlist_id,video_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                   (playlist_id, video_id))

@app.delete("/api/playlists/{playlist_id}/videos/{video_id}", tags=["Плейлисты"], status_code=204)
def remove_from_playlist(playlist_id: int, video_id: UUID, user=Depends(signed_in)):
    with pool.connection() as db:
        if playlist_for(db, playlist_id)["owner_id"] != user["id"]:
            fail(403, "Плейлист принадлежит другому пользователю")
        db.execute("DELETE FROM playlist_items WHERE playlist_id=%s AND video_id=%s", (playlist_id, video_id))

@app.post("/api/videos/{video_id}/reports", tags=["Модерация"], status_code=201)
def report(video_id: UUID, payload: TextInput, user=Depends(signed_in)):
    with pool.connection() as db:
        video_for(db, video_id, user)
        row = db.execute("""
            INSERT INTO reports (video_id,reporter_id,reason) VALUES (%s,%s,%s)
            ON CONFLICT DO NOTHING RETURNING id
        """, (video_id, user["id"], payload.body)).fetchone()
        if not row:
            fail(409, "Ваша жалоба уже на рассмотрении")
        return row

@app.get("/api/moderation/reports", tags=["Модерация"])
def reports(status: Literal["open", "dismissed", "hidden"] = "open",
            offset: int = Query(0, ge=0), user=Depends(staff)):
    with pool.connection() as db:
        return db.execute("""
            SELECT r.id,r.video_id,r.reason,r.status,r.created_at,v.hidden,
                CASE WHEN v.private THEN NULL ELSE v.title END AS title,a.handle
            FROM reports r JOIN videos v ON v.id=r.video_id JOIN accounts a ON a.id=r.reporter_id
            WHERE r.status=%s ORDER BY r.id DESC LIMIT 50 OFFSET %s
        """, (status, offset)).fetchall()

@app.patch("/api/moderation/reports/{report_id}", tags=["Модерация"], status_code=204)
def resolve_report(report_id: int, payload: ReportDecision, user=Depends(staff)):
    with pool.connection() as db:
        row = db.execute("SELECT video_id FROM reports WHERE id=%s", (report_id,)).fetchone()
        if not row:
            fail(404, "Жалоба не найдена")
        video_id = row["video_id"]
        db.execute("SELECT id FROM videos WHERE id=%s FOR UPDATE", (video_id,))
        decision = "hidden" if payload.action == "hide" else "dismissed"
        updated = db.execute("""
            UPDATE reports SET status=%s,reviewed_by=%s WHERE id=%s AND status='open' RETURNING id
        """, (decision, user["id"], report_id)).fetchone()
        if not updated:
            fail(409, "Жалоба уже рассмотрена")
        if payload.action == "hide":
            db.execute("UPDATE videos SET hidden=true WHERE id=%s", (video_id,))
            db.execute("UPDATE reports SET status='hidden',reviewed_by=%s WHERE video_id=%s AND status='open'",
                       (user["id"], video_id))

@app.patch("/api/moderation/videos/{video_id}", tags=["Модерация"], status_code=204)
def set_hidden(video_id: UUID, payload: VisibilityInput, user=Depends(staff)):
    with pool.connection() as db:
        row = db.execute("UPDATE videos SET hidden=%s WHERE id=%s RETURNING id", (payload.hidden, video_id)).fetchone()
        if not row:
            fail(404, "Видео не найдено")
        if payload.hidden:
            db.execute("UPDATE reports SET status='hidden',reviewed_by=%s WHERE video_id=%s AND status='open'",
                       (user["id"], video_id))

@app.patch("/api/admin/accounts/{handle}/role", tags=["Администрирование"], status_code=204)
def change_role(handle: Handle, payload: RoleInput, user=Depends(admin)):
    with pool.connection() as db:
        row = db.execute("UPDATE accounts SET role=%s WHERE handle=%s AND role<>'admin' RETURNING id",
                         (payload.role, handle)).fetchone()
        if not row:
            fail(404, "Пользователь для назначения роли не найден")

@app.get("/", include_in_schema=False)
@app.get("/watch/{video_id}", include_in_schema=False)
@app.get("/channel/{handle}", include_in_schema=False)
@app.get("/playlist/{playlist_id}", include_in_schema=False)
@app.get("/manage/{video_id}", include_in_schema=False)
@app.get("/search", include_in_schema=False)
@app.get("/upload", include_in_schema=False)
@app.get("/login", include_in_schema=False)
@app.get("/register", include_in_schema=False)
@app.get("/subscriptions", include_in_schema=False)
@app.get("/mine", include_in_schema=False)
@app.get("/shared", include_in_schema=False)
@app.get("/moderation", include_in_schema=False)
def frontend():
    return FileResponse(ROOT / "static" / "index.html", media_type="text/html")


"""Shared PostgreSQL rate limits for password endpoints."""

import logging
from contextlib import contextmanager
from ipaddress import ip_address
from threading import BoundedSemaphore

from fastapi import HTTPException

logger = logging.getLogger("uvicorn.error")

LOGIN_IP = ("login_ip", 20, 300)
LOGIN_ACCOUNT = ("login_account", 5, 300)
REGISTER_IP = ("register_ip", 5, 86400)
AUTH_GLOBAL = ("auth_global", 120, 60)
password_slots = BoundedSemaphore(2)


def client_ip(request):
    if request.client is None:
        return "unknown"
    try:
        address = ip_address(request.client.host)
    except ValueError:
        return "unknown"
    return str(getattr(address, "ipv4_mapped", None) or address)


def audit(event, ip, handle, *, reason):
    logger.warning("auth event=%s ip=%s handle=%s reason=%s", event, ip, handle, reason)


def consume(db, policy, key):
    scope, limit, seconds = policy
    accepted = db.execute("""
        INSERT INTO auth_rate_limits (scope, subject, attempts, expires_at)
        VALUES (%s,%s,1,clock_timestamp() + make_interval(secs => %s))
        ON CONFLICT (scope, subject) DO UPDATE SET
            attempts = CASE WHEN auth_rate_limits.expires_at <= clock_timestamp()
                            THEN 1 ELSE auth_rate_limits.attempts + 1 END,
            expires_at = CASE WHEN auth_rate_limits.expires_at <= clock_timestamp()
                              THEN excluded.expires_at ELSE auth_rate_limits.expires_at END
        WHERE auth_rate_limits.expires_at <= clock_timestamp() OR auth_rate_limits.attempts < %s
        RETURNING attempts
    """, (scope, key, seconds, limit)).fetchone()
    if accepted:
        return None, accepted["attempts"] == 1
    return db.execute("""
        SELECT greatest(1, ceil(extract(epoch FROM expires_at - clock_timestamp())))::integer AS seconds
        FROM auth_rate_limits WHERE scope=%s AND subject=%s
    """, (scope, key)).fetchone()["seconds"], False


def enforce(pool, action, ip, handle):
    policies = [(REGISTER_IP if action == "register" else LOGIN_IP, ip), (AUTH_GLOBAL, "*")]
    if action == "login":
        policies.append((LOGIN_ACCOUNT, handle))

    cleanup = False
    with pool.connection() as db:
        for policy, key in policies:
            retry_after, opened_window = consume(db, policy, key)
            if policy == AUTH_GLOBAL:
                cleanup = opened_window
            if retry_after is not None:
                break
    if cleanup:
        with pool.connection() as db:
            db.execute("""
                WITH expired AS (
                    SELECT scope,subject FROM auth_rate_limits
                    WHERE expires_at < clock_timestamp() - interval '1 day'
                    LIMIT 500 FOR UPDATE SKIP LOCKED
                )
                DELETE FROM auth_rate_limits r USING expired
                WHERE r.scope=expired.scope AND r.subject=expired.subject
            """)
    if retry_after is None:
        return
    audit("rate_limited", ip, handle, reason=policy[0])
    raise HTTPException(429, "Слишком много попыток. Повторите позже.",
                        headers={"Retry-After": str(retry_after)})



@contextmanager
def password_slot(ip, handle):
    if not password_slots.acquire(blocking=False):
        audit("busy", ip, handle, reason="password_slots")
        raise HTTPException(503, "Сервер занят. Повторите позже.", headers={"Retry-After": "2"})
    try:
        yield
    finally:
        password_slots.release()

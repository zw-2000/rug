"""Server-side sessions, signed cookies, CSRF tokens, login throttling, client address.

The cookie carries `token.signature`. Only sha256(token) is stored, so a database leak does
not yield usable sessions, and the HMAC (keyed by RUG_SESSION_SECRET) lets forged or damaged
cookies be rejected without a database lookup. Sessions expire absolutely (no sliding window)
and hold a snapshot of the user's AD groups taken at login.
"""

import hashlib
import hmac
import ipaddress
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from rug import audit
from rug.auth.ldap import Identity
from rug.config import Settings
from rug.db.models import SessionRow, User
from rug.perms import Principal

COOKIE_NAME = "rug_session"
MIN_SECRET_CHARS = 32


class ConfigError(Exception):
    pass


def check_secret(settings: Settings) -> None:
    if len(settings.session_secret) < MIN_SECRET_CHARS:
        raise ConfigError(
            f"RUG_SESSION_SECRET must be set to at least {MIN_SECRET_CHARS} random characters "
            '(e.g. python -c "import secrets; print(secrets.token_urlsafe(48))")'
        )


def _sig(settings: Settings, token: str) -> str:
    return hmac.new(settings.session_secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def _id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Active:
    principal: Principal
    csrf_token: str
    session_id: str


def create(db: Session, settings: Settings, identity: Identity, ip: str | None) -> tuple[str, str]:
    """Start a session. Returns (cookie value, csrf token)."""
    check_secret(settings)
    now = datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    db.add(
        SessionRow(
            id=_id(token),
            username=identity.username,
            display_name=identity.display_name,
            groups=sorted(identity.groups),
            is_admin=identity.is_admin,
            csrf_token=csrf,
            created_at=now,
            expires_at=now + timedelta(seconds=settings.session_ttl_s),
            last_seen=now,
            ip=ip,
        )
    )
    user = db.get(User, identity.username)
    if user is None:
        db.add(
            User(
                username=identity.username,
                display_name=identity.display_name,
                first_login=now,
                last_login=now,
            )
        )
    else:
        user.display_name, user.last_login = identity.display_name, now
    db.flush()
    return f"{token}.{_sig(settings, token)}", csrf


def lookup(db: Session, settings: Settings, cookie: str | None) -> Active | None:
    """The live session behind a cookie, or None (bad signature, unknown, expired, revoked,
    or the user has been disabled)."""
    if not cookie or "." not in cookie or len(cookie) > 256:
        return None
    token, _, sig = cookie.rpartition(".")
    if not hmac.compare_digest(sig, _sig(settings, token)):
        return None
    row = db.get(SessionRow, _id(token))
    now = datetime.now(UTC)
    if row is None:
        return None
    if row.expires_at <= now:
        db.delete(row)
        db.flush()
        return None
    user = db.get(User, row.username)
    if user is not None and user.disabled:
        return None
    row.last_seen = now
    db.flush()
    return Active(
        Principal(row.username, row.display_name, frozenset(row.groups), row.is_admin),
        row.csrf_token,
        row.id,
    )


def csrf_ok(active: Active, header_value: str | None) -> bool:
    return bool(header_value) and hmac.compare_digest(header_value or "", active.csrf_token)


def destroy(db: Session, session_id: str) -> None:
    db.execute(delete(SessionRow).where(SessionRow.id == session_id))


def purge_expired(db: Session) -> int:
    rows = db.scalars(select(SessionRow).where(SessionRow.expires_at <= datetime.now(UTC))).all()
    for r in rows:
        db.delete(r)
    db.flush()
    return len(rows)


# -- login throttling (counted from the audit log, so it survives restarts) ------------------


def throttle_key(username: str) -> str:
    """Actor recorded for a failed attempt: bounded so junk input cannot bloat the log."""
    return username.strip().lower()[:64]


def login_blocked(db: Session, settings: Settings, username: str, ip: str | None) -> bool:
    w = settings.login_window_s
    if audit.count_since(db, "login.fail", w, actor=throttle_key(username)) >= (
        settings.login_max_failures_user
    ):
        return True
    return bool(ip) and (
        audit.count_since(db, "login.fail", w, ip=ip) >= settings.login_max_failures_ip
    )


# -- client address ---------------------------------------------------------------------------


def _trusted(addr: str, trusted: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    for t in trusted:
        try:
            if ip in ipaddress.ip_network(t, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(peer: str | None, headers: Mapping[str, str], settings: Settings) -> str | None:
    """The caller's address. X-Forwarded-For is believed only when the direct peer is a
    configured proxy, and then read from the right, skipping other trusted proxies, so a
    client cannot choose its own address by sending the header."""
    if not peer:
        return None
    if not _trusted(peer, settings.trusted_proxies):
        return peer
    hops = [h.strip() for h in headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for hop in reversed(hops):
        if not _trusted(hop, settings.trusted_proxies):
            try:
                return str(ipaddress.ip_address(hop))
            except ValueError:
                return peer
    return peer

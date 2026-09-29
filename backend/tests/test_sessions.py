from datetime import UTC, datetime, timedelta

import pytest

from rug import audit
from rug.auth import sessions
from rug.auth.ldap import Identity
from rug.config import Settings
from rug.db.models import SessionRow, User

SECRET = "x" * 40
ANN = Identity("ann", "Ann A", frozenset({"cn=sales,dc=x"}), False)


def cfg(**kw):
    return Settings(_env_file=None, session_secret=SECRET, **kw)


def test_secret_required():
    with pytest.raises(sessions.ConfigError):
        sessions.check_secret(Settings(_env_file=None, session_secret=""))
    with pytest.raises(sessions.ConfigError):
        sessions.check_secret(Settings(_env_file=None, session_secret="short"))
    sessions.check_secret(cfg())


def test_roundtrip_and_snapshot(db):
    cookie, csrf = sessions.create(db, cfg(), ANN, "10.0.0.1")
    db.commit()
    active = sessions.lookup(db, cfg(), cookie)
    assert active and active.principal.username == "ann"
    assert active.principal.groups == {"cn=sales,dc=x"} and active.csrf_token == csrf
    assert not active.principal.is_admin
    assert db.get(User, "ann").last_login is not None


def test_only_a_hash_is_stored(db):
    cookie, _ = sessions.create(db, cfg(), ANN, None)
    db.commit()
    token = cookie.rsplit(".", 1)[0]
    assert db.get(SessionRow, token) is None
    assert db.get(SessionRow, sessions._id(token)) is not None


@pytest.mark.parametrize(
    "mangle",
    [
        lambda c: c[:-1] + ("0" if c[-1] != "0" else "1"),
        lambda c: "x" + c,
        lambda c: c.split(".")[0],
        lambda c: "",
        lambda c: "." + c,
        lambda c: c * 20,
    ],
)
def test_forged_cookies_rejected(db, mangle):
    cookie, _ = sessions.create(db, cfg(), ANN, None)
    db.commit()
    assert sessions.lookup(db, cfg(), mangle(cookie)) is None


def test_cookie_signed_with_other_secret_rejected(db):
    cookie, _ = sessions.create(db, cfg(), ANN, None)
    db.commit()
    other = Settings(_env_file=None, session_secret="y" * 40)
    assert sessions.lookup(db, other, cookie) is None


def test_expiry_logout_revoke_disable(db):
    cookie, _ = sessions.create(db, cfg(), ANN, None)
    db.commit()
    row = db.query(SessionRow).one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert sessions.lookup(db, cfg(), cookie) is None
    assert db.query(SessionRow).count() == 0  # expired row removed

    cookie, _ = sessions.create(db, cfg(), ANN, None)
    active = sessions.lookup(db, cfg(), cookie)
    sessions.destroy(db, active.session_id)
    assert sessions.lookup(db, cfg(), cookie) is None

    cookie, _ = sessions.create(db, cfg(), ANN, None)
    db.get(User, "ann").disabled = True
    db.flush()
    assert sessions.lookup(db, cfg(), cookie) is None


def test_ttl_is_absolute(db):
    cookie, _ = sessions.create(db, cfg(session_ttl_s=100), ANN, None)
    row = db.query(SessionRow).one()
    assert 90 < (row.expires_at - row.created_at).total_seconds() <= 100
    sessions.lookup(db, cfg(), cookie)
    assert (db.query(SessionRow).one().expires_at - row.created_at).total_seconds() <= 100


def test_csrf(db):
    cookie, csrf = sessions.create(db, cfg(), ANN, None)
    active = sessions.lookup(db, cfg(), cookie)
    assert sessions.csrf_ok(active, csrf)
    assert not sessions.csrf_ok(active, None)
    assert not sessions.csrf_ok(active, "")
    assert not sessions.csrf_ok(active, csrf + "x")


def test_purge_expired(db):
    sessions.create(db, cfg(session_ttl_s=-5), ANN, None)
    sessions.create(db, cfg(), Identity("bob", "Bob", frozenset(), False), None)
    assert sessions.purge_expired(db) == 1


def test_throttle_by_user_and_ip(db):
    s = cfg(login_max_failures_user=3, login_max_failures_ip=5)
    for _ in range(3):
        audit.log(db, "ann", "login.fail", ip="1.1.1.1")
    assert sessions.login_blocked(db, s, "ANN ", "2.2.2.2")  # same user, other address
    assert not sessions.login_blocked(db, s, "bob", "2.2.2.2")
    for i in range(5):
        audit.log(db, f"user{i}", "login.fail", ip="3.3.3.3")
    assert sessions.login_blocked(db, s, "fresh", "3.3.3.3")  # one address, many names
    assert not sessions.login_blocked(db, s, "fresh", None)


def test_client_ip_trusts_forwarded_header_only_from_proxies():
    direct = cfg()
    assert sessions.client_ip("9.9.9.9", {"x-forwarded-for": "1.2.3.4"}, direct) == "9.9.9.9"
    proxied = cfg(trusted_proxies=["10.0.0.0/8"])
    h = {"x-forwarded-for": "6.6.6.6, 1.2.3.4, 10.1.1.1"}
    assert sessions.client_ip("10.0.0.5", h, proxied) == "1.2.3.4"  # spoofed left entry ignored
    assert sessions.client_ip("10.0.0.5", {}, proxied) == "10.0.0.5"
    assert sessions.client_ip("10.0.0.5", {"x-forwarded-for": "junk"}, proxied) == "10.0.0.5"
    assert sessions.client_ip("8.8.8.8", h, proxied) == "8.8.8.8"
    assert sessions.client_ip(None, h, proxied) is None

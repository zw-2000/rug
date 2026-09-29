import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from rug import audit


def test_log_and_recent_filters(db):
    audit.log(db, "ann", "login.ok", "ann", ip="10.0.0.1")
    audit.log(db, "bob", "download", "doc-1")
    db.commit()
    assert [e.action for e in audit.recent(db)] == ["download", "login.ok"]
    assert [e.actor for e in audit.recent(db, action="login.ok")] == ["ann"]
    assert [e.actor for e in audit.recent(db, actor="bob")] == ["bob"]
    newest = audit.recent(db, limit=1)[0]
    assert [e.actor for e in audit.recent(db, before_id=newest.id)] == ["ann"]


def test_secrets_are_scrubbed(db):
    audit.log(
        db, "ann", "login.fail", "ann", {"password": "hunter2", "Session_Token": "abc", "why": "x"}
    )
    db.commit()
    detail = audit.recent(db)[0].detail
    assert detail == {"password": "[redacted]", "Session_Token": "[redacted]", "why": "x"}


def test_append_only_trigger(db):
    audit.log(db, "ann", "login.ok")
    db.commit()
    for stmt in ("UPDATE audit_log SET actor = 'evil'", "DELETE FROM audit_log"):
        with pytest.raises(DBAPIError):
            db.execute(text(stmt))
        db.rollback()
    assert audit.recent(db)[0].actor == "ann"


def test_count_since_by_actor_and_ip(db):
    for _ in range(3):
        audit.log(db, "ann", "login.fail", ip="1.1.1.1")
    audit.log(db, "bob", "login.fail", ip="1.1.1.1")
    audit.log(db, "ann", "login.ok", ip="1.1.1.1")
    db.commit()
    assert audit.count_since(db, "login.fail", 600, actor="ann") == 3
    assert audit.count_since(db, "login.fail", 600, ip="1.1.1.1") == 4
    assert audit.count_since(db, "login.fail", 600, ip="2.2.2.2") == 0


def test_entry_rolls_back_with_the_change(db):
    audit.log(db, "ann", "x")
    db.rollback()
    assert audit.recent(db) == []

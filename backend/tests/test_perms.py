import pytest

from rug import audit, perms
from rug.perms import Principal

GROUP = "CN=Sales,OU=Groups,DC=corp,DC=local"


def who(name="ann", groups=(GROUP,), admin=False):
    return Principal(name, name, frozenset(perms.normalize_dn(g) for g in groups), admin)


def test_no_grants_means_nothing(db):
    assert perms.effective_folders(db, who()) == frozenset()
    assert perms.effective_folders(db, who(groups=())) == frozenset()


def test_group_map_and_dn_normalisation(db):
    perms.set_group_folders(
        db, "root", "cn=sales, ou=Groups,dc=corp,dc=local", ["sales", "delivery"]
    )
    db.commit()
    assert perms.effective_folders(db, who()) == {"sales", "delivery"}
    assert perms.effective_folders(db, who(groups=("CN=Other,DC=corp,DC=local",))) == frozenset()


def test_allow_deny_and_deny_wins(db):
    perms.set_group_folders(db, "root", GROUP, ["sales", "delivery"])
    perms.set_override(db, "root", "ann", "legal", "allow")
    perms.set_override(db, "root", "ann", "delivery", "deny")
    perms.set_override(db, "root", "ann", "sales", "allow")
    perms.set_override(db, "root", "ann", "sales", "deny")  # replaces the allow
    db.commit()
    assert perms.effective_folders(db, who()) == {"legal"}
    perms.set_override(db, "root", "ann", "delivery", None)
    assert perms.effective_folders(db, who()) == {"legal", "delivery"}
    assert perms.effective_folders(db, who("bob")) == {"sales", "delivery"}


def test_everyone_key_and_admin_grants_nothing(db):
    assert perms.effective_folders(db, who(admin=True)) == frozenset()
    perms.set_group_folders(db, "root", "*", ["public"])
    assert perms.effective_folders(db, who(groups=(), admin=True)) == {"public"}


def test_root_folder_needs_explicit_mapping(db):
    assert "" not in perms.effective_folders(db, who())
    perms.set_group_folders(db, "root", GROUP, [""])
    assert perms.effective_folders(db, who()) == {""}


def test_group_replace_semantics_and_audit(db):
    perms.set_group_folders(db, "root", GROUP, ["a", "b"])
    change = perms.set_group_folders(db, "root", GROUP, ["b", "c"], ip="10.0.0.9")
    assert change == {"before": ["a", "b"], "after": ["b", "c"]}
    assert perms.group_folder_map(db) == {perms.normalize_dn(GROUP): ["b", "c"]}
    entry = audit.recent(db, action="admin.group_folders")[0]
    assert entry.actor == "root" and entry.ip == "10.0.0.9" and entry.detail == change


@pytest.mark.parametrize(
    "bad", ["../legal", "a/b", "a\\b", ".hidden", "..", " x", "x\x00", "a" * 300]
)
def test_bad_folder_names_rejected(db, bad):
    with pytest.raises(ValueError):
        perms.set_group_folders(db, "root", GROUP, [bad])
    with pytest.raises(ValueError):
        perms.set_override(db, "root", "ann", bad, "allow")
    assert perms.group_folder_map(db) == {}


@pytest.mark.parametrize("bad", ["", "a b", "a/b", "x" * 65, "ann;drop"])
def test_bad_usernames_rejected(bad):
    with pytest.raises(ValueError):
        perms.normalize_username(bad)


def test_bad_dn_and_effect_rejected(db):
    with pytest.raises(ValueError):
        perms.normalize_dn("not a dn")
    with pytest.raises(ValueError):
        perms.set_override(db, "root", "ann", "sales", "maybe")


def test_disable_ends_sessions(db):
    from datetime import UTC, datetime, timedelta

    from rug.db.models import SessionRow

    now = datetime.now(UTC)
    db.add(
        SessionRow(
            id="h",
            username="ann",
            display_name="Ann",
            groups=[],
            is_admin=False,
            csrf_token="c",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            last_seen=now,
        )
    )
    db.flush()
    perms.set_user_disabled(db, "root", "Ann", True)
    assert db.get(SessionRow, "h") is None
    assert audit.recent(db)[0].action == "admin.user_disabled"

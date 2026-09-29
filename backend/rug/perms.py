"""Who may see which top-level NAS folder.

    effective = (folders of the user's AD groups  ∪  per-user allows)  −  per-user denies

Rules that matter:
  * Computed on every request from the session's group snapshot plus the live tables, so an
    administrator's change applies immediately, while an AD group change applies at the next
    login (the snapshot is taken then).
  * A deny always wins. No groups and no overrides means the empty set, never "everything".
  * Being an administrator (member of the configured admin group) lets someone manage this
    configuration; it grants no documents. Give an admin folders like anyone else.
  * Files directly in the NAS root have the folder "" and are visible to nobody until an
    administrator maps "" to a group (shown as "(root)").
  * The group key "*" means every signed-in user. It also covers Active Directory's primary
    group (Domain Users), which `memberOf` never lists.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ldap3.utils.dn import parse_dn
from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session

from rug import audit
from rug.db.models import Document, GroupFolder, SessionRow, User, UserOverride

ANY_USER = "*"
_FOLDER_BAD = re.compile(r"[\x00-\x1f/\\]")
_USERNAME = re.compile(r"^[a-z0-9._-]{1,64}$")


@dataclass(frozen=True)
class Principal:
    """An authenticated caller. `groups` are normalised DNs as of the last login."""

    username: str
    display_name: str
    groups: frozenset[str]
    is_admin: bool


# -- normalisation ------------------------------------------------------------------------


def normalize_username(raw: str) -> str:
    name = raw.strip().lower()
    if not _USERNAME.match(name):
        raise ValueError("invalid username")
    return name


def normalize_dn(dn: str) -> str:
    """Canonical form for comparing group DNs: lower-case attributes and values, no spaces
    around separators. Active Directory's casing and spacing vary between tools."""
    dn = dn.strip()
    if dn == ANY_USER:
        return dn
    try:
        parts = parse_dn(dn, strip=True)
    except Exception as e:
        raise ValueError(f"not a valid distinguished name: {dn!r}") from e
    if not parts:
        raise ValueError("empty distinguished name")
    return ",".join(f"{attr.strip().lower()}={value.strip().lower()}" for attr, value, _ in parts)


def validate_folder(name: str) -> str:
    """A single top-level folder name. "" is the NAS root."""
    if (
        name != name.strip()
        or len(name) > 255
        or _FOLDER_BAD.search(name)
        or name.startswith(".")
        or name in {".", ".."}
    ):
        raise ValueError(f"invalid folder name: {name!r}")
    return name


# -- effective access ---------------------------------------------------------------------


def effective_folders(db: Session, principal: Principal) -> frozenset[str]:
    groups = set(principal.groups) | {ANY_USER}
    granted = set(db.scalars(select(GroupFolder.folder).where(GroupFolder.group_dn.in_(groups))))
    allow: set[str] = set()
    deny: set[str] = set()
    for row in db.execute(
        select(UserOverride.folder, UserOverride.effect).where(
            UserOverride.username == principal.username
        )
    ):
        (allow if row.effect == "allow" else deny).add(row.folder)
    return frozenset((granted | allow) - deny)


def known_folders(db: Session, docs_dir: Path) -> list[str]:
    """Top-level folders that exist on the share or hold indexed documents."""
    names = set(db.scalars(select(Document.folder).distinct()))
    if docs_dir.is_dir():
        names |= {
            p.name
            for p in docs_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".") and not p.is_symlink()
        }
    return sorted(names)


# -- administration (each change is audited in the same transaction) ----------------------


def group_folder_map(db: Session) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for row in db.execute(select(GroupFolder.group_dn, GroupFolder.folder)):
        out.setdefault(row.group_dn, []).append(row.folder)
    return {g: sorted(f) for g, f in sorted(out.items())}


def set_group_folders(
    db: Session, actor: str, group_dn: str, folders: Iterable[str], ip: str | None = None
) -> dict[str, list[str]]:
    """Replace the folders granted to one group. Returns {"before": [...], "after": [...]}."""
    key = normalize_dn(group_dn)
    wanted = sorted({validate_folder(f) for f in folders})
    before = sorted(db.scalars(select(GroupFolder.folder).where(GroupFolder.group_dn == key)))
    db.execute(delete(GroupFolder).where(GroupFolder.group_dn == key))
    db.add_all(GroupFolder(group_dn=key, folder=f, created_by=actor) for f in wanted)
    db.flush()
    change = {"before": before, "after": wanted}
    audit.log(db, actor, "admin.group_folders", key, change, ip)
    return change


def overrides(db: Session, username: str | None = None) -> list[dict[str, Any]]:
    q = select(UserOverride).order_by(UserOverride.username, UserOverride.folder)
    if username is not None:
        q = q.where(UserOverride.username == normalize_username(username))
    return [
        {"username": o.username, "folder": o.folder, "effect": o.effect, "created_by": o.created_by}
        for o in db.scalars(q)
    ]


def set_override(
    db: Session,
    actor: str,
    username: str,
    folder: str,
    effect: str | None,
    ip: str | None = None,
) -> dict[str, str | None]:
    """Allow or deny one folder for one user; `effect=None` removes the exception."""
    user = normalize_username(username)
    folder = validate_folder(folder)
    if effect not in {"allow", "deny", None}:
        raise ValueError("effect must be 'allow', 'deny' or null")
    row = db.get(UserOverride, (user, folder))
    before = row.effect if row else None
    if effect is None:
        if row:
            db.delete(row)
    elif row:
        row.effect, row.created_by = effect, actor
    else:
        db.add(UserOverride(username=user, folder=folder, effect=effect, created_by=actor))
    db.flush()
    change = {"before": before, "after": effect}
    audit.log(db, actor, "admin.override", f"{user}:{folder}", change, ip)
    return change


def revoke_sessions(db: Session, actor: str, username: str, ip: str | None = None) -> int:
    user = normalize_username(username)
    result: CursorResult[Any] = db.execute(  # type: ignore[assignment]
        delete(SessionRow).where(SessionRow.username == user)
    )
    n = result.rowcount or 0
    audit.log(db, actor, "admin.revoke_sessions", user, {"sessions": n}, ip)
    return n


def set_user_disabled(
    db: Session, actor: str, username: str, disabled: bool, ip: str | None = None
) -> None:
    """Block (or unblock) a user in this application regardless of the directory. Blocking
    also ends their sessions."""
    user = normalize_username(username)
    row = db.get(User, user)
    if row is None:
        row = User(username=user, disabled=disabled)
        db.add(row)
    else:
        row.disabled = disabled
    if disabled:
        db.execute(delete(SessionRow).where(SessionRow.username == user))
    db.flush()
    audit.log(db, actor, "admin.user_disabled" if disabled else "admin.user_enabled", user, {}, ip)

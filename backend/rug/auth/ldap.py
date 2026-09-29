"""Sign-in against Active Directory by binding as the user (no service-account secret).

The password is only ever sent to the directory, over TLS. After a successful bind we read
the user's own entry (which every user may read) for `displayName` and `memberOf`.

Known limits (unverified against a real directory): `memberOf` lists direct memberships only,
so nested groups are not followed and the primary group (usually Domain Users) is absent;
map the key "*" (every signed-in user) for the latter.
"""

import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ldap3 import NONE, SIMPLE, Connection, Server, Tls
from ldap3.core.exceptions import (
    LDAPBindError,
    LDAPException,
    LDAPInvalidCredentialsResult,
    LDAPPasswordIsMandatoryError,
)
from ldap3.utils.conv import escape_filter_chars

from rug.config import Settings
from rug.perms import normalize_dn, normalize_username

MAX_PASSWORD_CHARS = 512
_RAW_USER = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class AuthError(Exception):
    """Wrong username or password (deliberately one message for both)."""


class DirectoryUnavailable(Exception):
    """The directory could not be reached or is misconfigured."""


@dataclass(frozen=True)
class Identity:
    username: str  # normalised, lower-case sAMAccountName
    display_name: str
    groups: frozenset[str]  # normalised DNs
    is_admin: bool


ConnectionFactory = Callable[[str, str], Connection]


def _one(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "")


class LdapAuthenticator:
    def __init__(self, settings: Settings, connect: ConnectionFactory | None = None):
        self.s = settings
        self._connect = connect or self._real_connection
        if not settings.ldap_url:
            raise DirectoryUnavailable("LDAP is not configured (RUG_LDAP_URL)")
        if not (settings.ldap_netbios_domain or settings.ldap_upn_suffix):
            raise DirectoryUnavailable("set RUG_LDAP_NETBIOS_DOMAIN or RUG_LDAP_UPN_SUFFIX")
        if not settings.ldap_base_dn:
            raise DirectoryUnavailable("set RUG_LDAP_BASE_DN")
        encrypted = settings.ldap_url.lower().startswith("ldaps://") or settings.ldap_start_tls
        if not encrypted and not settings.ldap_allow_insecure:
            raise DirectoryUnavailable(
                "refusing to send passwords unencrypted: use ldaps://, RUG_LDAP_START_TLS=true, "
                "or RUG_LDAP_ALLOW_INSECURE=true for development"
            )

    # -- input handling ---------------------------------------------------------------------

    def parse_username(self, raw: str) -> str:
        """Accept `user`, `user@suffix` or `DOMAIN\\user`; return the normalised account name.
        Raises AuthError for anything else (including a foreign domain or suffix)."""
        text = raw.strip()
        user = text
        if "\\" in text:
            domain, _, user = text.partition("\\")
            if (
                not self.s.ldap_netbios_domain
                or domain.lower() != self.s.ldap_netbios_domain.lower()
            ):
                raise AuthError
        elif "@" in text:
            user, _, suffix = text.partition("@")
            if not self.s.ldap_upn_suffix or suffix.lower() != self.s.ldap_upn_suffix.lower():
                raise AuthError
        if not _RAW_USER.match(user):
            raise AuthError
        return normalize_username(user)

    def bind_identity(self, username: str) -> str:
        if self.s.ldap_netbios_domain:
            return f"{self.s.ldap_netbios_domain}\\{username}"
        return f"{username}@{self.s.ldap_upn_suffix}"

    # -- the real connection ----------------------------------------------------------------

    def _real_connection(self, identity: str, password: str) -> Connection:
        tls = None
        if self.s.ldap_url.lower().startswith("ldaps://") or self.s.ldap_start_tls:
            tls = Tls(
                validate=ssl.CERT_REQUIRED,
                ca_certs_file=self.s.ldap_ca_certs_file or None,
                version=ssl.PROTOCOL_TLS_CLIENT,
            )
        server = Server(
            self.s.ldap_url, get_info=NONE, tls=tls, connect_timeout=self.s.ldap_timeout_s
        )
        conn = Connection(
            server,
            user=identity,
            password=password,
            authentication=SIMPLE,
            receive_timeout=self.s.ldap_timeout_s,
            raise_exceptions=True,
            auto_referrals=False,
        )
        conn.open()
        if self.s.ldap_start_tls and not self.s.ldap_url.lower().startswith("ldaps://"):
            conn.start_tls()
        conn.bind()
        return conn

    # -- sign-in ----------------------------------------------------------------------------

    def authenticate(self, raw_username: str, password: str) -> Identity:
        # Checked before anything else: some directories treat a name with an empty password
        # as an anonymous ("unauthenticated") bind that succeeds.
        if not password or not password.strip() or len(password) > MAX_PASSWORD_CHARS:
            raise AuthError
        username = self.parse_username(raw_username)
        try:
            conn = self._connect(self.bind_identity(username), password)
        except (LDAPBindError, LDAPInvalidCredentialsResult, LDAPPasswordIsMandatoryError) as e:
            raise AuthError from e
        except (LDAPException, OSError) as e:
            raise DirectoryUnavailable("the directory is unreachable") from e
        try:
            if not conn.bound:
                raise AuthError
            return self._identity(conn, username)
        except LDAPException as e:
            raise DirectoryUnavailable("the directory returned an error") from e
        finally:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001 - closing must never mask the result
                pass

    def _identity(self, conn: Connection, username: str) -> Identity:
        conn.search(
            self.s.ldap_base_dn,
            f"(&(objectClass=user)(sAMAccountName={escape_filter_chars(username)}))",
            attributes=["displayName", "memberOf", "sAMAccountName"],
            size_limit=2,
        )
        entries = [
            e
            for e in conn.response or []
            if e.get("type", "searchResEntry") == "searchResEntry" and e.get("attributes")
        ]
        if len(entries) != 1:
            # Bound fine but we cannot find (or uniquely find) the account: do not guess.
            raise AuthError
        attrs = entries[0]["attributes"]
        if _one(attrs.get("sAMAccountName")).lower() != username:
            raise AuthError
        raw_groups = attrs.get("memberOf") or []
        if isinstance(raw_groups, str):
            raw_groups = [raw_groups]
        groups: set[str] = set()
        for g in raw_groups:
            try:
                groups.add(normalize_dn(str(g)))
            except ValueError:
                continue
        admin_group = normalize_dn(self.s.ldap_admin_group_dn) if self.s.ldap_admin_group_dn else ""
        return Identity(
            username=username,
            display_name=_one(attrs.get("displayName")) or username,
            groups=frozenset(groups),
            is_admin=bool(admin_group) and admin_group in groups,
        )

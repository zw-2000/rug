import pytest
from ldap3.core.exceptions import LDAPSocketOpenError

from eval.ldapmock import MockDirectory
from rug.auth.ldap import AuthError, DirectoryUnavailable, LdapAuthenticator
from rug.config import Settings

BASE = "dc=corp,dc=local"
SALES = "CN=Sales,OU=Groups,DC=corp,DC=local"
ADMINS = "CN=RugAdmins,OU=Groups,DC=corp,DC=local"


def settings(**kw):
    base = dict(
        ldap_url="ldaps://dc.corp.local",
        ldap_base_dn=BASE,
        ldap_netbios_domain="CORP",
        ldap_admin_group_dn=ADMINS,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture
def directory():
    d = MockDirectory(
        {
            "ann": ("s3cret!", "Ann A", [SALES]),
            "root": ("adminpw", "Root R", [SALES, ADMINS]),
            "nogroups": ("pw12345", "No Groups", []),
        }
    )
    return d, d.calls


def auth(directory, **kw):
    return LdapAuthenticator(settings(**kw), directory[0])


def test_good_login_reads_name_and_groups(directory):
    ident = auth(directory).authenticate("Ann", "s3cret!")
    assert ident.username == "ann" and ident.display_name == "Ann A"
    assert ident.groups == {"cn=sales,ou=groups,dc=corp,dc=local"}
    assert not ident.is_admin


def test_admin_group_member(directory):
    assert auth(directory).authenticate("root", "adminpw").is_admin


def test_no_groups_is_fine(directory):
    assert auth(directory).authenticate("nogroups", "pw12345").groups == frozenset()


@pytest.mark.parametrize("pw", ["", " ", "   \t", "x" * 600])
def test_bad_passwords_never_contact_the_directory(directory, pw):
    with pytest.raises(AuthError):
        auth(directory).authenticate("ann", pw)
    assert directory[1] == []


def test_wrong_password_and_unknown_user_look_the_same(directory):
    a = auth(directory)
    with pytest.raises(AuthError) as e1:
        a.authenticate("ann", "nope")
    with pytest.raises(AuthError) as e2:
        a.authenticate("ghost", "whatever")
    assert str(e1.value) == str(e2.value)


@pytest.mark.parametrize(
    "raw", ["", "a b", "ann)(cn=*", "*", "ann*", "OTHER\\ann", "ann@evil.com", "..\\ann", "a" * 65]
)
def test_hostile_usernames_rejected_before_binding(directory, raw):
    with pytest.raises(AuthError):
        auth(directory).authenticate(raw, "s3cret!")
    assert directory[1] == []


def test_name_forms(directory):
    a = auth(directory, ldap_upn_suffix="corp.local")
    assert a.parse_username("CORP\\Ann") == "ann"
    assert a.parse_username("ann@CORP.local") == "ann"
    assert a.parse_username(" ANN ") == "ann"
    assert a.bind_identity("ann") == "CORP\\ann"


def test_upn_bind_identity_when_no_netbios(directory):
    a = auth(directory, ldap_netbios_domain="", ldap_upn_suffix="corp.local")
    assert a.bind_identity("ann") == "ann@corp.local"
    with pytest.raises(AuthError):
        a.parse_username("CORP\\ann")


def test_unreachable_directory():
    def down(identity, password):
        raise LDAPSocketOpenError("no route")

    with pytest.raises(DirectoryUnavailable):
        LdapAuthenticator(settings(), down).authenticate("ann", "pw")

    def refused(identity, password):
        raise ConnectionRefusedError()

    with pytest.raises(DirectoryUnavailable):
        LdapAuthenticator(settings(), refused).authenticate("ann", "pw")


@pytest.mark.parametrize(
    "kw",
    [
        dict(ldap_url=""),
        dict(ldap_base_dn=""),
        dict(ldap_netbios_domain="", ldap_upn_suffix=""),
        dict(ldap_url="ldap://dc.corp.local"),  # plaintext without StartTLS
    ],
)
def test_misconfiguration_fails_at_startup(kw):
    with pytest.raises(DirectoryUnavailable):
        LdapAuthenticator(settings(**kw))


def test_plaintext_allowed_only_explicitly_or_with_starttls():
    LdapAuthenticator(settings(ldap_url="ldap://dc", ldap_start_tls=True), lambda *_: None)  # type: ignore[arg-type,return-value]
    LdapAuthenticator(settings(ldap_url="ldap://dc", ldap_allow_insecure=True), lambda *_: None)  # type: ignore[arg-type,return-value]


def test_real_connection_pins_tls_validation(monkeypatch):
    import ssl

    from ldap3 import Connection

    events: list[str] = []
    seen: dict[str, object] = {}
    monkeypatch.setattr(Connection, "start_tls", lambda self, *a, **k: events.append("starttls"))
    monkeypatch.setattr(Connection, "bind", lambda self, *a, **k: events.append("bind"))

    real_init = Connection.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        seen["server"], seen["user"] = self.server, self.user
        self.open = lambda *a, **k: events.append("open")  # `open` is set per instance

    monkeypatch.setattr(Connection, "__init__", init)

    LdapAuthenticator(settings(ldap_url="ldaps://dc.corp.local"))._real_connection(
        "CORP\\ann", "pw"
    )
    server = seen["server"]
    assert server.ssl and server.tls.validate == ssl.CERT_REQUIRED  # type: ignore[attr-defined]
    assert events == ["open", "bind"] and seen["user"] == "CORP\\ann"

    events.clear()
    LdapAuthenticator(
        settings(ldap_url="ldap://dc.corp.local", ldap_start_tls=True)
    )._real_connection("CORP\\ann", "pw")
    server = seen["server"]
    assert not server.ssl and server.tls.validate == ssl.CERT_REQUIRED  # type: ignore[attr-defined]
    assert events == ["open", "starttls", "bind"]  # encrypted before the password is sent


def test_malformed_admin_group_fails_at_startup():
    with pytest.raises(DirectoryUnavailable):
        LdapAuthenticator(settings(ldap_admin_group_dn="not a dn"))

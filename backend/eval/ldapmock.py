"""A mock Active Directory shared by the LDAP and API tests.

ldap3's mock matches a bind against the exact entry DN and rejects non-DN identities, so the
factory translates the identity the application asks for (CORP\\name) into the entry's DN.
`calls` records what the application actually asked for."""

from ldap3 import MOCK_SYNC, Connection, Server

BASE = "dc=corp,dc=local"


class MockDirectory:
    def __init__(self, users: dict[str, tuple[str, str, list[str]]]):
        self.server = Server("mock")
        self.seed = Connection(self.server, user="cn=seed", password="x", client_strategy=MOCK_SYNC)
        self.seed.strategy.add_entry("cn=seed", {"userPassword": "x", "objectClass": "top"})
        self.seed.bind()
        self.dns: dict[str, str] = {}
        self.calls: list[str] = []
        for name, (password, display, groups) in users.items():
            dn = f"CN={display},OU=Users,{BASE}"
            self.seed.strategy.add_entry(
                dn,
                {
                    "objectClass": "user",
                    "sAMAccountName": name,
                    "displayName": display,
                    "memberOf": groups,
                    "userPassword": password,
                },
            )
            self.dns[f"CORP\\{name}"] = dn

    def set_groups(self, name: str, groups: list[str]) -> None:
        entry = self.server.dit[self.dns[f"CORP\\{name}"]]
        entry["memberOf"] = [g.encode() for g in groups]

    def __call__(self, identity: str, password: str) -> Connection:
        self.calls.append(identity)
        conn = Connection(
            self.server,
            user=self.dns.get(identity, identity),
            password=password,
            client_strategy=MOCK_SYNC,
            raise_exceptions=True,
        )
        conn.bind()
        return conn

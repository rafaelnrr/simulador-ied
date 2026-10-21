"""Autenticação (RN-UI-06, RN-SEG-03): obrigatória quando a administração não escuta só em 127.0.0.1.

Usuários locais (banco) têm prioridade; se o nome não existe localmente e há LDAP configurado,
a senha é validada por bind no diretório e o papel vem dos grupos (posixGroup/groupOfNames por cn).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass, field

PAPEIS = ("leitura", "operacao", "administrador")
log = logging.getLogger(__name__)


LOGIN_VALIDO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass
class LdapConfig:
    url: str                                   # ldaps://srv:636 ou ldap://srv:389 com starttls
    base: str                                  # dc=redetocot,dc=local
    grupos_operacao: list[str] = field(default_factory=list)  # cn dos grupos com papel operacao
    grupos_leitura: list[str] = field(default_factory=list)   # cn dos grupos com papel leitura
    grupos_admin: list[str] = field(default_factory=list)     # cn dos grupos com papel administrador
    usuarios_dn: str | None = None             # padrão ou=Usuarios,<base>
    grupos_dn: str | None = None               # padrão ou=Grupos,<base>
    starttls: bool = False
    ca_file: str | None = None                 # CA do servidor; o certificado é sempre validado
    tls_nome: str | None = None                # nome no certificado, quando difere do host da URL
    timeout: int = 5  # ldap3 exige inteiro (struct.pack)

    @property
    def tls(self) -> bool:
        return self.url.startswith("ldaps://") or self.starttls


class Auth:
    def __init__(self, storage, required: bool, ldap: LdapConfig | None = None):
        self.storage = storage
        self.required = required
        self.ldap = ldap
        self.tokens: dict[str, tuple[str, str, float]] = {}

    @staticmethod
    def _hash(pw: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()

    def add_user(self, name: str, pw: str, papel: str):
        if papel not in PAPEIS:
            raise ValueError(f"papel deve ser {PAPEIS}")
        if len(pw) < 8:
            raise ValueError("senha deve ter pelo menos 8 caracteres")
        salt = secrets.token_hex(16)
        self.storage.set_user(name, self._hash(pw, salt), salt, papel)

    def _ldap_conn(self, user=None, pw=None):
        import ssl

        import ldap3
        c = self.ldap
        tls = None
        if c.tls:
            tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=c.ca_file, version=ssl.PROTOCOL_TLS_CLIENT,
                            valid_names=[c.tls_nome] if c.tls_nome else None)
        srv = ldap3.Server(c.url, connect_timeout=c.timeout, get_info=ldap3.NONE, tls=tls,
                           use_ssl=c.url.startswith("ldaps://"))
        conn = ldap3.Connection(srv, user=user, password=pw, receive_timeout=c.timeout,
                                raise_exceptions=False, read_only=True)
        conn.open()
        if c.starttls and not conn.start_tls():
            raise ConnectionError(f"StartTLS falhou: {conn.result}")
        if not conn.bind():
            conn.unbind()
            return None
        return conn

    def _ldap_login(self, name: str, pw: str) -> str | None:
        """Localiza o uid (leitura anônima), faz bind com a senha do usuário e devolve o papel pelos grupos."""
        import ldap3
        from ldap3.utils.conv import escape_filter_chars

        c = self.ldap
        # senha vazia faria bind anônimo e "autenticaria" qualquer um
        if not LOGIN_VALIDO.match(name or "") or not pw:
            return None
        anon = user = None
        try:
            anon = self._ldap_conn()
            if anon is None:
                raise ConnectionError("bind anônimo recusado")
            anon.search(c.usuarios_dn or f"ou=Usuarios,{c.base}",
                        f"(&(|(objectClass=posixAccount)(objectClass=inetOrgPerson))(uid={escape_filter_chars(name)}))",
                        attributes=["uid"], size_limit=2)
            if len(anon.entries) != 1:  # 0 = não existe; 2 = ambíguo
                return None
            dn = anon.entries[0].entry_dn
            user = self._ldap_conn(dn, pw)
            if user is None:
                log.info("LDAP: senha recusada para %s", name)
                return None
            user.search(c.grupos_dn or f"ou=Grupos,{c.base}",
                        f"(|(&(objectClass=posixGroup)(memberUid={escape_filter_chars(name)}))"
                        f"(&(objectClass=groupOfNames)(member={escape_filter_chars(dn)})))", attributes=["cn"])
            grupos = {str(e.cn.value).lower() for e in user.entries}
            if grupos & {g.lower() for g in c.grupos_admin}:
                return "administrador"
            if grupos & {g.lower() for g in c.grupos_operacao}:
                return "operacao"
            if grupos & {g.lower() for g in c.grupos_leitura}:
                return "leitura"
            log.info("LDAP: %s sem grupo do simulador (tem %s)", name, sorted(grupos))
            return None
        except (ldap3.core.exceptions.LDAPException, OSError, ConnectionError) as e:
            log.warning("LDAP indisponível: %s", e)
            return None
        finally:
            for conn in (anon, user):
                if conn is not None:
                    conn.unbind()

    def login(self, name: str, pw: str) -> tuple[str, str] | None:
        u = self.storage.user(name)
        if u:
            if not hmac.compare_digest(u[1], self._hash(pw, u[2])):
                return None
            papel = u[3]
        elif self.ldap:
            papel = self._ldap_login(name, pw)
            if papel is None:
                return None
        else:
            return None
        tok = secrets.token_urlsafe(32)
        self.tokens[tok] = (name, papel, time.time() + 12 * 3600)
        return tok, papel

    def check(self, tok: str | None) -> tuple[str, str] | None:
        if not self.required:
            return ("local", "administrador")
        if not tok:
            return None
        t = self.tokens.get(tok)
        if not t or t[2] < time.time():
            self.tokens.pop(tok, None)
            return None
        return t[0], t[1]

    def logout(self, tok: str):
        self.tokens.pop(tok, None)

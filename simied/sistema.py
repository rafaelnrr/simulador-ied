"""Painel de configuração: preferências do servidor, usuários locais, informações do sistema e backup."""
from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import time
import zipfile
from pathlib import Path

from . import __version__
from .auth import LOGIN_VALIDO, PAPEIS
from .ied import PointError

# Preferências guardadas em data/config.json. Cada chave: (padrão, validador que devolve o valor normalizado).
PADRAO = {
    "novo_ied": {"perfil": "auto", "porta": 102, "max_associacoes": 3},
    "sage": {"opmsk_padrao": "10C830"},
    "editor": {"vista_inicial": "modelo"},
}


def _int(lo, hi):
    def f(v, nome):
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise PointError(f"{nome} deve ser inteiro", status=422) from None
        if not lo <= n <= hi:
            raise PointError(f"{nome} deve ficar entre {lo} e {hi}", status=422)
        return n
    return f


def _hex24(v, nome):
    s = str(v).strip().upper()
    try:
        n = int(s, 16)
    except ValueError:
        raise PointError(f"{nome} deve ser hexadecimal (ex.: 10C830)", status=422) from None
    if not 0 <= n < 1 << 24:
        raise PointError(f"{nome} tem só 24 bits (até FFFFFF)", status=422)
    return f"{n:X}"


def _choice(*opts):
    def f(v, nome):
        if v not in opts:
            raise PointError(f"{nome} deve ser um de {list(opts)}", status=422)
        return v
    return f


VALIDA = {
    ("novo_ied", "porta"): _int(1, 65535),
    ("novo_ied", "max_associacoes"): _int(1, 64),
    ("sage", "opmsk_padrao"): _hex24,
    ("editor", "vista_inicial"): _choice("modelo", "tipos", "xml"),
}


class Sistema:
    def __init__(self, mgr):
        self.mgr = mgr
        self.path = mgr.data_dir / "config.json"

    # ------------------------------------------------------------------ preferências
    def settings(self) -> dict:
        cfg = copy.deepcopy(PADRAO)
        try:
            saved = json.loads(self.path.read_text())
        except (OSError, ValueError):
            saved = {}
        for sec, vals in saved.items():
            if sec in cfg and isinstance(vals, dict):
                cfg[sec].update({k: v for k, v in vals.items() if k in cfg[sec]})
        return cfg

    def save_settings(self, body: dict) -> dict:
        cfg = self.settings()
        perfis = set(self.mgr.profiles.all())
        for sec, vals in body.items():
            if sec not in cfg or not isinstance(vals, dict):
                raise PointError(f"seção desconhecida '{sec}'; use {list(cfg)}", status=422)
            for k, v in vals.items():
                if k not in cfg[sec]:
                    raise PointError(f"campo desconhecido '{sec}.{k}'", status=422)
                if (sec, k) == ("novo_ied", "perfil"):
                    if v != "auto" and v not in perfis:
                        raise PointError(f"perfil '{v}' não existe; use 'auto' ou um de {sorted(perfis)}", status=422)
                else:
                    v = VALIDA[(sec, k)](v, f"{sec}.{k}")
                cfg[sec][k] = v
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        tmp.replace(self.path)
        return cfg

    # ------------------------------------------------------------------ usuários locais
    def users(self) -> list:
        return self.mgr.storage.users()

    def _admins(self) -> int:
        return sum(1 for u in self.users() if u["papel"] == "administrador")

    def add_user(self, body: dict) -> dict:
        nome = str(body.get("usuario", "")).strip()
        if not LOGIN_VALIDO.match(nome):
            raise PointError("usuário inválido: letras, números, ponto, hífen e sublinhado (até 64)", status=422)
        if self.mgr.storage.user(nome):
            raise PointError(f"usuário '{nome}' já existe", status=409)
        papel = body.get("papel", "operacao")
        try:
            self.mgr.auth.add_user(nome, str(body.get("senha", "")), papel)
        except ValueError as e:
            raise PointError(str(e), status=422) from e
        return {"usuario": nome, "papel": papel}

    def update_user(self, nome: str, body: dict, quem: str) -> dict:
        u = self.mgr.storage.user(nome)
        if not u:
            raise PointError(f"usuário '{nome}' não existe", status=404)
        papel = body.get("papel", u[3])
        if papel not in PAPEIS:
            raise PointError(f"papel deve ser {list(PAPEIS)}", status=422)
        if u[3] == "administrador" and papel != "administrador" and self._admins() <= 1:
            raise PointError("é o único administrador local; crie outro antes de rebaixar este", status=409)
        if body.get("senha"):
            try:
                self.mgr.auth.add_user(nome, str(body["senha"]), papel)
            except ValueError as e:
                raise PointError(str(e), status=422) from e
        else:
            self.mgr.storage.set_role(nome, papel)
        if (papel != u[3] or body.get("senha")) and nome != quem:
            self._drop_sessions(nome)  # quem mudou o próprio usuário continua logado
        return {"usuario": nome, "papel": papel}

    def delete_user(self, nome: str, quem: str):
        u = self.mgr.storage.user(nome)
        if not u:
            raise PointError(f"usuário '{nome}' não existe", status=404)
        if nome == quem:
            raise PointError("não dá para remover o próprio usuário", status=409)
        if u[3] == "administrador" and self._admins() <= 1:
            raise PointError("é o único administrador local", status=409)
        self.mgr.storage.del_user(nome)
        self._drop_sessions(nome)

    def _drop_sessions(self, nome: str):
        """Encerra os logins abertos do usuário (papel ou senha mudaram, ou foi removido)."""
        for tok, (n, _, _) in list(self.mgr.auth.tokens.items()):
            if n == nome:
                self.mgr.auth.tokens.pop(tok, None)

    def change_own_password(self, quem: str, atual: str, nova: str):
        u = self.mgr.storage.user(quem)
        if not u:
            raise PointError("só usuários locais trocam a senha aqui (usuários LDAP trocam no diretório)", status=422)
        if self.mgr.auth.login(quem, atual) is None:
            raise PointError("senha atual incorreta", status=403)
        try:
            self.mgr.auth.add_user(quem, nova, u[3])
        except ValueError as e:
            raise PointError(str(e), status=422) from e

    # ------------------------------------------------------------------ informações e backup
    def info(self) -> dict:
        m = self.mgr
        d = m.data_dir

        def size(p: Path) -> int:
            if p.is_file():
                return p.stat().st_size
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0

        ld = m.auth.ldap
        return {
            "versao": __version__, "admin": f"{m.admin_host}:{m.admin_port}", "dados": str(d),
            "teste_local": m.local_test, "autenticacao": m.auth.required,
            "ldap": {"url": ld.url, "base": ld.base, "tls": ld.tls, "grupos_admin": ld.grupos_admin,
                     "grupos_operacao": ld.grupos_operacao, "grupos_leitura": ld.grupos_leitura} if ld else None,
            "ieds": len(m.ieds), "rodando": sum(1 for i in m.ieds.values() if i.state == "rodando"),
            "sessoes_abertas": len(m.auth.tokens),
            "disco": {"banco": size(d / "simied.db") + size(d / "simied.db-wal"), "cids": size(d / "cids"),
                      "editor": size(d / "cid_editor"), "perfis": size(d / "profiles"), "total": size(d)},
            "pid": os.getpid(), "inicio": getattr(m, "started_at", None),
        }

    def backup(self) -> tuple[bytes, str]:
        """ZIP com banco (cópia consistente), CIDs, perfis, rascunhos do editor e config.json."""
        d = self.mgr.data_dir
        buf = io.BytesIO()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "simied.db"
            self.mgr.storage.backup_to(db)
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(db, "simied.db")
                for sub in ("cids", "profiles", "cid_editor"):
                    for f in sorted((d / sub).rglob("*")) if (d / sub).exists() else []:
                        if f.is_file():
                            z.write(f, str(f.relative_to(d)))
                if self.path.exists():
                    z.write(self.path, "config.json")
                z.writestr("LEIAME.txt", "Backup do SimIED. Para restaurar: pare o serviço, extraia este arquivo na "
                                         "pasta de dados (ex.: data/) e inicie o serviço de novo.\n")
        return buf.getvalue(), time.strftime("simied_backup_%Y%m%d_%H%M%S.zip")

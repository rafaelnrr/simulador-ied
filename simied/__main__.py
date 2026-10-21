"""Linha de comando: python -m simied [serve|usuario] ..."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from pathlib import Path

from . import __version__, netif


def main(argv=None):
    ap = argparse.ArgumentParser(prog="simied", description="Simulador de IEDs IEC 61850")
    ap.add_argument("--dados", default=str(Path.cwd() / "data"), help="diretório de dados (banco, CIDs, perfis)")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="inicia a aplicação (padrão)")
    s.add_argument("--admin-host", default="127.0.0.1",
                   help="IP da interface web/API: 127.0.0.1 ou IP de gerenciamento (RN-NET-08)")
    s.add_argument("--admin-port", type=int, default=8080)
    s.add_argument("--teste-local", action="store_true",
                   help="permite IEDs em 127.0.0.1 e mostra o loopback na lista de interfaces (RN-NET-14)")
    s.add_argument("--log", default="info")
    g = s.add_argument_group("LDAP", "login pelo diretório (usuários locais continuam valendo)")
    g.add_argument("--ldap-url", help="ldaps://servidor:636 ou ldap://servidor:389 com --ldap-starttls")
    g.add_argument("--ldap-base", help="Base DN, ex.: dc=redetocot,dc=local")
    g.add_argument("--ldap-usuarios-dn", help="padrão ou=Usuarios,<base>")
    g.add_argument("--ldap-grupos-dn", help="padrão ou=Grupos,<base>")
    g.add_argument("--ldap-grupo-admin", action="append", default=[], metavar="CN",
                   help="cn de grupo com papel administrador: altera toda a configuração (pode repetir)")
    g.add_argument("--ldap-grupo-operacao", action="append", default=[], metavar="CN",
                   help="cn de grupo com papel operacao (pode repetir)")
    g.add_argument("--ldap-grupo-leitura", action="append", default=[], metavar="CN",
                   help="cn de grupo com papel leitura (pode repetir)")
    g.add_argument("--ldap-starttls", action="store_true")
    g.add_argument("--ldap-ca", help="arquivo PEM da CA do servidor LDAP")
    g.add_argument("--ldap-tls-nome", help="nome esperado no certificado, quando difere do host da URL")
    u = sub.add_parser("usuario", help="cria/altera usuário da interface (RN-SEG-03)")
    u.add_argument("nome")
    u.add_argument("--papel", choices=["leitura", "operacao", "administrador"], default="operacao")
    args = ap.parse_args(argv)
    data_dir = Path(args.dados).resolve()

    if args.cmd == "usuario":
        from .auth import Auth
        from .storage import Storage
        st = Storage(data_dir / "simied.db")
        pw = getpass.getpass("senha: ")
        if pw != getpass.getpass("repita: "):
            sys.exit("senhas diferentes")
        Auth(st, True).add_user(args.nome, pw, args.papel)
        st.close()
        print(f"usuário {args.nome} ({args.papel}) gravado")
        return

    if args.cmd is None:
        args = ap.parse_args(["--dados", str(data_dir), "serve"] + (argv or sys.argv[1:]))
    logging.basicConfig(level=getattr(logging, args.log.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = args.admin_host
    if host in ("0.0.0.0", "::", ""):
        sys.exit("RN-NET-08: a administração não pode escutar em todas as interfaces; use 127.0.0.1 ou o IP de gerenciamento")
    if host not in ("127.0.0.1", "localhost", "::1") and host not in netif.local_ips():
        sys.exit(f"IP de administração {host} não existe nesta máquina")
    import uvicorn

    from .api import create_app
    from .manager import Manager
    ldap = None
    if args.ldap_url:
        from .auth import LdapConfig
        if not args.ldap_base:
            sys.exit("--ldap-base é obrigatório com --ldap-url")
        if not (args.ldap_grupo_admin or args.ldap_grupo_operacao or args.ldap_grupo_leitura):
            sys.exit("informe ao menos um --ldap-grupo-admin, --ldap-grupo-operacao ou --ldap-grupo-leitura")
        ldap = LdapConfig(args.ldap_url, args.ldap_base, grupos_operacao=args.ldap_grupo_operacao,
                          grupos_leitura=args.ldap_grupo_leitura, grupos_admin=args.ldap_grupo_admin,
                          usuarios_dn=args.ldap_usuarios_dn, grupos_dn=args.ldap_grupos_dn,
                          starttls=args.ldap_starttls, ca_file=args.ldap_ca, tls_nome=args.ldap_tls_nome)
        if not ldap.tls:
            logging.warning("LDAP sem TLS: senhas trafegam em claro até o servidor (use ldaps:// ou --ldap-starttls)")
    mgr = Manager(data_dir, host, args.admin_port, args.teste_local, ldap)
    if mgr.auth.required and not mgr.storage.users() and not ldap:
        sys.exit("RN-UI-06: fora de 127.0.0.1 a autenticação é obrigatória. Crie um usuário antes: "
                 "python -m simied usuario <nome> --papel operacao (ou configure --ldap-url)")
    app = create_app(mgr)
    print(f"Simulador de IEDs IEC 61850 {__version__} — interface em http://{host}:{args.admin_port}/  (dados: {data_dir})")
    config = uvicorn.Config(app, host=host, port=args.admin_port, log_level="warning", ws_ping_interval=20)
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()

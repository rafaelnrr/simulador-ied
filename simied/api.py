"""API REST (/api/v1) e WebSocket de eventos (seção 11)."""
from __future__ import annotations

import asyncio
import csv
import io
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, netif
from .cidedit import CidEditor
from .ied import PointError
from .manager import Manager
from .mms.types import ERR_ACCESS_DENIED  # noqa: F401
from .pcap import build_pcap
from .scl import SclError, parse_scl
from .sistema import Sistema

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Bits do token OPMSK do atributo CONFIG da entidade CNF (SAGE_ManCfg_Anx17_61850, seção 17, OPT0 a OPT23):
# bit -> (resumo, efeito quando ligado comparado ao comportamento padrão do SAGE)
OPMSK_BITS = {
    0: ("Não verifica os Services Supported do MMS",
        "O SAGE não confere os serviços MMS que o IED declara suportar na troca de PDUs Initiate. "
        "Padrão: confere e pode recusar o IED que não declara um serviço necessário."),
    1: ("Não envia o calling-AR no pedido de associação",
        "Omite do pedido de associação ACSE o endereçamento do SAGE local (AP-Title, AE-Qualifier, P/S/T-Sel). "
        "Padrão: o calling-AR é incluído."),
    2: ("Envia o called-AR no pedido de associação",
        "Inclui no pedido de associação ACSE o endereçamento do IED remoto. Padrão: o called-AR não é incluído. "
        "Use quando o IED rejeita associações sem o próprio AP-Title."),
    3: ("Sem uso", "OPT3 não é usada nesta versão do SAGE."),
    4: ("Usa os DataSets pré-configurados do IED",
        "O SAGE usa os DataSets já existentes no IED, e os RCBs ligados a eles, sem tentar criar DataSets dinâmicos. "
        "Padrão: cria DataSets dinâmicos. A ferramenta xml61850 controla este bit conforme a capacidade declarada no ICD."),
    5: ("Procura os RCBs no LLN0",
        "Procura e usa os Report Control Blocks do LN LLN0, em vez de um RCB em cada Logical Node como sugere a norma. "
        "É o caso da maioria dos IEDs, que concentram os RCBs no LLN0."),
    6: ("Ativa o GOOSE Control Block do LLN0",
        "Ativa o GoCB achado no LLN0. Padrão: não ativa, porque o SAGE prefere InformationReports controlados pelos RCBs. "
        "GOOSE com o APPID configurado em CNF.CONFIG (token GOOSE) é processado mesmo sem este bit."),
    7: ("Ativa os RCBs com BufTm=0",
        "Ativa os RCBs com o tempo de bufferização zerado: cada mudança gera um report imediato. "
        "Padrão: BufTm de 100 ms, que agrupa mudanças próximas num mesmo report."),
    8: ("Força InterLockCheck nos controles",
        "Envia todos os comandos com InterlockCheck, independentemente do token \"I\" no atributo KCONV da entidade CGF."),
    9: ("TrgOps só com data-change (dchg)",
        "Ativa os RCBs pedindo report só por mudança de valor. Padrão: data-change e quality-change. "
        "Com o bit ligado, mudanças só de qualidade (por exemplo, inválido ou em teste) não geram report."),
    10: ("Usa o último RCB do Logical Node",
         "Quando há vários RCBs no mesmo Logical Node, usa o último encontrado em vez do primeiro. "
         "Útil quando outro cliente (por exemplo, a IHM local) ocupa as primeiras instâncias."),
    11: ("Falha ao ativar um RCB não derruba a comunicação",
         "Uma falha na ativação de um RCB deixa de ser erro fatal. Padrão: a associação com o IED é interrompida "
         "quando a ativação de um RCB fracassa. Com o bit ligado, os pontos daquele RCB ficam sem reports, mas o resto segue."),
    12: ("IED redundante (LD com prefixo P/D)",
         "Trata o IED como redundante: alterna a primeira letra do nome dos Logical Devices entre 'P' (principal) e "
         "'D' (dual), conforme o IP conectado entre os 4 possíveis (IED principal/dual × rede principal/reserva)."),
    13: ("Informa no GOOSE do SAGE o IED conectado",
         "Inclui na mensagem GOOSE difundida pelo SAGE o estado de conexão com o IED (principal ou dual), "
         "para que os outros receptores do Station Bus saibam qual está ativo."),
    14: ("Ignora pontos fora dos DataSets pré-configurados",
         "Para IEDs sem DataSets dinâmicos, não inclui na base de dados, nem lê em tempo real, objetos que o IED "
         "não colocou em nenhum DataSet pré-configurado. Padrão: esses objetos entram na base e são lidos por polling."),
    15: ("Ignora a invalidade da estampa de tempo",
         "Registra no SOE (arquivos .sde) os eventos recebidos mesmo com atributos de invalidade no time-tag "
         "(por exemplo, ClockNotSynchronized ou ClockFailure). Padrão: esses eventos não vão para o SOE."),
    16: ("Inverte a polaridade dos pontos duplos",
         "Inverte a polaridade dos pontos duplos recebidos nos CDCs DPC e SPC (aberto passa a ser fechado e vice-versa)."),
    17: ("Usa só URCBs",
         "Ignora os BRCBs (bufferizados) e usa apenas os URCBs (não bufferizados) do IED. "
         "Eventos que ocorrerem com a associação caída se perdem. Tem prioridade sobre o bit 19."),
    18: ("Failover por GOOSE do IED secundário",
         "Faz o failover da comunicação entre IEDs redundantes ao receber GOOSE vindo do MAC address do IED "
         "considerado secundário no momento (MACs configurados na parte opcional de CNF.CONFIG)."),
    19: ("Usa só BRCBs",
         "Ignora os URCBs e usa apenas os BRCBs (bufferizados) do IED, que guardam eventos durante a queda da associação. "
         "Ignorado se o bit 17 estiver ligado."),
    20: ("Pede DataReference no OptFlds dos RCBs",
         "Liga a opção data-reference no OptFlds dos RCBs. Os reports passam a trazer a referência de cada ponto "
         "reportado, entre o inclusion-bitstring e os valores, e não só a posição no DataSet."),
    21: ("DataSets dinâmicos em BRCBs com ResvTms (Ed. 2)",
         "Permite gerenciar DataSets dinâmicos (bit 4 desligado) em IEDs cujos BRCBs têm o atributo ResvTms (edição 2). "
         "Também remove o nome do LD após a barra no nome do DataSet criado. Não é ligado automaticamente pela xml61850."),
    22: ("Arredonda o milissegundo do SOE",
         "Calcula o milissegundo dos eventos de SOE a partir do FractionOfSecond por arredondamento, em vez de truncagem "
         "(por exemplo, 0,805987 s vira 806 ms em vez de 805 ms)."),
    23: ("Janela MMS fixada em 1",
         "Fixa em 1 a janela de requisições pendentes, em vez do maxServOutstanding negociado no Initiate. "
         "Para IEDs cujo servidor MMS falha com várias requisições simultâneas (módulo i61850 1.27 ou mais novo)."),
}


# Configuração do simulador: só o perfil administrador. A operação (pontos, falhas, relógio, cenários, ensaios,
# iniciar/parar) fica com operacao e administrador; leitura só consulta.
_ID = r"[^/]+"
CONFIG_ROTAS = [(m, re.compile("/api/v1" + p)) for m, p in [
    ("POST", "/net/alias"), ("POST", "/cid"),
    ("POST", "/ieds"), ("PATCH", f"/ieds/{_ID}"), ("DELETE", f"/ieds/{_ID}"),
    ("POST", f"/ieds/{_ID}/duplicate"), ("POST", f"/ieds/{_ID}/reimport"),
    ("POST", f"/ieds/{_ID}/rcbs"), ("DELETE", f"/ieds/{_ID}/rcbs/.+"),
    ("POST", f"/ieds/{_ID}/datasets"), ("DELETE", f"/ieds/{_ID}/datasets/.+"),
    ("DELETE", f"/ieds/{_ID}/logs"),
    ("POST", f"/ieds/{_ID}/logicas"), ("PUT", f"/ieds/{_ID}/logicas/.+"), ("DELETE", f"/ieds/{_ID}/logicas/.+"),
    ("PATCH", f"/ieds/{_ID}/controles/config"), ("DELETE", f"/ieds/{_ID}/comandos"),
    ("POST", f"/profiles/{_ID}/copy"), ("PUT", f"/profiles/{_ID}"), ("DELETE", f"/profiles/{_ID}"),
    ("POST", "/cid-editor/.+"), ("DELETE", "/cid-editor/.+"),
    ("PUT", "/settings"), ("POST", "/users"), ("PATCH", f"/users/{_ID}"), ("DELETE", f"/users/{_ID}"),
]]


def create_app(mgr: Manager) -> FastAPI:
    # Todos os endpoints são async: o estado dos IEDs (motor de reports, sockets MMS, timers) vive no
    # event loop e não pode ser tocado a partir do threadpool que o FastAPI usa para endpoints síncronos.
    @asynccontextmanager
    async def lifespan(app):
        await mgr.startup()
        yield
        await mgr.shutdown()

    def so_admin(method: str, path: str) -> bool:
        return method not in ("GET", "HEAD") and any(m == method and r.fullmatch(path) for m, r in CONFIG_ROTAS)

    app = FastAPI(title="Simulador de IEDs IEC 61850", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.exception_handler(PointError)
    async def _perr(request, e: PointError):
        return JSONResponse({"erro": str(e), "caminho": e.path, "codigo": e.code}, status_code=e.status)

    @app.exception_handler(ValueError)
    async def _verr(request, e: ValueError):
        return JSONResponse({"erro": str(e)}, status_code=400)

    @app.exception_handler(KeyError)
    async def _kerr(request, e: KeyError):
        return JSONResponse({"erro": f"não encontrado: {e}"}, status_code=404)

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in ("/api/v1/login", "/api/v1/info") and \
                not path.startswith("/api/docs") and path != "/api/openapi.json":
            tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or \
                request.query_params.get("token")
            who = mgr.auth.check(tok)
            if who is None:
                return JSONResponse({"erro": "autenticação obrigatória (RN-UI-06)"}, status_code=401)
            if who[1] == "leitura" and request.method not in ("GET", "HEAD") and \
                    path not in ("/api/v1/me/password", "/api/v1/logout"):
                return JSONResponse({"erro": "perfil de leitura não pode alterar o simulador (RN-SEG-03)"},
                                    status_code=403)
            if who[1] != "administrador" and so_admin(request.method, path):
                return JSONResponse({"erro": "somente o perfil administrador altera a configuração do simulador"},
                                    status_code=403)
            request.state.quem = who
        resp = await call_next(request)
        if path == "/" or path.startswith("/static/"):
            # o navegador revalida (ETag) a cada carga: depois de atualizar o simulador, a tela já vem nova
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/api/v1/eu")
    async def eu(request: Request):
        usuario, papel = request.state.quem
        return {"usuario": usuario, "papel": papel}

    # ------------------------------------------------------------------ geral
    @app.get("/api/v1/info")
    async def info():
        return {"versao": __version__, "admin": f"{mgr.admin_host}:{mgr.admin_port}",
                "autenticacao": mgr.auth.required, "teste_local": mgr.local_test,
                "rodando": [i.name for i in mgr.ieds.values() if i.state == "rodando"],
                "hora_utc": time.time()}

    @app.post("/api/v1/login")
    async def login(body: dict):
        r = await asyncio.to_thread(mgr.auth.login, str(body.get("usuario", "")), str(body.get("senha", "")))
        if r is None:
            return JSONResponse({"erro": "usuário ou senha inválidos"}, status_code=401)
        return {"token": r[0], "papel": r[1]}

    @app.post("/api/v1/logout")
    async def logout(request: Request):
        """Encerra a sessão: o token deixa de valer no servidor."""
        tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token")
        if tok:
            mgr.auth.logout(tok)
        return {"ok": True}

    @app.post("/api/v1/panic")
    async def panic():
        return await mgr.panic()

    @app.get("/api/v1/opmsk/bits")
    async def opmsk_bits():
        """Os 24 bits do OPMSK (CNF.CONFIG do SAGE) com resumo e efeito, para o gerador."""
        return {"bits": [{"bit": b, "opt": f"OPT{b}", "efeito": OPMSK_BITS.get(b, ("Sem uso documentado", ""))[0],
                          "detalhe": OPMSK_BITS.get(b, ("", ""))[1], "valor": f"{1 << b:X}"} for b in range(24)]}

    @app.get("/api/v1/opmsk/sugestao")
    async def opmsk_sugestao(ied: int):
        """Bits que dependem do CID do IED (DataSets dinâmicos e local dos RCBs), com o motivo de cada um."""
        r = mgr.get(ied)
        scl = await asyncio.to_thread(parse_scl, mgr.read_cid(r.cfg["cid_arquivo"]))
        si = next((i for i in scl.ieds if i.name == r.cfg.get("ied_scl")), scl.ieds[0])
        rcbs = [(ln.name, rc) for ld in si.lds for ln in ld.lns for rc in ln.rcbs]
        brcb = sum(1 for _, rc in rcbs if rc.buffered)
        dyn = "DynDataSet" in si.services
        fora = sorted({n for n, _ in rcbs if n != "LLN0"})
        bits = [
            {"bit": 4, "ligar": not dyn,
             "motivo": "o IED declara DynDataSet nos Services: o SAGE pode criar DataSets dinâmicos" if dyn else
             "o IED não declara DynDataSet nos Services: use os DataSets pré-configurados (é o que a xml61850 faz)"},
            {"bit": 5, "ligar": bool(rcbs) and not fora,
             "motivo": ("todos os RCBs estão no LLN0" if rcbs and not fora else
                        f"há RCBs fora do LLN0 ({', '.join(fora[:6])})" if fora else "o CID não tem ReportControl")},
        ]
        if brcb and brcb < len(rcbs):
            bits.append({"bit": 19, "ligar": None, "motivo": f"o IED tem {brcb} BRCB(s) e {len(rcbs) - brcb} URCB(s): "
                         "ligue para usar só BRCBs (guardam eventos durante a queda da associação), ou o 17 para só URCBs"})
        elif rcbs and not brcb:
            bits.append({"bit": 17, "ligar": None, "motivo": "o IED só tem URCBs; o bit 17 não muda nada, o 19 deixaria o SAGE sem RCB"})
        if scl.edition >= 2 and dyn and brcb:
            bits.append({"bit": 21, "ligar": None, "motivo": "Edição 2 com DataSets dinâmicos e BRCBs (ResvTms): avalie ligar"})
        return {"ied": r.name, "ied_scl": si.name, "edicao": scl.edition, "rcbs": len(rcbs), "brcb": brcb,
                "urcb": len(rcbs) - brcb, "dynamic": dyn, "bits": bits}

    @app.get("/api/v1/opmsk")
    async def opmsk(valor: str):
        try:
            v = int(valor, 16)
        except ValueError as e:
            raise PointError("OPMSK deve ser hexadecimal (ex.: 10C830)") from e
        bits = [{"bit": b, "opt": f"OPT{b}", "ligado": bool(v >> b & 1), "efeito": OPMSK_BITS.get(b, ("", ""))[0],
                 "detalhe": OPMSK_BITS.get(b, ("", ""))[1]} for b in range(24) if (v >> b & 1) or b in OPMSK_BITS]
        return {"valor": f"{v:X}", "bits": bits}

    # ------------------------------------------------------------------ rede
    @app.get("/api/v1/net/interfaces")
    async def interfaces():
        return {"interfaces": mgr.interfaces(), "teste_local": mgr.local_test}

    @app.post("/api/v1/net/alias")
    async def alias(body: dict):
        ok, msg = await asyncio.get_running_loop().run_in_executor(
            None, netif.add_alias, body["interface"], body["ip"], int(body.get("prefixo", 24)))
        if not ok:
            raise PointError(msg, status=409)
        return {"ok": True, "mensagem": msg}

    @app.get("/api/v1/net/monitor")
    async def monitor():
        return mgr.netmon.estado()

    @app.delete("/api/v1/net/monitor")
    async def monitor_limpar():
        mgr.netmon.limpar()
        return {"ok": True}

    # ------------------------------------------------------------------ CID
    @app.post("/api/v1/cid")
    async def upload_cid(arquivo: UploadFile = File(...)):
        data = await arquivo.read()
        if len(data) > 50 * 1024 * 1024:
            raise PointError("arquivo grande demais (50 MB)", status=413)
        return mgr.upload(data, arquivo.filename or "ied.cid")

    # ------------------------------------------------------------------ IEDs
    @app.get("/api/v1/ieds")
    async def list_ieds():
        out = [i.summary() for i in mgr.ieds.values()]
        out += [{"id": k, "nome": c.get("nome"), "estado": "erro", "erro": f"falha ao carregar: {e}",
                 "cid": c.get("cid_nome"), "perfil": c.get("perfil"), "ip": c.get("ip"), "porta": c.get("porta"),
                 "associacoes": 0, "reports_enviados": 0, "falhas_ativas": []} for k, (c, e) in mgr.broken.items()]
        return {"ieds": out}

    @app.post("/api/v1/ieds")
    async def create_ied(body: dict):
        return mgr.create_ied(body).detail()

    @app.get("/api/v1/ieds/{iid}")
    async def get_ied(iid: int):
        return mgr.get(iid).detail()

    @app.patch("/api/v1/ieds/{iid}")
    async def patch_ied(iid: int, body: dict):
        return (await mgr.update_ied(iid, body)).detail()

    @app.delete("/api/v1/ieds/{iid}")
    async def del_ied(iid: int):
        await mgr.delete_ied(iid)
        return {"ok": True}

    @app.post("/api/v1/ieds/{iid}/start")
    async def start(iid: int):
        await mgr.start_ied(iid)
        return mgr.get(iid).summary()

    @app.post("/api/v1/ieds/{iid}/stop")
    async def stop(iid: int):
        await mgr.stop_ied(iid)
        return mgr.get(iid).summary()

    @app.post("/api/v1/ieds/{iid}/duplicate")
    async def duplicate(iid: int, body: dict | None = None):
        return mgr.duplicate(iid, body or {}).summary()

    @app.post("/api/v1/ieds/{iid}/reimport")
    async def reimport(iid: int, body: dict):
        return await mgr.reimport(iid, body)

    @app.get("/api/v1/ieds/{iid}/export-cid")
    async def export(iid: int):
        data, name = mgr.export_cid(iid)
        return Response(data, media_type="application/xml",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------------ configuração do sistema
    sistema = Sistema(mgr)

    def so_adm(request: Request):
        q = getattr(request.state, "quem", ("", ""))
        if q[1] != "administrador":
            raise PointError("somente o perfil administrador", status=403)
        return q[0]

    @app.get("/api/v1/settings")
    async def get_settings():
        """Preferências do servidor (padrões para novos IEDs, OPMSK padrão, visão inicial do editor)."""
        return sistema.settings()

    @app.put("/api/v1/settings")
    async def put_settings(body: dict):
        return sistema.save_settings(body)

    @app.get("/api/v1/system")
    async def system_info(request: Request):
        """Versão, endereço, pasta de dados, autenticação/LDAP, IEDs e uso de disco."""
        so_adm(request)
        return await asyncio.to_thread(sistema.info)

    @app.get("/api/v1/users")
    async def users(request: Request):
        so_adm(request)
        return {"usuarios": sistema.users(), "eu": request.state.quem[0]}

    @app.post("/api/v1/users")
    async def add_user(body: dict):
        return await asyncio.to_thread(sistema.add_user, body)

    @app.patch("/api/v1/users/{nome}")
    async def patch_user(nome: str, body: dict, request: Request):
        return await asyncio.to_thread(sistema.update_user, nome, body, request.state.quem[0])

    @app.delete("/api/v1/users/{nome}")
    async def del_user(nome: str, request: Request):
        sistema.delete_user(nome, request.state.quem[0])
        return {"ok": True}

    @app.post("/api/v1/me/password")
    async def my_password(body: dict, request: Request):
        """Troca a senha do próprio usuário local (qualquer papel)."""
        await asyncio.to_thread(sistema.change_own_password, request.state.quem[0], str(body.get("atual", "")),
                                str(body.get("nova", "")))
        return {"ok": True}

    @app.get("/api/v1/backup")
    async def backup(request: Request):
        """ZIP com banco, CIDs, perfis, rascunhos do editor e config.json."""
        so_adm(request)
        data, nome = await asyncio.to_thread(sistema.backup)
        return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{nome}"'})

    # ------------------------------------------------------------------ editor de CID
    ed = CidEditor(mgr)
    app.state.cid_editor = ed

    async def in_session(sid: str, fn):
        """Roda fn(sessão) fora do event loop (documentos grandes levam décimos de segundo) e em série."""
        def run():
            s = ed.get(sid)
            with s.lock:
                return fn(s)
        return await asyncio.to_thread(run)

    def quem(request: Request) -> str:
        q = getattr(request.state, "quem", None)
        return q[0] if q else ""

    @app.get("/api/v1/cid-editor")
    async def ed_list():
        """Rascunhos guardados (CIDs de IEDs com alterações não aplicadas e arquivos avulsos)."""
        return {"rascunhos": await asyncio.to_thread(ed.list)}

    @app.post("/api/v1/cid-editor/files")
    async def ed_upload(arquivo: UploadFile = File(...)):
        """Abre um arquivo SCL avulso no editor, sem criar IED."""
        data = await arquivo.read()
        if len(data) > 50 * 1024 * 1024:
            raise PointError("arquivo grande demais (50 MB)", status=413)
        try:
            s = await asyncio.to_thread(ed.upload, data, arquivo.filename or "arquivo.cid")
        except SclError as e:
            raise PointError(str(e), status=422) from e
        return await in_session(s.sid, lambda s: s.summary())

    @app.get("/api/v1/cid-editor/{sid}")
    async def ed_summary(sid: str):
        """Resumo do rascunho. `ied-<id>` abre o CID do IED (sem criar rascunho até a primeira alteração)."""
        try:
            return await in_session(sid, lambda s: s.summary())
        except SclError as e:
            raise PointError(str(e), status=422) from e

    @app.delete("/api/v1/cid-editor/{sid}")
    async def ed_discard(sid: str):
        """Descarta o rascunho (o CID do IED não muda)."""
        await asyncio.to_thread(ed.discard, sid)
        return {"ok": True}

    @app.get("/api/v1/cid-editor/{sid}/children")
    async def ed_children(sid: str, vista: str = "modelo", no: str = "", offset: int = 0):
        """Filhos de um nó da árvore (`no` vazio = primeiro nível da visão modelo, tipos ou xml)."""
        return await in_session(sid, lambda s: s.children(vista, no, offset))

    @app.get("/api/v1/cid-editor/{sid}/node")
    async def ed_node(sid: str, no: str):
        """Detalhes de um nó: atributos, tipo, instância, referências, DataSets que o contêm e o XML."""
        return await in_session(sid, lambda s: s.detail(no))

    @app.get("/api/v1/cid-editor/{sid}/locate")
    async def ed_locate(sid: str, no: str, vista: str = "auto"):
        """Caminho na árvore para revelar o nó numa visão (ou na mais adequada, com `vista=auto`)."""
        return await in_session(sid, lambda s: s.locate(no, vista))

    @app.get("/api/v1/cid-editor/{sid}/search")
    async def ed_search(sid: str, q: str, vista: str = "modelo"):
        """Busca por nome, referência, descrição ou valor de atributo."""
        return await in_session(sid, lambda s: s.search(q, vista))

    @app.get("/api/v1/cid-editor/{sid}/validate")
    async def ed_validate(sid: str):
        """Verificações do editor (tipos, referências, FCDAs, nomes repetidos, valores) e do carregador do simulador."""
        return await in_session(sid, lambda s: s.validate())

    @app.get("/api/v1/cid-editor/{sid}/diff")
    async def ed_diff(sid: str, contexto: int = 3):
        """Diferenças entre o CID original e o rascunho (formato unified diff)."""
        return await in_session(sid, lambda s: s.diff(max(0, min(contexto, 20))))

    @app.get("/api/v1/cid-editor/{sid}/download")
    async def ed_download(sid: str):
        """Baixa o rascunho como arquivo SCL."""
        data, nome = await in_session(sid, lambda s: (s.bytes, s.meta["nome"]))
        base = Path(nome).stem
        ext = Path(nome).suffix or ".cid"
        return Response(data, media_type="application/xml",
                        headers={"Content-Disposition": f'attachment; filename="{base}_editado{ext}"'})

    @app.get("/api/v1/cid-editor/{sid}/overview")
    async def ed_overview(sid: str):
        """Visão geral para o visualizador: cabeçalho, IEDs, comunicação, blocos de controle, DataSets,
        assinaturas, subestação e dados privados."""
        return await in_session(sid, lambda s: s.overview())

    @app.get("/api/v1/cid-editor/{sid}/export")
    async def ed_export(sid: str, tipo: str):
        """CSV (separado por ponto e vírgula): datasets, controles, comunicacao ou pontos."""
        head, rows, nome = await in_session(sid, lambda s: (*s.export_rows(tipo), s.meta["nome"]))
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(head)
        w.writerows(rows)
        return PlainTextResponse("\ufeff" + buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{Path(nome).stem}_{tipo}.csv"'})

    @app.get("/api/v1/cid-editor/{sid}/compare")
    async def ed_compare(sid: str, com: str = "base", sem_nome: bool | None = None):
        """Compara o rascunho com o original (`com=base`) ou com outra sessão (`ied-<id>` ou `arq-<hex>`):
        IEDs, LNs, instâncias, DataSets, blocos de controle, comunicação e tipos."""
        from .cidedit import Doc
        if com == "base":
            base, nome = await in_session(sid, lambda s: (s.base, "CID original"))
        else:
            if com == sid:
                raise PointError("escolha outro arquivo para comparar", status=422)
            base, nome = await in_session(com, lambda s: (s.bytes, s.meta["nome"]))
        other = await asyncio.to_thread(Doc, base)
        return await in_session(sid, lambda s: s.compare(other, nome, sem_nome))

    @app.post("/api/v1/cid-editor/{sid}/ops")
    async def ed_op(sid: str, body: dict, request: Request):
        """Aplica uma operação ao rascunho; ver API.md (atributos, texto, excluir, mover, duplicar, adicionar,
        substituir, valor, descricao, fcda)."""
        user = quem(request)
        return await in_session(sid, lambda s: dict(s.apply_op(body, user), sessao=s.summary()))

    @app.post("/api/v1/cid-editor/{sid}/undo")
    async def ed_undo(sid: str, request: Request):
        user = quem(request)
        return await in_session(sid, lambda s: dict(s.undo_op(user), sessao=s.summary()))

    @app.post("/api/v1/cid-editor/{sid}/redo")
    async def ed_redo(sid: str, request: Request):
        user = quem(request)
        return await in_session(sid, lambda s: dict(s.undo_op(user, redo=True), sessao=s.summary()))

    @app.post("/api/v1/cid-editor/{sid}/upload-token")
    async def ed_token(sid: str):
        """Prepara o rascunho como um upload de CID, para criar um IED novo com ele (POST /ieds)."""
        data, nome = await in_session(sid, lambda s: (s.bytes, s.meta["nome"]))
        return await asyncio.to_thread(mgr.upload, data, nome)

    @app.post("/api/v1/cid-editor/{sid}/apply")
    async def ed_apply(sid: str, request: Request, body: dict | None = None):
        """Aplica o rascunho no IED de origem (reimportação: mantém o estado dos pontos que continuam existindo)."""
        body = body or {}
        s = await asyncio.to_thread(ed.get, sid)
        if s.meta["origem"] != "ied":
            raise PointError("arquivo avulso: baixe o CID ou crie um IED com ele", status=422)
        ied = mgr.get(s.meta["ied"])
        data, mudou = await in_session(sid, lambda s: (s.bytes, s.base_changed()))
        avisos = []
        if mudou:
            avisos.append("o CID do IED mudou depois que este rascunho foi aberto; aplicar substitui aquela versão")
        if ied.state == "rodando":
            avisos.append(f"o IED {ied.name} está rodando: aplicar derruba as associações")
        if avisos and not body.get("confirmar"):
            raise PointError("; ".join(avisos).capitalize() + ". Confirme.", status=409, code="confirmar")
        up = await asyncio.to_thread(mgr.upload, data, ied.cfg.get("cid_nome") or "ied.cid")
        res = await mgr.reimport(ied.id, {"token": up["token"], "confirmar": True, "ied_scl": body.get("ied_scl")})
        mgr.uploads.pop(up["token"], None)
        await in_session(sid, lambda s: ed.rebase(s, data))
        mgr.broadcast({"evento": "ied.state", "ied": ied.id, "dados": ied.summary()})
        return dict(res, sessao=await in_session(sid, lambda s: s.summary()))

    # ------------------------------------------------------------------ pontos
    @app.get("/api/v1/ieds/{iid}/points")
    async def points(iid: int, q: str = "", ln: str = "", dataset: str = "", modo: str = "", tipo: str = "",
               ld: str = "", cdc: str = "", limit: int = 5000, offset: int = 0):
        ied = mgr.get(iid)
        ql = q.lower()
        out = []
        for p in ied.points:
            if tipo and p.kind not in tipo.split(","):
                continue
            if ql and ql not in p.path.lower() and ql not in (p.desc or "").lower():
                continue
            if ln and p.ln != ln:
                continue
            if ld and p.domain != ld:
                continue
            if modo and p.mode != modo:
                continue
            if cdc and p.cdc not in cdc.split(","):
                continue
            if dataset and not any(dataset == d or d.endswith("$" + dataset) for d, _ in p.datasets):
                continue
            out.append(p)
        return {"total": len(out), "pontos": [ied.point_json(p) for p in out[offset:offset + limit]],
                "lns": sorted({p.ln for p in ied.points}), "lds": sorted(ied.domains),
                "datasets": sorted(ds.ref for ds in ied.all_datasets())}

    @app.get("/api/v1/ieds/{iid}/points/{pid}")
    async def point(iid: int, pid: str):
        ied = mgr.get(iid)
        return ied.point_json(ied.find_point(pid))

    @app.patch("/api/v1/ieds/{iid}/points/{pid}")
    async def patch_point(iid: int, pid: str, body: dict):
        ied = mgr.get(iid)
        p = ied.find_point(pid)
        op = dict(body, id=p.id)
        ied.set_points([op], origin=body.get("origem", "manual") if body.get("origem") in ("script",) else "manual")
        return ied.point_json(p)

    @app.post("/api/v1/ieds/{iid}/points:batch")
    async def batch(iid: int, body: dict):
        ied = mgr.get(iid)
        ops = body.get("operacoes") or body.get("ops") or []
        if not isinstance(ops, list) or not ops:
            raise PointError("informe 'operacoes': [{id|path, valor, qualidade, modo, ...}]")
        origin = "script" if body.get("origem") == "script" else "manual"
        return {"resultados": ied.set_points(ops, origin=origin)}

    @app.post("/api/v1/ieds/{iid}/points/{pid}/pulse")
    async def pulse(iid: int, pid: str, body: dict | None = None):
        b = body or {}
        mgr.get(iid).pulse(pid, int(b.get("ms", 500)), int(b.get("ciclos", 1)),
                           int(b["periodo_ms"]) if b.get("periodo_ms") else None)
        return {"ok": True}

    @app.post("/api/v1/ieds/{iid}/points:csv")
    async def points_csv(iid: int, request: Request):
        """RN-ANA-07: CSV com linhas 'caminho;valor' (ou vírgula)."""
        ied = mgr.get(iid)
        text = (await request.body()).decode("utf-8-sig")
        ops = []
        sample = text[:2000]
        delim = ";" if sample.count(";") >= sample.count(",") else ","
        for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delim)):
            if not row or not row[0].strip() or row[0].strip().startswith("#"):
                continue
            if i == 0 and row[0].strip().lower() in ("caminho", "path", "ponto"):
                continue
            if len(row) < 2:
                raise PointError(f"linha {i + 1}: esperado caminho{delim}valor")
            val = row[1].strip().replace(",", ".") if delim == ";" else row[1].strip()
            ops.append({"path": row[0].strip(), "valor": val, "confirmar_fora_faixa": True})
        if not ops:
            raise PointError("CSV vazio")
        return {"resultados": ied.set_points(ops, origin="csv")}

    # ------------------------------------------------------------------ controles, comandos e lógicas
    @app.get("/api/v1/ieds/{iid}/controles")
    async def controles(iid: int):
        """Objetos de controle (DOs com Oper/SBOw/SBO): ctlModel, estado, seleção, bloqueios e quem executa."""
        ied = mgr.get(iid)
        return {"controles": [o.json() for o in ied.control.objs.values()]}

    @app.post("/api/v1/ieds/{iid}/controles/comando")
    async def comando_local(iid: int, body: dict):
        """Comando de teste pela tela {chave, valor}: passa pelas mesmas verificações de um comando MMS."""
        ied = mgr.get(iid)
        return ied.control.local_command(str(body.get("chave", "")), body.get("valor"))

    @app.patch("/api/v1/ieds/{iid}/controles/config")
    async def controles_config(iid: int, body: dict):
        """{chave: {tempo_ms, rejeitar_mesma_posicao, timeout_ms}} por objeto de controle (eco e terminação)."""
        ied = mgr.get(iid)
        cfg = ied.cfg.setdefault("controles", {})
        for k, v in body.items():
            if k not in ied.control.objs or not isinstance(v, dict):
                raise PointError(f"objeto de controle inexistente: {k}", status=404)
            c = cfg.setdefault(k, {})
            for f in ("tempo_ms", "timeout_ms"):
                if f in v:
                    n = int(v[f])
                    if not 0 <= n <= 600000:
                        raise PointError(f"{f} deve ficar entre 0 e 600000 ms", status=422)
                    c[f] = n
            if "rejeitar_mesma_posicao" in v:
                c["rejeitar_mesma_posicao"] = bool(v["rejeitar_mesma_posicao"])
        mgr.save(ied)
        return {"controles": [o.json() for o in ied.control.objs.values()]}

    @app.get("/api/v1/ieds/{iid}/comandos")
    async def comandos(iid: int, limit: int = 500, ref: str = ""):
        """Comandos recebidos (mais recentes primeiro): objeto, endereço MMS, serviço, valor, origem, ctlNum,
        Test/Check, resultado (AddCause) e CommandTermination. `ref` filtra por objeto (ex.: LD/CSWI1.Pos;
        vários separados por vírgula)."""
        mgr.get(iid)
        mgr.storage.flush()
        refs = [r for r in ref.split(",") if r]
        return {"comandos": mgr.storage.cmds(iid, max(1, min(limit, 5000)), refs or None)}

    @app.delete("/api/v1/ieds/{iid}/comandos")
    async def limpar_comandos(iid: int):
        mgr.get(iid)
        mgr.storage.clear_cmds(iid)
        return {"ok": True}

    @app.get("/api/v1/ieds/{iid}/logicas")
    async def logicas(iid: int):
        from .logic import BLOCOS
        ied = mgr.get(iid)
        lst = ied.cfg.get("logicas", [])
        refs = {b.get("p", {}).get("ponto") for lg in lst for b in lg.get("blocos", [])} - {None, ""}
        pontos = {r: {"cdc": ied.by_path[r].cdc, "desc": ied.by_path[r].desc} for r in refs if r in ied.by_path}
        return {"logicas": lst, "blocos": BLOCOS, "pontos": pontos,
                "controles": [o.json() for o in ied.control.objs.values()]}

    @app.get("/api/v1/ieds/{iid}/logicas/estado")
    async def logicas_estado(iid: int):
        """Valores ao vivo das saídas de cada bloco, último comando e bloqueios ativos."""
        return mgr.get(iid).logic.live()

    def _save_logic(ied, lg: dict, lid: str | None):
        from .logic import validate
        lst = ied.cfg.setdefault("logicas", [])
        norm, avisos = validate(ied, lg)
        if lid is None:
            n = 1
            ids = {x["id"] for x in lst}
            while f"L{n}" in ids:
                n += 1
            norm["id"] = f"L{n}"
            lst.append(norm)
        else:
            i = next((i for i, x in enumerate(lst) if x["id"] == lid), None)
            if i is None:
                raise PointError(f"lógica {lid} não existe", status=404)
            norm["id"] = lid
            lst[i] = norm
        ied.logic.load(lst)
        mgr.save(ied)
        return {"logica": norm, "avisos": avisos}

    @app.post("/api/v1/ieds/{iid}/logicas")
    async def nova_logica(iid: int, body: dict):
        return _save_logic(mgr.get(iid), body, None)

    @app.put("/api/v1/ieds/{iid}/logicas/{lid}")
    async def salvar_logica(iid: int, lid: str, body: dict):
        return _save_logic(mgr.get(iid), body, lid)

    @app.delete("/api/v1/ieds/{iid}/logicas/{lid}")
    async def apagar_logica(iid: int, lid: str):
        ied = mgr.get(iid)
        lst = ied.cfg.setdefault("logicas", [])
        ied.cfg["logicas"] = [x for x in lst if x["id"] != lid]
        ied.logic.load(ied.cfg["logicas"])
        mgr.save(ied)
        return {"ok": True}

    # ------------------------------------------------------------------ subida de pontos
    @app.get("/api/v1/ieds/{iid}/subida")
    async def subida(iid: int):
        return mgr.get(iid).subida.estado()

    @app.get("/api/v1/ieds/{iid}/subida/pontos")
    async def subida_pontos(iid: int):
        return {"pontos": mgr.get(iid).subida.pontos()}

    @app.put("/api/v1/ieds/{iid}/subida/config")
    async def subida_config(iid: int, body: dict):
        return mgr.get(iid).subida.configurar(body)

    @app.post("/api/v1/ieds/{iid}/subida/linhas")
    async def subida_add(iid: int, body: dict):
        return mgr.get(iid).subida.adicionar(body.get("linhas"), body.get("modo") == "substituir")

    @app.patch("/api/v1/ieds/{iid}/subida/linhas/{lid}")
    async def subida_edit(iid: int, lid: int, body: dict):
        s = mgr.get(iid).subida
        if "posicao" in body:
            s.mover(lid, int(body.pop("posicao")))
        return s.editar(lid, body) if body else s.linha_json(s.linha(lid))

    @app.delete("/api/v1/ieds/{iid}/subida/linhas/{lid}")
    async def subida_del(iid: int, lid: int):
        mgr.get(iid).subida.remover(lid)
        return {"ok": True}

    @app.delete("/api/v1/ieds/{iid}/subida")
    async def subida_apagar(iid: int, resultados: bool = False):
        s = mgr.get(iid).subida
        s.limpar_resultados() if resultados else s.apagar_tudo()
        return {"ok": True}

    @app.post("/api/v1/ieds/{iid}/subida/linhas/{lid}/aplicar")
    async def subida_aplicar(iid: int, lid: int, body: dict):
        s = mgr.get(iid).subida
        s.aplicar(lid, body.get("etapa", ""))
        return s.linha_json(s.linha(lid))

    @app.post("/api/v1/ieds/{iid}/subida/executar")
    async def subida_executar(iid: int, body: dict):
        s = mgr.get(iid).subida
        s.executar(body.get("ids"), bool(body.get("so_pendentes", True)))
        return s.execucao()

    @app.post("/api/v1/ieds/{iid}/subida/{acao}")
    async def subida_controle(iid: int, acao: str):
        s = mgr.get(iid).subida
        if acao == "pausar":
            s.pausar(True)
        elif acao == "continuar":
            s.pausar(False)
        elif acao == "parar":
            s.interromper()
        else:
            raise PointError(f"ação desconhecida: {acao}", status=404)
        return s.execucao()

    @app.post("/api/v1/ieds/{iid}/subida-importar")
    async def subida_importar(iid: int, arquivo: UploadFile = File(...)):
        data = await arquivo.read()
        if len(data) > 20 * 1024 * 1024:
            raise PointError("planilha maior que 20 MB")
        return mgr.get(iid).subida.ler_arquivo(arquivo.filename or "", data)

    @app.get("/api/v1/ieds/{iid}/subida-planilha")
    async def subida_planilha(iid: int, modo: str = "modelo", resultado: bool = False, tipos: str = "dig",
                              so_dataset: bool = True):
        ied = mgr.get(iid)
        modo = "resultado" if resultado else modo
        if modo not in ("modelo", "preenchida", "resultado"):
            raise PointError("modo deve ser modelo, preenchida ou resultado")
        ts = {t for t in tipos.split(",") if t in ("dig", "ana")} or {"dig"}
        data = await asyncio.get_running_loop().run_in_executor(None, ied.subida.planilha, modo, ts, so_dataset)
        nome = f"subida_{modo}_{re.sub(r'[^A-Za-z0-9_-]', '_', ied.name)}.xlsx"
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{nome}"'})

    # ------------------------------------------------------------------ RCBs e DataSets
    @app.get("/api/v1/ieds/{iid}/rcbs")
    async def rcbs(iid: int):
        ied = mgr.get(iid)
        return {"rcbs": [r.state() for r in ied.engine.rcbs], "extras": ied.cfg.get("extras", {}).get("rcbs", [])}

    @app.post("/api/v1/ieds/{iid}/rcbs")
    async def add_rcb(iid: int, body: dict):
        return await mgr.add_extra_rcb(iid, body)

    @app.delete("/api/v1/ieds/{iid}/rcbs/{ld}/{ln}/{name}")
    async def del_rcb(iid: int, ld: str, ln: str, name: str, confirmar: bool = False):
        await mgr.del_extra_rcb(iid, ld, ln, name, confirmar)
        return {"ok": True}

    @app.patch("/api/v1/ieds/{iid}/rcbs/policy")
    async def set_rcb_policy_bulk(iid: int, body: dict):
        """Mesma política em vários RCBs: {"rcbs": [nomes], politica, atributos, ips, ocultar, restaurar_perfil,
        derrubar}. Valida tudo antes de aplicar (nada muda se um nome ou valor for inválido); sem 'instanciar'."""
        return await mgr.set_rcb_policy_bulk(iid, body)

    @app.get("/api/v1/ieds/{iid}/rcbs/{nome}/policy")
    async def rcb_policy(iid: int, nome: str):
        """Política de acesso do RCB (nome-base, nome da instância ou referência): valores efetivos, origem de cada
        campo (padrao, perfil, ied) e estado de cada instância (habilitado, cliente, escritas recusadas)."""
        return mgr.rcb_policy(iid, nome)

    @app.patch("/api/v1/ieds/{iid}/rcbs/{nome}/policy")
    async def set_rcb_policy(iid: int, nome: str, body: dict, request: Request):
        """Muda a política do RCB. Corpo (todos opcionais): politica (livre|ocupado|negado|somente_leitura),
        atributos {atributo: ok|erro2|erro3}, ips [lista], ocultar (bool), instanciar (true|false|null),
        restaurar_perfil (bool), derrubar (bool), confirmar (bool). Vale para as próximas escritas, sem reiniciar
        o IED; mudar 'instanciar' altera o modelo, reinicia o IED (409 'confirmar' se rodando) e exige administrador."""
        return await mgr.set_rcb_policy(iid, nome, body, admin=request.state.quem[1] == "administrador")

    @app.post("/api/v1/ieds/{iid}/rcbs/{nome}/disable-client")
    async def rcb_disable_client(iid: int, nome: str):
        """Desabilita na hora o RCB que um cliente habilitou e libera a reserva."""
        return mgr.disable_rcb_client(iid, nome)

    @app.get("/api/v1/ieds/{iid}/rcb-denied")
    async def rcb_denied(iid: int, limit: int = Query(500, le=100000), rcb: str | None = None):
        """Escritas recusadas em RCBs (mais recentes primeiro): RCB, atributo, código, IP do cliente e horário."""
        mgr.get(iid)
        return {"recusas": mgr.storage.rcb_denied(iid, limit, rcb)}

    @app.get("/api/v1/ieds/{iid}/datasets")
    async def datasets(iid: int):
        ied = mgr.get(iid)
        out = []
        for ds in ied.all_datasets():
            out.append({"ref": ds.ref, "ld": ds.domain, "ln": ds.ln, "nome": ds.name, "desc": ds.desc,
                        "extra": ds.extra, "dinamico": getattr(ds, "dynamic", False),
                        "membros": [{"pos": i, "ref": m.ref, "fc": m.fc} for i, m in enumerate(ds.members)],
                        "rcbs": [r.ref for r in ied.engine.rcbs if r.datset == ds.ref]})
        return {"datasets": out, "max": ied.profile["rcb"].get("max_datasets")}

    @app.post("/api/v1/ieds/{iid}/datasets")
    async def add_ds(iid: int, body: dict):
        return await mgr.add_extra_dataset(iid, body)

    @app.delete("/api/v1/ieds/{iid}/datasets/{ld}/{ln}/{name}")
    async def del_ds(iid: int, ld: str, ln: str, name: str, confirmar: bool = False):
        await mgr.del_extra_dataset(iid, ld, ln, name, confirmar)
        return {"ok": True}

    @app.get("/api/v1/ieds/{iid}/model")
    async def model(iid: int, ld: str, path: str = ""):
        ied = mgr.get(iid)
        d = ied.domains.get(ld)
        if d is None:
            raise PointError("LD inexistente", status=404)
        if not path:
            return {"filhos": [{"nome": n, "path": n, "folha": False} for n in d.lns]}
        node = d.lookup(path)
        if node is None:
            raise PointError("caminho inexistente", status=404)
        if node.is_leaf:
            return {"valor": node.json(), "tipo": node.mt.kind}
        return {"filhos": [{"nome": c.name, "path": c.path, "folha": c.is_leaf,
                            "valor": c.json() if c.is_leaf else None, "tipo": c.mt.kind}
                           for c in node.comps.values()]}

    # ------------------------------------------------------------------ relógio e falhas
    @app.get("/api/v1/ieds/{iid}/clock")
    async def clock(iid: int):
        return mgr.get(iid).clock.to_json()

    @app.put("/api/v1/ieds/{iid}/clock")
    async def set_clock(iid: int, body: dict):
        ied = mgr.get(iid)
        ied.clock.update(body)
        mgr.save(ied)
        ied.emit("clock.changed", ied.clock.to_json())
        return ied.clock.to_json()

    @app.get("/api/v1/ieds/{iid}/faults")
    async def faults(iid: int):
        ied = mgr.get(iid)
        return {"falhas": ied.faults.to_json(), "ativas": ied.faults.active()}

    @app.put("/api/v1/ieds/{iid}/faults")
    async def set_faults(iid: int, body: dict):
        ied = mgr.get(iid)
        ied.faults.update(body)
        mgr.save(ied)
        ied.mms_event(None, "Falhas", f"falhas ativas: {[f['id'] for f in ied.faults.active()] or 'nenhuma'}",
                      "configuração")
        ied.emit("ied.state", ied.summary())
        return {"falhas": ied.faults.to_json(), "ativas": ied.faults.active()}

    # ------------------------------------------------------------------ histórico
    @app.get("/api/v1/ieds/{iid}/reports")
    async def reports(iid: int, format: str = "json", since: float | None = None, until: float | None = None,
                limit: int = Query(500, le=100000), rcb: str | None = None):
        ied = mgr.get(iid)
        if format == "pcap":
            frames = [f for f in ied.frames if (since is None or f[0] >= since) and (until is None or f[0] <= until)]
            data = build_pcap(frames)
            return Response(data, media_type="application/vnd.tcpdump.pcap",
                            headers={"Content-Disposition": f'attachment; filename="simied_{iid}_{int(time.time())}.pcap"'})
        rows = mgr.storage.reports(iid, since, until, limit, rcb)
        if format == "csv":
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            w.writerow(["id", "hora_utc", "rcb", "rptid", "entry_id", "sqnum", "tipo", "cliente", "descartado",
                        "pontos", "notas", "pdu_hex"])
            for r in reversed(rows):
                w.writerow([r["id"], time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(r["ts"])) + f".{int(r['ts'] % 1 * 1000):03d}Z",
                            r["rcb"], r["rptid"], r["entry_id"], r["sqnum"], r["tipo"], r["cliente"],
                            r["descartado"], " | ".join(f"{p['ref']}={p['valor']} ({','.join(p['motivo'])})"
                                                        for p in r["pontos"]),
                            " | ".join(r["notas"]), r["pdu"]])
            return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                     headers={"Content-Disposition": f'attachment; filename="reports_{iid}.csv"'})
        return {"reports": rows}

    @app.get("/api/v1/ieds/{iid}/history")
    async def history(iid: int, since: float | None = None, limit: int = 500, ponto: str | None = None,
                format: str = "json"):
        mgr.get(iid)
        rows = mgr.storage.changes(iid, since, limit, ponto)
        if format == "csv":
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            w.writerow(["id", "hora_local", "ponto", "valor", "qualidade", "t", "origem", "reports"])
            for r in reversed(rows):
                w.writerow([r["id"], time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])), r["path"], r["valor"],
                            (r["qualidade"] or {}).get("validity", ""), r["t"], r["origem"],
                            ",".join(map(str, r["reports"]))])
            return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                     headers={"Content-Disposition": f'attachment; filename="historico_{iid}.csv"'})
        return {"mudancas": rows}

    @app.get("/api/v1/ieds/{iid}/mms-log")
    async def mms_log(iid: int, limit: int = 500):
        mgr.get(iid)
        return {"eventos": mgr.storage.mms_log(iid, limit)}

    @app.delete("/api/v1/ieds/{iid}/logs")
    async def clear_logs(iid: int):
        mgr.get(iid)
        mgr.storage.clear_logs(iid)
        return {"ok": True}

    @app.get("/api/v1/ieds/{iid}/sessions")
    async def sessions(iid: int):
        ied = mgr.get(iid)
        return {"sessoes": [ied.session_json(a) for a in (ied.server.associations() if ied.server else [])]}

    @app.get("/api/v1/ieds/{iid}/links")
    async def links(iid: int):
        ied = mgr.get(iid)
        pts = [ied.point_json(p) for p in ied.points if p.link]
        return {"grupos": ied.mirror.state(), "pontos": pts}

    # ------------------------------------------------------------------ cenários
    @app.get("/api/v1/scenarios")
    async def scenarios():
        return {"cenarios": mgr.storage.list_json("scenario")}

    @app.post("/api/v1/scenarios")
    async def add_scenario(body: dict):
        if body.get("de_ied"):
            kinds = tuple(body.get("tipos") or ("dig", "ana"))
            body = dict(mgr.scenario_from_ied(int(body["de_ied"]), body.get("nome") or "cenário", kinds),
                        **{k: v for k, v in body.items() if k not in ("de_ied", "tipos")})
        if not body.get("nome"):
            raise PointError("informe o nome do cenário")
        _validate_steps(body.get("passos", []))
        sid = mgr.storage.add_json("scenario", body)
        return mgr.storage.get_json("scenario", sid)

    @app.get("/api/v1/scenarios/{sid}")
    async def get_scenario(sid: int):
        s = mgr.storage.get_json("scenario", sid)
        if s is None:
            raise PointError("cenário inexistente", status=404)
        return s

    @app.put("/api/v1/scenarios/{sid}")
    async def put_scenario(sid: int, body: dict):
        if mgr.storage.get_json("scenario", sid) is None:
            raise PointError("cenário inexistente", status=404)
        _validate_steps(body.get("passos", []))
        mgr.storage.put_json("scenario", sid, body)
        return mgr.storage.get_json("scenario", sid)

    @app.delete("/api/v1/scenarios/{sid}")
    async def del_scenario(sid: int):
        mgr.storage.del_json("scenario", sid)
        return {"ok": True}

    @app.post("/api/v1/scenarios/{sid}/apply")
    async def apply_scenario(sid: int, ied: int):
        return mgr.apply_scenario(sid, ied)

    # ------------------------------------------------------------------ perfis
    @app.get("/api/v1/profiles")
    async def profiles():
        return {"perfis": list(mgr.profiles.all().values())}

    @app.get("/api/v1/profiles/{pid}")
    async def profile(pid: str):
        p = mgr.profiles.get(pid)
        if p is None:
            raise PointError("perfil inexistente", status=404)
        return p

    @app.post("/api/v1/profiles/{pid}/copy")
    async def copy_profile(pid: str, body: dict):
        return mgr.profiles.copy(pid, body.get("novo_id", ""), body.get("nome"))

    @app.put("/api/v1/profiles/{pid}")
    async def put_profile(pid: str, body: dict):
        body["id"] = pid
        p = mgr.profiles.save(body)
        return p

    @app.delete("/api/v1/profiles/{pid}")
    async def del_profile(pid: str):
        if any(i.cfg.get("perfil") == pid for i in mgr.ieds.values()):
            raise PointError("perfil em uso por algum IED", status=409)
        mgr.profiles.delete(pid)
        return {"ok": True}

    # ------------------------------------------------------------------ ensaios (seção 13)
    @app.get("/api/v1/ensaios")
    async def ensaios():
        return {"ensaios": mgr.storage.list_json("ensaio")}

    @app.post("/api/v1/ensaios")
    async def add_ensaio(body: dict):
        body.setdefault("registrado_em", time.time())
        return mgr.storage.get_json("ensaio", mgr.storage.add_json("ensaio", body))

    @app.put("/api/v1/ensaios/{eid}")
    async def put_ensaio(eid: int, body: dict):
        mgr.storage.put_json("ensaio", eid, body)
        return mgr.storage.get_json("ensaio", eid)

    @app.delete("/api/v1/ensaios/{eid}")
    async def del_ensaio(eid: int):
        mgr.storage.del_json("ensaio", eid)
        return {"ok": True}

    # ------------------------------------------------------------------ WebSocket
    @app.websocket("/api/v1/ws")
    async def ws(sock: WebSocket):
        if mgr.auth.check(sock.query_params.get("token")) is None:
            await sock.close(code=4401)
            return
        await sock.accept()
        q: asyncio.Queue = asyncio.Queue()
        mgr.ws.add(q)

        async def sender():
            while True:
                msg = await q.get()
                batch = [msg]
                while not q.empty() and len(batch) < 200:
                    batch.append(q.get_nowait())
                await sock.send_json(batch)

        st = asyncio.create_task(sender())
        try:
            while True:
                await sock.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            st.cancel()
            mgr.ws.discard(q)

    # ------------------------------------------------------------------ interface web
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index():
            return FileResponse(WEB_DIR / "index.html")

    return app


def _validate_steps(steps):
    if not isinstance(steps, list):
        raise PointError("'passos' deve ser uma lista")
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or not s.get("ponto"):
            raise PointError(f"passo {i + 1}: informe 'ponto'")
        if int(s.get("atraso_ms") or 0) < 0:
            raise PointError(f"passo {i + 1}: atraso negativo")

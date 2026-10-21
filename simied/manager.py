"""Aplicação: lista de IEDs, uploads de CID, vigilância de rede, cenários e parada rápida."""
from __future__ import annotations

import asyncio
import copy
import logging
import re
import secrets
import time
import uuid
from pathlib import Path

from . import netif
from .auth import Auth
from .ied import IedRuntime, PointError
from .netmon import NetMonitor
from .model import Builder, build_points, resolve_fcda
from .profiles import ProfileStore
from .rcbpolicy import CHAVES as RCB_POLICY_KEYS
from .rcbpolicy import effective as rcb_policy_effective
from .rcbpolicy import validate as rcb_policy_validate
from .scl import SclError, export_cid, parse_scl
from .storage import Storage

log = logging.getLogger("simied")


class Manager:
    def __init__(self, data_dir: Path, admin_host: str = "127.0.0.1", admin_port: int = 8080,
                 local_test: bool = False, ldap=None):
        self.data_dir = data_dir
        self.cid_dir = data_dir / "cids"
        self.cid_dir.mkdir(parents=True, exist_ok=True)
        self.storage = Storage(data_dir / "simied.db")
        self.profiles = ProfileStore(data_dir)
        self.admin_host = admin_host
        self.admin_port = admin_port
        self.local_test = local_test
        self.auth = Auth(self.storage, admin_host not in ("127.0.0.1", "::1", "localhost"), ldap)
        self.ieds: dict[int, IedRuntime] = {}
        self.netmon = NetMonitor(self)
        self.broken: dict[int, tuple[dict, str]] = {}
        self.ws: set[asyncio.Queue] = set()
        self.uploads: dict[str, dict] = {}
        self.tasks: list[asyncio.Task] = []
        self.scenario_tasks: dict[int, asyncio.Task] = {}

    # ------------------------------------------------------------------ ciclo de vida
    async def startup(self):
        for iid, cfg in self.storage.ieds():
            try:
                self.ieds[iid] = IedRuntime(self, iid, cfg)
            except Exception as e:  # noqa: BLE001
                log.exception("falha ao carregar IED %s", iid)
                self.broken[iid] = (cfg, str(e))
        loop = asyncio.get_running_loop()
        self.tasks.append(loop.create_task(self._flusher()))
        self.tasks.append(loop.create_task(self._netwatch()))
        self.netmon.start()

    async def shutdown(self):
        self.netmon.stop()
        for t in self.tasks:
            t.cancel()
        for ied in self.ieds.values():
            if ied.state == "rodando":
                await ied.stop("aplicação encerrada")
        self.storage.close()

    async def _flusher(self):
        while True:
            await asyncio.sleep(0.5)
            try:
                self.storage.flush()
            except Exception:  # noqa: BLE001
                log.exception("falha ao gravar histórico")

    async def _netwatch(self):
        """RN-NET-16: IP/interface que some com o IED rodando leva o IED a erro."""
        while True:
            await asyncio.sleep(2.0)
            running = [i for i in self.ieds.values() if i.state == "rodando"]
            if not running:
                continue
            try:
                ifs = await asyncio.get_running_loop().run_in_executor(None, netif.list_interfaces, True)
            except Exception:  # noqa: BLE001
                continue
            ips = {a["ip"]: it for it in ifs for a in it["ips"]}
            for ied in running:
                ip = ied.cfg.get("ip")
                it = ips.get(ip)
                if it is None:
                    await ied.stop("IP removido da máquina", error=f"o IP {ip} sumiu da interface "
                                                                  f"{ied.cfg.get('interface', '?')} (RN-NET-16)")
                elif not it["ativa"]:
                    await ied.stop("interface inativa", error=f"a interface {it['nome']} ficou inativa (RN-NET-16)")

    def broadcast(self, msg: dict):
        for q in list(self.ws):
            if q.qsize() < 5000:
                q.put_nowait(msg)

    # ------------------------------------------------------------------ CID
    def read_cid(self, fname: str) -> bytes:
        return (self.cid_dir / fname).read_bytes()

    def _store_cid(self, data: bytes, original: str) -> str:
        ext = Path(original).suffix.lower() or ".cid"
        if ext not in (".cid", ".icd", ".scd", ".iid", ".xml", ".sed"):
            ext = ".cid"
        fname = f"{uuid.uuid4().hex}{ext}"
        (self.cid_dir / fname).write_bytes(data)
        return fname

    def analyze(self, data: bytes) -> dict:
        scl = parse_scl(data)
        out = []
        for ied in scl.ieds:
            b = Builder(scl, ied)
            doms = b.build()
            errors = list(b.errors)
            nds = nrc = 0
            for ld in ied.lds:
                dom = doms.get(b.ld_domain_name(ld))
                for ln in ld.lns:
                    for ds in ln.datasets:
                        nds += 1
                        for f in ds.fcdas:
                            m, why = resolve_fcda(doms, b, dom, f, ied.name) if dom else (None, "LD inválido")
                            if m is None:
                                errors.append(f"DataSet {ld.inst}/{ln.name}.{ds.name}: FCDA {f.text()} ({why})")
                    for r in ln.rcbs:
                        nrc += 1
                        if r.datset and not any(d.name == r.datset for d in ln.datasets):
                            errors.append(f"ReportControl {ld.inst}/{ln.name}.{r.name}: DataSet '{r.datset}' inexistente")
            pts = build_points(doms, b)
            out.append({
                "nome": ied.name, "fabricante": ied.manufacturer, "tipo": ied.type, "desc": ied.desc,
                "ip_cid": ied.address.get("IP", ""), "ld": [b.ld_domain_name(ld) for ld in ied.lds],
                "pontos": len(pts), "digitais": sum(1 for p in pts if p.kind == "dig"),
                "analogicos": sum(1 for p in pts if p.kind == "ana"),
                "somente_leitura": sum(1 for p in pts if p.kind == "ro"),
                "datasets": nds, "rcbs": nrc, "erros": errors, "avisos": b.warnings,
                "servicos": ied.services,
            })
        return {"edicao": scl.edition, "edicao_texto": scl.edition_label, "erros": scl.errors,
                "avisos": scl.warnings, "ieds": out}

    def upload(self, data: bytes, filename: str) -> dict:
        try:
            info = self.analyze(data)
        except SclError as e:
            raise PointError(str(e), status=422) from e
        token = secrets.token_hex(12)
        now = time.time()
        for k in [k for k, v in self.uploads.items() if now - v["ts"] > 3600]:
            del self.uploads[k]
        self.uploads[token] = {"data": data, "nome": filename, "ts": now, "info": info}
        return dict(info, token=token, arquivo=filename)

    def _take_upload(self, token: str) -> dict:
        up = self.uploads.get(token)
        if up is None:
            raise PointError("arquivo CID não encontrado ou expirado; envie de novo", status=404)
        return up

    # ------------------------------------------------------------------ IEDs
    def get(self, iid: int) -> IedRuntime:
        ied = self.ieds.get(iid)
        if ied is None:
            raise PointError(f"IED {iid} inexistente", status=404)
        return ied

    def save(self, ied: IedRuntime):
        ied.cfg["relogio"] = ied.clock.config()
        ied.cfg["falhas"] = ied.faults.config()
        self.storage.save_ied(ied.id, ied.cfg)

    def _validate_name(self, nome: str, skip: int | None = None):
        if not nome or not nome.strip():
            raise PointError("informe o nome do IED")
        for i in self.ieds.values():
            if i.id != skip and i.name == nome:
                raise PointError(f"já existe um IED com o nome '{nome}'", status=409)

    def create_ied(self, body: dict) -> IedRuntime:
        up = self._take_upload(body.get("token", ""))
        info = up["info"]
        names = [i["nome"] for i in info["ieds"]]
        ied_scl = body.get("ied_scl") or (names[0] if len(names) == 1 else None)
        if ied_scl not in names:
            raise PointError("o arquivo tem vários IEDs; escolha qual instanciar (RN-CID-02)", status=422)
        ii = next(i for i in info["ieds"] if i["nome"] == ied_scl)
        if (ii["erros"] or info["erros"]) and not body.get("aceitar_erros"):
            raise PointError("o CID tem erros de importação; confirme para subir só com o que é válido (RN-CID-09)",
                             status=422, code="erros_importacao")
        nome = body.get("nome") or ied_scl
        self._validate_name(nome)
        perfil = body.get("perfil") or ("generico-ed2" if info["edicao"] >= 2 else "generico-ed1")
        if self.profiles.get(perfil) is None:
            raise PointError(f"perfil inexistente: {perfil}", status=422)
        fname = self._store_cid(up["data"], up["nome"])
        cfg = {
            "nome": nome, "cid_arquivo": fname, "cid_nome": up["nome"], "ied_scl": ied_scl,
            "nome_ied": body.get("nome_ied") or ied_scl, "perfil": perfil,
            "perfil_sobrescritas": body.get("perfil_sobrescritas") or {},
            "interface": body.get("interface", ""), "ip": body.get("ip", ""), "porta": int(body.get("porta") or 102),
            "max_associacoes": int(body.get("max_associacoes") or 3),
            "opcoes": {"restaurar_valores": True}, "extras": {"datasets": [], "rcbs": []},
            "criado_em": time.time(), "ip_cid_real": ii.get("ip_cid", ""),
        }
        self._check_ip_cfg(cfg, None)
        iid = self.storage.add_ied(cfg)
        ied = IedRuntime(self, iid, cfg)
        self.ieds[iid] = ied
        self.save(ied)
        self.broadcast({"evento": "ied.created", "ied": iid, "dados": ied.summary()})
        return ied

    def _check_ip_cfg(self, cfg: dict, skip: int | None):
        ip = cfg.get("ip")
        if not ip:
            return
        port = int(cfg.get("porta") or 102)
        if not 1 <= port <= 65535:
            raise PointError("porta inválida")
        if ip == cfg.get("ip_cid_real"):  # preenchido na criação e a cada carga do CID
            raise PointError("o IP do CID (relé real) nunca é usado pelo simulador (RN-NET-03)")
        it = netif.iface_of(ip)
        if it is None:
            raise PointError(f"o IP {ip} não pertence a nenhuma interface desta máquina (RN-NET-03/RN-NET-12)")
        if it["loopback"] and not self.local_test:
            raise PointError("loopback só com a opção de teste local ligada (RN-NET-14)")
        if not it["ativa"]:
            raise PointError(f"interface {it['nome']} inativa (RN-NET-14)")
        if ip == self.admin_host and not it["loopback"]:
            raise PointError("este IP é o da administração web (RN-NET-08)")

    async def update_ied(self, iid: int, body: dict) -> IedRuntime:
        ied = self.get(iid)
        cfg = copy.deepcopy(ied.cfg)
        structural = False
        restart_net = False
        if "nome" in body:
            self._validate_name(body["nome"], iid)
            cfg["nome"] = body["nome"]
        if "nome_ied" in body and body["nome_ied"] != cfg.get("nome_ied"):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", body["nome_ied"] or ""):
                raise PointError("nome do IED inválido (letras, números e _; começa com letra)")
            cfg["nome_ied"] = body["nome_ied"]
            structural = True
        if "perfil" in body and body["perfil"] != cfg.get("perfil"):
            if self.profiles.get(body["perfil"]) is None:
                raise PointError(f"perfil inexistente: {body['perfil']}")
            cfg["perfil"] = body["perfil"]
            structural = True
        if "perfil_sobrescritas" in body:
            cfg["perfil_sobrescritas"] = body["perfil_sobrescritas"] or {}
            structural = True
        for k in ("interface", "ip", "porta"):
            if k in body and body[k] != cfg.get(k):
                cfg[k] = body[k]
                restart_net = True
        if "max_associacoes" in body:
            v = int(body["max_associacoes"])
            if v < 1 or v > 64:
                raise PointError("máximo de associações entre 1 e 64")
            cfg["max_associacoes"] = v
        if "opcoes" in body:
            cfg["opcoes"] = dict(cfg.get("opcoes", {}), **body["opcoes"])
        self._check_ip_cfg(cfg, iid)
        running = ied.state == "rodando"
        if running and (structural or restart_net) and not body.get("confirmar"):
            raise PointError("o IED está rodando: a mudança derruba as associações e reinicia o IED; confirme "
                             "(RN-PRF-04, RN-UI-03, RN-UI-07)", status=409, code="confirmar")
        if running and (structural or restart_net):
            await ied.stop("configuração alterada (perfil/interface)")
        ied.cfg = cfg
        if structural:
            ied.load()
        self.save(ied)
        if running and (structural or restart_net):
            await ied.start(self.admin_host, self.local_test)
        self.broadcast({"evento": "ied.state", "ied": iid, "dados": ied.summary()})
        return ied

    async def start_ied(self, iid: int):
        ied = self.get(iid)
        await ied.start(self.admin_host, self.local_test)
        self.save(ied)

    async def stop_ied(self, iid: int):
        await self.get(iid).stop("parado pelo usuário")

    def duplicate(self, iid: int, body: dict) -> IedRuntime:
        src = self.get(iid)
        cfg = copy.deepcopy(src.cfg)
        base = body.get("nome") or f"{src.name} (cópia)"
        self._validate_name(base)
        cfg["nome"] = base
        cfg["nome_ied"] = body.get("nome_ied") or cfg.get("nome_ied")
        cfg["ip"] = body.get("ip", "")
        cfg["interface"] = body.get("interface", "")
        data = self.read_cid(src.cfg["cid_arquivo"])
        cfg["cid_arquivo"] = self._store_cid(data, src.cfg.get("cid_nome", "copia.cid"))
        cfg["criado_em"] = time.time()
        self._check_ip_cfg(cfg, None)
        nid = self.storage.add_ied(cfg)
        ied = IedRuntime(self, nid, cfg)
        self.ieds[nid] = ied
        self.save(ied)
        return ied

    async def delete_ied(self, iid: int):
        if iid in self.broken:
            self.broken.pop(iid)
            self.storage.del_ied(iid)
            return
        ied = self.get(iid)
        if ied.state == "rodando":
            await ied.stop("IED removido")
        ied.logic.stop()
        self.ieds.pop(iid)
        self.storage.del_ied(iid)
        try:
            (self.cid_dir / ied.cfg["cid_arquivo"]).unlink()
        except OSError:
            pass
        self.broadcast({"evento": "ied.deleted", "ied": iid, "dados": {}})

    @staticmethod
    def _logic_refs(cfg: dict) -> list[dict]:
        """Pontos e objetos de controle usados pelas lógicas e pela configuração dos comandos."""
        out = []
        for lg in cfg.get("logicas", []):
            for b in lg.get("blocos", []):
                p = b.get("p") or {}
                campo = "ponto" if b.get("tipo") in ("entrada", "saida") else \
                    "controle" if b.get("tipo") in ("comando", "bloqueio") else None
                if campo and p.get(campo):
                    out.append({"tipo": campo, "ref": p[campo], "logica": lg.get("nome", ""), "bloco": b.get("id")})
        for k in cfg.get("controles", {}):
            out.append({"tipo": "controle", "ref": k, "logica": None, "bloco": None})
        return out

    @staticmethod
    def _suggest(old, cands: list, attr: str) -> str | None:
        """Equivalente no CID novo de um ponto/objeto que sumiu: mesmo DO e FC num LN da mesma classe (prefixo,
        instância ou LD mudaram) ou mesma descrição e CDC. `cands` são só os que não existiam no CID antigo.
        Só sugere quando há um único melhor candidato."""
        def cls(ln):
            m = re.match(r"^[A-Za-z0-9_]*?(LLN0|[A-Z]{4})(\d*)$", ln)
            return m.group(1) if m else ln
        best, score = [], 0
        for c in cands:
            if c.cdc != old.cdc and old.cdc and c.cdc:
                continue
            s = 0
            if c.do == old.do and getattr(c, "fc", None) == getattr(old, "fc", None):
                s = 4 if c.ln == old.ln else 3 if cls(c.ln) == cls(old.ln) else 0
            if old.desc and c.desc == old.desc:
                s += 2
            if s > score:
                best, score = [c], s
            elif s == score and s:
                best.append(c)
        return getattr(best[0], attr) if score >= 2 and len(best) == 1 else None

    def _reimport_preview(self, ied: IedRuntime, up: dict, target: str) -> dict:
        """Monta o modelo do CID novo à parte (sem mexer no IED) e diz o que as lógicas e comandos perdem."""
        cfg = copy.deepcopy(ied.cfg)
        cfg.update(cid_arquivo=self._store_cid(up["data"], up["nome"]), ied_scl=target, logicas=[])
        try:
            novo = IedRuntime(self, ied.id, cfg)
        except Exception as e:
            raise PointError(f"CID novo inválido: {e}", status=422) from e
        finally:
            (self.cid_dir / cfg["cid_arquivo"]).unlink(missing_ok=True)
        old_paths, new_paths = set(ied.by_path), set(novo.by_path)
        pend: dict[tuple, dict] = {}
        for r in self._logic_refs(ied.cfg):
            novos = novo.by_path if r["tipo"] == "ponto" else novo.control.objs
            if r["ref"] in novos:
                continue
            k = (r["tipo"], r["ref"])
            if k not in pend:
                velho = (ied.by_path if r["tipo"] == "ponto" else ied.control.objs).get(r["ref"])
                attr = "path" if r["tipo"] == "ponto" else "key"
                antigos = ied.by_path if r["tipo"] == "ponto" else ied.control.objs
                cands = [c for k2, c in novos.items() if k2 not in antigos]  # o renomeado está entre os novos
                pend[k] = {"tipo": r["tipo"], "ref": r["ref"], "usos": [],
                           "cdc": velho.cdc if velho else "", "desc": velho.desc if velho else "",
                           "sugestao": self._suggest(velho, cands, attr) if velho else None,
                           "candidatos": [getattr(c, attr) for c in novos.values()
                                          if not velho or not velho.cdc or c.cdc == velho.cdc][:500]}
            if r["logica"] is not None:
                pend[k]["usos"].append({"logica": r["logica"], "bloco": r["bloco"]})
            else:
                pend[k]["usos"].append({"logica": None, "bloco": "configuração do comando"})
        return {"novo": novo, "mantidos": len(old_paths & new_paths), "removidos": sorted(old_paths - new_paths),
                "novos": sorted(new_paths - old_paths), "referencias": list(pend.values()),
                "logicas": len(ied.cfg.get("logicas", [])), "erros": novo.errors, "avisos": novo.warnings}

    def _remap(self, ied: IedRuntime, mapa: dict, novo: IedRuntime):
        """Reescreve nas lógicas, na configuração dos comandos e no estado salvo as referências de `mapa`."""
        for de, para in mapa.items():
            if para not in novo.by_path and para not in novo.control.objs:
                raise PointError(f"'{para}' não existe no CID novo", status=422)
        for lg in ied.cfg.get("logicas", []):
            for b in lg.get("blocos", []):
                p = b.get("p") or {}
                for campo in ("ponto", "controle"):
                    if p.get(campo) in mapa:
                        p[campo] = mapa[p[campo]]
        ctl = ied.cfg.get("controles", {})
        for de, para in mapa.items():
            if de in ctl:
                ctl[para] = dict(ctl.get(para, {}), **ctl.pop(de))
        self.storage.rename_points(ied.id, {de: para for de, para in mapa.items() if para in novo.by_path})

    async def reimport(self, iid: int, body: dict) -> dict:
        """RN-CID-08: mantém o estado dos pontos que continuam existindo e lista os que sumiram e os novos.
        As lógicas e a configuração dos comandos ficam no IED; `simular` só devolve a prévia (referências que
        sumiriam, com sugestão de equivalente) e `mapa` ({antigo: novo}) as reescreve antes de carregar."""
        ied = self.get(iid)
        up = self._take_upload(body.get("token", ""))
        names = [i["nome"] for i in up["info"]["ieds"]]
        target = body.get("ied_scl") or (ied.cfg["ied_scl"] if ied.cfg["ied_scl"] in names else
                                         (names[0] if len(names) == 1 else None))
        if target not in names:
            raise PointError("escolha qual IED do arquivo reimportar", status=422)
        mapa = {k: v for k, v in (body.get("mapa") or {}).items() if v and k != v}
        if body.get("simular") or mapa:
            prev = self._reimport_preview(ied, up, target)
            novo = prev.pop("novo")
            if body.get("simular"):
                return prev
        if ied.state == "rodando" and not body.get("confirmar"):
            raise PointError("o IED está rodando: reimportar derruba as associações; confirme (RN-UI-03)",
                             status=409, code="confirmar")
        old_paths = {p.path for p in ied.points}
        running = ied.state == "rodando"
        if running:
            await ied.stop("CID reimportado")
        self.storage.flush()
        old_cfg = copy.deepcopy(ied.cfg)
        if mapa:
            self._remap(ied, mapa, novo)
        old_file = ied.cfg["cid_arquivo"]
        ied.cfg["cid_arquivo"] = self._store_cid(up["data"], up["nome"])
        ied.cfg["cid_nome"] = up["nome"]
        ied.cfg["ied_scl"] = target
        try:
            ied.load()
        except Exception as e:
            (self.cid_dir / ied.cfg["cid_arquivo"]).unlink(missing_ok=True)
            if mapa:
                self.storage.rename_points(ied.id, {v: k for k, v in mapa.items() if v in novo.by_path})
            ied.cfg = old_cfg
            ied.load()
            raise PointError(f"CID novo inválido: {e}", status=422) from e
        try:
            (self.cid_dir / old_file).unlink()
        except OSError:
            pass
        self.save(ied)
        new_paths = {p.path for p in ied.points}
        if running:
            await ied.start(self.admin_host, self.local_test)
        orfas = [r for r in self._logic_refs(ied.cfg)
                 if r["ref"] not in (ied.by_path if r["tipo"] == "ponto" else ied.control.objs)]
        return {"mantidos": len(old_paths & new_paths), "removidos": sorted(old_paths - new_paths),
                "novos": sorted(new_paths - old_paths), "erros": ied.errors, "avisos": ied.warnings,
                "logicas": len(ied.cfg.get("logicas", [])), "remapeados": len(mapa), "orfas": orfas}

    def export_cid(self, iid: int) -> tuple[bytes, str]:
        ied = self.get(iid)
        data = self.read_cid(ied.cfg["cid_arquivo"])
        ex = ied.cfg.get("extras", {})
        out = export_cid(data, ied.cfg["ied_scl"], ex.get("datasets", []), ex.get("rcbs", []))
        base = Path(ied.cfg.get("cid_nome", "ied.cid")).stem
        return out, f"{base}_com_extras.cid"

    # ------------------------------------------------------------------ extras (RN-CID-06)
    async def _structural(self, ied: IedRuntime, confirm: bool, why: str):
        if ied.state == "rodando" and not confirm:
            raise PointError(f"o IED está rodando: {why} reinicia o IED e derruba as associações; confirme",
                             status=409, code="confirmar")

    async def add_extra_dataset(self, iid: int, body: dict) -> dict:
        ied = self.get(iid)
        await self._structural(ied, body.get("confirmar"), "criar DataSet")
        name = body.get("nome", "")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", name):
            raise PointError("nome de DataSet inválido")
        dom = next((d for d in ied.domains.values() if d.inst == body.get("ld") or d.name == body.get("ld")), None)
        if dom is None:
            raise PointError("LD inexistente")
        ln = body.get("ln") or "LLN0"
        if ln not in dom.lns:
            raise PointError(f"LN {ln} inexistente em {dom.name}")
        if f"{ln}${name}" in dom.datasets and not body.get("substituir"):
            raise PointError("já existe DataSet com esse nome", status=409)
        maxds = ied.profile["rcb"].get("max_datasets")
        nds = sum(len(d.datasets) for d in ied.domains.values())
        if maxds and nds >= maxds and not body.get("substituir"):
            raise PointError(f"limite de DataSets do perfil/CID atingido ({maxds})", status=409)
        fcdas = []
        for m in body.get("membros", []):
            p = ied.find_point(m.get("ponto") if isinstance(m, dict) else m)
            fc = (m.get("fc") if isinstance(m, dict) else None) or p.fc
            ld_inst, prefix, ln_class, ln_inst = ied.ln_parts[(p.domain, p.ln)]
            da = (m.get("da") if isinstance(m, dict) else "") or ""
            fcdas.append({"ld_inst": ld_inst, "prefix": prefix, "ln_class": ln_class, "ln_inst": ln_inst,
                          "do_name": p.do.replace("$", "."), "da_name": da, "fc": fc})
        if not fcdas:
            raise PointError("informe ao menos um membro")
        ex = ied.cfg.setdefault("extras", {"datasets": [], "rcbs": []})
        ex["datasets"] = [d for d in ex["datasets"] if not (d["ld"] == dom.inst and d.get("ln") == ln and d["name"] == name)]
        ex["datasets"].append({"ld": dom.inst, "ln": ln, "name": name, "desc": body.get("desc", "extra de teste"),
                               "fcdas": fcdas})
        # RN-RCB-13: alterar o DataSet em teste incrementa o ConfRev dos RCBs que o usam
        bump = {r.base for r in ied.engine.rcbs if r.datset == f"{dom.name}/{ln}${name}"}
        for r in ex.get("rcbs", []):
            if r["name"] in bump or (r["ld"] == dom.inst and r.get("ln") == ln and r["datset"] == name):
                r["conf_rev"] = int(r.get("conf_rev", 1)) + 1
        await self._reload(ied)
        return {"ok": True, "erros": ied.errors}

    async def del_extra_dataset(self, iid: int, ld: str, ln: str, name: str, confirm: bool):
        ied = self.get(iid)
        await self._structural(ied, confirm, "remover DataSet")
        ex = ied.cfg.setdefault("extras", {"datasets": [], "rcbs": []})
        n = len(ex["datasets"])
        ex["datasets"] = [d for d in ex["datasets"] if not (d["ld"] == ld and d.get("ln", "LLN0") == ln and d["name"] == name)]
        if len(ex["datasets"]) == n:
            raise PointError("DataSet extra inexistente", status=404)
        await self._reload(ied)

    async def add_extra_rcb(self, iid: int, body: dict) -> dict:
        ied = self.get(iid)
        await self._structural(ied, body.get("confirmar"), "criar RCB")
        name = body.get("nome", "")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,29}", name):
            raise PointError("nome de RCB inválido")
        dom = next((d for d in ied.domains.values() if d.inst == body.get("ld") or d.name == body.get("ld")), None)
        if dom is None:
            raise PointError("LD inexistente")
        ln = body.get("ln") or "LLN0"
        if ied.profile["rcb"].get("posicao") == "LLN0" and not ln.endswith("LLN0"):
            raise PointError("o perfil exige RCBs só no LLN0", status=409)
        if f"{ln}${body.get('datset', '')}" not in dom.datasets:
            raise PointError("DataSet do RCB deve existir no mesmo LN")
        maxn = int(body.get("max", 3))
        if not 1 <= maxn <= 99:
            raise PointError("RptEnabled max entre 1 e 99")
        maxr = ied.profile["rcb"].get("max_rcbs")
        if maxr and len(ied.engine.rcbs) + maxn > maxr:
            raise PointError(f"limite de RCBs do perfil/CID ({maxr}) seria excedido", status=409)
        ex = ied.cfg.setdefault("extras", {"datasets": [], "rcbs": []})
        if any(r["name"] == name and r["ld"] == dom.inst and r.get("ln") == ln for r in ex["rcbs"]):
            raise PointError("já existe RCB extra com esse nome", status=409)
        trg = body.get("trgops") or {"dchg": True, "qchg": True, "gi": True}
        opt = body.get("optflds") or {"seqNum": True, "timeStamp": True, "dataSet": True, "reasonCode": True,
                                      "entryID": True, "configRef": True}
        ex["rcbs"].append({"ld": dom.inst, "ln": ln, "name": name, "datset": body["datset"],
                           "rptid": body.get("rptid", ""), "buffered": bool(body.get("buffered")),
                           "buf_time": int(body.get("buf_time", 0)), "intg_pd": int(body.get("intg_pd", 0)),
                           "trgops": trg, "optflds": opt, "max": maxn, "conf_rev": int(body.get("conf_rev", 1)),
                           "desc": body.get("desc", "extra de teste")})
        await self._reload(ied)
        return {"ok": True, "avisos": ied.warnings}

    async def del_extra_rcb(self, iid: int, ld: str, ln: str, name: str, confirm: bool):
        ied = self.get(iid)
        await self._structural(ied, confirm, "remover RCB")
        ex = ied.cfg.setdefault("extras", {"datasets": [], "rcbs": []})
        n = len(ex["rcbs"])
        ex["rcbs"] = [r for r in ex["rcbs"] if not (r["ld"] == ld and r.get("ln", "LLN0") == ln and r["name"] == name)]
        if len(ex["rcbs"]) == n:
            raise PointError("RCB extra inexistente", status=404)
        await self._reload(ied)

    async def _reload(self, ied: IedRuntime):
        running = ied.state == "rodando"
        if running:
            await ied.stop("modelo alterado (extras)")
        self.storage.flush()
        ied.load()
        self.save(ied)
        if running:
            await ied.start(self.admin_host, self.local_test)

    # ------------------------------------------------------------------ política de acesso por RCB
    def _rcbs(self, ied: IedRuntime, nome: str) -> list:
        rcbs = ied.rcbs_named(nome)
        if not rcbs:
            raise PointError(f"RCB '{nome}' inexistente no IED {ied.name}", status=404)
        return rcbs

    def rcb_policy(self, iid: int, nome: str) -> dict:
        ied = self.get(iid)
        base = self._rcbs(ied, nome)[0].base
        eff, orig = rcb_policy_effective(ied.profile, ied.cfg, base)
        return dict(eff, rcb=base, perfil=ied.profile.get("id"), origem=orig, instancias=[
            {"nome": r.name, "ref": r.ref, "tipo": "BRCB" if r.buffered else "URCB", "estado": r.policy.politica,
             "habilitado": r.ena, "cliente": r.owner.peer_str if r.owner is not None else None,
             "recusadas": r.denied} for r in ied.engine.rcbs if r.base == base])

    async def set_rcb_policy(self, iid: int, nome: str, body: dict, admin: bool = True) -> dict:
        """Muda a política com o IED rodando (vale para as próximas escritas); 'instanciar' muda o modelo e
        reinicia o IED. 'restaurar_perfil' apaga a sobrescrita do IED; 'derrubar' desabilita o RCB na hora."""
        ied = self.get(iid)
        if not isinstance(body, dict):
            raise PointError("corpo deve ser um objeto JSON")
        extra = set(body) - set(RCB_POLICY_KEYS) - {"derrubar", "confirmar", "restaurar_perfil"}
        if extra:
            raise PointError(f"campos desconhecidos {sorted(extra)}; use {list(RCB_POLICY_KEYS)}, derrubar, "
                             f"restaurar_perfil ou confirmar", status=422)
        base = self._rcbs(ied, nome)[0].base
        try:
            entry = rcb_policy_validate({k: v for k, v in body.items() if k in RCB_POLICY_KEYS}, base)
        except ValueError as e:
            raise PointError(str(e), status=422) from e
        old, _ = rcb_policy_effective(ied.profile, ied.cfg, base)
        cur = {} if body.get("restaurar_perfil") else dict((ied.cfg.get("rcb_politicas") or {}).get(base) or {})
        cur.update(entry)
        pols = dict(ied.cfg.get("rcb_politicas") or {})
        if cur:
            pols[base] = cur
        else:
            pols.pop(base, None)
        new, _ = rcb_policy_effective(ied.profile, dict(ied.cfg, rcb_politicas=pols), base)
        structural = new["instanciar"] != old["instanciar"]
        if structural and not admin:
            raise PointError("só o perfil administrador muda 'instanciar' (altera o modelo do IED)", status=403)
        if structural:
            await self._structural(ied, body.get("confirmar"), "mudar 'instanciar'")
        ied.cfg["rcb_politicas"] = pols
        if structural:
            await self._reload(ied)
        else:
            for r in ied.engine.rcbs:
                if r.base == base:
                    r.policy.update(new)
                    ied.emit("rcb.changed", r.state())
            self.save(ied)
        ied.mms_event(None, "Política de RCB", f"{base}: {new['politica']}, atributos {new['atributos'] or '{}'}, "
                                              f"IPs {new['ips'] or 'todos'}, ocultar={new['ocultar']}, "
                                              f"instanciar={new['instanciar']}", "configuração")
        if body.get("derrubar"):
            self.disable_rcb_client(iid, base)
        return self.rcb_policy(iid, base)

    async def set_rcb_policy_bulk(self, iid: int, body: dict) -> dict:
        """Mesma mudança em vários RCBs de uma vez (ex.: deixar todos livres ou ocupados). Tudo é validado antes
        de aplicar: um nome inexistente ou um valor inválido não altera nenhum RCB. 'instanciar' não vale em
        lote porque reinicia o IED a cada RCB."""
        ied = self.get(iid)
        if not isinstance(body, dict):
            raise PointError("corpo deve ser um objeto JSON")
        nomes = body.get("rcbs")
        if not isinstance(nomes, list) or not nomes or not all(isinstance(n, str) and n for n in nomes):
            raise PointError("informe 'rcbs': lista com os nomes dos RCBs", status=422)
        if "instanciar" in body:
            raise PointError("'instanciar' não pode ser mudado em lote (reinicia o IED); altere RCB a RCB", status=422)
        bases = []
        missing = []
        for n in nomes:
            rs = ied.rcbs_named(n)
            if not rs:
                missing.append(n)
            elif rs[0].base not in bases:
                bases.append(rs[0].base)
        if missing:
            raise PointError(f"RCB(s) inexistente(s) no IED {ied.name}: {', '.join(missing)}; nada foi alterado",
                             status=404)
        upd = {k: v for k, v in body.items() if k != "rcbs"}
        extra = set(upd) - set(RCB_POLICY_KEYS) - {"derrubar", "restaurar_perfil"}
        if extra:
            raise PointError(f"campos desconhecidos {sorted(extra)}", status=422)
        try:
            rcb_policy_validate({k: v for k, v in upd.items() if k in RCB_POLICY_KEYS}, "lote")
        except ValueError as e:
            raise PointError(str(e), status=422) from e
        if not set(upd) & (set(RCB_POLICY_KEYS) | {"derrubar", "restaurar_perfil"}):
            raise PointError("nada para alterar: informe politica, atributos, ips, ocultar, restaurar_perfil ou derrubar",
                             status=422)
        return {"rcbs": [await self.set_rcb_policy(iid, b, upd) for b in bases]}

    def disable_rcb_client(self, iid: int, nome: str) -> dict:
        """Desabilita na hora o RCB que um cliente habilitou (e libera a reserva)."""
        ied = self.get(iid)
        out = []
        for r in self._rcbs(ied, nome):
            if r.ena or r.reserved_by is not None:
                cli = (r.owner or r.reserved_by).peer_str
                if r.ena:
                    r.disable("derrubado pelo usuário")
                r.reserved_by = None
                r.resvtms = 0
                out.append({"rcb": r.ref, "cliente": cli})
                ied.mms_event(None, "Política de RCB", f"{r.ref}: habilitação de {cli} derrubada", "derrubado")
                ied.emit("rcb.changed", r.state())
        return {"derrubados": out}

    # ------------------------------------------------------------------ cenários
    def scenario_from_ied(self, iid: int, nome: str, kinds=("dig", "ana")) -> dict:
        ied = self.get(iid)
        steps = [{"ponto": p.path, "valor": p.value(), "atraso_ms": 0} for p in ied.points if p.kind in kinds]
        return {"nome": nome, "ied_origem": ied.name, "passos": steps}

    def apply_scenario(self, sid: int, iid: int) -> dict:
        sc = self.storage.get_json("scenario", sid)
        if sc is None:
            raise PointError("cenário inexistente", status=404)
        ied = self.get(iid)
        steps = sc.get("passos", [])
        groups: list[tuple[int, list]] = []
        for s in steps:
            delay = int(s.get("atraso_ms") or 0)
            op = {"path": s["ponto"]}
            if "valor" in s and s["valor"] is not None:
                op["valor"] = s["valor"]
            if s.get("qualidade"):
                op["qualidade"] = s["qualidade"]
            op["confirmar_fora_faixa"] = True
            if delay > 0 or not groups:
                groups.append((delay, [op]))
            else:
                groups[-1][1].append(op)
        # valida antes de começar
        for _, ops in groups:
            for op in ops:
                ied.find_point(op["path"])

        async def run():
            for delay, ops in groups:
                if delay:
                    await asyncio.sleep(delay / 1000.0)
                try:
                    ied.set_points(ops, origin=f"cenario:{sc.get('nome', sid)}")
                except PointError as e:
                    ied.emit("scenario.error", {"cenario": sid, "erro": str(e), "caminho": e.path})
            ied.emit("scenario.done", {"cenario": sid})

        old = self.scenario_tasks.get(iid)
        if old and not old.done():
            old.cancel()
        self.scenario_tasks[iid] = asyncio.get_running_loop().create_task(run())
        return {"passos": len(steps), "grupos": len(groups)}

    # ------------------------------------------------------------------ parada rápida (RN-SEG-05)
    async def panic(self) -> dict:
        out = []
        for t in self.scenario_tasks.values():
            t.cancel()
        for ied in self.ieds.values():
            if ied.state == "rodando":
                await ied.stop("PARADA RÁPIDA")
                n = ied.restore_snapshot()
                out.append({"ied": ied.name, "pontos_restaurados": n})
        self.storage.flush()
        self.broadcast({"evento": "panic", "ied": 0, "dados": out})
        return {"parados": out}

    def interfaces(self) -> list[dict]:
        ifs = netif.list_interfaces(self.local_test)
        for it in ifs:
            ips = {a["ip"] for a in it["ips"]}
            it["ieds"] = [{"id": i.id, "nome": i.name, "ip": i.cfg.get("ip"), "estado": i.state}
                          for i in self.ieds.values() if i.cfg.get("ip") in ips]
            it["admin"] = self.admin_host in ips
        return ifs

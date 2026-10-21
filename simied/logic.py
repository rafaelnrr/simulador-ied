"""Lógicas do relé: diagrama de blocos (como um CLP) que lê pontos e comandos e escreve pontos e bloqueios.

Cada lógica é guardada na configuração do IED:
    {"id", "nome", "ativa", "blocos": [{"id", "tipo", "x", "y", "p": {parâmetros}}],
     "fios": [{"de": bloco, "saida": porta, "para": bloco, "entrada": porta}]}

O motor varre as lógicas ativas a cada 50 ms (e na hora em que um ponto muda ou chega um comando). Sinais são
booleanos (ou números, nas entradas analógicas). Saídas escrevem só na mudança do sinal (borda), para não brigar
com quem mexe no ponto à mão; na primeira varredura só memorizam o estado.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("simied.logic")

SCAN = 0.05
DIG = {"SPS", "SPC", "ACT", "ACD"}
DBL = {"DPS", "DPC"}

# Catálogo de blocos (a interface usa os mesmos nomes de portas).
BLOCOS = {
    "entrada": {"grupo": "Entradas", "nome": "Ponto (entrada)", "desc": "Lê o estado de um ponto do IED"},
    "comando": {"grupo": "Entradas", "nome": "Comando recebido", "desc": "Pulso quando o SAGE (ou a tela) comanda o objeto: Close = ctlVal 1 (on/true), Trip = ctlVal 0 (off/false). Dá para usar só um dos sinais (comandos que só mandam Trip ou só Close). Com este bloco a lógica decide o que acontece; o estado do próprio objeto (ex.: CSWI.Pos) só deixa de acompanhar o comando se alguma lógica escrever nele com um bloco Ponto (saída)"},
    "const": {"grupo": "Entradas", "nome": "Constante", "desc": "Valor fixo (verdadeiro/falso ou número)"},
    "and": {"grupo": "Lógica", "nome": "E (AND)", "desc": "Verdadeiro quando todas as entradas são verdadeiras"},
    "or": {"grupo": "Lógica", "nome": "OU (OR)", "desc": "Verdadeiro quando alguma entrada é verdadeira"},
    "xor": {"grupo": "Lógica", "nome": "OU exclusivo (XOR)", "desc": "Verdadeiro quando um número ímpar de entradas é verdadeiro"},
    "not": {"grupo": "Lógica", "nome": "NÃO (NOT)", "desc": "Inverte o sinal"},
    "ton": {"grupo": "Tempo e memória", "nome": "Atraso para ligar", "desc": "A saída liga depois que a entrada fica ligada pelo tempo ajustado (TON)"},
    "tof": {"grupo": "Tempo e memória", "nome": "Atraso para desligar", "desc": "A saída desliga depois que a entrada fica desligada pelo tempo ajustado (TOF)"},
    "pulso": {"grupo": "Tempo e memória", "nome": "Pulso", "desc": "Na subida da entrada, liga a saída pelo tempo ajustado"},
    "sr": {"grupo": "Tempo e memória", "nome": "Memória (SR)", "desc": "S liga e mantém; R desliga (prioridade ajustável)"},
    "comparar": {"grupo": "Lógica", "nome": "Comparar", "desc": "Compara um valor analógico com um limite"},
    "saida": {"grupo": "Saídas", "nome": "Ponto (saída)", "desc": "Escreve no ponto quando o sinal muda. Ponto duplo: entrada fechar/abrir, com tempo de manobra opcional"},
    "bloqueio": {"grupo": "Saídas", "nome": "Bloqueio de comando", "desc": "Enquanto o sinal estiver ligado, recusa o comando do objeto (intertravamento, sincronismo ou processo)"},
}


def _cdc_of(ied, path):
    p = ied.by_path.get(path or "")
    return p.cdc if p is not None else ""


def ports(ied, b: dict) -> tuple[list, list]:
    t, p = b.get("tipo"), b.get("p") or {}
    if t == "entrada":
        c = _cdc_of(ied, p.get("ponto"))
        return [], (["fechado", "aberto"] if c in DBL else ["q"] if c in DIG else ["v"])
    if t == "comando":
        o = ied.control.objs.get(p.get("controle") or "")
        if o is not None and o.cdc not in DBL | DIG:
            return [], ["rec"]
        sinais = p.get("sinais") or "ambos"  # comandos que só mandam Close (on) ou só Trip (off)
        return [], [s for s in ("on", "off") if sinais in ("ambos", s)]
    if t == "const":
        return [], ["q"]
    if t in ("and", "or", "xor"):
        n = max(2, min(8, int(p.get("n") or 2)))
        return [f"in{i}" for i in range(1, n + 1)], ["q"]
    if t in ("not", "ton", "tof", "pulso"):
        return ["a"], ["q"]
    if t == "sr":
        return ["s", "r"], ["q"]
    if t == "comparar":
        return ["v"], ["q"]
    if t == "saida":
        c = _cdc_of(ied, p.get("ponto"))
        return (["on", "off"] if c in DBL else ["a"] if c in DIG else ["v"]), []
    if t == "bloqueio":
        return ["a"], []
    return [], []


def validate(ied, lg: dict) -> tuple[dict, list]:
    """Normaliza uma lógica vinda da interface; devolve (lógica, avisos). Erros de estrutura levantam ValueError."""
    if not isinstance(lg, dict):
        raise ValueError("lógica deve ser um objeto")
    nome = str(lg.get("nome") or "").strip() or "Lógica"
    blocos, ids, avisos = [], set(), []
    for b in lg.get("blocos") or []:
        t = b.get("tipo")
        if t not in BLOCOS:
            raise ValueError(f"bloco de tipo desconhecido: {t}")
        bid = str(b.get("id") or "")
        if not bid or bid in ids:
            raise ValueError(f"id de bloco vazio ou repetido: {bid!r}")
        ids.add(bid)
        p = dict(b.get("p") or {})
        if t in ("entrada", "saida") and p.get("ponto") and p["ponto"] not in ied.by_path:
            avisos.append(f"{BLOCOS[t]['nome']} {bid}: ponto {p['ponto']} não existe neste IED")
        if t in ("entrada", "saida") and not p.get("ponto"):
            avisos.append(f"{BLOCOS[t]['nome']} {bid}: escolha o ponto")
        if t in ("comando", "bloqueio") and p.get("controle") not in ied.control.objs:
            avisos.append(f"{BLOCOS[t]['nome']} {bid}: escolha um objeto de controle existente")
        for k in ("ms", "tempo_ms"):
            if k in p:
                try:
                    p[k] = max(0, int(float(p[k] or 0)))
                except (TypeError, ValueError):
                    raise ValueError(f"bloco {bid}: {k} deve ser número de milissegundos") from None
        blocos.append({"id": bid, "tipo": t, "x": float(b.get("x") or 0), "y": float(b.get("y") or 0), "p": p})
    fios = []
    by_id = {b["id"]: b for b in blocos}
    usados = set()
    for f in lg.get("fios") or []:
        a, z = by_id.get(f.get("de")), by_id.get(f.get("para"))
        if a is None or z is None:
            continue
        if f.get("saida") not in ports(ied, a)[1] or f.get("entrada") not in ports(ied, z)[0]:
            continue  # porta deixou de existir (trocou o ponto): o fio cai
        k = (z["id"], f["entrada"])
        if k in usados:
            raise ValueError(f"a entrada {f['entrada']} do bloco {z['id']} tem mais de um fio")
        usados.add(k)
        fios.append({"de": a["id"], "saida": f["saida"], "para": z["id"], "entrada": f["entrada"]})
    return {"id": str(lg.get("id") or ""), "nome": nome, "ativa": bool(lg.get("ativa", True)),
            "blocos": blocos, "fios": fios}, avisos


class _Compiled:
    def __init__(self, ied, lg: dict):
        self.cfg = lg
        self.blocks = {b["id"]: b for b in lg["blocos"]}
        self.src = {(f["para"], f["entrada"]): (f["de"], f["saida"]) for f in lg["fios"]}
        self.out: dict[str, dict] = {bid: {} for bid in self.blocks}
        self.st: dict[str, dict] = {bid: {} for bid in self.blocks}
        self.errors: dict[str, str] = {}
        self.ports = {bid: ports(ied, b) for bid, b in self.blocks.items()}
        # ordem topológica (ciclos usam o valor da varredura anterior)
        deps = {bid: {self.src[k][0] for k in self.src if k[0] == bid} for bid in self.blocks}
        order, done = [], set()
        while len(order) < len(self.blocks):
            ready = [b for b in self.blocks if b not in done and deps[b] <= done]
            if not ready:
                ready = [next(b for b in self.blocks if b not in done)]
            for b in ready:
                order.append(b)
                done.add(b)
        self.order = order

    def inp(self, bid, port, default=False):
        s = self.src.get((bid, port))
        if s is None:
            return default
        return self.out.get(s[0], {}).get(s[1], default)


class LogicEngine:
    def __init__(self, ied):
        self.ied = ied
        self.logics: list[_Compiled] = []
        self.pending: dict[str, list] = {}
        self.task = None
        self.event: asyncio.Event | None = None
        self.scans = 0

    # ------------------------------------------------------------------ configuração
    def load(self, lst: list):
        out = []
        for lg in lst or []:
            try:
                norm, _ = validate(self.ied, lg)
                out.append(_Compiled(self.ied, norm))
            except ValueError as e:
                log.warning("lógica %s ignorada: %s", lg.get("nome"), e)
        self.logics = out
        self.ied.control.blocks = {}
        self.kick()

    def command_refs(self) -> set:
        return {b["p"].get("controle") for lg in self.logics if lg.cfg["ativa"]
                for b in lg.blocks.values() if b["tipo"] == "comando"}

    def writes_point(self, path: str) -> bool:
        """Alguma lógica ativa escreve neste ponto (bloco Ponto (saída))?"""
        return any(b["tipo"] == "saida" and b["p"].get("ponto") == path
                   for lg in self.logics if lg.cfg["ativa"] for b in lg.blocks.values())

    def command(self, o, ctl, rec: dict):
        self.pending.setdefault(o.key, []).append(ctl)
        rec["logicas"] = [lg.cfg["nome"] for lg in self.logics if lg.cfg["ativa"] and any(
            b["tipo"] == "comando" and b["p"].get("controle") == o.key for b in lg.blocks.values())]
        self.kick()  # a varredura sai logo depois da resposta do Write (o pulso vale para essa varredura)

    # ------------------------------------------------------------------ laço
    def kick(self):
        if not any(lg.cfg["ativa"] for lg in self.logics):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self.event is None:
            self.event = asyncio.Event()
        if self.task is None or self.task.done():
            self.task = loop.create_task(self._run())
        self.event.set()

    async def _run(self):
        try:
            while any(lg.cfg["ativa"] for lg in self.logics):
                try:
                    await asyncio.wait_for(self.event.wait(), SCAN)
                except asyncio.TimeoutError:
                    pass
                self.event.clear()
                self.scan()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("motor de lógicas do IED %s parou", self.ied.id)

    def stop(self):
        if self.task is not None:
            self.task.cancel()
            self.task = None

    # ------------------------------------------------------------------ varredura
    def scan(self):
        now = time.monotonic()
        blocks: dict[str, list] = {}
        writes = []
        pend, self.pending = self.pending, {}
        for lg in self.logics:
            if not lg.cfg["ativa"]:
                continue
            for bid in lg.order:
                try:
                    self._eval(lg, lg.blocks[bid], now, pend, blocks, writes)
                    lg.errors.pop(bid, None)
                except Exception as e:  # noqa: BLE001
                    lg.errors[bid] = str(e)
        self.ied.control.blocks = blocks
        self.scans += 1
        for lg, bid, fn in writes:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                lg.errors[bid] = str(e)

    def _eval(self, lg: _Compiled, b: dict, now: float, pend: dict, blocks: dict, writes: list):
        t, p, bid = b["tipo"], b["p"], b["id"]
        out, st = lg.out[bid], lg.st[bid]
        ins, _ = lg.ports[bid]
        ied = self.ied
        if t == "entrada":
            pt = ied.by_path.get(p.get("ponto") or "")
            if pt is None:
                raise ValueError("ponto não encontrado")
            v = pt.value()
            if pt.cdc in DBL:
                out["fechado"], out["aberto"] = v == 2, v == 1
            elif pt.cdc in DIG:
                out["q"] = bool(v)
            else:
                out["v"] = float(v["mag"] if isinstance(v, dict) else v or 0)
        elif t == "comando":
            cmds = pend.get(p.get("controle"), [])
            out["on"] = any(c is True for c in cmds)
            out["off"] = any(c is False for c in cmds)
            out["rec"] = bool(cmds)
            if cmds:
                st["ultimo"] = {"ts": time.time(), "valor": cmds[-1]}
        elif t == "const":
            v = p.get("valor", False)
            out["q"] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else v in (True, "true", "1", 1)
        elif t in ("and", "or", "xor"):
            vals = [bool(lg.inp(bid, i)) for i in ins]
            out["q"] = all(vals) if t == "and" else any(vals) if t == "or" else sum(vals) % 2 == 1
        elif t == "not":
            out["q"] = not lg.inp(bid, "a")
        elif t == "ton":
            if lg.inp(bid, "a"):
                st.setdefault("t0", now)
                out["q"] = (now - st["t0"]) * 1000 >= int(p.get("ms") or 0)
            else:
                st.pop("t0", None)
                out["q"] = False
        elif t == "tof":
            if lg.inp(bid, "a"):
                st.pop("t1", None)
                out["q"] = True
            elif out.get("q"):
                st.setdefault("t1", now)
                out["q"] = (now - st["t1"]) * 1000 < int(p.get("ms") or 0)
            else:
                out["q"] = False
        elif t == "pulso":
            a = bool(lg.inp(bid, "a"))
            if a and not st.get("prev"):
                st["ate"] = now + int(p.get("ms") or 0) / 1000.0
            st["prev"] = a
            out["q"] = now < st.get("ate", 0)
        elif t == "sr":
            s, r = bool(lg.inp(bid, "s")), bool(lg.inp(bid, "r"))
            q = out.get("q", False)
            if p.get("prioridade") == "set":
                q = True if s else False if r else q
            else:
                q = False if r else True if s else q
            out["q"] = q
        elif t == "comparar":
            v = float(lg.inp(bid, "v", 0.0) or 0.0)
            lim = float(p.get("limite") or 0)
            op = p.get("op") or ">"
            out["q"] = {">": v > lim, ">=": v >= lim, "<": v < lim, "<=": v <= lim, "==": v == lim,
                        "!=": v != lim}.get(op, False)
        elif t == "saida":
            pt = ied.by_path.get(p.get("ponto") or "")
            if pt is None:
                raise ValueError("ponto não encontrado")
            cur = {i: lg.inp(bid, i, False if i != "v" else 0.0) for i in ins}
            prev = st.get("prev")
            st["prev"] = cur
            if prev is None:
                return  # primeira varredura: só memoriza
            origin = f"lógica “{lg.cfg['nome']}”"
            if pt.cdc in DBL:
                for port, val in (("on", 2), ("off", 1)):
                    if cur[port] and not prev.get(port):
                        writes.append((lg, bid, self._dbl_writer(pt, val, int(p.get("tempo_ms") or 0), origin)))
            else:
                k = ins[0]
                if cur[k] != prev.get(k):
                    v = bool(cur[k]) if pt.cdc in DIG else cur[k]
                    writes.append((lg, bid, lambda pt=pt, v=v: ied.set_points([{"id": pt.id, "valor": v}],
                                                                               origin=origin)))
        elif t == "bloqueio":
            a = bool(lg.inp(bid, "a"))
            out["ativo"] = a
            if a and p.get("controle"):
                blocks.setdefault(p["controle"], []).append({
                    "direcao": p.get("direcao") or "ambos", "causa": p.get("causa") or "intertravamento",
                    "so_com_check": bool(p.get("so_com_check")), "logica": lg.cfg["nome"]})

    def _dbl_writer(self, pt, val: int, ms: int, origin: str):
        ied = self.ied

        def w():
            if ms <= 0:
                ied.set_points([{"id": pt.id, "valor": val}], origin=origin)
                return
            ied.set_points([{"id": pt.id, "valor": 0}], origin=origin + " (manobra)")

            async def fim():
                await asyncio.sleep(ms / 1000.0)
                ied.set_points([{"id": pt.id, "valor": val}], origin=origin)
            ied._spawn(fim())
        return w

    # ------------------------------------------------------------------ estado ao vivo
    def live(self) -> dict:
        res = {}
        for lg in self.logics:
            res[lg.cfg["id"]] = {
                "ativa": lg.cfg["ativa"],
                "saidas": {bid: {k: v for k, v in o.items()} for bid, o in lg.out.items()},
                "ultimo": {bid: s["ultimo"] for bid, s in lg.st.items() if "ultimo" in s},
                "erros": dict(lg.errors),
            }
        return {"logicas": res, "varreduras": self.scans,
                "bloqueios": {k: v for k, v in self.ied.control.blocks.items()}}

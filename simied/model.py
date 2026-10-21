"""Modelo de dados em execução: árvore MMS por LD, DataSets e catálogo de pontos (seção 4)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .mms.types import Bits, MmsType, UtcTime, enc_data, value_to_json
from .scl import SclDA, SclFcda, SclFile, SclIED, Templates

FC_ORDER = ["ST", "MX", "SP", "SV", "CF", "DC", "SG", "SE", "SR", "OR", "BL", "EX", "CO",
            "RP", "LG", "BR", "GO", "GS", "MS", "US"]

DBPOS = {"intermediate-state": 0, "off": 1, "on": 2, "bad-state": 3}
TCMD = {"stop": 0, "lower": 1, "higher": 2, "reserved": 3}


def btype_mms(btype: str) -> MmsType | None:
    b = btype
    simple = {
        "BOOLEAN": ("bool", 0), "INT8": ("int", 8), "INT16": ("int", 16), "INT24": ("int", 32),
        "INT32": ("int", 32), "INT64": ("int", 64), "INT128": ("int", 128), "INT8U": ("uint", 8),
        "INT16U": ("uint", 16), "INT24U": ("uint", 32), "INT32U": ("uint", 32), "FLOAT32": ("float", 32),
        "FLOAT64": ("float", 64), "Enum": ("int", 8), "Dbpos": ("bits", 2), "Tcmd": ("bits", 2),
        "Check": ("bits", 2), "Quality": ("bits", 13), "Timestamp": ("utc", 0), "VisString32": ("vis", -32),
        "VisString64": ("vis", -64), "VisString65": ("vis", -65), "VisString129": ("vis", -129),
        "VisString255": ("vis", -255), "Unicode255": ("mmsstr", -255), "Octet64": ("octet", -64),
        "Octet6": ("octet", -6), "Octet16": ("octet", -16), "EntryTime": ("btime", 6),
        "ObjRef": ("vis", -129), "Currency": ("vis", -3), "TrgOps": ("bits", 6), "OptFlds": ("bits", 10),
        "SvOptFlds": ("bits", 5), "EntryID": ("octet", 8), "PhyComAddr": ("octet", -64),
    }
    if b in simple:
        k, s = simple[b]
        return MmsType(k, s)
    return None


class Node:
    __slots__ = ("name", "mt", "comps", "value", "parent", "path", "fc", "trg", "handler", "enum",
                 "btype", "cdc", "desc", "dotted", "domain")

    def __init__(self, name: str, parent: "Node | None" = None):
        self.name = name
        self.mt: MmsType | None = None
        self.comps: dict[str, Node] | None = None
        self.value = None
        self.parent = parent
        self.path = name if parent is None else f"{parent.path}${name}"
        self.fc = parent.fc if parent is not None else ""
        self.trg: frozenset = frozenset()
        self.handler = None
        self.enum: dict | None = None
        self.btype = ""
        self.cdc = ""
        self.desc = ""
        self.dotted = ""
        self.domain = parent.domain if parent is not None else ""

    @property
    def is_leaf(self) -> bool:
        return self.comps is None

    def add(self, child: "Node"):
        if self.comps is None:
            self.comps = {}
        self.comps[child.name] = child

    def finalize(self):
        if self.comps is not None:
            for c in self.comps.values():
                c.finalize()
            if self.mt is None or self.mt.kind != "array":
                self.mt = MmsType("struct", comps=[(n, c.mt) for n, c in self.comps.items()])
            else:
                first = next(iter(self.comps.values()))
                self.mt = MmsType("array", count=len(self.comps), elem=first.mt)

    def get(self):
        if self.comps is None:
            if self.handler is not None:
                return self.handler.read(self)
            return self.value
        return [c.get() for c in self.comps.values()]

    def encode(self) -> bytes:
        return enc_data(self.mt, self.get())

    def leaves(self):
        if self.comps is None:
            yield self
        else:
            for c in self.comps.values():
                yield from c.leaves()

    def walk(self):
        yield self
        if self.comps:
            for c in self.comps.values():
                yield from c.walk()

    def child(self, rel: str) -> "Node | None":
        n = self
        for p in rel.split("$"):
            if not n.comps or p not in n.comps:
                return None
            n = n.comps[p]
        return n

    def json(self):
        return value_to_json(self.mt, self.get())


@dataclass
class Member:
    domain: str
    item: str
    fc: str
    node: Node

    @property
    def ref(self) -> str:
        return f"{self.domain}/{self.item}"


@dataclass
class DataSet:
    domain: str
    ln: str
    name: str
    members: list = field(default_factory=list)
    desc: str = ""
    extra: bool = False
    fcdas: list = field(default_factory=list)

    @property
    def item(self) -> str:
        return f"{self.ln}${self.name}"

    @property
    def ref(self) -> str:
        return f"{self.domain}/{self.ln}${self.name}"

    @property
    def obj_ref(self) -> str:
        return f"{self.domain}/{self.ln}.{self.name}"


class Domain:
    def __init__(self, name: str, inst: str):
        self.name = name
        self.inst = inst
        self.lns: dict[str, Node] = {}
        self.index: dict[str, Node] = {}
        self.datasets: dict[str, DataSet] = {}
        self._names: list | None = None
        self.desc = ""

    def reindex(self):
        self.index = {}
        for ln in self.lns.values():
            for n in ln.walk():
                self.index[n.path] = n
        self._names = None

    def var_names(self) -> list[str]:
        if self._names is None:
            self._names = sorted(self.index.keys())
        return self._names

    def lookup(self, item: str) -> Node | None:
        return self.index.get(item)


# ---------------------------------------------------------------------------
# Conversão de texto (Val do SCL) para valores MMS

def parse_text(node: Node, text: str):
    k = node.mt.kind
    t = (text or "").strip()
    try:
        if k == "bool":
            return t.lower() in ("true", "1")
        if k in ("int", "uint"):
            if node.enum:
                for o, txt in node.enum.items():
                    if txt == t:
                        return o
            return int(float(t)) if t else 0
        if k == "float":
            return float(t) if t else 0.0
        if k == "bits":
            n = abs(node.mt.size)
            if node.btype == "Dbpos":
                return Bits(2, DBPOS.get(t, int(t) if t.isdigit() else 0))
            if node.btype in ("Tcmd", "Check"):
                return Bits(2, TCMD.get(t, int(t) if t.isdigit() else 0))
            return Bits(n, int(t, 0) if t else 0)
        if k in ("vis", "mmsstr"):
            return t
        if k == "octet":
            try:
                return bytes.fromhex(t)
            except ValueError:
                return t.encode()
    except (ValueError, TypeError):
        pass
    return node.mt.default()


class Builder:
    def __init__(self, scl: SclFile, ied: SclIED):
        self.scl = scl
        self.ied = ied
        self.tpl: Templates = scl.templates
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.ied_name_override = ied.name

    def ld_domain_name(self, ld) -> str:
        return ld.ld_name or f"{self.ied_name_override}{ld.inst}"

    def build_da(self, da: SclDA, parent: Node, path_hint: str) -> Node | None:
        node = Node(da.name, parent)
        node.btype = da.btype
        node.trg = da.trg
        node.desc = da.desc
        if da.count and da.count > 0:
            # array de elementos iguais
            node.comps = {}
            node.mt = MmsType("array")
            for i in range(da.count):
                sub = SclDA(str(i), da.btype, da.type, da.fc, 0, da.trg, da.val)
                c = self.build_da(sub, node, path_hint)
                if c is None:
                    return None
                node.comps[str(i)] = c
            return node
        if da.btype == "Struct":
            bdas = self.tpl.datype.get(da.type)
            if bdas is None:
                self.errors.append(f"DAType ausente '{da.type}' em {path_hint}.{da.name}")
                return None
            node.comps = {}
            for b in bdas:
                c = self.build_da(SclDA(b.name, b.btype, b.type, da.fc, b.count, da.trg, b.val), node,
                                  f"{path_hint}.{da.name}")
                if c is not None:
                    node.comps[b.name] = c
            if not node.comps:
                return None
            return node
        mt = btype_mms(da.btype)
        if mt is None:
            self.warnings.append(f"bType '{da.btype}' não suportado em {path_hint}.{da.name}; tratado como Octet64")
            mt = MmsType("octet", -64)
        node.mt = mt
        if da.btype == "Enum":
            node.enum = self.tpl.enum.get(da.type)
            if node.enum is None:
                self.warnings.append(f"EnumType ausente '{da.type}' em {path_hint}.{da.name}")
        node.value = parse_text(node, da.val) if da.val is not None else mt.default()
        if da.btype == "Enum" and da.val is None and node.enum:
            node.value = min(node.enum.keys())
        return node

    def build_do(self, dotype_id: str, fc: str, name: str, parent: Node, path_hint: str) -> Node | None:
        dt = self.tpl.dotype.get(dotype_id)
        if dt is None:
            self.errors.append(f"DOType ausente '{dotype_id}' em {path_hint}")
            return None
        node = Node(name, parent)
        node.cdc = dt.cdc
        node.comps = {}
        for e in dt.elems:
            if e[0] == "DA":
                da = e[1]
                if da.fc != fc:
                    continue
                c = self.build_da(da, node, f"{path_hint}")
            else:
                c = self.build_do(e[2], fc, e[1], node, f"{path_hint}.{e[1]}")
                if c is not None:
                    c.desc = e[3]
            if c is not None:
                node.comps[c.name] = c
        if not node.comps:
            return None
        return node

    def has_fc(self, dotype_id: str, fc: str, depth=0) -> bool:
        dt = self.tpl.dotype.get(dotype_id)
        if dt is None or depth > 8:
            return False
        for e in dt.elems:
            if e[0] == "DA" and e[1].fc == fc:
                return True
            if e[0] == "SDO" and self.has_fc(e[2], fc, depth + 1):
                return True
        return False

    def build_ln(self, dom: Domain, ln) -> Node | None:
        lt = self.tpl.lnode.get(ln.ln_type)
        if lt is None:
            self.errors.append(f"LNodeType ausente '{ln.ln_type}' para {dom.name}/{ln.name}")
            return None
        lnn = Node(ln.name)
        lnn.domain = dom.name
        lnn.desc = ln.desc
        lnn.comps = {}
        for fc in FC_ORDER:
            fcn = Node(fc, lnn)
            fcn.fc = fc
            fcn.comps = {}
            for do_name, do_type, do_desc in lt.dos:
                if not self.has_fc(do_type, fc):
                    continue
                d = self.build_do(do_type, fc, do_name, fcn, f"{dom.name}/{ln.name}.{do_name}")
                if d is not None:
                    d.desc = ln.descs.get(do_name, do_desc)
                    fcn.comps[do_name] = d
            if fcn.comps:
                lnn.comps[fc] = fcn
        lnn.finalize()
        # valores iniciais das instâncias (DOI/DAI)
        dotted: dict[str, Node] = {}
        for fcn in lnn.comps.values():
            for n in fcn.walk():
                if n is fcn:
                    continue
                n.dotted = n.path.split("$", 2)[2].replace("$", ".")
                if n.is_leaf:
                    dotted[n.dotted] = n
        for key, txt in ln.inits.items():
            n = dotted.get(key)
            if n is not None:
                n.value = parse_text(n, txt)
        return lnn

    def build(self) -> dict[str, Domain]:
        doms: dict[str, Domain] = {}
        for ld in self.ied.lds:
            dom = Domain(self.ld_domain_name(ld), ld.inst)
            dom.desc = ld.desc
            for ln in ld.lns:
                lnn = self.build_ln(dom, ln)
                if lnn is not None:
                    dom.lns[ln.name] = lnn
            doms[dom.name] = dom
        for d in doms.values():
            for ln in d.lns.values():
                for n in ln.walk():
                    n.domain = d.name
            d.reindex()
        return doms

    def domain_for_inst(self, doms: dict, inst: str) -> Domain | None:
        for ld in self.ied.lds:
            if ld.inst == inst:
                return doms.get(self.ld_domain_name(ld))
        return None


def resolve_fcda(doms: dict, builder: "Builder | None", own_domain: Domain, f: SclFcda,
                 ied_name: str) -> tuple[Member | None, str]:
    dom = None
    if f.ld_inst:
        if builder is not None:
            dom = builder.domain_for_inst(doms, f.ld_inst)
        if dom is None:
            dom = doms.get(f"{ied_name}{f.ld_inst}")
    else:
        dom = own_domain
    if dom is None:
        return None, f"LD '{f.ld_inst}' inexistente"
    ln = f"{f.prefix}{f.ln_class}{f.ln_inst}"
    item = f"{ln}${f.fc}"
    if f.do_name:
        item += "$" + f.do_name.replace(".", "$")
    if f.da_name:
        item += "$" + f.da_name.replace(".", "$")
    node = dom.lookup(item)
    if node is None:
        return None, f"objeto '{dom.name}/{item}' inexistente"
    return Member(dom.name, item, f.fc, node), ""


# ---------------------------------------------------------------------------
# Catálogo de pontos

DIG_CDC = {"SPS": "stVal", "SPC": "stVal", "DPS": "stVal", "DPC": "stVal", "ACT": "general", "ACD": "general"}
ANA_CDC = {"MV": "mag", "CMV": "cVal", "INS": "stVal", "INC": "stVal", "ENS": "stVal", "ENC": "stVal",
           "BCR": "actVal", "ISC": "valWTr"}
VALUE_FC = {"MV": "MX", "CMV": "MX"}


@dataclass
class Point:
    id: int
    domain: str
    ln: str
    do: str  # caminho do DO relativo ao FC (ex.: "Ind3", "A$phsA")
    fc: str
    cdc: str
    kind: str  # dig | ana | ro
    desc: str = ""
    do_node: Node | None = None
    v: Node | None = None  # folha do valor principal
    vi: Node | None = None  # inteiro equivalente (mag.i)
    a: Node | None = None  # ângulo (CMV)
    ai: Node | None = None
    q: Node | None = None
    t: Node | None = None
    datasets: list = field(default_factory=list)  # [(ds_ref, posição)]
    units: str = ""
    db: int = 0
    zero_db: int = 0
    rng: tuple | None = None
    svc: tuple | None = None
    mode: str = "manual"
    prev_mode: str = "manual"
    link: dict | None = None
    last_reported: float | None = None
    last_change: float = 0.0
    writable: bool = False
    cid_val: object = None  # valor que o CID dá ao ponto na carga (antes de restaurar o estado salvo)

    @property
    def path(self) -> str:
        return f"{self.domain}/{self.ln}${self.fc}${self.do}"

    @property
    def ln_path(self) -> str:
        return f"{self.ln}${self.fc}${self.do}"

    def value(self):
        if self.v is None:
            return None
        if self.cdc in ("DPS", "DPC"):
            return self.v.get().val
        if self.cdc == "CMV":
            return {"mag": self.v.get(), "ang": self.a.get() if self.a else 0.0}
        return self.v.get()

    def quality(self) -> Bits | None:
        return self.q.get() if self.q is not None else None

    def timestamp(self) -> UtcTime | None:
        return self.t.get() if self.t is not None else None


def _units_text(cf: Node | None, ctx) -> str:
    if cf is None:
        return ""
    u = cf.child("units$SIUnit")
    m = cf.child("units$multiplier")
    txt = ""
    if u is not None and u.enum:
        txt = u.enum.get(u.value, str(u.value))
    elif u is not None:
        txt = str(u.value)
    if m is not None and m.enum and m.value:
        txt = f"{m.enum.get(m.value, '')}{txt}"
    return txt


def build_points(doms: dict, builder: Builder) -> list[Point]:
    pts: list[Point] = []
    tpl = builder.tpl

    def cf_node(dom: Domain, ln: str, do: str) -> Node | None:
        return dom.lookup(f"{ln}$CF${do}")

    def add_point(dom: Domain, ln_name: str, do_rel: str, cdc: str, desc: str):
        if cdc in DIG_CDC:
            kind, attr = "dig", DIG_CDC[cdc]
        elif cdc in ANA_CDC:
            kind, attr = "ana", ANA_CDC[cdc]
        else:
            kind, attr = "ro", ""
        fc = VALUE_FC.get(cdc, "ST")
        do_node = dom.lookup(f"{ln_name}${fc}${do_rel}")
        if do_node is None:
            for alt in ("ST", "MX", "SP"):
                do_node = dom.lookup(f"{ln_name}${alt}${do_rel}")
                if do_node is not None:
                    fc = alt
                    break
        if do_node is None:
            return
        p = Point(len(pts), dom.name, ln_name, do_rel, fc, cdc, kind, desc, do_node)
        if kind == "dig":
            p.v = do_node.child(attr)
        elif cdc == "MV":
            p.v = do_node.child("mag$f")
            p.vi = do_node.child("mag$i")
            if p.v is None:
                p.v, p.vi = p.vi, None
        elif cdc == "CMV":
            p.v = do_node.child("cVal$mag$f")
            p.a = do_node.child("cVal$ang$f")
            p.vi = do_node.child("cVal$mag$i")
            p.ai = do_node.child("cVal$ang$i")
            if p.v is None:
                p.v, p.vi = p.vi, None
            if p.a is None:
                p.a, p.ai = p.ai, None
        elif kind == "ana":
            p.v = do_node.child(attr)
        if p.v is None:
            # CDC desconhecido ou incompleto: primeira folha relevante, somente leitura
            p.kind = "ro"
            for leaf in do_node.leaves():
                if leaf.name not in ("q", "t"):
                    p.v = leaf
                    break
        p.q = do_node.child("q")
        p.t = do_node.child("t")
        cf = cf_node(dom, ln_name, do_rel)
        p.units = _units_text(cf, tpl)
        if cf is not None:
            dbn = cf.child("db")
            if dbn is not None:
                p.db = int(dbn.value or 0)
            zn = cf.child("zeroDb")
            if zn is not None:
                p.zero_db = int(zn.value or 0)
            lo = cf.child("rangeC$min$f") or cf.child("minVal$f") or cf.child("minVal")
            hi = cf.child("rangeC$max$f") or cf.child("maxVal$f") or cf.child("maxVal")
            if lo is not None and hi is not None:
                try:
                    lv, hv = float(lo.value), float(hi.value)
                    if hv > lv:
                        p.rng = (lv, hv)
                except (TypeError, ValueError):
                    pass
            sf = cf.child("sVC$scaleFactor")
            off = cf.child("sVC$offset")
            if sf is not None:
                p.svc = (float(sf.value or 1.0), float(off.value or 0.0) if off is not None else 0.0)
        if p.kind == "ro":
            builder.warnings.append(f"CDC '{cdc}' de {p.path} tratado como somente leitura")
        pts.append(p)

    def walk_do(dom: Domain, ln_name: str, do_rel: str, do_type: str, desc: str, depth=0):
        dt = tpl.dotype.get(do_type)
        if dt is None or depth > 6:
            return
        if dt.cdc in DIG_CDC or dt.cdc in ANA_CDC:
            add_point(dom, ln_name, do_rel, dt.cdc, desc)
            return
        sdos = [e for e in dt.elems if e[0] == "SDO"]
        if sdos and not any(e[0] == "DA" and e[1].fc in ("ST", "MX") for e in dt.elems):
            for e in sdos:
                walk_do(dom, ln_name, f"{do_rel}${e[1]}", e[2], f"{desc or do_rel.replace('$', '.')} {e[1]}".strip(), depth + 1)
            return
        if any(e[0] == "DA" and e[1].fc in ("ST", "MX", "SP") for e in dt.elems):
            add_point(dom, ln_name, do_rel, dt.cdc, desc)

    for ld in builder.ied.lds:
        dom = doms.get(builder.ld_domain_name(ld))
        if dom is None:
            continue
        for ln in ld.lns:
            if ln.name not in dom.lns:
                continue
            lt = tpl.lnode.get(ln.ln_type)
            if lt is None:
                continue
            for do_name, do_type, do_desc in lt.dos:
                walk_do(dom, ln.name, do_name, do_type, ln.descs.get(do_name, do_desc))
    return pts


def link_points_to_datasets(points: list[Point], datasets: list[DataSet]):
    by_do: dict[tuple, Point] = {(p.domain, p.ln_path): p for p in points}
    for p in points:
        p.datasets = []
    for ds in datasets:
        for idx, m in enumerate(ds.members):
            seen = set()
            for leaf in m.node.leaves():
                # sobe até achar o DO de um ponto
                parts = leaf.path.split("$")
                for cut in range(len(parts), 2, -1):
                    key = (m.domain, "$".join(parts[:cut]))
                    p = by_do.get(key)
                    if p is not None and p.id not in seen:
                        seen.add(p.id)
                        p.datasets.append((ds.ref, idx))
                        break

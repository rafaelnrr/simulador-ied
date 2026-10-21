"""Editor de CID: rascunho editável do SCL de um IED (ou de um arquivo avulso).

Três visões do mesmo documento:
- modelo: IED → LD → LN → DO/SDO → DA/BDA, expandido a partir dos DataTypeTemplates e combinado com as
  instâncias (DOI/SDI/DAI), como num navegador de IED; DataSets e ReportControls aparecem dentro do LN;
- tipos: LNodeType, DOType, DAType e EnumType, com quem usa cada um;
- xml: todos os elementos do arquivo.

Identificadores de nó:
- "x.3.0.1": elemento pela posição entre os filhos-elemento (a raiz <SCL> é "x");
- "m/IED/LD/LN/DO/SDO/DA": nó do modelo, pelos nomes;
- "t/DOType": grupo da visão de tipos.
Edições estruturais mudam as posições; cada operação devolve o identificador a selecionar.

O rascunho é gravado em data/cid_editor a cada alteração e só vai para o IED quando aplicado (reimportação,
que mantém o estado dos pontos que continuam existindo).
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import io
import json
import re
import secrets
import threading
import time
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from pathlib import Path

from .ied import PointError
from .scl import NS_SCL, SclError

MAX_UNDO = 30
MAX_FILHOS = 1000
MAX_XML = 200_000

# Ordem dos filhos segundo o esquema SCL; usada para inserir elementos novos no lugar certo.
ORDEM = {
    "SCL": ["Text", "Private", "Header", "Substation", "Communication", "IED", "DataTypeTemplates", "Line", "Process"],
    "Header": ["Text", "History"],
    "IED": ["Text", "Private", "Services", "AccessPoint", "KDC"],
    "AccessPoint": ["Text", "Private", "Server", "LN", "ServerAt", "Services", "GOOSESecurity", "SMVSecurity"],
    "Server": ["Text", "Private", "Authentication", "LDevice", "Association"],
    "LDevice": ["Text", "Private", "LN0", "LN", "AccessControl"],
    "LN0": ["Text", "Private", "DataSet", "ReportControl", "LogControl", "DOI", "Inputs", "Log", "GSEControl",
            "SampledValueControl", "SettingControl"],
    "LN": ["Text", "Private", "DataSet", "ReportControl", "LogControl", "DOI", "Inputs", "Log"],
    "DOI": ["Text", "Private", ("SDI", "DAI")],
    "SDI": ["Text", "Private", ("SDI", "DAI")],
    "DAI": ["Text", "Private", "Val"],
    "DataSet": ["Text", "Private", ("FCDA", "FCCB")],
    "ReportControl": ["Text", "Private", "TrgOps", "OptFields", "RptEnabled"],
    "RptEnabled": ["Text", "Private", "ClientLN"],
    "GSEControl": ["Text", "Private", "IEDName", "Protocol"],
    "Inputs": ["Text", "Private", "ExtRef"],
    "DataTypeTemplates": ["LNodeType", "DOType", "DAType", "EnumType"],
    "LNodeType": ["Text", "Private", "DO"],
    "DOType": ["Text", "Private", ("SDO", "DA")],
    "DA": ["Text", "Private", "ProtNs", "Val"],
    "DAType": ["Text", "Private", "BDA", "ProtNs"],
    "BDA": ["Text", "Private", "Val"],
    "EnumType": ["Text", "Private", "EnumVal"],
    "Communication": ["Text", "Private", "SubNetwork"],
    "SubNetwork": ["Text", "Private", "BitRate", "ConnectedAP"],
    "ConnectedAP": ["Text", "Private", "Address", "GSE", "SMV", "PhysConn"],
    "Address": ["P"],
    "GSE": ["Text", "Private", "Address", "MinTime", "MaxTime"],
}

# Atributos sugeridos ao criar um elemento (os demais podem ser acrescentados depois).
MODELOS = {
    "DataSet": {"name": "", "desc": ""},
    "FCDA": {"ldInst": "", "prefix": "", "lnClass": "", "lnInst": "", "doName": "", "daName": "", "fc": "ST"},
    "ReportControl": {"name": "", "datSet": "", "rptID": "", "confRev": "1", "buffered": "false", "bufTime": "0",
                      "intgPd": "0", "desc": ""},
    "TrgOps": {"dchg": "true", "qchg": "true", "dupd": "false", "period": "false", "gi": "true"},
    "OptFields": {"seqNum": "true", "timeStamp": "true", "dataSet": "true", "reasonCode": "true", "dataRef": "false",
                  "entryID": "false", "configRef": "true"},
    "RptEnabled": {"max": "1"},
    "ClientLN": {"iedName": "", "ldInst": "", "prefix": "", "lnClass": "", "lnInst": ""},
    "DOI": {"name": "", "desc": ""},
    "SDI": {"name": ""},
    "DAI": {"name": "", "valKind": "Set"},
    "Val": {},
    "LDevice": {"inst": "", "desc": ""},
    "LN": {"prefix": "", "lnClass": "", "inst": "1", "lnType": "", "desc": ""},
    "LNodeType": {"id": "", "lnClass": ""},
    "DO": {"name": "", "type": ""},
    "DOType": {"id": "", "cdc": ""},
    "SDO": {"name": "", "type": ""},
    "DA": {"name": "", "bType": "", "fc": "ST", "dchg": "false", "qchg": "false", "dupd": "false"},
    "DAType": {"id": ""},
    "BDA": {"name": "", "bType": ""},
    "EnumType": {"id": ""},
    "EnumVal": {"ord": "0"},
    "P": {"type": ""},
    "Private": {"type": ""},
    "ConnectedAP": {"iedName": "", "apName": ""},
    "SubNetwork": {"name": "", "type": "8-MMS"},
    "ExtRef": {"iedName": "", "ldInst": "", "prefix": "", "lnClass": "", "lnInst": "", "doName": "", "daName": ""},
}
# Elementos cujo conteúdo é texto.
TEXTO = {"Val", "EnumVal", "P", "Text", "IEDName", "Protocol", "MinTime", "MaxTime", "BitRate", "ProtNs"}

# Formulário do ReportControl (atributos e opções conforme o esquema SCL).
RCB_ATTRS = ["name", "desc", "datSet", "rptID", "buffered", "intgPd", "confRev", "bufTime", "indexed"]
RCB_TRG = ["dchg", "qchg", "dupd", "period", "gi"]
RCB_OPT_ED1 = ["seqNum", "timeStamp", "dataSet", "reasonCode", "dataRef", "entryID", "configRef", "bufOvfl",
               "segmentation"]
RCB_OPT_ED2 = RCB_OPT_ED1[:-1]
CLIENTLN_ATTRS = ["iedName", "apRef", "ldInst", "prefix", "lnClass", "lnInst", "desc"]

TIPO = {
    "SCL": "doc", "Header": "doc", "History": "doc", "Hitem": "doc", "Text": "doc",
    "Communication": "com", "SubNetwork": "com", "ConnectedAP": "com", "Address": "com", "GSE": "com",
    "SMV": "com", "PhysConn": "com", "P": "val",
    "IED": "ied", "AccessPoint": "ap", "Server": "ap", "Services": "srv", "LDevice": "ld", "LN0": "ln", "LN": "ln",
    "DOI": "do", "SDI": "sdo", "DAI": "da", "Val": "val", "DataSet": "ds", "FCDA": "fcda", "FCCB": "fcda",
    "ReportControl": "rcb", "TrgOps": "opt", "OptFields": "opt", "RptEnabled": "opt", "ClientLN": "opt",
    "GSEControl": "goose", "SampledValueControl": "goose", "LogControl": "log", "Log": "log",
    "SettingControl": "opt", "Inputs": "in", "ExtRef": "in",
    "DataTypeTemplates": "tpl", "LNodeType": "lnt", "DOType": "dot", "DAType": "dat", "EnumType": "enum",
    "DO": "do", "SDO": "sdo", "DA": "da", "BDA": "bda", "EnumVal": "val", "Private": "priv",
    "Substation": "sub", "VoltageLevel": "sub", "Bay": "sub", "ConductingEquipment": "sub", "LNode": "sub",
}
GRUPOS_TIPOS = ["LNodeType", "DOType", "DAType", "EnumType"]

_FAIXA = {"INT8": (-2**7, 2**7 - 1), "INT16": (-2**15, 2**15 - 1), "INT32": (-2**31, 2**31 - 1),
          "INT64": (-2**63, 2**63 - 1), "INT8U": (0, 255), "INT16U": (0, 2**16 - 1), "INT24U": (0, 2**24 - 1),
          "INT32U": (0, 2**32 - 1)}
_TAM = {"VisString32": 32, "VisString64": 64, "VisString65": 65, "VisString129": 129, "VisString255": 255,
        "Unicode255": 255, "Octet64": 64}
_NOME_XML = re.compile(r"^[A-Za-z_][\w.\-]*(:[A-Za-z_][\w.\-]*)?$")


def _t(el) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _els(el) -> list:
    return [c for c in el if isinstance(c.tag, str)]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def ln_name(el) -> str:
    return el.get("prefix", "") + el.get("lnClass", "") + el.get("inst", "")


def fcda_text(f) -> str:
    s = f"{f.get('ldInst', '')}/{f.get('prefix', '')}{f.get('lnClass', '')}{f.get('lnInst', '')}.{f.get('doName', '')}"
    if f.get("daName"):
        s += "." + f.get("daName")
    return f"{s} [{f.get('fc', '')}]"


def check_value(btype: str, valor: str, enum: list | None = None) -> str:
    """enum: [(ord, texto)] do EnumType; o Val pode trazer o texto (o usual) ou o ordinal."""
    v = valor.strip()
    if btype == "BOOLEAN":
        if v.lower() in ("true", "1"):
            return "true"
        if v.lower() in ("false", "0"):
            return "false"
        raise ValueError(f"valor '{valor}' inválido para BOOLEAN; use true ou false")
    if btype in _FAIXA:
        try:
            n = int(v)
        except ValueError:
            raise ValueError(f"valor '{valor}' inválido para {btype}: deve ser inteiro") from None
        lo, hi = _FAIXA[btype]
        if not lo <= n <= hi:
            raise ValueError(f"valor {n} fora da faixa de {btype} ({lo} a {hi})")
        return str(n)
    if btype in ("FLOAT32", "FLOAT64"):
        try:
            float(v)
        except ValueError:
            raise ValueError(f"valor '{valor}' inválido para {btype}: deve ser número (ponto decimal)") from None
        return v
    if btype == "Enum" and enum is not None and v not in [t for _, t in enum] and v not in [o for o, _ in enum]:
        raise ValueError(f"valor '{valor}' não existe no EnumType; use um de {[t for _, t in enum]} (ou o ordinal)")
    if btype in _TAM and len(valor) > _TAM[btype]:
        raise ValueError(f"texto com {len(valor)} caracteres; {btype} aceita até {_TAM[btype]}")
    return valor


# ---------------------------------------------------------------------------------------------- documento
class Doc:
    """O documento SCL em árvore, com serialização estável e índices recalculados a cada versão."""

    def __init__(self, data: bytes):
        self.load(data)

    def load(self, data: bytes):
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        try:
            parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
            root = ET.fromstring(data, parser)
        except ET.ParseError as e:
            raise SclError(f"XML inválido: {e}") from e
        if _t(root) != "SCL":
            raise SclError("O arquivo não é SCL (raiz diferente de <SCL>)")
        m = re.search(rb"<(?:[A-Za-z_][\w.\-]*:)?SCL[\s>/]", data)
        prolog = data[:m.start()] if m else b""
        # O comentário antes de <SCL> fica no prólogo (o ElementTree não guarda o que está fora da raiz).
        self.prolog = re.sub(rb"""encoding\s*=\s*["'][^"']*["']""", b'encoding="UTF-8"', prolog, count=1)
        self.ns: list[tuple[str, str]] = []
        self.root_ns: list[tuple[str, str]] = []  # declarados na própria <SCL>, mesmo que não usados
        for ev, x in ET.iterparse(io.BytesIO(data), events=("start-ns", "start")):
            if ev == "start" and not self.root_ns:
                self.root_ns = list(self.ns) or [("", "")]
            elif ev == "start-ns" and x not in self.ns:
                self.ns.append(x)
        self.root = root
        self.ns_scl = root.tag[1:].split("}")[0] if root.tag.startswith("{") else ""
        first = (root.text or "").rsplit("\n", 1)[-1]
        self.unit = first if first and not first.strip() else "  "  # recuo usado no arquivo
        self._idx = None

    def _register(self):
        for pfx, uri in self.ns:
            if uri == self.ns_scl:
                ET.register_namespace("", uri)
            elif pfx and not re.fullmatch(r"ns\d+", pfx) and pfx != "xml":
                try:
                    ET.register_namespace(pfx, uri)
                except ValueError:
                    pass
        if self.ns_scl:
            ET.register_namespace("", self.ns_scl)

    def serialize(self) -> bytes:
        self._register()
        body = ET.tostring(self.root, encoding="unicode").replace(" />", "/>")
        head = body[:body.index(">")]
        missing = "".join(f' xmlns:{p}="{u}"' for p, u in self.root_ns
                          if p and f'xmlns:{p}="' not in head and f'="{u}"' not in head)
        if missing:
            cut = len("<SCL") if head.startswith("<SCL") else head.index(" ")
            body = body[:cut] + missing + body[cut:]
        body = body.encode("utf-8")
        pro = self.prolog or b'<?xml version="1.0" encoding="UTF-8"?>\n'
        return pro + body + b"\n"

    def fragment(self, el) -> str:
        self._register()
        tail, el.tail = el.tail, None
        try:
            s = ET.tostring(el, encoding="unicode").replace(" />", "/>")
        finally:
            el.tail = tail
        # O ElementTree repete no fragmento as declarações de namespace; as do próprio arquivo são retiradas
        # (o editor as declara de novo ao ler o fragmento editado).
        known = {(p, u) for p, u in self.ns} | {("", self.ns_scl)}

        def drop(m):
            return "" if (m.group(1) or "", m.group(2)) in known else m.group(0)
        return re.sub(r'\s+xmlns(?::([\w.\-]+))?="([^"]*)"', drop, s)

    def q(self, tag: str) -> str:
        return f"{{{self.ns_scl}}}{tag}" if self.ns_scl else tag

    def touched(self):
        self._idx = None

    # ------------------------------------------------------------------ endereçamento
    def at(self, xid: str):
        """Elemento e a cadeia de ancestrais (raiz primeiro) para um identificador "x.i.j"."""
        if xid != "x" and not xid.startswith("x."):
            raise PointError(f"nó '{xid}' não é um elemento XML", status=422)
        chain = [self.root]
        for part in xid.split(".")[1:]:
            kids = _els(chain[-1])
            try:
                chain.append(kids[int(part)])
            except (ValueError, IndexError):
                raise PointError(f"nó '{xid}' não existe (o documento mudou?)", status=404) from None
        return chain

    def el(self, xid: str):
        return self.at(xid)[-1]

    @property
    def idx(self) -> "Index":
        if self._idx is None:
            self._idx = Index(self)
        return self._idx

    def xid(self, el) -> str:
        return self.idx.ids[el]


class Index:
    """Índices de uma versão do documento: identificadores, tipos, usos, IEDs/LDs/LNs e FCDAs."""

    def __init__(self, doc: Doc):
        self.doc = doc
        self.ids: dict = {}
        self.parent: dict = {}
        self.tpl: dict[str, dict] = {k: {} for k in GRUPOS_TIPOS}
        self.tpl_dup: list = []
        self.uses: dict[tuple, list] = {}
        self.ieds: dict[str, object] = {}
        self.lds: dict[str, dict] = {}       # ied -> {inst: LDevice}
        self.lns: dict[tuple, dict] = {}     # (ied, ld) -> {nome: LN}
        self.fcdas: dict[str, list] = {}     # ied -> [(ld, ln, segs, fc, fcda, dataset)]
        self._model = None
        self._xml = None
        stack = [(doc.root, "x")]
        while stack:
            el, xid = stack.pop()
            self.ids[el] = xid
            kids = _els(el)
            for i in range(len(kids) - 1, -1, -1):  # empilha ao contrário: ids fica na ordem do documento
                self.parent[kids[i]] = el
                stack.append((kids[i], f"{xid}.{i}"))
        root = doc.root
        for dtt in (c for c in _els(root) if _t(c) == "DataTypeTemplates"):
            for c in _els(dtt):
                k = _t(c)
                if k in self.tpl:
                    if c.get("id", "") in self.tpl[k]:
                        self.tpl_dup.append(c)
                    else:
                        self.tpl[k][c.get("id", "")] = c
        for el in self.ids:
            tag = _t(el)
            if tag in ("LN0", "LN") and el.get("lnType"):
                self.uses.setdefault(("LNodeType", el.get("lnType")), []).append(el)
            elif tag in ("DO", "SDO") and el.get("type"):
                self.uses.setdefault(("DOType", el.get("type")), []).append(el)
            elif tag in ("DA", "BDA") and el.get("type"):
                k = "EnumType" if el.get("bType") == "Enum" else "DAType"
                self.uses.setdefault((k, el.get("type")), []).append(el)
        for ied in (c for c in _els(root) if _t(c) == "IED"):
            nm = ied.get("name", "")
            self.ieds[nm] = ied
            lds = self.lds.setdefault(nm, {})
            for ap in (c for c in _els(ied) if _t(c) == "AccessPoint"):
                for srv in (c for c in _els(ap) if _t(c) == "Server"):
                    for ld in (c for c in _els(srv) if _t(c) == "LDevice"):
                        lds.setdefault(ld.get("inst", ""), ld)
                        lns = self.lns.setdefault((nm, ld.get("inst", "")), {})
                        for ln in (c for c in _els(ld) if _t(c) in ("LN0", "LN")):
                            lns.setdefault(ln_name(ln), ln)
            out = self.fcdas.setdefault(nm, [])
            for ld in lds.values():
                for ln in self.lns[(nm, ld.get("inst", ""))].values():
                    for ds in (c for c in _els(ln) if _t(c) == "DataSet"):
                        for f in (c for c in _els(ds) if _t(c) == "FCDA"):
                            segs = [s for s in (f.get("doName", "").split(".") + f.get("daName", "").split("."))
                                    if s]
                            fln = f.get("prefix", "") + f.get("lnClass", "") + f.get("lnInst", "")
                            out.append((f.get("ldInst", ""), fln, segs, f.get("fc", ""), f, ds))

    def ied_of(self, el):
        while el is not None and _t(el) != "IED":
            el = self.parent.get(el)
        return el

    def type_ref(self, el) -> tuple[str, str] | None:
        """(grupo, id) do tipo a que o elemento se refere, se houver."""
        tag = _t(el)
        if tag in ("LN0", "LN") and el.get("lnType"):
            return "LNodeType", el.get("lnType")
        if tag in ("DO", "SDO") and el.get("type"):
            return "DOType", el.get("type")
        if tag in ("DA", "BDA") and el.get("type") and el.get("bType") in ("Struct", "Enum"):
            return ("EnumType" if el.get("bType") == "Enum" else "DAType"), el.get("type")
        return None

    def enum_texts(self, tid: str) -> list | None:
        """[(ord, texto)] do EnumType, ou None se ele não existe."""
        e = self.tpl["EnumType"].get(tid)
        return None if e is None else [(c.get("ord", ""), (c.text or "").strip()) for c in _els(e) if _t(c) == "EnumVal"]

    # ------------------------------------------------------------------ modelo expandido
    def expand(self, kind: str, tid: str) -> list:
        """Filhos de um tipo: [(nome, elemento do template)] em ordem."""
        el = self.tpl.get(kind, {}).get(tid)
        if el is None:
            return []
        want = {"LNodeType": ("DO",), "DOType": ("SDO", "DA"), "DAType": ("BDA",)}[kind]
        return [(c.get("name", ""), c) for c in _els(el) if _t(c) in want]

    def tchildren(self, tel) -> list:
        """Filhos no modelo de um elemento de template (DO/SDO/DA/BDA)."""
        tag = _t(tel)
        if tag in ("DO", "SDO"):
            return self.expand("DOType", tel.get("type", ""))
        if tag in ("DA", "BDA") and tel.get("bType") == "Struct":
            return self.expand("DAType", tel.get("type", ""))
        return []

    def model_index(self) -> list:
        """[(m-id, referência, descrição, tipo)] de todo o modelo, para a busca."""
        if self._model is not None:
            return self._model
        memo: dict = {}

        def rel(tel, depth=0):
            key = id(tel)
            if key in memo:
                return memo[key]
            out = []
            if depth < 12:
                for nm, c in self.tchildren(tel):
                    out.append(([nm], c))
                    out.extend(([nm] + p, cc) for p, cc in rel(c, depth + 1))
            memo[key] = out
            return out

        res = []
        for ied, lds in self.lds.items():
            for ld_inst, ld in lds.items():
                res.append((f"m/{ied}/{ld_inst}", f"{ied}{ld_inst}", ld.get("desc", ""), "ld"))
                for lnn, ln in self.lns[(ied, ld_inst)].items():
                    base = f"m/{ied}/{ld_inst}/{lnn}"
                    res.append((base, f"{ld_inst}/{lnn}", ln.get("desc", ""), "ln"))
                    descs = {d.get("name", ""): d.get("desc", "") for d in _els(ln) if _t(d) == "DOI"}
                    for nm, do in self.expand("LNodeType", ln.get("lnType", "")):
                        res.append((f"{base}/{nm}", f"{ld_inst}/{lnn}.{nm}", descs.get(nm, "") or do.get("desc", ""),
                                    "do"))
                        for p, c in rel(do):
                            fc = c.get("fc", "")
                            res.append((f"{base}/{nm}/" + "/".join(p), f"{ld_inst}/{lnn}.{nm}." + ".".join(p)
                                        + (f" [{fc}]" if fc else ""), c.get("desc", ""), TIPO.get(_t(c), "da")))
                    for c in _els(ln):
                        if _t(c) in ("DataSet", "ReportControl", "GSEControl", "LogControl", "SampledValueControl"):
                            res.append((self.ids[c], f"{ld_inst}/{lnn}.{c.get('name', '')}", c.get("desc", ""),
                                        TIPO.get(_t(c), "el")))
        self._model = res
        return res

    def xml_index(self) -> list:
        if self._xml is not None:
            return self._xml
        res = []
        for el, xid in self.ids.items():
            vals = " ".join(el.attrib.values())
            txt = (el.text or "").strip() if _t(el) in TEXTO else ""
            res.append((xid, _t(el), f"{_t(el)} {vals} {txt}".lower(), el))
        self._xml = res
        return res


# ---------------------------------------------------------------------------------------------- rótulos
def label(el) -> str:
    tag = _t(el)
    g = el.get
    if tag in ("LN0", "LN"):
        return ln_name(el)
    if tag in ("LDevice",):
        return g("inst", "")
    if tag in ("LNodeType", "DOType", "DAType", "EnumType"):
        return g("id", "")
    if tag == "FCDA":
        return fcda_text(el)
    if tag in ("SDI", "DAI") and g("ix"):
        return f"{g('name', '')}({g('ix')})"
    if tag == "EnumVal":
        return f"{g('ord', '')} = {(el.text or '').strip()}"
    if tag == "P":
        return f"{g('type', '')} = {(el.text or '').strip()}"
    if tag == "Val":
        return (el.text or "").strip() or "(vazio)"
    if tag == "ConnectedAP":
        return f"{g('iedName', '')} / {g('apName', '')}"
    if tag == "ClientLN":
        return f"{g('iedName', '')} {g('ldInst', '')}/{g('prefix', '')}{g('lnClass', '')}{g('lnInst', '')}"
    if tag == "ExtRef":
        return f"{g('iedName', '')}{g('ldInst', '')}/{g('prefix', '')}{g('lnClass', '')}{g('lnInst', '')}." \
               f"{g('doName', '')}" + (f".{g('daName')}" if g("daName") else "")
    if tag == "Private":
        return g("type", "")
    if tag == "SCL":
        return (g("version", "") + g("revision", "")) or "Ed. 1"
    return g("name", "") or g("id", "") or g("type", "")


def _short_attrs(el, skip=()) -> str:
    return " ".join(f"{k}={v}" for k, v in el.attrib.items() if k not in skip and not k.startswith("{"))[:120]


# ---------------------------------------------------------------------------------------------- sessão
class Session:
    """Um rascunho aberto: documento atual, linha de base (o CID do IED ou o arquivo enviado) e desfazer."""

    def __init__(self, ed: "CidEditor", sid: str, meta: dict, base: bytes, data: bytes):
        self.ed = ed
        self.sid = sid
        self.meta = meta
        self.base = base
        self.doc = Doc(data)
        self.bytes = data
        self._new_ind: dict = {}
        self.undo: list[tuple[bytes, str]] = []
        self.redo: list[tuple[bytes, str]] = []
        self.version = 0
        self.lock = threading.RLock()
        self.used = time.time()
        self._base_norm = None  # linha de base regravada pelo editor (para comparar e mostrar diferenças)

    def base_norm(self) -> bytes:
        if self._base_norm is None:
            try:
                self._base_norm = Doc(self.base).serialize()
            except SclError:
                self._base_norm = self.base
        return self._base_norm

    @property
    def dirty(self) -> bool:
        return self.bytes != self.base and self.bytes != self.base_norm()

    # ------------------------------------------------------------------ resumo
    def summary(self) -> dict:
        ix = self.doc.idx
        m = self.meta
        ied = None
        if m["origem"] == "ied":
            r = self.ed.mgr.ieds.get(m["ied"])
            ied = {"id": m["ied"], "nome": r.name if r else None, "estado": r.state if r else "removido",
                   "ied_scl": r.cfg.get("ied_scl") if r else None}
        lns = sum(len(v) for v in ix.lns.values())
        cont = Counter(_t(e) for e in ix.ids)
        return {
            "sessao": self.sid, "nome": m["nome"], "origem": m["origem"], "ied": ied,
            "alterado": self.dirty, "versao": self.version, "desfazer": len(self.undo), "refazer": len(self.redo),
            "proximo_desfazer": self.undo[-1][1] if self.undo else None,
            "proximo_refazer": self.redo[-1][1] if self.redo else None,
            "base_mudou": self.base_changed(), "tamanho": len(self.bytes), "historico": m.get("historico", [])[-60:],
            "ieds_scl": list(ix.ieds), "versao_scl": (self.doc.root.get("version", "") + self.doc.root.get("revision", ""))
            or "Ed. 1",
            "contagem": {"LDevice": sum(len(v) for v in ix.lds.values()), "LN": lns,
                         "DataSet": cont["DataSet"], "ReportControl": cont["ReportControl"],
                         "GSEControl": cont["GSEControl"], "LNodeType": len(ix.tpl["LNodeType"]),
                         "DOType": len(ix.tpl["DOType"]), "DAType": len(ix.tpl["DAType"]),
                         "EnumType": len(ix.tpl["EnumType"]), "elementos": len(ix.ids)},
        }

    def base_changed(self) -> bool:
        if self.meta["origem"] != "ied":
            return False
        r = self.ed.mgr.ieds.get(self.meta["ied"])
        if r is None:
            return False
        try:
            return _sha(self.ed.mgr.read_cid(r.cfg["cid_arquivo"])) != self.meta.get("base_sha")
        except OSError:
            return False

    # ------------------------------------------------------------------ árvore
    def children(self, vista: str, no: str, offset: int = 0) -> dict:
        ix = self.doc.idx
        if not no:
            if vista == "modelo":
                rows = [self._mrow(f"m/{n}") for n in ix.ieds]
            elif vista == "tipos":
                rows = [{"id": f"t/{k}", "rotulo": k, "tipo": "tpl", "filhos": bool(ix.tpl[k]),
                         "marcas": [{"t": str(len(ix.tpl[k])), "c": "neutral"}]} for k in GRUPOS_TIPOS]
            else:
                rows = [self._xrow(self.doc.root)]
        elif no.startswith("t/"):
            rows = [self._xrow(e) for e in ix.tpl.get(no[2:], {}).values()]
        elif no.startswith("m"):
            rows = self._mchildren(no)
        else:
            rows = [self._xrow(c) for c in _els(self.doc.el(no))]
        total = len(rows)
        return {"no": no, "total": total, "offset": offset, "filhos": rows[offset:offset + MAX_FILHOS]}

    def _xrow(self, el) -> dict:
        ix = self.doc.idx
        tag = _t(el)
        marks = []
        desc = el.get("desc", "")
        if tag in GRUPOS_TIPOS:
            n = len(ix.uses.get((tag, el.get("id", "")), []))
            marks.append({"t": f"{n} uso{'s' if n != 1 else ''}" if n else "sem uso", "c": "neutral" if n else "warn"})
            if tag == "LNodeType":
                marks.append({"t": el.get("lnClass", ""), "c": "info"})
            if tag == "DOType":
                marks.append({"t": el.get("cdc", ""), "c": "info"})
        elif tag in ("DO", "SDO"):
            marks.append({"t": el.get("type", ""), "c": "neutral"})
        elif tag in ("DA", "BDA"):
            if el.get("fc"):
                marks.append({"t": el.get("fc"), "c": "info"})
            marks.append({"t": el.get("bType", "") + (f" {el.get('type')}" if el.get("type") else ""), "c": "neutral"})
        elif tag in ("LN0", "LN"):
            marks.append({"t": el.get("lnType", ""), "c": "neutral"})
        elif tag == "DAI":
            v = next((c for c in _els(el) if _t(c) == "Val"), None)
            if v is not None:
                marks.append({"t": (v.text or "").strip()[:60], "c": "val"})
        elif tag == "FCDA":
            ok, why = self.fcda_status(el)
            if not ok:
                marks.append({"t": "não resolve", "c": "bad"})
        elif tag in ("ReportControl", "GSEControl"):
            marks.append({"t": el.get("datSet", "") or "sem DataSet", "c": "neutral"})
            if tag == "ReportControl":
                marks.append({"t": "BRCB" if el.get("buffered") in ("true", "1") else "URCB", "c": "info"})
        elif tag == "DataSet":
            n = sum(1 for c in _els(el) if _t(c) == "FCDA")
            marks.append({"t": f"{n} FCDA", "c": "neutral"})
        if tag == "Header":
            desc = _short_attrs(el)
        return {"id": ix.ids[el], "rotulo": label(el) if label(el) else tag, "tag": tag, "tipo": TIPO.get(tag, "el"),
                "desc": desc, "filhos": bool(_els(el)), "marcas": marks}

    # ------------------------------------------------------------------ modelo
    def mresolve(self, mid: str) -> dict:
        """Resolve um identificador do modelo para os elementos do IED, do template e da instância."""
        parts = mid.split("/")
        ix = self.doc.idx
        r = {"id": mid, "nivel": len(parts) - 1, "partes": parts}
        if len(parts) < 2 or parts[0] != "m":
            raise PointError(f"nó do modelo inválido: '{mid}'", status=422)
        ied = ix.ieds.get(parts[1])
        if ied is None:
            raise PointError(f"IED '{parts[1]}' não existe no arquivo", status=404)
        r["ied"] = ied
        if len(parts) == 2:
            r["kind"] = "ied"
            return r
        ld = ix.lds.get(parts[1], {}).get(parts[2])
        if ld is None:
            raise PointError(f"LD '{parts[2]}' não existe", status=404)
        r["ld"] = ld
        if len(parts) == 3:
            r["kind"] = "ld"
            return r
        ln = ix.lns.get((parts[1], parts[2]), {}).get(parts[3])
        if ln is None:
            raise PointError(f"LN '{parts[3]}' não existe em {parts[2]}", status=404)
        r["ln"] = ln
        if len(parts) == 4:
            r["kind"] = "ln"
            return r
        segs = parts[4:]
        chain = []
        cur = dict(ix.expand("LNodeType", ln.get("lnType", "")))
        fc = ""
        for s in segs:
            t = cur.get(s)
            if t is None:
                raise PointError(f"'{s}' não existe no tipo de {'/'.join(parts[:4 + len(chain)])}", status=404)
            chain.append(t)
            fc = t.get("fc", fc)
            cur = dict(ix.tchildren(t))
        r.update(kind={"DO": "do", "SDO": "sdo", "DA": "da", "BDA": "bda"}[_t(chain[-1])], segs=segs,
                 tchain=chain, tel=chain[-1], fc=fc or None, inst=self._inst(ln, segs))
        return r

    def _inst(self, ln, segs) -> list:
        """Elementos de instância (DOI, SDI…, DAI) existentes ao longo do caminho; para no primeiro ausente."""
        out = []
        cur = ln
        for i, s in enumerate(segs):
            want = ("DOI",) if i == 0 else ("SDI", "DAI")
            nxt = next((c for c in _els(cur) if _t(c) in want and c.get("name") == s and not c.get("ix")), None)
            if nxt is None:
                break
            out.append(nxt)
            cur = nxt
        return out

    def _mrow(self, mid: str, r: dict | None = None, inst_parent=None, have_parent=False) -> dict:
        ix = self.doc.idx
        parts = mid.split("/")
        nm = parts[-1]
        if r is None and len(parts) <= 4:
            r = self.mresolve(mid)
        if len(parts) == 2:
            el = r["ied"]
            return {"id": mid, "rotulo": nm, "tipo": "ied", "desc": el.get("desc", ""), "filhos": True,
                    "marcas": [{"t": " ".join(x for x in (el.get("manufacturer"), el.get("type")) if x), "c": "neutral"}]}
        if len(parts) == 3:
            el = r["ld"]
            return {"id": mid, "rotulo": nm, "tipo": "ld", "desc": el.get("desc", ""), "filhos": True,
                    "marcas": [{"t": parts[1] + nm, "c": "neutral"}]}
        if len(parts) == 4:
            el = r["ln"]
            ok = el.get("lnType", "") in ix.tpl["LNodeType"]
            return {"id": mid, "rotulo": nm, "tipo": "ln", "desc": el.get("desc", ""), "filhos": True,
                    "marcas": [{"t": el.get("lnType", ""), "c": "neutral" if ok else "bad"}]}
        tel = r["tel"]
        tag = _t(tel)
        marks = []
        inst = r.get("inst_el")
        desc = (inst.get("desc", "") if inst is not None else "") or tel.get("desc", "")
        if tag in ("DO", "SDO"):
            dt = ix.tpl["DOType"].get(tel.get("type", ""))
            marks.append({"t": dt.get("cdc", "") if dt is not None else f"tipo {tel.get('type', '')} ausente",
                          "c": "info" if dt is not None else "bad"})
            kids = bool(dt is not None and _els(dt))
        else:
            if tel.get("fc"):
                marks.append({"t": tel.get("fc"), "c": "info"})
            marks.append({"t": tel.get("bType", ""), "c": "neutral"})
            kids = tel.get("bType") == "Struct"
            if tel.get("count") not in (None, "", "0"):
                marks.append({"t": f"[{tel.get('count')}]", "c": "neutral"})
            if not kids:
                v, origem = self._value(tel, inst)
                if v is not None:
                    marks.append({"t": v[:60], "c": "val" if origem == "instância" else "muted"})
        return {"id": mid, "rotulo": nm, "tipo": TIPO.get(tag, "da"), "desc": desc, "filhos": kids, "marcas": marks}

    @staticmethod
    def _value(tel, inst) -> tuple[str | None, str | None]:
        if inst is not None and _t(inst) == "DAI":
            v = next((c for c in _els(inst) if _t(c) == "Val"), None)
            if v is not None:
                return (v.text or "").strip(), "instância"
        v = next((c for c in _els(tel) if _t(c) == "Val"), None)
        if v is not None:
            return (v.text or "").strip(), "tipo"
        return None, None

    def _mchildren(self, mid: str) -> list:
        ix = self.doc.idx
        parts = mid.split("/")
        if mid == "m":
            return [self._mrow(f"m/{n}") for n in ix.ieds]
        r = self.mresolve(mid)
        if r["kind"] == "ied":
            return [self._mrow(f"{mid}/{k}") for k in ix.lds.get(parts[1], {})]
        if r["kind"] == "ld":
            return [self._mrow(f"{mid}/{k}") for k in ix.lns.get((parts[1], parts[2]), {})]
        if r["kind"] == "ln":
            ln = r["ln"]
            dois = {c.get("name"): c for c in _els(ln) if _t(c) == "DOI"}
            rows = []
            for nm, do in ix.expand("LNodeType", ln.get("lnType", "")):
                rows.append(self._mrow(f"{mid}/{nm}", {"tel": do, "inst_el": dois.get(nm)}))
            extra = [c for c in _els(ln) if _t(c) != "DOI"]
            rows += [self._xrow(c) for c in extra]
            return rows
        inst = r["inst"]
        parent_inst = inst[-1] if len(inst) == len(r["segs"]) else None
        sub = {}
        if parent_inst is not None:
            sub = {c.get("name"): c for c in _els(parent_inst) if _t(c) in ("SDI", "DAI") and not c.get("ix")}
        return [self._mrow(f"{mid}/{nm}", {"tel": c, "inst_el": sub.get(nm)}) for nm, c in ix.tchildren(r["tel"])]

    # ------------------------------------------------------------------ FCDA
    def fcda_status(self, f) -> tuple[bool, str]:
        ix = self.doc.idx
        ied = ix.ied_of(f)
        if ied is None:
            return False, "FCDA fora de um IED"
        nm = ied.get("name", "")
        ln = ix.lns.get((nm, f.get("ldInst", "")), {}).get(
            f.get("prefix", "") + f.get("lnClass", "") + f.get("lnInst", ""))
        if (nm, f.get("ldInst", "")) not in ix.lns:
            return False, f"LD '{f.get('ldInst', '')}' não existe"
        if ln is None:
            return False, "LN não existe no LD"
        segs = [s for s in f.get("doName", "").split(".") + f.get("daName", "").split(".") if s]
        if not segs:
            return False, "doName vazio"
        cur = dict(ix.expand("LNodeType", ln.get("lnType", "")))
        t = None
        for s in segs:
            t = cur.get(s)
            if t is None:
                return False, f"'{s}' não existe no tipo"
            cur = dict(ix.tchildren(t))
        fc = f.get("fc", "")
        if not fc:
            return False, "sem fc"
        if f.get("daName"):
            top = next((x for x in self._chain_fc(ln, segs) if x), "")
            if top != fc:
                return False, f"FC do atributo é {top or '?'}, FCDA pede {fc}"
            return True, ""
        if fc not in self._fcs_below(t):
            return False, f"o objeto não tem atributos com FC {fc}"
        return True, ""

    def _chain_fc(self, ln, segs) -> list:
        ix = self.doc.idx
        cur = dict(ix.expand("LNodeType", ln.get("lnType", "")))
        out = []
        for s in segs:
            t = cur.get(s)
            if t is None:
                break
            out.append(t.get("fc", ""))
            cur = dict(ix.tchildren(t))
        return out

    def _fcs_below(self, tel, depth=0) -> list:
        ix = self.doc.idx
        if tel.get("fc"):
            return [tel.get("fc")]
        out = []
        if depth < 10:
            for _, c in ix.tchildren(tel):
                for fc in self._fcs_below(c, depth + 1):
                    if fc not in out:
                        out.append(fc)
        return out

    def _datasets_with(self, ied_name: str, ld: str, ln: str, segs: list, fc) -> list:
        out = []
        for fld, fln, fsegs, ffc, f, ds in self.doc.idx.fcdas.get(ied_name, []):
            if fld != ld or fln != ln:
                continue
            n = min(len(fsegs), len(segs))
            if fsegs[:n] != segs[:n]:
                continue
            if fc and ffc != fc:
                continue
            out.append({"dataset": self.doc.xid(ds), "dataset_nome": self._ds_label(ds), "fcda": self.doc.xid(f),
                        "texto": fcda_text(f)})
        return out

    def _ds_label(self, ds) -> str:
        ln = self.doc.idx.parent.get(ds)
        ld = self.doc.idx.parent.get(ln) if ln is not None else None
        return f"{ld.get('inst', '') if ld is not None else ''}/{ln_name(ln) if ln is not None else ''}.{ds.get('name', '')}"

    # ------------------------------------------------------------------ detalhes
    def detail(self, no: str) -> dict:
        if no.startswith("t/"):
            k = no[2:]
            return {"id": no, "classe": "grupo", "tipo": "tpl", "rotulo": k, "caminho": [],
                    "total": len(self.doc.idx.tpl.get(k, {}))}
        if no.startswith("m"):
            return self._mdetail(no)
        return self._xdetail(no)

    def _attr_name(self, k: str) -> str:
        if k.startswith("{"):
            uri, loc = k[1:].split("}", 1)
            pfx = next((p for p, u in self.doc.ns if u == uri and p), None)
            return f"{pfx}:{loc}" if pfx else k
        return k

    def _attr_key(self, k: str) -> str:
        if ":" in k and not k.startswith("{"):
            pfx, loc = k.split(":", 1)
            uri = next((u for p, u in self.doc.ns if p == pfx), None)
            if uri is None:
                raise PointError(f"prefixo de namespace '{pfx}' não declarado no arquivo", status=422)
            return f"{{{uri}}}{loc}"
        return k

    def _xdetail(self, no: str) -> dict:
        ix = self.doc.idx
        chain = self.doc.at(no)
        el = chain[-1]
        tag = _t(el)
        frag = self.doc.fragment(el)
        d = {
            "id": no, "classe": "xml", "tag": tag, "tipo": TIPO.get(tag, "el"), "rotulo": label(el) or tag,
            "caminho": [{"id": ix.ids[a], "rotulo": f"{_t(a)} {label(a)}".strip()} for a in chain[:-1]],
            "atributos": [[self._attr_name(k), v] for k, v in el.attrib.items()],
            "texto": (el.text or "").strip() if tag in TEXTO or (el.text and el.text.strip() and not _els(el)) else None,
            "aceita_texto": tag in TEXTO,
            "filhos": dict(Counter(_t(c) for c in _els(el))),
            "permitidos": [x for o in ORDEM.get(tag, []) for x in (o if isinstance(o, tuple) else (o,))],
            "sugeridos": sorted(set(MODELOS.get(tag, {})) - set(el.attrib) | ({"desc"} - set(el.attrib))),
            "modelos": {t: dict(MODELOS.get(t, {}), **({"#texto": ""} if t in TEXTO else {}))
                        for o in ORDEM.get(tag, []) for t in (o if isinstance(o, tuple) else (o,))},
            "xml": frag[:MAX_XML], "xml_truncado": len(frag) > MAX_XML, "xml_tamanho": len(frag),
            "refs": [], "usado_por": None, "modelo": self.model_id_of(el), "pode_mover": el is not self.doc.root,
        }
        tr = ix.type_ref(el)
        if tr:
            tel = ix.tpl[tr[0]].get(tr[1])
            d["refs"].append({"rotulo": f"{tr[0]} {tr[1]}", "no": ix.ids[tel] if tel is not None else None,
                              "erro": None if tel is not None else "tipo inexistente"})
        if tag in GRUPOS_TIPOS:
            us = ix.uses.get((tag, el.get("id", "")), [])
            d["usado_por"] = [{"no": ix.ids[u], "rotulo": self._where(u)} for u in us[:300]]
            d["usado_total"] = len(us)
            if tag == "EnumType":
                d["enum"] = [[c.get("ord", ""), (c.text or "").strip()] for c in _els(el) if _t(c) == "EnumVal"]
        if tag == "DataSet":
            d["fcdas"] = []
            for f in (c for c in _els(el) if _t(c) == "FCDA"):
                ok, why = self.fcda_status(f)
                d["fcdas"].append({"no": ix.ids[f], "texto": fcda_text(f), "ok": ok, "motivo": why,
                                   "modelo": self.model_id_of(f)})
            ln = ix.parent.get(el)
            d["usado_por"] = [{"no": ix.ids[c], "rotulo": f"{_t(c)} {c.get('name', '')}"}
                              for c in _els(ln) if _t(c) in ("ReportControl", "GSEControl", "LogControl",
                                                             "SampledValueControl") and c.get("datSet") == el.get("name")]
        if tag == "FCDA":
            ok, why = self.fcda_status(el)
            d["fcda"] = {"ok": ok, "motivo": why}
        if tag in ("ReportControl", "GSEControl", "LogControl", "SampledValueControl") and el.get("datSet"):
            ln = ix.parent.get(el)
            ds = next((c for c in _els(ln) if _t(c) == "DataSet" and c.get("name") == el.get("datSet")), None)
            d["refs"].append({"rotulo": f"DataSet {el.get('datSet')}", "no": ix.ids[ds] if ds is not None else None,
                              "erro": None if ds is not None else "DataSet inexistente neste LN"})
        if tag == "ReportControl":
            d["rcb"] = self._rcb_info(el)
        return d

    # ------------------------------------------------------------------ formulário do ReportControl
    def _rcb_info(self, el) -> dict:
        """O que o formulário do RCB precisa: atributos efetivos (com os padrões do esquema), TrgOps, OptFields,
        RptEnabled/ClientLN, DataSets do LN, capacidades do IED (Services) e a análise das opções."""
        ix = self.doc.idx
        ln = ix.parent.get(el)
        ld = ix.parent.get(ln) if ln is not None else None
        ied = ix.ied_of(el)
        ed2 = self.doc.root.get("version", "") >= "2007"
        sub = {_t(c): c for c in _els(el)}
        tg, of, re_ = sub.get("TrgOps"), sub.get("OptFields"), sub.get("RptEnabled")

        def flags(node, nomes, padrao):
            out = {}
            for k in nomes:
                v = node.get(k) if node is not None else None
                out[k] = {"valor": (v if v is not None else padrao.get(k, "false")) in ("true", "1"),
                          "explicito": v is not None, "padrao": padrao.get(k, "false") == "true"}
            return out

        trg = flags(tg, RCB_TRG, {"gi": "true"})
        nomes_opt = RCB_OPT_ED2 if ed2 else RCB_OPT_ED1
        nomes_opt = [k for k in RCB_OPT_ED1 if k in nomes_opt or (of is not None and of.get(k) is not None)]
        opt = flags(of, nomes_opt, {"bufOvfl": "true"} if ed2 else {})
        at = {k: el.get(k) for k in RCB_ATTRS}
        buffered = (at["buffered"] or "false") in ("true", "1")
        ints = {}
        for k in ("intgPd", "bufTime", "confRev"):
            try:
                ints[k] = int(at[k]) if at[k] not in (None, "") else 0
            except ValueError:
                ints[k] = None
        try:
            mx = int(re_.get("max", "1") or 1) if re_ is not None else 1
        except ValueError:
            mx = None
        clients = [{k: c.get(k, "") for k in CLIENTLN_ATTRS} for c in (_els(re_) if re_ is not None else [])
                   if _t(c) == "ClientLN"]

        # DataSets do LN e o que os membros conseguem disparar
        irmaos = _els(ln) if ln is not None else []
        datasets = [{"nome": c.get("name", ""), "n": sum(1 for f in _els(c) if _t(f) == "FCDA"), "no": ix.ids[c],
                     "desc": c.get("desc", "")} for c in irmaos if _t(c) == "DataSet"]
        ds = next((c for c in irmaos if _t(c) == "DataSet" and c.get("name") == at["datSet"]), None) \
            if at["datSet"] else None
        membros = None
        if ds is not None:
            fcdas = [f for f in _els(ds) if _t(f) == "FCDA"]
            membros = {"total": len(fcdas), "dchg": 0, "qchg": 0, "dupd": 0}
            for f in fcdas:
                for k in self._fcda_trg(f):
                    membros[k] += 1

        # capacidades declaradas pelo IED
        servicos = None
        if ied is not None:
            srv = next((c for c in _els(ied) if _t(c) == "Services"), None)
            if srv is None:
                srv = next((s for ap in _els(ied) if _t(ap) == "AccessPoint" for s in _els(ap)
                            if _t(s) == "Services"), None)
            if srv is not None:
                rs = next((c for c in _els(srv) if _t(c) == "ReportSettings"), None)
                cr = next((c for c in _els(srv) if _t(c) == "ConfReportControl"), None)
                servicos = {"report_settings": dict(rs.attrib) if rs is not None else None,
                            "conf_report_control": dict(cr.attrib) if cr is not None else None}
        total_rcbs = sum(1 for _ in ied.iter(self.doc.q("ReportControl"))) if ied is not None else 0

        avisos = []

        def av(nivel, campo, msg):
            avisos.append({"nivel": nivel, "campo": campo, "msg": msg})

        nome = at["name"] or ""
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", nome):
            av("erro", "name", "O nome deve começar com letra e ter só letras, números e sublinhado.")
        elif len(nome) > 30:
            av("aviso", "name", f"Nome com {len(nome)} caracteres: com o sufixo de instância (01, 02…) passa do "
                                "limite usual de 32 do nome MMS.")
        if sum(1 for c in irmaos if _t(c) == "ReportControl" and c.get("name") == nome) > 1:
            av("erro", "name", "Há outro ReportControl com este nome no mesmo LN.")
        if not at["datSet"]:
            av("aviso", "datSet", "Sem DataSet: o RCB existe, mas não tem o que enviar.")
        elif ds is None:
            av("erro", "datSet", f"O DataSet '{at['datSet']}' não existe neste LN.")
        elif not membros["total"]:
            av("aviso", "datSet", "O DataSet está vazio.")
        if not at["rptID"]:
            av("info", "rptID", "rptID vazio: o servidor usa a referência do RCB como identificação do report.")
        elif len(at["rptID"]) > 129:
            av("erro", "rptID", f"rptID com {len(at['rptID'])} caracteres; o limite é 129 (VisString129).")
        for k in ("intgPd", "bufTime", "confRev"):
            if ints[k] is None or not 0 <= ints[k] <= 2**32 - 1:
                av("erro", k, f"{k} deve ser inteiro entre 0 e 4294967295.")
        if mx is None or not 0 <= mx <= 99:
            av("erro", "max", "RptEnabled max deve ser inteiro entre 0 e 99.")
        elif mx == 0:
            av("aviso", "max", "max = 0: nenhuma instância declarada (algumas ferramentas exportam assim). Clientes "
                               "não conseguem habilitar o RCB; no simulador, conforme o perfil, pode não sobrar "
                               "nenhuma instância. Use pelo menos 1.")
        elif mx < 3:
            av("info", "max", f"max = {mx}: só {mx} cliente(s) ao mesmo tempo; 3 SCADAs em paralelo precisam de 3.")
        t = {k: v["valor"] for k, v in trg.items()}
        if not any(t.values()):
            av("erro", "trgops", "Nenhum gatilho ligado: o RCB nunca envia report.")
        if t["period"] and ints["intgPd"] == 0:
            av("aviso", "intgPd", "TrgOps period ligado com intgPd = 0: não sai report de integridade. Defina o "
                                  "período em ms.")
        if ints["intgPd"] and not t["period"]:
            av("info", "intgPd", f"intgPd = {ints['intgPd']} ms sem efeito: o gatilho period está desligado.")
        if membros and membros["total"]:
            for k, oque in (("dchg", "mudança de dado"), ("qchg", "mudança de qualidade"), ("dupd", "atualização")):
                n = membros[k]
                if t[k] and not n:
                    av("aviso", k, f"{k} ligado, mas nenhum membro do DataSet dispara por {oque}.")
                elif not t[k] and n and k != "dupd":
                    av("aviso", k, f"{k} desligado: {n} de {membros['total']} membro(s) do DataSet disparam por "
                                   f"{oque} e não vão gerar report espontâneo.")
        if not t["gi"]:
            av("aviso", "gi", "GI desligado: o cliente não consegue pedir interrogação geral (o SAGE pede ao "
                              "conectar).")
        o = {k: v["valor"] for k, v in opt.items()}
        if not buffered and o.get("entryID"):
            av("info", "entryID", "entryID só tem efeito em RCB com buffer (BRCB).")
        if buffered and not o.get("entryID"):
            av("aviso", "entryID", "BRCB sem entryID no report: o cliente não consegue retomar o buffer de onde "
                                   "parou ao reconectar.")
        if not o.get("seqNum"):
            av("info", "seqNum", "Sem seqNum o cliente não detecta report perdido ou repetido.")
        if not o.get("reasonCode"):
            av("info", "reasonCode", "Sem reasonCode o cliente não sabe se o report veio de GI, integridade ou "
                                     "mudança.")
        if not o.get("timeStamp"):
            av("info", "timeStamp", "Sem timeStamp o report não leva a hora em que foi gerado.")
        if mx is not None and len(clients) > mx:
            av("erro", "max", f"{len(clients)} ClientLN para max = {mx}: cada cliente reservado ocupa uma instância.")
        crmax = ((servicos or {}).get("conf_report_control") or {}).get("max", "")
        if crmax.isdigit() and total_rcbs > int(crmax):
            av("aviso", "max", f"O IED tem {total_rcbs} ReportControl, acima do ConfReportControl max = {crmax} "
                               "declarado em Services.")
        if ints["confRev"] == 0:
            av("info", "confRev", "confRev = 0; a norma pede incrementar a cada mudança do DataSet ou do RCB.")
        if not buffered and ints["bufTime"]:
            av("info", "bufTime", "Em URCB o bufTime só agrupa eventos próximos; não há buffer se o cliente cair.")

        prefixo = f"{ld.get('inst', '') if ld is not None else ''}/{ln_name(ln) if ln is not None else ''}"
        return {
            "edicao": 2 if ed2 else 1, "atributos": at, "buffered": buffered,
            "trgops": trg, "optfields": opt, "tem": {k: k in sub for k in ("TrgOps", "OptFields", "RptEnabled")},
            "rptenabled": {"max": re_.get("max", "1") if re_ is not None else "1",
                           "desc": re_.get("desc", "") if re_ is not None else ""},
            "clientlns": clients, "datasets": datasets, "membros": membros, "servicos": servicos,
            "total_rcbs_ied": total_rcbs, "avisos": avisos, "ieds": sorted(ix.ieds),
            "rptid_sugerido": f"{prefixo}.{'BR' if buffered else 'RP'}.{nome}",
        }

    def _fcda_trg(self, f) -> set:
        """Gatilhos (dchg/qchg/dupd) que os atributos cobertos por uma FCDA declaram no tipo."""
        ix = self.doc.idx
        ied = ix.ied_of(f)
        if ied is None:
            return set()
        ln = ix.lns.get((ied.get("name", ""), f.get("ldInst", "")), {}).get(
            f.get("prefix", "") + f.get("lnClass", "") + f.get("lnInst", ""))
        if ln is None:
            return set()
        segs = [s for s in f.get("doName", "").split(".") + f.get("daName", "").split(".") if s]
        cur = dict(ix.expand("LNodeType", ln.get("lnType", "")))
        t = top = None
        for s in segs:
            t = cur.get(s)
            if t is None:
                return set()
            if top is None and _t(t) == "DA":
                top = t
            cur = dict(ix.tchildren(t))
        if top is not None:  # aponta para um DA (ou BDA dentro dele): vale o que o DA declara
            return {k for k in ("dchg", "qchg", "dupd") if top.get(k) in ("true", "1")}
        out = set()
        fc = f.get("fc", "")

        def walk(tel, depth=0):
            for _, c in ix.tchildren(tel):
                if _t(c) == "DA":
                    if c.get("fc") == fc:
                        out.update(k for k in ("dchg", "qchg", "dupd") if c.get(k) in ("true", "1"))
                elif depth < 8:
                    walk(c, depth + 1)
        if t is not None:
            walk(t)
        return out

    def _op_rcb(self, body):
        """Grava o formulário do ReportControl de uma vez: atributos, TrgOps, OptFields, RptEnabled e ClientLN."""
        no = self._need(body, "no")
        el = self.doc.el(no)
        if _t(el) != "ReportControl":
            raise PointError("o elemento não é um ReportControl", status=422)
        ix = self.doc.idx
        ln = ix.parent.get(el)
        at = body.get("atributos") or {}
        if not isinstance(at, dict):
            raise PointError("'atributos' deve ser um objeto", status=422)
        mud = []
        for k, v in at.items():
            if k not in RCB_ATTRS:
                raise PointError(f"atributo '{k}' não é do ReportControl; use {RCB_ATTRS}", status=422)
            v = None if v is None else ("true" if v is True else "false" if v is False else str(v).strip())
            if k == "name":
                if not v or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", v):
                    raise PointError("nome inválido: comece com letra; só letras, números e sublinhado", status=422)
                if any(c is not el and _t(c) == "ReportControl" and c.get("name") == v for c in _els(ln)):
                    raise PointError(f"já existe ReportControl '{v}' neste LN", status=409)
            elif k in ("intgPd", "bufTime", "confRev") and v not in (None, ""):
                if not v.isdigit() or int(v) > 2**32 - 1:
                    raise PointError(f"{k} deve ser inteiro entre 0 e 4294967295", status=422)
                v = str(int(v))
            elif k in ("buffered", "indexed") and v not in (None, "", "true", "false"):
                raise PointError(f"{k} deve ser true ou false", status=422)
            elif k == "datSet" and v and not body.get("confirmar") and not any(
                    _t(c) == "DataSet" and c.get("name") == v for c in _els(ln)):
                raise PointError(f"DataSet '{v}' não existe neste LN; gravar mesmo assim?", status=409,
                                 code="confirmar")
            if v in (None, "") and k != "rptID":
                v = None
            if el.get(k) != v:
                mud.append(k)
                if v is None:
                    el.attrib.pop(k, None)
                else:
                    el.set(k, v)

        def sub(tag):
            c = next((c for c in _els(el) if _t(c) == tag), None)
            if c is None:
                c = ET.Element(self.doc.q(tag))
                self._insert(el, c)
            return c

        for chave, tag, validos in (("trgops", "TrgOps", RCB_TRG), ("optfields", "OptFields", RCB_OPT_ED1)):
            fl = body.get(chave)
            if fl is None:
                continue
            if not isinstance(fl, dict):
                raise PointError(f"'{chave}' deve ser um objeto {{opção: true/false}}", status=422)
            node = sub(tag)
            for k, v in fl.items():
                if k not in validos:
                    raise PointError(f"opção '{k}' não existe em {tag}; use {validos}", status=422)
                v = None if v is None else ("true" if v in (True, "true", "1", 1) else "false")
                if node.get(k) != v:
                    mud.append(f"{tag}.{k}")
                    if v is None:
                        node.attrib.pop(k, None)
                    else:
                        node.set(k, v)
        rep = body.get("rptenabled")
        cls = body.get("clientlns")
        if rep is not None or cls is not None:
            node = sub("RptEnabled")
            if rep is not None:
                mx = str(rep.get("max", node.get("max", "1"))).strip()
                if not mx.isdigit() or int(mx) > 99:
                    raise PointError("RptEnabled max deve ser inteiro entre 0 e 99", status=422)
                mx = str(int(mx))
                if node.get("max") != mx:
                    mud.append("max")
                    node.set("max", mx)
                if "desc" in rep:
                    d = str(rep.get("desc") or "")
                    if (node.get("desc") or "") != d:
                        mud.append("RptEnabled.desc")
                        if d:
                            node.set("desc", d)
                        else:
                            node.attrib.pop("desc", None)
            if cls is not None:
                if not isinstance(cls, list):
                    raise PointError("'clientlns' deve ser uma lista", status=422)
                novos = []
                for c in cls:
                    if not isinstance(c, dict) or not str(c.get("iedName", "")).strip() \
                            or not str(c.get("lnClass", "")).strip():
                        raise PointError("cada ClientLN precisa de iedName e lnClass", status=422)
                    novos.append({k: str(c.get(k, "")).strip() for k in CLIENTLN_ATTRS
                                  if str(c.get(k, "")).strip() or k in ("ldInst", "lnInst")})
                velhos = [c for c in _els(node) if _t(c) == "ClientLN"]
                if [dict(c.attrib) for c in velhos] != novos:
                    mud.append(f"ClientLN ({len(novos)})")
                    for c in velhos:
                        self._remove(node, c)
                    for a in novos:
                        self._insert(node, ET.Element(self.doc.q("ClientLN"), a))
            if int(node.get("max", "1") or 1) < sum(1 for c in _els(node) if _t(c) == "ClientLN"):
                raise PointError("há mais ClientLN do que RptEnabled max", status=422)
        return {"no": no, "mensagem": f"ReportControl {el.get('name', '')}: " + (", ".join(mud) or "nada mudou")}

    def _remove(self, parent, el):
        kids = _els(parent)
        i = kids.index(el)
        if i == len(kids) - 1 and i > 0:
            kids[i - 1].tail = el.tail
        parent.remove(el)
        if not _els(parent):
            parent.text = None

    def _where(self, el) -> str:
        ix = self.doc.idx
        tag = _t(el)
        p = ix.parent.get(el)
        if tag in ("LN0", "LN"):
            ld = p
            ied = ix.ied_of(el)
            return f"{ied.get('name', '') if ied is not None else ''}{ld.get('inst', '')}/{ln_name(el)}"
        return f"{_t(p)} {p.get('id', '') if p is not None else ''} › {tag} {el.get('name', '')}"

    def model_id_of(self, el) -> str | None:
        """Identificador do modelo equivalente a um elemento XML (LDevice, LN, DOI/SDI/DAI, FCDA)."""
        ix = self.doc.idx
        tag = _t(el)
        if tag == "FCDA":
            ied = ix.ied_of(el)
            if ied is None or not self.fcda_status(el)[0]:
                return None
            segs = [s for s in el.get("doName", "").split(".") + el.get("daName", "").split(".") if s]
            ln = el.get("prefix", "") + el.get("lnClass", "") + el.get("lnInst", "")
            return "/".join(["m", ied.get("name", ""), el.get("ldInst", ""), ln] + segs)
        names = []
        cur = el
        while cur is not None and _t(cur) in ("DOI", "SDI", "DAI"):
            if cur.get("ix"):
                return None
            names.insert(0, cur.get("name", ""))
            cur = ix.parent.get(cur)
        if cur is None:
            return None
        if _t(cur) in ("LN0", "LN"):
            ld = ix.parent.get(cur)
            ied = ix.ied_of(cur)
            if ied is None or ld is None or _t(ld) != "LDevice":
                return None
            mid = "/".join(["m", ied.get("name", ""), ld.get("inst", ""), ln_name(cur)] + names)
            try:
                self.mresolve(mid)
            except PointError:
                return None
            return mid
        if tag == "LDevice":
            ied = ix.ied_of(el)
            return f"m/{ied.get('name', '')}/{el.get('inst', '')}" if ied is not None else None
        if tag == "IED":
            return f"m/{el.get('name', '')}"
        return None

    def _mdetail(self, no: str) -> dict:
        ix = self.doc.idx
        r = self.mresolve(no)
        parts = r["partes"]
        crumbs = [{"id": "/".join(parts[:i + 1]), "rotulo": parts[i]} for i in range(1, len(parts) - 1)]
        d = {"id": no, "classe": "modelo", "nivel": r["kind"], "rotulo": parts[-1], "caminho": crumbs}
        if r["kind"] in ("ied", "ld", "ln"):
            el = r[r["kind"]]
            d.update(tipo=r["kind"], xml_no=ix.ids[el], desc=el.get("desc", ""),
                     atributos=[[self._attr_name(k), v] for k, v in el.attrib.items()])
            if r["kind"] == "ln":
                lt = ix.tpl["LNodeType"].get(el.get("lnType", ""))
                d["tipo_no"] = ix.ids[lt] if lt is not None else None
                d["ref"] = f"{parts[1]}{parts[2]}/{parts[3]}"
                d["lnType"] = el.get("lnType", "")
                d["datasets"] = [{"no": ix.ids[c], "rotulo": c.get("name", ""), "n": len(_els(c))}
                                 for c in _els(el) if _t(c) == "DataSet"]
                d["rcbs"] = [{"no": ix.ids[c], "rotulo": c.get("name", ""), "datSet": c.get("datSet", "")}
                             for c in _els(el) if _t(c) == "ReportControl"]
                d["objetos"] = [self._mrow(f"{no}/{nm}", {"tel": do, "inst_el": next(
                    (c for c in _els(el) if _t(c) == "DOI" and c.get("name") == nm), None)})
                    for nm, do in ix.expand("LNodeType", el.get("lnType", ""))]
            if r["kind"] == "ied":
                d["ref"] = parts[1]
                d["lds"] = list(ix.lds.get(parts[1], {}))
            if r["kind"] == "ld":
                d["ref"] = f"{parts[1]}{parts[2]}"
            return d
        tel = r["tel"]
        inst = r["inst"]
        full = len(inst) == len(r["segs"])
        inst_el = inst[-1] if full else None
        segs = r["segs"]
        ref = f"{parts[1]}{parts[2]}/{parts[3]}." + ".".join(segs)
        tr = ix.type_ref(tel)
        tdef = ix.tpl[tr[0]].get(tr[1]) if tr else None
        d.update(
            tipo=TIPO.get(_t(tel), "da"), ref=ref + (f" [{r['fc']}]" if r["fc"] and r["kind"] in ("da", "bda") else ""),
            mms=f"{parts[1]}{parts[2]}/{parts[3]}${r['fc'] or '??'}$" + "$".join(segs),
            tag_tipo=_t(tel), template_no=ix.ids[tel], tipo_no=ix.ids[tdef] if tdef is not None else None,
            tipo_ref=f"{tr[0]} {tr[1]}" if tr else None, tipo_erro=bool(tr and tdef is None),
            instancia_no=ix.ids[inst_el] if inst_el is not None else None,
            instancia_parcial=[ix.ids[e] for e in inst],
            fc=r["fc"], desc_tipo=tel.get("desc", ""),
            desc=inst_el.get("desc", "") if inst_el is not None else "",
            atributos_tipo=[[k, v] for k, v in tel.attrib.items()],
        )
        d["em_datasets"] = self._datasets_with(parts[1], parts[2], parts[3], segs, r["fc"])
        d["datasets_disponiveis"] = [{"no": ix.ids[ds], "rotulo": self._ds_label(ds)}
                                     for ld_inst, ld in ix.lds.get(parts[1], {}).items()
                                     for lnel in ix.lns[(parts[1], ld_inst)].values()
                                     for ds in _els(lnel) if _t(ds) == "DataSet"]
        if r["kind"] in ("do", "sdo"):
            dt = ix.tpl["DOType"].get(tel.get("type", ""))
            d["cdc"] = dt.get("cdc", "") if dt is not None else None
            d["fcs"] = self._fcs_below(tel)
            sub = {c.get("name"): c for c in _els(inst_el) if _t(c) in ("SDI", "DAI")} if inst_el is not None else {}
            d["atributos_modelo"] = [self._mrow(f"{no}/{nm}", {"tel": c, "inst_el": sub.get(nm)})
                                     for nm, c in ix.tchildren(tel)]
        else:
            leaf = tel.get("bType") != "Struct"
            d["bType"] = tel.get("bType", "")
            d["folha"] = leaf
            d["fcs"] = [r["fc"]] if r["fc"] else []
            if leaf:
                v, origem = self._value(tel, inst_el)
                d["valor"] = v
                d["valor_origem"] = origem
                d["valor_tipo"] = self._value(tel, None)[0]
                if tel.get("bType") == "Enum":
                    d["opcoes"] = [t for _, t in ix.enum_texts(tel.get("type", "")) or []]
                    d["enum"] = [[o, t] for o, t in ix.enum_texts(tel.get("type", "")) or []]
                elif tel.get("bType") == "BOOLEAN":
                    d["opcoes"] = ["false", "true"]
                if inst_el is not None:
                    d["instancia_atributos"] = [[self._attr_name(k), v] for k, v in inst_el.attrib.items()]
                d["array"] = tel.get("count") not in (None, "", "0")
            else:
                sub = {c.get("name"): c for c in _els(inst_el) if _t(c) in ("SDI", "DAI")} if inst_el is not None else {}
                d["atributos_modelo"] = [self._mrow(f"{no}/{nm}", {"tel": c, "inst_el": sub.get(nm)})
                                         for nm, c in ix.tchildren(tel)]
            top = next((t for t in r["tchain"] if _t(t) == "DA"), tel)
            d["trg"] = [k for k in ("dchg", "qchg", "dupd") if top.get(k) in ("true", "1")]
        return d

    # ------------------------------------------------------------------ localizar / buscar
    def locate(self, no: str, vista: str = "auto") -> dict:
        """Caminho (do primeiro nível da árvore até o nó) para revelar o nó numa visão."""
        ix = self.doc.idx
        if no.startswith("m"):
            self.mresolve(no)
            parts = no.split("/")
            return {"vista": "modelo", "no": no, "caminho": ["/".join(parts[:i]) for i in range(2, len(parts) + 1)]}
        if no.startswith("t/"):
            return {"vista": "tipos", "no": no, "caminho": [no]}
        chain = self.doc.at(no)
        ids = [ix.ids[e] for e in chain]
        tags = [_t(e) for e in chain]
        if vista in ("tipos", "auto") and "DataTypeTemplates" in tags:
            i = tags.index("DataTypeTemplates")
            if len(chain) > i + 1 and tags[i + 1] in GRUPOS_TIPOS:
                return {"vista": "tipos", "no": no, "caminho": [f"t/{tags[i + 1]}"] + ids[i + 1:]}
        if vista in ("modelo", "auto"):
            for j in range(len(chain) - 1, -1, -1):
                if tags[j] not in ("DOI", "SDI", "DAI", "LN0", "LN", "LDevice", "IED"):
                    continue
                mid = self.model_id_of(chain[j])
                if not mid:
                    continue
                p = self.locate(mid, "modelo")
                if tags[j] in ("LN0", "LN") and j + 1 < len(chain):
                    if tags[j + 1] == "DOI":
                        break  # DOI que não existe no LNodeType: só na visão XML
                    return {"vista": "modelo", "no": no, "caminho": p["caminho"] + ids[j + 1:]}
                if tags[j] in ("LDevice", "IED") and j + 1 < len(chain):
                    break
                return p
        return {"vista": "xml", "no": no, "caminho": ids}
    def search(self, q: str, vista: str, limit: int = 300) -> dict:
        ql = q.strip().lower()
        if not ql:
            return {"resultados": [], "total": 0}
        hits = []
        if vista == "modelo":
            for mid, ref, desc, kind in self.doc.idx.model_index():
                if ql in ref.lower() or (desc and ql in desc.lower()):
                    hits.append(({"no": mid, "rotulo": ref, "desc": desc, "tipo": kind}, ref))
        else:
            pfx = self._dtt_prefix() if vista == "tipos" else "x"
            for xid, tag, text, el in self.doc.idx.xml_index():
                if xid.startswith(pfx) and ql in text:
                    lb = label(el)
                    hits.append(({"no": xid, "rotulo": f"{tag} {lb}".strip(), "desc": el.get("desc", ""),
                                  "tipo": TIPO.get(tag, "el")}, lb))
        # primeiro quem tem um nome igual ao procurado (Ind3 antes de Ind30), depois a ordem do documento
        exact = re.compile(rf"(^|[/.$ \[]){re.escape(ql)}($|[/.$ \]])")
        hits.sort(key=lambda x: 0 if exact.search(x[1].lower()) else 1)
        return {"resultados": [r for r, _ in hits[:limit]], "total": len(hits)}

    def _dtt_prefix(self) -> str:
        dtt = next((c for c in _els(self.doc.root) if _t(c) == "DataTypeTemplates"), None)
        return (self.doc.idx.ids[dtt] + ".") if dtt is not None else "x.-"

    # ------------------------------------------------------------------ validação e diferenças
    def validate(self) -> dict:
        ix = self.doc.idx
        erros, avisos = [], []

        def e(msg, el):
            if len(erros) < 1000:
                erros.append({"msg": msg, "no": ix.ids.get(el)})

        def w(msg, el):
            if len(avisos) < 1000:
                avisos.append({"msg": msg, "no": ix.ids.get(el)})

        for el in ix.tpl_dup:
            e(f"{_t(el)} com id repetido: {el.get('id')}", el)
        for k in GRUPOS_TIPOS:
            for tid, el in ix.tpl[k].items():
                if not ix.uses.get((k, tid)):
                    w(f"{k} {tid} não é usado", el)
        for el in ix.ids:
            tr = ix.type_ref(el)
            if tr and tr[1] not in ix.tpl[tr[0]]:
                e(f"{_t(el)} {label(el)}: {tr[0]} '{tr[1]}' não existe", el)
            tag = _t(el)
            if tag in ("DA", "BDA") and el.get("bType") in ("Struct", "Enum") and not el.get("type"):
                e(f"{tag} {el.get('name')}: bType {el.get('bType')} sem atributo type", el)
        for (ied, ld), lns in ix.lns.items():
            ldel = ix.lds[ied][ld]
            seen = Counter(ln_name(c) for c in _els(ldel) if _t(c) in ("LN0", "LN"))
            for n, c in seen.items():
                if c > 1:
                    e(f"{ied}{ld}: LN {n} repetido", ldel)
            if not any(_t(c) == "LN0" for c in _els(ldel)):
                e(f"{ied}{ld}: LDevice sem LN0", ldel)
            for lnn, ln in lns.items():
                base = f"{ld}/{lnn}"
                names = Counter((_t(c), c.get("name")) for c in _els(ln) if c.get("name"))
                for (tg, n), c in names.items():
                    if c > 1:
                        e(f"{base}: {tg} '{n}' repetido", ln)
                dsn = {c.get("name") for c in _els(ln) if _t(c) == "DataSet"}
                for c in _els(ln):
                    tg = _t(c)
                    if tg in ("ReportControl", "GSEControl", "LogControl", "SampledValueControl") and c.get("datSet") \
                            and c.get("datSet") not in dsn:
                        e(f"{base}.{c.get('name')}: DataSet '{c.get('datSet')}' inexistente", c)
                    if tg == "DataSet":
                        seenf = Counter(fcda_text(f) for f in _els(c) if _t(f) == "FCDA")
                        for f in _els(c):
                            if _t(f) != "FCDA":
                                continue
                            ok, why = self.fcda_status(f)
                            if not ok:
                                e(f"{base}.{c.get('name')}: FCDA {fcda_text(f)} — {why}", f)
                            if seenf[fcda_text(f)] > 1:
                                w(f"{base}.{c.get('name')}: FCDA {fcda_text(f)} repetido", f)
                                seenf[fcda_text(f)] = 0
                    if tg == "DOI":
                        mid = self.model_id_of(c)
                        if mid is None:
                            w(f"{base}: DOI '{c.get('name')}' não existe no LNodeType {ln.get('lnType')}", c)
                        else:
                            self._check_inst(c, mid, w)
        try:
            carga = self.ed.mgr.analyze(self.bytes)
        except SclError as ex:
            carga = {"erros": [str(ex)], "avisos": [], "ieds": []}
        except Exception as ex:  # noqa: BLE001 — o carregador do simulador não deve derrubar a validação
            carga = {"erros": [f"falha no carregador: {ex}"], "avisos": [], "ieds": []}
        return {"erros": erros, "avisos": avisos,
                "carga": {"erros": carga.get("erros", []) + [x for i in carga.get("ieds", []) for x in i.get("erros", [])],
                          "avisos": carga.get("avisos", []) + [x for i in carga.get("ieds", []) for x in i.get("avisos", [])],
                          "ieds": [{k: i.get(k) for k in ("nome", "pontos", "digitais", "analogicos", "datasets", "rcbs")}
                                   for i in carga.get("ieds", [])]}}

    def _check_inst(self, el, mid, w):
        ix = self.doc.idx
        for c in _els(el):
            tag = _t(c)
            if tag not in ("SDI", "DAI"):
                continue
            if c.get("ix"):
                continue
            sub = f"{mid}/{c.get('name', '')}"
            try:
                r = self.mresolve(sub)
            except PointError:
                w(f"{mid.split('/', 2)[2]}: {tag} '{c.get('name')}' não existe no tipo", c)
                continue
            if tag == "SDI":
                self._check_inst(c, sub, w)
                continue
            v = next((x for x in _els(c) if _t(x) == "Val"), None)
            if v is None:
                continue
            bt = r["tel"].get("bType", "")
            enum = ix.enum_texts(r["tel"].get("type", "")) if bt == "Enum" else None
            try:
                check_value(bt, v.text or "", enum)
            except ValueError as ex:
                w(f"{'/'.join(sub.split('/')[2:])}: {ex}", c)

    def diff(self, ctx: int = 3, limit: int = 4000) -> dict:
        old = self.base_norm().decode("utf-8", "replace").splitlines()
        cur = self.bytes.decode("utf-8", "replace").splitlines()
        lines = list(difflib.unified_diff(old, cur, "original", "rascunho", n=ctx, lineterm=""))
        add = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
        rem = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
        return {"linhas": lines[:limit], "truncado": len(lines) > limit, "adicionadas": add, "removidas": rem}

    # ------------------------------------------------------------------ visualizador (visão geral)
    def overview(self) -> dict:
        """Tudo o que o visualizador mostra, em uma chamada: cabeçalho, IEDs, comunicação, blocos de controle,
        DataSets, assinaturas (ExtRef e clientes de report), subestação e dados privados."""
        ix = self.doc.idx
        root = self.doc.root
        kids = lambda el, *tags: [c for c in _els(el) if _t(c) in tags]
        hdr = next(iter(kids(root, "Header")), None)
        cab = None
        if hdr is not None:
            hist = [dict(h.attrib) for hi in kids(hdr, "History") for h in kids(hi, "Hitem")]
            cab = {"atributos": dict(hdr.attrib), "historico": hist, "no": ix.ids[hdr]}
        ieds = []
        for nome, el in ix.ieds.items():
            srv = next(iter(kids(el, "Services")), None)
            if srv is None:
                srv = next((s for ap in kids(el, "AccessPoint") for s in kids(ap, "Services")), None)
            ieds.append({"nome": nome, "no": ix.ids[el], "desc": el.get("desc", ""),
                         "fabricante": el.get("manufacturer", ""), "tipo": el.get("type", ""),
                         "configVersion": el.get("configVersion", ""),
                         "aps": [ap.get("name", "") for ap in kids(el, "AccessPoint")],
                         "lds": list(ix.lds.get(nome, {})), "lns": sum(len(ix.lns[(nome, ld)]) for ld in ix.lds.get(nome, {})),
                         "servicos": [[_t(c), dict(c.attrib)] for c in _els(srv)] if srv is not None else []})
        com = []
        for comm in kids(root, "Communication"):
            for sn in kids(comm, "SubNetwork"):
                br = next(iter(kids(sn, "BitRate")), None)
                aps = []
                for cap in kids(sn, "ConnectedAP"):
                    addr = {p.get("type", ""): (p.text or "").strip() for a in kids(cap, "Address") for p in kids(a, "P")}
                    gse = []
                    for g in kids(cap, "GSE", "SMV"):
                        ga = {p.get("type", ""): (p.text or "").strip() for a in kids(g, "Address") for p in kids(a, "P")}
                        mt = {_t(c): f"{(c.text or '').strip()} {c.get('unit', '')}{c.get('multiplier', '')}".strip()
                              for c in kids(g, "MinTime", "MaxTime")}
                        gse.append({"tipo": _t(g), "ldInst": g.get("ldInst", ""), "cbName": g.get("cbName", ""),
                                    "endereco": ga, **mt, "no": ix.ids[g]})
                    aps.append({"ied": cap.get("iedName", ""), "ap": cap.get("apName", ""), "endereco": addr,
                                "gse": gse, "no": ix.ids[cap], "ied_existe": cap.get("iedName", "") in ix.ieds})
                com.append({"nome": sn.get("name", ""), "tipo": sn.get("type", ""), "desc": sn.get("desc", ""),
                            "bitrate": f"{(br.text or '').strip()} {br.get('multiplier', '')}{br.get('unit', 'b/s')}" if br is not None else "",
                            "aps": aps, "no": ix.ids[sn]})
        gse_addr = {(a["ied"], g["ldInst"], g["cbName"]): g for sn in com for a in sn["aps"] for g in a["gse"]}
        controles, datasets, extrefs, clientes = [], [], [], []
        for ied_name, lds in ix.lds.items():
            for ld_inst in lds:
                for lnn, ln in ix.lns[(ied_name, ld_inst)].items():
                    descs = {d.get("name", ""): d.get("desc", "") for d in kids(ln, "DOI")}
                    dsmap = {d.get("name", ""): d for d in kids(ln, "DataSet")}
                    for ds in dsmap.values():
                        membros = []
                        for i, f in enumerate(kids(ds, "FCDA")):
                            ok, why = self.fcda_status(f)
                            fln = f.get("prefix", "") + f.get("lnClass", "") + f.get("lnInst", "")
                            src = ix.lns.get((ied_name, f.get("ldInst", "")), {}).get(fln)
                            d = ""
                            if src is not None:
                                d = next((x.get("desc", "") for x in kids(src, "DOI")
                                          if x.get("name") == f.get("doName", "").split(".")[0]), "")
                            mms = f"{ied_name}{f.get('ldInst', '')}/{fln}${f.get('fc', '')}$" + "$".join(
                                s for s in f.get("doName", "").split(".") + f.get("daName", "").split(".") if s)
                            membros.append({"pos": i, "texto": fcda_text(f), "fc": f.get("fc", ""), "mms": mms,
                                            "desc": d, "ok": ok, "motivo": why, "no": ix.ids[f],
                                            "modelo": self.model_id_of(f) if ok else None})
                        datasets.append({"ied": ied_name, "ld": ld_inst, "ln": lnn, "nome": ds.get("name", ""),
                                         "desc": ds.get("desc", ""), "no": ix.ids[ds], "membros": membros,
                                         "usado_por": [c.get("name", "") for c in _els(ln) if c.get("datSet") == ds.get("name")]})
                    for c in kids(ln, "ReportControl", "GSEControl", "SampledValueControl", "LogControl"):
                        tag = _t(c)
                        ds = dsmap.get(c.get("datSet", ""))
                        item = {"tipo": tag, "ied": ied_name, "ld": ld_inst, "ln": lnn, "nome": c.get("name", ""),
                                "desc": c.get("desc", ""), "datSet": c.get("datSet", ""), "no": ix.ids[c],
                                "atributos": {k: v for k, v in c.attrib.items() if k not in ("name", "desc", "datSet")},
                                "dataset": {"no": ix.ids[ds], "membros": len(kids(ds, "FCDA"))} if ds is not None else None}
                        if tag == "ReportControl":
                            item["trgops"] = {k: v for t in kids(c, "TrgOps") for k, v in t.attrib.items()}
                            item["optflds"] = {k: v for t in kids(c, "OptFields") for k, v in t.attrib.items()}
                            re_ = next(iter(kids(c, "RptEnabled")), None)
                            item["max"] = re_.get("max", "1") if re_ is not None else "1"
                            item["clientes"] = []
                            for cl in (kids(re_, "ClientLN") if re_ is not None else []):
                                lab = label(cl)
                                item["clientes"].append(lab)
                                clientes.append({"rcb": f"{ied_name}{ld_inst}/{lnn}.{c.get('name', '')}", "cliente": lab,
                                                 "ied_cliente": cl.get("iedName", ""), "no": ix.ids[cl],
                                                 "existe": cl.get("iedName", "") in ix.ieds})
                        if tag == "GSEControl":
                            item["comunicacao"] = gse_addr.get((ied_name, ld_inst, c.get("name", "")))
                            item["assinantes"] = [x.get("name", "") for x in kids(c, "IEDName")] + \
                                                 [(x.text or "").strip() for x in kids(c, "IEDName") if not x.get("name")]
                            item["assinantes"] = [a for a in item["assinantes"] if a]
                        controles.append(item)
                    for inp in kids(ln, "Inputs"):
                        for e in kids(inp, "ExtRef"):
                            src_ied = e.get("iedName", "")
                            src = ix.lns.get((src_ied, e.get("ldInst", "")), {}).get(
                                e.get("prefix", "") + e.get("lnClass", "") + e.get("lnInst", ""))
                            extrefs.append({"ied": ied_name, "ld": ld_inst, "ln": lnn, "origem": label(e),
                                            "intAddr": e.get("intAddr", ""), "desc": e.get("desc", ""),
                                            "servico": e.get("serviceType", ""), "cb": e.get("srcCBName", ""),
                                            "ok": src is not None or not src_ied, "no": ix.ids[e]})

        def sub_tree(el, depth=0):
            out = []
            for c in _els(el):
                tag = _t(c)
                if tag in ("Text", "Private", "LNode") or depth > 12:
                    continue
                out.append({"tag": tag, "nome": c.get("name", ""), "desc": c.get("desc", ""), "tipo": c.get("type", ""),
                            "no": ix.ids[c], "lnodes": [label_lnode(x) for x in kids(c, "LNode")],
                            "filhos": sub_tree(c, depth + 1)})
            return out

        def label_lnode(x):
            ref = f"{x.get('iedName', '')}{x.get('ldInst', '')}/{x.get('prefix', '')}{x.get('lnClass', '')}{x.get('lnInst', '')}"
            ok = x.get("iedName", "None") in ("None", "") or x.get("iedName") in ix.ieds
            return {"ref": ref, "ok": ok, "no": ix.ids[x]}

        subestacao = [{"tag": "Substation", "nome": s.get("name", ""), "desc": s.get("desc", ""), "tipo": "",
                       "no": ix.ids[s], "lnodes": [label_lnode(x) for x in kids(s, "LNode")], "filhos": sub_tree(s)}
                      for s in kids(root, "Substation")]
        priv = Counter()
        for el in ix.ids:
            if _t(el) == "Private":
                priv[(el.get("type", "") or "(sem type)", _t(ix.parent.get(el)) if ix.parent.get(el) is not None else "")] += 1
        nss = [{"prefixo": p or "(padrão)", "uri": u} for p, u in self.doc.ns if u != self.doc.ns_scl]
        return {"cabecalho": cab, "versao_scl": (root.get("version", "") + root.get("revision", "")) or "Ed. 1",
                "ieds": ieds, "comunicacao": com, "controles": controles, "datasets": datasets,
                "extrefs": extrefs, "clientes": clientes, "subestacao": subestacao,
                "privados": [{"type": t, "em": p, "n": n} for (t, p), n in priv.most_common()], "namespaces": nss}

    # ------------------------------------------------------------------ exportação CSV
    def export_rows(self, tipo: str) -> tuple[list, list]:
        ov = self.overview()
        if tipo == "datasets":
            head = ["IED", "LD", "LN", "DataSet", "Posição", "FCDA", "FC", "Referência MMS", "Descrição", "Resolve", "Usado por"]
            rows = [[d["ied"], d["ld"], d["ln"], d["nome"], m["pos"] + 1, m["texto"], m["fc"], m["mms"], m["desc"],
                     "sim" if m["ok"] else m["motivo"], ", ".join(d["usado_por"])] for d in ov["datasets"] for m in d["membros"]]
        elif tipo == "controles":
            head = ["Tipo", "IED", "LD", "LN", "Nome", "DataSet", "Membros", "rptID/appID", "buffered", "bufTime", "intgPd",
                    "confRev", "max", "TrgOps", "OptFields", "Clientes", "Descrição"]
            rows = []
            for c in ov["controles"]:
                a = c["atributos"]
                rows.append([c["tipo"], c["ied"], c["ld"], c["ln"], c["nome"], c["datSet"],
                             c["dataset"]["membros"] if c["dataset"] else "", a.get("rptID") or a.get("appID") or a.get("smvID", ""),
                             a.get("buffered", ""), a.get("bufTime", ""), a.get("intgPd", ""), a.get("confRev", ""), c.get("max", ""),
                             " ".join(k for k, v in c.get("trgops", {}).items() if v in ("true", "1")),
                             " ".join(k for k, v in c.get("optflds", {}).items() if v in ("true", "1")),
                             ", ".join(c.get("clientes", []) + c.get("assinantes", [])), c["desc"]])
        elif tipo == "comunicacao":
            head = ["SubNetwork", "Tipo", "IED", "AccessPoint", "Parâmetro", "Valor"]
            rows = []
            for sn in ov["comunicacao"]:
                for a in sn["aps"]:
                    rows += [[sn["nome"], sn["tipo"], a["ied"], a["ap"], k, v] for k, v in a["endereco"].items()]
                    for g in a["gse"]:
                        rows += [[sn["nome"], sn["tipo"], a["ied"], a["ap"], f"{g['tipo']} {g['ldInst']}/{g['cbName']} {k}", v]
                                 for k, v in g["endereco"].items()]
        elif tipo == "pontos":
            head = ["IED", "LD", "LN", "DO", "Atributo", "FC", "bType", "CDC", "Referência MMS", "Descrição", "Valor no CID"]
            rows = self._point_rows()
        else:
            raise PointError("tipo de exportação inválido; use datasets, controles, comunicacao ou pontos", status=422)
        return head, rows

    def _point_rows(self) -> list:
        """Todos os atributos simples do modelo (folhas), com FC, tipo e descrição da instância."""
        ix = self.doc.idx
        rows = []
        for ied, lds in ix.lds.items():
            for ld_inst in lds:
                for lnn, ln in ix.lns[(ied, ld_inst)].items():
                    dois = {d.get("name", ""): d for d in _els(ln) if _t(d) == "DOI"}
                    for do_name, do in ix.expand("LNodeType", ln.get("lnType", "")):
                        dt = ix.tpl["DOType"].get(do.get("type", ""))
                        cdc = dt.get("cdc", "") if dt is not None else ""
                        doi = dois.get(do_name)
                        desc = doi.get("desc", "") if doi is not None else ""
                        stack = [([do_name], do, None, doi)]
                        while stack:
                            path, tel, fc, inst = stack.pop()
                            for nm, c in reversed(ix.tchildren(tel)):
                                cfc = c.get("fc", fc)
                                sub = next((x for x in _els(inst) if _t(x) in ("SDI", "DAI") and x.get("name") == nm
                                            and not x.get("ix")), None) if inst is not None else None
                                if ix.tchildren(c) and len(path) < 10:
                                    stack.append((path + [nm], c, cfc, sub))
                                elif _t(c) in ("DA", "BDA"):
                                    v, _ = self._value(c, sub)
                                    rows.append([ied, ld_inst, lnn, do_name, ".".join(path[1:] + [nm]), cfc or "",
                                                 c.get("bType", ""), cdc,
                                                 f"{ied}{ld_inst}/{lnn}${cfc}$" + "$".join(path + [nm]), desc, v or ""])
        return rows

    # ------------------------------------------------------------------ comparação
    def compare(self, other: Doc, nome_outro: str, sem_nome: bool | None = None) -> dict:
        if sem_nome is None:  # um IED em cada arquivo, com nomes diferentes: compara o conteúdo
            sem_nome = len(other.idx.ieds) == 1 and len(self.doc.idx.ieds) == 1 and \
                set(other.idx.ieds) != set(self.doc.idx.ieds)
        a, b = _signature(other, sem_nome), _signature(self.doc, sem_nome)
        out = []
        for cat in a:
            ka, kb = a[cat], b[cat]
            for k in sorted(set(ka) | set(kb)):
                if k not in kb:
                    out.append({"categoria": cat, "chave": k, "mudanca": "removido", "detalhes": []})
                elif k not in ka:
                    out.append({"categoria": cat, "chave": k, "mudanca": "adicionado", "detalhes": []})
                elif ka[k] != kb[k]:
                    out.append({"categoria": cat, "chave": k, "mudanca": "alterado", "detalhes": _detail(ka[k], kb[k])})
        resumo = Counter((x["categoria"], x["mudanca"]) for x in out)
        return {"com": nome_outro, "sem_nome": sem_nome, "ieds": [list(other.idx.ieds), list(self.doc.idx.ieds)],
                "total": len(out), "diferencas": out[:5000], "truncado": len(out) > 5000,
                "resumo": [{"categoria": c, "mudanca": m, "n": n} for (c, m), n in sorted(resumo.items())]}

    # ------------------------------------------------------------------ operações
    def apply_op(self, body: dict, user: str) -> dict:
        op = body.get("op")
        fn = getattr(self, f"_op_{op}", None) if isinstance(op, str) else None
        if fn is None:
            raise PointError(f"operação '{op}' desconhecida; use {sorted(OPS)}", status=422)
        before = self.bytes
        self._new_ind = {}
        try:
            res = fn(body) or {}
        except Exception:
            self.doc.load(before)  # nada pela metade
            raise
        self.doc.touched()
        data = self.doc.serialize()
        if data == before:
            return dict(res, alterado=False, mensagem=res.get("mensagem", "nada mudou"))
        desc = res.get("mensagem", op)
        self.undo.append((zlib.compress(before, 1), desc))
        del self.undo[:-MAX_UNDO]
        self.redo.clear()
        self._commit(data, desc, user)
        return dict(res, alterado=True)

    def _commit(self, data: bytes, desc: str, user: str):
        self.bytes = data
        self.version += 1
        h = self.meta.setdefault("historico", [])
        h.append({"ts": time.time(), "usuario": user, "acao": desc})
        del h[:-500]
        self.ed.persist(self)

    def undo_op(self, user: str, redo: bool = False) -> dict:
        src, dst = (self.redo, self.undo) if redo else (self.undo, self.redo)
        if not src:
            raise PointError("nada para " + ("refazer" if redo else "desfazer"), status=409)
        packed, desc = src.pop()
        dst.append((zlib.compress(self.bytes, 1), desc))
        data = zlib.decompress(packed)
        self.doc.load(data)
        self._commit(data, ("refeito: " if redo else "desfeito: ") + desc, user)
        return {"mensagem": ("Refeito: " if redo else "Desfeito: ") + desc}
    def _need(self, body, k):
        if body.get(k) in (None, ""):
            raise PointError(f"campo '{k}' obrigatório", status=422)
        return body[k]

    def _op_atributos(self, body):
        el = self.doc.el(self._need(body, "no"))
        attrs = body.get("atributos")
        if not isinstance(attrs, dict) or not attrs:
            raise PointError("'atributos' deve ser um objeto {nome: valor ou null}", status=422)
        plan = []
        for k, v in attrs.items():
            if not _NOME_XML.match(str(k)):
                raise PointError(f"nome de atributo inválido: '{k}'", status=422)
            if v is not None and not isinstance(v, (str, int, float, bool)):
                raise PointError(f"valor do atributo '{k}' deve ser texto", status=422)
            plan.append((self._attr_key(str(k)), None if v is None else
                         ("true" if v is True else "false" if v is False else str(v))))
        old = {k: el.get(k) for k, _ in plan}
        for k, v in plan:
            if v is None:
                el.attrib.pop(k, None)
            else:
                el.set(k, v)
        mud = ", ".join(f"{self._attr_name(k)}={v if v is not None else '(removido)'}" for k, v in plan
                        if old[k] != v)
        return {"no": body["no"], "mensagem": f"{_t(el)} {label(el)}: {mud}" if mud else "nada mudou"}

    def _op_texto(self, body):
        el = self.doc.el(self._need(body, "no"))
        txt = body.get("texto")
        if not isinstance(txt, str):
            raise PointError("'texto' deve ser texto", status=422)
        if _els(el):
            raise PointError(f"{_t(el)} tem elementos filhos; edite o XML do elemento", status=422)
        el.text = txt
        return {"no": body["no"], "mensagem": f"{_t(el)} {label(el)}: texto = {txt[:60]}"}

    def _op_excluir(self, body):
        no = self._need(body, "no")
        chain = self.doc.at(no)
        if len(chain) == 1:
            raise PointError("a raiz <SCL> não pode ser excluída", status=422)
        el, parent = chain[-1], chain[-2]
        aviso = ""
        ix = self.doc.idx
        if _t(el) in GRUPOS_TIPOS and ix.uses.get((_t(el), el.get("id", ""))):
            n = len(ix.uses[(_t(el), el.get("id", ""))])
            if not body.get("confirmar"):
                raise PointError(f"{_t(el)} {el.get('id')} é usado por {n} elemento(s); excluir deixa referências "
                                 "quebradas. Confirme.", status=409, code="confirmar")
            aviso = f" (deixou {n} referência(s) quebrada(s))"
        if _t(el) == "DataSet":
            refs = [c.get("name") for c in _els(parent) if c.get("datSet") == el.get("name")]
            if refs and not body.get("confirmar"):
                raise PointError(f"DataSet {el.get('name')} é usado por {', '.join(refs)}. Confirme.", status=409,
                                 code="confirmar")
        kids = _els(parent)
        i = kids.index(el)
        if i == len(kids) - 1 and i > 0:
            kids[i - 1].tail = el.tail  # o último irmão leva o recuo de fechamento do pai
        parent.remove(el)
        left = len(kids) - 1
        pid = ".".join(no.split(".")[:-1])
        return {"no": f"{pid}.{min(i, left - 1)}" if left else pid, "mensagem": f"excluído {_t(el)} {label(el)}{aviso}"}
    def _op_mover(self, body):
        no = self._need(body, "no")
        chain = self.doc.at(no)
        if len(chain) == 1:
            raise PointError("a raiz não se move", status=422)
        el, parent = chain[-1], chain[-2]
        step = -1 if int(body.get("direcao", -1)) < 0 else 1
        kids = _els(parent)
        i = kids.index(el)
        j = i + step
        if not 0 <= j < len(kids):
            raise PointError("já está na ponta", status=409)
        other = kids[j]
        el.tail, other.tail = other.tail, el.tail
        parent.remove(el)
        pos = list(parent).index(other)
        parent.insert(pos if step < 0 else pos + 1, el)
        pid = ".".join(no.split(".")[:-1])
        return {"no": f"{pid}.{j}", "mensagem": f"movido {_t(el)} {label(el)} para {'cima' if step < 0 else 'baixo'}"}
    def _op_duplicar(self, body):
        no = self._need(body, "no")
        chain = self.doc.at(no)
        if len(chain) == 1:
            raise PointError("a raiz não pode ser duplicada", status=422)
        el, parent = chain[-1], chain[-2]
        dup = copy.deepcopy(el)
        key = "id" if _t(el) in GRUPOS_TIPOS else ("inst" if _t(el) in ("LN", "LDevice") else "name")
        if dup.get(key) is not None:
            base = dup.get(key)
            sibs = {(c.get("prefix", ""), c.get("lnClass", ""), c.get(key)) if key == "inst" and _t(el) == "LN"
                    else c.get(key) for c in _els(parent)}
            same = (lambda v: (el.get("prefix", ""), el.get("lnClass", ""), v)) if key == "inst" and _t(el) == "LN" \
                else (lambda v: v)
            if key == "inst" and base.isdigit():
                n = int(base) + 1
                while same(str(n)) in sibs:
                    n += 1
                dup.set(key, str(n))
            else:
                n = 2
                while same(f"{base}_{n}") in sibs:
                    n += 1
                dup.set(key, f"{base}_{n}")
        kids = _els(parent)
        if el is kids[-1]:
            dup.tail, el.tail = el.tail, (kids[-2].tail if len(kids) > 1 else parent.text)
        else:
            dup.tail = el.tail
        parent.insert(list(parent).index(el) + 1, dup)
        pid = ".".join(no.split(".")[:-1])
        return {"no": f"{pid}.{_els(parent).index(dup)}", "mensagem": f"duplicado {_t(el)} {label(el)} → {label(dup)}"}
    def _parse_fragment(self, xml: str):
        decl = " ".join(f'xmlns:{p}="{u}"' for p, u in self.doc.ns if p)
        wrap = f'<w xmlns="{self.doc.ns_scl}" {decl}>{xml}</w>' if self.doc.ns_scl else f"<w {decl}>{xml}</w>"
        try:
            parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
            w = ET.fromstring(wrap.encode("utf-8"), parser)
        except ET.ParseError as e:
            raise PointError(f"XML inválido: {e}", status=422) from e
        els = _els(w)
        if len(els) != 1:
            raise PointError(f"o XML deve ter exatamente um elemento (tem {len(els)})", status=422)
        return els[0]

    def _indent(self, el) -> str:
        """Recuo (espaços antes da tag) de um elemento já na árvore."""
        known = self._new_ind.get(el)
        if known is not None:
            return known
        p = self.doc.idx.parent.get(el)
        if p is None:
            return ""
        allk = list(p)
        i = allk.index(el) if el in allk else 0
        prev = allk[i - 1].tail if i > 0 else p.text
        return (prev or "").rsplit("\n", 1)[-1] if "\n" in (prev or "") else ""

    def _insert(self, parent, el) -> int:
        """Insere conforme a ordem do esquema, com recuo igual ao dos irmãos; devolve o índice entre os elementos."""
        order = ORDEM.get(_t(parent))
        rank = {}
        for i, o in enumerate(order or []):
            for x in (o if isinstance(o, tuple) else (o,)):
                rank[x] = i
        allk = list(parent)
        pos = len(allk)
        if order and _t(el) in rank:
            r = rank[_t(el)]
            for i, c in enumerate(allk):
                if isinstance(c.tag, str) and rank.get(_t(c), -1) > r:
                    pos = i
                    break
        unit = self.doc.unit
        pind = self._indent(parent)
        kids = _els(parent)
        inner = "\n" + pind + unit
        if not kids:
            if not (parent.text or "").strip():
                parent.text = inner
            el.tail = "\n" + pind
        else:
            last_i = max(i for i, c in enumerate(allk) if isinstance(c.tag, str))
            if pos > last_i:
                el.tail = allk[last_i].tail
                allk[last_i].tail = inner
            else:
                el.tail = inner
        self._new_ind[el] = pind + unit
        self._pretty(el, pind + unit, unit)
        parent.insert(pos, el)
        return _els(parent).index(el)
    def _op_adicionar(self, body):
        pai = self._need(body, "pai")
        parent = self.doc.el(pai)
        if body.get("xml"):
            el = self._parse_fragment(str(body["xml"]))
        else:
            tag = self._need(body, "tag")
            if not _NOME_XML.match(tag) or ":" in tag:
                raise PointError(f"nome de elemento inválido: '{tag}'", status=422)
            el = ET.Element(self.doc.q(tag))
            attrs = body.get("atributos") or {}
            if not isinstance(attrs, dict):
                raise PointError("'atributos' deve ser um objeto", status=422)
            for k, v in attrs.items():
                if v in (None, "") and k not in ("prefix", "lnInst"):
                    continue
                if not _NOME_XML.match(str(k)):
                    raise PointError(f"nome de atributo inválido: '{k}'", status=422)
                el.set(self._attr_key(str(k)), str(v))
            if body.get("texto") not in (None, ""):
                el.text = str(body["texto"])
            if tag == "ReportControl":
                for sub in ("TrgOps", "OptFields", "RptEnabled"):
                    ET.SubElement(el, self.doc.q(sub), MODELOS[sub])
            if tag == "DAI" and body.get("valor") not in (None, ""):
                ET.SubElement(el, self.doc.q("Val")).text = str(body["valor"])
        allowed = [x for o in ORDEM.get(_t(parent), []) for x in (o if isinstance(o, tuple) else (o,))]
        if allowed and _t(el) not in allowed and not body.get("confirmar"):
            raise PointError(f"{_t(el)} não é filho previsto de {_t(parent)} no esquema SCL (previstos: "
                             f"{', '.join(allowed)}). Inserir mesmo assim?", status=409, code="confirmar")
        j = self._insert(parent, el)
        return {"no": f"{pai}.{j}", "mensagem": f"adicionado {_t(el)} {label(el)} em {_t(parent)} {label(parent)}"}

    @staticmethod
    def _pretty(el, ind, unit):
        kids = _els(el)
        if not kids:
            return
        if not (el.text or "").strip():
            el.text = "\n" + ind + unit
        for i, c in enumerate(kids):
            Session._pretty(c, ind + unit, unit)
            if not (c.tail or "").strip():
                c.tail = "\n" + ind + (unit if i < len(kids) - 1 else "")
    def _op_substituir(self, body):
        no = self._need(body, "no")
        chain = self.doc.at(no)
        new = self._parse_fragment(str(self._need(body, "xml")))
        if len(chain) == 1:
            if _t(new) != "SCL":
                raise PointError("a raiz deve continuar sendo <SCL>", status=422)
            self.doc.root = new
            return {"no": "x", "mensagem": "XML do documento substituído"}
        el, parent = chain[-1], chain[-2]
        new.tail = el.tail
        allk = list(parent)
        i = allk.index(el)
        parent.remove(el)
        parent.insert(i, new)
        return {"no": no, "mensagem": f"XML de {_t(el)} {label(el)} substituído"}

    def _op_limpar(self, body):
        """Remove os tipos (DataTypeTemplates) que ninguém usa, repetindo até não sobrar nenhum."""
        removidos = Counter()
        while True:
            self.doc.touched()
            ix = self.doc.idx
            sobra = [(k, el) for k in GRUPOS_TIPOS for tid, el in ix.tpl[k].items() if not ix.uses.get((k, tid))]
            if not sobra:
                break
            for k, el in sobra:
                p = ix.parent[el]
                kids = _els(p)
                i = kids.index(el)
                if i == len(kids) - 1 and i > 0:
                    kids[i - 1].tail = el.tail
                p.remove(el)
                removidos[k] += 1
        if not removidos:
            return {"mensagem": "nenhum tipo sem uso"}
        return {"no": None, "mensagem": "limpeza: removidos " + ", ".join(f"{n} {k}" for k, n in removidos.items())}

    # -- operações do modelo
    def _ensure_inst(self, r) -> object:
        """Cria (se preciso) DOI/SDI/DAI até o nó do modelo e devolve o último elemento."""
        ln = r["ln"]
        cur = ln
        for i, s in enumerate(r["segs"]):
            t = r["tchain"][i]
            want = ("DOI",) if i == 0 else ("SDI", "DAI")
            nxt = next((c for c in _els(cur) if _t(c) in want and c.get("name") == s and not c.get("ix")), None)
            if nxt is None:
                tag = "DOI" if i == 0 else ("SDI" if _t(t) == "SDO" or t.get("bType") == "Struct" else "DAI")
                nxt = ET.Element(self.doc.q(tag), {"name": s})
                self._insert(cur, nxt)
            cur = nxt
        return cur

    def _prune(self, el):
        """Remove instâncias que ficaram vazias (só com name), subindo até o LN."""
        ix_parent = {c: p for p in self.doc.root.iter() for c in p}
        while el is not None and _t(el) in ("DOI", "SDI", "DAI") and not _els(el) and set(el.attrib) <= {"name"}:
            p = ix_parent.get(el)
            p.remove(el)
            el = p

    def _op_valor(self, body):
        no = self._need(body, "no")
        r = self.mresolve(no)
        if r["kind"] not in ("da", "bda") or r["tel"].get("bType") == "Struct":
            raise PointError("só atributos simples (DA/BDA que não são Struct) têm valor", status=422)
        if r["tel"].get("count") not in (None, "", "0"):
            raise PointError("atributo em array: edite pela visão XML (DAI/SDI com ix)", status=422)
        valor = body.get("valor")
        bt = r["tel"].get("bType", "")
        if valor is None:
            inst = r["inst"]
            if len(inst) == len(r["segs"]):
                dai = inst[-1]
                for v in [c for c in _els(dai) if _t(c) == "Val"]:
                    dai.remove(v)
                if set(dai.attrib) <= {"name", "valKind"} and not _els(dai):
                    dai.attrib.pop("valKind", None)
                    self._prune(dai)
            return {"no": no, "mensagem": f"{'.'.join(r['segs'])}: valor da instância removido (vale o do tipo)"}
        enum = self.doc.idx.enum_texts(r["tel"].get("type", "")) if bt == "Enum" else None
        try:
            v = check_value(bt, str(valor), enum)
        except ValueError as e:
            raise PointError(str(e), status=422) from e
        dai = self._ensure_inst(r)
        vals = [c for c in _els(dai) if _t(c) == "Val"]
        if vals:
            vals[0].text = v
            for extra in vals[1:]:
                if not extra.get("sGroup"):
                    dai.remove(extra)
        else:
            val = ET.Element(self.doc.q("Val"))
            val.text = v
            self._insert(dai, val)
        return {"no": no, "mensagem": f"{'.'.join(r['segs'])} = {v}"}

    def _op_descricao(self, body):
        no = self._need(body, "no")
        desc = str(body.get("desc") or "")
        if not no.startswith("m"):
            return self._op_atributos({"no": no, "atributos": {"desc": desc or None}})
        r = self.mresolve(no)
        if r["kind"] in ("ied", "ld", "ln"):
            el = r[r["kind"]]
        elif desc:
            el = self._ensure_inst(r)
        else:
            inst = r["inst"]
            if len(inst) != len(r["segs"]):
                return {"no": no, "mensagem": "nada mudou"}
            el = inst[-1]
        if desc:
            el.set("desc", desc)
        else:
            el.attrib.pop("desc", None)
            if _t(el) in ("DOI", "SDI", "DAI"):
                self._prune(el)
        return {"no": no, "mensagem": f"{no.split('/', 2)[-1]}: desc = {desc or '(removida)'}"}

    def _op_fcda(self, body):
        no = self._need(body, "no")
        r = self.mresolve(no)
        if r["kind"] not in ("do", "sdo", "da", "bda"):
            raise PointError("escolha um DO ou DA para incluir no DataSet", status=422)
        ds = self.doc.el(self._need(body, "dataset"))
        if _t(ds) != "DataSet":
            raise PointError("o destino não é um DataSet", status=422)
        if self.doc.idx.ied_of(ds) is not r["ied"]:
            raise PointError("o DataSet deve ser do mesmo IED", status=422)
        fc = r["fc"] or body.get("fc")
        fcs = [r["fc"]] if r["fc"] else self._fcs_below(r["tel"])
        if not fc or fc not in fcs:
            raise PointError(f"escolha o FC ({', '.join(fcs) or 'nenhum disponível'})", status=422)
        segs = r["segs"]
        nd = 1
        for i, t in enumerate(r["tchain"][1:], 1):
            if _t(t) == "SDO":
                nd = i + 1
        ln = r["ln"]
        attrs = {"ldInst": r["ld"].get("inst", ""), "prefix": ln.get("prefix", ""), "lnClass": ln.get("lnClass", ""),
                 "lnInst": ln.get("inst", ""), "doName": ".".join(segs[:nd])}
        if len(segs) > nd:
            attrs["daName"] = ".".join(segs[nd:])
        attrs["fc"] = fc
        f = ET.Element(self.doc.q("FCDA"), attrs)
        txt = fcda_text(f)
        if any(_t(c) == "FCDA" and fcda_text(c) == txt for c in _els(ds)):
            raise PointError(f"{txt} já está no DataSet {ds.get('name')}", status=409)
        self._insert(ds, f)
        return {"no": no, "mensagem": f"FCDA {txt} incluído em {ds.get('name')}"}


# ---------------------------------------------------------------------------------------------- comparação
def _canon(el) -> str:
    """Forma canônica de um elemento (atributos ordenados, sem espaços), para comparar tipos."""
    attrs = " ".join(f'{k}="{v}"' for k, v in sorted(el.attrib.items()))
    txt = (el.text or "").strip()
    return f"<{_t(el)} {attrs}>{txt}" + "".join(_canon(c) for c in _els(el) if _t(c) != "Private") + f"</{_t(el)}>"


def _signature(doc: Doc, sem_nome: bool = False) -> dict:
    """Resumo comparável do documento, por categoria: {chave: conteúdo}. Com sem_nome, o nome do IED sai das
    chaves (para comparar dois relés do mesmo modelo com nomes diferentes)."""
    ix = doc.idx
    chave = (lambda n: "<IED>" if n in ix.ieds else n) if sem_nome else (lambda n: n)
    sig = {"IED": {}, "LN": {}, "Instância (DOI)": {}, "DataSet": {}, "Bloco de controle": {}, "Comunicação": {},
           "Tipo": {}}
    for name, el in ix.ieds.items():
        sig["IED"][chave(name)] = {k: el.get(k, "") for k in ("manufacturer", "type", "configVersion", "desc")}
    for (ied, ld), lns in ix.lns.items():
        for lnn, ln in lns.items():
            base = f"{chave(ied)}{ld}/{lnn}"
            sig["LN"][base] = {"lnType": ln.get("lnType", ""), "desc": ln.get("desc", "")}
            for c in _els(ln):
                tag = _t(c)
                if tag == "DOI":
                    vals = {}
                    stack = [(c, c.get("name", ""))]
                    while stack:
                        el, path = stack.pop()
                        for x in _els(el):
                            nm = x.get("name", "") + (f"({x.get('ix')})" if x.get("ix") else "")
                            if _t(x) == "DAI":
                                v = next((y for y in _els(x) if _t(y) == "Val"), None)
                                vals[f"{path}.{nm}"] = (v.text or "").strip() if v is not None else None
                                if x.get("sAddr"):
                                    vals[f"{path}.{nm} (sAddr)"] = x.get("sAddr")
                            elif _t(x) == "SDI":
                                stack.append((x, f"{path}.{nm}"))
                    sig["Instância (DOI)"][f"{base}.{c.get('name', '')}"] = {"desc": c.get("desc", ""), **vals}
                elif tag == "DataSet":
                    sig["DataSet"][f"{base}.{c.get('name', '')}"] = {
                        "membros": [fcda_text(f) for f in _els(c) if _t(f) == "FCDA"], "desc": c.get("desc", "")}
                elif tag in ("ReportControl", "GSEControl", "SampledValueControl", "LogControl"):
                    d = dict(c.attrib)
                    for sub in _els(c):
                        if _t(sub) in ("TrgOps", "OptFields", "RptEnabled"):
                            d.update({f"{_t(sub)}.{k}": v for k, v in sub.attrib.items()})
                            if _t(sub) == "RptEnabled":
                                d["clientes"] = [label(x) for x in _els(sub) if _t(x) == "ClientLN"]
                    sig["Bloco de controle"][f"{base}.{c.get('name', '')} ({tag})"] = d
    for comm in (c for c in _els(doc.root) if _t(c) == "Communication"):
        for sn in (c for c in _els(comm) if _t(c) == "SubNetwork"):
            for cap in (c for c in _els(sn) if _t(c) == "ConnectedAP"):
                for g in [cap] + [x for x in _els(cap) if _t(x) in ("GSE", "SMV")]:
                    key = f"{sn.get('name', '')}/{chave(cap.get('iedName', ''))}/{cap.get('apName', '')}"
                    if g is not cap:
                        key += f" {_t(g)} {g.get('ldInst', '')}/{g.get('cbName', '')}"
                    sig["Comunicação"][key] = {p.get("type", ""): (p.text or "").strip()
                                               for a in _els(g) if _t(a) == "Address" for p in _els(a)}
    for k in GRUPOS_TIPOS:
        for tid, el in ix.tpl[k].items():
            sig["Tipo"][f"{k} {tid}"] = _canon(el)
    return sig


def _detail(a, b) -> list:
    """Diferenças legíveis entre dois conteúdos do resumo."""
    if isinstance(a, str) or isinstance(b, str):
        return ["definição diferente"]
    out = []
    if "membros" in a or "membros" in b:
        ma, mb = a.get("membros", []), b.get("membros", [])
        for m in mb:
            if m not in ma:
                out.append(f"+ membro {m}")
        for m in ma:
            if m not in mb:
                out.append(f"− membro {m}")
        if [m for m in ma if m in mb] != [m for m in mb if m in ma]:
            out.append("ordem dos membros mudou")
    for k in sorted(set(a) | set(b)):
        if k == "membros":
            continue
        if a.get(k) != b.get(k):
            out.append(f"{k}: {a.get(k) if a.get(k) is not None else '(ausente)'} → {b.get(k) if b.get(k) is not None else '(ausente)'}")
    return out[:200]


OPS = {n[4:] for n in dir(Session) if n.startswith("_op_")}


# ---------------------------------------------------------------------------------------------- editor
class CidEditor:
    """Sessões de edição: "ied-<id>" (CID de um IED) e "arq-<hex>" (arquivo avulso)."""

    MAX_ABERTAS = 4

    def __init__(self, mgr):
        self.mgr = mgr
        self.dir = mgr.data_dir / "cid_editor"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ persistência
    def _paths(self, sid: str) -> tuple[Path, Path, Path]:
        if not re.fullmatch(r"(ied-\d+|arq-[0-9a-f]{16})", sid):
            raise PointError(f"sessão '{sid}' inválida", status=404)
        return self.dir / f"{sid}.json", self.dir / f"{sid}.cid", self.dir / f"{sid}.base"

    def persist(self, s: Session):
        meta, cur, base = self._paths(s.sid)
        s.meta["alterado_em"] = time.time()
        if s.meta["origem"] == "ied" and not s.dirty:
            for p in (meta, cur, base):
                p.unlink(missing_ok=True)
            return
        cur.write_bytes(s.bytes)
        if s.meta["origem"] == "arquivo" and not base.exists():
            base.write_bytes(s.base)
        if s.meta["origem"] == "ied":
            base.write_bytes(s.base)
        meta.write_text(json.dumps(s.meta, ensure_ascii=False))

    def get(self, sid: str) -> Session:
        with self.lock:
            s = self.sessions.get(sid)
            if s is None:
                s = self._open(sid)
                self.sessions[sid] = s
                for k in sorted(self.sessions, key=lambda k: self.sessions[k].used)[:-self.MAX_ABERTAS]:
                    if k != sid:
                        del self.sessions[k]  # o rascunho continua em disco
            s.used = time.time()
            return s

    def _open(self, sid: str) -> Session:
        meta_p, cur_p, base_p = self._paths(sid)
        if sid.startswith("ied-"):
            iid = int(sid[4:])
            ied = self.mgr.ieds.get(iid)
            if meta_p.exists() and cur_p.exists() and base_p.exists():
                meta = json.loads(meta_p.read_text())
                return Session(self, sid, meta, base_p.read_bytes(), cur_p.read_bytes())
            if ied is None:
                raise PointError(f"IED {iid} inexistente", status=404)
            data = self.mgr.read_cid(ied.cfg["cid_arquivo"])
            meta = {"nome": ied.cfg.get("cid_nome") or "ied.cid", "origem": "ied", "ied": iid,
                    "base_sha": _sha(data), "criado_em": time.time(), "historico": []}
            return Session(self, sid, meta, data, data)
        if not meta_p.exists():
            raise PointError("arquivo do editor não encontrado (descartado?)", status=404)
        meta = json.loads(meta_p.read_text())
        return Session(self, sid, meta, base_p.read_bytes(), cur_p.read_bytes())

    def upload(self, data: bytes, nome: str) -> Session:
        sid = "arq-" + secrets.token_hex(8)
        meta = {"nome": nome, "origem": "arquivo", "ied": None, "base_sha": _sha(data), "criado_em": time.time(),
                "historico": []}
        s = Session(self, sid, meta, data, data)
        meta_p, cur_p, base_p = self._paths(sid)
        base_p.write_bytes(data)
        self.persist(s)
        with self.lock:
            self.sessions[sid] = s
        return s

    def discard(self, sid: str):
        for p in self._paths(sid):
            p.unlink(missing_ok=True)
        with self.lock:
            self.sessions.pop(sid, None)

    def list(self) -> list:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            sid = p.stem
            try:
                m = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            ied = self.mgr.ieds.get(m.get("ied")) if m.get("origem") == "ied" else None
            out.append({"sessao": sid, "nome": m.get("nome"), "origem": m.get("origem"), "ied": m.get("ied"),
                        "ied_nome": ied.name if ied else None, "alterado_em": m.get("alterado_em"),
                        "alteracoes": len(m.get("historico", []))})
        return sorted(out, key=lambda x: -(x["alterado_em"] or 0))

    def rebase(self, s: Session, data: bytes):
        """Depois de aplicar no IED: o CID do IED passa a ser a nova linha de base."""
        s.base = data
        s.bytes = data
        s._base_norm = None
        s.meta["base_sha"] = _sha(data)
        s.meta.setdefault("historico", []).append({"ts": time.time(), "usuario": "", "acao": "aplicado no IED"})
        self.persist(s)

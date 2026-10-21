"""Leitor de arquivos SCL (CID/ICD/SCD/IID), Edição 1 e 2 (RN-CID-01 a RN-CID-10)."""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NS_SCL = "http://www.iec.ch/61850/2003/SCL"


def _t(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _kids(el: ET.Element, name: str | None = None):
    for c in el:
        if not isinstance(c.tag, str):
            continue
        if name is None or _t(c) == name:
            yield c


def _first(el: ET.Element, name: str):
    return next(_kids(el, name), None)


def _val(el: ET.Element) -> str | None:
    v = _first(el, "Val")
    return v.text.strip() if v is not None and v.text is not None else (None if v is None else "")


def _bool(s: str | None, default: bool = False) -> bool:
    if s is None:
        return default
    return s.strip().lower() in ("true", "1")


@dataclass
class SclDA:
    name: str
    btype: str
    type: str = ""
    fc: str = ""
    count: int = 0
    trg: frozenset = frozenset()
    val: str | None = None
    desc: str = ""


@dataclass
class SclDOType:
    id: str
    cdc: str
    elems: list = field(default_factory=list)  # ("DA", SclDA) | ("SDO", name, type, desc)
    desc: str = ""


@dataclass
class SclLNodeType:
    id: str
    ln_class: str
    dos: list = field(default_factory=list)  # (name, type, desc)


@dataclass
class Templates:
    lnode: dict = field(default_factory=dict)
    dotype: dict = field(default_factory=dict)
    datype: dict = field(default_factory=dict)  # id -> list[SclDA]
    enum: dict = field(default_factory=dict)  # id -> {ord: texto}


@dataclass
class SclFcda:
    ld_inst: str = ""
    prefix: str = ""
    ln_class: str = ""
    ln_inst: str = ""
    do_name: str = ""
    da_name: str = ""
    fc: str = ""

    def text(self) -> str:
        s = f"{self.ld_inst}/{self.prefix}{self.ln_class}{self.ln_inst}.{self.do_name}"
        if self.da_name:
            s += "." + self.da_name
        return f"{s} [{self.fc}]"


@dataclass
class SclDataSet:
    name: str
    desc: str = ""
    fcdas: list = field(default_factory=list)
    extra: bool = False


@dataclass
class SclRcb:
    name: str
    datset: str = ""
    rptid: str = ""
    conf_rev: int = 1
    buffered: bool = False
    buf_time: int = 0
    intg_pd: int = 0
    indexed: bool = True
    trgops: dict = field(default_factory=dict)
    optflds: dict = field(default_factory=dict)
    max: int = 1
    desc: str = ""
    extra: bool = False


@dataclass
class SclLN:
    prefix: str
    ln_class: str
    inst: str
    ln_type: str
    desc: str = ""
    datasets: list = field(default_factory=list)
    rcbs: list = field(default_factory=list)
    inits: dict = field(default_factory=dict)  # "Mod.ctlModel" -> texto
    descs: dict = field(default_factory=dict)  # "Ind3" -> desc

    @property
    def name(self) -> str:
        return f"{self.prefix}{self.ln_class}{self.inst}"


@dataclass
class SclLD:
    inst: str
    ld_name: str = ""
    desc: str = ""
    lns: list = field(default_factory=list)


@dataclass
class SclIED:
    name: str
    manufacturer: str = ""
    type: str = ""
    desc: str = ""
    config_version: str = ""
    services: dict = field(default_factory=dict)
    lds: list = field(default_factory=list)
    ap_name: str = ""
    address: dict = field(default_factory=dict)


@dataclass
class SclFile:
    edition: int
    edition_label: str
    version: str
    revision: str
    ieds: list
    templates: Templates
    errors: list
    warnings: list
    tree: ET.ElementTree | None = None


class SclError(Exception):
    pass


def _parse_templates(root: ET.Element, errors: list) -> Templates:
    tpl = Templates()
    dtt = _first(root, "DataTypeTemplates")
    if dtt is None:
        errors.append("Arquivo sem DataTypeTemplates")
        return tpl
    for el in _kids(dtt, "LNodeType"):
        lt = SclLNodeType(el.get("id", ""), el.get("lnClass", ""))
        for d in _kids(el, "DO"):
            lt.dos.append((d.get("name", ""), d.get("type", ""), d.get("desc", "")))
        tpl.lnode[lt.id] = lt
    for el in _kids(dtt, "DOType"):
        dt = SclDOType(el.get("id", ""), el.get("cdc", ""), desc=el.get("desc", ""))
        for c in el:
            if not isinstance(c.tag, str):
                continue
            tn = _t(c)
            if tn == "DA":
                trg = frozenset(k for k in ("dchg", "qchg", "dupd") if _bool(c.get(k)))
                dt.elems.append(("DA", SclDA(c.get("name", ""), c.get("bType", ""), c.get("type", ""),
                                             c.get("fc", ""), int(c.get("count", "0") or 0), trg, _val(c),
                                             c.get("desc", ""))))
            elif tn == "SDO":
                dt.elems.append(("SDO", c.get("name", ""), c.get("type", ""), c.get("desc", "")))
        tpl.dotype[dt.id] = dt
    for el in _kids(dtt, "DAType"):
        bdas = []
        for c in _kids(el, "BDA"):
            bdas.append(SclDA(c.get("name", ""), c.get("bType", ""), c.get("type", ""), "",
                              int(c.get("count", "0") or 0), frozenset(), _val(c)))
        tpl.datype[el.get("id", "")] = bdas
    for el in _kids(dtt, "EnumType"):
        vals = {}
        for c in _kids(el, "EnumVal"):
            try:
                vals[int(c.get("ord", "0"))] = (c.text or "").strip()
            except ValueError:
                pass
        tpl.enum[el.get("id", "")] = vals
    return tpl


def _parse_inits(doi: ET.Element, prefix: str, inits: dict):
    for c in doi:
        if not isinstance(c.tag, str):
            continue
        tn = _t(c)
        if tn == "DAI":
            v = _val(c)
            if v is not None:
                inits[f"{prefix}.{c.get('name', '')}"] = v
        elif tn == "SDI":
            ix = c.get("ix")
            nm = c.get("name", "") + (f"({ix})" if ix else "")
            _parse_inits(c, f"{prefix}.{nm}", inits)


def _parse_ln(el: ET.Element) -> SclLN:
    ln = SclLN(el.get("prefix", ""), el.get("lnClass", ""), el.get("inst", ""), el.get("lnType", ""),
               el.get("desc", ""))
    for ds in _kids(el, "DataSet"):
        d = SclDataSet(ds.get("name", ""), ds.get("desc", ""))
        for f in _kids(ds, "FCDA"):
            d.fcdas.append(SclFcda(f.get("ldInst", ""), f.get("prefix", ""), f.get("lnClass", ""),
                                   f.get("lnInst", ""), f.get("doName", ""), f.get("daName", ""),
                                   f.get("fc", "")))
        ln.datasets.append(d)
    for rc in _kids(el, "ReportControl"):
        r = SclRcb(rc.get("name", ""), rc.get("datSet", ""), rc.get("rptID", ""),
                   int(rc.get("confRev", "1") or 1), _bool(rc.get("buffered")),
                   int(rc.get("bufTime", "0") or 0), int(rc.get("intgPd", "0") or 0),
                   _bool(rc.get("indexed"), True), desc=rc.get("desc", ""))
        tg = _first(rc, "TrgOps")
        if tg is not None:
            r.trgops = {k: _bool(tg.get(k)) for k in ("dchg", "qchg", "dupd", "period", "gi")}
        of = _first(rc, "OptFields")
        if of is not None:
            r.optflds = {k: _bool(of.get(k)) for k in ("seqNum", "timeStamp", "dataSet", "reasonCode",
                                                        "dataRef", "entryID", "configRef", "bufOvfl",
                                                        "segmentation")}
        re_ = _first(rc, "RptEnabled")
        r.max = int(re_.get("max", "1") or 1) if re_ is not None else 1
        ln.rcbs.append(r)
    for doi in _kids(el, "DOI"):
        if doi.get("desc"):
            ln.descs[doi.get("name", "")] = doi.get("desc")
        dv = None
        for dai in _kids(doi, "DAI"):
            if dai.get("name") == "d":
                dv = _val(dai)
        if dv and doi.get("name", "") not in ln.descs:
            ln.descs[doi.get("name", "")] = dv
        _parse_inits(doi, doi.get("name", ""), ln.inits)
    return ln


def _parse_services(el: ET.Element | None) -> dict:
    s: dict = {}
    if el is None:
        return s
    for c in el:
        if not isinstance(c.tag, str):
            continue
        s[_t(c)] = dict(c.attrib)
    return s


def _parse_comm(root: ET.Element) -> dict:
    out: dict = {}
    comm = _first(root, "Communication")
    if comm is None:
        return out
    for sn in _kids(comm, "SubNetwork"):
        for cap in _kids(sn, "ConnectedAP"):
            addr = _first(cap, "Address")
            ps = {}
            if addr is not None:
                for p in _kids(addr, "P"):
                    ps[p.get("type", "")] = (p.text or "").strip()
            out.setdefault(cap.get("iedName", ""), {})[cap.get("apName", "")] = dict(ps, subnet=sn.get("name", ""))
    return out


def parse_scl(data: bytes) -> SclFile:
    try:
        tree = ET.ElementTree(ET.fromstring(data))
    except ET.ParseError as e:
        raise SclError(f"XML inválido: {e}") from e
    root = tree.getroot()
    if _t(root) != "SCL":
        raise SclError("O arquivo não é SCL (raiz diferente de <SCL>)")
    ns = root.tag[1:].split("}")[0] if root.tag.startswith("{") else ""
    version = root.get("version", "")
    revision = root.get("revision", "")
    errors: list = []
    warnings: list = []
    if ns != NS_SCL:
        warnings.append(f"Namespace SCL inesperado: {ns or '(nenhum)'}")
    if version.startswith("2007"):
        edition = 2
        label = f"Edição 2{'.1' if revision >= 'B' else ''} (xmlns 2003, version {version}{revision})"
    else:
        edition = 1
        label = "Edição 1 (xmlns 2003, sem version)"
    tpl = _parse_templates(root, errors)
    comm = _parse_comm(root)
    ieds = []
    for iel in _kids(root, "IED"):
        ied = SclIED(iel.get("name", ""), iel.get("manufacturer", ""), iel.get("type", ""),
                     iel.get("desc", ""), iel.get("configVersion", ""))
        ied.services = _parse_services(_first(iel, "Services"))
        for ap in _kids(iel, "AccessPoint"):
            srv = _first(ap, "Server")
            if srv is None:
                continue
            if not ied.ap_name:
                ied.ap_name = ap.get("name", "")
                ied.address = comm.get(ied.name, {}).get(ied.ap_name, {})
                if not ied.services:
                    ied.services = _parse_services(_first(ap, "Services"))
            for ldel in _kids(srv, "LDevice"):
                ld = SclLD(ldel.get("inst", ""), ldel.get("ldName", ""), ldel.get("desc", ""))
                ln0 = _first(ldel, "LN0")
                if ln0 is not None:
                    ld.lns.append(_parse_ln(ln0))
                else:
                    errors.append(f"LDevice {ld.inst} sem LN0")
                for lnel in _kids(ldel, "LN"):
                    ld.lns.append(_parse_ln(lnel))
                ied.lds.append(ld)
        ieds.append(ied)
    if not ieds:
        raise SclError("Nenhum IED encontrado no arquivo")
    return SclFile(edition, label, version, revision, ieds, tpl, errors, warnings, tree)


# ---------------------------------------------------------------------------
# Exportação de CID com DataSets e RCBs extras (RN-CID-06)

def export_cid(original: bytes, ied_name: str, extras_ds: list, extras_rcb: list) -> bytes:
    """Gera um CID novo acrescentando DataSets/ReportControls extras ao LN0 indicado.

    extras_ds: [{"ld": inst, "ln": "LLN0", "name":..., "fcdas":[SclFcda-like dict]}]
    extras_rcb: [{"ld":..., "ln":..., "name":..., "datset":..., ...}]
    """
    ET.register_namespace("", NS_SCL)
    root = ET.fromstring(original)
    ns = root.tag[1:].split("}")[0] if root.tag.startswith("{") else ""
    q = (lambda n: f"{{{ns}}}{n}") if ns else (lambda n: n)
    for iel in _kids(root, "IED"):
        if iel.get("name") != ied_name:
            continue
        lds = {ld.get("inst"): ld for ap in _kids(iel, "AccessPoint")
               for srv in _kids(ap, "Server") for ld in _kids(srv, "LDevice")}

        def find_ln(ld_inst, ln_name):
            ld = lds.get(ld_inst)
            if ld is None:
                return None
            for c in ld:
                if isinstance(c.tag, str) and _t(c) in ("LN0", "LN"):
                    nm = c.get("prefix", "") + c.get("lnClass", "") + c.get("inst", "")
                    if nm == ln_name:
                        return c
            return None

        for ds in extras_ds:
            ln = find_ln(ds["ld"], ds.get("ln", "LLN0"))
            if ln is None:
                continue
            el = ET.Element(q("DataSet"), {"name": ds["name"], "desc": ds.get("desc", "extra de teste")})
            for f in ds["fcdas"]:
                attrs = {k: v for k, v in (("ldInst", f.get("ld_inst")), ("prefix", f.get("prefix")),
                                            ("lnClass", f.get("ln_class")), ("lnInst", f.get("ln_inst")),
                                            ("doName", f.get("do_name")), ("daName", f.get("da_name")),
                                            ("fc", f.get("fc"))) if v}
                if "prefix" not in attrs:
                    attrs["prefix"] = ""
                if "lnInst" not in attrs:
                    attrs["lnInst"] = ""
                ET.SubElement(el, q("FCDA"), attrs)
            _insert_after(ln, el, ("DataSet",))
        for r in extras_rcb:
            ln = find_ln(r["ld"], r.get("ln", "LLN0"))
            if ln is None:
                continue
            el = ET.Element(q("ReportControl"), {
                "name": r["name"], "datSet": r["datset"], "rptID": r.get("rptid", ""),
                "confRev": str(r.get("conf_rev", 1)), "buffered": "true" if r.get("buffered") else "false",
                "bufTime": str(r.get("buf_time", 0)), "intgPd": str(r.get("intg_pd", 0)),
                "desc": r.get("desc", "extra de teste")})
            tg = r.get("trgops", {})
            ET.SubElement(el, q("TrgOps"), {k: "true" if v else "false" for k, v in tg.items()})
            of = r.get("optflds", {})
            ET.SubElement(el, q("OptFields"), {k: "true" if v else "false" for k, v in of.items()})
            ET.SubElement(el, q("RptEnabled"), {"max": str(r.get("max", 1))})
            _insert_after(ln, el, ("DataSet", "ReportControl"))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _insert_after(parent: ET.Element, el: ET.Element, after: tuple):
    idx = 0
    for i, c in enumerate(list(parent)):
        if isinstance(c.tag, str) and _t(c) in ("Private", "Text") + after:
            idx = i + 1
    parent.insert(idx, el)


def copy_tree(t):
    return copy.deepcopy(t)

"""Persistência local em SQLite (seção 11 e requisitos de persistência da seção 14)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ied (id INTEGER PRIMARY KEY AUTOINCREMENT, cfg TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS point_state (ied_id INTEGER, path TEXT, data TEXT, PRIMARY KEY (ied_id, path));
CREATE TABLE IF NOT EXISTS scenario (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ensaio (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report_log (
  id INTEGER PRIMARY KEY, ied_id INTEGER, ts REAL, rcb TEXT, rptid TEXT, entry_id TEXT, sqnum INTEGER,
  tipo TEXT, cliente TEXT, pontos TEXT, pdu BLOB, notas TEXT, change_ids TEXT, descartado INTEGER);
CREATE INDEX IF NOT EXISTS ix_report_ied ON report_log(ied_id, ts);
CREATE TABLE IF NOT EXISTS change_log (
  id INTEGER PRIMARY KEY, ied_id INTEGER, ts REAL, path TEXT, valor TEXT, qualidade TEXT, t TEXT, origem TEXT);
CREATE INDEX IF NOT EXISTS ix_change_ied ON change_log(ied_id, ts);
CREATE TABLE IF NOT EXISTS mms_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ied_id INTEGER, ts REAL, cliente TEXT, servico TEXT, detalhe TEXT,
  resultado TEXT);
CREATE INDEX IF NOT EXISTS ix_mms_ied ON mms_log(ied_id, ts);
CREATE TABLE IF NOT EXISTS rcb_denied (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ied_id INTEGER, ts REAL, rcb TEXT, atributo TEXT, codigo INTEGER,
  ip TEXT, cliente TEXT, politica TEXT, motivo TEXT);
CREATE INDEX IF NOT EXISTS ix_rcb_denied_ied ON rcb_denied(ied_id, ts);
CREATE TABLE IF NOT EXISTS cmd_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ied_id INTEGER, ts REAL, data TEXT);
CREATE INDEX IF NOT EXISTS ix_cmd_ied ON cmd_log(ied_id, ts);
CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, pwhash TEXT, salt TEXT, papel TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.q_reports: list = []
        self.q_changes: list = []
        self.q_mms: list = []
        self.q_denied: list = []
        self.q_points: dict = {}
        self.next_report_id = (self.one("SELECT MAX(id) FROM report_log") or 0) + 1
        self.next_change_id = (self.one("SELECT MAX(id) FROM change_log") or 0) + 1

    def one(self, sql: str, *args):
        r = self.db.execute(sql, args).fetchone()
        return r[0] if r else None

    # ------------------------------------------------------------------ IEDs
    def ieds(self) -> list[tuple[int, dict]]:
        return [(r[0], json.loads(r[1])) for r in self.db.execute("SELECT id, cfg FROM ied ORDER BY id")]

    def add_ied(self, cfg: dict) -> int:
        cur = self.db.execute("INSERT INTO ied (cfg) VALUES (?)", (json.dumps(cfg),))
        return cur.lastrowid

    def save_ied(self, iid: int, cfg: dict):
        self.db.execute("UPDATE ied SET cfg=? WHERE id=?", (json.dumps(cfg), iid))

    def del_ied(self, iid: int):
        self.flush()
        self.db.execute("DELETE FROM ied WHERE id=?", (iid,))
        self.db.execute("DELETE FROM point_state WHERE ied_id=?", (iid,))
        self.db.execute("DELETE FROM rcb_denied WHERE ied_id=?", (iid,))
        self.db.execute("DELETE FROM cmd_log WHERE ied_id=?", (iid,))

    def point_states(self, iid: int) -> dict:
        self.flush()
        return {r[0]: json.loads(r[1]) for r in
                self.db.execute("SELECT path, data FROM point_state WHERE ied_id=?", (iid,))}

    def rename_points(self, iid: int, mapa: dict):
        """Leva o estado salvo de cada ponto para o novo caminho (reimportação com pontos renomeados)."""
        if not mapa:
            return
        self.flush()
        states = self.point_states(iid)
        mov = {para: states[de] for de, para in mapa.items() if de in states}
        with self.lock:
            self.db.executemany("DELETE FROM point_state WHERE ied_id=? AND path=?", [(iid, de) for de in mapa])
            self.db.executemany("INSERT OR REPLACE INTO point_state VALUES (?,?,?)",
                                [(iid, para, json.dumps(st)) for para, st in mov.items()])

    def queue_point(self, iid: int, path: str, data: dict):
        self.q_points[(iid, path)] = data

    # ------------------------------------------------------------------ registros
    def new_report_id(self) -> int:
        i = self.next_report_id
        self.next_report_id += 1
        return i

    def new_change_id(self) -> int:
        i = self.next_change_id
        self.next_change_id += 1
        return i

    def queue_report(self, r: dict):
        self.q_reports.append(r)

    def queue_change(self, c: dict):
        self.q_changes.append(c)

    def queue_mms(self, m: dict):
        self.q_mms.append(m)

    # ------------------------------------------------------------------ comandos (gravados na hora: são poucos)
    def add_cmd(self, rec: dict) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO cmd_log (ied_id, ts, data) VALUES (?,?,?)",
                                  (rec["ied_id"], rec["ts"], json.dumps(rec, default=str)))
            return cur.lastrowid

    def update_cmd(self, rec: dict):
        with self.lock:
            self.db.execute("UPDATE cmd_log SET data=? WHERE id=?", (json.dumps(rec, default=str), rec["id"]))

    def cmds(self, iid: int, limit: int = 500, refs: list[str] | None = None) -> list[dict]:
        out = []
        filtro, args = "", [iid]
        if refs:
            filtro = f" AND json_extract(data, '$.ref') IN ({','.join('?' * len(refs))})"
            args += refs
        for i, d in self.db.execute(f"SELECT id, data FROM cmd_log WHERE ied_id=?{filtro} ORDER BY id DESC LIMIT ?",
                                    (*args, limit)):
            r = json.loads(d)
            r["id"] = i
            out.append(r)
        return out

    def clear_cmds(self, iid: int):
        self.db.execute("DELETE FROM cmd_log WHERE ied_id=?", (iid,))

    def queue_rcb_denied(self, d: dict):
        self.q_denied.append(d)

    def flush(self):
        with self.lock:
            reps, self.q_reports = self.q_reports, []
            chg, self.q_changes = self.q_changes, []
            mms, self.q_mms = self.q_mms, []
            den, self.q_denied = self.q_denied, []
            pts, self.q_points = self.q_points, {}
            if not (reps or chg or mms or den or pts):
                return
            self.db.execute("BEGIN")
            try:
                if reps:
                    self.db.executemany(
                        "INSERT OR REPLACE INTO report_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(r["id"], r["ied_id"], r["ts"], r["rcb"], r["rptid"], r["entry_id"], r["sqnum"], r["tipo"],
                          r["cliente"], json.dumps(r["pontos"]), bytes.fromhex(r["pdu"]), json.dumps(r["notas"]),
                          json.dumps(r["change_ids"]), 1 if r["descartado"] else 0) for r in reps])
                if chg:
                    self.db.executemany(
                        "INSERT OR REPLACE INTO change_log VALUES (?,?,?,?,?,?,?,?)",
                        [(c["id"], c["ied_id"], c["ts"], c["path"], json.dumps(c["valor"]),
                          json.dumps(c["qualidade"]), c["t"], c["origem"]) for c in chg])
                if mms:
                    self.db.executemany(
                        "INSERT INTO mms_log (ied_id, ts, cliente, servico, detalhe, resultado) VALUES (?,?,?,?,?,?)",
                        [(m["ied_id"], m["ts"], m["cliente"], m["servico"], m["detalhe"], m["resultado"])
                         for m in mms])
                if den:
                    self.db.executemany(
                        "INSERT INTO rcb_denied (ied_id, ts, rcb, atributo, codigo, ip, cliente, politica, motivo) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        [(d["ied_id"], d["ts"], d["rcb"], d["atributo"], d["codigo"], d["ip"], d["cliente"],
                          d["politica"], d["motivo"]) for d in den])
                if pts:
                    self.db.executemany("INSERT OR REPLACE INTO point_state VALUES (?,?,?)",
                                        [(k[0], k[1], json.dumps(v)) for k, v in pts.items()])
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ consultas
    def reports(self, iid: int, since: float | None = None, until: float | None = None, limit: int = 500,
                rcb: str | None = None) -> list[dict]:
        self.flush()
        sql = "SELECT * FROM report_log WHERE ied_id=?"
        args: list = [iid]
        if since:
            sql += " AND ts>=?"
            args.append(since)
        if until:
            sql += " AND ts<=?"
            args.append(until)
        if rcb:
            sql += " AND rcb LIKE ?"
            args.append(f"%{rcb}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self.db.execute(sql, args):
            out.append({"id": r[0], "ied_id": r[1], "ts": r[2], "rcb": r[3], "rptid": r[4], "entry_id": r[5],
                        "sqnum": r[6], "tipo": r[7], "cliente": r[8], "pontos": json.loads(r[9] or "[]"),
                        "pdu": (r[10] or b"").hex(), "notas": json.loads(r[11] or "[]"),
                        "change_ids": json.loads(r[12] or "[]"), "descartado": bool(r[13])})
        return out

    def changes(self, iid: int, since: float | None = None, limit: int = 500, path: str | None = None) -> list[dict]:
        self.flush()
        sql = "SELECT * FROM change_log WHERE ied_id=?"
        args: list = [iid]
        if since:
            sql += " AND ts>=?"
            args.append(since)
        if path:
            sql += " AND path LIKE ?"
            args.append(f"%{path}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = [{"id": r[0], "ied_id": r[1], "ts": r[2], "path": r[3], "valor": json.loads(r[4]),
                 "qualidade": json.loads(r[5]), "t": r[6], "origem": r[7]} for r in self.db.execute(sql, args)]
        if rows:
            ids = {r["id"]: r for r in rows}
            for r in rows:
                r["reports"] = []
            lo = min(ids)
            for rid, cids in self.db.execute(
                    "SELECT id, change_ids FROM report_log WHERE ied_id=? AND id>=0 AND ts>=? ORDER BY id",
                    (iid, rows[-1]["ts"] - 1)):
                for c in json.loads(cids or "[]"):
                    if c in ids and c >= lo:
                        ids[c]["reports"].append(rid)
        return rows

    def mms_log(self, iid: int, limit: int = 500) -> list[dict]:
        self.flush()
        return [{"id": r[0], "ts": r[2], "cliente": r[3], "servico": r[4], "detalhe": r[5], "resultado": r[6]}
                for r in self.db.execute("SELECT * FROM mms_log WHERE ied_id=? ORDER BY id DESC LIMIT ?",
                                         (iid, limit))]

    def rcb_denied(self, iid: int, limit: int = 500, rcb: str | None = None) -> list[dict]:
        self.flush()
        sql = "SELECT * FROM rcb_denied WHERE ied_id=?"
        args: list = [iid]
        if rcb:
            sql += " AND rcb LIKE ?"
            args.append(f"%{rcb}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [{"id": r[0], "ts": r[2], "rcb": r[3], "atributo": r[4], "codigo": r[5], "ip": r[6],
                 "cliente": r[7], "politica": r[8], "motivo": r[9]} for r in self.db.execute(sql, args)]

    def clear_logs(self, iid: int):
        self.flush()
        for t in ("report_log", "change_log", "mms_log", "rcb_denied"):
            self.db.execute(f"DELETE FROM {t} WHERE ied_id=?", (iid,))

    # ------------------------------------------------------------------ cenários / ensaios
    def list_json(self, table: str) -> list[dict]:
        return [dict(json.loads(r[1]), id=r[0]) for r in self.db.execute(f"SELECT id, data FROM {table} ORDER BY id")]

    def get_json(self, table: str, iid: int) -> dict | None:
        r = self.db.execute(f"SELECT id, data FROM {table} WHERE id=?", (iid,)).fetchone()
        return dict(json.loads(r[1]), id=r[0]) if r else None

    def add_json(self, table: str, data: dict) -> int:
        data = {k: v for k, v in data.items() if k != "id"}
        return self.db.execute(f"INSERT INTO {table} (data) VALUES (?)", (json.dumps(data),)).lastrowid

    def put_json(self, table: str, iid: int, data: dict):
        data = {k: v for k, v in data.items() if k != "id"}
        self.db.execute(f"UPDATE {table} SET data=? WHERE id=?", (json.dumps(data), iid))

    def del_json(self, table: str, iid: int):
        self.db.execute(f"DELETE FROM {table} WHERE id=?", (iid,))

    # ------------------------------------------------------------------ usuários
    def user(self, name: str):
        return self.db.execute("SELECT username, pwhash, salt, papel FROM users WHERE username=?", (name,)).fetchone()

    def set_user(self, name: str, pwhash: str, salt: str, papel: str):
        self.db.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?)", (name, pwhash, salt, papel))

    def users(self) -> list:
        return [{"usuario": r[0], "papel": r[1]} for r in self.db.execute("SELECT username, papel FROM users ORDER BY username")]

    def set_role(self, name: str, papel: str):
        self.db.execute("UPDATE users SET papel=? WHERE username=?", (papel, name))

    def del_user(self, name: str):
        self.db.execute("DELETE FROM users WHERE username=?", (name,))

    def backup_to(self, path):
        """Cópia consistente do banco (API de backup do SQLite, com o banco em uso)."""
        import sqlite3
        self.flush()
        dst = sqlite3.connect(str(path))
        try:
            self.db.backup(dst)
        finally:
            dst.close()

    def close(self):
        self.flush()
        self.db.close()


def now() -> float:
    return time.time()

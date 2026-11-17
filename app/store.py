"""SQLite 存储：批次、表计契约、读数、区间、月度分摊、账单、隔离、阶段、合计、清单。

约定：
- 用量与金额一律 TEXT（Decimal 字符串），不落 float；
- 一次运行的所有行在同一个事务里「先删后插」，失败整体回滚，不留半批数据；
- 页面永远只展示一个批次，按 batch_id 过滤。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    batch_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sample TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    input_digest TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_key ON batches(batch_key, status);
CREATE TABLE IF NOT EXISTS meters (
    batch_id TEXT NOT NULL, meter_id TEXT NOT NULL, name TEXT NOT NULL,
    capacity TEXT NOT NULL, multiplier TEXT NOT NULL, unit TEXT NOT NULL,
    registers TEXT NOT NULL, replaced_at TEXT, replaced_by TEXT, tariff TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
    batch_id TEXT NOT NULL, lineno INTEGER NOT NULL, segment INTEGER NOT NULL,
    meter_id TEXT NOT NULL, register TEXT NOT NULL, read_at TEXT NOT NULL,
    reading_text TEXT NOT NULL, unit_raw TEXT NOT NULL, reading_norm TEXT,
    unit_norm TEXT, read_type TEXT NOT NULL, status TEXT NOT NULL,
    code TEXT, flags_json TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS intervals (
    batch_id TEXT NOT NULL, interval_id TEXT NOT NULL, meter_id TEXT NOT NULL,
    register TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
    prev_reading TEXT NOT NULL, cur_reading TEXT NOT NULL, delta TEXT NOT NULL,
    multiplier TEXT NOT NULL, unit TEXT NOT NULL, usage_kwh TEXT NOT NULL,
    days TEXT NOT NULL, method TEXT NOT NULL, flags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS allocations (
    batch_id TEXT NOT NULL, month TEXT NOT NULL, meter_id TEXT NOT NULL,
    register TEXT NOT NULL, usage_kwh TEXT NOT NULL, exact_share TEXT NOT NULL,
    tou_adjusted INTEGER NOT NULL DEFAULT 0, sources_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS bills (
    batch_id TEXT NOT NULL, month TEXT NOT NULL, meter_id TEXT NOT NULL,
    usage_kwh TEXT NOT NULL, amount_yuan TEXT NOT NULL,
    components_json TEXT NOT NULL, adjust_cents INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quarantine (
    batch_id TEXT NOT NULL, lineno INTEGER NOT NULL, segment INTEGER NOT NULL,
    code TEXT NOT NULL, stage TEXT NOT NULL, level TEXT NOT NULL,
    field TEXT, raw_value TEXT, meter_id TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS stages (
    batch_id TEXT NOT NULL, stage TEXT NOT NULL, in_count INTEGER NOT NULL,
    out_count INTEGER NOT NULL, quarantine_count INTEGER NOT NULL,
    status TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS totals (
    batch_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifests (
    batch_id TEXT PRIMARY KEY, json TEXT NOT NULL
);
-- 跨站补估：邻站实测台账（只收实测；估值永不写进这张表）
CREATE TABLE IF NOT EXISTS hydro_observations (
    station_id TEXT NOT NULL,
    obs_date TEXT NOT NULL,
    rainfall_mm TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'measured' CHECK (kind = 'measured'),
    revision INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (station_id, obs_date)
);
CREATE INDEX IF NOT EXISTS idx_hydro_obs_month
    ON hydro_observations(station_id, obs_date);
CREATE TABLE IF NOT EXISTS hydro_neighbors (
    station_id TEXT NOT NULL,
    neighbor_id TEXT NOT NULL,
    coefficient TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (station_id, neighbor_id)
);
-- 已发应急补估：basis_json / result_json 是不可变快照，状态变化不改快照
CREATE TABLE IF NOT EXISTS supplement_estimates (
    estimate_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    target_station TEXT NOT NULL,
    month TEXT NOT NULL,
    neighbors_json TEXT NOT NULL,
    basis_digest TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    measured_mm TEXT NOT NULL,
    estimated_mm TEXT NOT NULL,
    total_mm TEXT NOT NULL,
    gap_days INTEGER NOT NULL,
    estimated_days INTEGER NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_job_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_supplement_target
    ON supplement_estimates(target_station, month, status);
CREATE TABLE IF NOT EXISTS supplement_entries (
    estimate_id TEXT NOT NULL,
    obs_date TEXT NOT NULL,
    estimated_mm TEXT NOT NULL,
    neighbor_basis_json TEXT NOT NULL,
    status TEXT NOT NULL,
    superseded_by_revision INTEGER,
    superseded_at TEXT,
    PRIMARY KEY (estimate_id, obs_date)
);
CREATE TABLE IF NOT EXISTS supplement_dependencies (
    estimate_id TEXT NOT NULL,
    station_id TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY (estimate_id, station_id, role)
);
CREATE INDEX IF NOT EXISTS idx_supplement_dep
    ON supplement_dependencies(station_id, role);
CREATE TABLE IF NOT EXISTS supplement_fillins (
    idempotency_key TEXT PRIMARY KEY,
    station_id TEXT NOT NULL,
    obs_date TEXT NOT NULL,
    rainfall_mm TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
-- 撤销作业：意图先落库，逐条处理；中断后 resume 从 pending 继续
CREATE TABLE IF NOT EXISTS supplement_revocations (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    total INTEGER NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supplement_revocation_items (
    job_id TEXT NOT NULL,
    estimate_id TEXT NOT NULL,
    status TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    processed_at TEXT,
    PRIMARY KEY (job_id, estimate_id)
);
"""


def _dec(text):
    return None if text is None else Decimal(str(text))


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in
                       conn.execute("PRAGMA table_info(batches)").fetchall()}
            if "updated_at" not in columns:
                conn.execute("ALTER TABLE batches ADD COLUMN updated_at TEXT")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 批次 ----------
    def get_batch(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM batches WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return dict(row) if row else None

    def find_done_by_key(self, batch_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? AND status='done' "
                "ORDER BY created_at LIMIT 1", (batch_key,)).fetchone()
        return dict(row) if row else None

    def find_by_key(self, batch_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? ORDER BY created_at DESC LIMIT 1",
                (batch_key,)).fetchone()
        return dict(row) if row else None

    def latest_batch(self, status: str = "done") -> dict | None:
        with self.connect() as conn:
            order = "COALESCE(updated_at, created_at) DESC, rowid DESC"
            row = conn.execute(
                f"SELECT * FROM batches WHERE status=? ORDER BY {order} LIMIT 1",
                (status,)).fetchone()
            if row is None:
                row = conn.execute(
                    f"SELECT * FROM batches ORDER BY {order} LIMIT 1").fetchone()
        return dict(row) if row else None

    def list_batches(self, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY COALESCE(updated_at, created_at) DESC, "
                "rowid DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def save_attempt(self, *, batch: dict, stages: list[dict], note: str = "") -> None:
        """失败尝试：只写批次与阶段，不动已有批次的数据。"""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,sample,status,created_at,
                        finished_at,input_digest,rule_version,params_json,note,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     note=excluded.note, run_id=excluded.run_id,
                     updated_at=excluded.updated_at""",
                (batch["batch_id"], batch["batch_key"], batch["run_id"], batch["sample"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch["rule_version"],
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 note or batch.get("note", ""), batch.get("updated_at")))
            conn.execute("DELETE FROM stages WHERE batch_id=?", (batch["batch_id"],))
            for s in stages:
                conn.execute(
                    """INSERT INTO stages (batch_id,stage,in_count,out_count,
                        quarantine_count,status,elapsed_ms,note) VALUES (?,?,?,?,?,?,?,?)""",
                    (batch["batch_id"], s["stage"], s["in_count"], s["out_count"],
                     s["quarantine_count"], s["status"], s["elapsed_ms"],
                     s.get("note", "")))

    def touch_batch(self, batch_id: str, stamp: str) -> None:
        """命中已有批次时刷新「最近一次运行」，页面才会停在这次看的结果上。"""
        with self.connect() as conn:
            conn.execute("UPDATE batches SET updated_at=? WHERE batch_id=?",
                         (stamp, batch_id))

    # ---------- 一次运行整体落库 ----------
    def save_run(self, *, batch: dict, meters: list[dict], readings: list[dict],
                 intervals: list[dict], allocations: list[dict], bills: list[dict],
                 quarantine: list[dict], stages: list[dict], totals: dict,
                 manifest_json: dict) -> None:
        b = batch["batch_id"]
        with self.connect() as conn:
            for table in ("meters", "readings", "intervals", "allocations", "bills",
                          "quarantine", "stages", "totals"):
                conn.execute(f"DELETE FROM {table} WHERE batch_id=?", (b,))
            for m in meters:
                conn.execute(
                    """INSERT INTO meters (batch_id,meter_id,name,capacity,multiplier,unit,
                        registers,replaced_at,replaced_by,tariff)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (b, m["meter_id"], m["name"], str(m["capacity"]),
                     str(m["multiplier"]), m["unit"], ";".join(m["registers"]),
                     m.get("replaced_at"), m.get("replaced_by"), m["tariff"]))
            for r in readings:
                conn.execute(
                    """INSERT INTO readings (batch_id,lineno,segment,meter_id,register,
                        read_at,reading_text,unit_raw,reading_norm,unit_norm,read_type,
                        status,code,flags_json,note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (b, r["lineno"], r["segment"], r["meter_id"], r["register"],
                     r["read_at"], r["reading_text"], r["unit_raw"],
                     None if r.get("reading_norm") is None else str(r["reading_norm"]),
                     r.get("unit_norm"), r["read_type"], r["status"], r.get("code"),
                     json.dumps(r.get("flags", []), ensure_ascii=False),
                     r.get("note", "")))
            for i in intervals:
                conn.execute(
                    """INSERT INTO intervals (batch_id,interval_id,meter_id,register,
                        start_at,end_at,prev_reading,cur_reading,delta,multiplier,unit,
                        usage_kwh,days,method,flags_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (b, i["interval_id"], i["meter_id"], i["register"], i["start_at"],
                     i["end_at"], str(i["prev_reading"]), str(i["cur_reading"]),
                     str(i["delta"]), str(i["multiplier"]), i["unit"],
                     str(i["usage_kwh"]), str(i["days"]), i["method"],
                     json.dumps(i.get("flags", []), ensure_ascii=False)))
            for a in allocations:
                conn.execute(
                    """INSERT INTO allocations (batch_id,month,meter_id,register,usage_kwh,
                        exact_share,tou_adjusted,sources_json)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (b, a["month"], a["meter_id"], a["register"], str(a["usage_kwh"]),
                     str(a["exact_share"]), 1 if a.get("tou_adjusted") else 0,
                     json.dumps(a.get("sources", []), ensure_ascii=False)))
            for bill in bills:
                conn.execute(
                    """INSERT INTO bills (batch_id,month,meter_id,usage_kwh,amount_yuan,
                        components_json,adjust_cents) VALUES (?,?,?,?,?,?,?)""",
                    (b, bill["month"], bill["meter_id"], str(bill["usage_kwh"]),
                     str(bill["amount_yuan"]),
                     json.dumps(bill["components"], ensure_ascii=False),
                     int(bill.get("adjust_cents", 0))))
            for q in quarantine:
                conn.execute(
                    """INSERT INTO quarantine (batch_id,lineno,segment,code,stage,level,
                        field,raw_value,meter_id,detail) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (b, q["lineno"], q.get("segment", 1), q["code"], q["stage"],
                     q["level"], q.get("field"), q.get("raw_value"),
                     q.get("meter_id"), q.get("detail")))
            for s in stages:
                conn.execute(
                    """INSERT INTO stages (batch_id,stage,in_count,out_count,
                        quarantine_count,status,elapsed_ms,note) VALUES (?,?,?,?,?,?,?,?)""",
                    (b, s["stage"], s["in_count"], s["out_count"],
                     s["quarantine_count"], s["status"], s["elapsed_ms"],
                     s.get("note", "")))
            for key, value in totals.items():
                conn.execute("INSERT INTO totals (batch_id,key,value) VALUES (?,?,?)",
                             (b, key, str(value)))
            conn.execute(
                "INSERT INTO manifests (batch_id,json) VALUES (?,?) "
                "ON CONFLICT(batch_id) DO UPDATE SET json=excluded.json",
                (b, json.dumps(manifest_json, ensure_ascii=False)))
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,sample,status,created_at,
                        finished_at,input_digest,rule_version,params_json,note,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     run_id=excluded.run_id, note=excluded.note,
                     updated_at=excluded.updated_at""",
                (b, batch["batch_key"], batch["run_id"], batch["sample"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch["rule_version"],
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 batch.get("note", ""), batch.get("updated_at")))

    # ---------- 读取 ----------
    def load_meters(self, batch_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM meters WHERE batch_id=? ORDER BY rowid", (batch_id,))
        for r in rows:
            r["capacity"] = _dec(r["capacity"])
            r["multiplier"] = _dec(r["multiplier"])
            r["registers"] = r["registers"].split(";")
        return rows

    def load_readings(self, batch_id: str, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM readings WHERE batch_id=?"
        args: list = [batch_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY lineno"
        rows = self._all(sql, tuple(args))
        for r in rows:
            r["flags"] = json.loads(r.pop("flags_json") or "[]")
            r["reading_norm"] = _dec(r["reading_norm"])
        return rows

    def load_intervals(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM intervals WHERE batch_id=? ORDER BY meter_id, register, start_at",
            (batch_id,))
        for r in rows:
            r["flags"] = json.loads(r.pop("flags_json") or "[]")
            for key in ("prev_reading", "cur_reading", "delta", "multiplier", "usage_kwh",
                        "days"):
                r[key] = _dec(r[key])
        return rows

    def load_allocations(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM allocations WHERE batch_id=? ORDER BY month, meter_id, register",
            (batch_id,))
        for r in rows:
            r["usage_kwh"] = _dec(r["usage_kwh"])
            r["exact_share"] = _dec(r["exact_share"])
            r["sources"] = json.loads(r.pop("sources_json") or "[]")
            r["tou_adjusted"] = bool(r["tou_adjusted"])
        return rows

    def load_bills(self, batch_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM bills WHERE batch_id=? ORDER BY month, meter_id",
                         (batch_id,))
        for r in rows:
            r["usage_kwh"] = _dec(r["usage_kwh"])
            r["amount_yuan"] = _dec(r["amount_yuan"])
            r["components"] = json.loads(r.pop("components_json") or "[]")
        return rows

    def load_quarantine(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM quarantine WHERE batch_id=? ORDER BY lineno", (batch_id,))

    def load_stages(self, batch_id: str) -> list[dict]:
        from . import config
        rows = self._all("SELECT * FROM stages WHERE batch_id=? ORDER BY rowid", (batch_id,))
        for row in rows:
            row["label"] = config.STAGE_LABELS.get(row["stage"], row["stage"])
        return rows

    def load_totals(self, batch_id: str) -> dict:
        rows = self._all("SELECT key, value FROM totals WHERE batch_id=?", (batch_id,))
        return {r["key"]: r["value"] for r in rows}

    def load_manifest(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT json FROM manifests WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return json.loads(row["json"]) if row else None

    def count_rows(self, batch_id: str) -> dict:
        tables = ("meters", "readings", "intervals", "allocations", "bills", "quarantine")
        with self.connect() as conn:
            return {t: conn.execute(
                f"SELECT COUNT(*) AS n FROM {t} WHERE batch_id=?", (batch_id,)
            ).fetchone()["n"] for t in tables}
    # ---------- 跨站补估 ----------
    # 读接口
    def get_observation(self, station_id: str, obs_date: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM hydro_observations WHERE station_id=? AND obs_date=?",
                (station_id, obs_date)).fetchone()
        return dict(row) if row else None

    def observations_between(self, station_id: str, start: str, end: str) -> list[dict]:
        """[start, end] 闭区间内的实测；只读 kind='measured'，估值从不入这张表。"""
        return self._all(
            "SELECT * FROM hydro_observations WHERE station_id=? AND kind='measured' "
            "AND obs_date>=? AND obs_date<=? ORDER BY obs_date",
            (station_id, start, end))

    def load_neighbors(self, station_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM hydro_neighbors WHERE station_id=? ORDER BY neighbor_id",
            (station_id,))

    def all_neighbors(self) -> list[dict]:
        return self._all(
            "SELECT * FROM hydro_neighbors ORDER BY station_id, neighbor_id", ())

    def get_supplement(self, estimate_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM supplement_estimates WHERE estimate_id=?",
                               (estimate_id,)).fetchone()
        return dict(row) if row else None

    def get_supplement_by_key(self, idempotency_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM supplement_estimates WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
        return dict(row) if row else None

    def list_supplements(self, *, target_station: str | None = None,
                         month: str | None = None,
                         include_revoked: bool = True) -> list[dict]:
        sql = "SELECT * FROM supplement_estimates WHERE 1=1"
        args: list = []
        if target_station:
            sql += " AND target_station=?"
            args.append(target_station)
        if month:
            sql += " AND month=?"
            args.append(month)
        if not include_revoked:
            sql += " AND status NOT IN ('revoked')"
        sql += " ORDER BY created_at, estimate_id"
        return self._all(sql, tuple(args))

    def load_supplement_entries(self, estimate_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM supplement_entries WHERE estimate_id=? ORDER BY obs_date",
            (estimate_id,))
        for row in rows:
            row["neighbor_basis"] = json.loads(row.pop("neighbor_basis_json") or "{}")
        return rows

    def load_supplement_dependencies(self, estimate_id: str) -> list[dict]:
        return self._all(
            "SELECT station_id, role FROM supplement_dependencies WHERE estimate_id=? "
            "ORDER BY role, station_id", (estimate_id,))

    # ---------- 写接口（显式事务，整段要么全成要么全不成） ----------
    @contextmanager
    def write_txn(self):
        """BEGIN IMMEDIATE：先拿写锁再读后写，避免并发补测/补估各读旧版本。"""
        conn = sqlite3.connect(self.path, timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def write_observation(self, *, station_id: str, obs_date: str, rainfall_mm: str,
                          source: str, stamp: str,
                          expected_revision: int | None = None) -> dict | None:
        """实测写入，revision 单调 +1；expected_revision 不符返回 None（过期依据）。"""
        with self.write_txn() as conn:
            row = conn.execute(
                "SELECT revision FROM hydro_observations WHERE station_id=? "
                "AND obs_date=?", (station_id, obs_date)).fetchone()
            current = row["revision"] if row else None
            if expected_revision is not None and (current or 0) != expected_revision:
                return None
            revision = (current or 0) + 1
            conn.execute(
                """INSERT INTO hydro_observations
                     (station_id,obs_date,rainfall_mm,kind,revision,source,updated_at)
                   VALUES (?,?,?,'measured',?,?,?)
                   ON CONFLICT(station_id,obs_date) DO UPDATE SET
                     rainfall_mm=excluded.rainfall_mm, revision=excluded.revision,
                     source=excluded.source, updated_at=excluded.updated_at""",
                (station_id, obs_date, rainfall_mm, revision, source, stamp))
            saved = conn.execute(
                "SELECT * FROM hydro_observations WHERE station_id=? AND obs_date=?",
                (station_id, obs_date)).fetchone()
        return dict(saved)

    def write_neighbor(self, *, station_id: str, neighbor_id: str, coefficient: str,
                       stamp: str) -> dict:
        with self.write_txn() as conn:
            conn.execute(
                """INSERT INTO hydro_neighbors
                     (station_id,neighbor_id,coefficient,updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(station_id,neighbor_id) DO UPDATE SET
                     coefficient=excluded.coefficient, updated_at=excluded.updated_at""",
                (station_id, neighbor_id, coefficient, stamp))
            row = conn.execute(
                "SELECT * FROM hydro_neighbors WHERE station_id=? AND neighbor_id=?",
                (station_id, neighbor_id)).fetchone()
        return dict(row)

    def save_supplement(self, *, estimate: dict, entries: list[dict],
                        dependencies: list[dict], basis_check: str = "none") -> bool:
        """补估落库。

        basis_check='strict' 时，在同一写事务里重新核对依据快照：
        - 目标站与邻站在该月的实测集合（日期、值、revision）必须与 basis 完全一致；
        - 邻站系数必须与 basis 一致；
        - 任一变化 → 返回 False（调用方抛过期依据），不写任何行。
        """
        with self.write_txn() as conn:
            if conn.execute(
                    "SELECT 1 FROM supplement_estimates WHERE idempotency_key=?",
                    (estimate["idempotency_key"],)).fetchone() is not None:
                return False
            if basis_check == "strict" and not _basis_matches(conn, estimate["basis"]):
                return False
            conn.execute(
                """INSERT INTO supplement_estimates
                     (estimate_id,idempotency_key,target_station,month,neighbors_json,
                      basis_digest,basis_json,result_json,measured_mm,estimated_mm,
                      total_mm,gap_days,estimated_days,status,status_reason,
                      created_at,updated_at,revoked_at,revoke_job_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)""",
                (estimate["estimate_id"], estimate["idempotency_key"],
                 estimate["target_station"], estimate["month"],
                 json.dumps(estimate["neighbors"], ensure_ascii=False),
                 estimate["basis_digest"],
                 json.dumps(estimate["basis"], ensure_ascii=False),
                 json.dumps(estimate["result"], ensure_ascii=False),
                 str(estimate["measured_mm"]), str(estimate["estimated_mm"]),
                 str(estimate["total_mm"]), int(estimate["gap_days"]),
                 int(estimate["estimated_days"]), "active", "",
                 estimate["created_at"], estimate["created_at"]))
            for entry in entries:
                conn.execute(
                    """INSERT INTO supplement_entries
                         (estimate_id,obs_date,estimated_mm,neighbor_basis_json,status,
                          superseded_by_revision,superseded_at)
                       VALUES (?,?,?,?,?,NULL,NULL)""",
                    (estimate["estimate_id"], entry["date"], str(entry["estimated_mm"]),
                     json.dumps(entry["neighbor_basis"], ensure_ascii=False), "active"))
            for dep in dependencies:
                conn.execute(
                    """INSERT OR IGNORE INTO supplement_dependencies
                         (estimate_id,station_id,role) VALUES (?,?,?)""",
                    (estimate["estimate_id"], dep["station_id"], dep["role"]))
        return True

    def mark_supplement_stale(self, estimate_id: str, reason: str,
                              stamp: str) -> bool:
        with self.write_txn() as conn:
            cur = conn.execute(
                "UPDATE supplement_estimates SET status='stale', status_reason=?, "
                "updated_at=? WHERE estimate_id=? AND status='active'",
                (reason, stamp, estimate_id))
            return cur.rowcount > 0

    def mark_supplement_superseded(self, estimate_id: str, reason: str,
                                   stamp: str, *, obs_date: str | None = None,
                                   revision: int | None = None) -> bool:
        """估值被实测取代：整体退出当前口径，但快照与逐日原值一字不改。"""
        with self.write_txn() as conn:
            cur = conn.execute(
                "UPDATE supplement_estimates SET status='superseded', "
                "status_reason=?, updated_at=? WHERE estimate_id=? "
                "AND status IN ('active','stale')",
                (reason, stamp, estimate_id))
            if cur.rowcount > 0:
                conn.execute(
                    "UPDATE supplement_entries SET status='superseded', "
                    "superseded_at=COALESCE(superseded_at, ?) "
                    "WHERE estimate_id=? AND status='active'", (stamp, estimate_id))
                if obs_date is not None:
                    conn.execute(
                        "UPDATE supplement_entries SET superseded_by_revision=? "
                        "WHERE estimate_id=? AND obs_date=?", (revision, estimate_id,
                                                              obs_date))
            return cur.rowcount > 0

    def basis_is_current(self, basis: dict) -> bool:
        with self.connect() as conn:
            return _basis_matches(conn, basis)

    def record_fillin(self, *, idempotency_key: str, station_id: str, obs_date: str,
                      rainfall_mm: str, revision: int, stamp: str) -> bool:
        with self.write_txn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO supplement_fillins "
                "(idempotency_key,station_id,obs_date,rainfall_mm,revision,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (idempotency_key, station_id, obs_date, rainfall_mm, revision, stamp))
            return cur.rowcount > 0

    def get_fillin(self, idempotency_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM supplement_fillins WHERE idempotency_key=?",
                               (idempotency_key,)).fetchone()
        return dict(row) if row else None

    # ---------- 撤销作业 ----------
    def create_revocation(self, *, job_id: str, idempotency_key: str, reason: str,
                          estimate_ids: list[str], stamp: str) -> dict | None:
        """建作业与逐条意图；同一幂等键第二次调用返回 None（调用方读旧作业）。"""
        with self.write_txn() as conn:
            row = conn.execute(
                "SELECT job_id FROM supplement_revocations WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
            if row is not None:
                return None
            conn.execute(
                """INSERT INTO supplement_revocations
                     (job_id,idempotency_key,reason,status,total,processed,
                      created_at,updated_at)
                   VALUES (?,?,?,?,?,0,?,?)""",
                (job_id, idempotency_key, reason, "pending", len(estimate_ids),
                 stamp, stamp))
            for estimate_id in estimate_ids:
                conn.execute(
                    """INSERT OR IGNORE INTO supplement_revocation_items
                         (job_id,estimate_id,status,code,processed_at)
                       VALUES (?,?,'pending','',NULL)""", (job_id, estimate_id))
        return self.get_revocation(job_id)

    def get_revocation(self, job_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM supplement_revocations WHERE job_id=?",
                               (job_id,)).fetchone()
        return dict(row) if row else None

    def get_revocation_by_key(self, idempotency_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM supplement_revocations WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
        return dict(row) if row else None

    def revocation_items(self, job_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM supplement_revocation_items WHERE job_id=? "
            "ORDER BY estimate_id", (job_id,))

    def pending_revocations(self) -> list[dict]:
        return self._all(
            "SELECT * FROM supplement_revocations WHERE status='pending' "
            "ORDER BY created_at, job_id", ())

    def revoke_one(self, *, job_id: str, estimate_id: str, reason: str,
                   stamp: str) -> str:
        """单条撤销，单独一个事务：中断时已完成条目仍生效，pending 可续跑。

        快照（basis_json/result_json）与逐日估算值一律不动，只改状态。
        """
        with self.write_txn() as conn:
            row = conn.execute(
                "SELECT status FROM supplement_estimates WHERE estimate_id=?",
                (estimate_id,)).fetchone()
            if row is None:
                code = "E_ESTIMATE_NOT_FOUND"
            elif row["status"] == "revoked":
                code = "K_ALREADY_REVOKED"
            else:
                conn.execute(
                    "UPDATE supplement_estimates SET status='revoked', "
                    "status_reason=?, revoked_at=?, revoke_job_id=?, updated_at=? "
                    "WHERE estimate_id=?",
                    (reason, stamp, job_id, stamp, estimate_id))
                conn.execute(
                    "UPDATE supplement_entries SET status='superseded', "
                    "superseded_at=COALESCE(superseded_at, ?) "
                    "WHERE estimate_id=? AND status='active'",
                    (stamp, estimate_id))
                code = "K_REVOKED"
            item = conn.execute(
                "SELECT status FROM supplement_revocation_items "
                "WHERE job_id=? AND estimate_id=?", (job_id, estimate_id)).fetchone()
            if item is not None:
                conn.execute(
                    "UPDATE supplement_revocation_items SET status='done', code=?, "
                    "processed_at=? WHERE job_id=? AND estimate_id=?",
                    (code, stamp, job_id, estimate_id))
        return code

    def finish_revocation(self, job_id: str, stamp: str) -> dict | None:
        with self.write_txn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM supplement_revocation_items "
                "WHERE job_id=?", (job_id,)).fetchone()["n"]
            done = conn.execute(
                "SELECT COUNT(*) AS n FROM supplement_revocation_items "
                "WHERE job_id=? AND status='done'", (job_id,)).fetchone()["n"]
            status = "done" if done == total else "pending"
            conn.execute(
                "UPDATE supplement_revocations SET status=?, processed=?, updated_at=? "
                "WHERE job_id=?", (status, done, stamp, job_id))
        return self.get_revocation(job_id)


    def _all(self, sql: str, args: tuple) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

def _basis_matches(conn, basis: dict) -> bool:
    """依据快照 vs 当前实测台账。只在写事务或只读连接里调用。"""
    for part in basis.get("observations", []):
        station = part["station_id"]
        rows = conn.execute(
            "SELECT obs_date, rainfall_mm, revision FROM hydro_observations "
            "WHERE station_id=? AND kind='measured' AND obs_date>=? AND obs_date<=? "
            "ORDER BY obs_date",
            (station, basis["window"]["start"], basis["window"]["end"])).fetchall()
        current = [[r["obs_date"], r["rainfall_mm"], r["revision"]] for r in rows]
        expected = [[r["date"], r["rainfall_mm"], r["revision"]]
                    for r in part["rows"]]
        if current != expected:
            return False
    # 目标站实测只增不减：补测到达后旧估值必须退出当前口径
    target = basis["observations"][0]
    target_now = _observation_count(conn, target["station_id"], basis["window"])
    if target_now > len(target["rows"]):
        return False
    for item in basis.get("neighbors", []):
        row = conn.execute(
            "SELECT coefficient FROM hydro_neighbors WHERE station_id=? "
            "AND neighbor_id=?",
            (basis["target_station"], item["neighbor_id"])).fetchone()
        if row is None or row["coefficient"] != item["coefficient"]:
            return False
    return True


def _observation_count(conn, station: str, window: dict) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM hydro_observations WHERE station_id=? "
        "AND kind='measured' AND obs_date>=? AND obs_date<=?",
        (station, window["start"], window["end"])).fetchone()["n"]

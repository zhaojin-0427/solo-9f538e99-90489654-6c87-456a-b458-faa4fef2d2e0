"""应收账龄核心账本引擎。

数据模型（SQLite）：
  invoices            发票主数据（CSV 上传）
  receipts            收款流水
  allocations         核销流水（关联收款 + 发票）
  unallocations       撤销核销流水（引用原核销）
  reversals           银行冲正流水（引用原收款）
  invoice_daily_delta 发票"已核销额"按 UTC 日的增量汇总表（派生数据）

设计要点：
  * 所有流水按 (UTC 生效时刻, 唯一编号) 的全序入账。
  * 补登（迟到流水）入账时，把它与库中旧流水合并后按全序重放，
    逐笔校验：发票未核销额、收款可用额、原核销可撤额都不得为负；
    有违规则报首笔违规记录并整批拒收（事务回滚，原账不受影响）。
  * 账龄查询只读 invoice_daily_delta 汇总表，不重读逐笔流水。
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from datetime import date, datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id           TEXT PRIMARY KEY,
    invoice_date TEXT NOT NULL,              -- YYYY-MM-DD
    due_date     TEXT NOT NULL,              -- YYYY-MM-DD
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS receipts (
    id           TEXT PRIMARY KEY,
    effective_at TEXT NOT NULL,              -- UTC, 'YYYY-MM-DD HH:MM:SS.ffffff'
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS allocations (
    id           TEXT PRIMARY KEY,
    effective_at TEXT NOT NULL,
    receipt_id   TEXT NOT NULL REFERENCES receipts(id),
    invoice_id   TEXT NOT NULL REFERENCES invoices(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS unallocations (
    id            TEXT PRIMARY KEY,
    effective_at  TEXT NOT NULL,
    allocation_id TEXT NOT NULL REFERENCES allocations(id),
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS reversals (
    id           TEXT PRIMARY KEY,
    effective_at TEXT NOT NULL,
    receipt_id   TEXT NOT NULL REFERENCES receipts(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)
);

-- 派生汇总：某发票在某 UTC 日"已核销额"的净增量（核销 +，撤销核销 -）。
-- 账龄查询按 day <= 截止日 求和即可，无需重读逐笔流水。
CREATE TABLE IF NOT EXISTS invoice_daily_delta (
    invoice_id  TEXT NOT NULL REFERENCES invoices(id),
    day         TEXT NOT NULL,               -- YYYY-MM-DD (UTC)
    delta_cents INTEGER NOT NULL,
    PRIMARY KEY (invoice_id, day)
);
"""

TXN_TABLES = ("receipts", "allocations", "unallocations", "reversals")

TYPE_ALIASES = {
    "receipt": "receipt", "收款": "receipt",
    "allocation": "allocation", "核销": "allocation",
    "unallocation": "unallocation", "撤销核销": "unallocation",
    "reversal": "reversal", "冲正": "reversal",
}

TYPE_LABELS = {
    "receipt": "收款",
    "allocation": "核销",
    "unallocation": "撤销核销",
    "reversal": "银行冲正",
}

INVOICE_HEADERS = {
    "invoice_id": "invoice_id", "单号": "invoice_id",
    "invoice_date": "invoice_date", "开票日": "invoice_date",
    "due_date": "due_date", "到期日": "due_date",
    "amount_cents": "amount_cents", "金额": "amount_cents",
}

TXN_HEADERS = {
    "txn_id": "txn_id", "编号": "txn_id",
    "txn_type": "txn_type", "类型": "txn_type",
    "effective_at": "effective_at", "生效时刻": "effective_at",
    "amount_cents": "amount_cents", "金额": "amount_cents",
    "receipt_id": "receipt_id", "收款编号": "receipt_id",
    "invoice_id": "invoice_id", "发票单号": "invoice_id",
    "allocation_id": "allocation_id", "核销编号": "allocation_id",
}


class BatchError(Exception):
    """整批拒收。offender 为首笔违规/出错记录（dict，可空）。"""

    def __init__(self, message: str, offender: dict | None = None):
        super().__init__(message)
        self.message = message
        self.offender = offender


# ---------------------------------------------------------------- 基础工具

def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def parse_amount_cents(raw: str) -> int:
    """金额一律为正整数分。"""
    s = (raw or "").strip()
    if not s or not s.isascii() or not s.isdigit():
        raise ValueError(f"金额必须为正整数分，得到：{raw!r}")
    value = int(s)
    if value <= 0:
        raise ValueError(f"金额必须为正整数分，得到：{raw!r}")
    return value


def parse_effective_at(raw: str) -> str:
    """解析 UTC 生效时刻，归一化为 'YYYY-MM-DD HH:MM:SS.ffffff'（UTC）。

    字符串字典序即时间序，便于排序与按日切片。无时区的输入按 UTC 处理。
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("生效时刻不能为空")
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"生效时刻格式非法：{raw!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def parse_day(raw: str, field: str) -> str:
    try:
        return date.fromisoformat((raw or "").strip()).isoformat()
    except ValueError:
        raise ValueError(f"{field} 格式非法（应为 YYYY-MM-DD）：{raw!r}")


def _normalize_headers(fieldnames, mapping, required):
    headers = {}
    for name in fieldnames or []:
        key = mapping.get((name or "").strip())
        if key:
            headers[key] = name
    missing = [k for k in required if k not in headers]
    if missing:
        raise BatchError(f"CSV 缺少必需列：{', '.join(missing)}")
    return headers


# ---------------------------------------------------------------- 发票导入

def parse_invoices_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    headers = _normalize_headers(
        reader.fieldnames, INVOICE_HEADERS,
        ("invoice_id", "invoice_date", "due_date", "amount_cents"))
    rows = []
    for lineno, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        try:
            rows.append({
                "id": (row[headers["invoice_id"]] or "").strip(),
                "invoice_date": parse_day(row[headers["invoice_date"]], "开票日"),
                "due_date": parse_day(row[headers["due_date"]], "到期日"),
                "amount_cents": parse_amount_cents(row[headers["amount_cents"]]),
            })
        except ValueError as exc:
            raise BatchError(f"发票 CSV 第 {lineno} 行：{exc}")
        if not rows[-1]["id"]:
            raise BatchError(f"发票 CSV 第 {lineno} 行：单号不能为空")
    return rows


def import_invoices(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """导入发票主数据；单号重复（批内或库中）则整批拒绝。"""
    if not rows:
        raise BatchError("发票 CSV 没有有效数据行")
    seen = set()
    for r in rows:
        if r["id"] in seen:
            raise BatchError(f"发票单号在文件内重复：{r['id']}")
        seen.add(r["id"])
    dup = [r["id"] for r in rows
           if conn.execute("SELECT 1 FROM invoices WHERE id=?", (r["id"],)).fetchone()]
    if dup:
        raise BatchError(f"发票单号已存在：{', '.join(dup)}")
    try:
        conn.executemany(
            "INSERT INTO invoices (id, invoice_date, due_date, amount_cents)"
            " VALUES (:id, :invoice_date, :due_date, :amount_cents)", rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


# ---------------------------------------------------------------- 流水解析

def parse_transactions_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    headers = _normalize_headers(
        reader.fieldnames, TXN_HEADERS,
        ("txn_id", "txn_type", "effective_at", "amount_cents"))

    def cell(row, key):
        name = headers.get(key)
        return (row.get(name) or "").strip() if name else ""

    txns = []
    for lineno, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        where = f"流水 CSV 第 {lineno} 行"
        ttype = TYPE_ALIASES.get(cell(row, "txn_type")) or \
            TYPE_ALIASES.get(cell(row, "txn_type").lower())
        if not ttype:
            raise BatchError(f"{where}：类型非法（应为 收款/核销/撤销核销/冲正 "
                             f"或 receipt/allocation/unallocation/reversal）："
                             f"{cell(row, 'txn_type')!r}")
        try:
            txn = {
                "id": cell(row, "txn_id"),
                "type": ttype,
                "effective_at": parse_effective_at(cell(row, "effective_at")),
                "amount_cents": parse_amount_cents(cell(row, "amount_cents")),
                "receipt_id": cell(row, "receipt_id"),
                "invoice_id": cell(row, "invoice_id"),
                "allocation_id": cell(row, "allocation_id"),
            }
        except ValueError as exc:
            raise BatchError(f"{where}：{exc}")
        if not txn["id"]:
            raise BatchError(f"{where}：编号不能为空")
        if ttype == "allocation" and (not txn["receipt_id"] or not txn["invoice_id"]):
            raise BatchError(f"{where}：核销必须填收款编号和发票单号")
        if ttype == "unallocation" and not txn["allocation_id"]:
            raise BatchError(f"{where}：撤销核销必须填核销编号")
        if ttype == "reversal" and not txn["receipt_id"]:
            raise BatchError(f"{where}：冲正必须填收款编号")
        txns.append(txn)
    return txns


# ---------------------------------------------------------------- 入账引擎

def post_batch(conn: sqlite3.Connection, txns: list[dict]) -> int:
    """整批入账。任一校验失败则回滚，原账不受影响。返回入账笔数。"""
    try:
        _post_batch_inner(conn, txns)
        conn.commit()
        return len(txns)
    except Exception:
        conn.rollback()
        raise


def _post_batch_inner(conn: sqlite3.Connection, txns: list[dict]) -> None:
    if not txns:
        raise BatchError("批次为空")

    # 1) 编号全局唯一（批内 + 库中四类流水）
    seen = set()
    for t in txns:
        if t["id"] in seen:
            raise BatchError(f"批次内编号重复：{t['id']}",
                             offender=_offender(t))
        seen.add(t["id"])
    for t in txns:
        for table in TXN_TABLES:
            if conn.execute(f"SELECT 1 FROM {table} WHERE id=?",
                            (t["id"],)).fetchone():
                raise BatchError(f"编号 {t['id']} 已入账过，不能重复",
                                 offender=_offender(t))

    # 2) 引用与余额合并为一次全序重放：本批新流水与库中旧流水合并，
    #    按 (UTC 生效时刻, 编号) 排序，依次判定。原单缺失、原单未生效、
    #    三类余额为负都在这个顺序里取“首笔”，而不是分阶段各自取首笔。
    #    只加载受本批影响的收款、发票与核销，不扫描无关历史流水。
    first = _replay_affected(conn, txns)
    if first is not None:
        raise BatchError(
            f"整批拒收：首笔违规记录 {first['id']}"
            f"（{TYPE_LABELS[first['type']]}，生效于 {first['effective_at']} UTC）"
            f"——{first['reason']}",
            offender=first)

    # 3) 全部通过后才落库。按外键依赖顺序插入（收款→核销→撤销/冲正），
    #    这样 CSV 中引用行排在被引用行之后的乱序合法批次也能正常入账。
    for t in txns:
        if t["type"] == "receipt":
            conn.execute(
                "INSERT INTO receipts (id, effective_at, amount_cents)"
                " VALUES (?,?,?)",
                (t["id"], t["effective_at"], t["amount_cents"]))
    for t in txns:
        if t["type"] == "allocation":
            conn.execute(
                "INSERT INTO allocations (id, effective_at, receipt_id,"
                " invoice_id, amount_cents) VALUES (?,?,?,?,?)",
                (t["id"], t["effective_at"], t["receipt_id"],
                 t["invoice_id"], t["amount_cents"]))
    for t in txns:
        if t["type"] == "unallocation":
            conn.execute(
                "INSERT INTO unallocations (id, effective_at, allocation_id,"
                " amount_cents) VALUES (?,?,?,?)",
                (t["id"], t["effective_at"], t["allocation_id"],
                 t["amount_cents"]))
        elif t["type"] == "reversal":
            conn.execute(
                "INSERT INTO reversals (id, effective_at, receipt_id,"
                " amount_cents) VALUES (?,?,?,?)",
                (t["id"], t["effective_at"], t["receipt_id"],
                 t["amount_cents"]))

    # 4) 与原始流水同一事务维护账龄用日增量；整批拒收时两者都不生效
    _apply_daily_deltas(conn, txns)


def _offender(t: dict) -> dict:
    return {"id": t["id"], "type": t["type"],
            "effective_at": t["effective_at"], "reason": ""}


def _violation(tid, ttype, eff, reason):
    return {"id": tid, "type": ttype, "effective_at": eff, "reason": reason}


def _affected_entities(txns: list[dict]) -> tuple[set, set, set]:
    """收集受本批影响的收款、发票、核销编号（不访问数据库）。"""
    receipts, invoices, allocations = set(), set(), set()
    for t in txns:
        if t["type"] == "receipt":
            receipts.add(t["id"])
        elif t["type"] == "allocation":
            receipts.add(t["receipt_id"])
            invoices.add(t["invoice_id"])
        elif t["type"] == "unallocation":
            allocations.add(t["allocation_id"])
        elif t["type"] == "reversal":
            receipts.add(t["receipt_id"])
    return receipts, allocations, invoices


def _replay_affected(conn: sqlite3.Connection,
                     txns: list[dict]) -> dict | None:
    """合并本批与库中受影响实体的全部流水，按 (生效时刻, 编号) 全序重放。

    返回首笔违规记录（含原因）；全部合法则返回 None。校验只读内存合并集，
    不向任何流水表写入，因此拒收时调用方回滚即可，原账不受影响。
    """
    receipts, allocations, invoices = _affected_entities(txns)
    batch_by_id = {t["id"]: t for t in txns}

    # 撤销/冲正的原单若在本批内，其关联实体也进入受影响集合
    for t in txns:
        if t["type"] == "unallocation" and t["allocation_id"] in batch_by_id:
            orig = batch_by_id[t["allocation_id"]]
            receipts.add(orig["receipt_id"])
            invoices.add(orig["invoice_id"])

    # 本批核销的原单信息（库中或批内），供撤销事件定位收款/发票
    alloc_meta = {}
    for aid in allocations:
        if aid in batch_by_id:
            a = batch_by_id[aid]
            alloc_meta[aid] = (a["receipt_id"], a["invoice_id"])
        else:
            row = conn.execute(
                "SELECT receipt_id, invoice_id FROM allocations WHERE id=?",
                (aid,)).fetchone()
            if row is not None:
                alloc_meta[aid] = (row["receipt_id"], row["invoice_id"])

    # 受影响收款 / 发票的库中初始余额；本批新增收款初始额取批内金额
    receipt_amount = {}
    for rid in receipts:
        if rid in batch_by_id:
            receipt_amount[rid] = batch_by_id[rid]["amount_cents"]
        else:
            row = conn.execute(
                "SELECT amount_cents FROM receipts WHERE id=?",
                (rid,)).fetchone()
            if row is not None:
                receipt_amount[rid] = row["amount_cents"]

    invoice_amount = {}
    for iid in invoices:
        row = conn.execute(
            "SELECT amount_cents FROM invoices WHERE id=?",
            (iid,)).fetchone()
        if row is not None:
            invoice_amount[iid] = row["amount_cents"]

    # 受影响核销的初始可撤额（库中核销额；本批核销取批内金额）
    allocation_amount = {}
    for aid in allocations:
        if aid in batch_by_id:
            allocation_amount[aid] = batch_by_id[aid]["amount_cents"]
        else:
            row = conn.execute(
                "SELECT amount_cents FROM allocations WHERE id=?",
                (aid,)).fetchone()
            if row is not None:
                allocation_amount[aid] = row["amount_cents"]

    # 加载受影响实体在库中的历史事件
    receipt_events = {rid: [] for rid in receipts}
    invoice_events = {iid: [] for iid in invoices}
    allocation_events = {aid: [] for aid in allocations}

    def add_receipt_event(rid, eff, tid, ttype, delta):
        if rid in receipt_events:
            receipt_events[rid].append((eff, tid, ttype, delta))

    def add_invoice_event(iid, eff, tid, ttype, delta):
        if iid in invoice_events:
            invoice_events[iid].append((eff, tid, ttype, delta))

    def add_allocation_event(aid, eff, tid, amount):
        if aid in allocation_events:
            allocation_events[aid].append((eff, tid, amount))

    if receipts:
        marks = ",".join("?" for _ in receipts)
        for a in conn.execute(
                f"SELECT id, effective_at, amount_cents, receipt_id, invoice_id"
                f" FROM allocations WHERE receipt_id IN ({marks})",
                tuple(receipts)):
            add_receipt_event(a["receipt_id"], a["effective_at"], a["id"],
                              "allocation", -a["amount_cents"])
        for u in conn.execute(
                f"SELECT u.id, u.effective_at, u.amount_cents, a.receipt_id,"
                f" a.invoice_id FROM unallocations u JOIN allocations a"
                f" ON a.id = u.allocation_id"
                f" WHERE a.receipt_id IN ({marks})", tuple(receipts)):
            add_receipt_event(u["receipt_id"], u["effective_at"], u["id"],
                              "unallocation", u["amount_cents"])
            add_invoice_event(u["invoice_id"], u["effective_at"], u["id"],
                              "unallocation", u["amount_cents"])
        for v in conn.execute(
                f"SELECT id, effective_at, amount_cents, receipt_id"
                f" FROM reversals WHERE receipt_id IN ({marks})",
                tuple(receipts)):
            add_receipt_event(v["receipt_id"], v["effective_at"], v["id"],
                              "reversal", -v["amount_cents"])
    if invoices:
        marks = ",".join("?" for _ in invoices)
        for a in conn.execute(
                f"SELECT id, effective_at, amount_cents, receipt_id, invoice_id"
                f" FROM allocations WHERE invoice_id IN ({marks})",
                tuple(invoices)):
            add_invoice_event(a["invoice_id"], a["effective_at"], a["id"],
                              "allocation", -a["amount_cents"])
    if allocations:
        marks = ",".join("?" for _ in allocations)
        for u in conn.execute(
                f"SELECT id, effective_at, amount_cents, allocation_id"
                f" FROM unallocations WHERE allocation_id IN ({marks})",
                tuple(allocations)):
            add_allocation_event(u["allocation_id"], u["effective_at"],
                                 u["id"], u["amount_cents"])

    # 合并本批新事件
    for t in txns:
        eff, tid, ttype = t["effective_at"], t["id"], t["type"]
        if ttype == "allocation":
            add_receipt_event(t["receipt_id"], eff, tid, ttype,
                              -t["amount_cents"])
            add_invoice_event(t["invoice_id"], eff, tid, ttype,
                              -t["amount_cents"])
        elif ttype == "unallocation":
            meta = alloc_meta.get(t["allocation_id"])
            if meta is not None:
                rid, iid = meta
                add_receipt_event(rid, eff, tid, ttype, t["amount_cents"])
                add_invoice_event(iid, eff, tid, ttype, t["amount_cents"])
            add_allocation_event(t["allocation_id"], eff, tid,
                                 t["amount_cents"])
        elif ttype == "reversal":
            add_receipt_event(t["receipt_id"], eff, tid, ttype,
                              -t["amount_cents"])

    first = None

    def note(violation):
        # 全序首笔：先按 (生效时刻, 编号)，同一事件上的多个原因保留先出现者
        nonlocal first
        key = (violation["effective_at"], violation["id"])
        if first is None or key < (first["effective_at"], first["id"]):
            first = violation

    def check_reference(t):
        """原单缺失 / 原单未生效；错误位置记在引用方自己的序位上。"""
        key = (t["effective_at"], t["id"])

        def original_state(table, oid):
            if oid in batch_by_id:
                orig_eff = batch_by_id[oid]["effective_at"]
            else:
                row = conn.execute(
                    f"SELECT effective_at FROM {table} WHERE id=?",
                    (oid,)).fetchone()
                if row is None:
                    note(_violation(
                        t["id"], t["type"], t["effective_at"],
                        f"引用的原单 {oid} 不存在"))
                    return
                orig_eff = row["effective_at"]
            if not ((orig_eff, oid) < key):
                note(_violation(
                    t["id"], t["type"], t["effective_at"],
                    f"入账时原单 {oid} 尚未生效"
                    f"（原单生效于 {orig_eff} UTC）"))

        if t["type"] == "allocation":
            original_state("receipts", t["receipt_id"])
            if t["invoice_id"] not in invoice_amount:
                note(_violation(
                    t["id"], t["type"], t["effective_at"],
                    f"引用的发票 {t['invoice_id']} 不存在"))
        elif t["type"] == "unallocation":
            original_state("allocations", t["allocation_id"])
        elif t["type"] == "reversal":
            original_state("receipts", t["receipt_id"])

    # 引用判定同样按全序进行，保证与余额违规在同一顺序里竞争“首笔”
    for t in sorted(txns, key=lambda x: (x["effective_at"], x["id"])):
        check_reference(t)

    # 收款可用额重放：收款额 + 撤销核销 - 核销 - 冲正，逐笔不得为负
    for rid, events in receipt_events.items():
        available = receipt_amount.get(rid)
        if available is None:
            # 收款本身缺失：引用它的新流水已在引用检查中报“原单不存在”，
            # 库中流水不会引用不存在的收款，这里无需再报
            continue
        for eff, tid, ttype, delta in sorted(events, key=lambda e: (e[0], e[1])):
            available += delta
            if available < 0:
                note(_violation(
                    tid, ttype, eff,
                    f"入账后收款 {rid} 可用额为 {available} 分（为负）"))
                break

    # 发票未核销额重放：发票额 - 核销 + 撤销核销，逐笔不得为负
    for iid, events in invoice_events.items():
        outstanding = invoice_amount.get(iid)
        if outstanding is None:
            continue
        for eff, tid, ttype, delta in sorted(events, key=lambda e: (e[0], e[1])):
            outstanding += delta
            if outstanding < 0:
                note(_violation(
                    tid, ttype, eff,
                    f"入账后发票 {iid} 未核销额为 {outstanding} 分（为负）"))
                break

    # 原核销可撤额重放：核销额 - 累计撤销，逐笔不得为负
    for aid, events in allocation_events.items():
        revocable = allocation_amount.get(aid)
        if revocable is None:
            continue
        for eff, tid, amount in sorted(events, key=lambda e: (e[0], e[1])):
            revocable -= amount
            if revocable < 0:
                note(_violation(
                    tid, "unallocation", eff,
                    f"入账后核销 {aid} 可撤额为 {revocable} 分（为负）"))
                break

    return first


def _apply_daily_deltas(conn, txns) -> None:
    upsert = (
        "INSERT INTO invoice_daily_delta (invoice_id, day, delta_cents)"
        " VALUES (?,?,?)"
        " ON CONFLICT(invoice_id, day) DO UPDATE"
        " SET delta_cents = delta_cents + excluded.delta_cents")
    for t in txns:
        if t["type"] == "allocation":
            conn.execute(upsert, (t["invoice_id"], t["effective_at"][:10],
                                  t["amount_cents"]))
        elif t["type"] == "unallocation":
            inv = conn.execute(
                "SELECT invoice_id FROM allocations WHERE id=?",
                (t["allocation_id"],)).fetchone()
            conn.execute(upsert, (inv["invoice_id"], t["effective_at"][:10],
                                  -t["amount_cents"]))


# ---------------------------------------------------------------- 账龄查询

BUCKETS = ("未逾期", "1-30天", "31-60天", "61-90天", "90天以上")


def bucket_of(overdue_days: int) -> str:
    if overdue_days <= 0:
        return BUCKETS[0]
    if overdue_days <= 30:
        return BUCKETS[1]
    if overdue_days <= 60:
        return BUCKETS[2]
    if overdue_days <= 90:
        return BUCKETS[3]
    return BUCKETS[4]


def aging_as_of(conn: sqlite3.Connection, cutoff: date) -> dict:
    """截止日（含）当天结束时的未结发票账龄。

    只读 invoices 与 invoice_daily_delta 汇总表，不重读逐笔流水。
    """
    day = cutoff.isoformat()
    rows = conn.execute(
        "SELECT i.id, i.invoice_date, i.due_date, i.amount_cents,"
        " COALESCE((SELECT SUM(d.delta_cents) FROM invoice_daily_delta d"
        "           WHERE d.invoice_id = i.id AND d.day <= ?), 0) AS allocated"
        " FROM invoices i WHERE i.invoice_date <= ?"
        " ORDER BY i.due_date, i.id", (day, day)).fetchall()

    items = []
    bucket_totals = {b: 0 for b in BUCKETS}
    total = 0
    for r in rows:
        outstanding = r["amount_cents"] - r["allocated"]
        if outstanding <= 0:
            continue
        overdue = max(0, (cutoff - date.fromisoformat(r["due_date"])).days)
        bucket = bucket_of(overdue)
        items.append({
            "invoice_id": r["id"],
            "invoice_date": r["invoice_date"],
            "due_date": r["due_date"],
            "amount_cents": r["amount_cents"],
            "outstanding_cents": outstanding,
            "overdue_days": overdue,
            "bucket": bucket,
        })
        bucket_totals[bucket] += outstanding
        total += outstanding
    return {"cutoff": day, "rows": items,
            "bucket_totals": bucket_totals, "total_cents": total}


# ---------------------------------------------------------------- 查询展示

def list_invoices(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT i.id, i.invoice_date, i.due_date, i.amount_cents,"
        " COALESCE((SELECT SUM(d.delta_cents) FROM invoice_daily_delta d"
        "           WHERE d.invoice_id = i.id), 0) AS allocated"
        " FROM invoices i ORDER BY i.id").fetchall()
    return [dict(r, outstanding_cents=r["amount_cents"] - r["allocated"])
            for r in rows]


def list_receipts(conn) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT id, effective_at, amount_cents FROM receipts"
            " ORDER BY effective_at, id"):
        allocated = conn.execute(
            "SELECT COALESCE(SUM(a.amount_cents),0) -"
            " COALESCE((SELECT SUM(u.amount_cents) FROM unallocations u"
            "           JOIN allocations a2 ON a2.id = u.allocation_id"
            "           WHERE a2.receipt_id = ?), 0)"
            " FROM allocations a WHERE a.receipt_id = ?",
            (r["id"], r["id"])).fetchone()[0]
        reversed_ = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM reversals"
            " WHERE receipt_id=?", (r["id"],)).fetchone()[0]
        out.append({
            "id": r["id"], "effective_at": r["effective_at"],
            "amount_cents": r["amount_cents"],
            "allocated_cents": allocated, "reversed_cents": reversed_,
            "available_cents": r["amount_cents"] - allocated - reversed_,
        })
    return out


def list_transactions(conn) -> list[dict]:
    txns = []
    for r in conn.execute("SELECT * FROM receipts"):
        txns.append({"id": r["id"], "type": "receipt",
                     "effective_at": r["effective_at"],
                     "amount_cents": r["amount_cents"], "detail": ""})
    for a in conn.execute("SELECT * FROM allocations"):
        txns.append({"id": a["id"], "type": "allocation",
                     "effective_at": a["effective_at"],
                     "amount_cents": a["amount_cents"],
                     "detail": f"收款 {a['receipt_id']} → 发票 {a['invoice_id']}"})
    for u in conn.execute("SELECT * FROM unallocations"):
        txns.append({"id": u["id"], "type": "unallocation",
                     "effective_at": u["effective_at"],
                     "amount_cents": u["amount_cents"],
                     "detail": f"撤销核销 {u['allocation_id']}"})
    for v in conn.execute("SELECT * FROM reversals"):
        txns.append({"id": v["id"], "type": "reversal",
                     "effective_at": v["effective_at"],
                     "amount_cents": v["amount_cents"],
                     "detail": f"冲正收款 {v['receipt_id']}"})
    txns.sort(key=lambda t: (t["effective_at"], t["id"]))
    for t in txns:
        t["type_label"] = TYPE_LABELS[t["type"]]
    return txns

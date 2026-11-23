from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3

from app.db import get_db
from app.services import audit as audit_svc
from app.services import settings as settings_svc
from app.services.errors import ServiceError

VISIT_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _fingerprint(
    batch_id: int,
    subject_key: str,
    visit_code: str,
    payload: str,
    quality: str,
) -> str:
    """对访视行清洗后的键锁内容算 SHA-256 指纹。"""
    material = json.dumps(
        {
            "batch_id": int(batch_id),
            "subject_key": subject_key,
            "visit_code": visit_code,
            "payload": payload,
            "quality": quality,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _refresh_batch_counts(conn, batch_id: int) -> None:
    item_count = conn.execute(
        "SELECT COUNT(*) AS c FROM visit_rows WHERE batch_id=?", (batch_id,)
    ).fetchone()["c"]
    gap_count = conn.execute(
        "SELECT COUNT(*) AS c FROM visit_rows WHERE batch_id=? AND quality!='合格'",
        (batch_id,),
    ).fetchone()["c"]
    conn.execute(
        "UPDATE visit_batches SET item_count=?, gap_count=? WHERE id=?",
        (item_count, gap_count, batch_id),
    )


def _validate_cleanse_input(visit_code: str, payload: str) -> tuple[str, str]:
    visit_code = (visit_code or "").strip().upper()
    if not visit_code:
        raise ServiceError("清洗后的访视编码不能空")
    if not VISIT_CODE_RE.match(visit_code):
        raise ServiceError("清洗后的访视编码格式无效（1-40 位字母数字/_-）")
    payload = payload if payload is not None else ""
    if not str(payload).strip():
        raise ServiceError("清洗后的载荷不能空")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ServiceError(f"清洗载荷必须是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ServiceError("清洗载荷必须是 JSON 对象")
    return visit_code, payload


def _sealed_voucher(conn, row_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM code_cleanse_vouchers WHERE row_id=? AND status='封档'",
        (row_id,),
    ).fetchone()


def assert_row_can_enter_warehouse(conn, row: sqlite3.Row) -> None:
    """入库闸门：编码空行必须有封档凭证；持凭证行（含已转合格）必须指纹核验通过。"""
    voucher = _sealed_voucher(conn, row["id"])
    if voucher is not None:
        current_fp = _fingerprint(
            row["batch_id"],
            row["subject_key"],
            row["visit_code"],
            row["payload"],
            row["quality"],
        )
        if current_fp != voucher["fingerprint"]:
            raise ServiceError(
                f"访视行 #{row['id']} 清洗凭证指纹核验不符，凭证已封档拒绝回刷，禁止入库"
            )
        return
    if row["quality"] == "编码空":
        raise ServiceError(
            f"访视行 #{row['id']} 为编码空且无封档清洗凭证，禁止入库；"
            "须先在字段质控页完成编码清洗"
        )


def verify_row_voucher(conn, row_id: int) -> dict:
    """重算当前行业务库指纹，与封档凭证比对。"""
    row = conn.execute("SELECT * FROM visit_rows WHERE id=?", (row_id,)).fetchone()
    voucher = _sealed_voucher(conn, row_id)
    if row is None or voucher is None:
        return {"row_id": row_id, "verified": False, "reason": "无封档凭证"}
    current_fp = _fingerprint(
        row["batch_id"],
        row["subject_key"],
        row["visit_code"],
        row["payload"],
        row["quality"],
    )
    return {
        "row_id": row_id,
        "verified": current_fp == voucher["fingerprint"],
        "voucher_code": voucher["voucher_code"],
        "stored_fingerprint": voucher["fingerprint"],
        "current_fingerprint": current_fp,
        "reason": "指纹一致" if current_fp == voucher["fingerprint"] else "指纹不符，行内容被改动",
    }


def submit_cleanse(
    row_id: int,
    visit_code: str,
    payload: str,
    db_path: str | None = None,
) -> dict:
    """整笔编码清洗：凭证落表 + 行回写 + 发现关闭，同事务成败一体。"""
    row_id = int(row_id or 0)
    if row_id <= 0:
        raise ServiceError("必须指定要清洗的访视行")
    visit_code, payload = _validate_cleanse_input(visit_code, payload)
    try:
        with get_db(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM visit_rows WHERE id=?", (row_id,)
            ).fetchone()
            if row is None:
                raise ServiceError("访视行不存在", 404)
            if row["quality"] != "编码空":
                raise ServiceError("只有编码空行可以提交编码清洗")
            batch = conn.execute(
                "SELECT * FROM visit_batches WHERE id=?", (row["batch_id"],)
            ).fetchone()
            if batch is None:
                raise ServiceError("访视批次不存在", 404)
            if batch["status"] == "已关闭":
                raise ServiceError("已关闭批次不能再做编码清洗")
            if _sealed_voucher(conn, row_id) is not None:
                raise ServiceError(
                    "该行已存在封档清洗凭证，凭证只读、拒绝回刷"
                )
            old_fingerprint = _fingerprint(
                row["batch_id"],
                row["subject_key"],
                row["visit_code"],
                row["payload"],
                row["quality"],
            )
            new_fingerprint = _fingerprint(
                row["batch_id"],
                row["subject_key"],
                visit_code,
                payload,
                "合格",
            )
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM code_cleanse_vouchers"
            ).fetchone()["c"] + 1
            voucher_code = f"CL-{n:04d}"
            token = secrets.token_hex(16)
            try:
                cur = conn.execute(
                    """
                    INSERT INTO code_cleanse_vouchers(
                      voucher_code, row_id, batch_id, subject_key, visit_code,
                      old_payload, new_payload, old_fingerprint, fingerprint,
                      token, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '封档')
                    """,
                    (
                        voucher_code,
                        row_id,
                        row["batch_id"],
                        row["subject_key"],
                        visit_code,
                        row["payload"],
                        payload,
                        old_fingerprint,
                        new_fingerprint,
                        token,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # 并发争用：UNIQUE(row_id) 钉点挡住第二笔，钉点不被冲掉
                raise ServiceError(
                    "编码清洗争用：该行已被另一笔凭证钉死，本笔拒绝写入"
                ) from exc
            voucher_id = cur.lastrowid
            cur = conn.execute(
                """
                UPDATE visit_rows
                SET visit_code=?, payload=?, quality='合格', status='已入库'
                WHERE id=? AND quality='编码空'
                """,
                (visit_code, payload, row_id),
            )
            if cur.rowcount != 1:
                raise ServiceError(
                    "编码清洗整笔落库失败：访视行回写未命中，事务回滚"
                )
            conn.execute(
                "UPDATE qc_findings SET status='已关闭' WHERE row_id=? AND status='开放'",
                (row_id,),
            )
            _refresh_batch_counts(conn, row["batch_id"])
            voucher = conn.execute(
                "SELECT * FROM code_cleanse_vouchers WHERE id=?", (voucher_id,)
            ).fetchone()
            verify = verify_row_voucher(conn, row_id)
            if not verify["verified"]:
                raise ServiceError("编码清洗整笔落库失败：指纹自检不符，事务回滚")
    except ServiceError:
        raise
    audit_svc.write_event(
        "编码清洗封档",
        "code_cleanse_voucher",
        voucher_id,
        f"{voucher_code} row={row_id} {row['subject_key']}/{visit_code}",
        db_path=db_path,
    )
    data = dict(voucher)
    data["verify"] = verify
    return data


def list_vouchers(
    db_path: str | None = None,
    batch_id: int | None = None,
) -> list[dict]:
    clauses = []
    params: list = []
    if batch_id:
        clauses.append("v.batch_id=?")
        params.append(int(batch_id))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT v.*, b.batch_code, s.name AS site_name,
                   r.status AS row_status, r.quality AS row_quality
            FROM code_cleanse_vouchers v
            JOIN visit_batches b ON b.id=v.batch_id
            JOIN trial_sites s ON s.id=b.site_id
            JOIN visit_rows r ON r.id=v.row_id
            {where}
            ORDER BY v.id DESC
            LIMIT 500
            """,
            params,
        ).fetchall()
        items = [dict(r) for r in rows]
        for item in items:
            item["verify"] = verify_row_voucher(conn, item["row_id"])
        return items


def get_voucher(voucher_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT v.*, b.batch_code, s.name AS site_name,
                   r.status AS row_status, r.quality AS row_quality
            FROM code_cleanse_vouchers v
            JOIN visit_batches b ON b.id=v.batch_id
            JOIN trial_sites s ON s.id=b.site_id
            JOIN visit_rows r ON r.id=v.row_id
            WHERE v.id=?
            """,
            (voucher_id,),
        ).fetchone()
        if row is None:
            raise ServiceError("编码清洗凭证不存在", 404)
        data = dict(row)
        data["verify"] = verify_row_voucher(conn, row["row_id"])
        return data


def verify_voucher(voucher_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT id, row_id FROM code_cleanse_vouchers WHERE id=?",
            (voucher_id,),
        ).fetchone()
        if row is None:
            raise ServiceError("编码清洗凭证不存在", 404)
        result = verify_row_voucher(conn, row["row_id"])
        result["voucher_id"] = voucher_id
        return result


def pending_empty_code_rows(db_path: str | None = None) -> list[dict]:
    """编码清洗队列：业务库里仍为编码空、尚未取得封档凭证的行。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.*, b.batch_code, b.status AS batch_status, s.name AS site_name
            FROM visit_rows r
            JOIN visit_batches b ON b.id=r.batch_id
            JOIN trial_sites s ON s.id=b.site_id
            WHERE r.quality='编码空'
              AND NOT EXISTS (
                SELECT 1 FROM code_cleanse_vouchers v
                WHERE v.row_id=r.id AND v.status='封档'
              )
            ORDER BY r.id
            LIMIT 500
            """
        ).fetchall()
        return [dict(r) for r in rows]


def badge_summary(db_path: str | None = None) -> dict:
    """字段质控页清洗徽标对照空码条数：两数必须能对账。"""
    with get_db(db_path) as conn:
        empty_total = conn.execute(
            "SELECT COUNT(*) AS c FROM visit_rows WHERE quality='编码空'"
        ).fetchone()["c"]
        pending = conn.execute(
            """
            SELECT COUNT(*) AS c FROM visit_rows r
            WHERE r.quality='编码空'
              AND NOT EXISTS (
                SELECT 1 FROM code_cleanse_vouchers v
                WHERE v.row_id=r.id AND v.status='封档'
              )
            """
        ).fetchone()["c"]
        sealed = conn.execute(
            "SELECT COUNT(*) AS c FROM code_cleanse_vouchers WHERE status='封档'"
        ).fetchone()["c"]
        invalid = 0
        for row in conn.execute(
            "SELECT row_id FROM code_cleanse_vouchers WHERE status='封档'"
        ).fetchall():
            if not verify_row_voucher(conn, row["row_id"])["verified"]:
                invalid += 1
    rule = ""
    try:
        rule = settings_svc.get_setting("code_cleanse_rule", db_path)["value"]
    except ServiceError:
        rule = ""
    return {
        "empty_code_total": empty_total,
        "pending_empty_code": pending,
        "sealed_vouchers": sealed,
        "invalid_vouchers": invalid,
        # 对账恒等式：仍为空码的行必然没有封档凭证（封档即转合格），
        # 且所有封档凭证指纹有效；拧花即不一致，入库门同时拦截空码。
        "badge_consistent": empty_total == pending and invalid == 0,
        "rule": rule,
    }

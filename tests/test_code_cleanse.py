"""编码清洗（甲侧封档凭证）专项测试，覆盖 docs/code-cleanse-plan.md 败例清单。"""
import json
import sqlite3
import threading

import pytest

from app.services import backfill, cleanse, qc, reconcile, visits
from app.services.errors import ServiceError

PAYLOAD = json.dumps(
    {"visit_date": "2026-11-23", "investigator": "清洗医生", "weight_kg": 60},
    ensure_ascii=False,
)


def _an_empty_row(db_path):
    rows = visits.list_rows(db_path, quality="编码空")
    assert rows, "种子库应至少有一行编码空"
    return rows[0]


def test_cleanse_lands_sealed_voucher_and_row_is_ingested(db_path):
    """败例1/2：清洗必须有真实落点（凭证表行），且行转合格入库。"""
    row = _an_empty_row(db_path)
    voucher = cleanse.submit_cleanse(row["id"], "V2", PAYLOAD, db_path=db_path)
    assert voucher["status"] == "封档"
    assert voucher["row_id"] == row["id"]
    assert len(voucher["fingerprint"]) == 64
    assert voucher["verify"]["verified"] is True

    # 落点确实入表，且一行只有一张凭证
    vouchers = [v for v in cleanse.list_vouchers(db_path) if v["row_id"] == row["id"]]
    assert len(vouchers) == 1

    fixed = next(r for r in visits.list_rows(db_path, batch_id=row["batch_id"])
                 if r["id"] == row["id"])
    assert fixed["quality"] == "合格"
    assert fixed["status"] == "已入库"
    assert fixed["payload"] == PAYLOAD


def test_empty_code_blocked_on_all_three_ingress_paths(db_path):
    """败例6：无凭证空码行，手改/批量/补采三条入库路径全拦。"""
    row = _an_empty_row(db_path)

    with pytest.raises(ServiceError) as exc:
        visits.set_row_status(row["id"], "已入库", db_path=db_path)
    assert "封档清洗凭证" in exc.value.message

    result = visits.ingest_ok_rows(row["batch_id"], db_path=db_path)
    still = next(r for r in visits.list_rows(db_path, batch_id=row["batch_id"])
                 if r["id"] == row["id"])
    assert still["status"] != "已入库"
    assert result["blocked_empty_code"] >= 1
    assert row["id"] in result["blocked_row_ids"]

    backfill.enqueue_from_row(row["id"], db_path=db_path)
    job = backfill.list_jobs(db_path, row_id=row["id"])[0]
    blocked = backfill.run_job(job["id"], succeed=True, db_path=db_path)
    failed = backfill.get_job(job["id"], db_path=db_path)
    assert blocked["status"] == "失败"
    assert failed["status"] == "失败"
    assert "清洗凭证" in failed["message"]
    still2 = next(r for r in visits.list_rows(db_path, batch_id=row["batch_id"])
                  if r["id"] == row["id"])
    assert still2["quality"] == "编码空"


def test_fingerprint_mismatch_rejects_warehouse(db_path):
    """败例3：封档后业务库行被偷改，指纹对不上必须拒绝入库。"""
    row = _an_empty_row(db_path)
    voucher = cleanse.submit_cleanse(row["id"], "V2", PAYLOAD, db_path=db_path)

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE visit_rows SET payload=? WHERE id=?",
        (json.dumps({"tampered": True}), row["id"]),
    )
    conn.commit()
    conn.close()

    verify = cleanse.verify_voucher(voucher["id"], db_path=db_path)
    assert verify["verified"] is False

    # 行当前质量已合格，但持凭证行仍须过指纹闸门
    with pytest.raises(ServiceError) as exc:
        visits.set_row_status(row["id"], "已入库", db_path=db_path)
    assert "指纹核验不符" in exc.value.message

    summary = cleanse.badge_summary(db_path)
    assert summary["invalid_vouchers"] == 1
    assert summary["badge_consistent"] is False


def test_sealed_voucher_is_read_only_no_rebrush(db_path):
    """凭证封档后拒绝二次清洗（回刷）。"""
    row = _an_empty_row(db_path)
    cleanse.submit_cleanse(row["id"], "V2", PAYLOAD, db_path=db_path)
    with pytest.raises(ServiceError):
        cleanse.submit_cleanse(row["id"], "V3", PAYLOAD, db_path=db_path)
    vouchers = [v for v in cleanse.list_vouchers(db_path) if v["row_id"] == row["id"]]
    assert len(vouchers) == 1


def test_concurrent_cleanses_only_one_wins(db_path):
    """败例7：并发争抢同一行，唯一钉点保证只成功一笔，钉点不被冲掉。"""
    row = _an_empty_row(db_path)
    outcomes = []

    def worker(code, doctor):
        try:
            cleanse.submit_cleanse(
                row["id"],
                code,
                json.dumps({"visit_date": "2026-11-23", "investigator": doctor},
                           ensure_ascii=False),
                db_path=db_path,
            )
            outcomes.append("ok")
        except ServiceError:
            outcomes.append("err")

    t1 = threading.Thread(target=worker, args=("V1", "甲医生"))
    t2 = threading.Thread(target=worker, args=("V2", "乙医生"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("err") == 1
    vouchers = [v for v in cleanse.list_vouchers(db_path) if v["row_id"] == row["id"]]
    assert len(vouchers) == 1
    final = next(r for r in visits.list_rows(db_path) if r["id"] == row["id"])
    assert final["visit_code"] == vouchers[0]["visit_code"]
    assert final["quality"] == "合格"


def test_bad_cleanse_input_writes_nothing(db_path):
    """败例5：整笔失败不允许留半笔凭证/脏行。"""
    row = _an_empty_row(db_path)
    before = len(cleanse.list_vouchers(db_path))
    for code, payload in (("", PAYLOAD), ("V9", ""), ("V9", "not-json"), ("V9", "[]")):
        with pytest.raises(ServiceError):
            cleanse.submit_cleanse(row["id"], code, payload, db_path=db_path)
    assert len(cleanse.list_vouchers(db_path)) == before
    untouched = next(r for r in visits.list_rows(db_path) if r["id"] == row["id"])
    assert untouched["quality"] == "编码空"


def test_badge_counts_match_database(db_path):
    """败例8：徽标数与业务库空码条数、凭证表行数严格对账。"""
    summary = cleanse.badge_summary(db_path)
    conn = sqlite3.connect(db_path)
    empty_total = conn.execute(
        "SELECT COUNT(*) FROM visit_rows WHERE quality='编码空'"
    ).fetchone()[0]
    sealed = conn.execute(
        "SELECT COUNT(*) FROM code_cleanse_vouchers WHERE status='封档'"
    ).fetchone()[0]
    conn.close()
    assert summary["empty_code_total"] == empty_total
    assert summary["sealed_vouchers"] == sealed
    assert summary["pending_empty_code"] == empty_total
    assert summary["badge_consistent"] is True

    row = _an_empty_row(db_path)
    cleanse.submit_cleanse(row["id"], "V2", PAYLOAD, db_path=db_path)
    after = cleanse.badge_summary(db_path)
    assert after["sealed_vouchers"] == sealed + 1
    assert after["empty_code_total"] == empty_total - 1
    assert after["badge_consistent"] is True


def test_qc_scan_catches_empty_code_already_ingested(db_path):
    """质控扫描不再漏掉已入库编码空行（历史脏数据补严重发现）。"""
    row = _an_empty_row(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE visit_rows SET status='已入库' WHERE id=?", (row["id"],)
    )
    conn.commit()
    conn.close()

    result = qc.scan_batch(row["batch_id"], db_path=db_path)
    assert result["created"] >= 1
    findings = qc.list_findings(db_path, row_id=row["id"], status="开放")
    assert any(f["severity"] == "严重" for f in findings)


def test_reconcile_reports_empty_code_holes(db_path):
    """对账矩阵编码列空洞：空码无凭证计入 mismatch 与明细。"""
    report = reconcile.run_reconcile(db_path)
    assert report["detail"]["uncovered_empty_code"] >= 1
    assert "空码无清洗凭证" in report["summary"]

    row = _an_empty_row(db_path)
    cleanse.submit_cleanse(row["id"], "V2", PAYLOAD, db_path=db_path)
    report2 = reconcile.run_reconcile(db_path)
    assert report2["detail"]["sealed_cleanse_vouchers"] >= 1

    matrix = reconcile.site_gap_matrix(db_path)
    assert "empty_code_count" in matrix[0]
    assert "sealed_cleanse_count" in matrix[0]

    dash = reconcile.dashboard(db_path)
    assert "empty_code_rows" in dash
    assert "sealed_cleanse_vouchers" in dash


def test_only_one_active_read_path_exists():
    """败例4：多余骨架拒绝交卷——不存在乙侧快照表/快照服务模块。"""
    import os
    import app.services as services_pkg
    import app.db as db_module
    assert "code_cleanse_snapshot" not in db_module.SCHEMA
    services_dir = os.path.dirname(services_pkg.__file__)
    files = os.listdir(services_dir)
    assert not any("snapshot" in name for name in files)
    assert "cleanse.py" in files


def test_cleanse_rule_setting_seeded(db_path):
    from app.services import settings
    item = settings.get_setting("code_cleanse_rule", db_path=db_path)
    assert "凭证" in item["value"] and "指纹" in item["value"]

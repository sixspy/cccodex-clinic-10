from app.services import backfill, qc, reconcile, visits


def test_qc_backfill_reconcile_chain(db_path):
    batches = visits.list_batches(db_path)
    batch = next(b for b in batches if b["status"] == "采集中")
    scanned = qc.scan_batch(batch["id"], db_path=db_path)
    assert scanned["created"] >= 0
    detail = visits.get_batch(batch["id"], db_path)
    gap = next(
        (
            r
            for r in detail["items"]
            if r["quality"] != "合格" and r["status"] in ("待检", "待补采")
        ),
        None,
    )
    assert gap is not None
    if gap["quality"] == "编码空":
        # 编码空按新口径只能走编码清洗，补采必被拦；跨批找一个非编码空缺口行
        ejob = backfill.enqueue_from_row(gap["id"], db_path=db_path)
        blocked_job = backfill.run_job(ejob["id"], succeed=True, db_path=db_path)
        assert blocked_job["status"] == "失败"
        all_rows = visits.list_rows(db_path)
        gap = next(
            (
                r
                for r in all_rows
                if r["quality"] not in ("合格", "编码空")
                and r["status"] in ("待检", "待补采")
            ),
            None,
        )
    assert gap is not None, "库里应存在非编码空的缺口行可供补采"
    job = backfill.enqueue_from_row(gap["id"], db_path=db_path)
    assert job["status"] == "排队"
    done = backfill.run_job(job["id"], succeed=True, db_path=db_path)
    assert done["status"] == "成功"
    fixed = next(r for r in visits.list_rows(db_path) if r["id"] == gap["id"])
    assert fixed["quality"] == "合格"
    assert fixed["status"] == "已入库"

    # 编码空行不走补采：执行补采必须被入库闸门拦下并置失败
    empty = next(
        (r for r in visits.list_rows(db_path, quality="编码空")),
        None,
    )
    if empty is not None:
        existing = backfill.list_jobs(db_path, row_id=empty["id"])
        if existing:
            ejob = existing[0]
        else:
            ejob = backfill.enqueue_from_row(empty["id"], db_path=db_path)
        blocked = backfill.run_job(ejob["id"], succeed=True, db_path=db_path)
        assert blocked["status"] == "失败"
        assert "清洗凭证" in blocked["message"]

    report = reconcile.run_reconcile(db_path)
    assert "缺口行" in report["summary"]
    assert "detail" in report


def test_mock_crawl_and_ingest(db_path):
    batches = visits.list_batches(db_path)
    batch = next(b for b in batches if b["status"] == "采集中")
    before = visits.get_batch(batch["id"], db_path)["item_count"]
    crawled = visits.mock_crawl(batch["id"], count=3, mix_bad=True, db_path=db_path)
    assert crawled["created"] == 3
    after = visits.get_batch(batch["id"], db_path)
    assert after["item_count"] >= before + 3
    ingested = visits.ingest_ok_rows(batch["id"], db_path=db_path)
    assert ingested["ingested"] >= 0

import os

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.models import BunchReport
from app.services.seed import seed_if_empty


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSession()
    seed_if_empty(db)  # 内置 B12 线路

    def override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield {"client": TestClient(app), "session": TestingSession}
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def report_count(session_factory):
    db = session_factory()
    try:
        return db.scalar(select(func.count()).select_from(BunchReport))
    finally:
        db.close()


def test_preview_does_not_create_report(ctx):
    """对 B12 跑试算，历史报告条数前后保持不变。"""
    before = report_count(ctx["session"])

    resp = ctx["client"].post("/api/reports/preview?line_id=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] is False
    assert "id" not in body
    assert len(body["events"]) > 0
    assert body["line_code"] == "B12"

    after = report_count(ctx["session"])
    assert after == before
    # 报告列表条数同样不变
    listed = ctx["client"].get("/api/reports").json()
    assert len(listed) == before


def test_preview_specific_stop(ctx):
    """试算支持指定站点，只返回该站事件，仍不写报告。"""
    before = report_count(ctx["session"])
    resp = ctx["client"].post("/api/reports/preview?line_id=1&stop_name=市民中心")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stop_name"] == "市民中心"
    assert body["events"]
    assert all(e["stop_name"] == "市民中心" for e in body["events"])
    assert report_count(ctx["session"]) == before


def test_run_still_creates_one_report(ctx):
    """真正检测写入且仅写入一条报告，事件与试算一致。"""
    before = report_count(ctx["session"])

    preview = ctx["client"].post("/api/reports/preview?line_id=1").json()
    run = ctx["client"].post("/api/reports/run?line_id=1")
    assert run.status_code == 200
    r = run.json()
    assert r["saved"] is True
    assert r["id"] is not None
    assert r["events"] == preview["events"]

    assert report_count(ctx["session"]) == before + 1
    listed = ctx["client"].get("/api/reports").json()
    assert len(listed) == before + 1
    assert listed[0]["id"] == r["id"]
    assert listed[0]["events"] == r["events"]


def test_suggestions_does_not_create_report(ctx):
    """建议接口复用只读试算逻辑，也不应再产生报告。"""
    before = report_count(ctx["session"])
    resp = ctx["client"].get("/api/reports/suggestions?line_id=1")
    assert resp.status_code == 200
    assert all(e["status"] != "normal" for e in resp.json()["suggestions"])
    assert report_count(ctx["session"]) == before


def test_preview_unknown_line_404(ctx):
    resp = ctx["client"].post("/api/reports/preview?line_id=999")
    assert resp.status_code == 404


def test_double_preview_keeps_count_and_timeline(ctx):
    """连点两次试算：报告条数原地不动，时间轴也不得多脏点。"""
    before = report_count(ctx["session"])
    marks_before = ctx["client"].get("/api/reports/timeline?line_id=1&stop_name=市民中心").json()["marks"]

    for _ in range(2):
        resp = ctx["client"].post("/api/reports/preview?line_id=1")
        assert resp.status_code == 200
        assert resp.json()["saved"] is False

    assert report_count(ctx["session"]) == before
    marks_after = ctx["client"].get("/api/reports/timeline?line_id=1&stop_name=市民中心").json()["marks"]
    assert len(marks_after) == len(marks_before)
    assert marks_after == marks_before


def test_run_failure_keeps_existing_reports(ctx):
    """真检失败（线路不存在 404）不得清掉已落报告行。"""
    ok = ctx["client"].post("/api/reports/run?line_id=1")
    assert ok.status_code == 200
    kept_id = ok.json()["id"]
    assert report_count(ctx["session"]) == 1

    bad = ctx["client"].post("/api/reports/run?line_id=999")
    assert bad.status_code == 404

    listed = ctx["client"].get("/api/reports").json()
    assert len(listed) == 1
    assert listed[0]["id"] == kept_id


def test_run_with_zero_events_still_persists_one(ctx):
    """真检跑通但无异常/无事件时也不得空转：仍恰好落一行。"""
    before = report_count(ctx["session"])
    resp = ctx["client"].post("/api/reports/run?line_id=1&stop_name=不存在的站点")
    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] is True
    assert body["id"] is not None
    assert body["events"] == []
    assert report_count(ctx["session"]) == before + 1
    listed = ctx["client"].get("/api/reports").json()
    assert listed[0]["id"] == body["id"]
    assert listed[0]["events"] == []

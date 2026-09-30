import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import Arrival, BunchReport, Line, Trip
from app.services.bunch_engine import detect_bunching, events_to_dicts
from app.services.scope_helpers import flatten_marks
router = APIRouter(prefix="/reports", tags=["reports"])

def _detect(db: Session, line_id: int, stop_name: str | None) -> tuple[Line, list[dict], str]:
    """按当前线路阈值对全线或指定站做一次检测，返回 (线路, 事件列表, 范围)。只读，不写报告。"""
    line = db.get(Line, line_id)
    if not line: raise HTTPException(404, "线路不存在")
    trips = db.scalars(select(Trip).where(Trip.line_id == line_id)).all()
    trip_ids = [t.id for t in trips]
    trip_no_map = {t.id: t.trip_no for t in trips}
    arrivals = db.scalars(select(Arrival).where(Arrival.trip_id.in_(trip_ids))).all()
    payload = [{"stop_name": a.stop_name, "trip_no": trip_no_map[a.trip_id], "actual_arrive": a.actual_arrive}
               for a in arrivals if stop_name is None or a.stop_name == stop_name]
    events = detect_bunching(payload, line.planned_headway_min, line.bunch_threshold, line.large_threshold)
    return line, events_to_dicts(events), (stop_name or "*")

def save_report(db: Session, line_id: int, scope: str, data: list[dict]) -> BunchReport:
    """真检落库：一次性插入并提交一行报告。

    提交失败必须回滚——不能清掉/覆盖此前已经落成的报告行，也不能伪装成成功。
    """
    report = BunchReport(line_id=line_id, stop_name=scope, created_at=datetime.utcnow(),
                         summary_json=json.dumps(data, ensure_ascii=False))
    db.add(report)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(report)
    return report

@router.get("")
def list_reports(db: Session = Depends(get_db)):
    rows = db.scalars(select(BunchReport).order_by(BunchReport.id.desc())).all()
    return [{"id": r.id, "line_id": r.line_id, "stop_name": r.stop_name,
             "created_at": r.created_at.isoformat(), "events": json.loads(r.summary_json)} for r in rows]

@router.post("/preview")
def preview_detection(line_id: int, stop_name: str | None = None, db: Session = Depends(get_db)):
    # 试算：只吐事件列表，不领 id、不增报告行、不产生轴点
    line, data, scope = _detect(db, line_id, stop_name)
    db.rollback()  # 只读检测，丢弃事务内任何待写状态
    return {"line_id": line_id, "line_code": line.code, "stop_name": scope,
            "saved": False, "events": data}

@router.post("/run")
def run_detection(line_id: int, stop_name: str | None = None, db: Session = Depends(get_db)):
    # 真检：用同一套检测逻辑算出事件，再原子落成恰好一行报告
    line, data, scope = _detect(db, line_id, stop_name)
    try:
        report = save_report(db, line_id, scope, data)
    except Exception:
        # 落库失败如实报 500；旧报告行与时间轴到站点均未被改动
        raise HTTPException(500, "检测结果保存失败，已保留全部历史报告")
    return {"id": report.id, "line_id": line_id, "line_code": line.code, "stop_name": scope,
            "saved": True, "events": data}

@router.get("/suggestions")
def suggestions(line_id: int, db: Session = Depends(get_db)):
    # 建议只来自最近一条【已落库】报告；试算结果不会出现在这里
    report = db.scalars(
        select(BunchReport).where(BunchReport.line_id == line_id)
        .order_by(BunchReport.id.desc())
    ).first()
    if report is None:
        return {"line_id": line_id, "report_id": None, "saved": False, "suggestions": []}
    events = json.loads(report.summary_json)
    return {"line_id": line_id, "report_id": report.id, "saved": True,
            "suggestions": [e for e in events if e["status"] != "normal"]}

@router.get("/timeline")
def timeline(line_id: int, stop_name: str = "市民中心", db: Session = Depends(get_db)):
    # 时间轴只反映真实到站记录；试算/真检都不会单独往轴上加点
    trips = db.scalars(select(Trip).where(Trip.line_id == line_id)).all()
    trip_ids = [t.id for t in trips]
    trip_no_map = {t.id: t.trip_no for t in trips}
    arrivals = sorted(db.scalars(select(Arrival).where(Arrival.trip_id.in_(trip_ids), Arrival.stop_name == stop_name)).all(),
                      key=lambda a: a.actual_arrive)
    if not arrivals: return {"stop_name": stop_name, "marks": []}
    t0 = arrivals[0].actual_arrive
    span = max((arrivals[-1].actual_arrive - t0).total_seconds(), 1)
    marks = [{"trip_no": trip_no_map[a.trip_id], "actual_arrive": a.actual_arrive.isoformat(),
              "pct": round((a.actual_arrive - t0).total_seconds() / span * 100, 2)} for a in arrivals]
    return {"stop_name": stop_name, "marks": flatten_marks(marks)}

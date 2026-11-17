"""跨站补估编排：写入实测、补估、撤销、回读。

每条写操作都有明确的「依据」与幂等键：
- 补测携带 expected_revision（可选），版本不符即拒绝，不覆盖别人刚写的实测；
- 补估携带 idempotency_key，重试返回同一结果，不重复估雨；
- 补估支持 expected_basis_digest，且落库事务里再核对一次依据（防并发窗口）；
- 撤销先落作业意图，再逐条处理，中断后可从 pending 继续，快照不动。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ... import errors
from ...store import Store
from . import estimator as est


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _require(payload: dict, field: str) -> str:
    value = str((payload or {}).get(field) or "").strip()
    if not value:
        raise errors.BadRequest(f"缺少字段 {field}",
                                code="E_SUPPLEMENT_FIELD_REQUIRED",
                                detail={"field": field})
    return value


# ---------------------------------------------------------------- 实测（含补测）
def put_observation(store: Store, payload: dict) -> dict:
    """写一条实测（普通抄报或补测）。补测到达时同步取代覆盖该日的估值。"""
    station = _require(payload, "station_id")
    date = est.parse_date(_require(payload, "date")).isoformat()
    rainfall = est.parse_mm(payload.get("rainfall_mm"))
    key = str(payload.get("idempotency_key") or "").strip()
    expected = payload.get("expected_revision")
    expected_revision = None if expected is None else int(expected)
    source = str(payload.get("source") or "manual")
    stamp = now_iso()

    if key:
        prior = store.get_fillin(key)
        if prior is not None:
            return _observation_payload(store, station, prior["obs_date"],
                                        replayed=True, fillin_key=key,
                                        superseded=[])

    saved = store.write_observation(
        station_id=station, obs_date=date, rainfall_mm=str(rainfall),
        source=source, stamp=stamp, expected_revision=expected_revision)
    if saved is None:
        raise errors.Conflict(
            f"{station} {date} 的实测已被并发更新，拒绝用过期版本覆盖",
            code="E_SUPPLEMENT_REVISION_STALE",
            detail={"station_id": station, "date": date,
                    "expected_revision": expected_revision})
    if key:
        store.record_fillin(idempotency_key=key, station_id=station, obs_date=date,
                            rainfall_mm=str(rainfall), revision=saved["revision"],
                            stamp=stamp)

    replaced = _replace_estimates(store, station, date, saved["revision"], stamp)
    invalidated = sweep_stale(store, station_id=station, stamp=stamp)
    return _observation_payload(store, station, date, replayed=False,
                                fillin_key=key or "", superseded=replaced,
                                invalidated=invalidated)


def _replace_estimates(store: Store, station: str, date: str, revision: int,
                       stamp: str) -> list[str]:
    """补测落在某份补估的缺日内 → 该份估值整体退出当前口径。

    逐日条目转 superseded 并记下取代它的实测版本；快照与估算原值一字不改，
    所以旧结果仍能按当时的依据复现。
    """
    affected = []
    for row in store.list_supplements(target_station=station, include_revoked=False):
        if row["status"] not in ("active", "stale"):
            continue
        gaps = set(_safe_json(row["basis_json"], {}).get("gap_dates") or [])
        if date not in gaps:
            continue
        if store.mark_supplement_superseded(row["estimate_id"], "MEASURED_FILLED",
                                            stamp, obs_date=date, revision=revision):
            affected.append(row["estimate_id"])
    return affected


def _observation_payload(store: Store, station: str, date: str, *, replayed: bool,
                         fillin_key: str, superseded: list[str],
                         invalidated: list[str] | None = None) -> dict:
    row = store.get_observation(station, date)
    return {
        "status": "ok",
        "station_id": station,
        "date": date,
        "rainfall_mm": row["rainfall_mm"] if row else None,
        "revision": row["revision"] if row else None,
        "source": row["source"] if row else None,
        "kind": "measured",
        "replayed": replayed,
        "idempotency_key": fillin_key,
        "superseded_estimates": superseded,
        "invalidated_estimates": invalidated or [],
        "updated_at": row["updated_at"] if row else None,
    }


def put_neighbor(store: Store, payload: dict) -> dict:
    target = _require(payload, "station_id")
    neighbor = _require(payload, "neighbor_id")
    coefficient = est.parse_coefficient(payload.get("coefficient"))
    if target == neighbor:
        raise errors.UnprocessableInput(
            "邻站不能是目标站本身",
            code="E_SUPPLEMENT_CYCLE", detail={"cycle": [target, target]})
    stamp = now_iso()
    saved = store.write_neighbor(station_id=target, neighbor_id=neighbor,
                                 coefficient=str(coefficient), stamp=stamp)
    invalidated = sweep_stale(store, station_id=target, stamp=stamp)
    return {"status": "ok", "station_id": target, "neighbor_id": neighbor,
            "coefficient": saved["coefficient"], "updated_at": saved["updated_at"],
            "invalidated_estimates": invalidated}


# ---------------------------------------------------------------- 补估
def preview(store: Store, payload: dict) -> dict:
    target = _require(payload, "station_id")
    month = _require(payload, "month")
    result = est.compute(store, target, month)
    return {
        "target_station": target,
        "month": month,
        "basis_digest": result["basis_digest"],
        "basis": _basis_payload(result["basis"]),
        "measured_mm": str(result["measured_mm"]),
        "measured_days": result["measured_days"],
        "estimated_mm": str(result["estimated_mm"]),
        "estimated_days": result["estimated_days"],
        "total_mm": str(result["total_mm"]),
        "gap_days": result["gap_days"],
        "unfilled_dates": result["unfilled_dates"],
        "entries": [{"date": item["date"],
                     "estimated_mm": str(item["estimated_mm"]),
                     "neighbor_basis": item["neighbor_basis"]}
                    for item in result["entries"]],
    }


def apply(store: Store, payload: dict) -> dict:
    target = _require(payload, "station_id")
    month = _require(payload, "month")
    key = _require(payload, "idempotency_key")

    existing = store.get_supplement_by_key(key)
    if existing is not None:
        return _estimate_payload(store, existing, replayed=True)

    # 客户端拿预览得到的摘要来锁依据；没有摘要也允许，落库事务里仍会核对
    basis = est.build_basis(store, target, month)
    expected = str(payload.get("expected_basis_digest") or "").strip()
    if expected and expected != est.basis_digest(basis):
        raise errors.Conflict(
            "依据已变化：邻站实测或系数在本次补估期间被更新，拒绝用旧依据估算",
            code="E_SUPPLEMENT_BASIS_STALE",
            detail={"expected_basis_digest": expected,
                    "actual_basis_digest": est.basis_digest(basis)})
    result = est.finalize(basis)

    stamp = now_iso()
    estimate_id = est.estimate_id_for(key)
    estimate = {
        "estimate_id": estimate_id,
        "idempotency_key": key,
        "target_station": target,
        "month": month,
        "neighbors": [{"neighbor_id": link["neighbor_id"],
                       "coefficient": link["coefficient"]}
                      for link in result["basis"]["neighbors"]],
        "basis": result["basis"],
        "basis_digest": result["basis_digest"],
        "result": {
            "measured_mm": str(result["measured_mm"]),
            "measured_days": result["measured_days"],
            "estimated_mm": str(result["estimated_mm"]),
            "estimated_days": result["estimated_days"],
            "total_mm": str(result["total_mm"]),
            "gap_days": result["gap_days"],
            "unfilled_dates": result["unfilled_dates"],
        },
        "measured_mm": result["measured_mm"],
        "estimated_mm": result["estimated_mm"],
        "total_mm": result["total_mm"],
        "gap_days": result["gap_days"],
        "estimated_days": result["estimated_days"],
        "created_at": stamp,
    }
    dependencies = [{"station_id": target, "role": "target"}]
    dependencies += [{"station_id": link["neighbor_id"], "role": "neighbor"}
                     for link in result["basis"]["neighbors"]]
    saved = store.save_supplement(
        estimate=estimate, entries=result["entries"], dependencies=dependencies,
        basis_check="strict")
    if not saved:
        # 同一写事务里发现：幂等键已被别人插入，或依据快照与当前台账不一致
        replay = store.get_supplement_by_key(key)
        if replay is not None:
            return _estimate_payload(store, replay, replayed=True)
        raise errors.Conflict(
            "依据在落库前发生变化（并发补测/补估），拒绝用过期依据估算",
            code="E_SUPPLEMENT_BASIS_STALE",
            detail={"target_station": target, "month": month})
    _supersede_prior(store, estimate_id, target, month, stamp)
    row = store.get_supplement(estimate_id)
    return _estimate_payload(store, row, replayed=False)


def _supersede_prior(store: Store, estimate_id: str, target: str, month: str,
                     stamp: str) -> None:
    """同一站同月再补估时，旧补估转 superseded，但快照原样保留。"""
    with store.write_txn() as conn:
        prior = [row["estimate_id"] for row in conn.execute(
            "SELECT estimate_id FROM supplement_estimates "
            "WHERE target_station=? AND month=? AND status IN ('active','stale') "
            "AND estimate_id<>?", (target, month, estimate_id)).fetchall()]
        conn.execute(
            "UPDATE supplement_estimates SET status='superseded', "
            "status_reason='NEW_ESTIMATE', updated_at=? "
            "WHERE target_station=? AND month=? AND status IN ('active','stale') "
            "AND estimate_id<>?", (stamp, target, month, estimate_id))
        for prior_id in prior:
            conn.execute(
                "UPDATE supplement_entries SET status='superseded', "
                "superseded_at=COALESCE(superseded_at, ?) "
                "WHERE estimate_id=? AND status='active'", (stamp, prior_id))


def _estimate_payload(store: Store, row: dict, *, replayed: bool) -> dict:
    entries = store.load_supplement_entries(row["estimate_id"])
    result = _safe_json(row["result_json"], {})
    basis = _safe_json(row["basis_json"], {})
    return {
        "status": "ok",
        "estimate_id": row["estimate_id"],
        "idempotency_key": row["idempotency_key"],
        "target_station": row["target_station"],
        "month": row["month"],
        "estimate_status": row["status"],
        "status_reason": row["status_reason"],
        "replayed": replayed,
        "basis_digest": row["basis_digest"],
        "basis": _basis_payload(basis),
        "neighbors": _safe_json(row["neighbors_json"], []),
        "measured_mm": row["measured_mm"],
        "measured_days": result.get("measured_days"),
        "estimated_mm": row["estimated_mm"],
        "estimated_days": row["estimated_days"],
        "total_mm": row["total_mm"],
        "gap_days": row["gap_days"],
        "unfilled_dates": result.get("unfilled_dates", []),
        "entries": [{
            "date": item["obs_date"],
            "estimated_mm": item["estimated_mm"],
            "neighbor_basis": item["neighbor_basis"],
            "status": item["status"],
            "superseded_by_revision": item["superseded_by_revision"],
        } for item in entries],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "revoked_at": row["revoked_at"],
        "revoke_job_id": row["revoke_job_id"],
    }


def _basis_payload(basis: dict) -> dict:
    return {
        "target_station": basis.get("target_station"),
        "month": basis.get("month"),
        "window": basis.get("window"),
        "calendar_days": basis.get("calendar_days"),
        "gap_dates": basis.get("gap_dates", []),
        "neighbors": basis.get("neighbors", []),
        "observations": basis.get("observations", []),
    }


def _safe_json(text: str, default):
    import json
    try:
        return json.loads(text or "")
    except Exception:
        return default


# ---------------------------------------------------------------- 撤销
def revoke(store: Store, payload: dict) -> dict:
    key = _require(payload, "idempotency_key")
    reason = str(payload.get("reason") or "依据更正").strip() or "依据更正"
    existing = store.get_revocation_by_key(key)
    if existing is not None:
        return {"status": "ok", "job": _job_payload(store, existing),
                "replayed": True}
    estimate_ids = [str(item) for item in (payload.get("estimate_ids") or []) if item]
    if not estimate_ids and payload.get("station_id") and payload.get("month"):
        estimate_ids = [row["estimate_id"] for row in store.list_supplements(
            target_station=str(payload["station_id"]), month=str(payload["month"]),
            include_revoked=False) if row["status"] in ("active", "stale")]
    if not estimate_ids:
        raise errors.BadRequest("撤销需要 estimate_ids 或 station_id + month",
                                code="E_SUPPLEMENT_FIELD_REQUIRED",
                                detail={"field": "estimate_ids"})
    job_id = est.job_id_for(key)
    created = store.create_revocation(job_id=job_id, idempotency_key=key,
                                      reason=reason, estimate_ids=estimate_ids,
                                      stamp=now_iso())
    if created is None:
        existing = store.get_revocation_by_key(key)
        return {"status": "ok", "job": _job_payload(store, existing),
                "replayed": True}
    interrupted = _process_revocation(store, job_id, reason,
                                      limit=payload.get("interrupt_after"))
    return {"status": "ok", "job": _job_payload(store, interrupted),
            "replayed": False}


def resume(store: Store, payload: dict | None = None) -> dict:
    body = payload or {}
    job_id = str(body.get("job_id") or "").strip()
    jobs = []
    if job_id:
        job = store.get_revocation(job_id)
        if job is None:
            raise errors.NotFound(f"撤销作业 {job_id} 不存在",
                                  code="E_SUPPLEMENT_JOB_NOT_FOUND")
        jobs = [job]
    else:
        jobs = store.pending_revocations()
    results = []
    for job in jobs:
        if job["status"] == "done" and not job_id:
            continue
        results.append(_process_revocation(store, job["job_id"], job["reason"],
                                           limit=body.get("interrupt_after")))
    return {"status": "ok", "jobs": [_job_payload(store, job) for job in results],
            "resumed": len(results)}


def resume_all_pending(store: Store) -> int:
    """读回/启动时自动收尾，崩溃留下的 pending 作业不会一直挂着。"""
    count = 0
    for job in store.pending_revocations():
        _process_revocation(store, job["job_id"], job["reason"])
        count += 1
    return count


def _process_revocation(store: Store, job_id: str, reason: str,
                        limit=None) -> dict:
    processed = 0
    for item in store.revocation_items(job_id):
        if item["status"] == "done":
            continue
        if limit is not None and processed >= int(limit):
            break
        store.revoke_one(job_id=job_id, estimate_id=item["estimate_id"],
                         reason=reason, stamp=now_iso())
        processed += 1
    return store.finish_revocation(job_id, now_iso())


def _job_payload(store: Store, job: dict) -> dict:
    items = store.revocation_items(job["job_id"])
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "reason": job["reason"],
        "total": job["total"],
        "processed": job["processed"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "items": [{"estimate_id": item["estimate_id"], "status": item["status"],
                   "code": item["code"], "processed_at": item["processed_at"]}
                  for item in items],
    }


# ---------------------------------------------------------------- 失效扫描 / 回读
def sweep_stale(store: Store, *, station_id: str | None = None,
                stamp: str | None = None) -> list[str]:
    """把依据已不再成立的 active 补估标为 stale；快照与结果原样保留。"""
    stamp = stamp or now_iso()
    marked = []
    for row in store.list_supplements(include_revoked=False):
        if row["status"] != "active":
            continue
        dependencies = store.load_supplement_dependencies(row["estimate_id"])
        if station_id and station_id not in {d["station_id"] for d in dependencies}:
            continue
        basis = _safe_json(row["basis_json"], {})
        if not store.basis_is_current(basis):
            if store.mark_supplement_stale(row["estimate_id"],
                                           "BASIS_CHANGED", stamp):
                marked.append(row["estimate_id"])
    return marked


def read_back(store: Store, *, station_id: str | None = None,
              month: str | None = None, include_revoked: bool = True) -> dict:
    """只读回读：扫一遍失效依据后可读，不改撤销作业状态（续跑走 resume）。"""
    sweep_stale(store)
    rows = store.list_supplements(target_station=station_id, month=month,
                                  include_revoked=include_revoked)
    estimates = [_estimate_payload(store, row, replayed=False) for row in rows]
    current = None
    for row in rows:
        if row["status"] == "active":
            current = _estimate_payload(store, row, replayed=False)
    body = {
        "status": "ok",
        "target_station": station_id,
        "month": month,
        "estimates": estimates,
        "current": current,
        "pending_revocations": [_job_payload(store, job)
                                for job in store.pending_revocations()],
    }
    if station_id and month:
        body["month_numbers"] = month_numbers(store, station_id, month, current)
    return body


def reproduce(store: Store, estimate_id: str) -> dict:
    """按落库时的依据快照重算一遍，核对是否与当时结果逐位一致。

    快照不可变，所以任何时候都能回答「这份已发/已撤销的应急月量按什么依据算出、
    现在还能不能复现」，不依赖当前台账。
    """
    row = store.get_supplement(estimate_id)
    if row is None:
        raise errors.NotFound(f"补估 {estimate_id} 不存在",
                              code="E_ESTIMATE_NOT_FOUND")
    basis = _safe_json(row["basis_json"], {})
    replayed = est.finalize(basis)
    stored = _safe_json(row["result_json"], {})
    stored_entries = {
        item["obs_date"]: (str(item["estimated_mm"]), item["neighbor_basis"])
        for item in store.load_supplement_entries(estimate_id)}
    replayed_entries = {
        item["date"]: (str(item["estimated_mm"]), item["neighbor_basis"])
        for item in replayed["entries"]}
    checks = {
        "basis_digest": est.basis_digest(basis) == row["basis_digest"],
        "measured_mm": str(replayed["measured_mm"]) == str(row["measured_mm"]),
        "estimated_mm": str(replayed["estimated_mm"]) == str(row["estimated_mm"]),
        "total_mm": str(replayed["total_mm"]) == str(row["total_mm"]),
        "entries": stored_entries == replayed_entries,
        "stored_result_matches": {
            "measured_mm": stored.get("measured_mm"),
            "estimated_mm": stored.get("estimated_mm"),
            "total_mm": stored.get("total_mm"),
        } == {"measured_mm": row["measured_mm"],
              "estimated_mm": row["estimated_mm"],
              "total_mm": row["total_mm"]},
    }
    return {
        "status": "ok",
        "estimate_id": estimate_id,
        "estimate_status": row["status"],
        "basis_digest": row["basis_digest"],
        "recomputed_basis_digest": est.basis_digest(basis),
        "reproducible": all(checks.values()),
        "checks": checks,
        "basis": _basis_payload(basis),
        "result": {
            "measured_mm": str(replayed["measured_mm"]),
            "measured_days": replayed["measured_days"],
            "estimated_mm": str(replayed["estimated_mm"]),
            "estimated_days": replayed["estimated_days"],
            "total_mm": str(replayed["total_mm"]),
            "gap_days": replayed["gap_days"],
            "unfilled_dates": replayed["unfilled_dates"],
        },
        "entries": [{"date": item["date"],
                     "estimated_mm": str(item["estimated_mm"]),
                     "neighbor_basis": item["neighbor_basis"]}
                    for item in replayed["entries"]],
    }


def month_numbers(store: Store, station_id: str, month: str,
                  current: dict | None = None) -> dict:
    """实测口径与含估口径分开给出；估值永远单列，不并入实测。"""
    start, end = est.parse_month(month)
    rows = store.observations_between(station_id, start.isoformat(), end.isoformat())
    measured = sum((Decimal(str(row["rainfall_mm"])) for row in rows), Decimal("0")
                   ).quantize(est.MM_QUANT)
    measured_dates = {row["obs_date"] for row in rows}
    calendar_days = (end - start).days + 1
    gap_dates = [(start + dt.timedelta(days=offset)).isoformat()
                 for offset in range(calendar_days)
                 if (start + dt.timedelta(days=offset)).isoformat() not in measured_dates]
    estimated = Decimal("0")
    estimated_days = 0
    unfilled = list(gap_dates)
    if current is not None:
        estimated = Decimal(str(current["estimated_mm"]))
        estimated_days = int(current["estimated_days"])
        unfilled = list(current["unfilled_dates"] or [])
    return {
        "calendar_days": calendar_days,
        "measured": {
            "days": len(rows),
            "rainfall_mm": str(measured),
            "gap_days": len(gap_dates),
            "gap_dates": gap_dates,
        },
        "estimated": {
            "days": estimated_days,
            "rainfall_mm": str(estimated),
            "unfilled_dates": unfilled,
            "estimate_id": current["estimate_id"] if current else None,
            "source": "neighbor_measured_x_coefficient" if current else None,
        },
        "with_estimate": {
            "days": len(rows) + estimated_days,
            "rainfall_mm": (measured + estimated).quantize(est.MM_QUANT),
        },
        "estimate_is_measured_basis": False,
    }

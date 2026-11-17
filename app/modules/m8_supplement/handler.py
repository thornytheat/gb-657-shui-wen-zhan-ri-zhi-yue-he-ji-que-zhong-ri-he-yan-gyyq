"""M8 入口：跨站补测、补估、撤销、回读。

接口分四组，全部走同一套 service，页面只读结果：
- POST /api/supplement/observations   写实测（含补测），可带 expected_revision；
- POST /api/supplement/neighbors      配置邻站系数；
- POST /api/supplement/preview        只读预览（不落库）；
- POST /api/supplement/estimates      补估落库，幂等键 + 依据摘要双重把关；
- POST /api/supplement/revocations    撤销（幂等键 + 作业号，可中断续跑）；
- GET  /api/supplement/readback       回读：实测/含估分开，旧结果保留原依据
- GET  /api/supplement/estimates/{id} 按快照复现某份补估（撤销后也可复算）；
- GET  /api/supplement               页面。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ...web import Ctx, get_ctx, render
from . import service

router = APIRouter(tags=["M8 跨站补估"])


@router.post("/api/supplement/observations")
def api_put_observation(payload: dict, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.put_observation(ctx.store, payload)


@router.post("/api/supplement/neighbors")
def api_put_neighbor(payload: dict, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.put_neighbor(ctx.store, payload)


@router.post("/api/supplement/preview")
def api_preview(payload: dict, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.preview(ctx.store, payload)


@router.post("/api/supplement/estimates")
def api_apply(payload: dict, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.apply(ctx.store, payload)


@router.post("/api/supplement/revocations")
def api_revoke(payload: dict, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.revoke(ctx.store, payload)


@router.post("/api/supplement/revocations/resume")
def api_resume(payload: dict | None = None,
               ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.resume(ctx.store, payload)


@router.get("/api/supplement/readback")
def api_readback(station_id: str | None = None, month: str | None = None,
                 include_revoked: bool = True,
                 ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.read_back(ctx.store, station_id=station_id, month=month,
                             include_revoked=include_revoked)


@router.get("/api/supplement/estimates/{estimate_id}")
def api_reproduce(estimate_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.reproduce(ctx.store, estimate_id)


@router.get("/supplement")
def page_supplement(request: Request, station_id: str | None = None,
                    month: str | None = None,
                    ctx: Ctx = Depends(get_ctx)):
    try:
        body = service.read_back(ctx.store, station_id=station_id, month=month)
        error = None
    except Exception as exc:  # 页面错误不阻断，接口仍抛业务异常
        body = {"estimates": [], "current": None, "pending_revocations": []}
        error = getattr(exc, "message", str(exc))
    return render(request, "supplement.html", page="supplement",
                  page_title="跨站补估", station_id=station_id, month=month,
                  supplement=body, supplement_error=error)

from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import db
from app.services.common import rows
from app.services.lifecycle import (
    OPEN_STATES,
    DomainError,
    begin_deregister,
    finalize_deregister,
    rollback_deregister,
)

router = APIRouter(tags=["舱体"])

_OPEN_MARKS = ",".join("?" * len(OPEN_STATES))


class DeregisterIn(BaseModel):
    reason: str | None = None


class RollbackIn(BaseModel):
    reason: str | None = None


@router.get("/舱体")
def list_pods():
    with db() as conn:
        return rows(
            conn.execute(
                f"""
                SELECT p.*, i.name AS image_name, i.tag AS image_tag, i.pull_state,
                       pr.color AS probe_color, pr.fail_streak,
                       (SELECT COUNT(*) FROM start_orders o
                         WHERE o.pod_id = p.id AND o.status IN ({_OPEN_MARKS})) AS open_order_count
                FROM pods p
                LEFT JOIN images i ON i.id = p.image_id
                LEFT JOIN probes pr ON pr.pod_id = p.id
                ORDER BY p.id
                """,
                OPEN_STATES,
            )
        )


@router.get("/撤册流水")
def list_deregistrations():
    with db() as conn:
        return rows(
            conn.execute(
                """
                SELECT d.*, p.name AS pod_name
                FROM deregistrations d JOIN pods p ON p.id = d.pod_id
                ORDER BY d.id DESC
                """
            )
        )


@router.post("/舱体/{pod_id}/撤册")
def begin_deregister_api(pod_id: int, body: DeregisterIn):
    with db() as conn:
        try:
            return begin_deregister(conn, pod_id, body.reason)
        except DomainError as e:
            return {"错误": str(e)}


@router.post("/舱体/{pod_id}/执行下架")
def finalize_deregister_api(pod_id: int):
    with db() as conn:
        try:
            return finalize_deregister(conn, pod_id)
        except DomainError as e:
            return {"错误": str(e)}


@router.post("/舱体/{pod_id}/撤回撤册")
def rollback_deregister_api(pod_id: int, body: RollbackIn):
    with db() as conn:
        try:
            return rollback_deregister(conn, pod_id, body.reason)
        except DomainError as e:
            return {"错误": str(e)}

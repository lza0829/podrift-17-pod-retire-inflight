from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import db
from app.services.common import rows
from app.services.lifecycle import DomainError, close_order, create_order

router = APIRouter(tags=["启停单"])


class OrderIn(BaseModel):
    pod_id: int
    action: str  # 启动 | 停止
    target_image_id: int | None = None


@router.get("/启停单")
def list_orders():
    with db() as conn:
        return rows(
            conn.execute(
                """
                SELECT o.*, p.name AS pod_name
                FROM start_orders o JOIN pods p ON p.id = o.pod_id
                ORDER BY o.id DESC
                """
            )
        )


@router.post("/启停单")
def create_order_api(body: OrderIn):
    with db() as conn:
        try:
            return create_order(conn, body.pod_id, body.action, body.target_image_id)
        except DomainError as e:
            return {"错误": str(e)}


@router.post("/启停单/{order_id}/收口")
def close_order_api(order_id: int):
    with db() as conn:
        try:
            return close_order(conn, order_id)
        except DomainError as e:
            return {"错误": str(e)}

from __future__ import annotations
from fastapi import APIRouter
from app.db import db
from app.services.common import rows
from app.services.lifecycle import OPEN_STATES, RUN_OPEN_STATES

router = APIRouter(tags=["对读屏"])

_OPEN_MARKS = ",".join("?" * len(OPEN_STATES))


@router.get("/对读")
def align_view():
    with db() as conn:
        pods = rows(
            conn.execute(
                f"""
                SELECT p.id, p.name, p.run_state, p.deregister_reason,
                       pr.color AS probe_color, pr.fail_streak,
                       i.pull_state,
                       o.status AS last_order_status, o.note AS last_order_note,
                       o.close_run_state AS last_close_run_state,
                       (SELECT COUNT(*) FROM start_orders oo
                         WHERE oo.pod_id = p.id AND oo.status IN ({_OPEN_MARKS})) AS open_order_count,
                       (SELECT COUNT(*) FROM start_orders oo
                         WHERE oo.pod_id = p.id AND oo.status = '已完成') AS finished_order_count
                FROM pods p
                LEFT JOIN probes pr ON pr.pod_id = p.id
                LEFT JOIN images i ON i.id = p.image_id
                LEFT JOIN start_orders o ON o.id = (
                  SELECT MAX(id) FROM start_orders WHERE pod_id = p.id
                )
                ORDER BY p.id
                """,
                OPEN_STATES,
            )
        )
        quota = dict(conn.execute("SELECT * FROM quotas WHERE id=1").fetchone())
        policy = dict(conn.execute("SELECT * FROM restart_policy WHERE id=1").fetchone())

        # 撤册中桶：口径 A 下撤册中舱体既不能新开启动，也不能把在途启动收口成成功。
        # 判定依据是每笔成功单收口瞬间盖的舱体运行态印（close_run_state），
        # 不依赖秒级时间戳比较。桶内「撤册中启停成功」必须恒为 0 —— 可复现的读侧不变量。
        deregistering = [p for p in pods if p["run_state"] == "撤册中"]
        marks = ",".join("?" * len(RUN_OPEN_STATES))
        deregistering_success = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM start_orders
            WHERE status = '已完成' AND close_run_state IN ({marks})
            """,
            RUN_OPEN_STATES,
        ).fetchone()["c"]
        if deregistering_success != 0:
            # 永不允许出现：一旦读到说明互斥口径被破坏，直接让读侧炸出而非静默展示
            raise RuntimeError(f"撤册中桶出现 {deregistering_success} 笔撤册中启停成功，违反口径 A")

        return {
            "舱体投影": pods,
            "配额": quota,
            "重启策": policy,
            "撤册中桶": {
                "舱数": len(deregistering),
                "撤册中启停成功数": deregistering_success,
                "在途启停总数": sum(int(p["open_order_count"]) for p in deregistering),
            },
        }

"""舱体撤册与在途启停的互斥生命周期。

口径锁定（口径 A，禁止再切）：
- 撤册必须收口或硬拒未完成启停：撤册受理前若存在未收口启停单，整笔硬拒；
- 撤册成功后不得继续启动：撤册中/已撤册舱体禁止新开启停单，在途单收口也硬拒；
- 读侧「撤册中」桶不得计入撤册中启停成功（撤册中根本开不出、也收不成成功单）；
- 撤册收口失败必须整票回滚，不得留下「半截撤册中」脏态。

同舱双启互斥不在本题范围。
"""
from __future__ import annotations
import os

from app.db import tx
from app.services.common import now_iso

# 在途（未收口）启停单状态集合：历史库中的「已受理」同样视为未收口
OPEN_STATES = ("启动中", "停止中", "已受理")
RUN_OPEN_STATES = ("撤册中", "已撤册")
FAULT_TEARDOWN = "deregister_teardown"


class DomainError(Exception):
    """业务硬拒/非法状态，路由层翻译成 {错误: ...}，HTTP 仍返回 200 与既有风格一致。"""


def record_event(conn, kind: str, ref: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
        (kind, ref, detail, now_iso()),
    )


def count_open_orders(conn, pod_id: int) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM start_orders WHERE pod_id=? AND status IN ({','.join('?' * len(OPEN_STATES))})",
        (pod_id, *OPEN_STATES),
    ).fetchone()
    return int(row["c"])


def _normalize_action(action: str) -> str:
    action = (action or "").strip()
    if action in ("启动", "start", "Start"):
        return "启动"
    if action in ("停止", "stop", "Stop"):
        return "停止"
    raise DomainError(f"未知动作:{action}")


def _assert_pod_mutable(pod) -> None:
    if not pod:
        raise DomainError("舱体不存在")
    if pod["run_state"] == "撤册中":
        raise DomainError("舱体撤册中，禁止新开启停单（口径 A：撤册即封口）")
    if pod["run_state"] == "已撤册":
        raise DomainError("舱体已撤册，禁止新开启停单")


def create_order(conn, pod_id: int, action: str, target_image_id: int | None) -> dict:
    with tx(conn):
        pod = conn.execute("SELECT * FROM pods WHERE id=?", (pod_id,)).fetchone()
        _assert_pod_mutable(pod)
        act = _normalize_action(action)
        img_id = target_image_id or pod["image_id"]
        if act == "启动":
            return _create_start(conn, pod, img_id)
        return _create_stop(conn, pod)


def _create_start(conn, pod, img_id: int) -> dict:
    img = conn.execute("SELECT * FROM images WHERE id=?", (img_id,)).fetchone()
    if not img or img["pull_state"] != "已就绪":
        # 受理即失败：镜像未就绪不产生在途单
        cur = conn.execute(
            "INSERT INTO start_orders(pod_id, action, target_image_id, status, note, created_at, closed_at, close_reason, close_run_state)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (pod["id"], "启动", img_id, "失败", "镜像未就绪", now_iso(), now_iso(), "受理校验失败", pod["run_state"]),
        )
        record_event(conn, "启停单", str(cur.lastrowid), "启动 → 失败 镜像未就绪")
        return {"id": cur.lastrowid, "status": "失败", "note": "镜像未就绪"}

    q = conn.execute("SELECT * FROM quotas WHERE id=1").fetchone()
    need_cpu = float(pod["cpu_quota"])
    need_mem = int(pod["mem_quota_mb"])
    if float(q["cpu_used"]) + need_cpu > float(q["cpu_limit"]) or int(q["mem_used_mb"]) + need_mem > int(q["mem_limit_mb"]):
        cur = conn.execute(
            "INSERT INTO start_orders(pod_id, action, target_image_id, status, note, created_at, closed_at, close_reason, close_run_state)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (pod["id"], "启动", img_id, "失败", "配额不足", now_iso(), now_iso(), "受理校验失败", pod["run_state"]),
        )
        record_event(conn, "启停单", str(cur.lastrowid), "启动 → 失败 配额不足")
        return {"id": cur.lastrowid, "status": "失败", "note": "配额不足"}

    # 校验通过 → 在途启动单，不占配额、不改运行态，等收口同笔落账
    cur = conn.execute(
        "INSERT INTO start_orders(pod_id, action, target_image_id, status, note, created_at) VALUES (?,?,?,?,?,?)",
        (pod["id"], "启动", img_id, "启动中", "等待收口", now_iso()),
    )
    record_event(conn, "启停单", str(cur.lastrowid), "启动 → 启动中（在途，未占配额）")
    return {"id": cur.lastrowid, "status": "启动中", "note": "等待收口"}


def _create_stop(conn, pod) -> dict:
    cur = conn.execute(
        "INSERT INTO start_orders(pod_id, action, target_image_id, status, note, created_at) VALUES (?,?,?,?,?,?)",
        (pod["id"], "停止", pod["image_id"], "停止中", "等待收口", now_iso()),
    )
    record_event(conn, "启停单", str(cur.lastrowid), "停止 → 停止中（在途）")
    return {"id": cur.lastrowid, "status": "停止中", "note": "等待收口"}


def close_order(conn, order_id: int) -> dict:
    with tx(conn):
        order = conn.execute("SELECT * FROM start_orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise DomainError("启停单不存在")
        if order["status"] not in OPEN_STATES:
            raise DomainError(f"启停单已收口：{order['status']}")
        pod = conn.execute("SELECT * FROM pods WHERE id=?", (order["pod_id"],)).fetchone()
        if not pod:
            raise DomainError("舱体不存在")
        if pod["run_state"] in RUN_OPEN_STATES:
            # 撤册中/已撤册：在途单收口硬拒，整票失败，不留成功账
            return _fail_open_order(conn, order, pod, f"舱体{pod['run_state']}，收口硬拒（口径 A）")
        if order["action"] == "启动":
            return _close_start(conn, order, pod)
        return _close_stop(conn, order, pod)


def _fail_open_order(conn, order, pod, reason: str) -> dict:
    conn.execute(
        "UPDATE start_orders SET status=?, note=?, close_reason=?, closed_at=?, close_run_state=? WHERE id=?",
        ("失败", reason, reason, now_iso(), pod["run_state"], order["id"]),
    )
    record_event(conn, "启停单", str(order["id"]), f"{order['action']} 收口 → 失败 {reason}")
    return {"id": order["id"], "status": "失败", "note": reason}


def _close_start(conn, order, pod) -> dict:
    # 收口时再次核验配额：在途期间配额可能已被他舱占用
    q = conn.execute("SELECT * FROM quotas WHERE id=1").fetchone()
    need_cpu = float(pod["cpu_quota"])
    need_mem = int(pod["mem_quota_mb"])
    if float(q["cpu_used"]) + need_cpu > float(q["cpu_limit"]) or int(q["mem_used_mb"]) + need_mem > int(q["mem_limit_mb"]):
        return _fail_open_order(conn, order, pod, "收口时配额不足，整笔失败")
    # 盖印先于状态翻转：记录收口决策瞬间的舱体运行态，作为读侧分桶的确定依据
    decision_state = pod["run_state"]
    conn.execute("UPDATE pods SET run_state=?, image_id=? WHERE id=?", ("运行中", order["target_image_id"], pod["id"]))
    conn.execute(
        "UPDATE quotas SET cpu_used=cpu_used+?, mem_used_mb=mem_used_mb+? WHERE id=1",
        (need_cpu, need_mem),
    )
    conn.execute(
        "UPDATE start_orders SET status=?, note=?, close_reason=?, closed_at=?, close_run_state=? WHERE id=?",
        ("已完成", "启动成功", "收口成功", now_iso(), decision_state, order["id"]),
    )
    record_event(conn, "启停单", str(order["id"]), "启动 收口 → 已完成 启动成功（同笔占配额）")
    return {"id": order["id"], "status": "已完成", "note": "启动成功"}


def _close_stop(conn, order, pod) -> dict:
    decision_state = pod["run_state"]
    if pod["run_state"] == "运行中":
        conn.execute("UPDATE pods SET run_state=? WHERE id=?", ("已停止", pod["id"]))
        conn.execute(
            "UPDATE quotas SET cpu_used=MAX(0, cpu_used-?), mem_used_mb=MAX(0, mem_used_mb-?) WHERE id=1",
            (float(pod["cpu_quota"]), int(pod["mem_quota_mb"])),
        )
    conn.execute(
        "UPDATE start_orders SET status=?, note=?, close_reason=?, closed_at=?, close_run_state=? WHERE id=?",
        ("已完成", "停止成功", "收口成功", now_iso(), decision_state, order["id"]),
    )
    record_event(conn, "启停单", str(order["id"]), "停止 收口 → 已完成 停止成功")
    return {"id": order["id"], "status": "已完成", "note": "停止成功"}


def begin_deregister(conn, pod_id: int, reason: str | None) -> dict:
    with tx(conn):
        pod = conn.execute("SELECT * FROM pods WHERE id=?", (pod_id,)).fetchone()
        if not pod:
            raise DomainError("舱体不存在")
        if pod["run_state"] in RUN_OPEN_STATES:
            raise DomainError(f"舱体已在撤册流程：{pod['run_state']}")
        open_n = count_open_orders(conn, pod_id)
        if open_n > 0:
            # 口径 A 硬拒：撤册前必须收口所有未完成启停，不得带着在途单进撤册
            raise DomainError(f"存在 {open_n} 笔未收口启停单，撤册硬拒（先收口或等其失败）")
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO deregistrations(pod_id, state, reason, pre_run_state, created_at) VALUES (?,?,?,?,?)",
            (pod_id, "撤册中", reason, pod["run_state"], ts),
        )
        conn.execute(
            "UPDATE pods SET run_state=?, deregister_reason=?, deregister_at=? WHERE id=?",
            ("撤册中", reason, ts, pod_id),
        )
        record_event(conn, "撤册", str(pod_id), f"撤册受理（流水 {cur.lastrowid}）：在途清零，进入撤册中")
        return {"id": cur.lastrowid, "pod_id": pod_id, "state": "撤册中"}


def finalize_deregister(conn, pod_id: int) -> dict:
    try:
        with tx(conn):
            pod = conn.execute("SELECT * FROM pods WHERE id=?", (pod_id,)).fetchone()
            if not pod:
                raise DomainError("舱体不存在")
            if pod["run_state"] != "撤册中":
                raise DomainError(f"舱体不在撤册中：{pod['run_state']}")
            rec = conn.execute(
                "SELECT * FROM deregistrations WHERE pod_id=? AND state='撤册中' ORDER BY id DESC LIMIT 1",
                (pod_id,),
            ).fetchone()
            if not rec:
                raise DomainError("撤册流水缺失，拒绝继续下架")
            # 收口防护：撤册中不允许新在途单，这里再核一次作为不变量断言
            if count_open_orders(conn, pod_id) != 0:
                raise DomainError("撤册中出现未收口启停单，违反口径 A，中止下架")
            if rec["pre_run_state"] == "运行中":
                # 撤册必须清配额占用
                conn.execute(
                    "UPDATE quotas SET cpu_used=MAX(0, cpu_used-?), mem_used_mb=MAX(0, mem_used_mb-?) WHERE id=1",
                    (float(pod["cpu_quota"]), int(pod["mem_quota_mb"])),
                )
            conn.execute("DELETE FROM probes WHERE pod_id=?", (pod_id,))
            conn.execute("UPDATE pods SET run_state=? WHERE id=?", ("已撤册", pod_id))
            conn.execute(
                "UPDATE deregistrations SET state=?, finished_at=? WHERE id=?",
                ("已撤册", now_iso(), rec["id"]),
            )
            record_event(conn, "撤册", str(pod_id), "执行下架完成：探针已摘、配额已清、舱体已撤册")
            if os.environ.get("PODRIFT_FAULT") == FAULT_TEARDOWN:
                # 夹具用：在全部写操作之后、提交之前炸掉，验证整票回滚
                raise RuntimeError("注入故障：撤册收口阶段提交失败")
        return {"pod_id": pod_id, "state": "已撤册"}
    except DomainError:
        raise
    except Exception as exc:  # 半截脏撤册：同票回滚补偿
        _compensate_rollback(conn, pod_id, f"收口失败自动回滚：{exc}")
        raise DomainError(f"撤册收口失败，已整票回滚：{exc}")


def _compensate_rollback(conn, pod_id: int, reason: str) -> None:
    with tx(conn):
        rec = conn.execute(
            "SELECT * FROM deregistrations WHERE pod_id=? AND state='撤册中' ORDER BY id DESC LIMIT 1",
            (pod_id,),
        ).fetchone()
        if not rec:
            record_event(conn, "撤册", str(pod_id), f"回滚补偿找不到撤册流水：{reason}")
            return
        conn.execute(
            "UPDATE pods SET run_state=? WHERE id=?",
            (rec["pre_run_state"], pod_id),
        )
        conn.execute(
            "UPDATE deregistrations SET state=?, rollback_reason=?, rollback_at=? WHERE id=?",
            ("已回滚", reason, now_iso(), rec["id"]),
        )
        record_event(conn, "撤册", str(pod_id), f"整票回滚至 {rec['pre_run_state']}：{reason}")


def rollback_deregister(conn, pod_id: int, reason: str | None) -> dict:
    with tx(conn):
        pod = conn.execute("SELECT * FROM pods WHERE id=?", (pod_id,)).fetchone()
        if not pod:
            raise DomainError("舱体不存在")
        if pod["run_state"] != "撤册中":
            raise DomainError(f"舱体不在撤册中：{pod['run_state']}")
        rec = conn.execute(
            "SELECT * FROM deregistrations WHERE pod_id=? AND state='撤册中' ORDER BY id DESC LIMIT 1",
            (pod_id,),
        ).fetchone()
        if not rec:
            raise DomainError("撤册流水缺失，无法回滚")
        conn.execute("UPDATE pods SET run_state=? WHERE id=?", (rec["pre_run_state"], pod_id))
        conn.execute(
            "UPDATE deregistrations SET state=?, rollback_reason=?, rollback_at=? WHERE id=?",
            ("已回滚", reason or "手动回滚", now_iso(), rec["id"]),
        )
        record_event(conn, "撤册", str(pod_id), f"手动整票回滚至 {rec['pre_run_state']}")
        return {"pod_id": pod_id, "state": rec["pre_run_state"]}

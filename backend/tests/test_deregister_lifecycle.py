"""撤册 × 在途启停互斥夹具（口径 A）。

覆盖题面四类夹具：
1. 撤册硬拒未完成启停；
2. 撤册放行跑完（先收口再撤册的正向路径，与硬拒不同站）；
3. 撤册与开启动并发（无双结局）；
4. 半截脏撤册回滚。
外加：整票失败、读侧撤册中桶可复现且成功恒 0。
"""
from __future__ import annotations
import sqlite3
import threading
import time
from pathlib import Path

import httpx

from conftest import _port_in_use, _stop_proc, sql


def _stop_and_wait(server_info, settle: float = 1.0) -> None:
    """停掉进程并确认端口释放，避免复用同一 SQLite 库时跨进程锁/句柄竞态。"""
    _stop_proc(server_info["proc"])
    deadline = time.time() + 5
    while time.time() < deadline and _port_in_use(server_info["port"]):
        time.sleep(0.1)
    time.sleep(settle)


# ---------- 夹具 1：撤册硬拒未完成 ----------

def test_撤册硬拒在途启动单且不得带在途进撤册(client, server):
    # web-a（id=1）镜像已就绪，开一笔启动 → 在途「启动中」，不占配额
    r = client.post("/api/启停单", json={"pod_id": 1, "action": "启动"})
    assert r.json()["status"] == "启动中"
    order_id = r.json()["id"]

    # 带未收口启动单撤册 → 硬拒
    r = client.post("/api/舱体/1/撤册", json={"reason": "现场撤册"})
    assert "错误" in r.json()
    assert "未收口" in r.json()["错误"]

    # 硬拒不得留下半截「撤册中」
    pod = sql(server["db"], "SELECT run_state, deregister_reason FROM pods WHERE id=1")[0]
    assert pod["run_state"] == "已停止"
    assert pod["deregister_reason"] is None
    flows = sql(server["db"], "SELECT * FROM deregistrations WHERE pod_id=1")
    assert flows == []

    # 撤册中/已撤册都没进，在途单原封不动仍是启动中
    order = sql(server["db"], "SELECT status, closed_at FROM start_orders WHERE id=?", (order_id,))[0]
    assert order["status"] == "启动中" and order["closed_at"] is None


def test_撤册硬拒在途停止单(client, server):
    # 先把 web-a 跑起来并收口，再开停止在途单
    r = client.post("/api/启停单", json={"pod_id": 1, "action": "启动"})
    client.post(f"/api/启停单/{r.json()['id']}/收口")
    r = client.post("/api/启停单", json={"pod_id": 1, "action": "停止"})
    assert r.json()["status"] == "停止中"

    r = client.post("/api/舱体/1/撤册", json={"reason": "x"})
    assert "错误" in r.json() and "未收口" in r.json()["错误"]
    assert sql(server["db"], "SELECT run_state FROM pods WHERE id=1")[0]["run_state"] == "运行中"


# ---------- 夹具 2：撤册放行跑完（与硬拒不同站：独立舱、独立库进程） ----------

def test_收口跑完后撤册放行并清配额摘探针(client, server):
    # 启动在途 → 收口成功（同笔占配额）→ 无在途 → 撤册受理 → 执行下架
    r = client.post("/api/启停单", json={"pod_id": 1, "action": "启动"})
    oid = r.json()["id"]
    r = client.post(f"/api/启停单/{oid}/收口")
    assert r.json()["status"] == "已完成"

    quota_before = sql(server["db"], "SELECT cpu_used, mem_used_mb FROM quotas WHERE id=1")[0]
    assert quota_before["cpu_used"] > 1.2  # 已占 web-a 的 0.5c
    assert sql(server["db"], "SELECT * FROM probes WHERE pod_id=1") != []

    r = client.post("/api/舱体/1/撤册", json={"reason": "退役"})
    assert r.json().get("state") == "撤册中"

    # 撤册中开新启动/停止都必须硬拒
    r = client.post("/api/启停单", json={"pod_id": 1, "action": "启动"})
    assert "错误" in r.json() and "撤册中" in r.json()["错误"]
    r = client.post("/api/启停单", json={"pod_id": 1, "action": "停止"})
    assert "错误" in r.json()

    # 执行下架 → 已撤册，配额回退、探针摘除
    r = client.post("/api/舱体/1/执行下架")
    assert r.json().get("state") == "已撤册"
    pod = sql(server["db"], "SELECT run_state FROM pods WHERE id=1")[0]
    assert pod["run_state"] == "已撤册"
    quota_after = sql(server["db"], "SELECT cpu_used, mem_used_mb FROM quotas WHERE id=1")[0]
    assert abs(quota_after["cpu_used"] - 1.2) < 1e-9
    assert quota_after["mem_used_mb"] == 768
    assert sql(server["db"], "SELECT * FROM probes WHERE pod_id=1") == []
    flow = sql(server["db"], "SELECT state, pre_run_state FROM deregistrations WHERE pod_id=1")[0]
    assert flow["state"] == "已撤册" and flow["pre_run_state"] == "运行中"

    # 已撤册再开启停仍然硬拒
    r = client.post("/api/启停单", json={"pod_id": 1, "action": "启动"})
    assert "错误" in r.json() and "已撤册" in r.json()["错误"]


def test_撤册中在途单收口硬拒整票失败(client, server):
    # 制造在途启动单（此时还在册），再经「先撤回再撤册」无法实现——
    # 口径 A 下带在途撤册被硬拒，因此在途单只可能先于撤册受理存在；
    # 这里验证：撤册受理后，任何遗留在途单的收口都必须失败（通过直接造数据模拟时序残留）。
    r = client.post("/api/启停单", json={"pod_id": 1, "action": "启动"})
    oid = r.json()["id"]
    # 直接把舱体推进撤册中（服务端在正常路径会硬拒；此处构造读侧/防御分支所需状态）
    import sqlite3
    conn = sqlite3.connect(server["db"])
    conn.execute("UPDATE pods SET run_state='撤册中' WHERE id=1")
    conn.commit()
    conn.close()

    r = client.post(f"/api/启停单/{oid}/收口")
    assert r.json()["status"] == "失败"
    assert "撤册" in r.json()["note"]
    order = sql(server["db"], "SELECT status, close_reason FROM start_orders WHERE id=?", (oid,))[0]
    assert order["status"] == "失败" and order["close_reason"]
    # 收口失败不得占配额、不得改运行态
    pod = sql(server["db"], "SELECT run_state FROM pods WHERE id=1")[0]
    assert pod["run_state"] == "撤册中"
    assert sql(server["db"], "SELECT cpu_used FROM quotas WHERE id=1")[0]["cpu_used"] == 1.2


# ---------- 夹具 3：撤册与开启动并发，无双结局 ----------

def test_撤册与开启动并发只有单一结局(client, server):
    outcomes = []

    def open_start():
        c = httpx.Client(base_url=client.base_url, timeout=10)
        try:
            outcomes.append(("启动", c.post("/api/启停单", json={"pod_id": 1, "action": "启动"}).json()))
        finally:
            c.close()

    def deregister():
        c = httpx.Client(base_url=client.base_url, timeout=10)
        try:
            outcomes.append(("撤册", c.post("/api/舱体/1/撤册", json={"reason": "并发"}).json()))
        finally:
            c.close()

    t1, t2 = threading.Thread(target=open_start), threading.Thread(target=deregister)
    t1.start(); t2.start(); t1.join(); t2.join()

    start_res = [v for k, v in outcomes if k == "启动"][0]
    dereg_res = [v for k, v in outcomes if k == "撤册"][0]
    pod = sql(server["db"], "SELECT run_state FROM pods WHERE id=1")[0]
    open_orders = sql(
        server["db"],
        "SELECT COUNT(*) AS c FROM start_orders WHERE pod_id=1 AND status IN ('启动中','停止中','已受理')",
    )[0]["c"]

    if "state" in dereg_res:
        # 撤册赢：不得存在任何在途启停，舱体撤册中
        assert pod["run_state"] == "撤册中"
        assert open_orders == 0
        assert "错误" in start_res and "撤册中" in start_res["错误"]
    else:
        # 启动赢：撤册必须硬拒，舱体在册且恰有一笔在途
        assert "未收口" in dereg_res["错误"]
        assert pod["run_state"] == "已停止"
        assert open_orders == 1
        assert start_res["status"] == "启动中"

    # 任何结局下都不允许「撤册中 + 在途启动单」共存
    assert not (pod["run_state"] == "撤册中" and open_orders > 0)


def test_撤册受理与执行下架并发不产生双结局(client, server):
    # 无在途 → 撤册受理；受理与执行下架本身两阶段，重复受理必须被状态闸挡住
    r = client.post("/api/舱体/1/撤册", json={"reason": "x"})
    assert r.json().get("state") == "撤册中"
    r = client.post("/api/舱体/1/撤册", json={"reason": "重复"})
    assert "错误" in r.json() and "撤册流程" in r.json()["错误"]

    r = client.post("/api/舱体/1/执行下架")
    assert r.json().get("state") == "已撤册"
    # 已撤册不能再执行下架
    r = client.post("/api/舱体/1/执行下架")
    assert "错误" in r.json()
    flows = sql(server["db"], "SELECT state FROM deregistrations WHERE pod_id=1")
    assert [f["state"] for f in flows] == ["已撤册"]


# ---------- 夹具 4：半截脏撤册整票回滚 ----------

def test_撤册收口故障整票回滚不留半截(server_factory):
    # 第一进程（无故障）：跑到运行中并受理撤册
    s1 = server_factory()
    c1 = httpx.Client(base_url=s1["base"], timeout=10)
    r = c1.post("/api/启停单", json={"pod_id": 1, "action": "启动"})
    c1.post(f"/api/启停单/{r.json()['id']}/收口")
    c1.post("/api/舱体/1/撤册", json={"reason": "故障演练"})
    assert sql(s1["db"], "SELECT run_state FROM pods WHERE id=1")[0]["run_state"] == "撤册中"
    c1.close()
    _stop_and_wait(s1)

    # 第二进程带故障注入，复用同一个库：执行下架在提交前炸掉
    s2 = server_factory(fault="deregister_teardown", reset=False, db_path=Path(s1["db"]))
    c2 = httpx.Client(base_url=s2["base"], timeout=10)
    r = c2.post("/api/舱体/1/执行下架")
    assert "错误" in r.json() and "整票回滚" in r.json()["错误"]

    # 半截全部回滚：舱体恢复撤前运行态、配额不动、探针仍在、流水标已回滚
    assert sql(s2["db"], "SELECT run_state FROM pods WHERE id=1")[0]["run_state"] == "运行中"
    quota = sql(s2["db"], "SELECT cpu_used, mem_used_mb FROM quotas WHERE id=1")[0]
    assert abs(quota["cpu_used"] - 1.7) < 1e-9 and quota["mem_used_mb"] == 1024
    assert sql(s2["db"], "SELECT * FROM probes WHERE pod_id=1") != []
    flow = sql(s2["db"], "SELECT state, rollback_reason FROM deregistrations WHERE pod_id=1")[0]
    assert flow["state"] == "已回滚" and flow["rollback_reason"]

    # 回滚后舱体恢复在册，可重新受理撤册（新流水）
    r = c2.post("/api/舱体/1/撤册", json={"reason": "重试"})
    assert r.json().get("state") == "撤册中"
    c2.close()
    _stop_and_wait(s2)

    # 第三进程去掉故障注入，复用同库：重试撤册最终下架成功
    s3 = server_factory(reset=False, db_path=Path(s1["db"]))
    c3 = httpx.Client(base_url=s3["base"], timeout=10)
    r = c3.post("/api/舱体/1/执行下架")
    assert r.json().get("state") == "已撤册"
    assert sql(s3["db"], "SELECT run_state FROM pods WHERE id=1")[0]["run_state"] == "已撤册"
    # 两条流水：先回滚、后成功
    flows = [f["state"] for f in sql(s3["db"], "SELECT state FROM deregistrations WHERE pod_id=1 ORDER BY id")]
    assert flows == ["已回滚", "已撤册"]
    c3.close()


# ---------- 整票失败：在途收口失败不产生任何副作用 ----------

def test_在途启动收口失败整票撤回不占配额(client, server):
    # worker-b 镜像未就绪，受理即失败（无在途）
    r = client.post("/api/启停单", json={"pod_id": 2, "action": "启动"})
    assert r.json()["status"] == "失败" and r.json()["note"] == "镜像未就绪"
    assert sql(server["db"], "SELECT cpu_used FROM quotas WHERE id=1")[0]["cpu_used"] == 1.2

    # web-a 开在途启动，然后把配额吃满，收口必须整笔失败、不占配额不改为运行中
    r = client.post("/api/启停单", json={"pod_id": 1, "action": "启动"})
    oid = r.json()["id"]
    import sqlite3
    conn = sqlite3.connect(server["db"])
    conn.execute("UPDATE quotas SET cpu_used=cpu_limit WHERE id=1")
    conn.commit(); conn.close()

    r = client.post(f"/api/启停单/{oid}/收口")
    assert r.json()["status"] == "失败" and "配额不足" in r.json()["note"]
    pod = sql(server["db"], "SELECT run_state FROM pods WHERE id=1")[0]
    assert pod["run_state"] == "已停止"
    quota = sql(server["db"], "SELECT cpu_used FROM quotas WHERE id=1")[0]
    assert quota["cpu_used"] == 4.0  # 仍是吃满后的值，未再加


# ---------- 手动撤回撤册 ----------

def test_手动撤回撤册恢复撤前态且可再撤(client, server):
    # 先把 web-a 跑到运行中，再受理撤册 → 撤册中
    r = client.post("/api/启停单", json={"pod_id": 1, "action": "启动"})
    client.post(f"/api/启停单/{r.json()['id']}/收口")
    quota_running = sql(server["db"], "SELECT cpu_used FROM quotas WHERE id=1")[0]["cpu_used"]
    client.post("/api/舱体/1/撤册", json={"reason": "暂缓"})
    assert sql(server["db"], "SELECT run_state FROM pods WHERE id=1")[0]["run_state"] == "撤册中"

    # 手动撤回：恢复到撤前运行态，配额/探针在撤册中本就未动，应保持
    r = client.post("/api/舱体/1/撤回撤册", json={"reason": "现场暂缓"})
    assert r.json().get("state") == "运行中"
    assert sql(server["db"], "SELECT run_state FROM pods WHERE id=1")[0]["run_state"] == "运行中"
    assert sql(server["db"], "SELECT cpu_used FROM quotas WHERE id=1")[0]["cpu_used"] == quota_running
    flow = sql(server["db"], "SELECT state, rollback_reason FROM deregistrations WHERE pod_id=1")[0]
    assert flow["state"] == "已回滚" and flow["rollback_reason"] == "现场暂缓"

    # 非撤册中再撤回必须报错
    r = client.post("/api/舱体/1/撤回撤册", json={"reason": "x"})
    assert "错误" in r.json() and "不在撤册中" in r.json()["错误"]

    # 恢复在册后可正常撤册并下架
    client.post("/api/舱体/1/撤册", json={"reason": "再撤"})
    r = client.post("/api/舱体/1/执行下架")
    assert r.json().get("state") == "已撤册"


# ---------- 读侧：撤册中桶可复现，成功恒 0，硬拒/放行不同站 ----------

def test_对读屏撤册中桶成功恒零且可复现(client, server):
    # 站 1（本 server 的 web-a）：走硬拒路径，舱体始终在册，不进撤册中桶
    r = client.post("/api/启停单", json={"pod_id": 1, "action": "启动"})
    client.post("/api/舱体/1/撤册", json={"reason": "硬拒站"})
    align = client.get("/api/对读").json()
    assert align["撤册中桶"] == {"舱数": 0, "撤册中启停成功数": 0, "在途启停总数": 0}

    # 收口后放行撤册，进入撤册中桶；桶内成功数仍必须为 0
    client.post(f"/api/启停单/{r.json()['id']}/收口")
    client.post("/api/舱体/1/撤册", json={"reason": "放行站"})
    align1 = client.get("/api/对读").json()
    assert align1["撤册中桶"]["舱数"] == 1
    assert align1["撤册中桶"]["撤册中启停成功数"] == 0
    # 撤册中尝试新开启停被硬拒，不会给桶增加任何成功
    client.post("/api/启停单", json={"pod_id": 1, "action": "启动"})
    align2 = client.get("/api/对读").json()
    assert align2["撤册中桶"]["撤册中启停成功数"] == 0
    assert align2["撤册中桶"] == align1["撤册中桶"]  # 可复现：硬拒不改变桶

    # 执行下架后桶清空，已撤册舱不留在撤册中桶
    client.post("/api/舱体/1/执行下架")
    align3 = client.get("/api/对读").json()
    assert align3["撤册中桶"]["舱数"] == 0
    assert align3["撤册中桶"]["撤册中启停成功数"] == 0

import React, { useEffect, useState } from "react";
import { api } from "../api";

const OPEN = ["启动中", "停止中", "已受理"];
const LOCKED = ["撤册中", "已撤册"];

const tagClass = (s) => (s === "已完成" ? "ok" : s === "失败" ? "bad" : "warn");

export default function Orders() {
  const [pods, setPods] = useState([]);
  const [orders, setOrders] = useState([]);
  const [err, setErr] = useState("");
  const load = () =>
    Promise.all([api.pods(), api.orders()]).then(([p, o]) => {
      setPods(p);
      setOrders(o);
    });
  useEffect(() => { load(); }, []);

  const call = (p) =>
    p.then((r) => {
      setErr(r.错误 || "");
      return load();
    });

  const act = (podId, action) => call(api.createOrder({ pod_id: podId, action }));
  const close = (id) => call(api.closeOrder(id));

  return (
    <>
      <div className="hero">
        <h1>启停单</h1>
        <p>
          口径 A：启动/停止先落「在途单」（启动中/停止中），收口同笔落账；撤册中/已撤册舱体禁止新开与收口。
          镜像未就绪或配额不足受理即失败，收口时配额不足整笔失败。
        </p>
      </div>
      {err && <div className="panel banner-bad">硬拒：{err}</div>}
      <div className="panel">
        <div className="row-actions">
          {pods.map((p) => {
            const locked = LOCKED.includes(p.run_state);
            return (
              <span key={p.id} className="pod-actions">
                <strong>{p.name}</strong>{" "}
                <span className={`tag ${locked ? "bad" : "warn"}`}>{p.run_state}</span>
                {p.open_order_count > 0 && (
                  <span className="tag warn">在途 {p.open_order_count}</span>
                )}
                <button className="btn" disabled={locked} onClick={() => act(p.id, "启动")}>启动</button>
                <button className="btn" disabled={locked} onClick={() => act(p.id, "停止")}>停止</button>
              </span>
            );
          })}
        </div>
      </div>
      <div className="panel">
        <table>
          <thead>
            <tr><th>ID</th><th>舱</th><th>动作</th><th>状态</th><th>说明</th><th>收口</th><th>时间</th></tr>
          </thead>
          <tbody>
            {orders.map((o) => {
              const open = OPEN.includes(o.status);
              return (
                <tr key={o.id}>
                  <td>{o.id}</td>
                  <td>{o.pod_name}</td>
                  <td>{o.action}</td>
                  <td><span className={`tag ${tagClass(o.status)}`}>{o.status}</span></td>
                  <td>{o.note}{o.close_reason ? `（${o.close_reason}）` : ""}</td>
                  <td>
                    {open ? (
                      <button className="btn" onClick={() => close(o.id)}>收口</button>
                    ) : (o.closed_at || "—")}
                  </td>
                  <td>{o.created_at}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

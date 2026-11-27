import React, { useEffect, useState } from "react";
import { api } from "../api";

const LOCKED = ["撤册中", "已撤册"];

export default function Pods() {
  const [rows, setRows] = useState([]);
  const [flow, setFlow] = useState([]);
  const [err, setErr] = useState("");
  const load = () =>
    Promise.all([api.pods(), api.deregistrations()]).then(([p, d]) => {
      setRows(p);
      setFlow(d);
    });
  useEffect(() => { load(); }, []);

  const call = (p) =>
    p.then((r) => {
      setErr(r.错误 || "");
      return load();
    });

  const beginDeregister = (id) => call(api.beginDeregister(id, "现场撤册"));
  const finalize = (id) => call(api.finalizeDeregister(id));
  const rollback = (id) => call(api.rollbackDeregister(id, "现场撤回"));

  return (
    <>
      <div className="hero">
        <h1>舱体</h1>
        <p>
          口径 A 撤册两阶段：受理先硬拒未收口启停（有在途单即撤册失败），进入「撤册中」后禁止任何启停；
          执行下架同票清配额、摘探针，失败整票回滚。
        </p>
      </div>
      {err && <div className="panel banner-bad">硬拒：{err}</div>}
      <div className="panel">
        <table>
          <thead>
            <tr><th>舱</th><th>镜像</th><th>拉取</th><th>运行</th><th>sidecar</th><th>配额</th><th>探针</th><th>在途</th><th>撤册操作</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const locked = LOCKED.includes(r.run_state);
              return (
                <tr key={r.id}>
                  <td>{r.name}</td>
                  <td>{r.image_name}:{r.image_tag}</td>
                  <td>{r.pull_state}</td>
                  <td><span className={`tag ${r.run_state === "运行中" ? "ok" : locked ? "bad" : "warn"}`}>{r.run_state}</span></td>
                  <td>{r.sidecar || "—"}</td>
                  <td>{r.cpu_quota}c / {r.mem_quota_mb}MB</td>
                  <td><span className={`tag ${r.probe_color === "绿" ? "ok" : "bad"}`}>{r.probe_color || "已摘"} · {r.fail_streak ?? 0}</span></td>
                  <td>{r.open_order_count > 0 ? <span className="tag warn">{r.open_order_count} 笔</span> : "0"}</td>
                  <td>
                    {r.run_state === "已停止" || r.run_state === "运行中" ? (
                      <button className="btn" onClick={() => beginDeregister(r.id)}>撤册</button>
                    ) : r.run_state === "撤册中" ? (
                      <>
                        <button className="btn" onClick={() => finalize(r.id)}>执行下架</button>
                        <button className="btn" onClick={() => rollback(r.id)}>撤回撤册</button>
                      </>
                    ) : (
                      <span className="tag bad">已撤册</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2 className="panel-title">撤册流水</h2>
        <table>
          <thead>
            <tr><th>ID</th><th>舱</th><th>状态</th><th>撤前运行态</th><th>原因</th><th>回滚原因</th><th>受理</th><th>完结</th></tr>
          </thead>
          <tbody>
            {flow.map((d) => (
              <tr key={d.id}>
                <td>{d.id}</td>
                <td>{d.pod_name}</td>
                <td><span className={`tag ${d.state === "已撤册" ? "ok" : d.state === "已回滚" ? "bad" : "warn"}`}>{d.state}</span></td>
                <td>{d.pre_run_state}</td>
                <td>{d.reason || "—"}</td>
                <td>{d.rollback_reason || "—"}</td>
                <td>{d.created_at}</td>
                <td>{d.finished_at || d.rollback_at || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

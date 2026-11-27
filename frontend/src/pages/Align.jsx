import React, { useEffect, useState } from "react";
import { api } from "../api";

export default function Align() {
  const [data, setData] = useState(null);
  useEffect(() => { api.align().then(setData); }, []);
  if (!data) return null;
  const bucket = data["撤册中桶"];
  return (
    <>
      <div className="hero">
        <h1>对读屏</h1>
        <p>
          口径 A 读侧：撤册中桶单独成桶，撤册中启停成功必须恒为 0（撤册即封口，开不出也收不成成功单），
          在途启停单独计数，不得当成普通在册启停。
        </p>
      </div>
      <div className="stat-strip">
        <div className="stat">
          <div className="k">撤册中舱数</div>
          <div className="v">{bucket["舱数"]}</div>
        </div>
        <div className="stat">
          <div className="k">撤册中启停成功（须恒 0）</div>
          <div className={`v ${bucket["撤册中启停成功数"] === 0 ? "ok-num" : "bad-num"}`}>
            {bucket["撤册中启停成功数"]}
          </div>
        </div>
        <div className="stat">
          <div className="k">撤册中在途启停总数</div>
          <div className="v">{bucket["在途启停总数"]}</div>
        </div>
      </div>
      <div className="panel">
        <table>
          <thead>
            <tr><th>舱</th><th>运行</th><th>探针</th><th>镜像</th><th>在途</th><th>最近启停</th></tr>
          </thead>
          <tbody>
            {data["舱体投影"].map((r) => (
              <tr key={r.id} className={r.run_state === "撤册中" ? "row-dereg" : ""}>
                <td>{r.name}{r.deregister_reason ? `（${r.deregister_reason}）` : ""}</td>
                <td>
                  <span className={`tag ${r.run_state === "运行中" ? "ok" : r.run_state === "已撤册" || r.run_state === "撤册中" ? "bad" : "warn"}`}>
                    {r.run_state}
                  </span>
                </td>
                <td>{r.probe_color || "已摘"} · {r.fail_streak ?? 0}</td>
                <td>{r.pull_state}</td>
                <td>{r.open_order_count > 0 ? <span className="tag warn">{r.open_order_count} 笔在途</span> : "0"}</td>
                <td>{r.last_order_status || "—"} {r.last_order_note || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

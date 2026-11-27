const j = async (url, opt) => {
  const r = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opt,
  });
  return r.json();
};

export const api = {
  health: () => j("/api/健康"),
  images: () => j("/api/镜像"),
  advancePull: (id) => j(`/api/镜像/${id}/推进拉取`, { method: "POST" }),
  pods: () => j("/api/舱体"),
  orders: () => j("/api/启停单"),
  createOrder: (body) => j("/api/启停单", { method: "POST", body: JSON.stringify(body) }),
  closeOrder: (id) => j(`/api/启停单/${id}/收口`, { method: "POST" }),
  deregistrations: () => j("/api/撤册流水"),
  beginDeregister: (podId, reason) =>
    j(`/api/舱体/${podId}/撤册`, { method: "POST", body: JSON.stringify({ reason }) }),
  finalizeDeregister: (podId) => j(`/api/舱体/${podId}/执行下架`, { method: "POST" }),
  rollbackDeregister: (podId, reason) =>
    j(`/api/舱体/${podId}/撤回撤册`, { method: "POST", body: JSON.stringify({ reason }) }),
  probes: () => j("/api/探针"),
  setProbe: (podId, body) => j(`/api/探针/${podId}`, { method: "POST", body: JSON.stringify(body) }),
  quota: () => j("/api/配额"),
  policy: () => j("/api/重启策"),
  setPolicy: (body) => j("/api/重启策", { method: "POST", body: JSON.stringify(body) }),
  align: () => j("/api/对读"),
  settings: () => j("/api/舱规"),
  events: () => j("/api/事件"),
};

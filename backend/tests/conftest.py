from __future__ import annotations
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _port_in_use(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _stop_proc(proc) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _wait_healthy(proc, base: str):
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"服务进程提前退出 code={proc.returncode}：{out}")
        try:
            if httpx.get(f"{base}/api/健康", timeout=1).status_code == 200:
                return
        except Exception:
            time.sleep(0.2)
    _stop_proc(proc)
    out = proc.stdout.read() if proc.stdout else ""
    raise RuntimeError(f"服务健康检查超时：{out}")


def _reset_db(db_path: Path) -> None:
    """删库重建并播种。在独立子进程里跑应用初始化代码：

    每个子进程独立解析 PODRIFT_DB，避免 pytest 主进程 import 应用后
    app.config.DB_PATH 被首个测试缓存、后续改环境变量失效的串库问题。
    """
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            side.unlink()
    script = (
        "from app.db import connect, init_schema; "
        "from app.seed import seed_if_empty; "
        "c=connect(); init_schema(c); c.close(); seed_if_empty()"
    )
    env = {**os.environ, "PODRIFT_DB": str(db_path), "PYTHONPATH": str(BACKEND)}
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(BACKEND),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def server_factory(tmp_path):
    """工厂：可按用例需要带故障环境变量起多个独立 server 进程。"""
    procs: list[subprocess.Popen] = []

    def _make(fault: str = "", reset: bool = True, db_path: Path | None = None):
        if db_path is None:
            db_path = tmp_path / ("fault.db" if fault else "test.db")
        if reset:
            _reset_db(db_path)
        port = _free_port()
        env = {
            **os.environ,
            "PODRIFT_DB": str(db_path),
            "PYTHONPATH": str(BACKEND),
            "PODRIFT_FAULT": fault,
        }
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
            ],
            cwd=str(BACKEND),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base = f"http://127.0.0.1:{port}"
        _wait_healthy(proc, base)
        procs.append(proc)
        return {"base": base, "db": str(db_path), "proc": proc, "port": port}

    yield _make

    for proc in procs:
        _stop_proc(proc)


@pytest.fixture()
def server(server_factory):
    """起真实 uvicorn 进程，HTTP 层与并发行为与线上一致。"""
    return server_factory()


@pytest.fixture()
def client(server):
    return httpx.Client(base_url=server["base"], timeout=10)


def sql(db_path: str, query: str, params=()):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
        return rows
    finally:
        conn.close()

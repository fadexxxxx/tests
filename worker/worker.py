"""
macOS 本地 Python worker（端口 28080）

- POST /execute { taskId, name, count }
  在 ~/Desktop/test 下创建 count 个 txt 文件

运行：
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  uvicorn worker.worker:app --host 0.0.0.0 --port 28080

可选：启动后向 Railway API 一次性注册（不是轮询）
  export WORKER_LABEL="mac-1"
  export PUBLIC_URL="https://xxxx.trycloudflare.com"
  export API_REGISTER_URL="https://你的railway域名/api/workers/register"
  uvicorn worker.worker:app --host 0.0.0.0 --port 28080
"""

import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


LABEL = (os.getenv("WORKER_LABEL") or "mac-worker").strip() or "mac-worker"

app = FastAPI(title="Local Worker", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExecuteIn(BaseModel):
    taskId: Optional[str] = None
    name: str = Field(min_length=1)
    count: int = Field(ge=0)


def _safe_base_name(name: str) -> str:
    bad = ['/', "\\", ":", "*", "?", '"', "<", ">", "|"]
    s = (name or "").strip()
    for ch in bad:
        s = s.replace(ch, "_")
    return (s[:80] if s else "task")


def _ensure_folder() -> Path:
    folder = Path.home() / "Desktop" / "test"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _execute(task_id: str, name: str, count: int) -> Dict[str, Any]:
    t0 = time.perf_counter()
    folder = _ensure_folder()
    base = _safe_base_name(name)

    created = 0
    sample_files = []
    for i in range(1, count + 1):
        filename = f"{base}-{i}.txt"
        fp = folder / filename
        fp.write_text(
            "\n".join(
                [
                    f"worker={LABEL}",
                    f"taskId={task_id or '-'}",
                    f"name={name}",
                    f"index={i}",
                    f"createdAt={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
                ]
            ),
            encoding="utf-8",
        )
        created += 1
        if len(sample_files) < 5:
            sample_files.append(filename)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "ok": True,
        "worker": LABEL,
        "taskId": task_id,
        "created": created,
        "folder": str(folder),
        "sampleFiles": sample_files,
        "elapsedMs": elapsed_ms,
    }


async def _register_once() -> None:
    register_url = (os.getenv("API_REGISTER_URL") or "").strip()
    public_url = (os.getenv("PUBLIC_URL") or "").strip()
    if not register_url or not public_url:
        return

    payload = {"url": public_url, "label": LABEL}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(register_url, json=payload, timeout=10.0)
            if r.status_code // 100 != 2:
                raise RuntimeError(r.text)
        print(f"[worker] registered to api: {public_url}")
    except Exception as e:
        print(f"[worker] register failed: {e}")


@app.on_event("startup")
async def _startup() -> None:
    await _register_once()


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "label": LABEL, "time": int(time.time() * 1000)}


@app.post("/execute")
def execute(body: ExecuteIn) -> Dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    if body.count < 0:
        raise HTTPException(status_code=400, detail="count 必须 >= 0")
    return _execute(body.taskId or "", name, int(body.count))



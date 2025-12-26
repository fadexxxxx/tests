import asyncio
import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


@dataclass
class Worker:
    id: str
    label: str
    url: str  # e.g. https://xxxx.trycloudflare.com
    registered_at: float
    last_seen_at: float
    source: str  # env | register


workers: Dict[str, Worker] = {}
task_seq = 0


def _now() -> float:
    return time.time()


def _normalize_url(url: str) -> str:
    u = (url or "").strip()
    while u.endswith("/"):
        u = u[:-1]
    return u


def _parse_workers_from_env() -> List[Tuple[str, str, str]]:
    """
    WORKERS 支持：
    1) JSON 数组: [{"label":"mac-1","url":"https://..."}, ...]
    2) 逗号分隔: https://a,https://b
    返回: [(id,label,url), ...]
    """
    raw = os.getenv("WORKERS", "").strip()
    if not raw:
        return []

    # JSON
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            out: List[Tuple[str, str, str]] = []
            for i, x in enumerate(parsed):
                if not isinstance(x, dict):
                    continue
                wid = str(x.get("id") or f"env-{i+1}").strip()
                label = str(x.get("label") or x.get("name") or wid).strip()
                url = _normalize_url(str(x.get("url") or ""))
                if url:
                    out.append((wid, label, url))
            return out
    except Exception:
        pass

    # CSV
    urls = [_normalize_url(x) for x in raw.split(",")]
    urls = [u for u in urls if u]
    return [(f"env-{i+1}", f"worker-{i+1}", u) for i, u in enumerate(urls)]


def _load_workers_env_once() -> None:
    now = _now()
    for wid, label, url in _parse_workers_from_env():
        workers[wid] = Worker(
            id=wid,
            label=label,
            url=url,
            registered_at=now,
            last_seen_at=now,
            source="env",
        )


def _get_workers_list() -> List[Worker]:
    return sorted(workers.values(), key=lambda w: w.registered_at)


def _distribute_evenly(total: int, ws: List[Worker]) -> List[Tuple[Worker, int]]:
    n = len(ws)
    base = total // n
    rem = total % n
    out: List[Tuple[Worker, int]] = []
    for i, w in enumerate(ws):
        c = base + (1 if i < rem else 0)
        out.append((w, c))
    return out


class RegisterWorkerIn(BaseModel):
    url: str
    label: str = "worker"
    id: Optional[str] = None


class CreateTaskIn(BaseModel):
    name: str = Field(min_length=1)
    count: int = Field(gt=0)


app = FastAPI(title="Task Dispatch API", version="0.1.0")

origins_raw = os.getenv("CORS_ORIGINS", "*").strip()
if origins_raw == "*" or not origins_raw:
    allow_origins = ["*"]
else:
    allow_origins = [o.strip() for o in origins_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    _load_workers_env_once()


@app.get("/api/workers")
def api_workers() -> Dict[str, Any]:
    return {
        "ok": True,
        "workers": [
            {
                "id": w.id,
                "label": w.label,
                "url": w.url,
                "registeredAt": int(w.registered_at * 1000),
                "lastSeenAt": int(w.last_seen_at * 1000),
                "source": w.source,
            }
            for w in _get_workers_list()
        ],
    }


@app.post("/api/workers/register")
def api_register_worker(body: RegisterWorkerIn) -> Dict[str, Any]:
    url = _normalize_url(body.url)
    if not url:
        raise HTTPException(status_code=400, detail="缺少 url")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="url 必须以 http(s):// 开头")

    wid = (body.id or "").strip() or f"reg-{int(_now()*1000)}"
    label = (body.label or "worker").strip() or "worker"
    now = _now()
    existing = workers.get(wid)

    workers[wid] = Worker(
        id=wid,
        label=label,
        url=url,
        registered_at=existing.registered_at if existing else now,
        last_seen_at=now,
        source="register",
    )
    return {"ok": True, "worker": asdict(workers[wid])}


async def _call_worker_execute(
    client: httpx.AsyncClient,
    worker: Worker,
    payload: Dict[str, Any],
    timeout_s: float,
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{worker.url}/execute", json=payload, timeout=timeout_s)
        data = None
        try:
            data = r.json()
        except Exception:
            data = None
        ok = 200 <= r.status_code < 300
        err = None if ok else (data.get("error") if isinstance(data, dict) else f"HTTP {r.status_code}")
        return {
            "workerId": worker.id,
            "label": worker.label,
            "url": worker.url,
            "ok": ok,
            "status": r.status_code,
            "elapsedMs": int((time.perf_counter() - t0) * 1000),
            "result": data,
            "error": err,
        }
    except Exception as e:
        return {
            "workerId": worker.id,
            "label": worker.label,
            "url": worker.url,
            "ok": False,
            "status": 0,
            "elapsedMs": int((time.perf_counter() - t0) * 1000),
            "result": None,
            "error": str(getattr(e, "message", None) or e),
        }


@app.post("/api/tasks")
async def api_create_task(body: CreateTaskIn) -> Dict[str, Any]:
    global task_seq
    name = body.name.strip()
    count = int(body.count)

    ws = _get_workers_list()
    if not ws:
        raise HTTPException(
            status_code=400,
            detail="当前没有可用 worker。请先配置 WORKERS 或调用 /api/workers/register 注册 worker 公网地址。",
        )

    task_seq += 1
    task_id = f"task-{task_seq}"
    assignments = [(w, c) for (w, c) in _distribute_evenly(count, ws) if c > 0]

    timeout_s = float(os.getenv("WORKER_TIMEOUT_S", "60"))
    api_t0 = time.perf_counter()

    async with httpx.AsyncClient() as client:
        calls = []
        for w, c in assignments:
            payload = {"taskId": task_id, "name": name, "count": c}
            calls.append(_call_worker_execute(client, w, payload, timeout_s))

        per_worker = await asyncio.gather(*calls)

    now = _now()
    for r in per_worker:
        wid = r.get("workerId")
        if wid in workers:
            w = workers[wid]
            workers[wid] = Worker(
                id=w.id,
                label=w.label,
                url=w.url,
                registered_at=w.registered_at,
                last_seen_at=now,
                source=w.source,
            )

    total_elapsed_ms = int((time.perf_counter() - api_t0) * 1000)
    success = sum(1 for x in per_worker if x.get("ok"))
    failed = len(per_worker) - success
    created_total = 0
    for x in per_worker:
        if isinstance(x.get("result"), dict):
            created_total += int(x["result"].get("created") or 0)

    return {
        "ok": True,
        "taskId": task_id,
        "name": name,
        "totalCount": count,
        "availableServers": len(ws),
        "perServerAssigned": [
            {"workerId": w.id, "label": w.label, "assignedCount": c} for (w, c) in assignments
        ],
        "perWorker": [
            {
                **x,
                "assignedCount": next((c for (w, c) in assignments if w.id == x["workerId"]), 0),
            }
            for x in per_worker
        ],
        "final": {
            "successServers": success,
            "failedServers": failed,
            "createdTotal": created_total,
            "totalElapsedMs": total_elapsed_ms,
        },
    }



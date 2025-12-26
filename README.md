# 任务分发示例：GitHub Pages 前端 + Railway Python API 控制多台本地 macOS Python worker（cloudflared tunnel 映射 28080）

你要的效果：
- 前端 `web/index.html`：两个输入框（名字、数量）+ 发送按钮（托管 GitHub Pages）
- Railway 上的 **Python API**：拿到总数量后 **平均分配** 给所有已配置/已注册的 worker，并发调用每台 worker 的 `28080`（通过 cloudflared 公网 URL），最终把 **每台 worker 的耗时与汇总结果 + 总用时** 返回给前端展示（同步返回，实时交互，不轮询）
- 每台本地 macOS **Python worker**：收到任务后在桌面创建 `test` 文件夹，并在里面生成 txt 文件（文件名包含输入名字）

---

## 一、Railway 部署（Python API）

本仓库提供 FastAPI + Uvicorn，Railway 直接使用 `Procfile` 启动：
- **启动入口**：`api/main.py`
- **启动命令**：`Procfile`

Railway 环境变量（可选）：
- **`WORKERS`**：worker 列表
  - JSON 形式（推荐）：

```json
[
  { "label": "mac-1", "url": "https://xxxx.trycloudflare.com" },
  { "label": "mac-2", "url": "https://yyyy.trycloudflare.com" }
]
```

  - 或者逗号分隔 URL：
    - `https://xxxx.trycloudflare.com,https://yyyy.trycloudflare.com`

部署完成后，你会得到一个 Railway 服务域名（用于前端跨域调用）。

---

## 二、worker（macOS 本地 28080 Python 服务）

本仓库提供一个最小 worker 示例：`worker/worker.py`（FastAPI）。

在 macOS 上运行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn worker.worker:app --host 0.0.0.0 --port 28080
```

默认：
- 端口：`28080`
- 健康检查：`GET /health`
- 执行任务：`POST /execute`，body：`{ taskId, name, count }`
- 文件输出目录：`~/Desktop/test`

生成文件名规则：`{name}-{i}.txt`（避免同名覆盖）

---

## 三、cloudflared tunnel（把 28080 映射成公网 URL）

你已经开通了 tunnels，这里只说明目标：
- 让外网能访问到：`https://你的域名/execute` -> 本机 `http://localhost:28080/execute`

只要你的 tunnel 公网 URL 可访问，就把它放进 Railway 的 `WORKERS`，或在 worker 启动后向 API 注册。

---

## 四、worker 注册（可选，不属于轮询）

如果你不想在 Railway 写死 `WORKERS`，可以在每台 worker 启动时 **一次性注册**（不是轮询）：

```bash
export WORKER_LABEL="mac-mini-1"
export PUBLIC_URL="https://xxxx.trycloudflare.com"
export API_REGISTER_URL="https://你的railway域名/api/workers/register"
uvicorn worker.worker:app --host 0.0.0.0 --port 28080
```

对应 API：
- `POST /api/workers/register`
  - body：`{ "url": "https://xxxx.trycloudflare.com", "label": "mac-mini-1" }`

查看当前 worker：
- `GET /api/workers`

---

## 五、前端使用

把 `web/index.html` 托管到 GitHub Pages 后，通过 URL 参数指定 Railway API：

- 方式 A（推荐）：访问 GitHub Pages 时加参数：
  - `?api=https://你的railway域名`
- 方式 B：直接编辑 `web/index.html` 里的默认值，把 `https://YOUR-RAILWAY-DOMAIN` 改成你的域名
  - 你当前示例域名：`https://testss.up.railway.app`

然后：
- 输入 **名字**（用于 txt 文件名）
- 输入 **数量**（总任务数）
- 点击 **发送任务**

结果面板会显示：
- `任务N 已创建/已完成`
- **可用服务器数量**
- **每台服务器分配数量**
- **每台服务器用时与汇总结果**
- **总创建文件数 / 总用时**



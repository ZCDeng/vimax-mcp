---
title: "feat: API+CLI 接入层（保留 MCP 作备用）"
type: feat
status: active
created: 2026-05-19
depth: standard
target_repo: vimax-mcp
origin:
  - docs/MCP_PROPOSAL.md (vimax-mcp 内部，原 MCP 提案)
  - ~/projects/html-anything/docs/solutions/agent-tool-architecture-api-mcp-cli.md (经验文档：API+CLI 实证)
---

# feat: API+CLI 接入层（保留 MCP 作备用）

**Target repo:** `vimax-mcp`（即 `~/projects/vimax-mcp/`）。所有 repo-relative 路径以此为根。

## Summary

把 `vimax-mcp` 的 agent 接入层从 MCP-only 改成 **REST API + `vimax` CLI 薄壳为主，MCP 保留为按需 enable 的备用通道**。内核（`ServerContext` / `JobRegistry` / `QuotaTracker` / `runner` / `artifacts`）零改动；只在 transport 层新增 REST router，并提供一个 shell-wrapper CLI（仿 `html-anything/convert.py` 模式）。launchd daemon 形态保持不变——同一个进程、同一个端口（7801），同时挂 REST 路由和（可选关闭的）MCP SSE。

---

## Problem Frame

当前 `vimax-mcp` 形态：单进程 FastMCP daemon（stdio + SSE 双 transport）→ 6 个 MCP tool → ServerContext。已通过 launchd 常驻 127.0.0.1:7801。

按 `agent-tool-architecture-api-mcp-cli.md` 的经验框架（HTML-anything 实证 + 12.3 万次 MCP 调用统计），ViMax 的特征对照：
- ✅ 后端是 warm daemon、有全局 quota state、多客户端 → API 内核做对了
- ❌ 输出不是 typed 复杂结构（mp4 路径 + 简单 JSON）→ MCP 无优势
- ❌ 不是多步交互（一次 submit 后是黑盒后台跑）→ MCP 无优势
- ❌ vimax 在该机本身被列为"低频 <1000 调用" → MCP schema 常驻是永久 token 税

实证对照：HTML-anything 是同形态（daemon + 大计算 + 多客户端），最终 reject MCP，走 API + CLI 薄壳。vimax-mcp 走同样的路径。

**用户决策点（已通过 ce-plan Phase 0.7 确认）：**
- MCP 通道命运 = (b) 双协议并存，MCP 转为"按需 enable"备用
- CLI 分发形态 = (b) shell 脚本 wrapper 在 `~/.local/bin/vimax`，调 `uv run --directory ...`

---

## Scope

### In scope

- 新增 REST API：mirror 现有 6 个 MCP tool 的功能（submit_idea2video / submit_script2video / get_job_status / list_artifacts / cancel_job / get_quota）
- 单进程同时挂载 REST + MCP SSE（共用一个 ServerContext 实例，共享 quota / job registry）
- `vimax` CLI 实现：subcommand 形式（`vimax submit-idea`、`vimax status` 等）+ `--json` 输出开关
- shell-wrapper 安装脚本：把 `~/.local/bin/vimax` symlink 或 wrapper 装上，背后 `uv run --directory ~/projects/vimax-mcp`
- README + clients 文档更新：把 REST+CLI 列为推荐路径，MCP 配置注释为"可选 / 按需"
- launchd plist 必要的最小修改（如 entrypoint 改名 / 添加 health 检查命令）

### Out of scope

- 不动 `ServerContext` / `JobRegistry` / `QuotaTracker` / `runner` / `artifacts` 的业务逻辑
- 不动 ViMax 主仓 pipeline、配置、限速器
- 不加 Web UI / dashboard
- 不加 auth / 多用户隔离
- 不重写 quota 估算逻辑

### Deferred to Follow-Up Work

- 把 vimax-mcp repo 改名到 `vimax-server` 或 `vimax`：等本计划落地稳定运行 ≥1 周再考虑，避免一次性切断 git history + launchd label
- CLI 加 `vimax find --idea "..."` 模糊搜索历史 job：用户没有强需求时不做
- REST 加 SSE 流式 progress 推送：当前轮询足够，等 CLI 用户体验真的痛了再加
- launchd label 改名 `com.zcdeng.vimax`（去 mcp 后缀）：跟仓库改名一起做

---

## Key Technical Decisions

### KD1. REST 框架选 Starlette，不引入 FastAPI

- **Why**: FastMCP 已经依赖 Starlette；pydantic 已经在依赖列表里。6 个端点的简单 JSON in/out 不需要 FastAPI 的 dependency injection / OpenAPI 自动生成 / pydantic v2 response model 等重型功能
- **Trade-off**: 失去自动 OpenAPI 文档。可接受——agent 通过 CLI 接入，不直接读 OpenAPI；本地开发用 `curl` 就够
- **Alternative considered**: FastAPI——更熟悉但多一层抽象、多约 5 个间接依赖
- **Alternative considered**: 不引入框架，手撕 BaseHTTPRequestHandler——太裸，JSON 解析 / 错误处理 / async 都要自己写

### KD2. 同进程双协议：Starlette parent app 挂 MCP SSE + REST router

- **Why**: 共享 `ServerContext` 单例 = 共享 quota / job registry，不需要 IPC。launchd 只管一个进程
- **How**: FastMCP 暴露 `.sse_app()` 返回 Starlette app，挂在 `/mcp` 前缀；REST router 挂在 `/api/v1` 前缀。用 uvicorn 直接跑 parent
- **Trade-off**: FastMCP 版本耦合——`.sse_app()` API 在新版本可能变。要 pin `mcp>=1.2.0,<2.0`，并在 `tests/test_rest_boot.py` 验证两个 mount 都活
- **Alternative considered**: REST 跑在 7802 second daemon——简单但 ServerContext 要做 IPC / 共享文件锁，复杂度大于收益

### KD3. CLI 用 httpx 同步 client，输出默认人类可读 + `--json`

- **Why**: httpx 是 mcp 生态常用 client，已经被间接拉进来（mcp client 本身用 httpx）；同步模式让 CLI 代码简单，不引入 asyncio
- **Output 默认人类可读**：例如 `vimax status <id>` 输出多行 key:value + 高亮 state；agent 用 `--json` flag 拿结构化数据
- **Alternative considered**: 用 stdlib `urllib.request`——零依赖，但 streaming/超时/错误处理代码 30+ 行
- **Alternative considered**: 全程 JSON 输出——agent 友好但人类不友好；用 flag 切换最便宜

### KD4. CLI 分发：shell wrapper + `uv run --directory`，不打包

- **Why**: 与 html-anything 的 `convert.py` 模式一致——零打包步骤、改代码即生效、`uv run` 解决环境隔离
- **How**: `scripts/vimax` 是 6 行 bash，exec 到 `uv run --directory <repo> python -m vimax_mcp.cli "$@"`。`scripts/install-cli.sh` 把它 symlink 到 `~/.local/bin/vimax`
- **Trade-off**: 启动延迟 ~0.5-1s（uv resolve + python import）。可接受——vimax 命令不在 hot path
- **Alternative considered**: `uv tool install`——要求所有用户机器装 uv globally；当前唯一用户（本机）已经有 uv，但 shell wrapper 仍是更显式的"调本仓代码"语义
- **Alternative considered**: PyInstaller 打成 binary——过度工程，单用户本机用

### KD5. MCP 通道默认安装文档移除，保留代码与 client 模板作"按需 enable"

- **Why**: 经验文档第 4 节"低频 MCP 改 `claude mcp remove` + 用时再 add"。代码保留成本接近零（已存在），收益是 Codex 老版本 stdio-only 时仍能用
- **How**: `clients/claude-code.mcp.json` 加注释说明"opt-in only"；README 把 MCP 章节降级到"高级 / 兼容场景"小节；新增 `clients/claude-code.settings.json` 片段，演示 `Bash(vimax:*)` 授权
- **不删 MCP tool decorators**：`server.py` 的 `@mcp.tool()` 全部保留——retire 一个能跑的 transport 不值得

---

## Output Structure

新增 / 修改的文件层级（不展开未改动文件）：

```
vimax-mcp/
├── src/vimax_mcp/
│   ├── server.py           # 修改：main() 用 Starlette parent app；保留 stdio 模式
│   ├── rest.py             # 新：REST router + handlers
│   ├── cli.py              # 新：argparse + httpx 的 CLI 入口
│   └── ... (jobs/quota/runner/artifacts/dotenv 不动)
├── scripts/
│   ├── vimax               # 新：shell wrapper，6 行 bash
│   ├── install-cli.sh      # 新：把 vimax wrapper symlink 到 ~/.local/bin
│   └── install-launchd.sh  # 修改：verify 命令换成 REST /api/v1/health
├── launchd/
│   └── com.zcdeng.vimax-mcp.plist  # 可能修改：--transport sse 改成 --transport both
├── clients/
│   ├── claude-code.mcp.json        # 修改：加注释 opt-in
│   ├── claude-code.settings.json   # 新：Bash(vimax:*) 授权片段（推荐路径）
│   └── codex.config.toml           # 修改：注明 MCP stdio 为兼容兜底
├── tests/
│   ├── test_rest.py            # 新：REST handlers 单元测试
│   ├── test_rest_boot.py       # 新：REST + MCP SSE 同进程绑定 smoke
│   ├── test_cli.py             # 新：CLI subcommand parsing + 输出格式
│   └── test_cli_wrapper.py     # 新：install-cli.sh 幂等性 + 符号链接正确
├── pyproject.toml              # 修改：加 httpx + starlette + uvicorn 依赖；加 vimax CLI script
└── README.md                   # 修改：API+CLI 列为推荐，MCP 降级
```

---

## High-Level Technical Design

REST endpoint 设计（**directional guidance, not implementation specification**）：

```
POST   /api/v1/jobs/idea2video
       body: {idea, user_requirement, style, profile?, job_id?}
       resp: {job_id, working_dir, state}
       error 429: {error: "quota_exhausted", retry_after_seconds, ...}

POST   /api/v1/jobs/script2video
       body: {script, user_requirement, style, profile?, job_id?}
       resp: {job_id, working_dir, state}

GET    /api/v1/jobs/{job_id}
       resp: {job_id, kind, state, progress, errors, final_video, working_dir, ...}
       404 if not found

GET    /api/v1/jobs/{job_id}/artifacts?kind=all|final|frames|intermediate
       resp: {job_id, artifacts: [{path, kind, size, mtime}, ...]}

POST   /api/v1/jobs/{job_id}/cancel
       resp: {ok, cancelled_at, job_id}

GET    /api/v1/quota
       resp: {chat: {used, limit}, image: {...}, video: {...}, day: "2026-05-19"}

GET    /api/v1/health
       resp: {status: "ok", started_at, version}
```

CLI subcommand 形态（directional）：

```
vimax submit-idea --idea "..." [--style "..."] [--user-requirement "..."] [--profile default] [--job-id <id>]
vimax submit-script --script "@path/to/script.txt" [...]   # @prefix = 读文件
vimax status <job_id>
vimax artifacts <job_id> [--kind final|frames|intermediate|all]
vimax cancel <job_id>
vimax quota
vimax health                # 检查 daemon 是否活着
# 全局 flag: --json （结构化输出）/ --server http://... （默认 localhost:7801）
```

进程结构（directional）：

```
launchd (com.zcdeng.vimax-mcp)
  └── uvicorn (vimax_mcp.server:app)            # parent Starlette
        ├── mount /mcp        → FastMCP.sse_app()  # MCP 备用通道
        ├── mount /api/v1     → vimax_mcp.rest.router  # REST 主通道
        └── shared ServerContext singleton      # jobs / quota / runner
```

---

## Implementation Units

### U1. REST router + handlers（无 transport mount）

**Goal:** 实现 6 个 REST endpoint，全部走现有 `ServerContext` 实例，零业务逻辑改动。

**Requirements:** 镜像 MCP_PROPOSAL §2.2 + §4.3 定义的 6 个 tool 功能。

**Dependencies:** 无（基础单元）。

**Files:**
- `src/vimax_mcp/rest.py` (新)
- `tests/test_rest.py` (新)
- `pyproject.toml` (加 `starlette>=0.37`, `uvicorn[standard]>=0.30` 到 dependencies)

**Approach:**
- 模块顶部接收一个 `ServerContext` 实例（依赖注入），不再访问全局 `_ctx`
- 每个 endpoint 是 async function，参数从 Starlette `Request` 解析 JSON
- 错误格式统一 `{error: str, ...detail}`；QuotaExhausted → 429；KeyError → 404；invalid kind → 400
- 暴露一个 `build_router(ctx: ServerContext) -> Starlette` 工厂，供 server.py mount

**Patterns to follow:**
- 镜像 `src/vimax_mcp/server.py` 152-260 的 6 个 `@mcp.tool` 函数体——同样的输入输出契约
- 错误 dict 形式参照现有 `QuotaExhausted.to_dict()`

**Test scenarios:**
- happy path: POST /jobs/idea2video with valid body → 200 + `{job_id, working_dir, state: "queued"}`
- happy path: GET /jobs/{id} for known job → 200 + 含 `progress.current_stage`
- happy path: GET /jobs/{id}/artifacts?kind=final → 只返回 final_video 一项
- happy path: POST /jobs/{id}/cancel → 200 + `{ok: true}`，job state 变 cancelled
- happy path: GET /quota → 200 + 三 provider 当日使用量
- happy path: GET /health → 200 + `{status: "ok"}`
- error: GET /jobs/nonexistent → 404 + `{error: "job nonexistent not found"}`
- error: POST /jobs/idea2video 触发 QuotaExhausted → 429 + `{error: "quota_exhausted", ...}`
- error: GET /jobs/{id}/artifacts?kind=bogus → 400 + `{error: "invalid kind: bogus"}`
- edge: POST with malformed JSON body → 400（Starlette 默认行为，确认不挂）
- edge: POST /jobs/idea2video 缺少 idea 字段 → 400

**Verification:** 所有 6 个 endpoint 在 `tests/test_rest.py` 用 `httpx.AsyncClient(app=...)` 直接打通；用 fake `ServerContext`（tmp_path + monkeypatch runner）避免触发真 pipeline。

---

### U2. Composite server entrypoint（REST + MCP SSE 同进程）

**Goal:** `vimax_mcp.server:main()` 启动一个 Starlette parent app，挂载 REST router (`/api/v1`) + 可选 MCP SSE (`/mcp`)；用 uvicorn 服务。

**Requirements:** 双 transport 共享 ServerContext 单例；MCP 可通过 flag 禁用以节省启动时间。

**Dependencies:** U1。

**Files:**
- `src/vimax_mcp/server.py` (修改：`main()` + 新增 `build_app(ctx)` 工厂；保留 `mcp = FastMCP(...)` 和所有 `@mcp.tool()` 装饰器)
- `tests/test_rest_boot.py` (新)
- `tests/test_sse_boot.py` (修改：调整为新启动方式)

**Approach:**
- 新增 `--transport` 选项值：`stdio`（原 MCP stdio）、`http`（仅 REST）、`both`（REST + MCP SSE，默认）
- `build_app(ctx)`: 创建 Starlette parent，挂载 `rest.build_router(ctx)` 到 `/api/v1`；当 transport 含 sse 时挂 `mcp.sse_app()` 到 `/mcp`
- 用 `uvicorn.run(app, host=..., port=...)` 启动（替代当前的 `mcp.run(transport="sse")`）
- `stdio` 模式保持原行为（`mcp.run()`），不走 uvicorn
- ServerContext 在 main() 里创建一次，注入到 router 和 mcp 工具（mcp 工具用闭包或全局，保留现有 `_ctx_or_die()` 模式）

**Patterns to follow:**
- 现有 `src/vimax_mcp/server.py:263-298` 的 argparse + transport 分支
- Starlette `Mount` 文档：`Starlette(routes=[Mount("/api/v1", routes=...), Mount("/mcp", app=mcp.sse_app())])`

**Test scenarios:**
- happy path: `--transport both --port <random>` → 等到 `/api/v1/health` 和 `/mcp/sse` 都 TCP 可达
- happy path: `--transport http` → `/api/v1/health` 可达，`/mcp/sse` 返回 404
- happy path: `--transport stdio` → 不绑端口，按原 stdio JSON-RPC 跑（现有 `test_stdio_handshake.py` 继续通过）
- edge: 同时启两个 `both` 进程绑同 port → 第二个失败（OSError），用 `test_sse_boot.py` 现有的 `_pick_port` 避开
- regression: 现有 `test_sse_boot.py` 仍然 pass（用新 default `both`）

**Verification:** `tests/test_rest_boot.py` subprocess 起 `--transport both`，curl `/api/v1/health` 和 `/mcp/sse` 都返回非错误状态码；clean SIGTERM。

---

### U3. `vimax` CLI（httpx + argparse）

**Goal:** Python CLI 模块 `vimax_mcp.cli`，subcommand 风格，调本地 REST，输出默认人类可读 + `--json`。

**Requirements:** 覆盖 6 个 endpoint + health；agent 通过 `Bash(vimax:*)` 授权即可全功能使用。

**Dependencies:** U1（REST 已可用）。

**Files:**
- `src/vimax_mcp/cli.py` (新)
- `tests/test_cli.py` (新)
- `pyproject.toml` (加 `httpx>=0.27` 依赖；加 `[project.scripts]` 的 `vimax = "vimax_mcp.cli:main"`)

**Approach:**
- `argparse` subparsers：submit-idea / submit-script / status / artifacts / cancel / quota / health
- 全局 flags：`--server URL`（默认 `http://127.0.0.1:7801`）、`--json`（结构化）、`--timeout SECONDS`（默认 30）
- `submit-script` 支持 `--script @path` 语法，读文件填 body
- 输出 formatter：人类可读时高亮 state 颜色（用 ANSI codes，TTY-only；非 TTY 自动关）；`--json` 直出 server JSON 到 stdout
- 错误 → stderr + 非零退出码（404=2, 4xx=3, 5xx=4, network=5）

**Patterns to follow:**
- `~/projects/html-anything/skills/html-beautify/assets/convert.py`（用户已熟悉的 CLI 薄壳）的 argparse + httpx 写法
- 退出码语义参考 sysexits.h

**Test scenarios:**
- happy path: `vimax submit-idea --idea "foo" --json` → stdout 是合法 JSON 含 `job_id` (mock httpx response)
- happy path: `vimax status <id>` → stdout 含 `state:` 行 + working_dir
- happy path: `vimax submit-script --script @/tmp/script.txt` → POST body 含文件内容
- happy path: `vimax quota --json` → JSON 输出
- error: server 不可达 → stderr "cannot reach daemon at ..."，退出码 5
- error: server 返回 404 → stderr "job not found"，退出码 2
- error: server 返回 429 quota_exhausted → stderr "quota exhausted, retry after ..."，退出码 3
- edge: `--script @missing.txt` → stderr file-not-found，退出码 66 (input)
- edge: 非 TTY 输出无 ANSI 颜色（pipe 到 file，验证字节里无 `\x1b`）
- edge: `vimax` 无子命令 → 打印 help + 退出码 0（argparse 默认行为）

**Verification:** 单测覆盖所有 subcommand 解析 + 输出格式；端到端冒烟靠 U6 launchd 启动后手跑一次。

---

### U4. Shell wrapper + 安装脚本

**Goal:** `scripts/vimax` 是可执行 shell 文件；`scripts/install-cli.sh` 把它装到 `~/.local/bin/vimax`（symlink 或 copy）。

**Requirements:** 用户在任意目录敲 `vimax ...` 都能跑；卸载干净；不破坏已有 `~/.local/bin/vimax`（若来自他源）。

**Dependencies:** U3。

**Files:**
- `scripts/vimax` (新，shebang + exec 到 `uv run --directory $REPO python -m vimax_mcp.cli "$@"`)
- `scripts/install-cli.sh` (新，install / status / remove 子命令，仿 `scripts/install-launchd.sh` 风格)
- `tests/test_cli_wrapper.py` (新)

**Approach:**
- wrapper 内容：
  ```bash
  #!/usr/bin/env bash
  exec uv run --directory "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" \
       python -m vimax_mcp.cli "$@"
  ```
- `install-cli.sh install`：
  - 检查 `command -v uv`
  - 创建 `~/.local/bin`（如不存在）
  - 若 `~/.local/bin/vimax` 已存在：用 `readlink` 判断是否指向本仓的 `scripts/vimax`，是则跳过；否则报警让用户手动处理（不覆盖）
  - 创建 symlink
  - 打印 PATH 提示（如 `~/.local/bin` 不在 PATH）
- `install-cli.sh status`：show `which vimax` + `vimax health` 输出
- `install-cli.sh remove`：仅删指向本仓的 symlink

**Patterns to follow:**
- `scripts/install-launchd.sh` 的 cmd_install / cmd_status / cmd_remove 三段结构

**Test scenarios:**
- happy path: 在干净 tmp HOME 跑 `install-cli.sh install` → `$HOME/.local/bin/vimax` 是 symlink 指向 `scripts/vimax`
- happy path: 第二次 install (幂等) → 不报错、不重复
- happy path: `install-cli.sh remove` → symlink 被删
- edge: `$HOME/.local/bin/vimax` 已存在且来自他源 → 不覆盖，stderr 警告 + 非零退出
- edge: 没装 uv → 报错退出
- edge: 测试用 fake `PATH=` 不含 `~/.local/bin` → install 后打印提示

**Verification:** `tests/test_cli_wrapper.py` 用 tmp_path 当 fake HOME，subprocess 跑 install-cli.sh 验证文件状态。手动验证：`install-cli.sh install` 后任意目录敲 `vimax health` 成功。

---

### U5. README + clients 文档重构

**Goal:** README 顶部叙事变成 "REST + CLI 是主接入路径，MCP 是可选兼容通道"。client 模板加注释明示 opt-in 语义。新增 `clients/claude-code.settings.json` 演示 `Bash(vimax:*)` 授权片段。

**Requirements:** 任何新用户读 README 走推荐路径都能 5 分钟内跑通；MCP 配置仍然完整可复制（兼容场景）。

**Dependencies:** U1-U4 全部完成（文档要反映落地形态）。

**Files:**
- `README.md` (修改)
- `clients/claude-code.mcp.json` (修改：加 "opt-in only" 顶部注释)
- `clients/claude-code.settings.json` (新：`{"permissions": {"allow": ["Bash(vimax:*)"]}}` 片段 + 一段说明)
- `clients/codex.config.toml` (修改：保留 stdio 配置，注释顶部加"当 Codex 不支持 HTTP 调用时使用"）

**Approach:**
- README 顶部结构：`## Quickstart (recommended: REST + CLI)` → `## Wire to Claude Code (Bash permission)` → `## Wire to Codex (Bash or MCP stdio fallback)` → `## Advanced: enable MCP transport`
- 删掉 README 中"SSE (shared instance, recommended for multi-CLI)"的"recommended"措辞；改成"Available for MCP-only clients"
- "Why not MCP" 加一段（≤5 行）链回 `docs/MCP_PROPOSAL.md` 和经验文档

**Patterns to follow:** 当前 README 的表格 / 代码块风格

**Test expectation:** none -- 纯文档，无可执行 assertion；通过 U6 的人工验证 (`./scripts/install-cli.sh install && vimax health`) 间接验证文档命令可跑通

**Verification:** 手读 README 一遍，按 Quickstart 步骤在干净环境跑通；`clients/*.json` 全部 `jq .` 合法。

---

### U6. launchd 兼容 + health 验证 + 集成冒烟

**Goal:** launchd 启动新 composite server 后 `/api/v1/health` 可达；`install-launchd.sh status` 的 verify 命令换成 REST。端到端跑一次：launchd 起、`vimax health` 通、`vimax quota` 返回数字。

**Requirements:** 部署形态零回归；现有 SSE client 配置（用户 `~/.claude/.mcp.json`）仍能用（因为 MCP 通道保留）。

**Dependencies:** U1-U5。

**Files:**
- `launchd/com.zcdeng.vimax-mcp.plist` (修改：`--transport sse` → `--transport both`)
- `scripts/install-launchd.sh` (修改：cmd_install 末尾的 verify 命令换成 `curl -s http://127.0.0.1:7801/api/v1/health`；cmd_status 加 `vimax quota` 输出)
- `tests/test_launchd_template.py` (修改：assert 新 transport 参数)

**Approach:**
- plist 改完用 `plutil -lint` 验证（脚本里已有）
- install-launchd.sh 已经是 idempotent，re-run 自动 bootout + bootstrap
- 文档强调："存量用户升级路径 = `git pull && ./scripts/install-launchd.sh install && ./scripts/install-cli.sh install`"

**Patterns to follow:**
- 现有 `install-launchd.sh` 的 ensure_prereqs / render_plist / bootstrap_agent 流程

**Test scenarios:**
- happy path: `test_launchd_template.py` 验证 plist 渲染后含 `--transport` `both` 而非 `sse`
- happy path: plist `plutil -lint` 通过
- edge: 老版本 plist 装机的存量用户重跑 install-launchd.sh → bootout 旧 label + bootstrap 新 plist，无残留

**Verification（人工冒烟，不进 pytest）:**
1. `git pull && uv sync`
2. `./scripts/install-launchd.sh install`
3. `./scripts/install-cli.sh install`
4. `curl -s http://127.0.0.1:7801/api/v1/health` → `{"status":"ok",...}`
5. `vimax health` → "ok"
6. `vimax quota` → 三 provider 当日 used/limit
7. `curl -sI http://127.0.0.1:7801/mcp/sse | head -1` → 200（MCP 仍可达，证明双协议生效）
8. tail logs 看无 ERROR

---

## System-Wide Impact

| 受影响方 | 影响 | 缓解 |
|---|---|---|
| 现有 MCP 客户端配置（`~/.claude/.mcp.json` 指 `:7801/sse`）| URL 变成 `:7801/mcp/sse`（多了 `/mcp` 前缀）| README 升级章节明示新 URL；用户改一行 JSON |
| launchd 进程 | entrypoint 不变（仍是 `python -m vimax_mcp.server`）；transport flag 变 | install-launchd.sh re-run 自动覆盖 |
| Claude Code 全局 settings | 推荐新增 `Bash(vimax:*)` 允许 | clients/claude-code.settings.json 片段示范 |
| Codex 用户 | 优先用 `Bash(vimax:*)`；老版本 Codex 不支持时回落 MCP stdio | codex.config.toml 模板保留 stdio 配置 |
| 全局 token 预算 | MCP 不默认 enable → 每会话省 ~80-400 tokens schema | 这是本次工作的核心收益 |

---

## Risks & Mitigations

| 风险 | 影响 | 缓解 |
|---|---|---|
| FastMCP `.sse_app()` API 在 `mcp>=1.2.0` 之外的版本签名变化 | composite server boot 失败 | pyproject 加 `mcp>=1.2.0,<2.0` 上限；`test_rest_boot.py` 守住回归 |
| 用户既有 `~/.claude/.mcp.json` 指 `:7801/sse`，升级后路径错位 | MCP 客户端 connection fail | README 升级章节明示要改成 `:7801/mcp/sse`；可选：在 server.py 加 `/sse` → `/mcp/sse` 的 308 重定向（看 FastMCP 内部路由能否容忍） |
| `~/.local/bin` 不在用户 PATH | `vimax` 命令找不到 | install-cli.sh install 时检测并提示 `export PATH=$HOME/.local/bin:$PATH` |
| Codex stable 不能调 HTTP CLI（沙盒禁 net） | Codex 用户必须留 MCP stdio 通道 | 不动 MCP；codex.config.toml 模板继续提供 stdio fallback |
| `uv run --directory` 第一次执行触发 sync，延迟 5-10s | CLI 首次启动慢，用户疑惑 | install-cli.sh install 末尾跑一次 `vimax health` 预热 |
| 同时启 stdio + both 模式互相冲突 | 罕见，但 argparse 没禁 | argparse 加 mutually-exclusive 校验：stdio 不能跟 host/port 同时给 |

---

## Verification Strategy

**自动化（pytest）：**
- 单元：`test_rest.py`、`test_cli.py`、`test_cli_wrapper.py` — 所有 endpoint + subcommand + 错误路径
- 集成：`test_rest_boot.py` — composite server 起得来，两个 mount 都活
- 回归：`test_sse_boot.py`、`test_stdio_handshake.py`、`test_server_registration.py`、`test_launchd_template.py` 全部继续通过（保护 MCP 通道未被回归打坏）

**人工冒烟（U6 verification 7 步）：**
- 装 launchd + CLI
- `curl /api/v1/health`、`vimax health`、`vimax quota`、`curl /mcp/sse`
- 实际跑一次最小 idea2video（≤2 shot）端到端，验证 `vimax submit-idea` → `vimax status` → `vimax artifacts --kind final` 链路

**性能 / token 节省（非阻塞验证）：**
- 升级后开新 Claude Code 会话，对比 system-reminder token 数（不装 vimax MCP）相比升级前节省的 schema 量；记录到 `docs/notes/` 或追加到经验文档

---

## Open Questions

仅运行时可知 / 实现时再决策：

1. **FastMCP `.sse_app()` 的精确签名** — `mcp>=1.2.0` 实际暴露什么属性需要在 U2 实现时 `python -c "from mcp.server.fastmcp import FastMCP; help(FastMCP)"` 确认；如不是 `.sse_app()` 而是 `.streamable_http_app()` 之类，对 mount 路径无影响只改一行
2. **`/sse` → `/mcp/sse` 重定向是否值得做** — 取决于用户现存 `.mcp.json` 数量；本机只有 ~1-2 处用到，手改更省事；如发现广泛使用再补 308 redirect
3. **CLI 颜色输出是否引入 `rich`** — 仅当人类可读 formatter 真的需要表格 / 进度条时考虑；初版 ANSI codes 手写足够，避免依赖
4. **stdio 模式是否仍要保留** — Codex 用户若全部升级到支持 HTTP 的版本，stdio mode 可在 U6 后某个 follow-up 单独删；本计划保留以避免回归

---

## References

- `docs/MCP_PROPOSAL.md` — 原 MCP 提案 + POC 实测数据（B.8 节 `<think>` wrapper 实测）
- `~/projects/html-anything/docs/solutions/agent-tool-architecture-api-mcp-cli.md` — API vs MCP vs CLI 经验框架 + html-anything 实证
- `src/vimax_mcp/server.py:152-260` — 6 个 MCP tool 现有实现（U1 镜像目标）
- `~/projects/html-anything/skills/html-beautify/assets/convert.py` — CLI 薄壳模式范例
- `scripts/install-launchd.sh` — install/status/remove 三段脚本范式（U4 install-cli.sh 仿照）

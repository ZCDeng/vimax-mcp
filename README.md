# vimax-mcp

MCP server wrapping [ViMax](https://github.com/HKUDS/ViMax) for use across multiple CLI clients (Claude Code, Codex, Gemini, Kimi, …).

Implements proposal §1 of `~/projects/ViMax/docs/MCP_PROPOSAL.md`.

## Status

Steps 1-3 done: 6 tools, stdio + SSE transports, daily-quota gate,
launchd agent template, multi-CLI configuration snippets.

## Tools

| Tool | Purpose |
|---|---|
| `submit_idea2video` | Kick off idea → video. Returns `job_id` immediately. Rejects with `quota_exhausted` if today's video budget is gone. |
| `submit_script2video` | Same, but starting from a screenplay. |
| `get_job_status` | State + progress (inferred from working_dir contents) + errors. |
| `list_artifacts` | List job output files, filterable by `final` / `frames` / `intermediate` / `all`. |
| `cancel_job` | Stop a running or queued job; working_dir preserved. |
| `get_quota` | Today's used / limit for chat / image / video (UTC day, resets at midnight UTC). |

## Requirements

- Python 3.12+, `uv`
- A ViMax checkout at `$VIMAX_HOME` (defaults to `~/projects/ViMax`)
- ViMax already wired with `MINIMAX_API_KEY` and `GOOGLE_API_KEY` env vars (see ViMax fork's `.env.example`)

## Run

```bash
cd ~/projects/vimax-mcp
uv sync

# stdio (default — used when wired into a CLI client)
uv run python -m vimax_mcp.server

# SSE (HTTP, share across all CLI clients on this machine)
uv run python -m vimax_mcp.server --transport sse --port 7801
```

## Wire to Claude Code

**stdio (per-CLI subprocess)** — `~/.claude/.mcp.json` or project `.mcp.json`:

```json
{
  "mcpServers": {
    "vimax": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/zcdeng/projects/vimax-mcp",
               "python", "-m", "vimax_mcp.server"]
    }
  }
}
```

**SSE (shared instance, recommended for multi-CLI)** — run `vimax-mcp --transport sse` once (e.g. via launchd in step 3), then point each client at it:

```json
{
  "mcpServers": {
    "vimax": {
      "transport": "sse",
      "url": "http://127.0.0.1:7801/sse"
    }
  }
}
```

## Environment

| Var | Default | Purpose |
|---|---|---|
| `VIMAX_HOME` | `~/projects/ViMax` | Path to ViMax checkout (added to `sys.path` at first job) |
| `VIMAX_JOBS_DIR` | `$VIMAX_HOME/.working_dir/jobs` | Per-job output root |
| `VIMAX_QUOTA_FILE` | `$VIMAX_HOME/.working_dir/quota.json` | Persisted daily-quota counter |
| `VIMAX_MCP_TRANSPORT` | `stdio` | `stdio` or `sse` (CLI flag overrides) |
| `VIMAX_MCP_HOST` | `127.0.0.1` | SSE bind host |
| `VIMAX_MCP_PORT` | `7801` | SSE bind port |
| `VIMAX_MCP_LOG` | `INFO` | Log level |
| `MINIMAX_API_KEY` | — | Forwarded to ViMax chat model |
| `GOOGLE_API_KEY` | — | Forwarded to ViMax image/video generators |

## Deploy as a launchd agent (recommended on macOS)

```bash
./scripts/install-launchd.sh         # install or refresh
./scripts/install-launchd.sh status  # show launchctl print + log tails
./scripts/install-launchd.sh remove  # uninstall
```

The template at `launchd/com.zcdeng.vimax-mcp.plist` is rendered into
`~/Library/LaunchAgents/com.zcdeng.vimax-mcp.plist` with your `$HOME`
and absolute `uv` path substituted. The agent runs `vimax-mcp
--transport sse --port 7801` and restarts on crash.

Secrets are not stored in the plist. The server loads
`$VIMAX_HOME/.env` on boot (see `vimax_mcp/dotenv.py`).

Verify:

```bash
curl -sI http://127.0.0.1:7801/sse | head -1   # → 200 OK
tail -f ~/projects/ViMax/.working_dir/logs/mcp.{out,err}.log
```

## Wire into your CLI clients

Ready-to-copy snippets live in `clients/`:

| File | Target |
|---|---|
| `clients/claude-code.mcp.json` | `~/.claude/.mcp.json` or project `.mcp.json` — works for every Claude Code-compatible CLI (Claude Code, the 6 forks, Gemini/Kimi via the symlink trick). |
| `clients/codex.config.toml` | Append to `~/.codex/config.toml`. Codex stable does not speak SSE; the snippet spawns a stdio subprocess instead. |

## Tests

```bash
uv run pytest
```

Smoke tests cover JobRegistry, artifact scanning, FastMCP tool registration, and a full stdio JSON-RPC handshake. They do **not** invoke ViMax pipelines or consume Veo / MiniMax quota.

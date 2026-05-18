# vimax-mcp

MCP server wrapping [ViMax](https://github.com/HKUDS/ViMax) for use across multiple CLI clients (Claude Code, Codex, Gemini, Kimi, …).

Implements proposal §1 of `~/projects/ViMax/docs/MCP_PROPOSAL.md`.

## Status

Step 1 of the rollout: 5 tools, stdio transport, in-memory + filesystem JobRegistry. HTTP/SSE and launchd integration come in steps 2-3.

## Tools

| Tool | Purpose |
|---|---|
| `submit_idea2video` | Kick off idea → video. Returns `job_id` immediately. |
| `submit_script2video` | Same, but starting from a screenplay. |
| `get_job_status` | State + progress (inferred from working_dir contents) + errors. |
| `list_artifacts` | List job output files, filterable by `final` / `frames` / `intermediate` / `all`. |
| `cancel_job` | Stop a running or queued job; working_dir preserved. |

## Requirements

- Python 3.12+, `uv`
- A ViMax checkout at `$VIMAX_HOME` (defaults to `~/projects/ViMax`)
- ViMax already wired with `MINIMAX_API_KEY` and `GOOGLE_API_KEY` env vars (see ViMax fork's `.env.example`)

## Run as stdio server

```bash
cd ~/projects/vimax-mcp
uv sync
uv run python -m vimax_mcp.server
```

## Wire to Claude Code (project-local `.mcp.json` or global config)

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

## Environment

| Var | Default | Purpose |
|---|---|---|
| `VIMAX_HOME` | `~/projects/ViMax` | Path to ViMax checkout (added to `sys.path` at first job) |
| `VIMAX_JOBS_DIR` | `$VIMAX_HOME/.working_dir/jobs` | Per-job output root |
| `VIMAX_MCP_LOG` | `INFO` | Log level |
| `MINIMAX_API_KEY` | — | Forwarded to ViMax chat model |
| `GOOGLE_API_KEY` | — | Forwarded to ViMax image/video generators |

## Tests

```bash
uv run pytest
```

Smoke tests cover JobRegistry, artifact scanning, FastMCP tool registration, and a full stdio JSON-RPC handshake. They do **not** invoke ViMax pipelines or consume Veo / MiniMax quota.

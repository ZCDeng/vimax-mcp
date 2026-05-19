# vimax-mcp

Daemon + CLI wrapping [ViMax](https://github.com/HKUDS/ViMax) for use across multiple AI coding agents (Claude Code, Codex, Gemini, Kimi, …).

Primary path: **REST API + `vimax` CLI authorized via `Bash(vimax:*)`**.
MCP transport still ships in the same process for hosts that can only speak MCP — see [Advanced: MCP transport](#advanced-mcp-transport).

## Why REST + CLI

`docs/plans/2026-05-19-001-feat-api-cli-transport-plan.md` walks through the migration. Short version:

- ViMax jobs are coarse (one `submit` returns a `job_id`, then poll). MCP's typed-schema strength is wasted on a "submit and poll" surface.
- An always-on MCP server prepays ~80–400 tokens of schema per agent session for a tool used <1000 times in local history. That's a permanent token tax.
- A `vimax` shell command can be reproduced from any terminal, debugged with `curl`, and shared across hosts that don't speak MCP.

Background: `docs/MCP_PROPOSAL.md` (the original MCP design + POC data) and `~/projects/html-anything/docs/solutions/agent-tool-architecture-api-mcp-cli.md` (the API+CLI empirical case study this repo follows).

## Tools

| Tool | Purpose |
|---|---|
| `vimax submit-idea --idea ...` | Kick off idea → video. Returns `job_id` immediately. Rejects with `quota_exhausted` if today's video budget is gone. |
| `vimax submit-script --script ...` | Same, but starting from a screenplay. `@path/to/file` loads from disk. |
| `vimax status <job_id>` | State + progress (inferred from working_dir contents) + errors. |
| `vimax artifacts <job_id> --kind final\|frames\|intermediate\|all` | List job output files. |
| `vimax cancel <job_id>` | Stop a running or queued job; working_dir preserved. |
| `vimax quota` | Today's used / limit per provider (UTC day, resets at midnight UTC). |
| `vimax health` | Daemon liveness probe. |

Global flags: `--server URL` (default `http://127.0.0.1:7801`), `--json` for structured output, `--timeout` seconds.

REST endpoints under `http://127.0.0.1:7801/api/v1/` mirror these one-to-one (e.g. `POST /jobs/idea2video`, `GET /jobs/{job_id}`, `GET /quota`, `GET /health`). The CLI is a thin wrapper — `curl` or `httpie` can drive the same flows.

## Requirements

- Python 3.12+, `uv`
- A ViMax checkout at `$VIMAX_HOME` (defaults to `~/projects/ViMax`)
- ViMax already wired with `MINIMAX_API_KEY` and `GOOGLE_API_KEY` env vars (see ViMax fork's `.env.example`)

## Quickstart

```bash
cd ~/projects/vimax-mcp
uv sync

# 1. Run the daemon (REST + MCP SSE on 127.0.0.1:7801).
uv run python -m vimax_mcp.server
# In production, install as a launchd agent — see "Deploy as a launchd agent".

# 2. Install the vimax CLI symlink into ~/.local/bin.
./scripts/install-cli.sh
# Make sure ~/.local/bin is on PATH (the script warns if it isn't).

# 3. Smoke test.
vimax health
vimax quota
```

Once the daemon is up and `vimax` is on PATH, the rest is shell:

```bash
vimax submit-idea --idea "a cat on a roof at sunset" --style "Studio Ghibli, warm"
# → job_id: 01HZ8XKQM2A...
vimax status 01HZ8XKQM2A
vimax artifacts 01HZ8XKQM2A --kind final
```

## Wire into agents — Claude Code

Drop `clients/claude-code.settings.json` into `~/.claude/settings.json` (or your project's `.claude/settings.json`):

```json
{
  "permissions": {
    "allow": ["Bash(vimax:*)"]
  }
}
```

That's it. The agent can call any `vimax ...` subcommand without prompting. Output is human-readable by default; agents typically prefer `vimax --json status <id>` for structured parsing.

## Wire into agents — Codex

If your Codex build supports running shell commands, allow `vimax` and skip the MCP block entirely.

If you need MCP fallback, append `clients/codex.config.toml` (or the inline snippet below) to `~/.codex/config.toml`:

```toml
[mcp_servers.vimax]
command = "uv"
args = [
  "run",
  "--directory",
  "/Users/zcdeng/projects/vimax-mcp",
  "python",
  "-m",
  "vimax_mcp.server",
  "--transport",
  "stdio",
]
```

Stdio bridging spins up a fresh process per Codex session, so the daemon-side quota / job registry is **not** shared. Use it for ad-hoc debugging, not concurrent multi-client work.

## Wire into agents — Gemini, Kimi, etc.

These tend to inherit Claude Code or Codex config via symlinks. Whichever host shape they wrap, the recommendation is the same: prefer `Bash(vimax:*)` over MCP.

## Environment

| Var | Default | Purpose |
|---|---|---|
| `VIMAX_HOME` | `~/projects/ViMax` | Path to ViMax checkout (added to `sys.path` at first job) |
| `VIMAX_JOBS_DIR` | `$VIMAX_HOME/.working_dir/jobs` | Per-job output root |
| `VIMAX_QUOTA_FILE` | `$VIMAX_HOME/.working_dir/quota.json` | Persisted daily-quota counter |
| `VIMAX_MCP_TRANSPORT` | `both` | `stdio` (MCP only), `http` (REST only), or `both` (default) |
| `VIMAX_MCP_HOST` | `127.0.0.1` | HTTP bind host |
| `VIMAX_MCP_PORT` | `7801` | HTTP bind port |
| `VIMAX_MCP_LOG` | `INFO` | Daemon log level |
| `VIMAX_SERVER` | `http://127.0.0.1:7801` | CLI default daemon URL |
| `VIMAX_CLI_TIMEOUT` | `30` | CLI request timeout (seconds) |
| `VIMAX_BIN_DIR` | `$HOME/.local/bin` | Where `install-cli.sh` puts the `vimax` symlink |
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
and absolute `uv` path substituted. The agent runs `vimax-mcp` in
composite mode (REST at `/api/v1` + MCP SSE at `/mcp/sse`) and
restarts on crash.

Secrets are not stored in the plist. The server loads
`$VIMAX_HOME/.env` on boot (see `vimax_mcp/dotenv.py`).

Verify:

```bash
curl -s http://127.0.0.1:7801/api/v1/health     # → {"status":"ok",...}
vimax health
vimax quota
tail -f ~/projects/ViMax/.working_dir/logs/mcp.{out,err}.log
```

## Advanced: MCP transport

The daemon still exposes MCP over SSE at `http://127.0.0.1:7801/mcp/sse`
and over stdio (run with `--transport stdio`). Use these when:

- An agent host can only speak MCP and can't shell out (rare).
- You want to compare behavior between transports for debugging.
- You're migrating from a pre-U2 deployment and need a temporary bridge.

Client templates live in `clients/`:

| File | Where to drop it | When to use |
|---|---|---|
| `clients/claude-code.settings.json` | `~/.claude/settings.json` | **Recommended** — Bash permission for `vimax` CLI |
| `clients/claude-code.mcp.json` | `~/.claude/.mcp.json` | Opt-in MCP SSE for hosts that need it |
| `clients/codex.config.toml` | append to `~/.codex/config.toml` | MCP stdio fallback for older Codex |

If you were on a pre-U2 deployment whose `.mcp.json` pointed at
`http://127.0.0.1:7801/sse`, update the URL to
`http://127.0.0.1:7801/mcp/sse` — the composite server moved MCP under
a `/mcp` prefix so REST can own the root.

## Tests

```bash
uv run pytest
```

Smoke tests cover JobRegistry, quota tracker, artifact scanning, REST
handlers, composite-server boot, the CLI parser + output formatter, the
shell wrapper + install script, FastMCP tool registration, SSE boot,
and a full stdio JSON-RPC handshake. They do **not** invoke ViMax
pipelines or consume Veo / MiniMax quota.

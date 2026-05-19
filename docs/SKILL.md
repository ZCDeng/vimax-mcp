---
name: vimax-cli
description: Submit, monitor, and harvest artifacts from ViMax video-generation jobs via a local REST daemon. Use when the user asks to make a short video from an idea or a script, when they need to check on a running job, or when they want to download / locate the final mp4 of a finished job. Authorize with the Bash(vimax:*) permission and prefer `--json` for any programmatic use.
---

# vimax CLI

Thin shell over the `vimax-mcp` daemon (REST API on 127.0.0.1:7801 by default). Wraps the ViMax pipeline (HKUDS): chat agents + Nano Banana image gen + Google Veo / MiniMax video gen.

A single submit kicks off a job that runs for **10–40 minutes**. The CLI never blocks for that long — you submit, get a `job_id`, then poll with `vimax status` or `vimax status <id> --watch`.

## Prerequisites

- Daemon running at `http://127.0.0.1:7801` (the recommended deploy uses launchd; see `README.md`).
- `vimax` on `$PATH` — `~/projects/vimax-mcp/scripts/install-cli.sh` symlinks it into `~/.local/bin`.
- For Claude Code / Codex hosts: allow `Bash(vimax:*)` in `~/.claude/settings.json` (or your host's equivalent).

## Subcommands

| Command | Purpose | Typical exit code |
|---|---|---|
| `vimax health` | Probe daemon liveness. | 0 ok / 5 unreachable |
| `vimax quota` | Today's used / limit per provider (chat, image, video). | 0 |
| `vimax submit-idea --idea "..." [--style "..."] [--user-requirement "..."]` | Kick off idea → video. Returns `job_id` immediately. | 0 / 3 quota_exhausted |
| `vimax submit-script --script "@path/to/file"` | Same, but starting from a screenplay. `@path` reads from disk. | 0 / 3 quota_exhausted |
| `vimax list [--limit N] [--state ...] [--kind ...]` | Recent jobs, newest first. Use when the user can't remember a `job_id`. | 0 |
| `vimax status <job_id> [--watch [INTERVAL]]` | State + progress + errors. `--watch` polls until the job reaches a terminal state. | 0 / 2 not_found |
| `vimax artifacts <job_id> [--kind final\|frames\|intermediate\|all]` | List output files with paths + sizes. The final mp4 has kind `final`. | 0 / 2 not_found |
| `vimax cancel <job_id>` | Stop a queued or running job; the working_dir is preserved. | 0 / 2 not_found |

Global flags (accepted in either position — before or after the subcommand):

- `--json` — emit structured JSON instead of human text. **Always use this when parsing the output.**
- `--server URL` — point at a non-default daemon (env: `VIMAX_SERVER`).
- `--timeout SECONDS` — request timeout (env: `VIMAX_CLI_TIMEOUT`, default 30).

Both `vimax --json status <id>` and `vimax status <id> --json` are valid.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 2 | not found (404) — usually a wrong `job_id` |
| 3 | client-side error (4xx incl. `quota_exhausted`) |
| 4 | server-side error (5xx) |
| 5 | cannot reach the daemon (connection refused, timeout) |
| 66 | invalid user input (e.g. `--script @missing.txt`) |

## Typical flows

### Submit a video then collect it

```bash
vimax submit-idea --idea "a cat on a roof at sunset" --style "Studio Ghibli, warm" --json
# → {"job_id":"01J...","working_dir":"/.../jobs/01J...","state":"queued"}

vimax status 01J... --watch 30 --json
# polls every 30 s until state in {done, failed, cancelled}

vimax artifacts 01J... --kind final --json
# → {"job_id":"...","artifacts":[{"path":".../final_video.mp4","kind":"final","size":...}]}
```

### Find a job you forgot the id of

```bash
vimax list --limit 10 --json
# → [{"job_id":"...","kind":"...","state":"...","submitted_at":"...","idea":"a cat..."}]
```

### Check quota before submitting

```bash
vimax quota --json
# → {"date":"2026-05-19","chat":{"used_today":12,"limit":100}, "video":{"used_today":7,"limit":10}, ...}
# If video.used_today is close to limit, do not submit — the daemon will reject with exit 3.
```

## For AI Agents

These are non-obvious rules. Read them before driving the CLI programmatically.

1. **Always pass `--json`.** Human-readable output uses ANSI codes when stdout is a TTY; structure can change. JSON output is the contract.
2. **Check exit codes, not stdout.** A 404 prints to stderr and exits 2; a quota_exhausted prints to stderr and exits 3. Do not assume "ran without crashing" means "did what I wanted".
3. **`vimax status <id> --watch` blocks for tens of minutes.** Only use it from foreground tasks. For background polling, prefer a loop that calls `vimax status <id> --json` and sleeps.
4. **A single job consumes 2N-1 Veo calls for N shots** (per `docs/MCP_PROPOSAL.md` POC). Before submitting, call `vimax quota --json` and refuse to submit if `video.limit - video.used_today < 2*expected_shots`.
5. **`@file` syntax for `--idea` / `--script` requires an absolute path.** Relative paths break under launchd because the daemon's CWD is the repo root, not yours.
6. **Never re-submit a non-retriable failure.** If `vimax status <id> --json` shows an error with `"retriable": false` (e.g. Google Veo content filter), resubmitting with the same `job_id` fails fast. Pick a new `job_id` and adjust the prompt.
7. **The working_dir survives failure and cancellation.** Pull intermediate artifacts via `vimax artifacts <id> --kind intermediate` for debugging before deciding to retry.
8. **Quota is global per-day across all callers.** If you're running parallel agents, coordinate or you'll thrash 429s and waste tokens on retries.
9. **Stderr is informational, stdout is data.** When `--json` is on and exit code is 0, stdout is parseable JSON. Do not parse stderr.
10. **MCP transport still exists** at `http://127.0.0.1:7801/mcp/sse` but is opt-in. Do not enable it for routine work — it adds ~80–400 tokens of schema to every session. Use the CLI.

## Related files

- `README.md` — Quickstart for humans, launchd install, env vars.
- `docs/MCP_PROPOSAL.md` — Original MCP design + POC measurements (35 min baseline, `<think>` wrapper, quota math).
- `docs/plans/2026-05-19-001-feat-api-cli-transport-plan.md` — Migration plan from MCP-only to REST+CLI.
- `clients/claude-code.settings.json` — Drop-in `Bash(vimax:*)` permission for Claude Code.

## Version

vimax-mcp 0.1.0 (this skill tracks the package version).

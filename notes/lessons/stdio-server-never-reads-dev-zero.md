Never feed `/dev/zero` to the stdio MCP server to keep it alive in a manual test; use `sleep infinity |` or `subprocess.PIPE`.

`server.py` reads JSON-RPC with `readline()`. `/dev/zero` never yields a newline, so the process grew to 14 GB in about a second and the kernel OOM killer fired (2026-09-27), taking the surrounding Claude Code session down with it. `/dev/null` is also wrong for a lifetime test: it gives EOF at once and the server exits cleanly before any signal arrives.

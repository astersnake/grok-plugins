Grok stops a stdio MCP server with SIGKILL to its whole process group, so no signal handler or `finally` in the server runs.

Grok 1.0.41 spawns MCP servers through `process-wrap` with `kill_on_drop`. With the 0.1.0 plugin every `/quit` left `~/.claude/sessions/<pid>.json`, its key and its socket behind, and Claude kept listing a dead peer until the next Grok start cleaned it up. A SIGTERM handler did not help. What works is a janitor: a `sh` started with `start_new_session=True`, outside the killed group, that blocks on a pipe whose only writer is the server and removes the files at EOF.

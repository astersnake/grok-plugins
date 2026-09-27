A Grok MCP server can read the session's live permission mode from `$GROK_HOME/sessions/*/$GROK_SESSION_ID/events.jsonl`: every `turn_started` event carries `yolo_mode` (true in always-approve).

Grok passes `GROK_SESSION_ID` to stdio MCP servers, and a tool call always runs inside a turn, so the last `turn_started` is current. The plugin needs this because Claude Code holds a peer message whose `from-mode` differs from its own class. Hard-coding `prompting` got every Grok message held by bypass-mode Claude sessions. Letting the model choose the mode would let it claim `bypass` falsely.

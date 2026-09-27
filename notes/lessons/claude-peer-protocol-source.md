Check claude-messaging against the JS embedded in the local Claude Code binary, not against assumptions.

The binary (`~/.local/share/claude/versions/<version>`) carries each bundled chunk as plain JS after a `// @bun @bytecode` header. Split the file on that header and search the chunks for `peer_message_status`, `cross-session-message`, `messagingSocketPath`, or `peerToken`. Exported names are aliased, so find a helper through the `export{...}` list of the chunk that defines it.

Why it mattered: reading 2.1.283 this way found bugs a spec-only review would miss:
- Claude adds `hop-chain` when it replies to a peer message.
- Claude sends no receipt when it accepts a message. `delivered` means a held message was released.
- `refused` travels as `expired` plus `status_detail: "refused"`.
- `finished_at` is epoch milliseconds.
- Grok's MCP client (rmcp) uses newline-delimited JSON on stdio, not `Content-Length` framing.

To check envelope parity, paste the extracted escaper and serializer into a Node script and compare its output with `protocol.py` on generated inputs.

Two more things found in 2.1.283 the same way:
- Inbound parity: search for `mode-mismatch`. With a `from-mode`, Claude accepts only when it equals its own class (`bypass` or `prompting`). Without one, a bypassing session holds and a prompting one accepts. An unknown own mode holds.
- The footer Claude puts on a peer message: search for `permission laundering`. The plugin's MCP instructions adapt it, so recheck them when Claude changes that text.

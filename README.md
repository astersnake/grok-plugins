# grok-plugins

Plugins I wrote for Grok. MIT licensed.

## claude-messaging

Lets Claude Code and Grok message each other on the same machine. A Grok session shows up in Claude Code's `/list-agents`, so Claude can hand it work (I use it to send QA passes to Grok) and Grok answers back. It speaks Claude Code's own [cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging) protocol.

Setup, how it works, and its limits: [plugins/claude-messaging/README.md](plugins/claude-messaging/README.md).

```bash
grok plugin marketplace add astersnake/grok-plugins
grok plugin install claude-messaging --trust
```

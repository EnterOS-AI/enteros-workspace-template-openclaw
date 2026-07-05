# TOOLS

## Built-in

- Bash — shell commands
- Read/Write/Edit — file operations
- Glob/Grep — search

## MCP Servers

- `molecule` — Molecule platform A2A tools (peer discovery, task
  delegation, workspace info, canvas messaging, memory). Registered by
  the adapter at setup time and served over HTTP transport on
  127.0.0.1:9100. Provides `list_peers`, `get_workspace_info`,
  `delegate_task`, `delegate_task_async`, `check_task_status`,
  `send_message_to_user`, `commit_memory`, `recall_memory`, and the
  inbox tools.

  NOTE: to see platform peers (other workspaces), use the `molecule`
  server's `list_peers` — NOT OpenClaw's native `sessions_list`, which
  only lists this runtime's own OpenClaw sessions.

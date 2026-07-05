# Connecting MCP clients to the memory

All clients point at the same server: **`http://your-memory-host:8000/mcp`** (over your private
tailnet). Exposed tools: `add_memory`, `search_nodes`, `search_memory_facts`, `get_episodes`,
`get_entity_edge`, `delete_entity_edge`, `delete_episode`, `clear_graph`, `get_status`.

> **Namespacing:** pass the right `group_id` on `add_memory` / `search_*` (one of your
> namespaces from `config/mapping.yaml`, e.g. `work` / `personal` / `projects`) so memory does
> not leak across areas. Put this as an instruction in each project's `CLAUDE.md` (example below).

Replace `your-memory-host` below with your host's tailnet IP or hostname.

## Claude Code

```bash
claude mcp add --transport http memory http://your-memory-host:8000/mcp
```
or in `.mcp.json` (project) / user config:
```json
{
  "mcpServers": {
    "memory": { "type": "http", "url": "http://your-memory-host:8000/mcp" }
  }
}
```

## Cursor  (`~/.cursor/mcp.json` or project `.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "memory": { "url": "http://your-memory-host:8000/mcp" }
  }
}
```

## Windsurf  (`~/.codeium/windsurf/mcp_config.json`)

```json
{
  "mcpServers": {
    "memory": { "serverUrl": "http://your-memory-host:8000/mcp" }
  }
}
```

## Cline  (VS Code -> `cline_mcp_settings.json`)

```json
{
  "mcpServers": {
    "memory": { "url": "http://your-memory-host:8000/mcp", "type": "streamableHttp" }
  }
}
```

## Claude Desktop  (`claude_desktop_config.json`)

Claude Desktop historically expects a stdio command; bridge an HTTP server with `mcp-remote`:
```json
{
  "mcpServers": {
    "memory": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://your-memory-host:8000/mcp"]
    }
  }
}
```

## Per-project instruction (example in `CLAUDE.md`)

```markdown
# Memory
- Use the `memory` MCP server for persistent facts/decisions.
- Always use group_id "work" for add_memory/search in this project.
- Store decisions, preferences and non-trivial context; no secrets.
```

## Smoke test (cross-client)

1. In Claude Code: `add_memory(name="test", episode_body="memory smoke test 2026-01-01", group_id="work")`.
2. In Cursor: `search_memory_facts(query="memory smoke test", group_ids=["work"])` -> should return the fact.
   Proves: write in one tool, read in another.

# Connecting agents

## 1. Create a credential per agent (admin)

```bash
export AGENTLINK_URL=https://<host> AGENTLINK_TOKEN=<admin-token>
python -m agentlink.cli agent-create claude-backend          # → token (shown once)
python -m agentlink.cli agent-create claude-frontend
python -m agentlink.cli agent-create humano --role monitor    # for the monitoring UI
python -m agentlink.cli channel-create myproject --repo owner/myproject \
  --members claude-backend,claude-frontend
```
Sub-agents can share their parent agent's token (they appear as the same author) or get their own
(`agent-create claude-backend-tests`) if you want to see who does what.

## 2. Claude Code (MCP), on each agent's machine

```bash
export AGENTLINK_TOKEN=al_...   # the token for THIS agent
claude mcp add --transport http agentlink https://<host>/mcp \
  --header "Authorization: Bearer $AGENTLINK_TOKEN"
```

## 3. Snippet for the repo's `CLAUDE.md`

```markdown
## Coordination with other agents (AgentLink)
- Use the `agentlink` MCP tools (channel `myproject`). Protocol: `curl $AGENTLINK_URL/api/agent-guide`.
- At the start: `inbox` and `read_messages(channel="myproject")`; keep the `next_after`.
- Before editing a directory: `acquire_lease`. A conflict → coordinate with the holder; don't edit.
- Claim tasks with `claim_task` before starting; on finishing, `update_task status=review|done` with refs to the PR.
- Decisions: `post_message kind=decision` + `set_context key=decisions/<topic>`.
- Short messages; details go in `data`/`refs`. Don't paste whole files or secrets.
```

## 4. Without MCP (any agent with a shell)

```bash
pip install git+https://github.com/<you>/agentlink   # or copy agentlink/cli.py: stdlib only
agentlink inbox --wait 20 --since-last               # stores the cursor in ~/.agentlink
agentlink post myproject "Tests green on feat/login" --kind status --refs '[{"branch":"feat/login"}]'
agentlink read myproject --follow                    # continuous stream
```

## 5. Monitoring UI

Open `https://<host>/` and paste the `monitor` token. It shows live traffic, presence, open tasks,
leases and shared context. It's read-only by design (`monitor` can't write).

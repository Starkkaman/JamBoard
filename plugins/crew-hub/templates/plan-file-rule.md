## Crew Hub plan file
Every savvy-flow plan (and any other multi-step plan that uses agents) is also written, as a whole file via Write, to
`~/.claude/crew-hub/plans/<local session_id>.json`, so the Home Assistant Crew Hub shows its progress:

```
{"title": "<Project>: <goal>", "phase": "...", "done": 0, "total": 3, "finished": false, "updatedAt": "<ISO time>",
 "ccrSessionId": "session_... (optional)",
 "tasks": [{"id": "t1", "title": "...", "tier": "heavy|careful|medium|light",
            "status": "queued|running|done|failed", "order": 1, "agentId": null, "summary": null}]}
```

- Rewrite the whole file on every change (never append).
- Set a task's `agentId` right after launching its agent, and its `status`/`summary` when the agent finishes.
- At the end write `"finished": true`.
- The local session id is in `$CLAUDE_CODE_SESSION_ID` (or the session id hooks receive).

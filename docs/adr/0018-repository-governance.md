---
status: accepted
date: 2026-09-28
deciders: repository owner (human), drafted by Claude Code
---

# 0018: Repository governance, agent rules and document precedence

## Decision Outcome

- **Binding documents and precedence** (highest first):
  1. decided ADRs, newest wins
  2. `docs/architecture.md`
  3. `docs/ENGINEERING.md` and `docs/THREAT_MODEL.md`
  4. `PLAN.md`

  When two of them conflict, the higher one wins, and the conflict is fixed in the next PR. ADRs refer to living documents rather than copying their details.
- **`AGENTS.md`** is the vendor-neutral instruction file for AI agents. `CLAUDE.md` contains only `@AGENTS.md`.
- **Merging:** only the human merges, or explicitly tells an agent to merge. Agents act with the owner's GitHub credentials, so this is a **procedural** rule, not a technical one (user decision, 2026-09-27; THREAT_MODEL T-605).
- **Commit signing is not required** (user decision, 2026-09-27). Agents would need the owner's key, so signatures couldn't tell agent commits from human ones. GitHub signs its merge commits. If releases are ever published for other users, release tags will be signed (T-606).
- **Reviews:** AI review output and the triage decisions are posted as PR comments.

### Consequences

- Good: clear rules for agents, and a tiebreak between documents.
- Bad: an agent with the owner's token could technically merge or change settings; only the procedure prevents it.

## References

- AGENTS.md; ENGINEERING §2.7, §6, §7; THREAT_MODEL T-605, T-606

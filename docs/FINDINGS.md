# TradeOS Findings Ledger

Append-only. Never edit a prior entry. A finding later proved wrong gets a NEW
entry saying so.

Every number carries the command that produced it and the raw output. A number
without a command behind it does not belong here. "Could not determine" is a
required section of every entry.

Format (from `docs/TRADEOS_ROADMAP.md`):

```markdown
## <date> — Stage <n> — <one-line result>

**Ran:** <exact commands>
**Raw output:** <pasted, or path to the file it was written to>
**Found:** <what the numbers say>
**Could not determine:** <what failed, what was missing, what n was too small>
**Recommends:** <action, or "no action">
**Gate:** PASS / BLOCKED / NEEDS DECISION
```

---

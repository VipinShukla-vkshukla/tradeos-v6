
# TradeOS - AI Development Instructions

## Project understanding
- This is an existing automated trading system.
- Understand the current architecture before making changes.
- Treat the actual source code as the source of truth.
- Do not rely on outdated documentation when it conflicts with source code.

## Development rules
- Make minimal, targeted changes.
- Preserve existing architecture and working functionality.
- Do not rewrite modules unnecessarily.
- Follow existing coding conventions and patterns.
- Explain the purpose of changes before implementing complex modifications.
- Check dependencies and integration impacts before changing interfaces.

## Trading safety
- Never place live orders or initiate live trading.
- Never execute the production trading workflow.
- Never change DRY_RUN, capital allocation, risk limits,
  broker settings, or production configuration without my
  explicit approval.
- Use mocks, simulations and test data for validation.
- Never use real broker credentials or expose API keys,
  passwords, tokens or secrets.
- Do not modify .env files or secret files.

## Testing and validation
- Inspect existing tests before changing code.
- Run relevant safe tests after modifications.
- Do not claim tests passed unless they were actually executed.
- Explain failures and unresolved issues.
- Show a summary of changed files and important code changes.

## Approval
- Ask before destructive operations, dependency upgrades,
  database changes, Git history changes or production actions.
- Do not commit or push changes unless explicitly asked.

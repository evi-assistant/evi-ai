# Example project context

Drop a file named `EVI.md` at the root of any project. Evi auto-loads
the **nearest** one walking up from your cwd, and appends its content
to every system prompt.

Use it for:

- Project conventions ("we use snake_case, pnpm not npm, …")
- Where things live ("tests in `__tests__/`, configs in `cfg/`")
- Glossary / terminology specific to this codebase
- Constraints ("never edit `vendor/`, never run migrations from the agent")
- Default model / personality preferences if they should override your
  global config

64 KB cap. Anything bigger gets truncated.

---

## Example skeleton

```markdown
# my-project

## Stack
- TypeScript + React 18, Vite
- Tailwind v4 for styling
- pnpm (NOT npm or yarn)
- Tests with vitest in `__tests__/`

## Where things live
- `src/app/`        application shell
- `src/components/` reusable UI
- `src/lib/`        framework-free utilities
- `src/api/`        server actions

## Conventions
- Files: kebab-case for components, snake_case for utilities
- Functions: camelCase, exhaustive switch with `never` guards
- No prop-drilling deeper than 2 levels — use context or a store

## Don'ts
- Never touch `dist/` or `node_modules/`
- Never push migrations via the agent — surface a SQL diff instead
- Never read `.env*` files
```

Tweak to your taste; the agent will read whichever file you keep.

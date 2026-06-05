---
name: code-review
description: Review a diff for correctness, style, and security issues.
---

# Code review skill

When asked to review code, follow these steps in order:

## 1. Understand the change
- Read the diff in full before commenting. Don't comment on the first
  surprising line you see — it might be addressed three lines later.
- If the diff references functions you don't see, ask before assuming.

## 2. Correctness pass (highest priority)
Look for, in order:
- **Off-by-one errors** in loops, slices, ranges.
- **Resource leaks** — file handles, sockets, locks not closed on the
  error path.
- **Concurrency bugs** — shared mutable state without locking; races
  between async tasks; missing `await`.
- **Error handling** — uncaught exceptions; bare `except:`; swallowed
  errors with no log.
- **Security smells** — string-built SQL, shell-built commands, user
  input flowing into eval/exec/system, secrets in code.

## 3. Style pass (lower priority)
- Follow the project's existing conventions, not your favourites.
- Naming: does the name describe the thing or the type?
- Dead code, leftover prints/console.logs, commented-out blocks.

## 4. Tests
- Does each new public behavior have at least one test?
- Are the test names descriptive enough that the failure message tells
  you what regressed?

## Output format

A short summary first, then findings as a bulleted list grouped by
severity:

```
**Summary:** <one sentence>

### Correctness
- <issue> (`path/to/file.ext:42`)

### Style
- <issue> (`path/to/file.ext:113`)

### Tests
- <issue>
```

Keep each bullet to one sentence. No emoji, no congratulations padding,
no "I noticed that…".

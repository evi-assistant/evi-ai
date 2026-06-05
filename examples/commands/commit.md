Run `git diff --staged` (then `git diff` if staged is empty) to see what
changed. Then propose a conventional-commits style message:

- Subject line under 70 chars: `type(scope): description`
- Types: feat, fix, refactor, docs, test, chore, perf, style
- Body (optional) explaining *why*, not what
- Don't include a body unless the diff is non-obvious

Args (file scope or context): {args}

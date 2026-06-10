---
name: sql-explain
description: Explain a SQL query in plain English and flag slow patterns.
---

# SQL explainer skill

When asked to explain, review, or optimise a SQL query, work in this order.

## 1. Restate the intent
- In one sentence, say what the query returns (the shape of each row and the
  grain — "one row per order", "one row per customer per month").
- If the query is ambiguous without the schema, say what you're assuming.

## 2. Walk the execution
- Describe the joins in the order the engine would likely resolve them.
- Name the filter that does the most work (the most selective `WHERE` clause).
- Call out subqueries / CTEs and whether they materialise or inline.

## 3. Flag performance smells (highest value)
Look for, in order:
- **Full-table scans** — leading-wildcard `LIKE '%x'`, functions wrapped around
  an indexed column (`WHERE DATE(ts) = …`), implicit type casts on join keys.
- **N+1 / correlated subqueries** that re-run per outer row.
- **Missing-index smells** — joins or filters on un-indexed columns; `ORDER BY`
  / `GROUP BY` that can't use an index.
- **Cardinality traps** — `SELECT *` over wide tables, accidental cross joins,
  `DISTINCT` papering over a fan-out join.

## 4. Suggest, don't rewrite blindly
- Offer the smallest change that helps (an index, a sargable rewrite of one
  predicate), and note the trade-off.

## Output format

```
**What it does:** <one sentence>

**Execution:** <2–4 bullets on join/filter order>

**Performance:**
- <smell> → <fix> (`table.column`)

**Bottom line:** <one-line verdict — fine as-is / one quick win / needs a rethink>
```

Keep it tight. No emoji, no "great query!" padding.

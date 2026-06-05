---
name: summarize-paper
description: Boil an academic paper down to 5 bullets a non-specialist can follow.
---

# Paper-summarization skill

When given a PDF or pasted paper text, produce a 5-bullet summary plus a
follow-up suggestion list.

## Read first
- Skim the abstract, intro, conclusion. Skip the related-work section
  unless the user asks.
- Use the `read_pdf` tool if a PDF path is provided, or `web_fetch` if a
  URL.

## Bullets

Produce exactly five bullets, in this order:

1. **Problem**: one sentence on what the paper is trying to do.
2. **Approach**: one sentence on the method — what's the trick?
3. **Result**: the headline number, in plain English. "X% better on Y
   than the previous best of Z%."
4. **Limitation**: the most important caveat the paper itself admits.
5. **Why it matters**: who should care, and what does it unlock?

## Follow-ups

End with a short bulleted list of *related work or next-step questions*
the user could ask — three or four items. Each should be specific enough
to actually research, not vague ("how does this compare to…").

## Style

- No formulas in the bullets unless they're essential.
- Define jargon on first use.
- Don't say "the authors". Just describe what was done.

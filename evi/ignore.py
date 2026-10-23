"""
eVi ignore rules implementation.
Supports a subset of .gitignore:
- '#' for comments, blank lines skipped.
- '!' for negation (last matching rule wins).
- Trailing '/' matches directories only.
- Patterns without '/' match at any depth.
- Patterns with '/' (leading or middle) are anchored to the root.
- '*' matches any characters except '/'.
- '?' matches any single character except '/'.
- '**/' matches zero or more directories.
- '/**' at the end matches everything inside.
- '[abc]' and '[!abc]' character classes.
- Files in ignored directories cannot be re-included.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List


@dataclass(frozen=True)


class Rule:
    regex: re.Pattern
    negate: bool
    dir_only: bool
    anchored: bool


def _translate_literal(text: str) -> str:
    i = 0
    res = []
    while i < len(text):
        if text[i] == '?':
            res.append('[^/]')
            i += 1
        elif text[i] == '[':
            j = i + 1
            while j < len(text) and text[j] != ']':
                j += 1
            if j < len(text):
                content = text[i+1:j]
                if content.startswith('!'):
                    content = '^' + content[1:]
                res.append(f'[{content}]')
                i = j + 1
            else:
                res.append(re.escape('['))
                i += 1
        else:
            res.append(re.escape(text[i]))
            i += 1
    return "".join(res)


def _translate(pattern: str) -> str:
    # Mark a trailing '/**' (\x02: '/.*') and each '**/' that starts the pattern
    # or follows a '/' (\x01: zero or more folders; a middle one keeps its '/').
    p = pattern
    if p.endswith('/**'):
        p = p[:-3] + '\x02'
    if p.startswith('**/'):
        p = '\x01' + p[3:]
    p = p.replace('/**/', '/\x01')

    # Any other run of stars is one '*'; split on them (CPython fnmatch's scheme).
    p = re.sub(r'\*+', '*', p)

    parts = p.split('*')

    translated_parts = []
    for part in parts:
        sub_parts = []
        curr = ""
        for char in part:
            if char == '\x01':
                sub_parts.append(_translate_literal(curr))
                sub_parts.append('(?:.*/)?')
                curr = ""
            elif char == '\x02':
                sub_parts.append(_translate_literal(curr))
                sub_parts.append('/.*')
                curr = ""
            else:
                curr += char
        sub_parts.append(_translate_literal(curr))
        translated_parts.append("".join(sub_parts))

    if len(translated_parts) == 1:
        return translated_parts[0]

    p0 = translated_parts[0]
    p_last = translated_parts[-1]
    middle = "".join(f'(?>[^/]*?{p})' for p in translated_parts[1:-1])

    return p0 + middle + '[^/]*' + p_last


def parse_rules(text: str) -> List[Rule]:
    rules = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith('#'):
            continue

        negate = line.startswith('!')
        if negate:
            line = line[1:]

        if line.startswith('\\'):
            line = line[1:]

        dir_only = line.endswith('/')
        if dir_only:
            line = line.rstrip('/')

        anchored = '/' in line
        if line.startswith('/'):
            line = line[1:]

        if not line:
            continue

        try:
            regex = re.compile(_translate(line))
            rules.append(Rule(regex, negate, dir_only, anchored))
        except re.error:
            continue
    return rules


class IgnoreRules:
    def __init__(self, rules: List[Rule]):
        self.rules = rules

    @classmethod
    def load(cls, root: Path) -> 'IgnoreRules':
        text = ""
        for filename in ['.gitignore', '.eviignore']:
            p = root / filename
            if p.exists():
                try:
                    text += p.read_text(encoding='utf-8', errors='replace') + "\n"
                except Exception:
                    pass
        return cls(parse_rules(text))

    def _matches(self, rule: Rule, rel: str, is_dir: bool) -> bool:
        if rule.dir_only and not is_dir:
            return False
        target = rel if rule.anchored else rel.rsplit('/', 1)[-1] if '/' in rel else rel
        return bool(rule.regex.fullmatch(target))

    def _decide(self, rel: str, is_dir: bool) -> bool:
        result = False
        for rule in self.rules:
            if self._matches(rule, rel, is_dir):
                result = not rule.negate
        return result

    def ignored(self, rel: str, is_dir: bool) -> bool:
        parts = rel.split('/')
        for i in range(1, len(parts)):
            if self._decide('/'.join(parts[:i]), True):
                return True
        return self._decide(rel, is_dir)


def ignore_filter(base: Path) -> Callable[[Path], bool]:
    from evi.project import git_toplevel
    root = git_toplevel(base) or base.resolve()

    base_resolved = base.resolve()
    try:
        prefix = base_resolved.relative_to(root).as_posix()
        if prefix == '.':
            prefix = ''
    except ValueError:
        prefix = ''

    rules = IgnoreRules.load(root)

    # Compute if base itself is ignored once
    base_rel = prefix if prefix else ''
    base_is_ignored = False
    if base_rel:
        base_is_ignored = rules.ignored(base_rel, base_resolved.is_dir())

    def predicate(p: Path) -> bool:
        if base_is_ignored:
            return False
        try:
            rel = p.relative_to(base).as_posix()
        except ValueError:
            return False

        full = f"{prefix}/{rel}" if prefix else rel
        return rules.ignored(full, p.is_dir())

    return predicate

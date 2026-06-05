"""Multi-model routing.

A `Router` decides which model to send each user turn to based on lightweight
keyword rules first, optionally falling back to a cheap classifier model.

Why route at all?
- Coder models (qwen2.5-coder, deepseek-coder) crush programming tasks but
  are slow for casual chat.
- Tiny models (qwen2.5-3b) are great for greetings + quick replies but
  fall apart on deep reasoning.
- Vision models cost more — only worth using when there's actually an image.

A route is a `(name, model, keywords)` tuple. The first route whose
keywords appear in the user message wins. If no route matches, the
default model (from `[llm] model`) is used.

Storage: `~/.evi/routes.json` — a JSON document with a `routes` array. We
use JSON (not TOML) because our minimal TOML writer doesn't speak
arrays-of-tables, and the file is small + simple.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from evi.config import HOME


ROUTES_PATH = HOME / "routes.json"


@dataclass
class Route:
    """One routing rule.

    `match_keywords` are case-insensitive substrings; if ANY substring
    appears in the user message, the route fires. Empty list = the route
    can only be picked by the LLM classifier (or never, if there's no
    classifier).
    """

    name: str
    model: str
    description: str = ""
    match_keywords: list[str] = field(default_factory=list)


@dataclass
class RouteDecision:
    """The result of picking a route. Carries enough metadata for logging."""

    model: str
    route_name: str   # "default" if nothing matched
    reason: str       # "keyword:debug" / "classifier" / "default"


class RouterStore:
    """JSON-on-disk store of routes."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else ROUTES_PATH

    def load(self) -> list[Route]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        raw = data.get("routes", []) if isinstance(data, dict) else []
        out: list[Route] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            model = str(entry.get("model", "")).strip()
            if not name or not model:
                continue
            keywords = entry.get("match_keywords", []) or []
            if not isinstance(keywords, list):
                keywords = []
            out.append(
                Route(
                    name=name,
                    model=model,
                    description=str(entry.get("description", "")),
                    match_keywords=[str(k) for k in keywords if str(k).strip()],
                )
            )
        return out

    def save(self, routes: list[Route]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"routes": [asdict(r) for r in routes]}
        self.path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    # --- mutations ------------------------------------------------------

    def add(self, route: Route, *, overwrite: bool = False) -> bool:
        """Append or replace a route. Returns True on success, False if a
        route by that name exists and `overwrite=False`."""
        routes = self.load()
        for i, r in enumerate(routes):
            if r.name == route.name:
                if not overwrite:
                    return False
                routes[i] = route
                self.save(routes)
                return True
        routes.append(route)
        self.save(routes)
        return True

    def remove(self, name: str) -> bool:
        routes = self.load()
        kept = [r for r in routes if r.name != name]
        if len(kept) == len(routes):
            return False
        self.save(kept)
        return True


class Router:
    """Resolve a user message to a `(model, route_name, reason)` decision.

    Construction:
        Router(routes, default_model="qwen2.5-7b-instruct",
               classifier_model="qwen2.5-3b", client=openai_client)

    The `client` (`openai.OpenAI`) is only used if `classifier_model` is
    set AND keyword matching fails. Pass `client=None` to disable the
    classifier fallback.
    """

    def __init__(
        self,
        routes: list[Route],
        *,
        default_model: str,
        classifier_model: str = "",
        client=None,
    ) -> None:
        self.routes = list(routes)
        self.default_model = default_model
        self.classifier_model = classifier_model.strip()
        self.client = client

    def pick(self, user_msg: str) -> RouteDecision:
        if not user_msg.strip() or not self.routes:
            return RouteDecision(self.default_model, "default", "default")

        # 1) Cheap keyword match. First hit wins so users get deterministic
        #    behaviour without an LLM round-trip.
        #
        #    We match at word boundaries (\b) rather than raw substrings so
        #    "hi" doesn't match "this" and "code" doesn't match "decode".
        #    Anchoring only at the start lets "debug" still hit "debugger"
        #    and "debugging", which matches what users usually want.
        lower = user_msg.lower()
        for r in self.routes:
            for kw in r.match_keywords:
                k = kw.strip().lower()
                if not k:
                    continue
                pattern = r"\b" + re.escape(k)
                if re.search(pattern, lower):
                    return RouteDecision(r.model, r.name, f"keyword:{k}")

        # 2) Optional LLM classifier.
        if self.classifier_model and self.client is not None:
            picked = self._classify(user_msg)
            if picked is not None:
                return RouteDecision(picked.model, picked.name, "classifier")

        return RouteDecision(self.default_model, "default", "default")

    # --- internals ------------------------------------------------------

    def _classify(self, user_msg: str) -> Route | None:
        """Ask the classifier model which route best fits. Best-effort —
        any failure (network, parse, unknown name) returns None."""
        catalogue = "\n".join(
            f"- {r.name}: {r.description or '(no description)'}"
            for r in self.routes
        )
        prompt = (
            "Pick the SINGLE best route for the user's message. "
            "Reply with the route name only — no punctuation, no explanation. "
            "If none fit well, reply 'default'.\n\n"
            f"Routes:\n{catalogue}\n\n"
            f"User message: {user_msg.strip()}\n\n"
            "Route:"
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.classifier_model,
                messages=[
                    {"role": "system", "content": "You are a router. Reply with one token."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=8,
                stream=False,
            )
            content = (resp.choices[0].message.content or "").strip().lower()
        except Exception:
            return None
        if not content or content == "default":
            return None
        # Normalise: take the first token only in case the model rambled.
        token = content.split()[0].rstrip(".,;:!?")
        for r in self.routes:
            if r.name.lower() == token:
                return r
        return None


# --- preset rule sets ---------------------------------------------------


PRESET_ROUTES: dict[str, list[Route]] = {
    "common": [
        Route(
            name="coder",
            model="qwen2.5-coder-14b-instruct",
            description="Programming, debugging, refactoring, code review",
            match_keywords=[
                "code", "function", "debug", "refactor", "stack trace",
                "bug", "compile", "import", "syntax", "regex", "snippet",
            ],
        ),
        Route(
            name="fast",
            model="qwen2.5-3b-instruct",
            description="Quick greetings + trivial Q&A",
            match_keywords=["hi", "hello", "thanks", "thank you", "yes", "no"],
        ),
    ],
}

"""eVi Agent SDK — constrain the model to a JSON Schema.

``as_response_format`` wraps a plain JSON Schema into the OpenAI
``response_format`` shape; ``run_headless(..., response_format=...)`` forwards it
so the backend returns schema-valid JSON (Structured Outputs).
Run with:  python examples/python/structured_output.py
"""

import json

from evi.sdk import as_response_format, build_agent, run_headless

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "language": {"type": "string"},
        "line_count": {"type": "integer"},
    },
    "required": ["title", "language", "line_count"],
    "additionalProperties": False,
}


def main() -> None:
    agent = build_agent(tools=[])  # no tools needed for a pure extraction task
    rf = as_response_format(SCHEMA, name="file_summary")

    result = run_headless(
        agent,
        "Describe a hypothetical 200-line Python CLI named 'ripgrep-lite'.",
        response_format=rf,
    )
    data = json.loads(result.text)  # guaranteed to match SCHEMA
    print(data)


if __name__ == "__main__":
    main()

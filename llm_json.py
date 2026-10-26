"""
Shared helper for getting structured JSON back from Claude.

Uses the API's structured outputs (output_config json_schema): the model's reply
is constrained to the schema, so stray quote marks or missing commas can no
longer break parsing. Schemas mirror the JSON formats described in prompts.py.
"""

import json

MODEL = "claude-sonnet-5-5"


def _obj(**props):
    """Object schema where every property is required and no extras are allowed."""
    return {"type": "object", "properties": props,
            "required": list(props), "additionalProperties": False}


def _arr(items):
    return {"type": "array", "items": items}


STR = {"type": "string"}
INT = {"type": "integer"}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}
SENTIMENT = {"type": "string", "enum": ["positive", "negative", "mixed", "neutral"]}
LEVEL = {"type": "string", "enum": ["high", "medium", "low"]}

_COMMERCIAL = _obj(
    brands=_arr(_obj(name=STR, category=STR, sentiment=SENTIMENT, mentions=INT)),
    recommendations=_arr(_obj(item=STR, category=STR, quote=STR, endorsements=INT)),
    opportunities=_arr(_obj(type=STR, target=STR, rationale=STR)),
)

_PASS_FAIL = {"type": "string", "enum": ["pass", "fail"]}
_PASS_FAIL_NA = {"type": "string", "enum": ["pass", "fail", "not_applicable"]}

SCHEMAS = {
    "sentiment": _obj(
        overall=_obj(positive=NUM, neutral=NUM, negative=NUM, summary=STR),
        byTopic=_arr(_obj(topic=STR, sentiment=SENTIMENT, percentage=NUM, explanation=STR)),
    ),
    "themes": _obj(themes=_arr(_obj(
        name=STR, description=STR, frequency=LEVEL, sentiment=SENTIMENT,
        representativeQuotes=_arr(STR), keywords=_arr(STR),
    ))),
    "summary": _obj(
        executiveSummary=STR, consensus=_arr(STR), contention=_arr(STR),
        notableComments=_arr(_obj(excerpt=STR, why=STR)),
    ),
    "followups": _obj(followUpIdeas=_arr(_obj(
        headline=STR, angle=STR, interestLevel=LEVEL, evidence=STR, suggestedSources=_arr(STR),
    ))),
    "questions": _obj(questions=_arr(_obj(question=STR, intent=STR))),
    "commercial": _COMMERCIAL,
    "alt_text": _obj(
        is_decorative=BOOL,
        score={"type": "string", "enum": ["pass", "needs_improvement", "fail"]},
        issues=_arr(STR),
        suggested_alt=STR,
        violated_criteria=_arr(STR),
        criteria_results=_obj(
            present=_PASS_FAIL, concise=_PASS_FAIL_NA, descriptive=_PASS_FAIL,
            no_redundant_phrases=_PASS_FAIL, not_caption_repeat=_PASS_FAIL,
            news_context=_PASS_FAIL, text_in_image=_PASS_FAIL_NA,
            no_keyword_stuffing=_PASS_FAIL, decorative_handled=_PASS_FAIL_NA,
        ),
    ),
}


def ask_json(client, content, max_tokens: int, schema: str) -> dict:
    """Send a prompt (string or list of content blocks) and return the parsed JSON reply.

    `schema` is a key of SCHEMAS. Raises ValueError with a readable message if no
    usable result comes back.
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMAS[schema]}},
    )
    if response.stop_reason == "max_tokens":
        raise ValueError(f"Claude's response was cut off (hit the {max_tokens}-token limit)")
    if response.stop_reason == "refusal":
        raise ValueError("Claude declined to analyze this content")
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned invalid JSON ({e})")

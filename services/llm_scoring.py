import json
import re
import time
from typing import Any, Tuple

from openai import OpenAI

from config import settings
from models import ScoreCriterion, ScoreResult


def score_candidate(payload: dict) -> dict | ScoreResult:
    """
    Returns ScoreResult on success.
    On failure, raises exception; caller should set scoring_failed + error.
    """
    client = OpenAI(api_key=settings.openai_api_key)
    model = settings.llm_model

    # Language: match candidate answers (RU if any Cyrillic, else EN)
    answers_text = ""
    try:
        answers = payload.get("answers") or {}
        answers_text = " ".join(str(v) for v in answers.values())
    except Exception:
        answers_text = ""
    is_ru = bool(re.search(r"[А-Яа-яЁё]", answers_text))
    lang_name = "Russian" if is_ru else "English"

    system = (
        "You are an HR screening scorer. Return JSON ONLY.\n"
        "\n"
        "IMPORTANT:\n"
        f"- Always write rationale and summary strictly in {lang_name}.\n"
        f"- Do NOT use any other language besides {lang_name} in explanations.\n"
        "- Keep explanations concise and professional.\n"
        "\n"
        "You MUST follow this schema exactly:\n"
        "{\n"
        '  "criteria": [\n'
        '    { "name": "string", "score_0_10": 0, "rationale": "1-2 sentences" },\n'
        '    { "name": "string", "score_0_10": 0, "rationale": "1-2 sentences" },\n'
        '    { "name": "string", "score_0_10": 0, "rationale": "1-2 sentences" }\n'
        "  ],\n"
        '  "overall_score_0_10": 0,\n'
        '  "hot": false,\n'
        '  "summary_1_2_lines": "string"\n'
        "}\n"
        "Rules:\n"
        "- criteria must be exactly 3 items\n"
        "- score_0_10 must be integer 0..10\n"
        "- rationale must be 1-2 sentences\n"
        "- summary_1_2_lines must be <= 2 lines\n"
        "- If project_link is 'declined', mention it explicitly without silently penalizing.\n"
    )

    user = (
        "Score this candidate based on their answers.\n"
        f"Payload JSON:\n{json.dumps(payload, ensure_ascii=False)}\n"
        "Use exactly these 3 criteria names:\n"
        "1) Practical AI Application\n"
        "2) AI Reasoning & Control\n"
        "3) AI Product Thinking\n"
        "Overall score must be round(mean(criteria scores)).\n"
        "hot must be true if overall>=8 OR at least 2 criteria>=8.\n"
        "Return JSON ONLY."
    )

    def _call(prompt: str) -> str:
        resp = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        # openai-python returns text via output_text helper
        return resp.output_text

    text = _call(user)
    try:
        data = json.loads(text)
        return ScoreResult.model_validate(data)
    except Exception:
        # 1 retry: force JSON only, no prose
        retry_prompt = user + "\n\nreturn valid JSON only, no prose"
        text2 = _call(retry_prompt)
        data2 = json.loads(text2)
        return ScoreResult.model_validate(data2)

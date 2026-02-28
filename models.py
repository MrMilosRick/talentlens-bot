from pydantic import BaseModel, Field


class ScoreCriterion(BaseModel):
    name: str
    score_0_10: int = Field(ge=0, le=10)
    rationale: str


class ScoreResult(BaseModel):
    criteria: list[ScoreCriterion] = Field(min_length=3, max_length=3)
    overall_score_0_10: int = Field(ge=0, le=10)
    hot: bool
    summary_1_2_lines: str


class SheetRow(BaseModel):
    timestamp_utc_iso: str
    tg_user_id: int = Field(ge=0)
    username: str | None = None
    full_name: str | None = None
    answers_json: str
    project_link: str
    project_note: str | None = None
    scores_json: str
    overall_score: int = Field(ge=0, le=10)
    top_candidate: bool
    llm_model: str
    latency_ms: int = Field(ge=0)
    scoring_failed: bool
    error: str | None = None

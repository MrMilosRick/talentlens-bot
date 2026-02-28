from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = Field(alias="BOT_TOKEN")
    admin_user_id: int = Field(alias="ADMIN_USER_ID")
    admin_alert_chat_id: int = Field(alias="ADMIN_ALERT_CHAT_ID")
    google_sheets_id: str = Field(alias="GOOGLE_SHEETS_ID")
    google_creds_path: str = Field(alias="GOOGLE_CREDS_PATH")
    llm_model: str = Field(alias="LLM_MODEL")
    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    google_sheets_worksheet: str = Field(default="Sheet1", alias="GOOGLE_SHEETS_WORKSHEET")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

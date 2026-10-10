import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    value = (os.getenv(name) or default).strip()
    # Treat the placeholders from .env.example as "not set".
    return "" if value.startswith("your_") else value


@dataclass(frozen=True)
class Settings:
    hindsight_api_url: str
    hindsight_api_key: str
    hindsight_bank_id: str
    groq_api_key: str
    groq_model: str
    data_dir: Path


settings = Settings(
    hindsight_api_url=_env("HINDSIGHT_API_URL", "https://api.hindsight.vectorize.io"),
    hindsight_api_key=_env("HINDSIGHT_API_KEY"),
    hindsight_bank_id=_env("HINDSIGHT_BANK_ID", "dealpilot"),
    groq_api_key=_env("GROQ_API_KEY"),
    groq_model=_env("GROQ_MODEL", "openai/gpt-oss-120b"),
    data_dir=BASE_DIR / "data",
)

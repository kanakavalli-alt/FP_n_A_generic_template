"""Runtime settings — environment only. Client knowledge lives in the semantic
layer, never here."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:  # optional
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    semantic_backend: str          # "yaml" | "bigquery"
    semantic_yaml_dir: str
    bq_project: str
    bq_dataset: str
    bq_table_prefix: str
    model_name: str
    explorer_model_name: str
    regeneration_model_name: str
    grounding_regenerate: bool
    turn_log_dir: str


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings(
        semantic_backend=os.getenv("SEMANTIC_BACKEND", "yaml").lower(),
        semantic_yaml_dir=os.getenv(
            "SEMANTIC_YAML_DIR", str(PROJECT_ROOT / "semantic_layer" / "demo")
        ),
        bq_project=os.getenv("BQ_PROJECT", ""),
        bq_dataset=os.getenv("BQ_DATASET", "fpa_semantic_layer"),
        bq_table_prefix=os.getenv("BQ_TABLE_PREFIX", "sl_"),
        model_name=os.getenv("MODEL_NAME", "gemini-2.5-flash"),
        explorer_model_name=os.getenv("EXPLORER_MODEL_NAME", os.getenv("MODEL_NAME", "gemini-2.5-flash")),
        regeneration_model_name=os.getenv("REGENERATION_MODEL_NAME", os.getenv("MODEL_NAME", "gemini-2.5-flash")),
        grounding_regenerate=os.getenv("GROUNDING_REGENERATE", "true").lower() == "true",
        turn_log_dir=os.getenv("TURN_LOG_DIR", str(PROJECT_ROOT / "logs" / "turns")),
    )

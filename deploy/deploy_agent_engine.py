"""
Deploy the agent to Vertex AI Agent Engine (then register it in Gemini
Enterprise from the console, as in the Kaplan build).

    python deploy/deploy_agent_engine.py --project my-proj --location us-central1 \
        --staging-bucket gs://my-staging --display-name "Acme FP&A Agent"

Runtime configuration is passed as env vars — client knowledge stays in the
BigQuery semantic layer; secrets stay in Secret Manager:
    SEMANTIC_BACKEND=bigquery BQ_PROJECT=... BQ_DATASET=...
    AGENT_SA_KEY_SECRET=projects/.../secrets/agent-sa-key/versions/latest
    <SOURCE>_MCP_URL=https://.../mcp      (one per source in sl_sources.connection.url_env)
    TURN_LOG_GCS_BUCKET=...               (audit trail)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS_THROUGH = ["SEMANTIC_BACKEND", "BQ_PROJECT", "BQ_DATASET", "BQ_TABLE_PREFIX", "MODEL_NAME",
                "EXPLORER_MODEL_NAME", "REGENERATION_MODEL_NAME", "GROUNDING_REGENERATE",
                "AGENT_SA_KEY_SECRET", "TURN_LOG_GCS_BUCKET"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--location", default="us-central1")
    ap.add_argument("--staging-bucket", required=True)
    ap.add_argument("--display-name", default="FP&A Agent")
    a = ap.parse_args()

    import vertexai
    from vertexai import agent_engines
    from vertexai.preview import reasoning_engines

    from fpa_agent.agents.root import root_agent
    from fpa_agent.semantic.repository import get_catalog

    env = {k: os.environ[k] for k in PASS_THROUGH if os.getenv(k)}
    for s in get_catalog().sources.values():           # each source's MCP URL env var
        name = s.connection.get("url_env")
        if name and os.getenv(name):
            env[name] = os.environ[name]
    vertexai.init(project=a.project, location=a.location, staging_bucket=a.staging_bucket)
    app = reasoning_engines.AdkApp(agent=root_agent, enable_tracing=True)
    remote = agent_engines.create(
        app,
        display_name=a.display_name,
        requirements=(ROOT / "requirements.txt").read_text().split(),
        extra_packages=[str(ROOT / "fpa_agent")],
        env_vars=env,
    )
    print("deployed:", remote.resource_name)


if __name__ == "__main__":
    main()

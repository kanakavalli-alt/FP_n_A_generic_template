"""ADK entry point:  adk web apps   |   adk run apps/fpa_analyst"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # project root

from fpa_agent.agents.root import root_agent  # noqa: E402,F401

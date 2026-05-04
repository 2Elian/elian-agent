"""Configuration loaded from environment variables."""
import os

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///data/claude.db")
API_KEY = os.environ.get("API_KEY", "sk-e20ua3ip9jkghdacjzyz1rbrmxk3h8ewdb8tqb7ajsnez7fm")
BASE_URL = os.environ.get("BASE_URL", "https://api.xiaomimimo.com/v1")
MODEL = os.environ.get("MODEL", "mimo-v2.5-pro")
DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "openai")
MAX_CONTEXT_TOKENS = int(os.environ.get("MAX_CONTEXT_TOKENS", "200000"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "32000"))
AUTO_COMPACT_ENABLED = os.environ.get("AUTO_COMPACT_ENABLED", "true").lower() == "true"
AUTO_MEMORY_ENABLED = os.environ.get("AUTO_MEMORY_ENABLED", "true").lower() == "true"
MAX_TURNS = int(os.environ.get("MAX_TURNS", "50"))
MAX_BUDGET_USD = float(os.environ.get("MAX_BUDGET_USD", "10.0"))
MEMORY_DIR = os.path.expanduser(os.environ.get("MEMORY_DIR", "~/.claude/projects"))

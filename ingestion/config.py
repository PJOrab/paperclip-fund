"""Konfiguration für die Ingestion — lädt .env aus dem Projekt-Root."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

# Supabase-Projekt (Settings → API im Supabase-Dashboard)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
# service_role-Key für serverseitiges Schreiben (NICHT der anon-Key!)
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "") or os.environ.get("SUPABASE_KEY", "")

# Pfad zum bestehenden macro-agent (dessen Adapter wir wiederverwenden)
MACRO_AGENT_DIR = os.environ.get(
    "MACRO_AGENT_DIR",
    str(Path.home() / "macro-agent"),
)


def require_supabase() -> None:
    missing = [k for k, v in {
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_SERVICE_KEY": SUPABASE_KEY,
    }.items() if not v]
    if missing:
        raise SystemExit(
            "Fehlende Supabase-Konfiguration: " + ", ".join(missing) +
            "\nLege sie in /root/ai-tech-fund/.env an (siehe .env.example)."
        )

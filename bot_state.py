import os
from typing import Optional

try:
    from supabase import create_client, Client
except Exception:
    create_client = None
    Client = None

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY and create_client:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Failed to connect to Supabase: {e}")


def get_bot_state() -> bool:
    if supabase:
        try:
            res = supabase.table("bot_control").select("running").eq("id", 1).execute()
            if res.data:
                return bool(res.data[0]["running"])
        except Exception as e:
            print(f"Supabase get error: {e}")
    return True


def set_bot_state(running: bool) -> None:
    if supabase:
        try:
            supabase.table("bot_control").upsert(
                {"id": 1, "running": bool(running), "updated_at": "now()"},
                on_conflict="id"
            ).execute()
            return
        except Exception as e:
            print(f"Supabase set error: {e}")
    # Fallback: no-op if Supabase unavailable
    pass


# Backwards compatible alias
_get_bot_state = get_bot_state

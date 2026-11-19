"""`python -m tower.migrate`: apply db/schema.sql and every db/migrations/*.sql (all idempotent)."""

from __future__ import annotations

from tower.config import load_settings
from tower.db import apply_schema


def main() -> None:
    applied = apply_schema(load_settings().db_url)
    print("applied: " + ", ".join(applied))


if __name__ == "__main__":
    main()

"""Settings from environment / .env. Secrets never get logged."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_prefix="CT_", extra="ignore")

    db_password: str = ""
    database_url: str = ""
    gps_secret: str = ""
    admin_key: str = ""
    carrier_api_key: str = ""
    carrier_admin_key: str = ""
    token_asha: str = ""
    token_kiran: str = ""
    token_meera: str = ""

    # milestone 2: local LLM, approval threshold, outbox
    llm_url: str = "http://localhost:11434"
    llm_model: str = "fusion27b:iq3"
    embed_model: str = "nomic-embed-text"
    llm_timeout_s: float = 240.0
    llm_num_ctx: int = 8192
    # the simulator draws its PDFs with reportlab, which the tower does not need: an interpreter that has it
    # (CT_PDF_PYTHON); empty means the one running the simulator
    pdf_python: str = ""
    approval_threshold_inr: int = 500_000
    chat_webhook_url: str = "http://127.0.0.1:9100/hooks/chat"
    impact_poll_s: float = 10.0
    outbox_poll_s: float = 1.0

    host: str = "127.0.0.1"
    port: int = 8500
    carrier_api_url: str = "http://127.0.0.1:9100"
    carrier_api_port: int = 9100

    data_dir: Path = ROOT / "data"
    workers: bool = True
    scenario_seed: int = 20261113

    # worker cadence (wall seconds)
    erp_poll_s: float = 1.0
    carrier_poll_s: float = 1.5
    alerts_poll_s: float = 2.0
    carrier_timeout_s: float = 1.5
    carrier_retries: int = 1          # extra attempts inside one poll, with jittered backoff
    breaker_threshold: int = 3        # consecutive failed polls before the circuit opens
    breaker_cooldown_s: float = 4.0
    breaker_max_cooldown_s: float = 16.0

    gps_window_s: int = 300           # HMAC timestamp window, either side
    gps_max_future_s: int = 300       # ping event time this far ahead of now is clock skew

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        if not self.db_password:
            raise RuntimeError("CT_DB_PASSWORD is empty: copy .env.example to .env and set it")
        return f"postgresql://ct:{self.db_password}@127.0.0.1:5436/ct"

    @property
    def network_dir(self) -> Path:
        return self.data_dir / "network"

    @property
    def inbox(self) -> Path:
        return self.data_dir / "inbox" / "erp"

    @property
    def processed(self) -> Path:
        return self.data_dir / "processed"

    @property
    def rejected(self) -> Path:
        return self.data_dir / "rejected"

    @property
    def bol_inbox(self) -> Path:
        return self.data_dir / "inbox" / "bol"

    @property
    def bol_processed(self) -> Path:
        return self.data_dir / "processed" / "bol"

    @property
    def bol_rejected(self) -> Path:
        return self.data_dir / "rejected" / "bol"

    @property
    def contracts_dir(self) -> Path:
        return self.data_dir / "contracts"

    @property
    def clock_file(self) -> Path:
        return self.data_dir / "scenario_clock.json"


def load_settings(**overrides) -> Settings:
    return Settings(**overrides)

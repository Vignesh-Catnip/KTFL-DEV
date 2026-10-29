"""Application settings (environment variables / .env). Passwords never live in code."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"

    # ---- application database ----
    database_url: str = "sqlite:///./clearmatch.db"

    # ---- SAP data lake (SQL Server, read only) ----
    sap_enabled: bool = True
    sap_server: str = ""
    sap_database: str = "BRONZE_DB"
    sap_user: str = ""
    sap_password: str = ""
    sap_driver: str = "ODBC Driver 17 for SQL Server"
    sap_client: str = "150"                  # MANDT filter ("" = no filter)
    sap_db_url: str = ""                     # full SQLAlchemy URL override (tests / other drivers)
    grn_movement_types: str = "101,105"
    # Where SAP keeps the HSN / SAC / NCM code, as TABLE.COLUMN list, e.g. "MSEG.J_1IG_HSN_SAC,EKPO.STEUC,MARC.STEUC".
    # Empty = auto-detect: every column of MSEG / EKPO / MARC whose name contains HSN, NBM (NCM), STEUC or STAWN.
    sap_hsn_columns: str = ""

    # ---- Ollama ----
    ollama_url: str = "http://localhost:11434"
    model_vlm: str = "qwen3-vl:32b"
    num_ctx: int = 16384
    num_predict: int = 6144
    vlm_timeout: int = 900
    main_pages: int = 2
    grn_pages: int = 3
    image_zoom: float = 1.4
    grn_zoom: float = 2.0

    # ---- matching thresholds ----
    amount_tolerance: float = 0.02           # 2 % invoice taxable value vs GRN value
    round_tolerance: float = 1.0             # INR slack for rounding
    qty_tolerance: float = 0.001
    ktfl_gstin_prefixes: str = "27AABCK0618A"

    # ---- files ----
    upload_dir: str = "./data/uploads"
    pdf_search_dirs: str = ""
    watch_folder: str = ""
    gst_rate_xlsx: str = "./data/GST_RATE.xlsx"
    max_upload_mb: int = 25
    allow_upload: bool = False               # intake is the watched folder; upload from the browser is off
    watch_settle_seconds: int = 5            # a PDF must be unchanged this long before it is picked up
    min_completeness: float = 0.70           # >= this share of mandatory fields read -> RECONCILE, below -> BLOCKED

    # ---- auth ----
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720
    admin_username: str = "admin"
    admin_password: str = ""

    # ---- behaviour ----
    worker_enabled: bool = True
    # ---- MIRO robot (Robot Framework + SAP GUI) started from the dashboard ----
    robot_enabled: bool = False
    robot_dir: str = ""                      # folder that contains tasks/miro_posting.robot  (its own .env is used)
    robot_python: str = ""                   # python that has robotframework installed ("" = this python)
    robot_task: str = "tasks/miro_posting.robot"
    robot_mode: str = ""                     # CHECK / PARK / POST - "" = whatever the robot's own .env says
    robot_auto_run: bool = True              # start the robot by itself when matched invoices are waiting
    robot_auto_delay_seconds: int = 60       # wait for more invoices before starting (one SAP session at a time)
    robot_timeout_minutes: int = 120
    robot_log_dir: str = "./data/robot_runs"
    watcher_poll_seconds: int = 20
    cors_origins: str = "http://localhost:5173"
    plant_names: str = "{}"

    # ---- helpers ----
    @property
    def grn_types(self) -> tuple[str, ...]:
        return tuple(x.strip() for x in self.grn_movement_types.split(",") if x.strip())

    @property
    def gstin_prefixes(self) -> tuple[str, ...]:
        return tuple(x.strip().upper() for x in self.ktfl_gstin_prefixes.split(",") if x.strip())

    @property
    def search_dirs(self) -> list[Path]:
        dirs = [Path(self.upload_dir)]
        dirs += [Path(x.strip()) for x in self.pdf_search_dirs.split(",") if x.strip()]
        return dirs

    @property
    def cors_list(self) -> list[str]:
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

    @property
    def plant_name_map(self) -> dict[str, str]:
        try:
            data = json.loads(self.plant_names or "{}")
            return {str(k): str(v) for k, v in data.items()}
        except Exception:
            return {}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()

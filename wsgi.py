from __future__ import annotations

import os
from pathlib import Path

from src.config import load_config, validate_config
from src.logger import setup_logger
from src.paths import ensure_directories
from src.webapp import create_web_app


_config_path = Path(os.environ.get("SAT32D_CONFIG_PATH", "config.yaml")).expanduser()
if not _config_path.is_absolute():
    _config_path = (Path(__file__).resolve().parent / _config_path).resolve()

config = load_config(_config_path)
validate_config(config)
ensure_directories(config.paths)
logger = setup_logger(config)
app = create_web_app(config, logger)

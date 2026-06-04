from __future__ import annotations

import logging

from src.models import AppConfig


def setup_logger(config: AppConfig) -> logging.Logger:
    logger = logging.getLogger(config.project_name)
    logger.setLevel(getattr(logging, config.logging.level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    log_path = config.paths.logs / config.logging.file_name
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger
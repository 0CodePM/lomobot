"""Configuration module for lomobot."""

from lomobot.config.loader import load_config, get_config_path
from lomobot.config.schema import Config

__all__ = ["Config", "load_config", "get_config_path"]

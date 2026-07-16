from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path


APP_DIR = Path.home() / ".mangashelf"
CONFIG_PATH = APP_DIR / "config.json"


@dataclass
class AppConfig:
    theme: str = "Dark"
    default_reading_mode: str = "Vertical Scroll"
    default_rtl: bool = False
    image_upscale_quality: str = "Balanced"


class ConfigManager:
    def __init__(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._config = AppConfig()
        self.load()

    @property
    def config(self) -> AppConfig:
        return self._config

    def load(self) -> AppConfig:
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                self._config = AppConfig(
                    theme=data.get("theme", "Dark"),
                    default_reading_mode=data.get("default_reading_mode", "Vertical Scroll"),
                    default_rtl=bool(data.get("default_rtl", False)),
                    image_upscale_quality=data.get("image_upscale_quality", "Balanced"),
                )
            except (json.JSONDecodeError, OSError):
                self._config = AppConfig()
        else:
            self.save()
        return self._config

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self._config), indent=2), encoding="utf-8")

    def update(self, **kwargs: object) -> AppConfig:
        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)
        self.save()
        return self._config

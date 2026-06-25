"""Runtime configuration: proxies, pacing, headless mode.

Loaded from a YAML file with environment-friendly defaults so the POC runs with
zero config, and the full system scales by editing one file.
"""

from __future__ import annotations

import itertools
import os
from dataclasses import dataclass, field

from .pacing import PacingPolicy
from .unblocker import Unblocker, from_config as unblocker_from_config

try:
    import yaml
except ImportError:  # config file is optional for a bare run
    yaml = None


@dataclass
class Config:
    proxies: list[str] = field(default_factory=list)
    headless: bool = True
    pacing: PacingPolicy = field(default_factory=PacingPolicy)
    db_path: str = "data/serviceability.db"
    request_timeout_seconds: float = 30.0
    unblocker: Unblocker = field(default_factory=Unblocker)

    def __post_init__(self):
        self._proxy_cycle = itertools.cycle(self.proxies) if self.proxies else None

    def next_proxy(self) -> str | None:
        """Round-robin the configured proxies. None means a direct connection."""
        if self._proxy_cycle is None:
            return None
        return next(self._proxy_cycle)


def load_config(path: str = "config.yaml") -> Config:
    if yaml is None or not os.path.exists(path):
        return Config()
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    pacing_data = data.get("pacing", {})
    return Config(
        proxies=data.get("proxies", []),
        headless=data.get("headless", True),
        db_path=data.get("db_path", "data/serviceability.db"),
        request_timeout_seconds=data.get("request_timeout_seconds", 30.0),
        pacing=PacingPolicy(**pacing_data) if pacing_data else PacingPolicy(),
        unblocker=unblocker_from_config(data.get("unblocker")),
    )

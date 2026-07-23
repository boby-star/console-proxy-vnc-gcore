from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json

@dataclass
class ConsoleSession:
    version: int
    mode: str
    provider: str
    upstream_url: str
    created_at: str
    ttl: int

    @classmethod
    def create(cls, mode: str, provider: str, upstream_url: str, ttl: int):
        return cls(2, mode, provider or "gcore", upstream_url, datetime.now(timezone.utc).isoformat(), ttl)
    def to_json(self): return json.dumps(asdict(self))
    @classmethod
    def from_redis(cls, raw: str, ttl: int):
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("version") == 2:
                return cls(**data)
        except Exception:
            pass
        return cls.create("vnc", "legacy", raw, ttl)
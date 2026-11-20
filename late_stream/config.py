"""Load the YAML schedule/config file."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from .encoder import EncoderSettings

VALID_PRIVACY = {"public", "unlisted", "private"}
VALID_LATENCY = {"normal", "low", "ultraLow"}


class ConfigError(ValueError):
    pass


@dataclass
class Job:
    id: str
    file: Path
    start: datetime
    title: str
    description: str = ""
    privacy: str = "unlisted"
    latency: str = "normal"
    auto_start: bool = True
    auto_stop: bool = True
    enable_dvr: bool = True
    made_for_kids: bool = False
    category_id: str | None = None
    thumbnail: Path | None = None
    ingest_stream: str = "late-stream-main"
    announce_minutes: float = 60
    max_late_minutes: float = 15
    encoder: EncoderSettings = field(default_factory=EncoderSettings)


@dataclass
class Config:
    path: Path
    client_secrets: Path
    token_file: Path
    state_file: Path
    log_dir: Path
    jobs: list[Job]

    def job(self, job_id: str) -> Job:
        for j in self.jobs:
            if j.id == job_id:
                return j
        raise ConfigError(f"No job with id '{job_id}' in {self.path}")


def _parse_dt(value, job_id: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError as e:
            raise ConfigError(f"job {job_id}: bad start time {value!r}") from e
    if dt.tzinfo is None:
        raise ConfigError(
            f"job {job_id}: start time {value!r} needs a UTC offset, e.g. 2026-11-21T09:00:00+07:00")
    return dt


def _resolve(base: Path, p) -> Path:
    p = Path(str(p)).expanduser()
    return p if p.is_absolute() else (base / p)


JOB_KEYS = {f for f in Job.__dataclass_fields__}


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    base = path.parent
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError as e:
        raise ConfigError(f"Config not found: {path}") from e
    yt = raw.get("youtube", {}) or {}
    defaults = raw.get("defaults", {}) or {}
    default_enc = defaults.pop("encoder", {}) or {}

    jobs: list[Job] = []
    seen = set()
    for i, rj in enumerate(raw.get("jobs", []) or []):
        rj = dict(rj)
        jid = str(rj.get("id") or f"job{i + 1}")
        if jid in seen:
            raise ConfigError(f"duplicate job id {jid}")
        seen.add(jid)
        merged = {**defaults, **rj, "id": jid}
        enc = {**default_enc, **(merged.pop("encoder", {}) or {})}
        unknown = set(merged) - JOB_KEYS
        if unknown:
            raise ConfigError(f"job {jid}: unknown keys {sorted(unknown)}")
        for req in ("file", "start", "title"):
            if req not in merged:
                raise ConfigError(f"job {jid}: '{req}' is required")
        merged["file"] = _resolve(base, merged["file"])
        merged["start"] = _parse_dt(merged["start"], jid)
        if merged.get("thumbnail"):
            merged["thumbnail"] = _resolve(base, merged["thumbnail"])
        if merged.get("category_id") is not None:
            merged["category_id"] = str(merged["category_id"])
        try:
            merged["encoder"] = EncoderSettings.from_dict(enc)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"job {jid}: {e}") from e
        job = Job(**merged)
        if job.privacy not in VALID_PRIVACY:
            raise ConfigError(f"job {jid}: privacy must be one of {sorted(VALID_PRIVACY)}")
        if job.latency not in VALID_LATENCY:
            raise ConfigError(f"job {jid}: latency must be one of {sorted(VALID_LATENCY)}")
        jobs.append(job)

    return Config(
        path=path,
        client_secrets=_resolve(base, yt.get("client_secrets", "client_secret.json")),
        token_file=_resolve(base, yt.get("token_file", "token.json")),
        state_file=_resolve(base, raw.get("state_file", "state.json")),
        log_dir=_resolve(base, raw.get("log_dir", "logs")),
        jobs=sorted(jobs, key=lambda j: j.start),
    )

"""launch.yaml loading and validation (spec Stage 1)."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import yaml


class LaunchConfigError(ValueError):
    pass


@dataclass
class SegmentConfig:
    slug: str
    name: str
    angle: str = ""
    leads: str = ""
    copy: str = ""
    priority: int = 100
    industry: str = ""
    keyword: str = ""
    employee_size: str = ""


@dataclass
class LintConfig:
    ban_em_dash: bool = True
    ban_exclamation: bool = False
    max_body_words: int = 120
    max_links: int = 1


@dataclass
class UnsubscribeConfig:
    mode: str = "append"  # append | require | off
    text: Optional[str] = None


@dataclass
class SegmentationConfig:
    """Auto-create one segment per unique value of a lead column (the 'List Name' flow)."""

    mode: str = ""  # "" = explicit segments | list_name
    column: str = ""
    source: str = "leads/all_leads.csv"
    name_template: str = "{value}"
    shared_copy: str = ""
    copy_map: dict[str, str] = field(default_factory=dict)
    exclude: list[str] = field(default_factory=list)
    min_leads: int = 1


@dataclass
class LaunchConfig:
    path: Path
    client: str
    settings_campaign: str
    provider: str = "instantly"
    variable_map: dict[str, str] = field(default_factory=dict)
    api_key_env: str = ""
    naming: str = "{client} | {segment} | {angle} | {date}"
    date_format: str = "%Y-%m-%d"
    lead_mode: str = "per_file"
    split_column: str = "segment"
    dedupe_across_segments: bool = True
    segments: list[SegmentConfig] = field(default_factory=list)
    lint: LintConfig = field(default_factory=LintConfig)
    unsubscribe: UnsubscribeConfig = field(default_factory=UnsubscribeConfig)
    segmentation: SegmentationConfig = field(default_factory=SegmentationConfig)
    segmentation_report: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def segment(self, slug: str) -> SegmentConfig:
        for s in self.segments:
            if s.slug == slug:
                return s
        raise KeyError(slug)


TOKENS = {
    "client",
    "segment",
    "angle",
    "date",
    "industry",
    "keyword",
    "employee_size",
    "batch",
}

APP_TIMEZONE = os.environ.get("TRI_LAUNCH_TZ", "Europe/Berlin")


def local_today() -> date:
    """Campaign dates use the operator's timezone, not the server's (often UTC)."""
    try:
        return datetime.now(ZoneInfo(APP_TIMEZONE)).date()
    except Exception:
        return date.today()


def load_launch(launch_dir: str | Path) -> LaunchConfig:
    d = Path(launch_dir)
    yml = d / "launch.yaml"
    if not yml.is_file():
        raise LaunchConfigError(f"launch.yaml not found in {d.resolve()}")
    try:
        data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise LaunchConfigError(f"launch.yaml is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise LaunchConfigError("launch.yaml must contain a mapping")

    segments = []
    for i, s in enumerate(data.get("segments") or []):
        if not isinstance(s, dict) or not s.get("slug"):
            raise LaunchConfigError(f"segments[{i}] needs at least a slug")
        segments.append(
            SegmentConfig(
                slug=str(s["slug"]).strip(),
                name=str(s.get("name") or s["slug"]).strip(),
                angle=str(s.get("angle") or "").strip(),
                leads=str(s.get("leads") or "").strip(),
                copy=str(s.get("copy") or "").strip(),
                priority=int(s.get("priority", 100)),
                industry=str(s.get("industry") or "").strip(),
                keyword=str(s.get("keyword") or "").strip(),
                employee_size=str(s.get("employee_size") or "").strip(),
            )
        )
    lint = data.get("lint") or {}
    unsub = data.get("unsubscribe") or {}
    seg_raw = data.get("segmentation") or {}
    cfg = LaunchConfig(
        path=d,
        client=str(data.get("client") or "").strip(),
        settings_campaign=str(data.get("settings_campaign") or "").strip(),
        provider=str(data.get("provider") or "instantly").strip().lower(),
        variable_map={
            str(k): str(v) for k, v in (data.get("variable_map") or {}).items()
        },
        api_key_env=str(data.get("api_key_env") or "").strip(),
        naming=str(data.get("naming") or "{client} | {segment} | {angle} | {date}"),
        date_format=str(data.get("date_format") or "%Y-%m-%d"),
        lead_mode=str(data.get("lead_mode") or "per_file"),
        split_column=str(data.get("split_column") or "segment"),
        dedupe_across_segments=bool(data.get("dedupe_across_segments", True)),
        segments=segments,
        lint=LintConfig(
            ban_em_dash=bool(lint.get("ban_em_dash", True)),
            ban_exclamation=bool(lint.get("ban_exclamation", False)),
            max_body_words=int(lint.get("max_body_words", 120)),
            max_links=int(lint.get("max_links", 1)),
        ),
        unsubscribe=UnsubscribeConfig(
            mode=str(unsub.get("mode") or "append").lower(),
            text=unsub.get("text"),
        ),
        segmentation=SegmentationConfig(
            mode=str(seg_raw.get("mode") or "").lower(),
            column=str(seg_raw.get("column") or "").strip(),
            source=str(seg_raw.get("source") or "leads/all_leads.csv").strip(),
            name_template=str(seg_raw.get("name_template") or "{value}"),
            shared_copy=str(seg_raw.get("shared_copy") or "").strip(),
            copy_map={str(k): str(v) for k, v in (seg_raw.get("copy_map") or {}).items()},
            exclude=[str(x) for x in (seg_raw.get("exclude") or [])],
            min_leads=int(seg_raw.get("min_leads", 1)),
        ),
        raw=data,
    )
    if cfg.segmentation.mode == "list_name" and not cfg.segments:
        _derive_segments(cfg)
    return cfg


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "segment"


def _read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    import csv as _csv

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = _csv.DictReader(fh)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        rows = [{k.strip(): (v or "") for k, v in row.items()} for row in reader]
    return headers, rows


def _derive_segments(cfg: LaunchConfig) -> None:
    """One segment per unique value in cfg.segmentation.column (the 'List Name' flow)."""
    seg = cfg.segmentation
    src = cfg.path / seg.source
    try:
        headers, rows = _read_csv_rows(src)
    except FileNotFoundError:
        cfg.segmentation_report = {"error": f"segmentation source not found: {seg.source}"}
        return
    column = seg.column
    if column not in headers:
        match = next((h for h in headers if h.lower() == column.lower()), None)
        if match is None:
            raise LaunchConfigError(
                f"segmentation.column {seg.column!r} not found in {seg.source}; "
                f"columns: {headers}"
            )
        column = match
    counts: dict[str, int] = {}
    for row in rows:
        value = (row.get(column) or "").strip()
        if value:
            counts[value] = counts.get(value, 0) + 1
    excluded = [
        v for v in counts if any(fnmatch.fnmatchcase(v.lower(), p.lower()) for p in seg.exclude)
    ]
    too_small = [v for v in counts if counts[v] < max(1, seg.min_leads) and v not in excluded]
    kept = sorted(
        ((v, c) for v, c in counts.items() if v not in excluded and v not in too_small),
        key=lambda item: (-item[1], item[0]),
    )
    used_slugs: set[str] = set()
    segments: list[SegmentConfig] = []
    for i, (value, _count) in enumerate(kept, start=1):
        slug = slugify(value)
        n = 2
        while slug in used_slugs:
            slug = f"{slugify(value)}-{n}"
            n += 1
        used_slugs.add(slug)
        copy = seg.copy_map.get(value) or seg.shared_copy or f"copy/{slug}.md"
        name = seg.name_template.replace("{value}", value)
        segments.append(
            SegmentConfig(
                slug=slug,
                name=name,
                leads=seg.source,
                copy=copy,
                priority=i,
            )
        )
    cfg.segments = segments
    cfg.lead_mode = "split_column"
    cfg.split_column = column
    cfg.segmentation_report = {
        "column": column,
        "values": len(counts),
        "created": [v for v, _ in kept],
        "excluded": excluded,
        "too_small": too_small,
        "counts": {v: counts[v] for v, _ in kept},
    }


def validate_launch(cfg: LaunchConfig, *, check_files: bool = True) -> list[str]:
    """Return blocking problems; empty list means the folder is valid."""
    issues: list[str] = []
    if not cfg.client:
        issues.append("client is required")
    if cfg.provider not in {"instantly", "outfalcon"}:
        issues.append(f"provider must be instantly or outfalcon, got {cfg.provider!r}")
    for variable, column in cfg.variable_map.items():
        if not str(variable).strip() or not str(column).strip():
            issues.append(f"variable_map entry {variable!r} needs a CSV column")
    if not cfg.settings_campaign:
        issues.append("settings_campaign is required")
    if not cfg.segments and cfg.segmentation.mode != "list_name":
        issues.append("at least one segment is required")
    if cfg.lead_mode not in {"per_file", "split_column"}:
        issues.append(f"lead_mode must be per_file or split_column, got {cfg.lead_mode!r}")
    if cfg.unsubscribe.mode not in {"append", "require", "off"}:
        issues.append(f"unsubscribe.mode must be append|require|off, got {cfg.unsubscribe.mode!r}")
    if cfg.segmentation.mode not in {"", "list_name"}:
        issues.append(
            f"segmentation.mode must be list_name (or omitted), got {cfg.segmentation.mode!r}"
        )
    if cfg.segmentation.mode == "list_name":
        if not cfg.segmentation.column:
            issues.append("segmentation.column is required when mode: list_name")
        if "error" in cfg.segmentation_report:
            issues.append(cfg.segmentation_report["error"])
        elif not cfg.segments:
            issues.append(
                "segmentation created no segments (all values excluded, below min_leads, or empty)"
            )
        if cfg.segmentation.shared_copy and check_files:
            if not (cfg.path / cfg.segmentation.shared_copy).is_file():
                issues.append(
                    f"segmentation.shared_copy not found: {cfg.segmentation.shared_copy}"
                )
    if not (1 <= cfg.lint.max_body_words <= 2000):
        issues.append("lint.max_body_words looks wrong")

    slugs = [s.slug for s in cfg.segments]
    if len(slugs) != len(set(slugs)):
        issues.append("segment slugs must be unique")

    tokens = set(re.findall(r"\{([a-z_]+)\}", cfg.naming))
    unknown = tokens - TOKENS
    if unknown:
        issues.append(f"naming uses unknown tokens: {sorted(unknown)}")

    copy_dir = cfg.path / "copy"
    for s in cfg.segments:
        if not s.copy:
            issues.append(f"segment {s.slug}: copy file is required")
        elif check_files and not (cfg.path / s.copy).is_file() and not (copy_dir / s.copy).is_file():
            issues.append(f"segment {s.slug}: copy file not found: {s.copy}")
        if cfg.lead_mode == "per_file":
            if not s.leads:
                issues.append(f"segment {s.slug}: leads file is required in per_file mode")
            elif check_files and not (cfg.path / s.leads).is_file():
                issues.append(f"segment {s.slug}: leads file not found: {s.leads}")
    if cfg.lead_mode == "split_column":
        if not cfg.split_column:
            issues.append("split_column is required in split_column mode")
        source = (
            cfg.segmentation.source
            if cfg.segmentation.mode == "list_name"
            else "leads/all_leads.csv"
        )
        if check_files and not (cfg.path / source).is_file():
            issues.append(f"lead_mode=split_column but {source} not found")
    return issues


def render_campaign_name(
    cfg: LaunchConfig, segment: SegmentConfig, *, on: Optional[date] = None, batch: str = ""
) -> str:
    values = {
        "client": cfg.client,
        "segment": segment.name or segment.slug,
        "angle": segment.angle,
        "date": (on or local_today()).strftime(cfg.date_format),
        "industry": segment.industry,
        "keyword": segment.keyword,
        "employee_size": segment.employee_size,
        "batch": batch,
    }
    try:
        name = cfg.naming.format(**values)
    except KeyError as exc:
        raise LaunchConfigError(f"naming token {exc} has no value") from exc
    return re.sub(r"\s*\|\s*\|\s*", " | ", name).strip(" |")

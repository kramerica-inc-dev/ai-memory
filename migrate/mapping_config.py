#!/usr/bin/env python3
"""
mapping_config.py — load the content-layer namespace mapping for the ingest scripts.

The migration/ingest scripts are content-agnostic: which namespaces exist and how
directories/projects route into them lives in a config file, never in the code.

Resolution order for the config path:
  1. explicit `path` argument
  2. env var MAPPING_CONFIG
  3. default: <repo-root>/config/mapping.yaml

Fails closed: if no config file is found, a MappingConfigError is raised (the caller
should not silently fall back to a hard-coded taxonomy). Copy config/mapping.example.yaml
to config/mapping.yaml to get started.

PyYAML is used when available; otherwise a tiny built-in parser handles the simple
subset used by mapping.yaml (top-level scalars, a `- item` list, and a nested `key: value`
map) so the scripts stay dependency-free.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "mapping.yaml"
DEFAULT_CONF = REPO_ROOT / "memctl.conf"


class MappingConfigError(RuntimeError):
    """The mapping config is missing or malformed — ingest is blocked."""


def apply_conf(path: str | os.PathLike | None = None) -> None:
    """Load KEY=VALUE pairs from memctl.conf into os.environ as *defaults*.

    memctl.sh and backup.sh source memctl.conf; the Python ingest tools call this so a
    bare `python3 migrate/<tool>.py` honors the same deployment config (MEMORY_HOST,
    MCP_URL, SANITIZER_URL, ARTIFACTS_DIR, …) without re-exporting. Existing environment
    variables always win, so explicit overrides keep working. Override the file location
    with MEMCTL_CONF. Missing file = no-op (env/defaults apply, same as the shell tools).
    """
    cfg = Path(path or os.environ.get("MEMCTL_CONF") or DEFAULT_CONF)
    if not cfg.exists():
        return
    for raw in cfg.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


class Mapping:
    def __init__(self, default_group: str, groups: list[str], group_map: dict[str, str]):
        self.default_group = default_group
        self.groups = groups
        self.group_map = group_map

    def group_for(self, key: str) -> str:
        return self.group_map.get(key, self.default_group)


def _parse_minimal(text: str) -> dict:
    """Parse the small YAML subset used by mapping.yaml without PyYAML.

    Supports:
      key: value                 (top-level scalar)
      key:                       (start of a block)
        - item                   (list item under the current block)
        subkey: value            (map entry under the current block)
    Ignores blank lines and lines whose first non-space character is '#'.
    """
    data: dict = {}
    current_key = None
    current_kind = None  # "list" | "map"
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[0] in (" ", "\t")
        stripped = line.strip()
        if not indented:
            # top-level key
            if ":" not in stripped:
                raise MappingConfigError(f"cannot parse line: {raw!r}")
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "":
                current_key = key
                current_kind = None
                data[key] = None  # filled in as children are read
            else:
                data[key] = _scalar(val)
                current_key = None
                current_kind = None
            continue
        # indented -> child of current_key
        if current_key is None:
            raise MappingConfigError(f"indented line without a parent key: {raw!r}")
        if stripped.startswith("- "):
            if current_kind is None:
                data[current_key] = []
                current_kind = "list"
            if current_kind != "list":
                raise MappingConfigError(f"mixed list/map under {current_key!r}")
            data[current_key].append(_scalar(stripped[2:].strip()))
        else:
            if ":" not in stripped:
                raise MappingConfigError(f"cannot parse line: {raw!r}")
            if current_kind is None:
                data[current_key] = {}
                current_kind = "map"
            if current_kind != "map":
                raise MappingConfigError(f"mixed list/map under {current_key!r}")
            k, _, v = stripped.partition(":")
            data[current_key][k.strip()] = _scalar(v.strip())
    return data


def _scalar(val: str):
    # strip an inline comment that is clearly separated by whitespace
    if " #" in val:
        val = val.split(" #", 1)[0].strip()
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    return val


def _load_raw(text: str) -> dict:
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        return _parse_minimal(text)


def load_mapping(path: str | os.PathLike | None = None) -> Mapping:
    cfg = Path(path or os.environ.get("MAPPING_CONFIG") or DEFAULT_CONFIG)
    if not cfg.exists():
        raise MappingConfigError(
            f"mapping config not found at {cfg}. "
            "Copy config/mapping.example.yaml to config/mapping.yaml (or set MAPPING_CONFIG)."
        )
    data = _load_raw(cfg.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MappingConfigError(f"{cfg}: top level must be a mapping")

    default_group = data.get("default_group")
    groups = data.get("groups")
    group_map = data.get("group_map") or {}
    if not default_group or not isinstance(default_group, str):
        raise MappingConfigError(f"{cfg}: 'default_group' is required (a string)")
    if not groups or not isinstance(groups, list):
        raise MappingConfigError(f"{cfg}: 'groups' is required (a list of namespaces)")
    if not isinstance(group_map, dict):
        raise MappingConfigError(f"{cfg}: 'group_map' must be a mapping of key -> group_id")

    groups = [str(g) for g in groups]
    group_map = {str(k): str(v) for k, v in group_map.items()}
    if default_group not in groups:
        groups.append(default_group)
    return Mapping(default_group, groups, group_map)

"""
YAML configuration loader for crawler sources.

Features:
- ${ENV_VAR} substitution for secrets (never put secrets in config files)
- Strict validation via Pydantic (unknown keys → error)
- Business logic validation beyond schema
"""
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import yaml
from pydantic import ValidationError

from .models import Source


_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _substitute_env(value: Any) -> Any:
    """Recursively substitute ${VAR} with os.environ values."""
    if isinstance(value, str):
        def replace(match):
            var = match.group(1)
            val = os.environ.get(var)
            if val is None:
                # Leave as placeholder — don't fail at load time
                # (integration tests may not have real credentials)
                return f"__ENV_{var}__"
            return val
        return _ENV_VAR_RE.sub(replace, value)
    elif isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_substitute_env(item) for item in value]
    return value


# Fields in YAML that are not part of the Source model but are valid connector metadata
_ALLOWED_EXTRA_KEYS = {"api", "docs", "notes"}


def load_source_config(path: str | Path) -> Source:
    """
    Load and parse a Source configuration from a YAML file.
    - Substitutes ${ENV_VAR} placeholders
    - Strips connector-specific extra keys not in the Source model
    - Raises ValueError or ValidationError on failure
    """
    path_obj = Path(path)
    if not path_obj.is_file():
        raise FileNotFoundError(f"Config file not found: {path_obj}")

    with path_obj.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML content in {path_obj}: expected a mapping")

    # Substitute env vars
    data = _substitute_env(data)

    # Strip extra connector-specific keys that aren't in the Source model
    for key in list(data.keys()):
        if key in _ALLOWED_EXTRA_KEYS:
            data.pop(key)

    # Strict model validation
    return Source.model_validate(data)


def validate_source(source: Source) -> List[str]:
    """
    Validate a Source model for business logic rules beyond schema validation.
    Returns a list of error strings (empty if valid).
    """
    errors = []

    if not source.discovery and source.access_class.value not in ("api",):
        # API sources don't need discovery config (they have their own sync)
        pass

    for idx, disc in enumerate(source.discovery):
        if not disc.urls and not disc.url:
            errors.append(f"Discovery config [{idx}] must specify at least one url or urls list.")

    if source.politeness.max_concurrency_per_host < 1:
        errors.append("max_concurrency_per_host must be at least 1.")

    # §4.2: restricted/api without authorization_ref must fail
    if source.access_class.value in ("restricted", "api") and not source.authorization_ref:
        errors.append(
            f"Source '{source.source_id}' has access_class='{source.access_class.value}' "
            f"but no authorization_ref. This is required by §4.2."
        )

    return errors

import json
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "mcp_runtime_config.json"

DEFAULT_CONFIG = {
    "playwright_browsers_path": ".ms-playwright",
    "override_playwright_browsers_path": True,
    "set_cwd_to_project_root": True,
    "allow_headful_fallback": False,
    "allow_headful_fallback_platforms": [],
    "manual_verification_timeout_seconds": 180,
    "library_root": ".repo",
    "library_enabled": True,
    "library_web_host": "127.0.0.1",
    "library_web_port": 5577,
}


def project_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str(PROJECT_ROOT / path)


def load_runtime_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config.update(loaded)
        except Exception:
            pass
    return config


def ensure_runtime_environment() -> dict:
    config = load_runtime_config()

    if config.get("set_cwd_to_project_root", True):
        os.chdir(PROJECT_ROOT)

    browsers_path = str(config.get("playwright_browsers_path") or ".ms-playwright")
    resolved_browsers_path = project_path(browsers_path)
    if config.get("override_playwright_browsers_path", True):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = resolved_browsers_path
    else:
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", resolved_browsers_path)
    os.environ.setdefault("PYTHONUTF8", "1")
    return config


def allow_headful_fallback_for(platform: str) -> bool:
    config = load_runtime_config()
    if bool(config.get("allow_headful_fallback", False)):
        return True
    platforms = config.get("allow_headful_fallback_platforms") or []
    return str(platform or "").upper() in {str(item).upper() for item in platforms}


def manual_verification_timeout_seconds() -> int:
    config = load_runtime_config()
    try:
        return max(30, int(config.get("manual_verification_timeout_seconds", 180)))
    except Exception:
        return 180


def library_root_path() -> Path:
    config = load_runtime_config()
    raw = str(config.get("library_root") or ".repo")
    return Path(project_path(raw))


def library_enabled() -> bool:
    config = load_runtime_config()
    return bool(config.get("library_enabled", True))


def library_web_host() -> str:
    config = load_runtime_config()
    return str(config.get("library_web_host") or "127.0.0.1")


def library_web_port() -> int:
    config = load_runtime_config()
    try:
        return int(config.get("library_web_port", 5577))
    except Exception:
        return 5577

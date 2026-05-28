from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ENV_PATH = Path(".env")
DEBUG_DIR = Path("debug")
CONVERSATION_DIR = DEBUG_DIR / "browser-use"
BROWSER_USE_VENV_DIR = Path(".venv-browser-use")
BROWSER_USE_SENTINEL = "BROWSER_USE_PYTHON_BOOTSTRAPPED"
MEET_CODE_PATTERN = re.compile(r"meet\.google\.com/([a-z0-9-]+)", re.IGNORECASE)


def load_dotenv():
    if not ENV_PATH.exists():
        return

    for raw_line in ENV_PATH.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')

        if key and key not in os.environ:
            os.environ[key] = value


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return int(value)


def log(message, session_id=None):
    prefix = "[browser-use-meet]" if not session_id else f"[browser-use-meet:{session_id}]"
    print(f"{prefix} {message}")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def normalize_message_text(text):
    return re.sub(r"\s+", " ", text).strip()


def extract_meeting_code(meet_url):
    match = MEET_CODE_PATTERN.search(meet_url or "")
    if match:
        return match.group(1).lower()
    return "unknown-meet"


def find_supported_python():
    candidates = [
        "python3.13",
        "python3.12",
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
        "/usr/local/bin/python3.13",
        "/usr/local/bin/python3.12",
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3.12",
    ]

    for candidate in candidates:
        resolved = shutil.which(candidate) if "/" not in candidate else candidate
        if not resolved or not Path(resolved).exists():
            continue

        try:
            version = subprocess.check_output(
                [resolved, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
                text=True,
            ).strip()
        except Exception:
            continue

        if version in {"3.12", "3.13"}:
            return resolved

    return None


def venv_python_path():
    return BROWSER_USE_VENV_DIR / "bin" / "python"


def ensure_browser_use_venv(argv=None, extra_imports=None):
    if sys.version_info < (3, 14):
        return

    if os.environ.get(BROWSER_USE_SENTINEL) == "1":
        version = ".".join(str(part) for part in sys.version_info[:3])
        raise RuntimeError(
            "Browser Use re-launch was attempted, but the interpreter is still "
            f"Python {version}. Check the dedicated venv at "
            f"{BROWSER_USE_VENV_DIR}."
        )

    supported_python = find_supported_python()
    if not supported_python:
        version = ".".join(str(part) for part in sys.version_info[:3])
        raise RuntimeError(
            "browser-use currently fails in this setup on Python "
            f"{version}. Install Python 3.12 or 3.13, then re-run this script."
        )

    target_python = venv_python_path()
    if not target_python.exists():
        log(f"Creating Browser Use venv with {supported_python}")
        subprocess.run(
            [supported_python, "-m", "venv", str(BROWSER_USE_VENV_DIR)],
            check=True,
        )

    import_check = "import browser_use, playwright"
    if extra_imports:
        import_check = f"{import_check}, {', '.join(extra_imports)}"

    try:
        subprocess.run(
            [
                str(target_python),
                "-c",
                import_check,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        raise RuntimeError(
            "The Browser Use venv exists but dependencies are missing. Run "
            f"`{target_python} -m pip install -r requirements.txt` and retry."
        )

    log(f"Re-launching with {target_python}")
    relaunched_env = os.environ.copy()
    relaunched_env[BROWSER_USE_SENTINEL] = "1"
    argv_to_run = list(argv or sys.argv)
    os.execve(
        str(target_python),
        [str(target_python), *argv_to_run],
        relaunched_env,
    )


def assert_supported_python(script_name="scripts/join_meet.py"):
    if sys.version_info >= (3, 14):
        version = ".".join(str(part) for part in sys.version_info[:3])
        raise RuntimeError(
            "browser-use currently fails in this setup on Python "
            f"{version}. Create a Python 3.12 or 3.13 virtualenv for "
            f"{script_name}."
        )


def ensure_browser_use_runtime(script_name="scripts/join_meet.py", extra_imports=None):
    load_dotenv()
    ensure_browser_use_venv(extra_imports=extra_imports)
    assert_supported_python(script_name)

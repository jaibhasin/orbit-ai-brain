import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path


ENV_PATH = Path(".env")
DEBUG_DIR = Path("debug")
CONVERSATION_DIR = DEBUG_DIR / "browser-use"
BROWSER_USE_VENV_DIR = Path(".venv-browser-use")
BROWSER_USE_SENTINEL = "BROWSER_USE_PYTHON_BOOTSTRAPPED"


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


def log(message):
    print(f"[browser-use-meet] {message}")


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


def ensure_browser_use_venv():
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

    try:
        subprocess.run(
            [
                str(target_python),
                "-c",
                "import browser_use, playwright",
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
    os.execve(
        str(target_python),
        [str(target_python), str(Path(__file__).resolve())],
        relaunched_env,
    )


def build_task(meet_url, display_name):
    return f"""
Open this Google Meet URL: {meet_url}

This automation is only allowed to join meetings I already have permission to join. Do not attempt to bypass sign-in, host approval, guest restrictions, or any Google security control.

Steps:
1. Stay on the Google Meet page for this URL.
2. If a modal asks whether people should see or hear you, click "Continue without microphone and camera". If that exact button is not available, choose the option that keeps microphone and camera disabled.
3. If a visible guest name field exists, fill it with "{display_name}".
4. If microphone or camera toggles are on in the pre-join screen, turn them off.
5. Click the best available join button, preferring "Ask to join", then "Join now", then "Request to join", then "Join".
6. Treat the guest pre-join area as the source of truth. If the page shows a name input and a join button, continue the guest join flow even if a top-right "Sign in" link, tooltip, or helper bubble is also visible.
7. Do not treat a generic top-right "Sign in" link or tooltip as a blocking condition by itself. Only treat sign-in as blocking if the main page content explicitly says sign-in is required or the meeting cannot be joined without it.
8. If the page says host approval is required, the request was denied, or the meeting cannot be joined, stop and report the exact visible reason.
9. After clicking the join button, remain on the meeting page and do not navigate away.
10. Finish only after you have either joined successfully or clearly determined that Google Meet blocked entry.
""".strip()


def assert_supported_python():
    if sys.version_info >= (3, 14):
        version = ".".join(str(part) for part in sys.version_info[:3])
        raise RuntimeError(
            "browser-use currently fails in this setup on Python "
            f"{version}. Create a Python 3.12 or 3.13 virtualenv for "
            "join_meet_browser_use.py."
        )


def build_browser(Browser):
    headless = env_bool("HEADLESS", False)
    use_system_chrome = env_bool("GMEET_USE_SYSTEM_CHROME", False)
    profile_directory = os.environ.get("GMEET_CHROME_PROFILE_DIRECTORY")

    if use_system_chrome:
        log("Using Browser Use with your installed Chrome profile.")
        if profile_directory:
            log(f"Requested Chrome profile: {profile_directory}")
            return Browser.from_system_chrome(
                profile_directory=profile_directory,
                keep_alive=True,
            )
        return Browser.from_system_chrome(keep_alive=True)

    log("Using Browser Use managed browser session for guest join flow.")
    return Browser(
        headless=headless,
        keep_alive=True,
        window_size={"width": 1440, "height": 960},
    )


async def main():
    load_dotenv()
    ensure_browser_use_venv()
    assert_supported_python()

    from browser_use import Agent, Browser, ChatOpenAI

    meet_url = os.environ.get("GMEET_URL")
    display_name = os.environ.get("GMEET_DISPLAY_NAME", "Orbit Agent")
    wait_after_run_ms = env_int("GMEET_WAIT_AFTER_JOIN_MS", 120000)
    max_steps = env_int("GMEET_BROWSER_USE_MAX_STEPS", 20)
    model_name = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
    openai_api_key = os.environ.get("OPENAI_API_KEY")

    if not meet_url:
        raise RuntimeError("Missing GMEET_URL in .env or environment.")
    if not openai_api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in .env or environment.")

    DEBUG_DIR.mkdir(exist_ok=True)
    CONVERSATION_DIR.mkdir(parents=True, exist_ok=True)

    llm = ChatOpenAI(model=model_name)
    browser = build_browser(Browser)

    agent = Agent(
        task=build_task(meet_url, display_name),
        llm=llm,
        browser=browser,
        use_vision=True,
        max_failures=3,
        save_conversation_path=str(CONVERSATION_DIR),
        generate_gif=str(DEBUG_DIR / "browser-use-meet.gif"),
    )

    log(f"Opening Meet URL: {meet_url}")
    log(f"Model: {model_name}")
    log(f"Agent max steps: {max_steps}")
    history = None

    try:
        history = await agent.run(max_steps=max_steps)
        log(f"Agent finished after {history.number_of_steps()} steps.")

        final_result = history.final_result()
        if final_result:
            log(f"Final result: {final_result}")

        if history.has_errors():
            log("Agent reported one or more step errors. Check debug/browser-use.")

        if history.is_successful():
            log("Browser Use marked the run as successful.")
        else:
            log("Browser Use did not mark the run as successful.")

        log(
            f"Keeping browser open for {wait_after_run_ms // 1000} seconds for inspection."
        )
        await asyncio.sleep(wait_after_run_ms / 1000)
    finally:
        log("Closing browser.")
        await browser.kill()


if __name__ == "__main__":
    asyncio.run(main())

"""
ai/providers/browser_chat.py
============================

Generic "send a request file, read the JSON answer" automation for a chat web
UI, reusing the same Chrome remote-debugger session (127.0.0.1:9222) that the
existing JobBot scripts use. It never starts, logs in to, or closes Chrome.

Used by:
* call_chatgpt_web() for every operation.
* call_claude_web() for recruiter e-mail operations.

Selectors live in config/ai_provider_settings.json because web UIs change
often. Every UI failure is mapped to ProviderUnavailableError so the router
can move on; input/output defects are not.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import time
from pathlib import Path
from typing import Any, Callable

from ai.errors import FailureType, InputValidationError, ProviderUnavailableError

CHROME_DEBUGGER_ADDRESS = os.getenv("CHROME_DEBUGGER_ADDRESS", "127.0.0.1:9222")
logger = logging.getLogger(__name__)

_LOGIN_MARKERS = ("/login", "/auth", "/oauth", "accounts.google.com", "auth.openai.com")


def debugger_reachable(address: str = CHROME_DEBUGGER_ADDRESS, timeout: float = 1.5) -> bool:
    """Only trust an actual Chrome DevTools JSON response (not a random open port)."""
    try:
        url = f"http://{address}/json/version"
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.load(response)
        return bool(isinstance(data, dict) and data.get('Browser') and data.get('webSocketDebuggerUrl'))
    except (OSError, ValueError, TypeError):
        return False


def start_debug_chrome() -> bool:
    """
    Start Chrome with remote debugging when it is not running yet.
    CHROME_PATH and CHROME_USER_DATA_DIR come from .env. The profile directory
    keeps your Claude / ChatGPT login between runs (log in once by hand).
    Set CHROME_AUTO_START=false to never start Chrome from the API.
    """
    import subprocess

    if os.getenv("CHROME_AUTO_START", "true").strip().lower() in {"0", "false", "no", "off"}:
        return False
    chrome = os.getenv("CHROME_PATH", "").strip() or (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe" if os.name == "nt" else "google-chrome")
    profile = os.getenv("CHROME_USER_DATA_DIR", "").strip() or str(
        Path(__file__).resolve().parents[2] / "data" / "chrome-profile")
    Path(profile).mkdir(parents=True, exist_ok=True)
    port = CHROME_DEBUGGER_ADDRESS.partition(":")[2] or "9222"
    subprocess.Popen([chrome, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile}",
                      "--no-first-run", "--disable-extensions"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(15):
        time.sleep(1)
        if debugger_reachable():
            return True
    return False


def ensure_browser(
    provider: str,
    bootstrap: Callable[[], bool] | None = start_debug_chrome,
    address: str = CHROME_DEBUGGER_ADDRESS,
) -> None:
    """Raise ProviderUnavailableError when the debugger Chrome is not usable."""
    if debugger_reachable(address):
        return
    if bootstrap is not None:
        try:
            if bootstrap() and debugger_reachable(address):
                return
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailableError(
                f"Could not start Chrome debugger: {exc}",
                failure_type=FailureType.BROWSER_UNAVAILABLE,
                provider=provider,
                retryable=False,
            ) from exc
    raise ProviderUnavailableError(
        f"Chrome remote debugger is not reachable at {address}.",
        failure_type=FailureType.BROWSER_UNAVAILABLE,
        provider=provider,
        retryable=False,
    )


def map_browser_exception(exc: BaseException, provider: str, page: str = "",
                          markers: list[str] | None = None) -> Exception:
    """Map Selenium/UI failures to the JobBot hierarchy."""
    if isinstance(exc, (InputValidationError,)):
        return exc  # type: ignore[return-value]
    if isinstance(exc, (FileNotFoundError, NotADirectoryError, ValueError)):
        return InputValidationError(str(exc), provider=provider)
    if isinstance(exc, ProviderUnavailableError):
        return exc  # type: ignore[return-value]

    message = str(exc)
    lowered = f"{message} {page}".lower()

    if any(marker in lowered for marker in ("requires login", "log in", "verification")):
        failure = FailureType.BROWSER_SESSION_EXPIRED
    elif any(marker in lowered for marker in (markers or [])):
        failure = FailureType.WEB_USAGE_LIMIT
    elif "could not attach to chrome" in lowered or "cannot connect to chrome" in lowered:
        failure = FailureType.BROWSER_UNAVAILABLE
    else:
        failure = FailureType.BROWSER_UI_TIMEOUT

    selenium_error = False
    try:
        from selenium.common.exceptions import WebDriverException

        selenium_error = isinstance(exc, WebDriverException)
    except ImportError:  # pragma: no cover
        pass

    if selenium_error or isinstance(exc, (TimeoutError, RuntimeError, ConnectionError)):
        return ProviderUnavailableError(
            f"{type(exc).__name__}: {message}",
            failure_type=failure,
            provider=provider,
            retryable=False,
        )
    return exc  # unknown -> router treats as ApplicationError (no fallback)


def write_web_request_file(directory: Path, name: str, system_prompt: str, user_prompt: str,
                           schema: dict[str, Any] | None, preamble: str = "") -> Path:
    """Write the canonical request (same text the APIs receive) to one file."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    parts = ["# JOBBOT AI REQUEST", ""]
    if preamble:
        parts += [preamble, ""]
    parts += [
        "## INSTRUCTIONS",
        system_prompt,
        "",
        "## TASK INPUT",
        user_prompt,
    ]
    if schema is not None:
        parts += [
            "",
            "## REQUIRED OUTPUT",
            "Reply with ONE JSON object only, inside a single ```json code block,"
            " that validates against this JSON schema. No prose before or after.",
            "```json",
            json.dumps(schema, indent=2),
            "```",
        ]
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Selenium helpers: plain functions over a small "chat" dict
#   chat = {"driver", "provider", "selectors", "markers"}
# ---------------------------------------------------------------------------

def attach_driver(provider: str, address: str = CHROME_DEBUGGER_ADDRESS) -> Any:
    """Attach Selenium to the already running debugger Chrome."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_experimental_option("debuggerAddress", address)
    try:
        driver = webdriver.Chrome(options=options)
    except Exception as exc:  # noqa: BLE001
        raise ProviderUnavailableError(
            f"Could not attach to Chrome at {address}: {exc}",
            failure_type=FailureType.BROWSER_UNAVAILABLE, provider=provider, retryable=False,
        ) from exc
    driver.set_page_load_timeout(40)
    return driver


def detach_driver(driver: Any) -> None:
    """Disconnect Selenium only; never close the user's debugger Chrome."""
    if driver is None:
        return
    try:
        driver.service.stop()
    except Exception:  # noqa: BLE001
        pass


def page_text(chat: dict[str, Any]) -> str:
    try:
        return chat["driver"].execute_script("return document.body ? document.body.innerText : ''") or ""
    except Exception:  # noqa: BLE001
        return ""


def visible_elements(chat: dict[str, Any], key: str) -> list[Any]:
    from selenium.webdriver.common.by import By

    for css in chat["selectors"].get(key, []):
        try:
            found = [e for e in chat["driver"].find_elements(By.CSS_SELECTOR, css)
                     if key == "file_input" or e.is_displayed()]
        except Exception:  # noqa: BLE001
            continue
        if found:
            return found
    return []


def check_login(chat: dict[str, Any]) -> None:
    url = (chat["driver"].current_url or "").lower()
    if any(marker in url for marker in _LOGIN_MARKERS):
        raise ProviderUnavailableError(
            f"{chat['provider']} requires login in the debugger Chrome.",
            failure_type=FailureType.BROWSER_SESSION_EXPIRED, provider=chat["provider"], retryable=False,
        )


def wait_for_element(chat: dict[str, Any], key: str, timeout: float = 30.0) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        check_login(chat)
        items = visible_elements(chat, key)
        if items:
            return items[-1]
        time.sleep(0.4)
    raise TimeoutError(f"{chat['provider']}: element '{key}' not found within {timeout:.0f}s")


def open_new_chat(chat: dict[str, Any], url: str) -> None:
    chat["driver"].switch_to.new_window("tab")
    # Remember the exact automation-owned handle immediately. Never close user tabs.
    chat["automation_tab_handle"] = chat["driver"].current_window_handle
    chat["driver"].get(url)
    time.sleep(2.0)
    check_login(chat)
    wait_for_element(chat, "composer", timeout=35)


def attach_files(chat: dict[str, Any], paths: list[Path]) -> None:
    for path in paths:
        if not Path(path).is_file():
            raise InputValidationError(f"Attachment not found: {path}", provider=chat["provider"])
        wait_for_element(chat, "file_input", timeout=20).send_keys(str(Path(path).resolve()))
        time.sleep(2.0)


def type_and_send(chat: dict[str, Any], prompt: str) -> None:
    from selenium.webdriver.common.keys import Keys

    composer = wait_for_element(chat, "composer", timeout=20)
    composer.click()
    composer.send_keys(Keys.CONTROL, "a")
    composer.send_keys(Keys.BACKSPACE)
    composer.send_keys(prompt)
    time.sleep(1.5)
    try:
        wait_for_element(chat, "send_button", timeout=30).click()
    except TimeoutError:
        composer.send_keys(Keys.ENTER)


def wait_for_response(chat: dict[str, Any], baseline_count: int, timeout: float) -> str:
    """Wait until a new assistant message stops changing, then return its text."""
    deadline = time.time() + timeout
    last_text, stable_polls = "", 0
    while time.time() < deadline:
        check_login(chat)
        messages = visible_elements(chat, "assistant_message")
        streaming = bool(visible_elements(chat, "stop_button"))
        if len(messages) > baseline_count and not streaming:
            text = messages[-1].get_attribute("innerText") or messages[-1].text or ""
            if text and text == last_text:
                stable_polls += 1
                if stable_polls >= 3:
                    return text
            else:
                stable_polls, last_text = 0, text
        time.sleep(2.0)
    page = page_text(chat).lower()
    failure = (FailureType.WEB_USAGE_LIMIT if any(m in page for m in chat["markers"])
               else FailureType.BROWSER_UI_TIMEOUT)
    raise ProviderUnavailableError(
        f"{chat['provider']}: no complete response within {timeout:.0f}s.",
        failure_type=failure, provider=chat["provider"], retryable=False,
    )


def ensure_inside_project(chat: dict[str, Any], project: dict[str, Any] | None, document: str) -> None:
    """Raise PROJECT_KNOWLEDGE_UNAVAILABLE unless the tab is inside the configured Project."""
    if not project or not project.get("configured"):
        return
    from ai.knowledge_prompts import verify_project_page

    problem = verify_project_page(chat["driver"].current_url or "", page_text(chat), project, document)
    if problem:
        raise ProviderUnavailableError(
            f"{chat['provider']}: {problem}. Check the Project URL in .env and that the Project is shared "
            "with the logged-in account.",
            failure_type=FailureType.PROJECT_KNOWLEDGE_UNAVAILABLE, provider=chat["provider"], retryable=False,
        )


def run_chat_json_request(
    *,
    provider: str,
    chat_url: str,
    selectors: dict[str, list[str]],
    markers: list[str],
    request_file: Path,
    extra_attachments: list[Path],
    timeout_seconds: float,
    message: str | None = None,
    project: dict[str, Any] | None = None,
    knowledge_document: str = "experience_details.md",
) -> tuple[str, str]:
    """
    Send the request file in a new chat. Return (assistant_text, chat_url).
    With ``project`` configured, ``chat_url`` is the Project page: the chat is
    created inside that Project after verifying the page really is the Project.
    """
    ensure_browser(provider)
    chat = {"driver": attach_driver(provider), "provider": provider, "selectors": selectors, "markers": markers}
    # Selenium attaches to the user's browser: remember existing handles so only
    # the new automation tab is closed after response/failure.
    previous_handle = None
    existing_handles: set[str] = set()
    try:
        previous_handle = chat["driver"].current_window_handle
        existing_handles = set(chat["driver"].window_handles)
        open_new_chat(chat, chat_url)
        ensure_inside_project(chat, project, knowledge_document)
        baseline = len(visible_elements(chat, "assistant_message"))
        attach_files(chat, [request_file, *extra_attachments])
        type_and_send(
            chat,
            message or (f"Read the attached file {request_file.name} completely and follow it exactly. "
                        "Reply with ONE JSON object inside a single ```json code block and nothing else."),
        )
        text = wait_for_response(chat, baseline, timeout_seconds)
        return text, chat["driver"].current_url or chat_url
    except Exception as exc:  # noqa: BLE001
        mapped = map_browser_exception(exc, provider, page_text(chat), markers)
        if mapped is exc:
            raise
        raise mapped from exc
    finally:
        # Close only the tab created by this request; preserve the user's tabs.
        driver = chat["driver"]
        try:
            owned = chat.get("automation_tab_handle")
            if not owned and set(driver.window_handles) - existing_handles:
                # Do not guess which newly opened tab belongs to the automation:
                # it could be an unrelated tab the user opened during the request.
                logger.warning("AI tab ownership unknown; preserving browser tabs")
            if owned:
                closed = owned not in driver.window_handles
                for attempt in range(2):
                    if closed:
                        break
                    try:
                        driver.switch_to.window(owned)
                        driver.close()
                        closed = owned not in driver.window_handles
                    except Exception as close_exc:  # noqa: BLE001
                        logger.warning("Could not close recruiter AI tab (attempt %s): %s",
                                       attempt + 1, type(close_exc).__name__)
                    if not closed:
                        time.sleep(0.1)
                if not closed and owned.startswith("CDwindow-"):
                    # ChromeDriver window handles expose target IDs; CDP is a
                    # fallback if Selenium's close() silently fails.
                    try:
                        driver.execute_cdp_cmd("Target.closeTarget", {"targetId": owned[len("CDwindow-"):]})
                        closed = owned not in driver.window_handles
                    except Exception as cdp_exc:  # noqa: BLE001
                        logger.warning("Chrome DevTools tab cleanup failed: %s", type(cdp_exc).__name__)
                if closed:
                    logger.info("Closed recruiter AI automation tab")
                else:
                    logger.warning("Recruiter AI automation tab remains open after cleanup")
        except Exception as cleanup_exc:  # noqa: BLE001
            logger.warning("Recruiter AI tab cleanup error: %s", type(cleanup_exc).__name__)
        finally:
            try:
                if previous_handle and previous_handle in driver.window_handles:
                    driver.switch_to.window(previous_handle)
            except Exception:  # noqa: BLE001
                logger.warning("Could not restore previous Chrome tab after AI request")
            detach_driver(driver)


def run_web_request(provider: str, request: Any, settings: dict[str, Any], default_url: str,
                    chat_runner: Callable[..., tuple[str, str]] | None = None,
                    env: dict[str, str] | None = None) -> dict[str, Any]:
    """
    Shared generic chat flow of Claude Web / ChatGPT Web.

    Without ``request.knowledge``: unchanged behaviour (new chat at default_url,
    attach the request file, read the JSON answer).

    With ``request.knowledge`` (recruiter e-mail): open the configured Project,
    verify it, reference the Project knowledge file explicitly in the request
    file and the typed message, and attach the local excerpts as fallback.
    """
    import uuid

    from ai.json_utils import parse_json_object
    from ai.knowledge_prompts import chat_message, project_config, request_preamble
    from ai.observability import ai_runs_root, today_ist
    from ai.provider_result import make_result

    runner = chat_runner or run_chat_json_request
    knowledge = dict(getattr(request, "knowledge", {}) or {})
    project = project_config(provider, settings, env) if knowledge else None
    if project and project["required"] and not project["configured"]:
        raise ProviderUnavailableError(
            f"{provider}: RECRUITER_REQUIRE_PROJECT_KNOWLEDGE=true but no Project URL is configured "
            f"({settings.get('project_url_env')}).",
            failure_type=FailureType.PROJECT_KNOWLEDGE_UNAVAILABLE, provider=provider, retryable=False)
    url = project["url"] if project and project["configured"] else default_url

    request_file = write_web_request_file(
        ai_runs_root() / today_ist() / "web_requests", f"{request.operation}_{uuid.uuid4().hex[:8]}.md",
        request.system_prompt, request.user_prompt, request.output_schema,
        preamble=request_preamble(provider, knowledge, project),
    )
    kwargs: dict[str, Any] = {
        "provider": provider, "chat_url": url, "selectors": settings.get("selectors", {}),
        "markers": settings.get("markers", []), "request_file": request_file,
        "extra_attachments": list(request.attachments),
        "timeout_seconds": float(settings.get("response_timeout_seconds", 900)),
    }
    if knowledge:
        kwargs.update(message=chat_message(provider, knowledge, request_file.name, project), project=project,
                      knowledge_document=knowledge.get("document") or "experience_details.md")
    text, chat_url = runner(**kwargs)
    data = parse_json_object(text, provider=provider) if request.output_schema else None
    handoff: dict[str, Any] = {"chat_url": chat_url, "request_file": str(request_file)}
    if knowledge:
        handoff["project_knowledge"] = {
            "project_configured": bool(project and project["configured"]),
            "project_verified": bool(project and project["configured"]),  # runner raised otherwise
            "project_id": project.get("id") if project else None,
            "local_excerpt_attached": True,
            "revision": knowledge.get("revision"),
        }
    return make_result(provider, request.operation, data=data, raw_text=text, handoff=handoff)

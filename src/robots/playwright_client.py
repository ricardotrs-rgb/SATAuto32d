from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from src.errors import RobotError
from src.models import WebRobotConfig


@contextmanager
def open_page(robot_config: WebRobotConfig):
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RobotError(
            "Playwright no esta instalado. Ejecuta 'pip install -r requirements.txt'."
        ) from error

    with sync_playwright() as playwright:
        browser_type = getattr(playwright, robot_config.browser, None)
        if browser_type is None:
            raise RobotError(
                f"Browser no soportado por Playwright: {robot_config.browser}"
            )

        try:
            browser = browser_type.launch(headless=robot_config.headless)
        except PlaywrightError as error:
            raise RobotError(
                "No se pudo iniciar el navegador. Ejecuta 'python -m playwright install chromium'."
            ) from error

        try:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.set_default_timeout(robot_config.timeout_seconds * 1000)
            yield page
        except PlaywrightError as error:
            raise RobotError(f"Fallo Playwright durante la ejecucion: {error}") from error
        finally:
            context.close()
            browser.close()


@contextmanager
def open_persistent_page(
    robot_config: WebRobotConfig,
    user_data_dir: Path,
    *,
    headless: bool | None = None,
):
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RobotError(
            "Playwright no esta instalado. Ejecuta 'pip install -r requirements.txt'."
        ) from error

    user_data_dir.mkdir(parents=True, exist_ok=True)

    requested_headless = robot_config.headless if headless is None else headless

    with sync_playwright() as playwright:
        browser_type = getattr(playwright, robot_config.browser, None)
        if browser_type is None:
            raise RobotError(
                f"Browser no soportado por Playwright: {robot_config.browser}"
            )

        context = None
        browser = None
        try:
            context = browser_type.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=requested_headless,
                accept_downloads=True,
            )
        except PlaywrightError as error:
            try:
                browser = browser_type.launch(headless=requested_headless)
                context = browser.new_context(accept_downloads=True)
            except PlaywrightError as fallback_error:
                raise RobotError(
                    "No se pudo iniciar ni el navegador persistente ni el navegador manual de respaldo. Ejecuta 'python -m playwright install chromium'."
                ) from fallback_error

        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(robot_config.timeout_seconds * 1000)
            yield page
        except PlaywrightError as error:
            raise RobotError(f"Fallo Playwright durante la ejecucion: {error}") from error
        finally:
            if context is not None:
                context.close()
            if browser is not None:
                browser.close()
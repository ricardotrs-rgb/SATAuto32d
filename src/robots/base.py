from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from src.errors import RobotError
from src.models import AppConfig, Cliente, RobotRunResult, RobotStepConfig, WebRobotConfig


class BaseWebRobot(ABC):
    robot_name: str = "base"
    processes_multiple_clientes: bool = False

    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        cliente: Cliente | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.cliente = cliente
        self.robot_config = self._get_robot_config()
        self.runtime_rfc: str | None = None
        self.runtime_password: str | None = None
        self._prefill_logged = False
        self._prefill_done = False
        self._prefilled_page_ids: set[int] = set()
        self.clientes_override: list[Cliente] | None = None
        self.result_callback: Callable[[Cliente, RobotRunResult], None] | None = None

    def _get_robot_config(self) -> WebRobotConfig:
        try:
            return self.config.robots[self.robot_name]
        except KeyError as error:
            raise RobotError(
                f"No existe configuracion para el robot {self.robot_name}"
            ) from error

    def validate(self) -> None:
        if not self.robot_config.enabled:
            raise RobotError(
                f"El robot {self.robot_name} esta deshabilitado en config.yaml"
            )

    def resolve_start_url(self) -> str:
        if self.robot_config.start_url.startswith("fixture:"):
            fixture_name = self.robot_config.start_url.split(":", 1)[1].strip()
            fixture_path = Path(__file__).resolve().parents[2] / "fixtures" / fixture_name
            if not fixture_path.exists():
                raise RobotError(f"Fixture no encontrado: {fixture_path}")
            return fixture_path.resolve().as_uri()

        return self.robot_config.start_url

    def set_runtime_credentials(self, *, rfc: str, password: str) -> None:
        self.runtime_rfc = rfc.strip().upper() or None
        self.runtime_password = password or None
        self._prefill_logged = False
        self._prefill_done = False
        self._prefilled_page_ids.clear()

    def set_clientes_override(self, clientes: list[Cliente]) -> None:
        self.clientes_override = list(clientes)

    def set_result_callback(
        self,
        callback: Callable[[Cliente, RobotRunResult], None] | None,
    ) -> None:
        self.result_callback = callback

    def get_configured_clientes(self) -> list[Cliente] | None:
        if self.clientes_override is not None:
            return list(self.clientes_override)
        if self.cliente is not None:
            return [self.cliente]
        return None

    def persist_result(self, cliente: Cliente, result: RobotRunResult) -> None:
        if self.result_callback is not None:
            self.result_callback(cliente, result)

    def prefill_sat_login_if_present(self, page) -> bool:
        if not self.runtime_rfc or not self.runtime_password:
            return False
        if self._prefill_done:
            return False

        page_id = id(page)
        if page_id in self._prefilled_page_ids:
            return False

        selectors = {
            "#rfc": self.runtime_rfc,
            "input[name='Ecom_User_ID']": self.runtime_rfc,
            "#password": self.runtime_password,
            "input[name='Ecom_Password']": self.runtime_password,
        }
        filled_any = False
        for selector, value in selectors.items():
            try:
                locator = page.locator(selector)
                if locator.count() == 0:
                    continue
                locator.first.fill(value)
                filled_any = True
            except Exception:
                continue

        if filled_any and not self._prefill_logged:
            self.logger.info(
                "Robot %s precargo RFC y contrasena del SAT solo en memoria.",
                self.robot_name,
            )
            self._prefill_logged = True
        if filled_any:
            self._prefill_done = True
            self._prefilled_page_ids.add(page_id)
        return filled_any

    def run_login_if_enabled(self, page) -> None:
        if not self.robot_config.login.enabled:
            return

        self.fill_login_if_enabled(page)
        self.logger.info(
            "Robot %s solo precargo credenciales; el envio del login queda manual.",
            self.robot_name,
        )

    def fill_login_if_enabled(self, page) -> None:
        if not self.robot_config.login.enabled:
            return

        page.fill(
            self.robot_config.login.username_selector,
            self.robot_config.login.username,
        )
        page.fill(
            self.robot_config.login.password_selector,
            self.robot_config.login.password,
        )
        self.logger.info("Robot %s precargo credenciales de acceso", self.robot_name)

    def _needs_start_url_recovery(self, current_url: str) -> bool:
        current_parts = urlsplit(current_url)
        target_parts = urlsplit(self.resolve_start_url())

        if not current_parts.scheme or not current_parts.netloc:
            return False
        if not target_parts.scheme or not target_parts.netloc:
            return False

        current_path = current_parts.path.rstrip("/") or "/"
        target_path = target_parts.path.rstrip("/") or "/"
        if (
            current_parts.scheme != target_parts.scheme
            or current_parts.netloc != target_parts.netloc
            or current_path != target_path
        ):
            return False

        current_fragment = current_parts.fragment.strip()
        target_fragment = target_parts.fragment.strip()
        return target_fragment not in {"", "/"} and current_fragment in {"", "/"}

    def restore_start_url_if_needed(self, page, *, current_url: str) -> bool:
        if not self._needs_start_url_recovery(current_url):
            return False

        target_url = self.resolve_start_url()
        try:
            page.goto(target_url, wait_until="domcontentloaded")
        except Exception:
            return False

        self.logger.info(
            "Robot %s restauro la ruta SAT despues del login manual: %s",
            self.robot_name,
            target_url,
        )
        return True

    def run_steps(self, page) -> None:
        for step_name, step in self.robot_config.steps.items():
            self._run_step(page, step_name, step)

    def _run_step(self, page, step_name: str, step: RobotStepConfig) -> None:
        if step.action == "click":
            page.click(step.selector)
        elif step.action == "fill":
            page.fill(step.selector, step.value)
        elif step.action == "wait_for":
            page.wait_for_selector(step.selector)
        elif step.action == "press":
            page.press(step.selector, step.value)
        else:
            raise RobotError(f"Accion no soportada en el paso {step_name}: {step.action}")

        self.logger.info(
            "Robot %s ejecuto paso %s (%s)",
            self.robot_name,
            step_name,
            step.action,
        )

    def capture_page_if_enabled(self, page, label: str | None = None) -> Path | None:
        if not self.robot_config.capture.enabled:
            return None

        capture_dir = self.config.paths.control / self.robot_name / "captures"
        capture_dir.mkdir(parents=True, exist_ok=True)
        file_name = self.robot_config.capture.file_name
        if label:
            file_stem = Path(file_name).stem
            file_suffix = Path(file_name).suffix or ".png"
            if not file_stem.endswith(label):
                file_name = f"{file_stem}_{label}{file_suffix}"

        capture_path = capture_dir / file_name
        page.screenshot(
            path=str(capture_path),
            full_page=self.robot_config.capture.full_page,
        )
        self.logger.info(
            "Robot %s guardo captura en %s", self.robot_name, capture_path
        )
        return capture_path

    def download_if_enabled(self, page) -> Path | None:
        if not self.robot_config.download.enabled:
            return None

        target_dir = (
            self.config.paths.publico / self.robot_config.download.target_subdir / "downloads"
        )
        target_dir.mkdir(parents=True, exist_ok=True)

        with page.expect_download() as download_info:
            page.click(self.robot_config.download.trigger_selector)

        download = download_info.value
        target_path = target_dir / download.suggested_filename
        download.save_as(str(target_path))
        self.logger.info(
            "Robot %s guardo descarga en %s", self.robot_name, target_path
        )
        return target_path

    @abstractmethod
    def run(self) -> RobotRunResult:
        raise NotImplementedError
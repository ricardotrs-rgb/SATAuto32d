from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import time

from src.errors import RobotError
from src.models import RobotRunResult
from src.robots.base import BaseWebRobot
from src.robots.playwright_client import open_persistent_page


class SAT32DAutorizaTerceroRobot(BaseWebRobot):
    robot_name = "sat32d_autoriza_tercero"
    processes_multiple_clientes = True

    def __init__(self, config, logger, cliente=None) -> None:
        super().__init__(config, logger, cliente=cliente)
        self.manual_timeout_seconds = 600
        self.target_tercero_rfc: str | None = None

    def configure_run(
        self,
        *,
        target_row: int | None = None,
        manual_timeout_seconds: int = 600,
        include_captcha_pendiente: bool = False,
        tercero_rfc: str | None = None,
    ) -> None:
        if target_row is not None:
            raise RobotError("sat32d_autoriza_tercero no soporta el parametro --row")
        if include_captcha_pendiente:
            raise RobotError(
                "sat32d_autoriza_tercero no soporta el parametro --include-captcha-pendiente"
            )
        self.manual_timeout_seconds = manual_timeout_seconds
        self.target_tercero_rfc = tercero_rfc.strip().upper() if tercero_rfc else None

    def run(self) -> RobotRunResult:
        target_rfcs = self._collect_target_rfcs()
        if not target_rfcs:
            raise RobotError(
                "No hay RFCs para autorizar. Captura un RFC o ejecuta desde la web con clientes registrados."
            )

        profile_dir = self.config.paths.control / self.robot_name / "profile"
        with open_persistent_page(self.robot_config, profile_dir) as page:
            page.goto(self.resolve_start_url(), wait_until="domcontentloaded")
            self.prefill_sat_login_if_present(page)
            print(
                "Completa manualmente el login del SAT. El robot continuara en cuanto detecte la pantalla de alta de tercero autorizado."
            )

            authorization_config = self.robot_config.third_party_authorization
            page = self._wait_until_ready_page(
                page,
                ready_selector=authorization_config.ready_selector,
                timeout_seconds=self.manual_timeout_seconds,
            )

            authorized_rfcs: list[str] = []
            failed_rfcs: list[str] = []

            for tercero_rfc in target_rfcs:
                try:
                    self._authorize_tercero(page, tercero_rfc)
                    message = f"Tercero autorizado correctamente: {tercero_rfc}"
                    self.logger.info(message)
                    print(message)
                    authorized_rfcs.append(tercero_rfc)
                except Exception as error:
                    failed_rfcs.append(tercero_rfc)
                    self.logger.exception(
                        "Fallo la autorizacion para RFC %s", tercero_rfc
                    )
                    print(f"Fallo la autorizacion para RFC {tercero_rfc}: {error}")

            if not authorized_rfcs:
                raise RobotError(
                    "No se pudo autorizar ningun RFC."
                    + (f" RFCs con error: {', '.join(failed_rfcs)}." if failed_rfcs else "")
                )

            capture_path = self.capture_page_if_enabled(page, label=authorized_rfcs[-1])
            message = (
                "Autorizacion de terceros completada. "
                f"Autorizados: {len(authorized_rfcs)}. "
                f"Errores: {len(failed_rfcs)}."
            )
            if failed_rfcs:
                message += f" RFCs con error: {', '.join(failed_rfcs)}."

            self.logger.info(message)
            print(message)
            return RobotRunResult(
                resultado=(
                    "TERCERO_AUTORIZADO"
                    if len(authorized_rfcs) == 1 and not failed_rfcs
                    else "TERCEROS_AUTORIZADOS"
                ),
                fecha_consulta=datetime.now(),
                archivo=str(capture_path) if capture_path else None,
                observaciones=message,
            )

    def _collect_target_rfcs(self) -> list[str]:
        rfcs: list[str] = []

        if self.target_tercero_rfc:
            normalized_target_rfc = self.target_tercero_rfc.strip().upper()
            if normalized_target_rfc:
                rfcs.append(normalized_target_rfc)

        configured_clientes = self.get_configured_clientes() or []
        for cliente in configured_clientes:
            normalized_rfc = (cliente.rfc or "").strip().upper()
            if normalized_rfc:
                rfcs.append(normalized_rfc)

        if not rfcs and sys.stdin and sys.stdin.isatty():
            captured_rfc = input("RFC del tercero a autorizar: ").strip().upper()
            if captured_rfc:
                rfcs.append(captured_rfc)

        unique_rfcs: list[str] = []
        seen_rfcs: set[str] = set()
        for rfc in rfcs:
            if rfc in seen_rfcs:
                continue
            seen_rfcs.add(rfc)
            unique_rfcs.append(rfc)

        return unique_rfcs

    def _authorize_tercero(self, page, tercero_rfc: str) -> None:
        authorization_config = self.robot_config.third_party_authorization
        page.fill(authorization_config.rfc_input_selector, "")
        page.click(authorization_config.rfc_input_selector)
        page.keyboard.type(tercero_rfc, delay=50)
        page.wait_for_function(
            "selector => { const button = document.querySelector(selector); return !!button && !button.disabled; }",
            arg=authorization_config.authorize_selector,
        )
        page.click(authorization_config.authorize_selector)
        page.wait_for_selector(authorization_config.success_selector)

    def _wait_until_ready_page(self, page, *, ready_selector: str, timeout_seconds: int):
        deadline = time.monotonic() + timeout_seconds
        last_url = ""
        route_recovery_attempted = False

        while time.monotonic() < deadline:
            live_page = self._pick_live_page(page)
            if live_page is None:
                time.sleep(0.25)
                continue

            page = live_page
            self.prefill_sat_login_if_present(page)

            try:
                if page.locator(ready_selector).first.is_visible(timeout=500):
                    return page
            except Exception:
                pass

            try:
                last_url = page.url or last_url
            except Exception:
                pass

            if not route_recovery_attempted and self._needs_start_url_recovery(last_url):
                route_recovery_attempted = True
                self.restore_start_url_if_needed(page, current_url=last_url)
                try:
                    last_url = page.url or last_url
                except Exception:
                    pass

            time.sleep(0.25)

        diagnostic_path = self._build_manual_timeout_screenshot_path()
        try:
            live_page = self._pick_live_page(page)
            if live_page is not None:
                live_page.screenshot(path=str(diagnostic_path), full_page=True)
                self.logger.error(
                    "No se detecto la pantalla lista para autorizar tercero. Captura de diagnostico guardada en %s",
                    diagnostic_path,
                )
                print(f"Captura de diagnostico guardada en: {diagnostic_path}")
            else:
                diagnostic_path = None
        except Exception:
            diagnostic_path = None

        diagnostic_message = (
            "No se detecto la pantalla de alta de tercero autorizado dentro del tiempo de espera configurado."
        )
        if last_url:
            diagnostic_message += f" URL actual: {last_url}."
        if diagnostic_path is not None:
            diagnostic_message += f" Revisar captura: {diagnostic_path}"
        raise RobotError(diagnostic_message)

    def _pick_live_page(self, page):
        try:
            if not page.is_closed():
                return page
        except Exception:
            pass

        try:
            for candidate in reversed(page.context.pages):
                try:
                    if not candidate.is_closed():
                        return candidate
                except Exception:
                    continue
        except Exception:
            return None

        return None

    def _build_manual_timeout_screenshot_path(self) -> Path:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        return self.config.paths.errores / f"sat32d_autoriza_tercero_timeout_{timestamp}.png"
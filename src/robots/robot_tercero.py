from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time

from pypdf import PdfReader

from src.errors import RobotError
from src.models import Cliente, RobotRunResult
from src.robots.base import BaseWebRobot
from src.robots.playwright_client import open_persistent_page


class SAT32DTerceroRobot(BaseWebRobot):
    robot_name = "sat32d_tercero"
    processes_multiple_clientes = True

    def __init__(self, config, logger, cliente: Cliente | None = None) -> None:
        super().__init__(config, logger, cliente=cliente)
        self.target_row_number: int | None = None
        self.manual_timeout_seconds = 600
        self.include_captcha_pendiente = False

    def configure_run(
        self,
        *,
        target_row: int | None = None,
        manual_timeout_seconds: int = 600,
        include_captcha_pendiente: bool = False,
        tercero_rfc: str | None = None,
    ) -> None:
        self.target_row_number = target_row
        self.manual_timeout_seconds = manual_timeout_seconds
        self.include_captcha_pendiente = include_captcha_pendiente

    def run(self) -> RobotRunResult:
        clientes = self._select_clientes_tercero()
        if not clientes:
            message = "No hay clientes TERCERO elegibles para procesar."
            self.logger.info(message)
            print(message)
            return RobotRunResult(
                resultado="SIN_CLIENTES",
                fecha_consulta=datetime.now(),
                archivo=None,
                observaciones=message,
            )

        profile_dir = self.config.paths.control / self.robot_name / "profile"
        with open_persistent_page(self.robot_config, profile_dir) as page:
            page.goto(self.resolve_start_url(), wait_until="domcontentloaded")
            print(f"SAT URL actual: {page.url}")
            print(f"SAT titulo actual: {page.title()}")
            self.prefill_sat_login_if_present(page)
            page = self._wait_for_manual_session(page)

            processed_count = 0
            error_count = 0
            last_file: str | None = None

            for cliente in clientes:
                result = self._process_cliente(page, cliente)
                self.persist_result(cliente, result)
                processed_count += 1
                last_file = result.archivo or last_file
                if result.resultado == "ERROR":
                    error_count += 1

        summary = (
            f"Consulta TERCERO completada. Procesados: {processed_count}. Errores: {error_count}."
        )
        self.logger.info(summary)
        print(summary)
        return RobotRunResult(
            resultado="FINALIZADO_TERCERO",
            fecha_consulta=datetime.now(),
            archivo=last_file,
            observaciones=summary,
        )

    def _select_clientes_tercero(self) -> list[Cliente]:
        configured_clientes = self.get_configured_clientes()
        if configured_clientes is not None:
            return configured_clientes

        raise RobotError(
            "sat32d_tercero ahora se ejecuta desde la interfaz web con clientes del almacenamiento local."
        )

    def _wait_for_manual_session(self, page):
        print(
            "Completa manualmente el login y captcha del SAT. El robot continuara en cuanto detecte la pantalla de consulta como tercero autorizado."
        )
        return self._wait_until_ready(page, timeout_seconds=self.manual_timeout_seconds)

    def has_active_session(self, *, timeout_seconds: int = 5) -> bool:
        profile_dir = self.config.paths.control / self.robot_name / "profile"
        with open_persistent_page(self.robot_config, profile_dir, headless=True) as page:
            page.goto(self.resolve_start_url(), wait_until="domcontentloaded")
            ready_page = self._wait_until_ready(
                page,
                timeout_seconds=timeout_seconds,
                capture_timeout_diagnostics=False,
            )
            return ready_page is not None

    def _wait_until_ready(
        self,
        page,
        *,
        timeout_seconds: int | None = None,
        capture_timeout_diagnostics: bool = True,
    ):
        ready_selector = self.robot_config.third_party_query.ready_selector
        deadline = time.monotonic() + (timeout_seconds or self.manual_timeout_seconds)
        current_url = ""
        route_recovery_attempted = False

        while time.monotonic() < deadline:
            live_page = self._pick_live_page(page)
            if live_page is None:
                time.sleep(0.25)
                continue

            page = live_page
            self.prefill_sat_login_if_present(page)

            try:
                current_url = page.url
            except Exception:
                current_url = ""

            if not route_recovery_attempted and self._needs_start_url_recovery(current_url):
                route_recovery_attempted = True
                self.restore_start_url_if_needed(page, current_url=current_url)
                try:
                    current_url = page.url
                except Exception:
                    pass

            if self._read_unauthorized_message(page):
                return page

            try:
                if page.locator(ready_selector).first.is_visible(timeout=500):
                    return page
            except Exception:
                pass

            time.sleep(0.25)

        if not capture_timeout_diagnostics:
            return None

        diagnostic_path = self._build_manual_timeout_screenshot_path()
        try:
            live_page = self._pick_live_page(page)
            if live_page is not None:
                live_page.screenshot(path=str(diagnostic_path), full_page=True)
                self.logger.error(
                    "No se detecto la pantalla lista de SAT. Captura de diagnostico guardada en %s",
                    diagnostic_path,
                )
                print(f"Captura de diagnostico guardada en: {diagnostic_path}")
            else:
                diagnostic_path = None
        except Exception:
            diagnostic_path = None

        diagnostic_message = (
            "No se detecto la pantalla de consulta de tercero autorizado dentro del tiempo de espera configurado."
        )
        if current_url:
            diagnostic_message += f" URL actual: {current_url}."
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

    def _process_cliente(self, page, cliente: Cliente) -> RobotRunResult:
        try:
            unauthorized_message = self._read_unauthorized_message(page)
            if unauthorized_message:
                screenshot_path = self._build_error_screenshot_path(cliente.rfc)
                page.screenshot(path=str(screenshot_path), full_page=True)
                return RobotRunResult(
                    resultado="NO_AUTORIZADO",
                    fecha_consulta=datetime.now(),
                    archivo=str(screenshot_path),
                    observaciones=unauthorized_message,
                )

            unauthorized_message = self._type_rfc(page, cliente.rfc)
            if unauthorized_message:
                screenshot_path = self._build_error_screenshot_path(cliente.rfc)
                page.screenshot(path=str(screenshot_path), full_page=True)
                return RobotRunResult(
                    resultado="NO_AUTORIZADO",
                    fecha_consulta=datetime.now(),
                    archivo=str(screenshot_path),
                    observaciones=unauthorized_message,
                )

            page.click(self.robot_config.third_party_query.submit_selector)

            unauthorized_message = self._read_unauthorized_message(page)
            if unauthorized_message:
                screenshot_path = self._build_error_screenshot_path(cliente.rfc)
                page.screenshot(path=str(screenshot_path), full_page=True)
                return RobotRunResult(
                    resultado="NO_AUTORIZADO",
                    fecha_consulta=datetime.now(),
                    archivo=str(screenshot_path),
                    observaciones=unauthorized_message,
                )

            download_path = self._download_pdf(page, cliente.rfc)
            pdf_text = self._extract_pdf_text(download_path)
            normalized_result = self._normalize_pdf_result(pdf_text)
            return RobotRunResult(
                resultado=normalized_result,
                fecha_consulta=datetime.now(),
                archivo=str(download_path),
                observaciones=self._build_result_observaciones(normalized_result),
            )
        except Exception as error:
            screenshot_path = self._build_error_screenshot_path(cliente.rfc)
            try:
                page.screenshot(path=str(screenshot_path), full_page=True)
            except Exception:
                pass

            self.logger.exception("Fallo el RFC %s en consulta TERCERO", cliente.rfc)
            return RobotRunResult(
                resultado="ERROR",
                fecha_consulta=datetime.now(),
                archivo=str(screenshot_path),
                observaciones=str(error),
            )

    def _type_rfc(self, page, rfc: str) -> str:
        rfc_mode_selector = self.robot_config.third_party_query.rfc_mode_selector
        selector = self.robot_config.third_party_query.rfc_input_selector
        page.click(rfc_mode_selector)
        page.wait_for_function(
            "selector => { const input = document.querySelector(selector); return !!input && !input.disabled && !input.readOnly; }",
            arg=selector,
        )
        page.fill(selector, "")
        page.click(selector)
        page.keyboard.type(rfc, delay=50)
        page.wait_for_function(
            "selector => { const button = document.querySelector(selector); const bodyText = (document.body && document.body.innerText ? document.body.innerText : '').toUpperCase(); return (!!button && !button.disabled) || bodyText.includes('NO ESTA AUTORIZADO PARA EJECUTAR ESTA ACCION') || bodyText.includes('NO ESTÁ AUTORIZADO PARA EJECUTAR ESTA ACCIÓN') || bodyText.includes('ERROR DE ACCESO'); }",
            arg=self.robot_config.third_party_query.submit_selector,
        )
        return self._read_unauthorized_message(page)

    def _read_unauthorized_message(self, page) -> str:
        selector = self.robot_config.third_party_query.unauthorized_selector
        try:
            if not page.is_visible(selector):
                raise ValueError("selector no visible")
            message = (page.text_content(selector) or "").strip()
            if message:
                return message
        except Exception:
            pass

        try:
            body_text = " ".join((page.text_content("body") or "").split())
        except Exception:
            return ""

        normalized_body = body_text.upper()
        unauthorized_markers = (
            "NO ESTA AUTORIZADO PARA EJECUTAR ESTA ACCION",
            "NO EST\u00c1 AUTORIZADO PARA EJECUTAR ESTA ACCI\u00d3N",
            "ERROR DE ACCESO",
        )
        if any(marker in normalized_body for marker in unauthorized_markers):
            return body_text
        return ""

    def _download_pdf(self, page, rfc: str) -> Path:
        target_path = self._build_pdf_path(rfc)
        download_page = self._pick_live_page(page) or page
        with download_page.expect_download() as download_info:
            download_page.click(self.robot_config.third_party_query.download_selector)

        download = download_info.value
        download.save_as(str(target_path))
        return target_path

    def _extract_pdf_text(self, pdf_path: Path) -> str:
        try:
            reader = PdfReader(str(pdf_path))
            extracted_pages = [(page.extract_text() or "").strip() for page in reader.pages]
            combined_text = "\n".join(part for part in extracted_pages if part)
            if combined_text:
                return combined_text
        except Exception:
            pass

        try:
            return pdf_path.read_bytes().decode("latin-1", errors="ignore")
        except Exception:
            return ""

    def _normalize_pdf_result(self, text: str) -> str:
        normalized_text = (text or "").upper()
        if "SUSPENS" in normalized_text:
            return "SUSPENSI\u00d3N"
        if "SIN OBLIGACION" in normalized_text:
            return "SIN OBLIGACIONES"
        if "POSITIV" in normalized_text:
            return "POSITIVA"
        if "NEGATIV" in normalized_text:
            return "NEGATIVA"
        unauthorized_markers = (
            "NO AUTORIZAD",
            "NO SE ENCUENTRA AUTORIZAD",
            "NO ESTA AUTORIZAD",
        )
        if any(marker in normalized_text for marker in unauthorized_markers):
            return "NO_AUTORIZADO"
        return "DESCARGADO"

    def _build_result_observaciones(self, normalized_result: str) -> str:
        if normalized_result == "DESCARGADO":
            return "PDF descargado correctamente."
        return f"Resultado detectado: {normalized_result}. PDF descargado correctamente."

    def _build_pdf_path(self, rfc: str) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        return self.config.paths.terceros / f"{rfc}_32D_{today}.pdf"

    def _build_error_screenshot_path(self, rfc: str) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        return self.config.paths.errores / f"{rfc}_32D_ERROR_{today}.png"

    def _build_manual_timeout_screenshot_path(self) -> Path:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        return self.config.paths.errores / f"sat32d_tercero_timeout_{timestamp}.png"
from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
import time
import unicodedata

from pypdf import PdfReader

from src.errors import RobotError
from src.models import Cliente, RobotRunResult
from src.robots.base import BaseWebRobot
from src.robots.playwright_client import open_page


class SAT32DOpinionRobot(BaseWebRobot):
    robot_name = "sat32d_opinion_cumplimiento"
    processes_multiple_clientes = True

    EFIRMA_BUTTON_SELECTOR = "#buttonFiel"
    EFIRMA_ERROR_SELECTOR = "#divError"
    CERTIFICATE_FILE_SELECTOR = "#fileCertificate"
    PRIVATE_KEY_FILE_SELECTOR = "#filePrivateKey"
    PRIVATE_KEY_PASSWORD_SELECTOR = "#privateKeyPassword"
    EFIRMA_SUBMIT_SELECTOR = "#submit"

    EFIRMA_REVOKED_MESSAGE = "No se puede acceder al aplicativo porque su E.FIRMA está revocada."
    EFIRMA_EXPIRED_MESSAGE = "No se puede acceder al aplicativo porque su E.FIRMA no está vigente."
    EFIRMA_INVALID_CREDENTIALS_MESSAGE = (
        "Certificado, clave privada o contraseña de clave privada inválidos, inténtelo nuevamente."
    )

    def run(self) -> RobotRunResult:
        clientes = self.get_configured_clientes()
        if not clientes:
            return self._run_manual_probe()

        processed_count = 0
        error_count = 0
        last_file: str | None = None

        for cliente in clientes:
            result = self._process_cliente(cliente)
            self.persist_result(cliente, result)
            processed_count += 1
            last_file = result.archivo or last_file
            if result.resultado == "ERROR":
                error_count += 1

        summary = (
            "Consulta con e.firma completada. "
            f"Procesados: {processed_count}. Errores: {error_count}."
        )
        self.logger.info(summary)
        print(summary)
        return RobotRunResult(
            resultado="FINALIZADO_EFIRMA",
            fecha_consulta=datetime.now(),
            archivo=last_file,
            observaciones=summary,
        )

    def _run_manual_probe(self) -> RobotRunResult:
        with open_page(self.robot_config) as page:
            page.goto(self.resolve_start_url(), wait_until="domcontentloaded")
            self.run_steps(page)

            if self.robot_config.login.enabled:
                self.fill_login_if_enabled(page)

            login_capture_path = self.capture_page_if_enabled(page, label="login")

            page_title = page.title()
            current_url = page.url

        self.logger.info(
            "Robot %s llego al acceso SAT. URL actual: %s | titulo: %s | captura: %s",
            self.robot_name,
            current_url,
            page_title,
            login_capture_path,
        )
        print(
            f"Robot {self.robot_name} listo en la pantalla de acceso SAT. Titulo detectado: {page_title}"
        )
        print(f"URL actual: {current_url}")
        if login_capture_path is not None:
            print(f"Captura guardada en: {login_capture_path}")
        if not self.robot_config.login.enabled:
            print(
                "Login automatico desactivado. Configura RFC y contraseña en config.yaml si quieres prellenar el formulario antes del captcha."
            )
        else:
            print("RFC y contraseña precargados. Completa el captcha manualmente antes de enviar.")

        target_label = self.cliente.rfc if self.cliente is not None else "sin cliente"
        return RobotRunResult(
            resultado="CAPTCHA_PENDIENTE",
            fecha_consulta=datetime.now(),
            archivo=str(login_capture_path) if login_capture_path is not None else None,
            observaciones=(
                f"Formulario SAT listo para captcha. Cliente objetivo: {target_label}. URL: {current_url}"
            ),
        )

    def _process_cliente(self, cliente: Cliente) -> RobotRunResult:
        try:
            certificate_path, private_key_path, private_key_password = self._resolve_efirma_inputs(cliente)

            with open_page(self.robot_config) as page:
                page.goto(self.resolve_start_url(), wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                page.click(self.EFIRMA_BUTTON_SELECTOR)
                page.wait_for_timeout(1500)
                page.set_input_files(self.CERTIFICATE_FILE_SELECTOR, str(certificate_path))
                page.set_input_files(self.PRIVATE_KEY_FILE_SELECTOR, str(private_key_path))
                page.fill(self.PRIVATE_KEY_PASSWORD_SELECTOR, private_key_password)
                self._submit_efirma_form(page)

                pdf_data_uri = self._wait_for_embedded_pdf(page, cliente=cliente)
                pdf_path = self._save_embedded_pdf(pdf_data_uri, cliente.rfc)
                pdf_text = self._extract_pdf_text(pdf_path)
                normalized_result = self._normalize_pdf_result(pdf_text)
                return RobotRunResult(
                    resultado=normalized_result,
                    fecha_consulta=datetime.now(),
                    archivo=str(pdf_path),
                    observaciones=self._build_result_observaciones(normalized_result),
                )
        except Exception as error:
            screenshot_path = self._build_error_screenshot_path(cliente.rfc)
            self.logger.exception("Fallo el RFC %s en consulta con e.firma", cliente.rfc)
            return RobotRunResult(
                resultado="ERROR",
                fecha_consulta=datetime.now(),
                archivo=str(screenshot_path),
                observaciones=str(error),
            )

    def _submit_efirma_form(self, page) -> None:
        try:
            page.click(self.EFIRMA_SUBMIT_SELECTOR, no_wait_after=True)
        except TypeError:
            page.click(self.EFIRMA_SUBMIT_SELECTOR)

    def _resolve_efirma_inputs(self, cliente: Cliente) -> tuple[Path, Path, str]:
        certificate_path = Path((cliente.cer_path or "").strip())
        private_key_path = Path((cliente.key_path or "").strip())
        private_key_password = cliente.efirma_password or ""

        if not private_key_password:
            raise RobotError("No hay contraseña de e.firma registrada para este cliente.")
        if not certificate_path.exists():
            raise RobotError(f"No se encontro el archivo .cer del cliente: {certificate_path}")
        if not private_key_path.exists():
            raise RobotError(f"No se encontro el archivo .key del cliente: {private_key_path}")

        return certificate_path, private_key_path, private_key_password

    def _wait_for_embedded_pdf(self, page, *, cliente: Cliente) -> str:
        deadline = time.monotonic() + self.robot_config.timeout_seconds

        while time.monotonic() < deadline:
            error_message = self._read_efirma_error(page)
            if error_message:
                raise RobotError(error_message)

            pdf_data_uri = self._read_embedded_pdf_data_uri(page)
            if pdf_data_uri:
                return pdf_data_uri

            page.wait_for_timeout(250)

        raise RobotError(
            f"La consulta con e.firma no devolvio la opinion PDF para el RFC {cliente.rfc} dentro del tiempo configurado."
        )

    def _read_efirma_error(self, page) -> str:
        visible_error = self._read_text_content(page, self.EFIRMA_ERROR_SELECTOR)
        if visible_error:
            return self._classify_efirma_error_text(visible_error) or visible_error

        body_text = self._read_text_content(page, "body")
        if not body_text:
            return ""

        concise_error = self._classify_efirma_error_text(body_text)
        if concise_error:
            return concise_error

        if not self._looks_like_efirma_script_dump(body_text) and len(body_text) <= 240:
            return body_text

        return ""

    def _read_text_content(self, page, selector: str) -> str:
        try:
            return " ".join((page.text_content(selector) or "").split())
        except Exception:
            return ""

    @classmethod
    def _classify_efirma_error_text(cls, raw_text: str) -> str:
        normalized_text = cls._normalize_efirma_text(raw_text)
        if not normalized_text:
            return ""

        direct_markers = (
            (
                "NO SE PUEDE ACCEDER AL APLICATIVO PORQUE SU E.FIRMA ESTA REVOCADA",
                cls.EFIRMA_REVOKED_MESSAGE,
            ),
            (
                "CERTIFICADO REVOCADO",
                cls.EFIRMA_REVOKED_MESSAGE,
            ),
            (
                "NO SE PUEDE ACCEDER AL APLICATIVO PORQUE SU E.FIRMA NO ESTA VIGENTE",
                cls.EFIRMA_EXPIRED_MESSAGE,
            ),
            (
                "CERTIFICADO CADUCO",
                cls.EFIRMA_EXPIRED_MESSAGE,
            ),
            (
                "CERTIFICADO, CLAVE PRIVADA O CONTRASENA DE CLAVE PRIVADA INVALID",
                cls.EFIRMA_INVALID_CREDENTIALS_MESSAGE,
            ),
        )
        for marker, message in direct_markers:
            if marker in normalized_text:
                if cls._looks_like_efirma_script_dump(raw_text):
                    break
                return message

        if cls._looks_like_efirma_script_dump(raw_text):
            contextual_markers = (
                (
                    "ACCESO CON E.FIRMA NO SE PUEDE ACCEDER AL APLICATIVO PORQUE SU E.FIRMA ESTA REVOCADA",
                    cls.EFIRMA_REVOKED_MESSAGE,
                ),
                (
                    "ACCESO CON E.FIRMA NO SE PUEDE ACCEDER AL APLICATIVO PORQUE SU E.FIRMA NO ESTA VIGENTE",
                    cls.EFIRMA_EXPIRED_MESSAGE,
                ),
            )
            for marker, message in contextual_markers:
                if marker in normalized_text:
                    return message

        return ""

    @staticmethod
    def _looks_like_efirma_script_dump(raw_text: str) -> bool:
        normalized_text = SAT32DOpinionRobot._normalize_efirma_text(raw_text)
        return "SHOWMSGERROR" in normalized_text or "FUNCTION (E)" in normalized_text

    @staticmethod
    def _normalize_efirma_text(raw_text: str) -> str:
        collapsed_text = " ".join((raw_text or "").split())
        if not collapsed_text:
            return ""
        normalized_text = unicodedata.normalize("NFKD", collapsed_text)
        without_marks = "".join(
            character for character in normalized_text if not unicodedata.combining(character)
        )
        return without_marks.upper()

    def _read_embedded_pdf_data_uri(self, page) -> str:
        selectors = (
            ("iframe", "src"),
            ("embed", "src"),
            ("object", "data"),
        )
        for selector, attribute_name in selectors:
            try:
                value = page.eval_on_selector(
                    selector,
                    f"node => node.getAttribute('{attribute_name}') || ''",
                )
            except Exception:
                continue
            if isinstance(value, str) and value.startswith("data:application/pdf"):
                return value
        return ""

    def _save_embedded_pdf(self, pdf_data_uri: str, rfc: str) -> Path:
        try:
            _, payload = pdf_data_uri.split(",", 1)
        except ValueError as error:
            raise RobotError("La respuesta PDF embebida del SAT es invalida.") from error

        pdf_bytes = base64.b64decode(payload)
        target_path = self._build_pdf_path(rfc)
        target_path.write_bytes(pdf_bytes)
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
            return "SUSPENSIÓN"
        if "SIN OBLIGACION" in normalized_text:
            return "SIN OBLIGACIONES"
        if "POSITIV" in normalized_text:
            return "POSITIVA"
        if "NEGATIV" in normalized_text:
            return "NEGATIVA"
        if "NO AUTORIZAD" in normalized_text or "NO ESTA AUTORIZAD" in normalized_text:
            return "NO_AUTORIZADO"
        return "DESCARGADO"

    def _build_result_observaciones(self, normalized_result: str) -> str:
        if normalized_result == "DESCARGADO":
            return "PDF obtenido correctamente con e.firma."
        return f"Resultado detectado: {normalized_result}. PDF obtenido correctamente con e.firma."

    def _build_pdf_path(self, rfc: str) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        return self.config.paths.terceros / f"{rfc}_32D_contribuyente_{today}.pdf"

    def _build_error_screenshot_path(self, rfc: str) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        return self.config.paths.errores / f"{rfc}_32D_EFIRMA_ERROR_{today}.png"
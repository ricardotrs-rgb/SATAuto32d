from __future__ import annotations

import base64
import html
import io
import json
import re
from datetime import datetime
from pathlib import Path
import time
from urllib.parse import unquote_to_bytes

from pypdf import PdfReader

from src.errors import RobotError
from src.models import Cliente, RobotRunResult
from src.robots.base import BaseWebRobot
from src.robots.playwright_client import open_page


class SAT32DPublicoRobot(BaseWebRobot):
    robot_name = "sat32d_publico"
    processes_multiple_clientes = True

    def run(self) -> RobotRunResult:
        clientes = self._select_clientes_publico()
        if not clientes:
            message = "No hay clientes PUBLICO elegibles para procesar."
            self.logger.info(message)
            print(message)
            return RobotRunResult(
                resultado="SIN_CLIENTES",
                fecha_consulta=datetime.now(),
                archivo=None,
                observaciones=message,
            )

        processed_count = 0
        error_count = 0
        last_file: str | None = None

        with open_page(self.robot_config) as page:
            for cliente in clientes:
                result = self._process_cliente(page, cliente)
                self.persist_result(cliente, result)
                processed_count += 1
                last_file = result.archivo or last_file
                if result.resultado == "ERROR":
                    error_count += 1

        summary = (
            f"Consulta PUBLICO completada. Procesados: {processed_count}. Errores: {error_count}."
        )
        self.logger.info(summary)
        print(summary)
        return RobotRunResult(
            resultado="FINALIZADO_PUBLICO",
            fecha_consulta=datetime.now(),
            archivo=last_file,
            observaciones=summary,
        )

    def _select_clientes_publico(self) -> list[Cliente]:
        configured_clientes = self.get_configured_clientes()
        if configured_clientes is not None:
            return configured_clientes

        raise RobotError(
            "sat32d_publico ahora se ejecuta desde la interfaz web con clientes del almacenamiento local."
        )

    def _process_cliente(self, page, cliente: Cliente) -> RobotRunResult:
        try:
            page.goto(self.resolve_start_url(), wait_until="domcontentloaded")
            self._type_rfc(page, cliente.rfc)

            consultation_state = self._submit_public_query(page)
            if consultation_state["status"] == "ERROR":
                screenshot_path = self._build_public_screenshot_path(cliente.rfc)
                page.screenshot(path=str(screenshot_path), full_page=True)
                return RobotRunResult(
                    resultado="NO_AUTORIZADO",
                    fecha_consulta=datetime.now(),
                    archivo=str(screenshot_path),
                    observaciones=consultation_state["message"],
                )

            result_text = consultation_state["message"]
            normalized_result = self._normalize_public_result(result_text)
            observaciones = self._build_result_observaciones(normalized_result, result_text)
            artifact_path = self._resolve_public_artifact_path(
                page,
                rfc=cliente.rfc,
                consultation_state=consultation_state,
            )

            return RobotRunResult(
                resultado=normalized_result,
                fecha_consulta=datetime.now(),
                archivo=str(artifact_path),
                observaciones=observaciones,
            )
        except Exception as error:
            error_path = self._build_error_screenshot_path(cliente.rfc)
            try:
                page.screenshot(path=str(error_path), full_page=True)
            except Exception:
                error_path = self._build_error_screenshot_path(cliente.rfc, fallback=True)

            self.logger.exception("Fallo el RFC %s en consulta PUBLICO", cliente.rfc)
            return RobotRunResult(
                resultado="ERROR",
                fecha_consulta=datetime.now(),
                archivo=str(error_path),
                observaciones=str(error),
            )

    def _type_rfc(self, page, rfc: str) -> None:
        page.fill(self.robot_config.public_query.rfc_input_selector, "")
        page.click(self.robot_config.public_query.rfc_input_selector)
        page.keyboard.type(rfc, delay=50)
        page.wait_for_function(
            "selector => { const button = document.querySelector(selector); return !!button && !button.disabled; }",
            arg=self.robot_config.public_query.submit_selector,
        )

    def _submit_public_query(self, page) -> dict[str, str]:
        try:
            with page.expect_response(
                lambda response: "/ConsultaPublico/Index" in response.url,
                timeout=self.robot_config.timeout_seconds * 1000,
            ) as response_info:
                page.click(self.robot_config.public_query.submit_selector)

            response = response_info.value
            response_text = self._read_response_text(response)
            parsed_state = self._extract_consultation_state_from_response(
                content_type=(response.headers or {}).get("content-type", ""),
                response_text=response_text,
            )
            if parsed_state is not None:
                return parsed_state
        except Exception:
            pass

        return self._wait_for_consultation_state(page)

    def _read_response_text(self, response) -> str:
        try:
            return response.text() or ""
        except Exception:
            return ""

    def _extract_consultation_state_from_response(
        self,
        *,
        content_type: str,
        response_text: str,
    ) -> dict[str, str] | None:
        normalized_content_type = (content_type or "").lower()
        if "application/json" in normalized_content_type:
            message = self._extract_message_from_json_response(response_text)
            if not message:
                return None
            status = "ERROR" if self._normalize_public_result(message) == "NO_AUTORIZADO" else "RESULT"
            return {
                "status": status,
                "message": message,
            }

        if "text/html" in normalized_content_type:
            message, embedded_payload = self._extract_message_from_html_response(response_text)
            if not message:
                return None
            status = "ERROR" if self._normalize_public_result(message) == "NO_AUTORIZADO" else "RESULT"
            return {
                "status": status,
                "message": message,
                "pdf_payload": embedded_payload,
            }

        return None

    def _extract_message_from_json_response(self, response_text: str) -> str:
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError:
            return ""

        message = payload.get("MsjeIformativo") or payload.get("message") or ""
        return self._strip_html_to_text(str(message))

    def _extract_message_from_html_response(self, response_text: str) -> tuple[str, str]:
        visible_text = self._extract_result_label_text(response_text)
        embedded_payload = self._extract_embedded_pdf_payload(response_text)
        extracted_text = self._extract_text_from_embedded_base64_pdf(embedded_payload)
        return self._compose_precise_public_message(visible_text, extracted_text), embedded_payload

    def _extract_result_label_text(self, response_text: str) -> str:
        label_match = re.search(
            r"<div[^>]*id=\"dvMsjessuccess\"[^>]*>.*?<label>(.*?)</label>",
            response_text,
            re.IGNORECASE | re.DOTALL,
        )
        if label_match is None:
            return ""

        return self._strip_html_to_text(label_match.group(1))

    def _extract_embedded_pdf_payload(self, response_text: str) -> str:
        payload_match = re.search(
            r"<div[^>]*id=\"contenidoBase64\"[^>]*>(.*?)</div>",
            response_text,
            re.IGNORECASE | re.DOTALL,
        )
        if payload_match is None:
            return ""

        return html.unescape(payload_match.group(1)).strip()

    def _strip_html_to_text(self, raw_html: str) -> str:
        text = html.unescape(raw_html or "")
        text = re.sub(r"(?i)<br\s*/?>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        return " ".join(text.split())

    def _wait_for_consultation_state(self, page) -> dict[str, str]:
        timeout_seconds = self.robot_config.timeout_seconds
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            if self._is_selector_visible(page, self.robot_config.public_query.unauthorized_selector):
                return {
                    "status": "ERROR",
                    "message": self._get_text(page, self.robot_config.public_query.unauthorized_selector)
                    or "RFC no autorizado para consulta publica.",
                }

            iframe_src = self._get_iframe_src(page)
            if iframe_src.startswith("data:application/pdf"):
                result_text = self._build_precise_public_message(page, iframe_src)
                return {
                    "status": "PDF",
                    "message": result_text,
                    "pdf_data_uri": iframe_src,
                }

            visible_result_text = self._get_text(page, self.robot_config.public_query.result_selector)
            if visible_result_text:
                return {
                    "status": "RESULT",
                    "message": visible_result_text,
                }

            page.wait_for_timeout(250)

        raise RobotError(
            "La consulta publica no devolvio PDF ni mensaje de error dentro del tiempo configurado."
        )

    def _is_selector_visible(self, page, selector: str) -> bool:
        try:
            return page.is_visible(selector)
        except Exception:
            return False

    def _get_text(self, page, selector: str) -> str:
        try:
            return (page.text_content(selector) or "").strip()
        except Exception:
            return ""

    def _get_iframe_src(self, page) -> str:
        try:
            return page.eval_on_selector("iframe", "node => node.getAttribute('src') || ''")
        except Exception:
            return ""

    def _normalize_public_result(self, text: str) -> str:
        normalized_text = text.upper()
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
        return "CONSULTADO"

    def _build_precise_public_message(self, page, iframe_src: str) -> str:
        visible_text = self._get_text(page, self.robot_config.public_query.result_selector)
        extracted_text = self._extract_text_from_iframe_data_uri(iframe_src)

        return self._compose_precise_public_message(visible_text, extracted_text)

    def _compose_precise_public_message(self, visible_text: str, extracted_text: str) -> str:
        visible_text = (visible_text or "").strip()
        extracted_text = (extracted_text or "").strip()
        extracted_state = self._extract_public_state(extracted_text)

        if extracted_state and extracted_state not in {"Positiva", "Negativa"}:
            if visible_text:
                return f"{extracted_state}. {visible_text}".strip()
            return extracted_state

        if visible_text and self._extract_public_state(visible_text):
            return visible_text

        if extracted_state and visible_text:
            if extracted_state in {"Positiva", "Negativa"}:
                return f"Opini\u00f3n {extracted_state}. {visible_text}".strip()
            return f"{extracted_state}. {visible_text}".strip()
        if extracted_state:
            if extracted_state in {"Positiva", "Negativa"}:
                return f"Opini\u00f3n {extracted_state}."
            return extracted_state
        if visible_text:
            return visible_text
        return "Consulta publica generada en PDF."

    def _build_result_observaciones(self, normalized_result: str, raw_text: str) -> str:
        pretty_result = normalized_result.replace("_", " ")
        details = (raw_text or "").strip()
        if details:
            return f"Resultado detectado: {pretty_result}. {details}"
        return f"Resultado detectado: {pretty_result}."

    def _extract_public_state(self, text: str) -> str:
        normalized_text = (text or "").upper()
        if "SUSPENS" in normalized_text:
            return "Suspensi\u00f3n"
        if "SIN OBLIGACION" in normalized_text:
            return "Sin obligaciones"
        if "POSITIV" in normalized_text:
            return "Positiva"
        if "NEGATIV" in normalized_text:
            return "Negativa"
        return ""

    def _extract_text_from_iframe_data_uri(self, iframe_src: str) -> str:
        if not iframe_src.startswith("data:"):
            return ""

        try:
            metadata, payload = iframe_src.split(",", 1)
        except ValueError:
            return ""

        try:
            if ";base64" in metadata:
                decoded = base64.b64decode(payload, validate=False)
            else:
                decoded = unquote_to_bytes(payload)
        except Exception:
            return ""

        return self._extract_text_from_pdf_bytes(decoded)

    def _extract_text_from_embedded_base64_pdf(self, payload: str) -> str:
        if not payload:
            return ""

        try:
            decoded = base64.b64decode(payload, validate=False)
        except Exception:
            return ""

        return self._extract_text_from_pdf_bytes(decoded)

    def _extract_text_from_pdf_bytes(self, pdf_bytes: bytes) -> str:
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            extracted_pages = [(page.extract_text() or "").strip() for page in reader.pages]
            combined_text = "\n".join(part for part in extracted_pages if part)
            if combined_text:
                return combined_text
        except Exception:
            pass

        return pdf_bytes.decode("latin-1", errors="ignore")

    def _resolve_public_artifact_path(self, page, *, rfc: str, consultation_state: dict[str, str]) -> Path:
        pdf_payload = consultation_state.get("pdf_payload") or ""
        if pdf_payload:
            return self._save_public_pdf_from_payload(rfc, pdf_payload)

        pdf_data_uri = consultation_state.get("pdf_data_uri") or ""
        if pdf_data_uri.startswith("data:application/pdf"):
            return self._save_public_pdf_from_data_uri(rfc, pdf_data_uri)

        screenshot_path = self._build_public_screenshot_path(rfc)
        page.screenshot(path=str(screenshot_path), full_page=True)
        return screenshot_path

    def _save_public_pdf_from_payload(self, rfc: str, payload: str) -> Path:
        pdf_bytes = base64.b64decode(payload, validate=False)
        target_path = self._build_public_pdf_path(rfc)
        target_path.write_bytes(pdf_bytes)
        return target_path

    def _save_public_pdf_from_data_uri(self, rfc: str, pdf_data_uri: str) -> Path:
        try:
            _, payload = pdf_data_uri.split(",", 1)
        except ValueError as error:
            raise RobotError("El PDF embebido de consulta PUBLICO es invalido.") from error

        return self._save_public_pdf_from_payload(rfc, payload)

    def _build_public_screenshot_path(self, rfc: str) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        return self.config.paths.publico / f"{rfc}_32D_{today}.png"

    def _build_public_pdf_path(self, rfc: str) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        return self.config.paths.publico / f"{rfc}_32D_{today}.pdf"

    def _build_error_screenshot_path(self, rfc: str, fallback: bool = False) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        suffix = "_fallback" if fallback else ""
        return self.config.paths.errores / f"{rfc}_32D_ERROR_{today}{suffix}.png"
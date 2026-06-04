from __future__ import annotations

import base64
import logging

from src.models import (
    AppConfig,
    CaptureConfig,
    DownloadConfig,
    LoginConfig,
    LoggingConfig,
    ProjectPaths,
    PublicQueryConfig,
    ThirdPartyAuthorizationConfig,
    ThirdPartyQueryConfig,
    WebRobotConfig,
)
from src.robots.robot_publico import SAT32DPublicoRobot


class _FakeKeyboard:
    def __init__(self) -> None:
        self.typed: list[tuple[str, int]] = []

    def type(self, text: str, delay: int = 0) -> None:
        self.typed.append((text, delay))


class _FakePage:
    def __init__(self) -> None:
        self.keyboard = _FakeKeyboard()
        self.fill_calls: list[tuple[str, str]] = []
        self.click_calls: list[str] = []
        self.wait_for_function_calls: list[tuple[str, str | None]] = []

    def fill(self, selector: str, value: str) -> None:
        self.fill_calls.append((selector, value))

    def click(self, selector: str) -> None:
        self.click_calls.append(selector)

    def wait_for_function(
        self,
        expression: str,
        *,
        arg: str | None = None,
        timeout: float | None = None,
        polling: float | str | None = None,
    ) -> None:
        del timeout, polling
        self.wait_for_function_calls.append((expression, arg))


def _build_publico_robot(tmp_path) -> SAT32DPublicoRobot:
    config = AppConfig(
        project_name="sat32d_robot",
        paths=ProjectPaths(
            control=tmp_path / "control",
            publico=tmp_path / "publico",
            terceros=tmp_path / "terceros",
            errores=tmp_path / "errores",
            logs=tmp_path / "logs",
        ),
        logging=LoggingConfig(level="INFO", file_name="sat32d_robot.log"),
        robots={
            "sat32d_publico": WebRobotConfig(
                enabled=True,
                start_url="https://example.com",
                timeout_seconds=45,
                browser="chromium",
                headless=True,
                capture=CaptureConfig(enabled=False, file_name="publico.png", full_page=True),
                download=DownloadConfig(enabled=False, trigger_selector="", target_subdir="sat32d_publico"),
                login=LoginConfig(
                    enabled=False,
                    username="",
                    password="",
                    username_selector="",
                    password_selector="",
                    submit_selector="",
                ),
                public_query=PublicQueryConfig(
                    rfc_input_selector="#txtRfc",
                    submit_selector="#buqueda",
                    result_selector="#dvMsjessuccess",
                    unauthorized_selector="#dvMsjesError",
                ),
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="",
                    rfc_mode_selector="",
                    rfc_input_selector="",
                    submit_selector="",
                    download_selector="",
                    unauthorized_selector="",
                ),
                third_party_authorization=ThirdPartyAuthorizationConfig(
                    ready_selector="",
                    rfc_input_selector="",
                    authorize_selector="",
                    revoke_selector="",
                    print_selector="",
                    success_selector="",
                ),
                steps={},
            )
        },
    )
    return SAT32DPublicoRobot(config, logging.getLogger("test.robot_publico"))


def test_type_rfc_waits_for_submit_button_using_keyword_arg(tmp_path) -> None:
    robot = _build_publico_robot(tmp_path)
    page = _FakePage()

    robot._type_rfc(page, "AAA010101AAA")

    assert page.fill_calls == [("#txtRfc", "")]
    assert page.click_calls == ["#txtRfc"]
    assert page.keyboard.typed == [("AAA010101AAA", 50)]
    assert page.wait_for_function_calls == [
        (
            "selector => { const button = document.querySelector(selector); return !!button && !button.disabled; }",
            "#buqueda",
        )
    ]


def test_normalize_public_result_detects_no_autorizado(tmp_path) -> None:
    robot = _build_publico_robot(tmp_path)

    result = robot._normalize_public_result(
        "El RFC consultado no se encuentra autorizado para hacerse publico"
    )

    assert result == "NO_AUTORIZADO"


def test_normalize_public_result_detects_suspension_and_no_obligations(tmp_path) -> None:
    robot = _build_publico_robot(tmp_path)

    suspension_result = robot._normalize_public_result(
        "Contribuyente en suspension de actividades"
    )
    no_obligations_result = robot._normalize_public_result(
        "Contribuyente inscrito sin obligaciones"
    )

    assert suspension_result == "SUSPENSI\u00d3N"
    assert no_obligations_result == "SIN OBLIGACIONES"


def test_normalize_public_result_prioritizes_suspension_over_positive(tmp_path) -> None:
    robot = _build_publico_robot(tmp_path)

    result = robot._normalize_public_result(
        "Opinion positiva. Contribuyente en proceso de suspension de actividades"
    )

    assert result == "SUSPENSI\u00d3N"


def test_extract_public_state_from_iframe_pdf_payload(tmp_path) -> None:
    robot = _build_publico_robot(tmp_path)
    pdf_like_payload = base64.b64encode(
        b"%PDF-1.4\n... OPINION POSITIVA ..."
    ).decode("ascii")

    extracted_text = robot._extract_text_from_iframe_data_uri(
        f"data:application/pdf;base64,{pdf_like_payload}"
    )

    assert robot._extract_public_state(extracted_text) == "Positiva"


def test_extract_public_state_detects_suspension_and_no_obligations(tmp_path) -> None:
    robot = _build_publico_robot(tmp_path)

    assert robot._extract_public_state("En suspension de actividades") == "Suspensi\u00f3n"
    assert robot._extract_public_state("Inscrito sin obligaciones") == "Sin obligaciones"


def test_extract_public_state_prioritizes_suspension_over_positive(tmp_path) -> None:
    robot = _build_publico_robot(tmp_path)

    assert (
        robot._extract_public_state(
            "Opinion positiva. Contribuyente en proceso de suspension de actividades"
        )
        == "Suspensi\u00f3n"
    )


def test_extract_consultation_state_from_json_response_detects_no_autorizado(tmp_path) -> None:
    robot = _build_publico_robot(tmp_path)

    state = robot._extract_consultation_state_from_response(
        content_type="application/json; charset=utf-8",
        response_text=(
            '{"MsjeIformativo":"El RFC o CURP consultado no se encuentra autorizado '
            'para hacerse p\\u00FAblico.\\u003Cbr\\u003E* Informaci\\u00F3n a la fecha de la consulta."}'
        ),
    )

    assert state == {
        "status": "ERROR",
        "message": "El RFC o CURP consultado no se encuentra autorizado para hacerse p\u00fablico. * Informaci\u00f3n a la fecha de la consulta.",
    }


def test_extract_consultation_state_from_html_response_prefers_embedded_pdf(tmp_path, monkeypatch) -> None:
    robot = _build_publico_robot(tmp_path)
    monkeypatch.setattr(
        robot,
        "_extract_text_from_embedded_base64_pdf",
        lambda payload: "Contribuyente en proceso de suspension de actividades",
    )

    state = robot._extract_consultation_state_from_response(
        content_type="text/html; charset=utf-8",
        response_text=(
            '<div class="alert alert-success" id="dvMsjessuccess">'
            '<label>Opinión Positiva.<br />* Información a la fecha de la consulta.</label>'
            '</div>'
            '<div id="contenidoBase64" style="display:none">JVBERi0xLjQ=</div>'
        ),
    )

    assert state == {
        "status": "RESULT",
        "message": "Suspensi\u00f3n. Opini\u00f3n Positiva. * Informaci\u00f3n a la fecha de la consulta.",
        "pdf_payload": "JVBERi0xLjQ=",
    }


def test_build_result_observaciones_includes_detected_result(tmp_path) -> None:
    robot = _build_publico_robot(tmp_path)

    observaciones = robot._build_result_observaciones(
        "POSITIVA",
        "Opini\u00f3n Positiva.* Informaci\u00f3n a la fecha de la consulta.",
    )

    assert observaciones.startswith("Resultado detectado: POSITIVA.")
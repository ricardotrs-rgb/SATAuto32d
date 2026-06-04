from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook
from werkzeug.datastructures import FileStorage

import src.certificate_utils as certificate_utils
import src.webapp as webapp_module
from src.certificate_utils import extract_rfc_from_subject_identifier
from src.config import validate_config
from src.local_client_store import LocalClientStore
from src.models import (
    AppConfig,
    Cliente,
    CaptureConfig,
    DownloadConfig,
    LoginConfig,
    LoggingConfig,
    ProjectPaths,
    PublicQueryConfig,
    RobotRunResult,
    RobotStepConfig,
    ThirdPartyAuthorizationConfig,
    ThirdPartyQueryConfig,
    WebRobotConfig,
)
from src.robots.base import BaseWebRobot
from src.robots import robot_autoriza_tercero as robot_autoriza_tercero_module
from src.robots.sat32d_opinion import SAT32DOpinionRobot
from src.robots.robot_autoriza_tercero import SAT32DAutorizaTerceroRobot
from src.robots.robot_tercero import SAT32DTerceroRobot
from src.webapp import (
    _build_local_consulta_resumen,
    _local_client_to_view_model,
    _sort_local_records_for_consulta,
    create_web_app,
    _select_local_records_for_local_efirma_consulta,
    _select_local_records_for_local_consulta,
    _select_local_records_for_robot,
    _select_local_records_with_local_efirma_credentials,
    _select_local_records_waiting_for_sat_session,
)


class _FakeKeyboard:
    def __init__(self) -> None:
        self.typed: list[tuple[str, int]] = []

    def type(self, text: str, delay: int = 0) -> None:
        self.typed.append((text, delay))


class _FakeLocator:
    def __init__(self, page, selector: str) -> None:
        self.page = page
        self.selector = selector
        self.first = self

    def is_visible(self, timeout: int | None = None) -> bool:
        del timeout
        return bool(self.page.visibility_by_selector.get(self.selector, False))


class _FakePage:
    def __init__(self) -> None:
        self.keyboard = _FakeKeyboard()
        self.fill_calls: list[tuple[str, str]] = []
        self.click_calls: list[str] = []
        self.goto_calls: list[tuple[str, str | None]] = []
        self.wait_for_function_calls: list[tuple[str, str | None]] = []
        self.wait_for_selector_calls: list[str] = []
        self.download_saved_paths: list[str] = []
        self.expect_download_entered = 0
        self.url = "https://example.com"
        self.visibility_by_selector: dict[str, bool] = {}
        self.text_by_selector: dict[str, str] = {}

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.goto_calls.append((url, wait_until))
        self.url = url

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

    def wait_for_selector(self, selector: str) -> None:
        self.wait_for_selector_calls.append(selector)

    def is_visible(self, selector: str) -> bool:
        return bool(self.visibility_by_selector.get(selector, False))

    def text_content(self, selector: str) -> str:
        return self.text_by_selector.get(selector, "")

    def screenshot(self, *args, **kwargs) -> None:
        return None

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    def expect_download(self):
        page = self

        class _FakeDownload:
            suggested_filename = "demo.pdf"

            def save_as(self, path: str) -> None:
                page.download_saved_paths.append(path)

        class _FakeDownloadInfo:
            value = _FakeDownload()

        class _FakeDownloadContext:
            def __enter__(self):
                page.expect_download_entered += 1
                return _FakeDownloadInfo()

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        return _FakeDownloadContext()


class _TestRobot(BaseWebRobot):
    robot_name = "test_robot"

    def run(self) -> RobotRunResult:
        return RobotRunResult(
            resultado="OK",
            fecha_consulta=None,
            archivo=None,
            observaciones=None,
        )


def _build_robot_config(
    *,
    start_url: str = "https://example.com",
    login: LoginConfig | None = None,
    public_query: PublicQueryConfig | None = None,
    third_party_query: ThirdPartyQueryConfig | None = None,
    third_party_authorization: ThirdPartyAuthorizationConfig | None = None,
) -> WebRobotConfig:
    return WebRobotConfig(
        enabled=True,
        start_url=start_url,
        timeout_seconds=45,
        browser="chromium",
        headless=True,
        capture=CaptureConfig(enabled=False, file_name="robot.png", full_page=True),
        download=DownloadConfig(enabled=False, trigger_selector="", target_subdir="robot"),
        login=login
        or LoginConfig(
            enabled=False,
            username="",
            password="",
            username_selector="",
            password_selector="",
            submit_selector="",
        ),
        public_query=public_query
        or PublicQueryConfig(
            rfc_input_selector="",
            submit_selector="",
            result_selector="",
            unauthorized_selector="",
        ),
        third_party_query=third_party_query
        or ThirdPartyQueryConfig(
            ready_selector="",
            rfc_mode_selector="",
            rfc_input_selector="",
            submit_selector="",
            download_selector="",
            unauthorized_selector="",
        ),
        third_party_authorization=third_party_authorization
        or ThirdPartyAuthorizationConfig(
            ready_selector="",
            rfc_input_selector="",
            authorize_selector="",
            revoke_selector="",
            print_selector="",
            success_selector="",
        ),
        steps={},
    )


def _build_app_config(tmp_path, *, robot_name: str, robot_config: WebRobotConfig) -> AppConfig:
    return AppConfig(
        project_name="sat32d_robot",
        paths=ProjectPaths(
            control=tmp_path / "control",
            publico=tmp_path / "publico",
            terceros=tmp_path / "terceros",
            errores=tmp_path / "errores",
            logs=tmp_path / "logs",
        ),
        logging=LoggingConfig(level="INFO", file_name="sat32d_robot.log"),
        robots={robot_name: robot_config},
    )


def test_base_robot_run_step_wait_for_uses_wait_for_selector(tmp_path) -> None:
    robot = _TestRobot(
        _build_app_config(
            tmp_path,
            robot_name="test_robot",
            robot_config=_build_robot_config(),
        ),
        logging.getLogger("test.base_robot"),
    )
    page = _FakePage()

    robot._run_step(
        page,
        "esperar_estado",
        RobotStepConfig(action="wait_for", selector="#status-ready", value=""),
    )

    assert page.wait_for_selector_calls == ["#status-ready"]


def test_base_robot_download_if_enabled_uses_expect_download(tmp_path) -> None:
    robot = _TestRobot(
        _build_app_config(
            tmp_path,
            robot_name="test_robot",
            robot_config=_build_robot_config(
                start_url="https://example.com",
            ),
        ),
        logging.getLogger("test.base_robot"),
    )
    robot.robot_config = _build_robot_config(
        start_url="https://example.com",
    )
    object.__setattr__(
        robot,
        "robot_config",
        _build_robot_config(
            start_url="https://example.com",
        ),
    )
    object.__setattr__(
        robot.robot_config,
        "download",
        DownloadConfig(
            enabled=True,
            trigger_selector="#download-file",
            target_subdir="sat32d_demo",
        ),
    )
    page = _FakePage()

    target_path = robot.download_if_enabled(page)

    assert page.expect_download_entered == 1
    assert page.click_calls == ["#download-file"]
    assert page.download_saved_paths == [str(tmp_path / "publico" / "sat32d_demo" / "downloads" / "demo.pdf")]
    assert target_path == tmp_path / "publico" / "sat32d_demo" / "downloads" / "demo.pdf"


def test_base_robot_run_login_if_enabled_only_prefills_credentials(tmp_path) -> None:
    robot = _TestRobot(
        _build_app_config(
            tmp_path,
            robot_name="test_robot",
            robot_config=_build_robot_config(
                login=LoginConfig(
                    enabled=True,
                    username="AAA010101AAA",
                    password="secreta",
                    username_selector="#rfc",
                    password_selector="#password",
                    submit_selector="#enviar",
                )
            ),
        ),
        logging.getLogger("test.base_robot"),
    )
    page = _FakePage()

    robot.run_login_if_enabled(page)

    assert page.fill_calls == [("#rfc", "AAA010101AAA"), ("#password", "secreta")]
    assert page.click_calls == []


def test_validate_config_allows_login_without_submit_selector(tmp_path) -> None:
    config = _build_app_config(
        tmp_path,
        robot_name="test_robot",
        robot_config=_build_robot_config(
            login=LoginConfig(
                enabled=True,
                username="AAA010101AAA",
                password="secreta",
                username_selector="#rfc",
                password_selector="#password",
                submit_selector="",
            )
        ),
    )

    validate_config(config)


def test_tercero_wait_until_ready_restores_target_route_after_login(
    tmp_path, monkeypatch
) -> None:
    start_url = "https://ptsc32d.clouda.sat.gob.mx/#/reporteOpinion32DTerceroAutorizado"
    robot = SAT32DTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                start_url=start_url,
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#inputRFCCURP",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                ),
            ),
        ),
        logging.getLogger("test.robot_tercero"),
    )
    page = _FakePage()
    page.url = "https://ptsc32d.clouda.sat.gob.mx/#/"
    restored_urls: list[str] = []

    monkeypatch.setattr(robot, "_pick_live_page", lambda current_page: current_page)
    monkeypatch.setattr(robot, "prefill_sat_login_if_present", lambda current_page: False)

    def _restore(current_page, *, current_url: str) -> bool:
        restored_urls.append(current_url)
        current_page.goto(start_url, wait_until="domcontentloaded")
        current_page.visibility_by_selector["#inputRFCCURP"] = True
        return True

    monkeypatch.setattr(robot, "restore_start_url_if_needed", _restore)

    ready_page = robot._wait_until_ready(page, timeout_seconds=1)

    assert restored_urls == ["https://ptsc32d.clouda.sat.gob.mx/#/"]
    assert page.goto_calls == [(start_url, "domcontentloaded")]
    assert ready_page is page


def test_tercero_wait_until_ready_returns_page_when_access_denied(
    tmp_path, monkeypatch
) -> None:
    robot = SAT32DTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                start_url="https://ptsc32d.clouda.sat.gob.mx/#/reporteOpinion32DTerceroAutorizado",
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#inputRFCCURP",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                ),
            ),
        ),
        logging.getLogger("test.robot_tercero"),
    )
    page = _FakePage()

    monkeypatch.setattr(robot, "_pick_live_page", lambda current_page: current_page)
    monkeypatch.setattr(robot, "prefill_sat_login_if_present", lambda current_page: False)
    monkeypatch.setattr(robot, "_read_unauthorized_message", lambda current_page: "No autorizado")

    ready_page = robot._wait_until_ready(page, timeout_seconds=1)

    assert ready_page is page


def test_tercero_read_unauthorized_message_uses_body_text_markers(tmp_path) -> None:
    robot = SAT32DTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#inputRFCCURP",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                ),
            ),
        ),
        logging.getLogger("test.robot_tercero"),
    )
    page = _FakePage()
    page.text_by_selector["body"] = "\u00a1Error de acceso! No est\u00e1 autorizado para ejecutar esta acci\u00f3n."

    message = robot._read_unauthorized_message(page)

    assert message == "\u00a1Error de acceso! No est\u00e1 autorizado para ejecutar esta acci\u00f3n."


def test_autoriza_wait_until_ready_restores_target_route_after_login(
    tmp_path, monkeypatch
) -> None:
    start_url = "https://ptsc32d.clouda.sat.gob.mx/#/nuevoTercero"
    robot = SAT32DAutorizaTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_autoriza_tercero",
            robot_config=_build_robot_config(
                start_url=start_url,
                third_party_authorization=ThirdPartyAuthorizationConfig(
                    ready_selector="#rfcTercero",
                    rfc_input_selector="#rfcTercero",
                    authorize_selector="#alta",
                    revoke_selector="#baja",
                    print_selector="#imprimir",
                    success_selector="text=Tercero autorizado agregado correctamente.",
                ),
            ),
        ),
        logging.getLogger("test.robot_autoriza_tercero"),
    )
    page = _FakePage()
    page.url = "https://ptsc32d.clouda.sat.gob.mx/#/"
    restored_urls: list[str] = []

    monkeypatch.setattr(robot, "_pick_live_page", lambda current_page: current_page)
    monkeypatch.setattr(robot, "prefill_sat_login_if_present", lambda current_page: False)

    def _restore(current_page, *, current_url: str) -> bool:
        restored_urls.append(current_url)
        current_page.goto(start_url, wait_until="domcontentloaded")
        current_page.visibility_by_selector["#rfcTercero"] = True
        return True

    monkeypatch.setattr(robot, "restore_start_url_if_needed", _restore)

    ready_page = robot._wait_until_ready_page(
        page,
        ready_selector="#rfcTercero",
        timeout_seconds=1,
    )

    assert restored_urls == ["https://ptsc32d.clouda.sat.gob.mx/#/"]
    assert page.goto_calls == [(start_url, "domcontentloaded")]
    assert ready_page is page


def test_tercero_type_rfc_uses_keyword_arg_for_wait_for_function(tmp_path) -> None:
    robot = SAT32DTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#ready",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                )
            ),
        ),
        logging.getLogger("test.robot_tercero"),
    )
    page = _FakePage()

    robot._type_rfc(page, "AAA010101AAA")

    assert page.click_calls == ["#flexRadioRFC", "#inputRFCCURP"]
    assert page.fill_calls == [("#inputRFCCURP", "")]
    assert page.keyboard.typed == [("AAA010101AAA", 50)]
    assert page.wait_for_function_calls == [
        (
            "selector => { const input = document.querySelector(selector); return !!input && !input.disabled && !input.readOnly; }",
            "#inputRFCCURP",
        ),
        (
            "selector => { const button = document.querySelector(selector); const bodyText = (document.body && document.body.innerText ? document.body.innerText : '').toUpperCase(); return (!!button && !button.disabled) || bodyText.includes('NO ESTA AUTORIZADO PARA EJECUTAR ESTA ACCION') || bodyText.includes('NO ESTÁ AUTORIZADO PARA EJECUTAR ESTA ACCIÓN') || bodyText.includes('ERROR DE ACCESO'); }",
            "#btnConsultar",
        ),
    ]


def test_tercero_normalize_pdf_result_detects_positive_and_fallback(tmp_path) -> None:
    robot = SAT32DTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(),
        ),
        logging.getLogger("test.robot_tercero"),
    )

    assert robot._normalize_pdf_result("Opinion Positiva") == "POSITIVA"
    assert robot._normalize_pdf_result("Sin coincidencias claras") == "DESCARGADO"


def test_tercero_normalize_pdf_result_detects_suspension_and_no_obligations(tmp_path) -> None:
    robot = SAT32DTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(),
        ),
        logging.getLogger("test.robot_tercero"),
    )

    assert robot._normalize_pdf_result("En suspension de actividades") == "SUSPENSI\u00d3N"
    assert robot._normalize_pdf_result("Inscrito sin obligaciones") == "SIN OBLIGACIONES"


def test_tercero_normalize_pdf_result_prioritizes_suspension_over_positive(tmp_path) -> None:
    robot = SAT32DTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(),
        ),
        logging.getLogger("test.robot_tercero"),
    )

    assert (
        robot._normalize_pdf_result(
            "Opinion positiva. Contribuyente en proceso de suspension de actividades"
        )
        == "SUSPENSI\u00d3N"
    )


def test_tercero_process_cliente_uses_pdf_result_from_download(tmp_path, monkeypatch) -> None:
    robot = SAT32DTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#ready",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                )
            ),
        ),
        logging.getLogger("test.robot_tercero"),
    )
    page = _FakePage()
    page.visibility_by_selector["#error"] = False
    pdf_path = tmp_path / "terceros" / "AAA010101AAA_32D_2026-05-18.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4 demo")

    monkeypatch.setattr(robot, "_download_pdf", lambda current_page, rfc: pdf_path)
    monkeypatch.setattr(robot, "_extract_pdf_text", lambda current_path: "Opinion Positiva")

    result = robot._process_cliente(
        page,
        Cliente(
            rfc="AAA010101AAA",
            cliente="Cliente demo",
            modo_consulta="TERCERO",
            requiere_pdf="SI",
            autorizado="",
            activo="SI",
            resultado="",
            fecha_consulta="",
            archivo="",
            observaciones="",
            prioridad="",
            row_number=1,
        ),
    )

    assert result.resultado == "POSITIVA"
    assert result.archivo == str(pdf_path)
    assert result.observaciones == "Resultado detectado: POSITIVA. PDF descargado correctamente."


def test_tercero_download_pdf_uses_live_page_when_original_reference_is_stale(tmp_path, monkeypatch) -> None:
    robot = SAT32DTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#ready",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="text=Exportar a PDF",
                    unauthorized_selector="#error",
                )
            ),
        ),
        logging.getLogger("test.robot_tercero"),
    )

    class _StalePage:
        def expect_download(self):
            raise AssertionError("No debe intentar descargar desde la pagina cerrada")

        def click(self, selector: str) -> None:
            raise AssertionError(f"No debe intentar hacer click desde la pagina cerrada: {selector}")

    fresh_page = _FakePage()
    monkeypatch.setattr(robot, "_pick_live_page", lambda current_page: fresh_page)

    download_path = robot._download_pdf(_StalePage(), "AAA010101AAA")

    assert fresh_page.expect_download_entered == 1
    assert fresh_page.click_calls == ["text=Exportar a PDF"]
    assert fresh_page.download_saved_paths == [str(download_path)]
    assert download_path.parent == tmp_path / "terceros"
    assert download_path.name.startswith("AAA010101AAA_32D_")
    assert download_path.suffix == ".pdf"


def test_tercero_process_cliente_returns_no_autorizado_before_typing(tmp_path, monkeypatch) -> None:
    robot = SAT32DTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#ready",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                )
            ),
        ),
        logging.getLogger("test.robot_tercero"),
    )
    page = _FakePage()
    page.visibility_by_selector["#error"] = True
    monkeypatch.setattr(
        robot,
        "_read_unauthorized_message",
        lambda current_page: "No esta autorizado para ejecutar esta accion.",
    )
    monkeypatch.setattr(
        robot,
        "_type_rfc",
        lambda current_page, rfc: (_ for _ in ()).throw(AssertionError("_type_rfc no debe ejecutarse")),
    )

    result = robot._process_cliente(
        page,
        Cliente(
            rfc="AAA010101AAA",
            cliente="Cliente demo",
            modo_consulta="TERCERO",
            requiere_pdf="SI",
            autorizado="",
            activo="SI",
            resultado="",
            fecha_consulta="",
            archivo="",
            observaciones="",
            prioridad="",
            row_number=1,
        ),
    )

    assert result.resultado == "NO_AUTORIZADO"
    assert result.observaciones == "No esta autorizado para ejecutar esta accion."


def test_tercero_process_cliente_returns_no_autorizado_after_typing(tmp_path, monkeypatch) -> None:
    robot = SAT32DTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#ready",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                )
            ),
        ),
        logging.getLogger("test.robot_tercero"),
    )
    page = _FakePage()
    monkeypatch.setattr(robot, "_read_unauthorized_message", lambda current_page: "")
    monkeypatch.setattr(robot, "_type_rfc", lambda current_page, rfc: "No esta autorizado para ejecutar esta accion.")

    result = robot._process_cliente(
        page,
        Cliente(
            rfc="AAA010101AAA",
            cliente="Cliente demo",
            modo_consulta="TERCERO",
            requiere_pdf="SI",
            autorizado="",
            activo="SI",
            resultado="",
            fecha_consulta="",
            archivo="",
            observaciones="",
            prioridad="",
            row_number=1,
        ),
    )

    assert result.resultado == "NO_AUTORIZADO"
    assert result.observaciones == "No esta autorizado para ejecutar esta accion."


def test_tercero_run_uses_live_page_returned_after_manual_login(tmp_path, monkeypatch) -> None:
    robot = SAT32DTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#ready",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                )
            ),
        ),
        logging.getLogger("test.robot_tercero"),
    )

    class _RunPage:
        def __init__(self, name: str) -> None:
            self.name = name
            self.url = "https://example.com"
            self.goto_calls: list[tuple[str, str | None]] = []

        def goto(self, url: str, wait_until: str | None = None) -> None:
            self.goto_calls.append((url, wait_until))
            self.url = url

        def title(self) -> str:
            return f"Page {self.name}"

    initial_page = _RunPage("initial")
    ready_page = _RunPage("ready")

    @contextmanager
    def _fake_open_persistent_page(_robot_config, _profile_dir):
        yield initial_page

    seen_pages: list[str] = []

    monkeypatch.setattr(
        "src.robots.robot_tercero.open_persistent_page",
        _fake_open_persistent_page,
    )
    monkeypatch.setattr(robot, "prefill_sat_login_if_present", lambda current_page: False)
    monkeypatch.setattr(robot, "_wait_for_manual_session", lambda current_page: ready_page)
    monkeypatch.setattr(
        robot,
        "_process_cliente",
        lambda current_page, cliente: (
            seen_pages.append(current_page.name),
            RobotRunResult(
                resultado="POSITIVA",
                fecha_consulta=None,
                archivo=None,
                observaciones="ok",
            ),
        )[1],
    )
    robot.set_clientes_override(
        [
            Cliente(
                rfc="AAA010101AAA",
                cliente="Cliente demo",
                modo_consulta="TERCERO",
                requiere_pdf="SI",
                autorizado="",
                activo="SI",
                resultado="",
                fecha_consulta="",
                archivo="",
                observaciones="",
                prioridad="",
                row_number=1,
            )
        ]
    )

    result = robot.run()

    assert initial_page.goto_calls == [("https://example.com", "domcontentloaded")]
    assert seen_pages == ["ready"]
    assert result.resultado == "FINALIZADO_TERCERO"


def test_tercero_has_active_session_probes_profile_headless(tmp_path, monkeypatch) -> None:
    robot = SAT32DTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#ready",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                )
            ),
        ),
        logging.getLogger("test.robot_tercero"),
    )

    probe_page = _FakePage()
    seen_headless_values: list[bool | None] = []

    @contextmanager
    def _fake_open_persistent_page(_robot_config, _profile_dir, *, headless=None):
        seen_headless_values.append(headless)
        yield probe_page

    monkeypatch.setattr(
        "src.robots.robot_tercero.open_persistent_page",
        _fake_open_persistent_page,
    )
    monkeypatch.setattr(robot, "_wait_until_ready", lambda current_page, **_: current_page)

    assert robot.has_active_session(timeout_seconds=1) is True
    assert seen_headless_values == [True]
    assert probe_page.goto_calls == [("https://example.com", "domcontentloaded")]


def test_autoriza_tercero_run_uses_keyword_arg_for_wait_for_function(
    tmp_path, monkeypatch
) -> None:
    robot = SAT32DAutorizaTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_autoriza_tercero",
            robot_config=_build_robot_config(
                third_party_authorization=ThirdPartyAuthorizationConfig(
                    ready_selector="#rfcTercero",
                    rfc_input_selector="#rfcTercero",
                    authorize_selector="#alta",
                    revoke_selector="#baja",
                    print_selector="#imprimir",
                    success_selector="text=Tercero autorizado agregado correctamente.",
                )
            ),
        ),
        logging.getLogger("test.robot_autoriza_tercero"),
    )
    page = _FakePage()
    robot.target_tercero_rfc = "AAA010101AAA"

    @contextmanager
    def _fake_open_persistent_page(_robot_config, _profile_dir):
        yield page

    monkeypatch.setattr(
        robot_autoriza_tercero_module,
        "open_persistent_page",
        _fake_open_persistent_page,
    )
    monkeypatch.setattr(robot, "_wait_until_ready_page", lambda current_page, **_: current_page)
    monkeypatch.setattr(robot, "capture_page_if_enabled", lambda current_page, label=None: None)

    result = robot.run()

    assert page.goto_calls == [("https://example.com", "domcontentloaded")]
    assert page.fill_calls == [("#rfcTercero", "")]
    assert page.click_calls == ["#rfcTercero", "#alta"]
    assert page.keyboard.typed == [("AAA010101AAA", 50)]
    assert page.wait_for_function_calls == [
        (
            "selector => { const button = document.querySelector(selector); return !!button && !button.disabled; }",
            "#alta",
        )
    ]
    assert page.wait_for_selector_calls == ["text=Tercero autorizado agregado correctamente."]
    assert result.resultado == "TERCERO_AUTORIZADO"


def test_autoriza_collect_target_rfcs_uses_override_and_deduplicates(tmp_path) -> None:
    robot = SAT32DAutorizaTerceroRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_autoriza_tercero",
            robot_config=_build_robot_config(),
        ),
        logging.getLogger("test.robot_autoriza_tercero"),
    )
    robot.target_tercero_rfc = "aaa010101aaa"
    robot.set_clientes_override(
        [
            Cliente(
                rfc="BBB010101BBB",
                cliente="Cliente B",
                modo_consulta="TERCERO",
                requiere_pdf="SI",
                autorizado="",
                activo="SI",
                resultado="PENDIENTE",
                fecha_consulta="",
                archivo="",
                observaciones="",
                prioridad="",
                row_number=1,
            ),
            Cliente(
                rfc="AAA010101AAA",
                cliente="Cliente A",
                modo_consulta="TERCERO",
                requiere_pdf="SI",
                autorizado="",
                activo="SI",
                resultado="PENDIENTE",
                fecha_consulta="",
                archivo="",
                observaciones="",
                prioridad="",
                row_number=2,
            ),
        ]
    )

    rfcs = robot._collect_target_rfcs()

    assert rfcs == ["AAA010101AAA", "BBB010101BBB"]


def test_select_local_records_for_robot_autoriza_uses_active_rfc() -> None:
    selected = _select_local_records_for_robot(
        [
            {"id": "1", "rfc": "AAA010101AAA", "activo": "SI"},
            {"id": "2", "rfc": "BBB010101BBB", "activo": "NO"},
            {"id": "3", "rfc": "", "activo": "SI"},
        ],
        robot_name="sat32d_autoriza_tercero",
        target_client_id=None,
        include_captcha_pendiente=False,
    )

    assert [record["id"] for record in selected] == ["1"]


def test_select_local_records_for_robot_publico_mass_ignores_previous_status() -> None:
    selected = _select_local_records_for_robot(
        [
            {
                "id": "1",
                "modo_consulta": "PUBLICO",
                "activo": "SI",
                "resultado": "POSITIVA",
            },
            {
                "id": "2",
                "modo_consulta": "PUBLICO",
                "activo": "SI",
                "resultado": "NO_AUTORIZADO",
            },
            {
                "id": "3",
                "modo_consulta": "PUBLICO",
                "activo": "NO",
                "resultado": "PENDIENTE",
            },
        ],
        robot_name="sat32d_publico",
        target_client_id=None,
        include_captcha_pendiente=False,
    )

    assert [record["id"] for record in selected] == ["1", "2"]


def test_select_local_records_for_local_consulta_falls_back_to_publico_without_sat_session() -> None:
    grouped = _select_local_records_for_local_consulta(
        [
            {
                "id": "1",
                "rfc": "AAA010101AAA",
                "modo_consulta": "PUBLICO",
                "activo": "SI",
                "resultado": "POSITIVA",
            },
            {
                "id": "2",
                "rfc": "BBB010101BBB",
                "modo_consulta": "TERCERO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "resultado": "DESCARGADO",
            },
            {
                "id": "3",
                "rfc": "CCC010101CCC",
                "modo_consulta": "TERCERO",
                "activo": "NO",
                "requiere_pdf": "SI",
                "resultado": "PENDIENTE",
            },
        ],
        include_captcha_pendiente=False,
    )

    assert [record["id"] for record in grouped["sat32d_publico"]] == ["1"]
    assert grouped["sat32d_tercero"] == []


def test_select_local_records_waiting_for_sat_session_returns_tercero_records() -> None:
    selected = _select_local_records_waiting_for_sat_session(
        [
            {
                "id": "1",
                "rfc": "AAA010101AAA",
                "modo_consulta": "PUBLICO",
                "activo": "SI",
                "resultado": "PENDIENTE",
            },
            {
                "id": "2",
                "rfc": "BBB010101BBB",
                "modo_consulta": "TERCERO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "resultado": "NO_AUTORIZADO",
            },
            {
                "id": "3",
                "rfc": "CCC010101CCC",
                "modo_consulta": "TERCERO",
                "activo": "NO",
                "requiere_pdf": "SI",
                "resultado": "PENDIENTE",
            },
        ],
        include_captcha_pendiente=False,
    )

    assert [record["id"] for record in selected] == ["2"]


def test_sort_local_records_for_consulta_orders_alphabetically_by_cliente_then_rfc() -> None:
    selected = _sort_local_records_for_consulta(
        [
            {
                "id": "3",
                "cliente": "zeta industrial",
                "rfc": "ZZZ010101ZZ1",
            },
            {
                "id": "2",
                "cliente": "Alfa Soluciones",
                "rfc": "BBB010101BBB",
            },
            {
                "id": "1",
                "cliente": "alfa soluciones",
                "rfc": "AAA010101AAA",
            },
        ]
    )

    assert [record["id"] for record in selected] == ["1", "2", "3"]


def test_select_local_records_with_local_efirma_credentials_filters_records() -> None:
    selected = _select_local_records_with_local_efirma_credentials(
        [
            {
                "id": "1",
                "key_path": "",
                "cer_path": "",
                "efirma_password": "",
            },
            {
                "id": "2",
                "key_path": "cliente.key",
                "cer_path": "cliente.cer",
                "efirma_password": "secreta",
            },
        ]
    )

    assert [record["id"] for record in selected] == ["2"]


def test_select_local_records_for_local_efirma_consulta_only_includes_tercero_with_efirma() -> None:
    selected = _select_local_records_for_local_efirma_consulta(
        [
            {
                "id": "1",
                "modo_consulta": "PUBLICO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "rfc": "AAA010101AAA",
                "key_path": "cliente.key",
                "cer_path": "cliente.cer",
                "efirma_password": "secreta",
            },
            {
                "id": "2",
                "modo_consulta": "PUBLICO",
                "activo": "SI",
                "requiere_pdf": "NO",
                "rfc": "BBB010101BBB",
                "key_path": "cliente.key",
                "cer_path": "cliente.cer",
                "efirma_password": "secreta",
            },
            {
                "id": "3",
                "modo_consulta": "TERCERO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "rfc": "CCC010101CCC",
                "key_path": "",
                "cer_path": "",
                "efirma_password": "",
            },
            {
                "id": "4",
                "modo_consulta": "TERCERO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "rfc": "DDD010101DDD",
                "key_path": "cliente.key",
                "cer_path": "cliente.cer",
                "efirma_password": "secreta",
            },
        ],
        include_captcha_pendiente=True,
        ignore_status=True,
    )

    assert [record["id"] for record in selected] == ["1", "4"]


def test_build_local_consulta_resumen_counts_publico_and_tercero() -> None:
    resumen = _build_local_consulta_resumen(
        [
            {
                "id": "1",
                "modo_consulta": "PUBLICO",
                "activo": "SI",
                "rfc": "AAA010101AAA",
                "requiere_pdf": "SI",
                "key_path": "cliente.key",
                "cer_path": "cliente.cer",
                "efirma_password": "secreta",
            },
            {
                "id": "2",
                "modo_consulta": "TERCERO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "rfc": "BBB010101BBB",
                "key_path": "cliente.key",
                "cer_path": "cliente.cer",
                "efirma_password": "secreta",
            },
            {
                "id": "3",
                "modo_consulta": "TERCERO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "rfc": "CCC010101CCC",
                "key_path": "",
                "cer_path": "",
                "efirma_password": "",
            },
        ],
        include_captcha_pendiente=True,
    )

    assert resumen == {
        "total": 3,
        "publico": 1,
        "tercero": 2,
        "tercero_efirma": 2,
    }


def test_local_client_to_view_model_adds_short_error_cause_label() -> None:
    view_model = _local_client_to_view_model(
        {
            "id": "1",
            "cliente": "Cliente demo",
            "rfc": "AAA010101AAA",
            "modo_consulta": "PUBLICO",
            "requiere_pdf": "SI",
            "activo": "SI",
            "resultado": "ERROR",
            "resultado_observaciones": "Interruptor de Navegación Trámites Gobierno Búsqueda $(function (e) { var error = ''; if (new String(error).valueOf() == new String('Certificado Revocado').valueOf()) { error = 'No se puede acceder al aplicativo porque su E.FIRMA está revocada.'; } if (new String(error).valueOf() == new String('Certificado Caduco').valueOf()) { error = 'No se puede acceder al aplicativo porque su E.FIRMA no está vigente.'; } showMsgError(error); }); Inicio Acceso con e.firma Certificado, clave privada o contraseña de clave privada inválidos, inténtelo nuevamente.",
            "notas_cliente": "Observación interna",
            "fecha_consulta": "2026-05-20 16:00:00",
            "archivo": "C:/SAT32D/Errores/demo.png",
            "key_file_name": "demo.key",
            "cer_file_name": "demo.cer",
            "created_at": "2026-05-11 10:00:00",
            "efirma_password": "secreta",
        }
    )

    assert view_model["resultado"] == "ERROR"
    assert view_model["error_cause_label"] == "Error SAT"
    assert view_model["resultado_observaciones"].startswith("Interruptor de Navegación")


def test_local_client_to_view_model_exposes_last_successful_snapshot() -> None:
    view_model = _local_client_to_view_model(
        {
            "id": "1",
            "cliente": "Cliente demo",
            "rfc": "AAA010101AAA",
            "modo_consulta": "PUBLICO",
            "requiere_pdf": "SI",
            "activo": "SI",
            "resultado": "NO_AUTORIZADO",
            "resultado_observaciones": "El RFC o CURP consultado no se encuentra autorizado para hacerse público.",
            "ultimo_resultado_exitoso": "POSITIVA",
            "ultima_fecha_consulta_exitosa": "2026-05-20 15:55:00",
            "ultimo_archivo_exitoso": "C:/SAT32D/Publico/demo_exito.png",
            "ultimas_observaciones_exitosas": "Resultado detectado: POSITIVA.",
            "fecha_consulta": "2026-05-20 16:00:00",
            "archivo": "C:/SAT32D/Publico/demo_no_autorizado.png",
            "key_file_name": "demo.key",
            "cer_file_name": "demo.cer",
            "created_at": "2026-05-11 10:00:00",
            "efirma_password": "secreta",
        }
    )

    assert view_model["ultimo_resultado_exitoso"] == "POSITIVA"
    assert view_model["ultima_fecha_consulta_exitosa"] == "2026-05-20 15:55:00"
    assert view_model["ultimo_archivo_exitoso"] == "C:/SAT32D/Publico/demo_exito.png"
    assert view_model["ultimas_observaciones_exitosas"] == "Resultado detectado: POSITIVA."
    assert view_model["mostrar_ultimo_resultado_exitoso"] is True


def test_select_local_records_for_local_consulta_respects_stored_publico_mode() -> None:
    grouped = _select_local_records_for_local_consulta(
        [
            {
                "id": "1",
                "rfc": "AAA010101AAA",
                "modo_consulta": "PUBLICO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "resultado": "NO_AUTORIZADO",
            },
            {
                "id": "2",
                "rfc": "BBB010101BBB",
                "modo_consulta": "PUBLICO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "resultado": "POSITIVA",
            },
            {
                "id": "3",
                "rfc": "CCC010101CCC",
                "modo_consulta": "PUBLICO",
                "activo": "SI",
                "requiere_pdf": "NO",
                "resultado": "PENDIENTE",
            },
            {
                "id": "4",
                "rfc": "DDD010101DDD",
                "modo_consulta": "TERCERO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "resultado": "POSITIVA",
            },
        ],
        include_captcha_pendiente=False,
        prefer_third_party=True,
    )

    assert [record["id"] for record in grouped["sat32d_publico"]] == ["1", "2", "3"]
    assert [record["id"] for record in grouped["sat32d_tercero"]] == ["4"]


def test_run_robot_from_web_allows_local_consulta_without_runtime_credentials(tmp_path, monkeypatch) -> None:
    class _FakeLocalClientStore:
        def load_registered_users(self):
            return {}

        def sync_registered_monitored_clients(self, **kwargs):
            return {"created": 0, "updated": 0, "removed": 0}

        def sync_client_rfcs_from_certificates(self, **kwargs):
            return {"updated": 0, "skipped": 0}

        def list_clients(self, *, owner_rfc=None):
            assert owner_rfc == "IOEF840128UC4"
            return [
                {
                    "id": "client-1",
                    "owner_rfc": owner_rfc,
                    "cliente": "Cliente demo",
                    "rfc": "AAA010101AAA",
                    "modo_consulta": "TERCERO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "resultado": "PENDIENTE",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "efirma_password": "demo",
                }
            ]

        def get_client(self, client_id, *, owner_rfc=None):
            assert client_id == "client-1"
            assert owner_rfc == "IOEF840128UC4"
            return self.list_clients(owner_rfc=owner_rfc)[0]

    class _FakeThread:
        def __init__(self, target=None, kwargs=None, daemon=None):
            self.target = target
            self.kwargs = kwargs or {}
            self.daemon = daemon
            self.started = False

        def start(self):
            self.started = True

    monkeypatch.setattr(webapp_module, "LocalClientStore", lambda storage_dir: _FakeLocalClientStore())
    monkeypatch.setattr(
        webapp_module,
        "_has_reusable_third_party_session",
        lambda **kwargs: False,
    )
    created_threads: list[_FakeThread] = []

    def _build_thread(*args, **kwargs):
        thread = _FakeThread(*args, **kwargs)
        created_threads.append(thread)
        return thread

    monkeypatch.setattr(webapp_module.threading, "Thread", _build_thread)

    app = create_web_app(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#inputRFCCURP",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                ),
            ),
        ),
        logging.getLogger("test.webapp"),
    )
    app.config["SAT32D_WEB_CREDENTIALS"]["session-1"] = {
        "rfc": "IOEF840128UC4",
        "password": "Javi1984",
    }
    app.config["SAT32D_WEB_REGISTERED_USERS"]["IOEF840128UC4"] = {
        "password_hash": "fixture-hash",
        "profile": {},
        "monitored_people": [],
    }

    with app.test_client() as client:
        with client.session_transaction() as session_state:
            session_state["user_rfc"] = "IOEF840128UC4"
            session_state["web_session_id"] = "session-1"

        response = client.post(
            "/robots/run",
            data={
                "robot_name": "sat32d_locales",
            },
        )

    assert response.status_code == 302
    assert created_threads
    assert created_threads[0].started is True
    jobs = app.config["SAT32D_WEB_JOBS"]
    assert len(jobs) == 1
    assert jobs[0]["robot_name"] == "sat32d_locales"
    assert "0 clientes PUBLICO" in jobs[0]["summary"]
    assert "1 clientes con e.firma" in jobs[0]["summary"]


def test_run_robot_from_web_keeps_publico_clients_in_public_mass_queue_when_sat_session_is_missing(
    tmp_path, monkeypatch
) -> None:
    class _FakeLocalClientStore:
        def load_registered_users(self):
            return {}

        def sync_registered_monitored_clients(self, **kwargs):
            return {"created": 0, "updated": 0, "removed": 0}

        def sync_client_rfcs_from_certificates(self, **kwargs):
            return {"updated": 0, "skipped": 0}

        def list_clients(self, *, owner_rfc=None):
            assert owner_rfc == "IOEF840128UC4"
            return [
                {
                    "id": "client-1",
                    "owner_rfc": owner_rfc,
                    "cliente": "Cliente demo",
                    "rfc": "AAA010101AAA",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "resultado": "POSITIVA",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "efirma_password": "demo",
                }
            ]

        def get_client(self, client_id, *, owner_rfc=None):
            assert client_id == "client-1"
            assert owner_rfc == "IOEF840128UC4"
            return self.list_clients(owner_rfc=owner_rfc)[0]

    class _FakeThread:
        def __init__(self, target=None, kwargs=None, daemon=None):
            self.target = target
            self.kwargs = kwargs or {}
            self.daemon = daemon
            self.started = False

        def start(self):
            self.started = True

    monkeypatch.setattr(webapp_module, "LocalClientStore", lambda storage_dir: _FakeLocalClientStore())
    monkeypatch.setattr(
        webapp_module,
        "_has_reusable_third_party_session",
        lambda **kwargs: False,
    )
    created_threads: list[_FakeThread] = []

    def _build_thread(*args, **kwargs):
        thread = _FakeThread(*args, **kwargs)
        created_threads.append(thread)
        return thread

    monkeypatch.setattr(webapp_module.threading, "Thread", _build_thread)

    app = create_web_app(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#inputRFCCURP",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                ),
            ),
        ),
        logging.getLogger("test.webapp"),
    )
    app.config["SAT32D_WEB_CREDENTIALS"]["session-1"] = {
        "rfc": "IOEF840128UC4",
        "password": "Javi1984",
    }
    app.config["SAT32D_WEB_REGISTERED_USERS"]["IOEF840128UC4"] = {
        "password_hash": "fixture-hash",
        "profile": {},
        "monitored_people": [],
    }

    with app.test_client() as client:
        with client.session_transaction() as session_state:
            session_state["user_rfc"] = "IOEF840128UC4"
            session_state["web_session_id"] = "session-1"

        response = client.post(
            "/robots/run",
            data={
                "robot_name": "sat32d_locales",
            },
        )

    assert response.status_code == 302
    assert created_threads
    assert created_threads[0].started is True
    jobs = app.config["SAT32D_WEB_JOBS"]
    assert len(jobs) == 1
    assert jobs[0]["robot_name"] == "sat32d_locales"
    assert "0 clientes PUBLICO" in jobs[0]["summary"]
    assert "1 clientes con e.firma" in jobs[0]["summary"]


def test_run_robot_from_web_persists_deferred_local_tercero_clients_when_sat_session_missing(
    tmp_path,
    monkeypatch,
) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "IOEF840128UC4",
                    "cliente": "Cliente tercero",
                    "rfc": "AAA010101AAA",
                    "modo_consulta": "TERCERO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "",
                    "notas_cliente": "",
                    "resultado_observaciones": "Error previo.",
                    "resultado": "ERROR",
                    "fecha_consulta": "2026-05-21 09:00:00",
                    "archivo": "C:/SAT32D/Errores/client-1.png",
                    "prioridad": "",
                    "key_file_name": "",
                    "cer_file_name": "",
                    "key_path": "",
                    "cer_path": "",
                    "created_at": "2026-05-21 08:30:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)

    class _FakeThread:
        def __init__(self, target=None, kwargs=None, daemon=None):
            self.target = target
            self.kwargs = kwargs or {}
            self.daemon = daemon
            self.started = False

        def start(self):
            self.started = True
            assert self.target is not None
            self.target(**self.kwargs)

    monkeypatch.setattr(webapp_module, "LocalClientStore", lambda storage_dir: store)
    monkeypatch.setattr(
        webapp_module,
        "_has_reusable_third_party_session",
        lambda **kwargs: False,
    )
    monkeypatch.setattr(webapp_module.threading, "Thread", _FakeThread)

    app = create_web_app(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#inputRFCCURP",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                ),
            ),
        ),
        logging.getLogger("test.webapp"),
    )
    app.config["SAT32D_WEB_CREDENTIALS"]["session-1"] = {
        "rfc": "IOEF840128UC4",
        "password": "Javi1984",
    }
    app.config["SAT32D_WEB_REGISTERED_USERS"]["IOEF840128UC4"] = {
        "password_hash": "fixture-hash",
        "profile": {},
        "monitored_people": [],
    }

    with app.test_client() as client:
        with client.session_transaction() as session_state:
            session_state["user_rfc"] = "IOEF840128UC4"
            session_state["web_session_id"] = "session-1"

        response = client.post(
            "/robots/run",
            data={
                "robot_name": "sat32d_locales",
            },
        )

    assert response.status_code == 302
    updated_record = store.list_clients(owner_rfc="IOEF840128UC4")[0]
    assert updated_record["resultado"] == "PENDIENTE"
    assert updated_record["fecha_consulta"] == ""
    assert updated_record["archivo"] == ""
    assert "se requiere una sesion SAT activa" in updated_record["resultado_observaciones"]


def test_run_robot_from_web_retries_publico_no_autorizado_with_local_efirma(
    tmp_path,
    monkeypatch,
) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "IOEF840128UC4",
                    "cliente": "ACC FIRE SYSTEMS",
                    "rfc": "AFS230503FN1",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "PENDIENTE",
                    "fecha_consulta": "",
                    "archivo": "",
                    "prioridad": "",
                    "key_file_name": "cliente.key",
                    "cer_file_name": "cliente.cer",
                    "key_path": "C:/efirma/cliente.key",
                    "cer_path": "C:/efirma/cliente.cer",
                    "created_at": "2026-05-21 08:30:00",
                    "source": "LOCAL_STORAGE",
                },
                {
                    "id": "client-2",
                    "owner_rfc": "IOEF840128UC4",
                    "cliente": "AGRISERVS",
                    "rfc": "ATG230728NE6",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "PENDIENTE",
                    "fecha_consulta": "",
                    "archivo": "",
                    "prioridad": "",
                    "key_file_name": "",
                    "cer_file_name": "",
                    "key_path": "",
                    "cer_path": "",
                    "created_at": "2026-05-21 08:35:00",
                    "source": "LOCAL_STORAGE",
                },
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)
    processed_batches: list[tuple[str, list[str]]] = []

    class _FakeThread:
        def __init__(self, target=None, kwargs=None, daemon=None):
            self.target = target
            self.kwargs = kwargs or {}
            self.daemon = daemon
            self.started = False

        def start(self):
            self.started = True
            assert self.target is not None
            self.target(**self.kwargs)

    class _FakeRobot:
        def __init__(self, robot_name: str):
            self.robot_name = robot_name
            self.clientes: list[Cliente] = []
            self.result_callback = None

        def validate(self) -> None:
            return None

        def set_clientes_override(self, clientes: list[Cliente]) -> None:
            self.clientes = clientes

        def set_result_callback(self, callback) -> None:
            self.result_callback = callback

        def set_runtime_credentials(self, *, rfc: str, password: str) -> None:
            return None

        def run(self) -> RobotRunResult:
            assert self.result_callback is not None
            processed_batches.append(
                (self.robot_name, [cliente.rfc for cliente in self.clientes])
            )
            for cliente in self.clientes:
                if self.robot_name == "sat32d_publico":
                    result = RobotRunResult(
                        resultado="NO_AUTORIZADO",
                        fecha_consulta=datetime(2026, 5, 25, 13, 0, 0),
                        archivo=f"C:/SAT32D/Publico/{cliente.rfc}_publico.png",
                        observaciones="El RFC o CURP consultado no se encuentra autorizado para hacerse público.",
                    )
                elif self.robot_name == "sat32d_tercero":
                    result = RobotRunResult(
                        resultado="NO_AUTORIZADO",
                        fecha_consulta=datetime(2026, 5, 25, 13, 2, 0),
                        archivo=f"C:/SAT32D/Publico/{cliente.rfc}_publico.png",
                        observaciones="El RFC o CURP consultado no se encuentra autorizado para hacerse público.",
                    )
                elif self.robot_name == "sat32d_opinion_cumplimiento":
                    result = RobotRunResult(
                        resultado="POSITIVA",
                        fecha_consulta=datetime(2026, 5, 25, 13, 5, 0),
                        archivo=f"C:/SAT32D/Opinion/{cliente.rfc}_efirma.pdf",
                        observaciones="Resultado detectado: POSITIVA.",
                    )
                else:
                    raise AssertionError(f"Robot inesperado: {self.robot_name}")
                self.result_callback(cliente, result)

            return RobotRunResult(
                resultado="FINALIZADO",
                fecha_consulta=datetime(2026, 5, 25, 13, 10, 0),
                archivo=None,
                observaciones=f"{self.robot_name} completado.",
            )

    monkeypatch.setattr(webapp_module, "LocalClientStore", lambda storage_dir: store)
    monkeypatch.setattr(
        webapp_module,
        "get_robot",
        lambda robot_name, config, logger: _FakeRobot(robot_name),
    )
    monkeypatch.setattr(
        webapp_module,
        "_has_reusable_third_party_session",
        lambda **kwargs: False,
    )
    monkeypatch.setattr(webapp_module.threading, "Thread", _FakeThread)

    app = create_web_app(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#inputRFCCURP",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                ),
            ),
        ),
        logging.getLogger("test.webapp"),
    )
    app.config["SAT32D_WEB_CREDENTIALS"]["session-1"] = {
        "rfc": "IOEF840128UC4",
        "password": "Javi1984",
    }
    app.config["SAT32D_WEB_REGISTERED_USERS"]["IOEF840128UC4"] = {
        "password_hash": "fixture-hash",
        "profile": {},
        "monitored_people": [],
    }

    with app.test_client() as client:
        with client.session_transaction() as session_state:
            session_state["user_rfc"] = "IOEF840128UC4"
            session_state["web_session_id"] = "session-1"

        response = client.post(
            "/robots/run",
            data={
                "robot_name": "sat32d_locales",
            },
        )

    assert response.status_code == 302
    updated_records = {
        record["id"]: record for record in store.list_clients(owner_rfc="IOEF840128UC4")
    }
    assert processed_batches == [
        ("sat32d_publico", ["ATG230728NE6"]),
        ("sat32d_tercero", ["ATG230728NE6"]),
        ("sat32d_opinion_cumplimiento", ["AFS230503FN1"]),
    ]
    assert updated_records["client-1"]["resultado"] == "POSITIVA"
    assert updated_records["client-1"]["archivo"] == "C:/SAT32D/Opinion/AFS230503FN1_efirma.pdf"
    assert updated_records["client-1"]["resultado_observaciones"] == "Resultado detectado: POSITIVA."
    assert updated_records["client-2"]["resultado"] == "NO_AUTORIZADO"
    assert updated_records["client-2"]["archivo"] == "C:/SAT32D/Publico/ATG230728NE6_publico.png"


def test_run_robot_from_web_keeps_publico_no_autorizado_when_efirma_fallback_errors(
    tmp_path,
    monkeypatch,
) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "IOEF840128UC4",
                    "cliente": "ACC FIRE SYSTEMS",
                    "rfc": "AFS230503FN1",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "PENDIENTE",
                    "fecha_consulta": "",
                    "archivo": "",
                    "prioridad": "",
                    "key_file_name": "cliente.key",
                    "cer_file_name": "cliente.cer",
                    "key_path": "C:/efirma/cliente.key",
                    "cer_path": "C:/efirma/cliente.cer",
                    "created_at": "2026-05-21 08:30:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)
    processed_batches: list[tuple[str, list[str]]] = []

    class _FakeThread:
        def __init__(self, target=None, kwargs=None, daemon=None):
            self.target = target
            self.kwargs = kwargs or {}
            self.daemon = daemon
            self.started = False

        def start(self):
            self.started = True
            assert self.target is not None
            self.target(**self.kwargs)

    class _FakeRobot:
        def __init__(self, robot_name: str):
            self.robot_name = robot_name
            self.clientes: list[Cliente] = []
            self.result_callback = None

        def validate(self) -> None:
            return None

        def set_clientes_override(self, clientes: list[Cliente]) -> None:
            self.clientes = clientes

        def set_result_callback(self, callback) -> None:
            self.result_callback = callback

        def set_runtime_credentials(self, *, rfc: str, password: str) -> None:
            return None

        def run(self) -> RobotRunResult:
            assert self.result_callback is not None
            processed_batches.append(
                (self.robot_name, [cliente.rfc for cliente in self.clientes])
            )
            for cliente in self.clientes:
                if self.robot_name == "sat32d_publico":
                    result = RobotRunResult(
                        resultado="NO_AUTORIZADO",
                        fecha_consulta=datetime(2026, 5, 25, 13, 0, 0),
                        archivo=f"C:/SAT32D/Publico/{cliente.rfc}_publico.png",
                        observaciones="El RFC o CURP consultado no se encuentra autorizado para hacerse público.",
                    )
                elif self.robot_name == "sat32d_opinion_cumplimiento":
                    result = RobotRunResult(
                        resultado="ERROR",
                        fecha_consulta=datetime(2026, 5, 25, 13, 5, 0),
                        archivo=f"C:/SAT32D/Errores/{cliente.rfc}_efirma.png",
                        observaciones="Certificado, clave privada o contraseña de clave privada inválidos, inténtelo nuevamente.",
                    )
                else:
                    raise AssertionError(f"Robot inesperado: {self.robot_name}")
                self.result_callback(cliente, result)

            return RobotRunResult(
                resultado="FINALIZADO",
                fecha_consulta=datetime(2026, 5, 25, 13, 10, 0),
                archivo=None,
                observaciones=f"{self.robot_name} completado.",
            )

    monkeypatch.setattr(webapp_module, "LocalClientStore", lambda storage_dir: store)
    monkeypatch.setattr(
        webapp_module,
        "get_robot",
        lambda robot_name, config, logger: _FakeRobot(robot_name),
    )
    monkeypatch.setattr(
        webapp_module,
        "_has_reusable_third_party_session",
        lambda **kwargs: False,
    )
    monkeypatch.setattr(webapp_module.threading, "Thread", _FakeThread)

    app = create_web_app(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#inputRFCCURP",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                ),
            ),
        ),
        logging.getLogger("test.webapp"),
    )
    app.config["SAT32D_WEB_CREDENTIALS"]["session-1"] = {
        "rfc": "IOEF840128UC4",
        "password": "Javi1984",
    }
    app.config["SAT32D_WEB_REGISTERED_USERS"]["IOEF840128UC4"] = {
        "password_hash": "fixture-hash",
        "profile": {},
        "monitored_people": [],
    }

    with app.test_client() as client:
        with client.session_transaction() as session_state:
            session_state["user_rfc"] = "IOEF840128UC4"
            session_state["web_session_id"] = "session-1"

        response = client.post(
            "/robots/run",
            data={
                "robot_name": "sat32d_locales",
            },
        )

    assert response.status_code == 302
    updated_record = store.list_clients(owner_rfc="IOEF840128UC4")[0]
    assert processed_batches == [
        ("sat32d_opinion_cumplimiento", ["AFS230503FN1"]),
    ]
    assert updated_record["resultado"] == "ERROR"
    assert updated_record["fecha_consulta"] == "2026-05-25 13:05:00"
    assert updated_record["archivo"] == ""
    assert (
        updated_record["resultado_observaciones"]
        == "Credenciales de e.firma invalidas. Verifica certificado, clave privada y contrasena."
    )


def test_consulta_individual_publico_retries_with_efirma_when_publico_no_autorizado(
    tmp_path,
    monkeypatch,
) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "IOEF840128UC4",
                    "cliente": "ACC FIRE SYSTEMS",
                    "rfc": "AFS230503FN1",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "PENDIENTE",
                    "fecha_consulta": "",
                    "archivo": "",
                    "prioridad": "",
                    "key_file_name": "cliente.key",
                    "cer_file_name": "cliente.cer",
                    "key_path": "C:/efirma/cliente.key",
                    "cer_path": "C:/efirma/cliente.cer",
                    "created_at": "2026-05-21 08:30:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)
    processed_batches: list[tuple[str, list[str]]] = []

    class _FakeThread:
        def __init__(self, target=None, kwargs=None, daemon=None):
            self.target = target
            self.kwargs = kwargs or {}
            self.daemon = daemon

        def start(self):
            assert self.target is not None
            self.target(**self.kwargs)

    class _FakeRobot:
        def __init__(self, robot_name: str):
            self.robot_name = robot_name
            self.clientes: list[Cliente] = []
            self.result_callback = None

        def validate(self) -> None:
            return None

        def set_clientes_override(self, clientes: list[Cliente]) -> None:
            self.clientes = clientes

        def set_result_callback(self, callback) -> None:
            self.result_callback = callback

        def set_runtime_credentials(self, *, rfc: str, password: str) -> None:
            return None

        def run(self) -> RobotRunResult:
            assert self.result_callback is not None
            processed_batches.append(
                (self.robot_name, [cliente.rfc for cliente in self.clientes])
            )
            for cliente in self.clientes:
                if self.robot_name == "sat32d_publico":
                    result = RobotRunResult(
                        resultado="NO_AUTORIZADO",
                        fecha_consulta=datetime(2026, 5, 25, 13, 0, 0),
                        archivo=f"C:/SAT32D/Publico/{cliente.rfc}_publico.png",
                        observaciones="El RFC o CURP consultado no se encuentra autorizado para hacerse público.",
                    )
                elif self.robot_name == "sat32d_opinion_cumplimiento":
                    result = RobotRunResult(
                        resultado="POSITIVA",
                        fecha_consulta=datetime(2026, 5, 25, 13, 5, 0),
                        archivo=f"C:/SAT32D/Opinion/{cliente.rfc}_efirma.pdf",
                        observaciones="Resultado detectado: POSITIVA.",
                    )
                else:
                    raise AssertionError(f"Robot inesperado: {self.robot_name}")
                self.result_callback(cliente, result)

            return RobotRunResult(
                resultado="FINALIZADO",
                fecha_consulta=datetime(2026, 5, 25, 13, 10, 0),
                archivo=None,
                observaciones=f"{self.robot_name} completado.",
            )

    monkeypatch.setattr(webapp_module, "LocalClientStore", lambda storage_dir: store)
    monkeypatch.setattr(
        webapp_module,
        "get_robot",
        lambda robot_name, config, logger: _FakeRobot(robot_name),
    )
    monkeypatch.setattr(webapp_module.threading, "Thread", _FakeThread)

    app = create_web_app(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_tercero",
            robot_config=_build_robot_config(
                third_party_query=ThirdPartyQueryConfig(
                    ready_selector="#inputRFCCURP",
                    rfc_mode_selector="#flexRadioRFC",
                    rfc_input_selector="#inputRFCCURP",
                    submit_selector="#btnConsultar",
                    download_selector="#download",
                    unauthorized_selector="#error",
                ),
            ),
        ),
        logging.getLogger("test.webapp"),
    )
    app.config["SAT32D_WEB_CREDENTIALS"]["session-1"] = {
        "rfc": "IOEF840128UC4",
        "password": "Javi1984",
    }
    app.config["SAT32D_WEB_REGISTERED_USERS"]["IOEF840128UC4"] = {
        "password_hash": "fixture-hash",
        "profile": {},
        "monitored_people": [],
    }

    with app.test_client() as client:
        with client.session_transaction() as session_state:
            session_state["user_rfc"] = "IOEF840128UC4"
            session_state["web_session_id"] = "session-1"

        response = client.post("/clientes/locales/client-1/consultar")

    assert response.status_code == 202
    updated_record = store.list_clients(owner_rfc="IOEF840128UC4")[0]
    assert processed_batches == [
        ("sat32d_publico", ["AFS230503FN1"]),
        ("sat32d_opinion_cumplimiento", ["AFS230503FN1"]),
    ]
    assert updated_record["resultado"] == "POSITIVA"
    assert updated_record["archivo"] == "C:/SAT32D/Opinion/AFS230503FN1_efirma.pdf"


def test_opinion_robot_uses_stored_efirma_and_embedded_pdf(tmp_path, monkeypatch) -> None:
    robot = SAT32DOpinionRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_opinion_cumplimiento",
            robot_config=_build_robot_config(
                start_url="https://example.com/opinion",
            ),
        ),
        logging.getLogger("test.robot_opinion"),
    )

    class _OpinionPage:
        def __init__(self) -> None:
            self.goto_calls: list[tuple[str, str | None]] = []
            self.click_calls: list[str] = []
            self.file_calls: list[tuple[str, str]] = []
            self.fill_calls: list[tuple[str, str]] = []
            self.wait_for_selector_calls: list[str] = []
            self.waits: list[int] = []
            self.url = "https://example.com/opinion"
            self._pdf_uri = "data:application/pdf;base64,ZmFrZS1wZGY="

        def goto(self, url: str, wait_until: str | None = None) -> None:
            self.goto_calls.append((url, wait_until))
            self.url = url

        def wait_for_selector(self, selector: str, state: str | None = None) -> None:
            self.wait_for_selector_calls.append(selector)

        def click(self, selector: str, **kwargs) -> None:
            del kwargs
            self.click_calls.append(selector)

        def set_input_files(self, selector: str, value: str) -> None:
            self.file_calls.append((selector, value))

        def fill(self, selector: str, value: str) -> None:
            self.fill_calls.append((selector, value))

        def wait_for_timeout(self, milliseconds: int) -> None:
            self.waits.append(milliseconds)

        def eval_on_selector(self, selector: str, _expression: str):
            if selector == "iframe":
                return self._pdf_uri
            raise RuntimeError("selector no encontrado")

        def text_content(self, selector: str) -> str:
            if selector == "body":
                return (
                    "Necesita habilitar JavaScript para ejecutar esta aplicación. "
                    "const systemResolve = System.resolve; "
                    "function (e) { return e; } "
                    "System.import('@sat-root-contribuyente/root-config'); "
                    "https://eu2dypprostamonitores.blob.core.windows.net " * 3
                )
            return ""

    page = _OpinionPage()

    @contextmanager
    def _fake_open_page(_robot_config):
        yield page

    monkeypatch.setattr("src.robots.sat32d_opinion.open_page", _fake_open_page)
    monkeypatch.setattr(
        robot,
        "_extract_pdf_text",
        lambda pdf_path: "INSTALINX SAS DE CV EN SUSPENSION DE ACTIVIDADES",
    )

    cer_path = tmp_path / "cliente.cer"
    key_path = tmp_path / "cliente.key"
    cer_path.write_bytes(b"cer")
    key_path.write_bytes(b"key")

    robot.set_clientes_override(
        [
            Cliente(
                rfc="INS240905TJ4",
                cliente="INSTALINX",
                modo_consulta="TERCERO",
                requiere_pdf="SI",
                autorizado="",
                activo="SI",
                resultado="",
                fecha_consulta="",
                archivo="",
                observaciones="",
                prioridad="",
                row_number=1,
                key_path=str(key_path),
                cer_path=str(cer_path),
                efirma_password="secreta",
            )
        ]
    )

    result = robot.run()

    assert page.goto_calls == [("https://example.com/opinion", "domcontentloaded")]
    assert page.click_calls == ["#buttonFiel", "#submit"]
    assert page.file_calls == [
        ("#fileCertificate", str(cer_path)),
        ("#filePrivateKey", str(key_path)),
    ]
    assert page.fill_calls == [("#privateKeyPassword", "secreta")]
    assert result.resultado == "FINALIZADO_EFIRMA"
    assert result.archivo is not None


def test_opinion_robot_submits_efirma_without_waiting_for_navigation(tmp_path, monkeypatch) -> None:
    robot = SAT32DOpinionRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_opinion_cumplimiento",
            robot_config=_build_robot_config(
                start_url="https://example.com/opinion",
            ),
        ),
        logging.getLogger("test.robot_opinion"),
    )

    class _OpinionPage:
        def __init__(self) -> None:
            self.click_calls: list[tuple[str, dict]] = []
            self.url = "https://example.com/opinion"
            self._pdf_uri = "data:application/pdf;base64,ZmFrZS1wZGY="

        def goto(self, url: str, wait_until: str | None = None) -> None:
            self.url = url

        def wait_for_timeout(self, milliseconds: int) -> None:
            return None

        def click(self, selector: str, **kwargs) -> None:
            self.click_calls.append((selector, kwargs))
            if selector == "#submit" and not kwargs.get("no_wait_after"):
                raise RuntimeError("Timeout 10000ms exceeded waiting for scheduled navigations to finish")

        def set_input_files(self, selector: str, value: str) -> None:
            return None

        def fill(self, selector: str, value: str) -> None:
            return None

        def eval_on_selector(self, selector: str, _expression: str):
            if selector == "iframe":
                return self._pdf_uri
            raise RuntimeError("selector no encontrado")

        def text_content(self, selector: str) -> str:
            if selector == "body":
                return (
                    "Necesita habilitar JavaScript para ejecutar esta aplicación. "
                    "const systemResolve = System.resolve; "
                    "function (e) { return e; } "
                    "System.import('@sat-root-contribuyente/root-config'); "
                    "https://eu2dypprostamonitores.blob.core.windows.net " * 3
                )
            return ""

    page = _OpinionPage()

    @contextmanager
    def _fake_open_page(_robot_config):
        yield page

    monkeypatch.setattr("src.robots.sat32d_opinion.open_page", _fake_open_page)
    monkeypatch.setattr(
        robot,
        "_extract_pdf_text",
        lambda pdf_path: "ACC FIRE SYSTEMS POSITIVA",
    )

    (tmp_path / "terceros").mkdir(parents=True, exist_ok=True)
    (tmp_path / "errores").mkdir(parents=True, exist_ok=True)
    cer_path = tmp_path / "cliente.cer"
    key_path = tmp_path / "cliente.key"
    cer_path.write_bytes(b"cer")
    key_path.write_bytes(b"key")

    result = robot._process_cliente(
        Cliente(
            rfc="AFS230503FN1",
            cliente="ACC FIRE SYSTEMS",
            modo_consulta="PUBLICO",
            requiere_pdf="SI",
            autorizado="",
            activo="SI",
            resultado="",
            fecha_consulta="",
            archivo="",
            observaciones="",
            prioridad="",
            row_number=1,
            key_path=str(key_path),
            cer_path=str(cer_path),
            efirma_password="secreta",
        )
    )

    assert result.resultado == "POSITIVA"
    assert ("#submit", {"no_wait_after": True}) in page.click_calls


def test_opinion_robot_reads_concise_efirma_error_from_error_selector(tmp_path) -> None:
    robot = SAT32DOpinionRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_opinion_cumplimiento",
            robot_config=_build_robot_config(
                start_url="https://example.com/opinion",
            ),
        ),
        logging.getLogger("test.robot_opinion"),
    )

    class _ErrorPage:
        def text_content(self, selector: str) -> str:
            values = {
                "#divError": "No se puede acceder al aplicativo porque su E.FIRMA no está vigente.",
                "body": "$(function (e) { var error = 'No se puede acceder al aplicativo porque su E.FIRMA no está vigente.'; showMsgError(error); }); Acceso con e.firma No se puede acceder al aplicativo porque su E.FIRMA no está vigente.",
            }
            if selector not in values:
                raise RuntimeError("selector no encontrado")
            return values[selector]

    assert robot._read_efirma_error(_ErrorPage()) == robot.EFIRMA_EXPIRED_MESSAGE


def test_opinion_robot_ignores_ambiguous_invalid_credentials_from_body_dump(tmp_path) -> None:
    robot = SAT32DOpinionRobot(
        _build_app_config(
            tmp_path,
            robot_name="sat32d_opinion_cumplimiento",
            robot_config=_build_robot_config(
                start_url="https://example.com/opinion",
            ),
        ),
        logging.getLogger("test.robot_opinion"),
    )

    class _ErrorPage:
        def text_content(self, selector: str) -> str:
            if selector == "#divError":
                raise RuntimeError("selector no encontrado")
            if selector == "body":
                return "Interruptor de Navegación Trámites Gobierno Búsqueda $(function (e) { var error = ''; if (new String(error).valueOf() == new String('Certificado Revocado').valueOf()) { error = 'No se puede acceder al aplicativo porque su E.FIRMA está revocada.'; } if (new String(error).valueOf() == new String('Certificado Caduco').valueOf()) { error = 'No se puede acceder al aplicativo porque su E.FIRMA no está vigente.'; } showMsgError(error); }); Inicio Acceso con e.firma Certificado, clave privada o contraseña de clave privada inválidos, inténtelo nuevamente. Certificado (.cer): Buscar"
            raise RuntimeError("selector no encontrado")

    assert robot._read_efirma_error(_ErrorPage()) == ""


def test_select_local_records_for_robot_tercero_requires_target_client_id() -> None:
    with pytest.raises(ValueError, match="requiere un cliente local especifico"):
        _select_local_records_for_robot(
            [
                {
                    "id": "1",
                    "rfc": "AAA010101AAA",
                    "modo_consulta": "TERCERO",
                    "activo": "SI",
                    "requiere_pdf": "SI",
                    "resultado": "DESCARGADO",
                }
            ],
            robot_name="sat32d_tercero",
            target_client_id=None,
            include_captcha_pendiente=False,
        )


def test_select_local_records_for_robot_tercero_returns_requested_client() -> None:
    selected = _select_local_records_for_robot(
        [
            {
                "id": "1",
                "rfc": "AAA010101AAA",
                "modo_consulta": "TERCERO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "resultado": "DESCARGADO",
            },
            {
                "id": "2",
                "rfc": "BBB010101BBB",
                "modo_consulta": "TERCERO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "resultado": "NEGATIVA",
            },
        ],
        robot_name="sat32d_tercero",
        target_client_id="2",
        include_captcha_pendiente=False,
    )

    assert [record["id"] for record in selected] == ["2"]


def test_select_local_records_for_robot_opinion_returns_requested_efirma_client() -> None:
    selected = _select_local_records_for_robot(
        [
            {
                "id": "1",
                "rfc": "AAA010101AAA",
                "modo_consulta": "PUBLICO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "resultado": "NO_AUTORIZADO",
                "key_path": "demo-1.key",
                "cer_path": "demo-1.cer",
                "efirma_password": "secreta",
            },
            {
                "id": "2",
                "rfc": "BBB010101BBB",
                "modo_consulta": "PUBLICO",
                "activo": "SI",
                "requiere_pdf": "SI",
                "resultado": "NEGATIVA",
                "key_path": "demo-2.key",
                "cer_path": "demo-2.cer",
                "efirma_password": "secreta",
            },
        ],
        robot_name="sat32d_opinion_cumplimiento",
        target_client_id="1",
        include_captcha_pendiente=False,
    )

    assert [record["id"] for record in selected] == ["1"]


def test_select_local_records_for_robot_opinion_rejects_client_without_efirma() -> None:
    with pytest.raises(ValueError):
        _select_local_records_for_robot(
            [
                {
                    "id": "1",
                    "rfc": "AAA010101AAA",
                    "modo_consulta": "PUBLICO",
                    "activo": "SI",
                    "requiere_pdf": "SI",
                    "resultado": "PENDIENTE",
                    "key_path": "",
                    "cer_path": "",
                    "efirma_password": "",
                }
            ],
            robot_name="sat32d_opinion_cumplimiento",
            target_client_id="1",
            include_captcha_pendiente=False,
        )


def test_sync_registered_monitored_clients_adds_records_consultable_by_robot(tmp_path) -> None:
    store = LocalClientStore(tmp_path / "local_clients")

    summary = store.sync_registered_monitored_clients(
        owner_rfc="AAA010101AAA",
        monitored_people=[
            {
                "rfc": "BBB010101BBB",
                "nombre": "Cliente publico",
                "modo_consulta": "PUBLICO",
                "requiere_pdf": "NO",
            },
            {
                "rfc": "CCC010101CCC",
                "nombre": "Cliente tercero",
                "modo_consulta": "TERCERO",
                "requiere_pdf": "SI",
            },
        ],
        synced_at="2026-05-18 17:00:00",
    )

    records = store.list_clients(owner_rfc="AAA010101AAA")
    grouped = _select_local_records_for_local_consulta(
        records,
        include_captcha_pendiente=False,
    )

    assert summary == {"created": 2, "updated": 0, "removed": 0}
    assert [record["rfc"] for record in grouped["sat32d_publico"]] == ["BBB010101BBB"]
    assert grouped["sat32d_tercero"] == []
    assert all(record["source"] == LocalClientStore.REGISTER_MONITOR_SOURCE for record in records)


def test_local_client_store_loads_utf8_sig_json(tmp_path) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "AAA010101AAA",
                }
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8-sig",
    )

    store = LocalClientStore(storage_dir)

    records = store.list_clients(owner_rfc="AAA010101AAA")

    assert len(records) == 1
    assert records[0]["cliente"] == "Cliente demo"


def test_sync_registered_monitored_clients_preserves_manual_local_client_without_duplicate(tmp_path) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "manual-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente manual",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-18 16:00:00",
                    "archivo": "",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                },
                {
                    "id": "sync-old",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente viejo",
                    "rfc": "CCC010101CCC",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "NO",
                    "activo": "SI",
                    "efirma_password": "",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "PENDIENTE",
                    "fecha_consulta": "",
                    "archivo": "",
                    "prioridad": "",
                    "key_file_name": "",
                    "cer_file_name": "",
                    "key_path": "",
                    "cer_path": "",
                    "created_at": "2026-05-18 15:30:00",
                    "source": LocalClientStore.REGISTER_MONITOR_SOURCE,
                },
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)

    summary = store.sync_registered_monitored_clients(
        owner_rfc="AAA010101AAA",
        monitored_people=[
            {
                "rfc": "BBB010101BBB",
                "nombre": "Cliente monitor duplicado",
                "modo_consulta": "PUBLICO",
                "requiere_pdf": "NO",
            },
            {
                "rfc": "DDD010101DDD",
                "nombre": "Cliente nuevo",
                "modo_consulta": "PUBLICO",
                "requiere_pdf": "NO",
            },
        ],
        synced_at="2026-05-18 17:00:00",
    )

    records = store.list_clients(owner_rfc="AAA010101AAA")

    assert summary == {"created": 1, "updated": 0, "removed": 1}
    assert [record["rfc"] for record in records] == ["DDD010101DDD", "BBB010101BBB"]
    assert records[0]["source"] == LocalClientStore.REGISTER_MONITOR_SOURCE
    assert records[1]["source"] == "LOCAL_STORAGE"


def test_extract_rfc_from_subject_identifier_prefers_client_rfc() -> None:
    result = extract_rfc_from_subject_identifier(
        "GSI210118TQ1 / IOEF840128UC4"
    )

    assert result == "GSI210118TQ1"


def test_extract_certificate_rfc_from_file_falls_back_to_powershell(monkeypatch, tmp_path) -> None:
    cert_path = tmp_path / "demo.cer"
    cert_path.write_bytes(b"demo")
    monkeypatch.setattr(
        certificate_utils,
        "extract_certificate_rfc_from_bytes",
        lambda cert_bytes: (_ for _ in ()).throw(ValueError("parse failure")),
    )
    monkeypatch.setattr(
        certificate_utils.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="OID.2.5.4.45=FNA220228RJ8 / VAFJ410816DD1",
        ),
    )

    result = certificate_utils.extract_certificate_rfc_from_file(cert_path)

    assert result == "FNA220228RJ8"


def test_update_client_result_preserves_existing_opinion_result_on_runtime_error(tmp_path) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "Resultado detectado: POSITIVA.",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-18 16:00:00",
                    "archivo": "C:/SAT32D/Publico/demo.png",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)

    updated = store.update_client_result(
        "client-1",
        resultado="ERROR",
        fecha_consulta="2026-05-18 17:00:00",
        archivo="C:/SAT32D/Errores/demo_error.png",
        observaciones="Error de acceso.",
    )

    assert updated["resultado"] == "ERROR"
    assert updated["resultado_observaciones"] == "Error de acceso."
    assert updated["archivo"] == "C:/SAT32D/Errores/demo_error.png"
    assert updated["fecha_consulta"] == "2026-05-18 17:00:00"


def test_update_client_result_stores_access_error_when_no_prior_opinion(tmp_path) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "PENDIENTE",
                    "fecha_consulta": "",
                    "archivo": "",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)

    updated = store.update_client_result(
        "client-1",
        resultado="NO_AUTORIZADO",
        fecha_consulta="2026-05-18 17:00:00",
        archivo="C:/SAT32D/Errores/demo_error.png",
        observaciones="No autorizado.",
    )

    assert updated["resultado"] == "NO_AUTORIZADO"
    assert updated["resultado_observaciones"] == "No autorizado."
    assert updated["archivo"] == "C:/SAT32D/Errores/demo_error.png"


def test_update_client_result_marks_err_connection_reset_as_pending(tmp_path) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "PENDIENTE",
                    "fecha_consulta": "",
                    "archivo": "",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)

    updated = store.update_client_result(
        "client-1",
        resultado="ERROR",
        fecha_consulta="2026-06-01 10:00:00",
        archivo="C:/SAT32D/Errores/demo_error.png",
        observaciones=(
            "Fallo Playwright durante la ejecucion: Page.goto: net::ERR_CONNECTION_RESET "
            "at https://ptsc32d.clouda.sat.gob.mx/?/reporteOpinion32DContribuyente"
        ),
    )

    assert updated["resultado"] == "PENDIENTE"
    assert updated["archivo"] == ""
    assert "ERR_CONNECTION_RESET" in updated["resultado_observaciones"]


def test_update_client_result_clears_artifact_for_efirma_invalid_credentials_error(tmp_path) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "PENDIENTE",
                    "fecha_consulta": "",
                    "archivo": "",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)

    updated = store.update_client_result(
        "client-1",
        resultado="ERROR",
        fecha_consulta="2026-06-01 10:05:00",
        archivo="C:/SAT32D/Errores/BBB010101BBB_efirma.png",
        observaciones="Certificado, clave privada o contraseña de clave privada inválidos, inténtelo nuevamente.",
    )

    assert updated["resultado"] == "ERROR"
    assert updated["archivo"] == ""
    assert (
        updated["resultado_observaciones"]
        == "Credenciales de e.firma invalidas. Verifica certificado, clave privada y contrasena."
    )


def test_update_client_result_replaces_inherited_tercero_result_during_publico_run(tmp_path) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "Resultado detectado: POSITIVA. PDF obtenido correctamente con e.firma.",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-18 16:00:00",
                    "archivo": "C:/SAT32D/Terceros/BBB010101BBB_32D_contribuyente_2026-05-18.pdf",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)

    updated = store.update_client_result(
        "client-1",
        resultado="NO_AUTORIZADO",
        fecha_consulta="2026-05-18 17:00:00",
        archivo="C:/SAT32D/Publico/BBB010101BBB_32D_2026-05-18.png",
        observaciones="El RFC o CURP consultado no se encuentra autorizado para hacerse público.",
    )

    assert updated["resultado"] == "NO_AUTORIZADO"
    assert updated["resultado_observaciones"] == (
        "El RFC o CURP consultado no se encuentra autorizado para hacerse público."
    )
    assert updated["archivo"] == "C:/SAT32D/Publico/BBB010101BBB_32D_2026-05-18.png"


def test_update_client_result_preserves_last_successful_snapshot_when_publico_turns_no_autorizado(
    tmp_path,
) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "Resultado detectado: POSITIVA.",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-18 16:00:00",
                    "archivo": "C:/SAT32D/Publico/BBB010101BBB_32D_2026-05-17.png",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)

    updated = store.update_client_result(
        "client-1",
        resultado="NO_AUTORIZADO",
        fecha_consulta="2026-05-18 17:00:00",
        archivo="C:/SAT32D/Publico/BBB010101BBB_32D_2026-05-18.png",
        observaciones="El RFC o CURP consultado no se encuentra autorizado para hacerse público.",
    )

    assert updated["resultado"] == "NO_AUTORIZADO"
    assert updated["ultimo_resultado_exitoso"] == "POSITIVA"
    assert updated["ultima_fecha_consulta_exitosa"] == "2026-05-18 16:00:00"
    assert updated["ultimo_archivo_exitoso"] == "C:/SAT32D/Publico/BBB010101BBB_32D_2026-05-17.png"
    assert updated["ultimas_observaciones_exitosas"] == "Resultado detectado: POSITIVA."


def test_update_client_result_replaces_prior_publico_result_when_sat_returns_no_autorizado(tmp_path) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "Resultado detectado: POSITIVA.",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-18 16:00:00",
                    "archivo": "C:/SAT32D/Publico/BBB010101BBB_32D_2026-05-17.png",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)

    updated = store.update_client_result(
        "client-1",
        resultado="NO_AUTORIZADO",
        fecha_consulta="2026-05-18 17:00:00",
        archivo="C:/SAT32D/Publico/BBB010101BBB_32D_2026-05-18.png",
        observaciones="El RFC o CURP consultado no se encuentra autorizado para hacerse público.",
    )

    assert updated["resultado"] == "NO_AUTORIZADO"
    assert updated["resultado_observaciones"] == (
        "El RFC o CURP consultado no se encuentra autorizado para hacerse público."
    )
    assert updated["archivo"] == "C:/SAT32D/Publico/BBB010101BBB_32D_2026-05-18.png"


def test_update_client_result_updates_last_successful_snapshot_on_success(tmp_path) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "PENDIENTE",
                    "fecha_consulta": "",
                    "archivo": "",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)

    updated = store.update_client_result(
        "client-1",
        resultado="POSITIVA",
        fecha_consulta="2026-05-18 17:00:00",
        archivo="C:/SAT32D/Publico/BBB010101BBB_32D_2026-05-18.png",
        observaciones="Resultado detectado: POSITIVA.",
    )

    assert updated["resultado"] == "POSITIVA"
    assert updated["ultimo_resultado_exitoso"] == "POSITIVA"
    assert updated["ultima_fecha_consulta_exitosa"] == "2026-05-18 17:00:00"
    assert updated["ultimo_archivo_exitoso"] == "C:/SAT32D/Publico/BBB010101BBB_32D_2026-05-18.png"
    assert updated["ultimas_observaciones_exitosas"] == "Resultado detectado: POSITIVA."


def test_normalize_manual_authorization_pending_resets_stale_public_no_autorizado(tmp_path) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente A",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "El RFC o CURP consultado no se encuentra autorizado para hacerse publico.",
                    "resultado": "NO_AUTORIZADO",
                    "fecha_consulta": "2026-05-18 17:00:00",
                    "archivo": "C:/SAT32D/Publico/BBB010101BBB_32D_2026-05-18.png",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                },
                {
                    "id": "client-2",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente B",
                    "rfc": "CCC010101CCC",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "",
                    "notas_cliente": "",
                    "resultado_observaciones": "El RFC o CURP consultado no se encuentra autorizado para hacerse publico.",
                    "resultado": "NO_AUTORIZADO",
                    "fecha_consulta": "2026-05-18 17:00:00",
                    "archivo": "C:/SAT32D/Publico/CCC010101CCC_32D_2026-05-18.png",
                    "prioridad": "",
                    "key_file_name": "",
                    "cer_file_name": "",
                    "key_path": "",
                    "cer_path": "",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                },
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)

    updated_count = store.normalize_manual_authorization_pending(owner_rfc="AAA010101AAA")

    assert updated_count == 1
    records = {record["id"]: record for record in store.list_clients(owner_rfc="AAA010101AAA")}
    assert records["client-1"]["resultado"] == "PENDIENTE"
    assert records["client-1"]["fecha_consulta"] == ""
    assert records["client-1"]["archivo"] == ""
    assert "pendiente" in records["client-1"]["resultado_observaciones"].lower()
    assert records["client-2"]["resultado"] == "NO_AUTORIZADO"


def test_normalize_public_png_pending_for_efirma_resets_successful_public_png(tmp_path) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente A",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "Resultado detectado: POSITIVA.",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-18 17:00:00",
                    "archivo": "C:/SAT32D/Publico/BBB010101BBB_32D_2026-05-18.png",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                },
                {
                    "id": "client-2",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente B",
                    "rfc": "CCC010101CCC",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "Resultado detectado: POSITIVA.",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-18 17:00:00",
                    "archivo": "C:/SAT32D/Publico/CCC010101CCC_32D_2026-05-18.pdf",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                },
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)

    updated_count = store.normalize_public_png_pending_for_efirma(owner_rfc="AAA010101AAA")

    assert updated_count == 1
    records = {record["id"]: record for record in store.list_clients(owner_rfc="AAA010101AAA")}
    assert records["client-1"]["resultado"] == "PENDIENTE"
    assert records["client-1"]["fecha_consulta"] == ""
    assert records["client-1"]["archivo"] == ""
    assert "png" in records["client-1"]["resultado_observaciones"].lower()
    assert records["client-1"]["ultimo_resultado_exitoso"] == "POSITIVA"
    assert records["client-1"]["ultimo_archivo_exitoso"].endswith(".png")
    assert records["client-2"]["resultado"] == "POSITIVA"
    assert records["client-2"]["archivo"].endswith(".pdf")


def test_normalize_public_png_pending_for_efirma_ignores_non_eligible_records(tmp_path) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Sin e.firma",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "",
                    "notas_cliente": "",
                    "resultado_observaciones": "Resultado detectado: POSITIVA.",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-18 17:00:00",
                    "archivo": "C:/SAT32D/Publico/BBB010101BBB_32D_2026-05-18.png",
                    "prioridad": "",
                    "key_file_name": "",
                    "cer_file_name": "",
                    "key_path": "",
                    "cer_path": "",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                },
                {
                    "id": "client-2",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Modo tercero",
                    "rfc": "CCC010101CCC",
                    "modo_consulta": "TERCERO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "Resultado detectado: POSITIVA.",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-18 17:00:00",
                    "archivo": "C:/SAT32D/Publico/CCC010101CCC_32D_2026-05-18.png",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": "demo.key",
                    "cer_path": "demo.cer",
                    "created_at": "2026-05-18 15:00:00",
                    "source": "LOCAL_STORAGE",
                },
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)

    updated_count = store.normalize_public_png_pending_for_efirma(owner_rfc="AAA010101AAA")

    assert updated_count == 0
    records = {record["id"]: record for record in store.list_clients(owner_rfc="AAA010101AAA")}
    assert records["client-1"]["resultado"] == "POSITIVA"
    assert records["client-1"]["archivo"].endswith(".png")
    assert records["client-2"]["resultado"] == "POSITIVA"
    assert records["client-2"]["archivo"].endswith(".png")


def test_sync_client_rfcs_from_certificates_updates_mismatched_records(tmp_path, monkeypatch) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    cert_a = tmp_path / "client-a.cer"
    cert_b = tmp_path / "client-b.cer"
    cert_a.write_bytes(b"a")
    cert_b.write_bytes(b"b")
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente A",
                    "rfc": "AAA010101AAA",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "Resultado previo equivocado.",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-19 11:30:00",
                    "archivo": "C:/SAT32D/Publico/AAA010101AAA_demo.png",
                    "prioridad": "",
                    "key_file_name": "a.key",
                    "cer_file_name": cert_a.name,
                    "key_path": "a.key",
                    "cer_path": str(cert_a),
                    "created_at": "2026-05-19 11:00:00",
                    "source": "LOCAL_STORAGE",
                },
                {
                    "id": "client-2",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente B",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "PENDIENTE",
                    "fecha_consulta": "",
                    "archivo": "",
                    "prioridad": "",
                    "key_file_name": "b.key",
                    "cer_file_name": cert_b.name,
                    "key_path": "b.key",
                    "cer_path": str(cert_b),
                    "created_at": "2026-05-19 11:10:00",
                    "source": "LOCAL_STORAGE",
                },
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)
    mapped_rfcs = {
        str(cert_a): "GSI210118TQ1",
        str(cert_b): "BBB010101BBB",
    }
    monkeypatch.setattr(
        "src.local_client_store.extract_certificate_rfc_from_file",
        lambda cert_path: mapped_rfcs[str(cert_path)],
    )

    summary = store.sync_client_rfcs_from_certificates(owner_rfc="AAA010101AAA")
    records = store.list_clients(owner_rfc="AAA010101AAA")

    assert summary == {"updated": 1, "skipped": 0}
    assert [record["rfc"] for record in records] == ["BBB010101BBB", "GSI210118TQ1"]
    corrected_record = next(record for record in records if record["id"] == "client-1")
    assert corrected_record["resultado"] == "PENDIENTE"
    assert corrected_record["fecha_consulta"] == ""
    assert corrected_record["archivo"] == ""
    assert corrected_record["resultado_observaciones"] == ""


def test_sync_client_rfcs_from_certificates_repairs_moved_upload_paths(tmp_path, monkeypatch) -> None:
    storage_dir = tmp_path / "local_clients"
    uploads_dir = storage_dir / "uploads" / "AAA010101AAA" / "client-1"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    actual_key_path = uploads_dir / "demo.key"
    actual_cer_path = uploads_dir / "demo.cer"
    actual_key_path.write_bytes(b"key")
    actual_cer_path.write_bytes(b"cer")
    old_key_path = (
        "C:\\Users\\old-user\\OneDrive\\Auto32D\\web_storage\\local_clients\\uploads"
        "\\AAA010101AAA\\client-1\\demo.key"
    )
    old_cer_path = (
        "C:\\Users\\old-user\\OneDrive\\Auto32D\\web_storage\\local_clients\\uploads"
        "\\AAA010101AAA\\client-1\\demo.cer"
    )
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "AAA010101AAA",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "PENDIENTE",
                    "fecha_consulta": "",
                    "archivo": "",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": "demo.cer",
                    "key_path": old_key_path,
                    "cer_path": old_cer_path,
                    "created_at": "2026-05-19 11:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)
    monkeypatch.setattr(
        "src.local_client_store.extract_certificate_rfc_from_file",
        lambda cert_path: "AAA010101AAA" if cert_path == actual_cer_path else "",
    )

    summary = store.sync_client_rfcs_from_certificates(owner_rfc="AAA010101AAA")
    records = store.list_clients(owner_rfc="AAA010101AAA")

    assert summary == {"updated": 1, "skipped": 0}
    assert records[0]["cer_path"] == str(actual_cer_path)
    assert records[0]["key_path"] == str(actual_key_path)


def test_sync_client_rfcs_from_certificates_resets_stale_result_when_artifact_rfc_mismatches(tmp_path, monkeypatch) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    cert_path = tmp_path / "client.cer"
    cert_path.write_bytes(b"cer")
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "GSI210118TQ1",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "Resultado heredado de otro RFC.",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-19 11:30:00",
                    "archivo": "C:/SAT32D/Publico/IOEF840128UC4_32D_2026-05-19.png",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": cert_path.name,
                    "key_path": "demo.key",
                    "cer_path": str(cert_path),
                    "created_at": "2026-05-19 11:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)
    monkeypatch.setattr(
        "src.local_client_store.extract_certificate_rfc_from_file",
        lambda cert_path: "GSI210118TQ1",
    )

    summary = store.sync_client_rfcs_from_certificates(owner_rfc="AAA010101AAA")
    record = store.list_clients(owner_rfc="AAA010101AAA")[0]

    assert summary == {"updated": 1, "skipped": 0}
    assert record["resultado"] == "PENDIENTE"
    assert record["fecha_consulta"] == ""
    assert record["archivo"] == ""
    assert record["resultado_observaciones"] == ""


def test_add_client_rejects_rfc_mismatch_against_certificate(tmp_path, monkeypatch) -> None:
    store = LocalClientStore(tmp_path / "local_clients")
    monkeypatch.setattr(
        "src.local_client_store.extract_certificate_rfc_from_file",
        lambda cert_path: "GSI210118TQ1",
    )
    key_file = FileStorage(stream=BytesIO(b"demo-key"), filename="demo.key")
    cer_file = FileStorage(stream=BytesIO(b"demo-cer"), filename="demo.cer")

    with pytest.raises(ValueError, match="no coincide con el certificado"):
        store.add_client(
            owner_rfc="AAA010101AAA",
            payload={
                "cliente": "Cliente demo",
                "rfc": "IOEF840128UC4",
                "modo_consulta": "PUBLICO",
                "requiere_pdf": "SI",
                "activo": "SI",
                "efirma_password": "secreta",
                "observaciones": "",
                "created_at": "2026-05-19 12:00:00",
            },
            key_file=key_file,
            cer_file=cer_file,
        )

    assert store.list_clients(owner_rfc="AAA010101AAA") == []


def test_update_client_rejects_rfc_mismatch_against_existing_certificate(tmp_path, monkeypatch) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    cert_path = tmp_path / "client.cer"
    cert_path.write_bytes(b"cer")
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "GSI210118TQ1",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "PENDIENTE",
                    "fecha_consulta": "",
                    "archivo": "",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": cert_path.name,
                    "key_path": "demo.key",
                    "cer_path": str(cert_path),
                    "created_at": "2026-05-19 11:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)
    monkeypatch.setattr(
        "src.local_client_store.extract_certificate_rfc_from_file",
        lambda cert_path: "GSI210118TQ1",
    )

    with pytest.raises(ValueError, match="no coincide con el certificado"):
        store.update_client(
            "client-1",
            owner_rfc="AAA010101AAA",
            payload={
                "cliente": "Cliente demo",
                "rfc": "IOEF840128UC4",
                "modo_consulta": "PUBLICO",
                "requiere_pdf": "SI",
                "activo": "SI",
                "efirma_password": "secreta",
                "observaciones": "",
            },
        )


def test_update_client_replaces_efirma_files_and_resets_result(tmp_path, monkeypatch) -> None:
    storage_dir = tmp_path / "local_clients"
    client_dir = storage_dir / "uploads" / "AAA010101AAA" / "client-1"
    client_dir.mkdir(parents=True, exist_ok=True)
    old_key_path = client_dir / "old.key"
    old_cer_path = client_dir / "old.cer"
    old_key_path.write_bytes(b"old-key")
    old_cer_path.write_bytes(b"old-cer")
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "GSI210118TQ1",
                    "modo_consulta": "TERCERO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "anterior",
                    "notas_cliente": "nota vieja",
                    "resultado_observaciones": "e.firma vencida",
                    "resultado": "ERROR",
                    "fecha_consulta": "2026-05-19 11:00:00",
                    "archivo": "C:/SAT32D/Errores/client-1.png",
                    "prioridad": "",
                    "key_file_name": "old.key",
                    "cer_file_name": "old.cer",
                    "key_path": str(old_key_path),
                    "cer_path": str(old_cer_path),
                    "created_at": "2026-05-19 10:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)
    monkeypatch.setattr(
        "src.local_client_store.extract_certificate_rfc_from_file",
        lambda cert_path: "GSI210118TQ1",
    )

    updated = store.update_client(
        "client-1",
        owner_rfc="AAA010101AAA",
        payload={
            "cliente": "Cliente demo actualizado",
            "rfc": "GSI210118TQ1",
            "modo_consulta": "TERCERO",
            "requiere_pdf": "NO",
            "activo": "SI",
            "efirma_password": "nueva-secreta",
            "observaciones": "nota nueva",
        },
        key_file=FileStorage(stream=BytesIO(b"new-key"), filename="renewed.key"),
        cer_file=FileStorage(stream=BytesIO(b"new-cer"), filename="renewed.cer"),
    )

    reloaded = store.get_client("client-1", owner_rfc="AAA010101AAA")

    assert updated["key_file_name"] == "renewed.key"
    assert updated["cer_file_name"] == "renewed.cer"
    assert reloaded is not None
    assert reloaded["resultado"] == "PENDIENTE"
    assert reloaded["fecha_consulta"] == ""
    assert reloaded["archivo"] == ""
    assert reloaded["resultado_observaciones"] == ""
    assert reloaded["key_file_name"] == "renewed.key"
    assert reloaded["cer_file_name"] == "renewed.cer"
    assert Path(reloaded["key_path"]).read_bytes() == b"new-key"
    assert Path(reloaded["cer_path"]).read_bytes() == b"new-cer"
    assert not old_key_path.exists()
    assert not old_cer_path.exists()


def test_edit_local_client_route_allows_replacing_efirma_files(tmp_path, monkeypatch) -> None:
    storage_dir = tmp_path / "local_clients"
    client_dir = storage_dir / "uploads" / "AAA010101AAA" / "client-1"
    client_dir.mkdir(parents=True, exist_ok=True)
    old_key_path = client_dir / "old.key"
    old_cer_path = client_dir / "old.cer"
    old_key_path.write_bytes(b"old-key")
    old_cer_path.write_bytes(b"old-cer")
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "GSI210118TQ1",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-19 11:00:00",
                    "archivo": "C:/SAT32D/Publico/demo.png",
                    "prioridad": "",
                    "key_file_name": "old.key",
                    "cer_file_name": "old.cer",
                    "key_path": str(old_key_path),
                    "cer_path": str(old_cer_path),
                    "created_at": "2026-05-19 10:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)
    monkeypatch.setattr(webapp_module, "LocalClientStore", lambda storage_dir: store)
    monkeypatch.setattr(
        "src.local_client_store.extract_certificate_rfc_from_file",
        lambda cert_path: "GSI210118TQ1",
    )

    app = create_web_app(
        _build_app_config(
            tmp_path,
            robot_name="test_robot",
            robot_config=_build_robot_config(),
        ),
        logging.getLogger("test.webapp"),
    )
    app.config["SAT32D_WEB_ALLOWED_RFCS"].add("AAA010101AAA")
    app.config["SAT32D_WEB_CREDENTIALS"]["session-aaa"] = {
        "rfc": "AAA010101AAA",
        "password": "Javi1984",
    }

    with app.test_client() as client:
        with client.session_transaction() as session_state:
            session_state["user_rfc"] = "AAA010101AAA"
            session_state["web_session_id"] = "session-aaa"

        response = client.post(
            "/clientes/locales/client-1/editar",
            data={
                "cliente": "Cliente demo",
                "rfc": "GSI210118TQ1",
                "modo_consulta": "TERCERO",
                "requiere_pdf": "NO",
                "activo": "SI",
                "efirma_password": "secreta-renovada",
                "observaciones": "renovada",
                "key_file": (BytesIO(b"route-key"), "route.key"),
                "cer_file": (BytesIO(b"route-cer"), "route.cer"),
            },
            content_type="multipart/form-data",
        )

    updated = store.get_client("client-1", owner_rfc="AAA010101AAA")

    assert response.status_code == 302
    assert updated is not None
    assert updated["modo_consulta"] == "TERCERO"
    assert updated["requiere_pdf"] == "NO"
    assert updated["efirma_password"] == "secreta-renovada"
    assert updated["key_file_name"] == "route.key"
    assert updated["cer_file_name"] == "route.cer"
    assert Path(updated["key_path"]).read_bytes() == b"route-key"
    assert Path(updated["cer_path"]).read_bytes() == b"route-cer"


def test_exportar_clientes_respects_active_filters(tmp_path, monkeypatch) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente Uno",
                    "rfc": "AAA010101AAA",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-26 10:00:00",
                    "observaciones": "alpha visible",
                    "created_at": "2026-05-26 09:00:00",
                    "cer_path": "",
                },
                {
                    "id": "client-2",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente Dos",
                    "rfc": "BBB010101BBB",
                    "modo_consulta": "TERCERO",
                    "requiere_pdf": "SI",
                    "activo": "NO",
                    "resultado": "ERROR",
                    "fecha_consulta": "2026-05-26 11:00:00",
                    "observaciones": "alpha oculto",
                    "created_at": "2026-05-26 08:00:00",
                    "cer_path": "",
                },
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)
    monkeypatch.setattr(webapp_module, "LocalClientStore", lambda storage_dir: store)

    app = create_web_app(
        _build_app_config(
            tmp_path,
            robot_name="test_robot",
            robot_config=_build_robot_config(),
        ),
        logging.getLogger("test.webapp"),
    )
    app.config["SAT32D_WEB_ALLOWED_RFCS"].add("AAA010101AAA")
    app.config["SAT32D_WEB_CREDENTIALS"]["session-aaa"] = {
        "rfc": "AAA010101AAA",
        "password": "Javi1984",
    }

    with app.test_client() as client:
        with client.session_transaction() as session_state:
            session_state["user_rfc"] = "AAA010101AAA"
            session_state["web_session_id"] = "session-aaa"

        response = client.get(
            "/clientes/exportar",
            query_string={
                "query": "visible",
                "modo": "PUBLICO",
                "activo": "SI",
                "resultado": "POSITIVA",
            },
        )

    assert response.status_code == 200
    assert response.headers["Content-Type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    workbook = load_workbook(filename=BytesIO(response.data))
    worksheet = workbook.active
    exported_rows = [
        row
        for row in worksheet.iter_rows(min_row=5, max_col=7, values_only=True)
        if row[1]
    ]

    assert exported_rows == [
        (
            "Cliente Uno",
            "AAA010101AAA",
            "PUBLICO",
            "SI",
            "POSITIVA",
            "2026-05-26 10:00:00",
            "alpha visible",
        )
    ]
    assert worksheet["A5"].value == "Cliente Uno"


def test_update_client_resets_stale_result_when_certified_rfc_changes(tmp_path, monkeypatch) -> None:
    storage_dir = tmp_path / "local_clients"
    storage_dir.mkdir(parents=True, exist_ok=True)
    cert_path = tmp_path / "client.cer"
    cert_path.write_bytes(b"cer")
    (storage_dir / "clients.json").write_text(
        json.dumps(
            [
                {
                    "id": "client-1",
                    "owner_rfc": "AAA010101AAA",
                    "cliente": "Cliente demo",
                    "rfc": "IOEF840128UC4",
                    "modo_consulta": "PUBLICO",
                    "requiere_pdf": "SI",
                    "activo": "SI",
                    "efirma_password": "secreta",
                    "notas_cliente": "",
                    "resultado_observaciones": "Resultado previo equivocado.",
                    "resultado": "POSITIVA",
                    "fecha_consulta": "2026-05-19 11:30:00",
                    "archivo": "C:/SAT32D/Publico/IOEF840128UC4_demo.png",
                    "prioridad": "",
                    "key_file_name": "demo.key",
                    "cer_file_name": cert_path.name,
                    "key_path": "demo.key",
                    "cer_path": str(cert_path),
                    "created_at": "2026-05-19 11:00:00",
                    "source": "LOCAL_STORAGE",
                }
            ],
            ensure_ascii=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    store = LocalClientStore(storage_dir)
    monkeypatch.setattr(
        "src.local_client_store.extract_certificate_rfc_from_file",
        lambda cert_path: "GSI210118TQ1",
    )

    updated = store.update_client(
        "client-1",
        owner_rfc="AAA010101AAA",
        payload={
            "cliente": "Cliente demo",
            "rfc": "GSI210118TQ1",
            "modo_consulta": "PUBLICO",
            "requiere_pdf": "SI",
            "activo": "SI",
            "efirma_password": "secreta",
            "observaciones": "",
        },
    )

    assert updated["rfc"] == "GSI210118TQ1"
    assert updated["resultado"] == "PENDIENTE"
    assert updated["fecha_consulta"] == ""
    assert updated["archivo"] == ""
    assert updated["resultado_observaciones"] == ""
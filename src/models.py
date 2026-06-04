from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


PENDING_RESULT_STATUSES = {"", "PENDIENTE"}
EXPLICIT_OPINION_RESULT_STATUSES = {
    "POSITIVA",
    "NEGATIVA",
    "SUSPENSIÓN",
    "SIN OBLIGACIONES",
}
TERMINAL_RESULT_STATUSES = {
    "CONSULTADO",
    "DESCARGADO",
    "NO_AUTORIZADO",
    "ERROR",
    "CAPTCHA_PENDIENTE",
    "POSITIVA",
    "NEGATIVA",
    "SUSPENSI\u00d3N",
    "SIN OBLIGACIONES",
}


@dataclass(frozen=True)
class ProjectPaths:
    control: Path
    publico: Path
    terceros: Path
    errores: Path
    logs: Path


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    file_name: str


@dataclass(frozen=True)
class CaptureConfig:
    enabled: bool
    file_name: str
    full_page: bool


@dataclass(frozen=True)
class DownloadConfig:
    enabled: bool
    trigger_selector: str
    target_subdir: str


@dataclass(frozen=True)
class LoginConfig:
    enabled: bool
    username: str
    password: str
    username_selector: str
    password_selector: str
    submit_selector: str


@dataclass(frozen=True)
class PublicQueryConfig:
    rfc_input_selector: str
    submit_selector: str
    result_selector: str
    unauthorized_selector: str


@dataclass(frozen=True)
class ThirdPartyQueryConfig:
    ready_selector: str
    rfc_mode_selector: str
    rfc_input_selector: str
    submit_selector: str
    download_selector: str
    unauthorized_selector: str


@dataclass(frozen=True)
class ThirdPartyAuthorizationConfig:
    ready_selector: str
    rfc_input_selector: str
    authorize_selector: str
    revoke_selector: str
    print_selector: str
    success_selector: str


@dataclass(frozen=True)
class RobotStepConfig:
    action: str
    selector: str
    value: str


@dataclass(frozen=True)
class WebRobotConfig:
    enabled: bool
    start_url: str
    timeout_seconds: int
    browser: str
    headless: bool
    capture: CaptureConfig
    download: DownloadConfig
    login: LoginConfig
    public_query: PublicQueryConfig
    third_party_query: ThirdPartyQueryConfig
    third_party_authorization: ThirdPartyAuthorizationConfig
    steps: dict[str, RobotStepConfig]


@dataclass(frozen=True)
class AppConfig:
    project_name: str
    paths: ProjectPaths
    logging: LoggingConfig
    robots: dict[str, WebRobotConfig]


@dataclass(frozen=True)
class Cliente:
    rfc: str
    cliente: str
    modo_consulta: str
    requiere_pdf: str
    autorizado: str
    activo: str
    resultado: str | None
    fecha_consulta: date | datetime | str | None
    archivo: str | None
    observaciones: str | None
    prioridad: str | None
    row_number: int
    key_path: str | None = None
    cer_path: str | None = None
    efirma_password: str | None = None


@dataclass(frozen=True)
class RobotRunResult:
    resultado: str
    fecha_consulta: date | datetime | str | None
    archivo: str | None
    observaciones: str | None
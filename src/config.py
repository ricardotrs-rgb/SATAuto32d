from __future__ import annotations

from pathlib import Path
from typing import Any

from src.errors import ConfigError
from src.models import (
    AppConfig,
    CaptureConfig,
    DownloadConfig,
    LoggingConfig,
    LoginConfig,
    ProjectPaths,
    PublicQueryConfig,
    RobotStepConfig,
    ThirdPartyAuthorizationConfig,
    ThirdPartyQueryConfig,
    WebRobotConfig,
)
from src.paths import iter_paths

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(config_path: Path | None = None) -> AppConfig:
    resolved_path = config_path or CONFIG_FILE

    if not resolved_path.exists():
        raise ConfigError(f"No se encontro el archivo de configuracion: {resolved_path}")

    with resolved_path.open("r", encoding="utf-8") as config_file:
        data = _parse_yaml_like_config(config_file.read())

    return _parse_config(data)


def validate_config(config: AppConfig) -> None:
    if not config.project_name.strip():
        raise ConfigError("El nombre del proyecto no puede estar vacio")

    for path in iter_paths(config.paths):
        if not path.is_absolute():
            raise ConfigError(f"La ruta debe ser absoluta: {path}")

    if not config.logging.file_name.strip():
        raise ConfigError("El nombre del archivo de log no puede estar vacio")

    for robot_name, robot in config.robots.items():
        if not robot.start_url.startswith(("http://", "https://", "file://", "fixture:")):
            raise ConfigError(
                f"El robot {robot_name} debe definir una URL valida en start_url"
            )

        if robot.timeout_seconds <= 0:
            raise ConfigError(
                f"El robot {robot_name} debe definir timeout_seconds mayor a cero"
            )

        if robot.browser not in {"chromium", "firefox", "webkit"}:
            raise ConfigError(
                f"El robot {robot_name} debe usar browser chromium, firefox o webkit"
            )

        if robot.capture.enabled and not robot.capture.file_name.strip():
            raise ConfigError(
                f"El robot {robot_name} debe definir capture.file_name cuando capture.enabled=true"
            )

        if robot.download.enabled:
            if not robot.download.trigger_selector.strip():
                raise ConfigError(
                    f"El robot {robot_name} debe definir download.trigger_selector cuando download.enabled=true"
                )
            if not robot.download.target_subdir.strip():
                raise ConfigError(
                    f"El robot {robot_name} debe definir download.target_subdir cuando download.enabled=true"
                )

        if robot.login.enabled:
            required_fields = {
                "login.username": robot.login.username,
                "login.password": robot.login.password,
                "login.username_selector": robot.login.username_selector,
                "login.password_selector": robot.login.password_selector,
            }
            missing_fields = [
                field_name for field_name, field_value in required_fields.items() if not field_value.strip()
            ]
            if missing_fields:
                missing = ", ".join(missing_fields)
                raise ConfigError(
                    f"El robot {robot_name} requiere estos campos para login.enabled=true: {missing}"
                )

        if robot_name == "sat32d_publico":
            required_public_query_fields = {
                "public_query.rfc_input_selector": robot.public_query.rfc_input_selector,
                "public_query.submit_selector": robot.public_query.submit_selector,
                "public_query.result_selector": robot.public_query.result_selector,
                "public_query.unauthorized_selector": robot.public_query.unauthorized_selector,
            }
            missing_public_query_fields = [
                field_name
                for field_name, field_value in required_public_query_fields.items()
                if not field_value.strip()
            ]
            if missing_public_query_fields:
                missing = ", ".join(missing_public_query_fields)
                raise ConfigError(
                    f"El robot {robot_name} requiere estos selectores configurados: {missing}"
                )

        if robot_name == "sat32d_tercero":
            required_third_party_fields = {
                "third_party_query.ready_selector": robot.third_party_query.ready_selector,
                "third_party_query.rfc_mode_selector": robot.third_party_query.rfc_mode_selector,
                "third_party_query.rfc_input_selector": robot.third_party_query.rfc_input_selector,
                "third_party_query.submit_selector": robot.third_party_query.submit_selector,
                "third_party_query.download_selector": robot.third_party_query.download_selector,
                "third_party_query.unauthorized_selector": robot.third_party_query.unauthorized_selector,
            }
            missing_third_party_fields = [
                field_name
                for field_name, field_value in required_third_party_fields.items()
                if not field_value.strip()
            ]
            if missing_third_party_fields:
                missing = ", ".join(missing_third_party_fields)
                raise ConfigError(
                    f"El robot {robot_name} requiere estos selectores configurados: {missing}"
                )

        if robot_name == "sat32d_autoriza_tercero":
            required_authorization_fields = {
                "third_party_authorization.ready_selector": robot.third_party_authorization.ready_selector,
                "third_party_authorization.rfc_input_selector": robot.third_party_authorization.rfc_input_selector,
                "third_party_authorization.authorize_selector": robot.third_party_authorization.authorize_selector,
                "third_party_authorization.revoke_selector": robot.third_party_authorization.revoke_selector,
                "third_party_authorization.print_selector": robot.third_party_authorization.print_selector,
                "third_party_authorization.success_selector": robot.third_party_authorization.success_selector,
            }
            missing_authorization_fields = [
                field_name
                for field_name, field_value in required_authorization_fields.items()
                if not field_value.strip()
            ]
            if missing_authorization_fields:
                missing = ", ".join(missing_authorization_fields)
                raise ConfigError(
                    f"El robot {robot_name} requiere estos selectores configurados: {missing}"
                )

        for step_name, step in robot.steps.items():
            if step.action not in {"click", "fill", "wait_for", "press"}:
                raise ConfigError(
                    f"El robot {robot_name} define una accion invalida en steps.{step_name}: {step.action}"
                )
            if not step.selector.strip():
                raise ConfigError(
                    f"El robot {robot_name} debe definir selector en steps.{step_name}"
                )
            if step.action in {"fill", "press"} and not step.value.strip():
                raise ConfigError(
                    f"El robot {robot_name} debe definir value en steps.{step_name} para la accion {step.action}"
                )


def _parse_config(data: dict[str, Any]) -> AppConfig:
    paths_section = data.get("paths") or {}
    logging_section = data.get("logging") or {}
    robots_section = data.get("robots") or {}

    required_path_keys = ["control", "publico", "terceros", "errores", "logs"]
    missing_keys = [key for key in required_path_keys if key not in paths_section]
    if missing_keys:
        missing = ", ".join(missing_keys)
        raise ConfigError(f"Faltan rutas requeridas en config.yaml: {missing}")

    try:
        paths = ProjectPaths(
            control=Path(paths_section["control"]),
            publico=Path(paths_section["publico"]),
            terceros=Path(paths_section["terceros"]),
            errores=Path(paths_section["errores"]),
            logs=Path(paths_section["logs"]),
        )
        logging_config = LoggingConfig(
            level=str(logging_section.get("level", "INFO")),
            file_name=str(logging_section.get("file_name", "sat32d_robot.log")),
        )
        project_name = str(data.get("project_name", "sat32d_robot"))
        robots = _parse_robots(robots_section)
    except TypeError as error:
        raise ConfigError("La configuracion contiene valores invalidos") from error

    return AppConfig(
        project_name=project_name,
        paths=paths,
        logging=logging_config,
        robots=robots,
    )


def _parse_robots(data: Any) -> dict[str, WebRobotConfig]:
    if not isinstance(data, dict):
        raise ConfigError("La seccion robots debe ser un mapa")

    robots: dict[str, WebRobotConfig] = {}
    for robot_name, robot_data in data.items():
        if not isinstance(robot_data, dict):
            raise ConfigError(f"La configuracion del robot {robot_name} es invalida")

        capture_data = robot_data.get("capture", {})
        download_data = robot_data.get("download", {})
        login_data = robot_data.get("login", {})
        public_query_data = robot_data.get("public_query", {})
        third_party_query_data = robot_data.get("third_party_query", {})
        third_party_authorization_data = robot_data.get("third_party_authorization", {})
        steps_data = robot_data.get("steps", {})
        if not isinstance(capture_data, dict):
            raise ConfigError(f"La seccion capture del robot {robot_name} es invalida")
        if not isinstance(download_data, dict):
            raise ConfigError(f"La seccion download del robot {robot_name} es invalida")
        if not isinstance(login_data, dict):
            raise ConfigError(f"La seccion login del robot {robot_name} es invalida")
        if not isinstance(public_query_data, dict):
            raise ConfigError(
                f"La seccion public_query del robot {robot_name} es invalida"
            )
        if not isinstance(third_party_query_data, dict):
            raise ConfigError(
                f"La seccion third_party_query del robot {robot_name} es invalida"
            )
        if not isinstance(third_party_authorization_data, dict):
            raise ConfigError(
                f"La seccion third_party_authorization del robot {robot_name} es invalida"
            )
        if not isinstance(steps_data, dict):
            raise ConfigError(f"La seccion steps del robot {robot_name} es invalida")

        robots[str(robot_name)] = WebRobotConfig(
            enabled=_parse_bool(robot_data.get("enabled", False)),
            start_url=str(robot_data.get("start_url", "")).strip(),
            timeout_seconds=_parse_int(robot_data.get("timeout_seconds", 30)),
            browser=str(robot_data.get("browser", "chromium")).strip(),
            headless=_parse_bool(robot_data.get("headless", True)),
            capture=CaptureConfig(
                enabled=_parse_bool(capture_data.get("enabled", False)),
                file_name=str(capture_data.get("file_name", "capture.png")).strip(),
                full_page=_parse_bool(capture_data.get("full_page", True)),
            ),
            download=DownloadConfig(
                enabled=_parse_bool(download_data.get("enabled", False)),
                trigger_selector=str(download_data.get("trigger_selector", "")).strip(),
                target_subdir=str(download_data.get("target_subdir", str(robot_name))).strip(),
            ),
            login=LoginConfig(
                enabled=_parse_bool(login_data.get("enabled", False)),
                username=str(login_data.get("username", "")).strip(),
                password=str(login_data.get("password", "")).strip(),
                username_selector=str(login_data.get("username_selector", "")).strip(),
                password_selector=str(login_data.get("password_selector", "")).strip(),
                submit_selector=str(login_data.get("submit_selector", "")).strip(),
            ),
            public_query=PublicQueryConfig(
                rfc_input_selector=str(
                    public_query_data.get("rfc_input_selector", "")
                ).strip(),
                submit_selector=str(
                    public_query_data.get("submit_selector", "")
                ).strip(),
                result_selector=str(public_query_data.get("result_selector", "")).strip(),
                unauthorized_selector=str(
                    public_query_data.get("unauthorized_selector", "")
                ).strip(),
            ),
            third_party_query=ThirdPartyQueryConfig(
                ready_selector=str(
                    third_party_query_data.get("ready_selector", "")
                ).strip(),
                rfc_mode_selector=str(
                    third_party_query_data.get("rfc_mode_selector", "")
                ).strip(),
                rfc_input_selector=str(
                    third_party_query_data.get("rfc_input_selector", "")
                ).strip(),
                submit_selector=str(
                    third_party_query_data.get("submit_selector", "")
                ).strip(),
                download_selector=str(
                    third_party_query_data.get("download_selector", "")
                ).strip(),
                unauthorized_selector=str(
                    third_party_query_data.get("unauthorized_selector", "")
                ).strip(),
            ),
            third_party_authorization=ThirdPartyAuthorizationConfig(
                ready_selector=str(
                    third_party_authorization_data.get("ready_selector", "")
                ).strip(),
                rfc_input_selector=str(
                    third_party_authorization_data.get("rfc_input_selector", "")
                ).strip(),
                authorize_selector=str(
                    third_party_authorization_data.get("authorize_selector", "")
                ).strip(),
                revoke_selector=str(
                    third_party_authorization_data.get("revoke_selector", "")
                ).strip(),
                print_selector=str(
                    third_party_authorization_data.get("print_selector", "")
                ).strip(),
                success_selector=str(
                    third_party_authorization_data.get("success_selector", "")
                ).strip(),
            ),
            steps=_parse_steps(steps_data, str(robot_name)),
        )

    return robots


def _parse_steps(data: dict[str, Any], robot_name: str) -> dict[str, RobotStepConfig]:
    steps: dict[str, RobotStepConfig] = {}
    for step_name, step_data in data.items():
        if not isinstance(step_data, dict):
            raise ConfigError(
                f"La configuracion del paso {step_name} en el robot {robot_name} es invalida"
            )

        steps[str(step_name)] = RobotStepConfig(
            action=str(step_data.get("action", "")).strip(),
            selector=str(step_data.get("selector", "")).strip(),
            value=str(step_data.get("value", "")).strip(),
        )

    return steps


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    raise ConfigError(f"Valor booleano invalido: {value}")


def _parse_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ConfigError(f"Valor entero invalido: {value}")


def _parse_yaml_like_config(content: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    section_stack: list[tuple[int, dict[str, Any]]] = [(-1, parsed)]

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip(" "))
        key, value = _split_key_value(stripped)
        while len(section_stack) > 1 and indent <= section_stack[-1][0]:
            section_stack.pop()

        if indent > section_stack[-1][0] and indent not in {0, 2, 4, 6, 8}:
            raise ConfigError("Indentacion invalida en config.yaml")

        current_section = section_stack[-1][1]
        if value == "":
            section: dict[str, Any] = {}
            current_section[key] = section
            section_stack.append((indent, section))
            continue

        current_section[key] = value

    return parsed


def _split_key_value(line: str) -> tuple[str, str]:
    if ":" not in line:
        raise ConfigError(f"Linea invalida en config.yaml: {line}")

    key, value = line.split(":", 1)
    parsed_key = key.strip()
    parsed_value = value.strip()

    if not parsed_key:
        raise ConfigError(f"Clave invalida en config.yaml: {line}")

    if len(parsed_value) >= 2 and parsed_value[0] == parsed_value[-1] and parsed_value[0] in {'"', "'"}:
        parsed_value = parsed_value[1:-1]

    return parsed_key, parsed_value
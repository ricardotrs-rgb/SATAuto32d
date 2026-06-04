from __future__ import annotations

import argparse
import sys

from src.config import load_config, validate_config
from src.errors import SAT32DError
from src.logger import setup_logger
from src.paths import ensure_directories, find_missing_directories
from src.robots import get_robot
from src.webapp import create_web_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Herramientas base para el proyecto sat32d_robot."
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "init",
        help="Crea carpetas requeridas y valida la configuracion del proyecto.",
    )

    subparsers.add_parser(
        "check",
        help="Valida configuracion y rutas sin crear carpetas.",
    )

    subparsers.add_parser(
        "status",
        help="Muestra el estado del entorno y de los robots configurados.",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="Ejecuta un robot configurado.",
    )
    run_parser.add_argument("robot_name", help="Nombre del robot a ejecutar.")
    run_parser.add_argument(
        "--manual-timeout-seconds",
        type=int,
        default=600,
        help="Tiempo maximo de espera para que el usuario deje lista una pantalla manual del SAT.",
    )
    run_parser.add_argument(
        "--include-captcha-pendiente",
        action="store_true",
        help="Permite reprocesar filas con Resultado=CAPTCHA_PENDIENTE cuando el robot lo soporte.",
    )

    web_parser = subparsers.add_parser(
        "web",
        help="Inicia la interfaz web local del proyecto.",
    )
    web_parser.add_argument("--host", default="127.0.0.1", help="Host de escucha.")
    web_parser.add_argument("--port", type=int, default=8000, help="Puerto HTTP.")

    return parser


def run_init() -> int:
    config = load_config()
    validate_config(config)
    created_paths = ensure_directories(config.paths)
    logger = setup_logger(config)

    logger.info("Inicializacion completada")
    print("Configuracion valida.")
    if created_paths:
        print("Carpetas creadas:")
        for path in created_paths:
            print(f"- {path}")
    else:
        print("No fue necesario crear carpetas. Todo ya existe.")

    return 0


def run_check() -> int:
    config = load_config()
    validate_config(config)

    missing_paths = find_missing_directories(config.paths)
    print("Configuracion valida.")
    if missing_paths:
        print("Faltan carpetas requeridas:")
        for path in missing_paths:
            print(f"- {path}")
        return 1

    print("Todas las carpetas requeridas existen.")
    return 0


def run_status() -> int:
    config = load_config()
    validate_config(config)

    missing_paths = set(find_missing_directories(config.paths))
    print("Estado del entorno:")
    for path in [
        config.paths.control,
        config.paths.publico,
        config.paths.terceros,
        config.paths.errores,
        config.paths.logs,
    ]:
        state = "faltante" if path in missing_paths else "ok"
        print(f"- {path}: {state}")

    print("Robots configurados:")
    for robot_name, robot in sorted(config.robots.items()):
        status = "habilitado" if robot.enabled else "deshabilitado"
        readiness_issues = _get_robot_preflight_issues(robot_name, robot)
        readiness = "listo" if not readiness_issues else "bloqueado"
        print(
            f"- {robot_name}: {status} | ready={readiness} | browser={robot.browser} | headless={robot.headless} | capture={robot.capture.enabled} | download={robot.download.enabled} | login={robot.login.enabled} | url={robot.start_url}"
        )
        for issue in readiness_issues:
            print(f"  falta: {issue}")

    return 0


def run_robot(
    robot_name: str,
    *,
    manual_timeout_seconds: int = 600,
    include_captcha_pendiente: bool = False,
) -> int:
    config = load_config()
    validate_config(config)
    missing_paths = find_missing_directories(config.paths)
    if missing_paths:
        print("Error: ejecuta primero 'python main.py init' para preparar el entorno.", file=sys.stderr)
        return 2

    logger = setup_logger(config)
    robot = get_robot(robot_name, config, logger)
    robot.validate()

    if robot_name in {"sat32d_publico", "sat32d_tercero"}:
        raise SAT32DError(
            f"{robot_name} ahora se ejecuta desde la interfaz web con clientes del almacenamiento local. Usa 'python main.py web'."
        )

    _configure_robot_run(
        robot,
        robot_name,
        manual_timeout_seconds,
        include_captcha_pendiente,
    )

    readiness_issues = _get_robot_preflight_issues(robot_name, robot.robot_config)
    if readiness_issues:
        print(f"Error: {robot_name} no esta listo para ejecutarse.", file=sys.stderr)
        for issue in readiness_issues:
            print(f"- {issue}", file=sys.stderr)
        return 2

    robot.run()
    return 0


def run_web(host: str, port: int) -> int:
    config = load_config()
    validate_config(config)
    missing_paths = find_missing_directories(config.paths)
    if missing_paths:
        print("Error: ejecuta primero 'python main.py init' para preparar el entorno.", file=sys.stderr)
        return 2

    logger = setup_logger(config)
    app = create_web_app(config, logger)
    print(f"Interfaz web disponible en http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
    return 0


def _get_robot_preflight_issues(robot_name: str, robot_config) -> list[str]:
    issues: list[str] = []

    if robot_name == "sat32d_tercero":
        if _is_placeholder_value(robot_config.start_url):
            issues.append("sat32d_tercero.start_url")

        third_party_query = getattr(robot_config, "third_party_query", None)
        if third_party_query is None:
            issues.append("sat32d_tercero.third_party_query")
            return issues

        required_fields = {
            "sat32d_tercero.third_party_query.ready_selector": third_party_query.ready_selector,
            "sat32d_tercero.third_party_query.rfc_mode_selector": third_party_query.rfc_mode_selector,
            "sat32d_tercero.third_party_query.rfc_input_selector": third_party_query.rfc_input_selector,
            "sat32d_tercero.third_party_query.submit_selector": third_party_query.submit_selector,
            "sat32d_tercero.third_party_query.download_selector": third_party_query.download_selector,
            "sat32d_tercero.third_party_query.unauthorized_selector": third_party_query.unauthorized_selector,
        }

        for field_name, value in required_fields.items():
            if _is_placeholder_value(value):
                issues.append(field_name)

        return issues

    if robot_name == "sat32d_autoriza_tercero":
        if _is_placeholder_value(robot_config.start_url):
            issues.append("sat32d_autoriza_tercero.start_url")

        third_party_authorization = getattr(robot_config, "third_party_authorization", None)
        if third_party_authorization is None:
            issues.append("sat32d_autoriza_tercero.third_party_authorization")
            return issues

        required_fields = {
            "sat32d_autoriza_tercero.third_party_authorization.ready_selector": third_party_authorization.ready_selector,
            "sat32d_autoriza_tercero.third_party_authorization.rfc_input_selector": third_party_authorization.rfc_input_selector,
            "sat32d_autoriza_tercero.third_party_authorization.authorize_selector": third_party_authorization.authorize_selector,
            "sat32d_autoriza_tercero.third_party_authorization.revoke_selector": third_party_authorization.revoke_selector,
            "sat32d_autoriza_tercero.third_party_authorization.print_selector": third_party_authorization.print_selector,
            "sat32d_autoriza_tercero.third_party_authorization.success_selector": third_party_authorization.success_selector,
        }

        for field_name, value in required_fields.items():
            if _is_placeholder_value(value):
                issues.append(field_name)

        return issues

    return issues


def _is_placeholder_value(value: str) -> bool:
    normalized = value.strip().upper()
    return not normalized or normalized.startswith("REEMPLAZAR_")


def _configure_robot_run(
    robot,
    robot_name: str,
    manual_timeout_seconds: int,
    include_captcha_pendiente: bool,
) -> None:
    if manual_timeout_seconds <= 0:
        raise SAT32DError("--manual-timeout-seconds debe ser mayor a cero")

    if hasattr(robot, "configure_run"):
        robot.configure_run(
            target_row=None,
            manual_timeout_seconds=manual_timeout_seconds,
            include_captcha_pendiente=include_captcha_pendiente,
        )
        return
    if include_captcha_pendiente:
        raise SAT32DError(
            f"El robot {robot_name} no soporta el parametro --include-captcha-pendiente"
        )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "init":
            return run_init()
        if args.command == "check":
            return run_check()
        if args.command == "status":
            return run_status()
        if args.command == "run":
            return run_robot(
                args.robot_name,
                manual_timeout_seconds=args.manual_timeout_seconds,
                include_captcha_pendiente=args.include_captcha_pendiente,
            )
        if args.command == "web":
            return run_web(args.host, args.port)

        parser.print_help()
        return 1
    except SAT32DError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
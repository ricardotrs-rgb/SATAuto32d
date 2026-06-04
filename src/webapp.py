from __future__ import annotations

import os
import secrets
import threading
import unicodedata
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from openpyxl import Workbook
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from src.local_client_store import LocalClientStore
from src.models import EXPLICIT_OPINION_RESULT_STATUSES, PENDING_RESULT_STATUSES, RobotRunResult
from src.robots import get_robot
from src.robots.sat32d_opinion import SAT32DOpinionRobot


LOCAL_CLIENT_ERROR_LABELS = {
    SAT32DOpinionRobot.EFIRMA_REVOKED_MESSAGE: "E.FIRMA revocada",
    SAT32DOpinionRobot.EFIRMA_EXPIRED_MESSAGE: "E.FIRMA no vigente",
    SAT32DOpinionRobot.EFIRMA_INVALID_CREDENTIALS_MESSAGE: "Credenciales inválidas",
}


def _has_reusable_third_party_session(*, config, logger, credentials: dict[str, str] | None) -> bool:
    try:
        tercero_robot = get_robot("sat32d_tercero", config, logger)
        tercero_robot.validate()
        if credentials:
            tercero_robot.set_runtime_credentials(
                rfc=credentials["rfc"],
                password=credentials["password"],
            )

        probe_session = getattr(tercero_robot, "has_active_session", None)
        if not callable(probe_session):
            return False

        return bool(probe_session(timeout_seconds=5))
    except Exception:
        logger.info(
            "No se detecto una sesion SAT reutilizable para consulta local masiva.",
            exc_info=True,
        )
        return False


def create_web_app(config, logger) -> Flask:
    base_dir = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        template_folder=str(base_dir / "templates"),
        static_folder=str(base_dir / "static"),
    )
    local_client_store = LocalClientStore(base_dir / "web_storage" / "local_clients")
    app.secret_key = os.environ.get("SAT32D_WEB_SECRET", secrets.token_hex(32))
    allowed_password = os.environ.get("SAT32D_WEB_ALLOWED_PASSWORD", "Javi1984")
    allowed_rfcs_raw = os.environ.get("SAT32D_WEB_ALLOWED_RFCS", "IOEF840128UC4")
    enforce_secure_defaults = os.environ.get("SAT32D_WEB_ENFORCE_SECURE_DEFAULTS", "0") == "1"
    if enforce_secure_defaults:
        if not os.environ.get("SAT32D_WEB_SECRET"):
            raise RuntimeError(
                "SAT32D_WEB_SECRET es obligatorio en produccion (SAT32D_WEB_ENFORCE_SECURE_DEFAULTS=1)."
            )
        if allowed_password == "Javi1984":
            raise RuntimeError(
                "SAT32D_WEB_ALLOWED_PASSWORD no puede usar el valor por defecto en produccion."
            )

    cookie_secure = os.environ.get("SAT32D_WEB_COOKIE_SECURE", "0") == "1"
    if cookie_secure:
        app.config["PREFERRED_URL_SCHEME"] = "https"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SECURE"] = cookie_secure
    app.config["SESSION_COOKIE_SAMESITE"] = os.environ.get(
        "SAT32D_WEB_COOKIE_SAMESITE",
        "Lax",
    )

    proxy_fix_enabled = os.environ.get("SAT32D_WEB_PROXY_FIX", "0") == "1"
    if proxy_fix_enabled:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[assignment]

    allowed_hosts_raw = os.environ.get("SAT32D_WEB_ALLOWED_HOSTS", "")
    allowed_hosts = {
        value.strip().lower()
        for value in allowed_hosts_raw.split(",")
        if value.strip()
    }
    app.config["SAT32D_WEB_ALLOWED_RFCS"] = {
        value.strip().upper()
        for value in allowed_rfcs_raw.split(",")
        if value.strip()
    }
    app.config["SAT32D_WEB_ALLOWED_PASSWORD"] = allowed_password
    app.config["SAT32D_WEB_CREDENTIALS"] = {}
    app.config["SAT32D_WEB_REGISTERED_USERS"] = local_client_store.load_registered_users()
    app.config["SAT32D_WEB_JOBS"] = []
    app.config["SAT32D_WEB_LOCK"] = threading.Lock()
    app.config["SAT32D_WEB_ALLOWED_HOSTS"] = allowed_hosts

    def _is_allowed_login_rfc(rfc: str) -> bool:
        return (rfc or "").strip().upper() in app.config["SAT32D_WEB_ALLOWED_RFCS"]

    def _is_allowed_password(password: str) -> bool:
        return (password or "") == app.config["SAT32D_WEB_ALLOWED_PASSWORD"]

    def _ensure_session_id() -> str:
        if "web_session_id" not in session:
            session["web_session_id"] = secrets.token_hex(16)
        return session["web_session_id"]

    def _store_credentials(rfc: str, password: str) -> None:
        session_id = _ensure_session_id()
        with app.config["SAT32D_WEB_LOCK"]:
            app.config["SAT32D_WEB_CREDENTIALS"][session_id] = {
                "rfc": rfc,
                "password": password,
            }

    def _get_credentials() -> dict[str, str] | None:
        session_id = session.get("web_session_id")
        if not session_id:
            return None
        with app.config["SAT32D_WEB_LOCK"]:
            creds = app.config["SAT32D_WEB_CREDENTIALS"].get(session_id)
            return creds.copy() if creds else None

    def _is_current_session_valid() -> bool:
        user_rfc = (session.get("user_rfc") or "").strip().upper()
        if not user_rfc or not _is_allowed_login_rfc(user_rfc):
            return False

        credentials = _get_credentials()
        if not credentials:
            return False

        credential_rfc = (credentials.get("rfc") or "").strip().upper()
        credential_password = credentials.get("password") or ""
        if credential_rfc != user_rfc:
            return False
        if not _is_allowed_password(credential_password):
            return False

        return True

    @app.before_request
    def _enforce_web_authentication():
        if app.config["SAT32D_WEB_ALLOWED_HOSTS"]:
            requested_host = (request.host or "").split(":", 1)[0].strip().lower()
            if requested_host not in app.config["SAT32D_WEB_ALLOWED_HOSTS"]:
                return "Host no permitido", 400

        public_endpoints = {
            "home",
            "register_form",
            "register_submit",
            "login",
            "logout",
            "healthz",
            "static",
        }
        endpoint = request.endpoint or ""
        if endpoint in public_endpoints:
            return None

        if _is_current_session_valid():
            return None

        session.clear()
        _ensure_session_id()
        flash("Debes iniciar sesion con el RFC y la contraseña autorizados.", "error")
        return redirect(url_for("home"))

    def _get_registered_user_record(rfc: str) -> dict | None:
        with app.config["SAT32D_WEB_LOCK"]:
            record = app.config["SAT32D_WEB_REGISTERED_USERS"].get(rfc)

        if record is None:
            return None

        if isinstance(record, str):
            return {
                "password_hash": record,
                "profile": {},
                "monitored_people": [],
            }

        return {
            "password_hash": record.get("password_hash", ""),
            "profile": record.get("profile", {}).copy(),
            "monitored_people": [
                person.copy() for person in record.get("monitored_people", [])
            ],
        }

    def _store_registered_user(
        rfc: str,
        password: str,
        *,
        profile: dict[str, str],
        monitored_people: list[dict[str, str]],
    ) -> bool:
        normalized_rfc = (rfc or "").strip().upper()
        password_hash = generate_password_hash(password)
        with app.config["SAT32D_WEB_LOCK"]:
            users = app.config["SAT32D_WEB_REGISTERED_USERS"]
            is_new_user = normalized_rfc not in users
            users[normalized_rfc] = {
                "password_hash": password_hash,
                "profile": profile.copy(),
                "monitored_people": [person.copy() for person in monitored_people],
            }
            local_client_store.save_registered_user(
                normalized_rfc,
                password_hash=password_hash,
                profile=profile,
                monitored_people=monitored_people,
            )
            return is_new_user

    def _validate_registered_user(rfc: str, password: str) -> bool:
        record = _get_registered_user_record(rfc)

        if not record:
            return False

        return check_password_hash(record["password_hash"], password)

    def _build_register_form_data(form=None) -> dict:
        source = form or {}
        monitored_people = []
        for index in range(1, 4):
            monitored_people.append(
                {
                    "rfc": (source.get(f"monitor_rfc_{index}") or "").strip().upper(),
                    "nombre": (source.get(f"monitor_nombre_{index}") or "").strip(),
                    "modo_consulta": (source.get(f"monitor_modo_{index}") or "TERCERO").strip().upper(),
                    "requiere_pdf": (source.get(f"monitor_pdf_{index}") or "SI").strip().upper(),
                }
            )

        return {
            "rfc": (source.get("rfc") or "").strip().upper(),
            "email": (source.get("email") or "").strip(),
            "monitored_people": monitored_people,
        }

    def _build_local_client_form_data(form=None) -> dict:
        source = form or {}
        return {
            "cliente": (source.get("cliente") or "").strip(),
            "rfc": (source.get("rfc") or "").strip().upper(),
            "modo_consulta": (source.get("modo_consulta") or "TERCERO").strip().upper(),
            "requiere_pdf": (source.get("requiere_pdf") or "SI").strip().upper(),
            "activo": (source.get("activo") or "SI").strip().upper(),
            "efirma_password": source.get("efirma_password") or "",
            "observaciones": (source.get("observaciones") or "").strip(),
        }

    def _clear_credentials() -> None:
        session_id = session.get("web_session_id")
        if not session_id:
            return
        with app.config["SAT32D_WEB_LOCK"]:
            app.config["SAT32D_WEB_CREDENTIALS"].pop(session_id, None)

    def _sync_registered_monitored_clients(owner_rfc: str) -> None:
        registered_user = _get_registered_user_record(owner_rfc)
        if not registered_user:
            return

        local_client_store.sync_registered_monitored_clients(
            owner_rfc=owner_rfc,
            monitored_people=registered_user.get("monitored_people", []),
        )

    def _sync_local_client_rfcs(owner_rfc: str) -> None:
        local_client_store.sync_client_rfcs_from_certificates(owner_rfc=owner_rfc)

    def _register_job(job: dict) -> None:
        with app.config["SAT32D_WEB_LOCK"]:
            jobs = app.config["SAT32D_WEB_JOBS"]
            jobs.insert(0, job)
            del jobs[10:]

    def _update_job(job_id: str, **changes) -> None:
        with app.config["SAT32D_WEB_LOCK"]:
            for job in app.config["SAT32D_WEB_JOBS"]:
                if job["id"] == job_id:
                    job.update(changes)
                    return

    def _get_jobs() -> list[dict]:
        with app.config["SAT32D_WEB_LOCK"]:
            return [job.copy() for job in app.config["SAT32D_WEB_JOBS"]]

    def _attach_local_records_to_robot(robot, records: list[dict]) -> None:
        selected_clientes = [
            local_client_store.to_cliente(record, row_number=index)
            for index, record in enumerate(records, start=1)
        ]
        robot.set_clientes_override(selected_clientes)

        if not records:
            return

        client_id_by_row = {
            index: record["id"]
            for index, record in enumerate(records, start=1)
        }
        robot.set_result_callback(
            lambda cliente, result: local_client_store.update_client_result(
                client_id_by_row[cliente.row_number],
                resultado=result.resultado,
                fecha_consulta=result.fecha_consulta,
                archivo=result.archivo,
                observaciones=result.observaciones,
            )
        )

    def _run_local_storage_consultation(
        *,
        owner_rfc: str,
        include_captcha_pendiente: bool,
        prefer_third_party: bool,
        credentials: dict[str, str] | None,
    ) -> RobotRunResult:
        local_records = local_client_store.list_clients(owner_rfc=owner_rfc)
        grouped_records = _select_local_records_for_local_consulta(
            local_records,
            include_captcha_pendiente=include_captcha_pendiente,
            prefer_third_party=prefer_third_party,
        )
        efirma_records = _select_local_records_for_local_efirma_consulta(
            local_records,
            include_captcha_pendiente=include_captcha_pendiente,
            ignore_status=True,
        )
        efirma_record_keys = {id(record) for record in efirma_records}
        public_records = [
            record
            for record in grouped_records["sat32d_publico"]
            if id(record) not in efirma_record_keys
        ]
        tercero_records = grouped_records["sat32d_tercero"]
        waiting_for_sat_records = (
            _select_local_records_waiting_for_sat_session(
                local_records,
                include_captcha_pendiente=include_captcha_pendiente,
            )
            if not prefer_third_party
            else []
        )
        deferred_tercero_records = (
            [
                record
                for record in waiting_for_sat_records
                if id(record) not in efirma_record_keys
            ]
            if not prefer_third_party
            else []
        )

        deferred_message = ""
        if deferred_tercero_records:
            deferred_message = (
                "Consulta TERCERO pendiente para "
                f"{len(deferred_tercero_records)} clientes: se requiere una sesion SAT activa del RFC dueno o una e.firma valida registrada."
            )
            _mark_local_records_pending_due_to_missing_sat_session(
                deferred_tercero_records,
                message=deferred_message,
            )

        if not public_records and not tercero_records and not efirma_records and not deferred_tercero_records:
            message = "No hay clientes locales elegibles para consultar opiniones."
            logger.info(message)
            return RobotRunResult(
                resultado="SIN_CLIENTES",
                fecha_consulta=datetime.now(),
                archivo=None,
                observaciones=message,
            )

        if not public_records and not tercero_records and not efirma_records and deferred_tercero_records:
            logger.info(deferred_message)
            return RobotRunResult(
                resultado="PENDIENTE_SESION_SAT",
                fecha_consulta=datetime.now(),
                archivo=None,
                observaciones=deferred_message,
            )

        summaries: list[str] = []
        last_file: str | None = None
        public_record_ids = [record.get("id") for record in public_records if record.get("id")]

        if public_records:
            public_robot = get_robot("sat32d_publico", config, logger)
            public_robot.validate()
            _attach_local_records_to_robot(public_robot, public_records)
            public_result = public_robot.run()
            summaries.append(public_result.observaciones or public_result.resultado)
            last_file = public_result.archivo or last_file

            # If PUBLICO returned NO_AUTORIZADO/ERROR, retry through TERCERO using owner credentials
            # so manually authorized clients can still get the PDF in the same local run.
            if credentials and not prefer_third_party and public_record_ids:
                refreshed_records = local_client_store.list_clients(owner_rfc=owner_rfc)
                retry_records = [
                    record
                    for record in refreshed_records
                    if record.get("id") in public_record_ids
                    and (record.get("resultado") or "").strip().upper() in {"NO_AUTORIZADO", "ERROR"}
                    and (record.get("activo") or "").strip().upper() == "SI"
                    and (record.get("requiere_pdf") or "").strip().upper() == "SI"
                ]
                if retry_records:
                    tercero_retry_robot = get_robot("sat32d_tercero", config, logger)
                    tercero_retry_robot.validate()
                    tercero_retry_robot.set_runtime_credentials(
                        rfc=credentials["rfc"],
                        password=credentials["password"],
                    )
                    _attach_local_records_to_robot(tercero_retry_robot, retry_records)
                    if hasattr(tercero_retry_robot, "configure_run"):
                        tercero_retry_robot.configure_run(
                            target_row=None,
                            manual_timeout_seconds=900,
                            include_captcha_pendiente=include_captcha_pendiente,
                            tercero_rfc=None,
                        )
                    tercero_retry_result = tercero_retry_robot.run()
                    summaries.append(
                        "Reintento TERCERO para clientes PUBLICO no autorizados: "
                        + (tercero_retry_result.observaciones or tercero_retry_result.resultado)
                    )
                    last_file = tercero_retry_result.archivo or last_file

        if efirma_records:
            opinion_robot = get_robot("sat32d_opinion_cumplimiento", config, logger)
            opinion_robot.validate()
            _attach_local_records_to_robot(opinion_robot, efirma_records)
            opinion_result = opinion_robot.run()
            summaries.append(opinion_result.observaciones or opinion_result.resultado)
            last_file = opinion_result.archivo or last_file

        if tercero_records:
            tercero_robot = get_robot("sat32d_tercero", config, logger)
            tercero_robot.validate()
            if credentials:
                tercero_robot.set_runtime_credentials(
                    rfc=credentials["rfc"],
                    password=credentials["password"],
                )
            _attach_local_records_to_robot(tercero_robot, tercero_records)
            if hasattr(tercero_robot, "configure_run"):
                tercero_robot.configure_run(
                    target_row=None,
                    manual_timeout_seconds=900,
                    include_captcha_pendiente=include_captcha_pendiente,
                    tercero_rfc=None,
                )
            tercero_result = tercero_robot.run()
            summaries.append(tercero_result.observaciones or tercero_result.resultado)
            last_file = tercero_result.archivo or last_file

        if deferred_message:
            summaries.append(deferred_message)

        return RobotRunResult(
            resultado="FINALIZADO_LOCAL",
            fecha_consulta=datetime.now(),
            archivo=last_file,
            observaciones=" | ".join(summary for summary in summaries if summary),
        )

    def _run_robot_job(
        job_id: str,
        *,
        owner_rfc: str,
        robot_name: str,
        target_client_id: str | None,
        include_captcha_pendiente: bool,
        prefer_third_party: bool,
        tercero_rfc: str | None,
        credentials: dict[str, str] | None,
    ) -> None:
        _update_job(job_id, status="running", started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        try:
            if robot_name == "sat32d_locales":
                result = _run_local_storage_consultation(
                    owner_rfc=owner_rfc,
                    include_captcha_pendiente=include_captcha_pendiente,
                    prefer_third_party=prefer_third_party,
                    credentials=credentials,
                )
            else:
                robot = get_robot(robot_name, config, logger)
                robot.validate()
                if credentials:
                    robot.set_runtime_credentials(
                        rfc=credentials["rfc"],
                        password=credentials["password"],
                    )

                selected_records: list[dict] = []
                if robot_name in {"sat32d_publico", "sat32d_tercero"}:
                    selected_records = _select_local_records_for_robot(
                        local_client_store.list_clients(owner_rfc=owner_rfc),
                        robot_name=robot_name,
                        target_client_id=target_client_id,
                        include_captcha_pendiente=include_captcha_pendiente,
                    )
                    _attach_local_records_to_robot(robot, selected_records)

                if hasattr(robot, "configure_run"):
                    robot.configure_run(
                        target_row=None,
                        manual_timeout_seconds=900,
                        include_captcha_pendiente=include_captcha_pendiente,
                        tercero_rfc=tercero_rfc,
                    )
                result = robot.run()

                if (
                    robot_name == "sat32d_publico"
                    and target_client_id
                    and selected_records
                ):
                    refreshed_target_record = (
                        local_client_store.get_client(target_client_id, owner_rfc=owner_rfc)
                        or selected_records[0]
                    )
                    refreshed_result = (refreshed_target_record.get("resultado") or "").strip().upper()
                    refreshed_observations = str(
                        refreshed_target_record.get("resultado_observaciones") or ""
                    ).upper()
                    should_retry_with_efirma = (
                        refreshed_result in {"NO_AUTORIZADO", "ERROR"}
                        or (
                            refreshed_result == "PENDIENTE"
                            and (
                                "NO AUTORIZADO" in refreshed_observations
                                or "REVALIDAR LA AUTORIZACION MANUAL" in refreshed_observations
                            )
                        )
                    )
                    if (
                        should_retry_with_efirma
                        and _record_has_local_efirma_credentials(refreshed_target_record)
                    ):
                        opinion_robot = get_robot("sat32d_opinion_cumplimiento", config, logger)
                        opinion_robot.validate()
                        _attach_local_records_to_robot(opinion_robot, [refreshed_target_record])
                        opinion_result = opinion_robot.run()
                        result = RobotRunResult(
                            resultado=opinion_result.resultado or result.resultado,
                            fecha_consulta=opinion_result.fecha_consulta or result.fecha_consulta,
                            archivo=opinion_result.archivo or result.archivo,
                            observaciones=(
                                opinion_result.observaciones
                                or result.observaciones
                                or result.resultado
                            ),
                        )
            _update_job(
                job_id,
                status="completed",
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                summary=result.observaciones or result.resultado,
            )
        except Exception as error:
            logger.exception("Fallo una ejecucion web del robot %s", robot_name)
            _update_job(
                job_id,
                status="error",
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                summary=str(error),
            )

    @app.get("/")
    def home():
        _ensure_session_id()
        user_rfc = (session.get("user_rfc") or "").strip().upper()
        if user_rfc and _is_current_session_valid():
            return redirect(url_for("clientes"))
        if user_rfc:
            session.clear()
            _ensure_session_id()
            if _is_allowed_login_rfc(user_rfc):
                flash("Tu usuario ya no esta registrado. Debes registrarte antes de entrar.", "error")
            else:
                flash("Ese RFC no esta autorizado para usar esta aplicacion.", "error")
        return render_template("login.html")

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "service": "sat32d-web"}), 200

    @app.get("/register")
    def register_form():
        _ensure_session_id()
        return render_template(
            "register.html",
            form_data=_build_register_form_data(),
        )

    @app.post("/login")
    def login():
        rfc = (request.form.get("rfc") or "").strip().upper()
        password = request.form.get("password") or ""

        session.clear()
        _ensure_session_id()

        if not rfc or not password:
            flash("Debes capturar RFC y contraseña para continuar.", "error")
            return redirect(url_for("home"))

        if not _is_allowed_login_rfc(rfc):
            flash("Ese RFC no esta autorizado para usar esta aplicacion.", "error")
            return redirect(url_for("home"))

        if not _is_allowed_password(password):
            flash("La contraseña no coincide con la autorizada para esta aplicacion.", "error")
            return redirect(url_for("home"))

        existing_user = _get_registered_user_record(rfc)
        if existing_user and not _validate_registered_user(rfc, password):
            flash("La contraseña no coincide con el RFC registrado en esta sesión local.", "error")
            return redirect(url_for("home"))

        session.clear()
        _ensure_session_id()
        session["user_rfc"] = rfc
        _store_credentials(rfc, password)
        flash("Sesion iniciada. La contraseña no se almacena; solo se conserva en memoria mientras la sesion web siga abierta.", "success")
        logger.info("Acceso web local iniciado para RFC %s", rfc)
        return redirect(url_for("clientes"))

    @app.post("/register")
    def register_submit():
        form_data = _build_register_form_data(request.form)
        password = request.form.get("password") or ""

        if not _is_allowed_login_rfc(form_data["rfc"]):
            flash("Solo el RFC autorizado puede registrarse en esta aplicacion.", "error")
            return render_template("register.html", form_data=form_data)

        if not _is_allowed_password(password):
            flash("Solo la contraseña autorizada puede registrarse en esta aplicacion.", "error")
            return render_template("register.html", form_data=form_data)

        if not form_data["rfc"] or not form_data["email"] or not password:
            flash("Debes capturar RFC, correo electrónico y contraseña para completar el registro local.", "error")
            return render_template("register.html", form_data=form_data)

        invalid_rows = [
            index
            for index, person in enumerate(form_data["monitored_people"], start=1)
            if (person["rfc"] and not person["nombre"])
            or (person["nombre"] and not person["rfc"])
        ]
        if invalid_rows:
            rows_text = ", ".join(str(index) for index in invalid_rows)
            flash(
                f"Completa RFC y nombre en cada persona a monitorear capturada. Revisa las filas: {rows_text}.",
                "error",
            )
            return render_template("register.html", form_data=form_data)

        monitored_people = [
            person
            for person in form_data["monitored_people"]
            if person["rfc"] and person["nombre"]
        ]

        is_new_user = _store_registered_user(
            form_data["rfc"],
            password,
            profile={
                "email": form_data["email"],
            },
            monitored_people=monitored_people,
        )

        session.clear()
        _ensure_session_id()
        session["user_rfc"] = form_data["rfc"]
        _store_credentials(form_data["rfc"], password)

        monitored_count = len(monitored_people)
        monitored_message = (
            f" Se guardaron {monitored_count} personas a monitorear."
            if monitored_count
            else ""
        )

        if is_new_user:
            flash(
                f"Registro local completado. Ya puedes entrar con ese RFC y contraseña.{monitored_message}",
                "success",
            )
        else:
            flash(
                f"Registro local actualizado. Ya puedes entrar con ese RFC y contraseña.{monitored_message}",
                "success",
            )

        logger.info("Registro web local guardado para RFC %s", form_data["rfc"])
        return redirect(url_for("clientes"))

    @app.get("/clientes")
    def clientes():
        user_rfc = session.get("user_rfc")
        if not user_rfc:
            return redirect(url_for("home"))

        try:
            _sync_registered_monitored_clients(user_rfc)
            _sync_local_client_rfcs(user_rfc)
        except Exception:
            logger.exception("Fallo la sincronizacion de clientes locales para RFC %s", user_rfc)
            flash(
                "No fue posible sincronizar algunos datos locales. Se muestra la ultima informacion disponible.",
                "warning",
            )

        if hasattr(local_client_store, "normalize_manual_authorization_pending"):
            try:
                local_client_store.normalize_manual_authorization_pending(owner_rfc=user_rfc)
            except Exception:
                logger.exception(
                    "Fallo la normalizacion manual de resultados locales para RFC %s",
                    user_rfc,
                )

        if hasattr(local_client_store, "normalize_public_png_pending_for_efirma"):
            try:
                local_client_store.normalize_public_png_pending_for_efirma(owner_rfc=user_rfc)
            except Exception:
                logger.exception(
                    "Fallo la normalizacion PUBLICO PNG para RFC %s",
                    user_rfc,
                )

        show_new_client_form = request.args.get("nuevo") == "1"
        local_client_form = _build_local_client_form_data()
        client_records = sorted(
            local_client_store.list_clients(owner_rfc=user_rfc),
            key=lambda client: (
                str(client.get("cliente") or "").casefold(),
                str(client.get("rfc") or "").casefold(),
            ),
        )
        clientes_registrados = [
            _local_client_to_view_model(client) for client in client_records
        ]
        resumen = _build_local_resumen(client_records)
        consulta_local_resumen = _build_local_consulta_resumen(
            client_records,
            include_captcha_pendiente=True,
        )
        consulta_local_count = consulta_local_resumen["total"]
        consulta_general_jobs = [
            job for job in _get_jobs() if (job.get("robot_name") or "") == "sat32d_locales"
        ]
        consulta_general_status = _build_consulta_general_status(consulta_general_jobs)

        return render_template(
            "clientes.html",
            user_rfc=user_rfc,
            resumen=resumen,
            consulta_local_count=consulta_local_count,
            consulta_local_resumen=consulta_local_resumen,
            consulta_general_status=consulta_general_status,
            local_client_form=local_client_form,
            show_new_client_form=show_new_client_form,
            clientes_registrados=clientes_registrados,
        )

    @app.get("/clientes/exportar-excel")
    def export_clientes_excel():
        user_rfc = session.get("user_rfc")
        if not user_rfc:
            return redirect(url_for("home"))

        try:
            _sync_registered_monitored_clients(user_rfc)
            _sync_local_client_rfcs(user_rfc)
        except Exception:
            logger.exception("Fallo la sincronizacion para exportar clientes RFC %s", user_rfc)
            flash(
                "No fue posible sincronizar todos los datos antes de exportar. Se usaran los registros disponibles.",
                "warning",
            )

        if hasattr(local_client_store, "normalize_manual_authorization_pending"):
            try:
                local_client_store.normalize_manual_authorization_pending(owner_rfc=user_rfc)
            except Exception:
                logger.exception(
                    "Fallo la normalizacion manual durante exportacion RFC %s",
                    user_rfc,
                )

        if hasattr(local_client_store, "normalize_public_png_pending_for_efirma"):
            try:
                local_client_store.normalize_public_png_pending_for_efirma(owner_rfc=user_rfc)
            except Exception:
                logger.exception(
                    "Fallo la normalizacion PUBLICO PNG durante exportacion RFC %s",
                    user_rfc,
                )

        client_records = sorted(
            local_client_store.list_clients(owner_rfc=user_rfc),
            key=lambda client: (
                str(client.get("cliente") or "").casefold(),
                str(client.get("rfc") or "").casefold(),
            ),
        )
        clientes_registrados = [
            _local_client_to_view_model(client) for client in client_records
        ]
        clientes_registrados = _filter_clientes_for_export(
            clientes_registrados,
            query=request.args.get("q"),
            modo=request.args.get("modo"),
            activo=request.args.get("activo"),
            resultado=request.args.get("resultado"),
        )

        workbook_bytes = _build_clientes_export_workbook(
            clientes_registrados=clientes_registrados,
            user_rfc=user_rfc,
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"clientes_consulta_{user_rfc}_{timestamp}.xlsx"

        return send_file(
            workbook_bytes,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.post("/clientes/locales")
    def create_local_client():
        user_rfc = session.get("user_rfc")
        if not user_rfc:
            return redirect(url_for("home"))

        form_data = _build_local_client_form_data(request.form)
        key_file = request.files.get("key_file")
        cer_file = request.files.get("cer_file")

        required_fields = {
            "cliente": "nombre del cliente",
            "rfc": "RFC del cliente",
            "efirma_password": "contrasena de la e.firma",
        }
        missing_fields = [
            label for field, label in required_fields.items() if not form_data[field]
        ]
        if missing_fields:
            flash(
                f"Debes capturar {', '.join(missing_fields)} para guardar el cliente local.",
                "error",
            )
            return redirect(url_for("clientes", nuevo=1))

        if key_file is None or cer_file is None:
            flash("Debes adjuntar los archivos .key y .cer del SAT para guardar el cliente local.", "error")
            return redirect(url_for("clientes", nuevo=1))

        try:
            local_client_store.add_client(
                owner_rfc=user_rfc,
                payload={
                    **form_data,
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
                key_file=key_file,
                cer_file=cer_file,
            )
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("clientes", nuevo=1))

        flash("Cliente local guardado correctamente sin depender del Excel.", "success")
        logger.info("Cliente local guardado para usuario RFC %s", user_rfc)
        return redirect(url_for("clientes"))

    @app.post("/clientes/locales/<client_id>/editar")
    def edit_local_client(client_id: str):
        user_rfc = session.get("user_rfc")
        if not user_rfc:
            return redirect(url_for("home"))

        form_data = _build_local_client_form_data(request.form)
        key_file = request.files.get("key_file")
        cer_file = request.files.get("cer_file")
        if key_file is not None and not (key_file.filename or "").strip():
            key_file = None
        if cer_file is not None and not (cer_file.filename or "").strip():
            cer_file = None
        required_fields = {
            "cliente": "nombre del cliente",
            "rfc": "RFC del cliente",
            "efirma_password": "contrasena de la e.firma",
        }
        missing_fields = [
            label for field, label in required_fields.items() if not form_data[field]
        ]
        if missing_fields:
            flash(
                f"Debes capturar {', '.join(missing_fields)} para editar el cliente local.",
                "error",
            )
            return redirect(url_for("clientes"))

        try:
            local_client_store.update_client(
                client_id,
                owner_rfc=user_rfc,
                payload=form_data,
                key_file=key_file,
                cer_file=cer_file,
            )
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("clientes"))

        flash("Cliente local actualizado correctamente.", "success")
        logger.info("Cliente local actualizado para usuario RFC %s", user_rfc)
        return redirect(url_for("clientes"))

    @app.post("/clientes/locales/<client_id>/eliminar")
    def delete_local_client(client_id: str):
        user_rfc = session.get("user_rfc")
        if not user_rfc:
            return redirect(url_for("home"))

        try:
            local_client_store.delete_client(client_id, owner_rfc=user_rfc)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("clientes"))

        flash("Cliente local eliminado correctamente.", "success")
        logger.info("Cliente local eliminado para usuario RFC %s", user_rfc)
        return redirect(url_for("clientes"))

    @app.post("/clientes/locales/<client_id>/consultar")
    def consult_local_client_individual(client_id: str):
        user_rfc = session.get("user_rfc")
        if not user_rfc:
            return jsonify({"ok": False, "message": "Sesion no valida."}), 401

        try:
            _sync_registered_monitored_clients(user_rfc)
            _sync_local_client_rfcs(user_rfc)
        except Exception:
            logger.exception("Fallo la sincronizacion previa a consulta individual para RFC %s", user_rfc)

        client_record = local_client_store.get_client(client_id, owner_rfc=user_rfc)
        if not client_record:
            return jsonify({"ok": False, "message": "No existe el cliente solicitado."}), 404

        robot_name = (
            "sat32d_tercero"
            if (client_record.get("modo_consulta") or "").strip().upper() == "TERCERO"
            else "sat32d_publico"
        )

        job_id = uuid.uuid4().hex[:8]
        _register_job(
            {
                "id": job_id,
                "robot_name": robot_name,
                "target_client_id": client_id,
                "target_client_rfc": client_record.get("rfc", ""),
                "tercero_rfc": "",
                "status": "queued",
                "summary": f"Consulta individual en cola para {client_record.get('rfc', '')}.",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

        worker = threading.Thread(
            target=_run_robot_job,
            kwargs={
                "job_id": job_id,
                "owner_rfc": user_rfc,
                "robot_name": robot_name,
                "target_client_id": client_id,
                "include_captcha_pendiente": False,
                "prefer_third_party": False,
                "tercero_rfc": None,
                "credentials": _get_credentials(),
            },
            daemon=True,
        )
        worker.start()

        refreshed_record = local_client_store.get_client(client_id, owner_rfc=user_rfc) or {}
        return jsonify(
            {
                "ok": True,
                "message": "Consulta individual enviada a ejecucion.",
                "resultado": refreshed_record.get("resultado") or "PENDIENTE",
                "fecha_consulta": refreshed_record.get("fecha_consulta") or "Sin fecha",
            }
        ), 202

    @app.get("/robots/jobs/<job_id>/status")
    def get_robot_job_status(job_id: str):
        user_rfc = session.get("user_rfc")
        if not user_rfc:
            return jsonify({"ok": False, "message": "Sesion no valida."}), 401

        job = next((item for item in _get_jobs() if (item.get("id") or "") == job_id), None)
        if not job:
            return jsonify({"ok": False, "message": "No existe el proceso solicitado."}), 404

        status = str(job.get("status") or "queued").strip().lower()
        return jsonify(
            {
                "ok": True,
                "job_id": job_id,
                "status": status,
                "status_label": _resolve_job_label(status),
                "started_at": str(job.get("started_at") or ""),
                "finished_at": str(job.get("finished_at") or ""),
                "summary": str(job.get("summary") or ""),
                "finished": status in {"completed", "error"},
            }
        )

    @app.post("/robots/run")
    def run_robot_from_web():
        user_rfc = session.get("user_rfc")
        if not user_rfc:
            return redirect(url_for("home"))

        _sync_registered_monitored_clients(user_rfc)
        _sync_local_client_rfcs(user_rfc)

        robot_name = (request.form.get("robot_name") or "").strip()
        target_client_id = (request.form.get("target_client_id") or "").strip() or None
        include_captcha_pendiente = request.form.get("include_captcha_pendiente") == "1"
        tercero_rfc = (request.form.get("tercero_rfc") or "").strip().upper() or None
        credentials = _get_credentials()
        queued_summary = "Esperando ejecucion"
        prefer_third_party = False
        efirma_count = 0
        deferred_tercero_count = 0

        local_records = local_client_store.list_clients(owner_rfc=user_rfc)
        target_record = (
            local_client_store.get_client(target_client_id, owner_rfc=user_rfc)
            if target_client_id
            else None
        )

        if robot_name == "sat32d_locales":
            prefer_third_party = _has_reusable_third_party_session(
                config=config,
                logger=logger,
                credentials=credentials,
            )
            grouped_records = _select_local_records_for_local_consulta(
                local_records,
                include_captcha_pendiente=include_captcha_pendiente,
                prefer_third_party=prefer_third_party,
            )
            tercero_count = len(grouped_records["sat32d_tercero"])
            efirma_records = _select_local_records_for_local_efirma_consulta(
                local_records,
                include_captcha_pendiente=include_captcha_pendiente,
                ignore_status=True,
            )
            efirma_count = len(efirma_records)
            efirma_record_keys = {id(record) for record in efirma_records}
            publico_count = len(
                [
                    record
                    for record in grouped_records["sat32d_publico"]
                    if id(record) not in efirma_record_keys
                ]
            )
            waiting_for_sat_records = (
                _select_local_records_waiting_for_sat_session(
                    local_records,
                    include_captcha_pendiente=include_captcha_pendiente,
                )
                if not prefer_third_party
                else []
            )
            deferred_tercero_count = len(waiting_for_sat_records)
            if not prefer_third_party:
                deferred_tercero_count = max(
                    0,
                    deferred_tercero_count
                    - len(
                        [
                            record
                            for record in waiting_for_sat_records
                            if id(record) in efirma_record_keys
                        ]
                    ),
                )

            total_count = publico_count + tercero_count + efirma_count + deferred_tercero_count

            if total_count == 0:
                flash("No hay clientes locales elegibles para consultar opiniones.", "error")
                return redirect(url_for("clientes"))

        if robot_name == "sat32d_tercero" and target_client_id is None:
            flash("La consulta TERCERO solo se permite para un cliente especifico.", "error")
            return redirect(url_for("clientes"))

        if robot_name == "sat32d_publico" and target_client_id is None:
            publico_count = sum(
                1
                for cliente in local_records
                if _is_local_publico_candidate(cliente, ignore_status=True)
            )
            if publico_count == 0:
                flash("No hay clientes PUBLICO elegibles para procesar.", "error")
                return redirect(url_for("clientes"))

        if robot_name == "sat32d_autoriza_tercero" and not tercero_rfc:
            flash("Debes capturar el RFC que se autorizara como tercero.", "error")
            return redirect(url_for("clientes"))

        if robot_name == "sat32d_tercero" and target_record is not None:
            queued_summary = f"Consulta TERCERO en cola para {target_record.get('rfc', '')}."
        elif robot_name == "sat32d_locales":
            if prefer_third_party:
                queued_summary = (
                    f"Consulta local en cola para {publico_count} clientes PUBLICO y {tercero_count} clientes TERCERO."
                )
            else:
                queued_summary = f"Consulta local en cola para {publico_count} clientes PUBLICO"
                if efirma_count:
                    queued_summary += (
                        f" y {efirma_count} clientes con e.firma."
                    )
                else:
                    queued_summary += "."
                if deferred_tercero_count:
                    queued_summary += (
                        f" {deferred_tercero_count} clientes TERCERO quedaran pendientes hasta contar con una sesion SAT activa o una e.firma valida."
                    )
        elif robot_name == "sat32d_publico":
            if target_record is None:
                publico_count = sum(
                    1
                    for cliente in local_records
                    if _is_local_publico_candidate(cliente, ignore_status=True)
                )
                queued_summary = f"Consulta PUBLICO masiva en cola para {publico_count} clientes locales."
            else:
                queued_summary = f"Consulta PUBLICO en cola para {target_record.get('rfc', '')}."
        elif robot_name == "sat32d_autoriza_tercero":
            queued_summary = f"Autorizacion de tercero en cola para RFC {tercero_rfc}."

        job_id = uuid.uuid4().hex[:8]
        _register_job(
            {
                "id": job_id,
                "robot_name": robot_name,
                "target_client_id": target_client_id,
                "target_client_rfc": target_record.get("rfc", "") if target_record else "",
                "tercero_rfc": tercero_rfc or "",
                "status": "queued",
                "summary": queued_summary,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

        worker = threading.Thread(
            target=_run_robot_job,
            kwargs={
                "job_id": job_id,
                "owner_rfc": user_rfc,
                "robot_name": robot_name,
                "target_client_id": target_client_id,
                "include_captcha_pendiente": include_captcha_pendiente,
                "prefer_third_party": prefer_third_party,
                "tercero_rfc": tercero_rfc,
                "credentials": credentials,
            },
            daemon=True,
        )
        worker.start()

        if robot_name == "sat32d_locales":
            if not prefer_third_party:
                if efirma_count:
                    flash(
                        "No se detecto una sesion SAT activa del RFC dueno; se usara la e.firma almacenada para los clientes con archivos validos y el resto quedara pendiente.",
                        "warning",
                    )
                else:
                    flash(
                        "No se detecto una sesion SAT activa del RFC dueno; los clientes TERCERO quedaran pendientes y solo se consultaran los clientes PUBLICO.",
                        "warning",
                    )
            flash("Se inicio la consulta de opiniones para los clientes locales elegibles.", "success")
        else:
            flash(f"Proceso {robot_name} enviado a ejecucion.", "success")
        return redirect(url_for("clientes"))

    @app.post("/logout")
    def logout():
        user_rfc = session.get("user_rfc")
        _clear_credentials()
        session.clear()
        if user_rfc:
            logger.info("Cierre de sesion web local para RFC %s", user_rfc)
        return redirect(url_for("home"))

    return app


def _local_client_to_view_model(client: dict) -> dict[str, str]:
    notas_cliente = client.get("notas_cliente") or client.get("observaciones") or ""
    resultado_observaciones = client.get("resultado_observaciones") or ""
    resultado = (client.get("resultado") or "PENDIENTE").strip().upper()
    error_cause_label = _build_local_client_error_label(
        resultado=resultado,
        resultado_observaciones=resultado_observaciones,
    )
    observaciones = " | ".join(
        part for part in [notas_cliente, resultado_observaciones] if part
    )

    return {
        "id": client.get("id", ""),
        "cliente": client.get("cliente", ""),
        "rfc": client.get("rfc", ""),
        "modo_consulta": client.get("modo_consulta", ""),
        "requiere_pdf": client.get("requiere_pdf", ""),
        "activo": client.get("activo", ""),
        "resultado": resultado,
        "error_cause_label": error_cause_label,
        "fecha_consulta": str(client.get("fecha_consulta") or ""),
        "archivo": client.get("archivo", ""),
        "observaciones": observaciones,
        "notas_cliente": client.get("notas_cliente", ""),
        "resultado_observaciones": client.get("resultado_observaciones", ""),
        "key_file_name": client.get("key_file_name", ""),
        "cer_file_name": client.get("cer_file_name", ""),
        "created_at": client.get("created_at", ""),
        "efirma_status": "Configurada" if client.get("efirma_password") else "Pendiente",
        "efirma_password": client.get("efirma_password", ""),
    }


def _build_local_client_error_label(*, resultado: str, resultado_observaciones: str) -> str:
    if (resultado or "").strip().upper() != "ERROR":
        return ""

    concise_message = SAT32DOpinionRobot._classify_efirma_error_text(
        resultado_observaciones or ""
    )
    if concise_message in LOCAL_CLIENT_ERROR_LABELS:
        return LOCAL_CLIENT_ERROR_LABELS[concise_message]

    normalized_observations = SAT32DOpinionRobot._normalize_efirma_text(
        resultado_observaciones or ""
    )
    if "TIEMPO DE ESPERA AGOTADO" in normalized_observations:
        return "Timeout e.firma"
    if "NO HAY CONTRASENA DE E.FIRMA" in normalized_observations:
        return "Sin contraseña e.firma"
    if (
        "NO SE ENCONTRO EL ARCHIVO .CER" in normalized_observations
        or "NO SE ENCONTRO EL ARCHIVO .KEY" in normalized_observations
    ):
        return "Archivo e.firma faltante"

    return "Error SAT"


def _resolve_job_badge_class(job_status: str) -> str:
    normalized_status = (job_status or "").strip().lower()
    return {
        "queued": "badge-job-queued",
        "running": "badge-job-running",
        "completed": "badge-job-completed",
        "error": "badge-job-error",
    }.get(normalized_status, "badge-job-queued")


def _resolve_job_label(job_status: str) -> str:
    normalized_status = (job_status or "").strip().lower()
    return {
        "queued": "En cola",
        "running": "En ejecucion",
        "completed": "Completada",
        "error": "Con error",
    }.get(normalized_status, "Sin estado")


def _build_consulta_general_status(jobs: list[dict]) -> dict[str, str]:
    if not jobs:
        return {
            "badge_class": "badge-job-queued",
            "label": "Sin ejecuciones",
            "timestamp": "",
            "detail": "Aun no hay ejecuciones de la consulta general.",
        }

    latest_job = jobs[0]
    latest_status = str(latest_job.get("status") or "")
    latest_summary = str(latest_job.get("summary") or "").strip()
    latest_timestamp = (
        str(latest_job.get("finished_at") or "").strip()
        or str(latest_job.get("started_at") or "").strip()
        or str(latest_job.get("created_at") or "").strip()
    )

    return {
        "badge_class": _resolve_job_badge_class(latest_status),
        "label": _resolve_job_label(latest_status),
        "timestamp": latest_timestamp,
        "detail": latest_summary or "Sin detalles disponibles.",
    }


def _build_consulta_general_history(jobs: list[dict], *, limit: int = 10) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for job in jobs[:limit]:
        job_status = str(job.get("status") or "")
        history.append(
            {
                "id": str(job.get("id") or ""),
                "label": _resolve_job_label(job_status),
                "badge_class": _resolve_job_badge_class(job_status),
                "summary": str(job.get("summary") or "").strip() or "Sin resumen disponible.",
                "created_at": str(job.get("created_at") or "").strip(),
                "started_at": str(job.get("started_at") or "").strip(),
                "finished_at": str(job.get("finished_at") or "").strip(),
            }
        )

    return history


def _build_clientes_export_workbook(
    *,
    clientes_registrados: list[dict[str, str]],
    user_rfc: str,
) -> BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Resultados"

    headers = [
        "Cliente",
        "RFC",
        "Modo",
        "Activo",
        "Resultado",
        "Causa error",
        "Fecha y hora de consulta",
        "Archivo",
        "Observaciones",
        "Estado e.firma",
        "Alta",
    ]
    sheet.append(headers)

    for client in clientes_registrados:
        sheet.append(
            [
                client.get("cliente", ""),
                client.get("rfc", ""),
                client.get("modo_consulta", ""),
                client.get("activo", ""),
                client.get("resultado", ""),
                client.get("error_cause_label", ""),
                client.get("fecha_consulta", ""),
                client.get("archivo", ""),
                client.get("observaciones", ""),
                client.get("efirma_status", ""),
                client.get("created_at", ""),
            ]
        )

    metadata = workbook.create_sheet(title="Metadata")
    metadata.append(["Campo", "Valor"])
    metadata.append(["RFC exportador", user_rfc])
    metadata.append(["Fecha exportacion", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    metadata.append(["Total registros", len(clientes_registrados)])

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


def _normalize_client_filter_value(value: str | None) -> str:
    normalized = unicodedata.normalize("NFD", (value or ""))
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn").strip().lower()


def _filter_clientes_for_export(
    clientes: list[dict[str, str]],
    *,
    query: str | None,
    modo: str | None,
    activo: str | None,
    resultado: str | None,
) -> list[dict[str, str]]:
    normalized_query = _normalize_client_filter_value(query)
    normalized_modo = _normalize_client_filter_value(modo)
    normalized_activo = _normalize_client_filter_value(activo)
    normalized_resultado = _normalize_client_filter_value(resultado)

    filtered_clients: list[dict[str, str]] = []
    for client in clientes:
        search_text = _normalize_client_filter_value(
            " ".join(
                [
                    client.get("cliente", ""),
                    client.get("rfc", ""),
                    client.get("modo_consulta", ""),
                    client.get("activo", ""),
                    client.get("resultado", "PENDIENTE") or "PENDIENTE",
                    client.get("fecha_consulta", "") or "Sin consulta",
                    client.get("observaciones", ""),
                    client.get("error_cause_label", ""),
                ]
            )
        )
        row_modo = _normalize_client_filter_value(client.get("modo_consulta", ""))
        row_activo = _normalize_client_filter_value(client.get("activo", ""))
        row_resultado = _normalize_client_filter_value(client.get("resultado", "PENDIENTE") or "PENDIENTE")

        matches_query = not normalized_query or normalized_query in search_text
        matches_modo = not normalized_modo or row_modo == normalized_modo
        matches_activo = not normalized_activo or row_activo == normalized_activo
        matches_resultado = not normalized_resultado or row_resultado == normalized_resultado

        if matches_query and matches_modo and matches_activo and matches_resultado:
            filtered_clients.append(client)

    return filtered_clients


def _build_local_resumen(local_clients: list[dict]) -> dict[str, int]:
    def _normalized(value: str | None) -> str:
        return (value or "").strip().upper()

    positive_statuses = {"POSITIVA", "POSITIVO"}
    negative_statuses = {"NEGATIVA", "NEGATIVO"}
    pending_statuses = {"", "PENDIENTE", "CAPTCHA_PENDIENTE", "PENDIENTE_SESION_SAT"}

    positivos = 0
    negativos = 0
    otros_resultados = 0
    errores = 0
    pendientes = 0

    for item in local_clients:
        normalized_result = _normalized(item.get("resultado"))
        if normalized_result in positive_statuses:
            positivos += 1
            continue
        if normalized_result in negative_statuses:
            negativos += 1
            continue
        if normalized_result == "ERROR":
            errores += 1
            continue
        if normalized_result in pending_statuses:
            pendientes += 1
            continue
        otros_resultados += 1

    return {
        "total": len(local_clients),
        "publico": sum(
            1 for item in local_clients if _normalized(item.get("modo_consulta")) == "PUBLICO"
        ),
        "tercero": sum(
            1 for item in local_clients if _normalized(item.get("modo_consulta")) == "TERCERO"
        ),
        "pendientes": sum(
            1
            for item in local_clients
            if _normalized(item.get("resultado")) in {"", "PENDIENTE", "CAPTCHA_PENDIENTE"}
        ),
        "positivos": positivos,
        "negativos": negativos,
        "otros_resultados": otros_resultados,
        "errores": errores,
        "pendiente_resultado": pendientes,
    }


def _is_local_publico_candidate(client: dict, *, ignore_status: bool = False) -> bool:
    if client.get("modo_consulta", "").strip().upper() != "PUBLICO":
        return False

    if client.get("activo", "").strip().upper() != "SI":
        return False

    if ignore_status:
        return True

    return (client.get("resultado") or "").strip().upper() in PENDING_RESULT_STATUSES


def _is_local_tercero_candidate(
    client: dict,
    *,
    include_captcha_pendiente: bool,
    ignore_status: bool = False,
) -> bool:
    return (
        client.get("modo_consulta", "").strip().upper() == "TERCERO"
        and _is_local_mass_tercero_candidate(
            client,
            include_captcha_pendiente=include_captcha_pendiente,
            ignore_status=ignore_status,
        )
    )


def _is_local_mass_tercero_candidate(
    client: dict,
    *,
    include_captcha_pendiente: bool,
    ignore_status: bool = False,
) -> bool:
    allowed_statuses = set(PENDING_RESULT_STATUSES)
    if include_captcha_pendiente:
        allowed_statuses.add("CAPTCHA_PENDIENTE")

    return (
        client.get("activo", "").strip().upper() == "SI"
        and client.get("requiere_pdf", "").strip().upper() == "SI"
        and bool((client.get("rfc") or "").strip())
        and (
            ignore_status
            or (client.get("resultado") or "").strip().upper() in allowed_statuses
        )
    )


def _is_local_authorization_candidate(client: dict) -> bool:
    return (
        client.get("activo", "").strip().upper() == "SI"
        and bool((client.get("rfc") or "").strip())
    )


def _select_local_records_for_local_consulta(
    records: list[dict],
    *,
    include_captcha_pendiente: bool,
    prefer_third_party: bool = False,
) -> dict[str, list[dict]]:
    tercero_records = (
        [
            record
            for record in records
            if _is_local_mass_tercero_candidate(
                record,
                include_captcha_pendiente=include_captcha_pendiente,
                ignore_status=True,
            )
        ]
        if prefer_third_party
        else []
    )
    tercero_record_keys = {id(record) for record in tercero_records}

    return {
        "sat32d_publico": [
            record
            for record in records
            if _is_local_publico_candidate(record, ignore_status=True)
            and id(record) not in tercero_record_keys
        ],
        "sat32d_tercero": tercero_records,
    }


def _select_local_records_waiting_for_sat_session(
    records: list[dict],
    *,
    include_captcha_pendiente: bool,
) -> list[dict]:
    return [
        record
        for record in records
        if _is_local_tercero_candidate(
            record,
            include_captcha_pendiente=include_captcha_pendiente,
            ignore_status=True,
        )
    ]


def _sort_local_records_for_consulta(records: list[dict]) -> list[dict]:
    return sorted(
        records,
        key=lambda record: (
            str(record.get("cliente") or "").casefold(),
            str(record.get("rfc") or "").casefold(),
        ),
    )


def _record_has_local_efirma_credentials(record: dict) -> bool:
    return bool(
        (record.get("key_path") or "").strip()
        and (record.get("cer_path") or "").strip()
        and (record.get("efirma_password") or "")
    )


def _select_local_records_with_local_efirma_credentials(records: list[dict]) -> list[dict]:
    return [record for record in records if _record_has_local_efirma_credentials(record)]


def _is_local_efirma_candidate(
    client: dict,
    *,
    include_captcha_pendiente: bool,
    ignore_status: bool = False,
) -> bool:
    return _record_has_local_efirma_credentials(client) and _is_local_mass_tercero_candidate(
        client,
        include_captcha_pendiente=include_captcha_pendiente,
        ignore_status=ignore_status,
    )


def _select_local_records_for_local_efirma_consulta(
    records: list[dict],
    *,
    include_captcha_pendiente: bool,
    ignore_status: bool = False,
) -> list[dict]:
    return [
        record
        for record in records
        if _is_local_efirma_candidate(
            record,
            include_captcha_pendiente=include_captcha_pendiente,
            ignore_status=ignore_status,
        )
    ]


def _build_local_consulta_resumen(
    records: list[dict],
    *,
    include_captcha_pendiente: bool,
) -> dict[str, int]:
    public_records = [
        record
        for record in records
        if _is_local_publico_candidate(record, ignore_status=True)
    ]
    tercero_records = [
        record
        for record in records
        if _is_local_tercero_candidate(
            record,
            include_captcha_pendiente=include_captcha_pendiente,
            ignore_status=True,
        )
    ]
    efirma_records = _select_local_records_for_local_efirma_consulta(
        records,
        include_captcha_pendiente=include_captcha_pendiente,
        ignore_status=True,
    )

    return {
        "total": len(public_records) + len(tercero_records),
        "publico": len(public_records),
        "tercero": len(tercero_records),
        "tercero_efirma": len(efirma_records),
    }


def _is_stale_public_no_autorizado_for_tercero(record: dict) -> bool:
    normalized_result = (record.get("resultado") or "").strip().upper()
    if normalized_result != "NO_AUTORIZADO":
        return False

    artifact_path = (record.get("archivo") or "").strip().lower()
    if "\\publico\\" in artifact_path or artifact_path.endswith(".png") and "publico" in artifact_path:
        return True

    observations = (
        record.get("resultado_observaciones")
        or record.get("observaciones")
        or ""
    ).upper()
    return (
        "NO SE ENCUENTRA AUTORIZADO PARA HACERSE PUBLICO" in observations
        or "NO SE ENCUENTRA AUTORIZADO PARA HACERSE PÚBLICO" in observations
    )


def _should_preserve_local_result_when_sat_session_is_missing(record: dict) -> bool:
    normalized_result = (record.get("resultado") or "").strip().upper()
    if normalized_result in EXPLICIT_OPINION_RESULT_STATUSES:
        return True
    if normalized_result in {"DESCARGADO", "CONSULTADO"}:
        return True
    if normalized_result == "NO_AUTORIZADO" and not _is_stale_public_no_autorizado_for_tercero(record):
        return True
    return False


def _mark_local_records_pending_due_to_missing_sat_session(
    records: list[dict],
    *,
    message: str,
) -> None:
    for record in records:
        if _should_preserve_local_result_when_sat_session_is_missing(record):
            continue

        record["resultado"] = "PENDIENTE"
        record["fecha_consulta"] = ""
        record["archivo"] = ""
        record["resultado_observaciones"] = message


def _select_local_records_for_robot(
    records: list[dict],
    *,
    robot_name: str,
    target_client_id: str | None,
    include_captcha_pendiente: bool,
) -> list[dict]:
    if robot_name == "sat32d_publico":
        publico_records = [
            record
            for record in records
            if _is_local_publico_candidate(
                record,
                ignore_status=True,
            )
        ]
        if target_client_id is None:
            return publico_records

        for record in publico_records:
            if record.get("id") == target_client_id:
                return [record]

        raise ValueError("El cliente local solicitado no existe o no es elegible para consulta PUBLICO.")

    if robot_name == "sat32d_tercero":
        if target_client_id is None:
            raise ValueError("La consulta TERCERO requiere un cliente local especifico.")

        tercero_records = [
            record
            for record in records
            if _is_local_tercero_candidate(
                record,
                include_captcha_pendiente=include_captcha_pendiente,
                ignore_status=True,
            )
        ]
        for record in tercero_records:
            if record.get("id") == target_client_id:
                return [record]

        raise ValueError("El cliente local solicitado no existe o no es elegible para consulta TERCERO.")

    if robot_name == "sat32d_autoriza_tercero":
        autoriza_records = [
            record
            for record in records
            if _is_local_authorization_candidate(record)
        ]
        if target_client_id is None:
            return autoriza_records

        for record in autoriza_records:
            if record.get("id") == target_client_id:
                return [record]

        raise ValueError(
            "El cliente local solicitado no existe o no es elegible para autorizacion de tercero."
        )

    return []


from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from src.certificate_utils import extract_certificate_rfc_from_file
from src.models import Cliente, EXPLICIT_OPINION_RESULT_STATUSES


ARTIFACT_RFC_PATTERN = re.compile(r"([A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3})_32D(?:_|\.)")


class LocalClientStore:
    REGISTER_MONITOR_SOURCE = "REGISTER_MONITOR"
    MANUAL_AUTHORIZATION_PENDING_MESSAGE = (
        "Resultado PUBLICO no autorizado marcado como pendiente para revalidar "
        "la autorizacion manual del SAT en la siguiente consulta."
    )
    PUBLICO_PNG_PENDING_MESSAGE = (
        "Resultado PUBLICO en PNG marcado como pendiente para reprocesar con e.firma "
        "y obtener PDF cuando SAT lo permita."
    )
    TRANSIENT_NETWORK_PENDING_MESSAGE = (
        "Error de red temporal con SAT (ERR_CONNECTION_RESET). "
        "Resultado marcado como PENDIENTE para reintento."
    )
    EFIRMA_INVALID_CREDENTIALS_CLEAN_MESSAGE = (
        "Credenciales de e.firma invalidas. "
        "Verifica certificado, clave privada y contrasena."
    )
    EFIRMA_REVOKED_CLEAN_MESSAGE = "La e.firma del cliente esta revocada."
    EFIRMA_EXPIRED_CLEAN_MESSAGE = "La e.firma del cliente no esta vigente."

    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = Path(storage_dir)
        self.uploads_dir = self.storage_dir / "uploads"
        self.db_path = self.storage_dir / "clients.json"
        self.registered_users_path = self.storage_dir / "registered_users.json"

    def load_registered_users(self) -> dict[str, dict]:
        payload = self._load_json_dict(self.registered_users_path)
        normalized_users: dict[str, dict] = {}

        for raw_rfc, raw_record in payload.items():
            rfc = str(raw_rfc or "").strip().upper()
            if not rfc:
                continue

            if isinstance(raw_record, str):
                normalized_users[rfc] = {
                    "password_hash": raw_record,
                    "profile": {},
                    "monitored_people": [],
                }
                continue

            if not isinstance(raw_record, dict):
                continue

            normalized_users[rfc] = {
                "password_hash": str(raw_record.get("password_hash") or ""),
                "profile": dict(raw_record.get("profile") or {}),
                "monitored_people": [
                    person.copy()
                    for person in raw_record.get("monitored_people", [])
                    if isinstance(person, dict)
                ],
            }

        return normalized_users

    def save_registered_user(
        self,
        rfc: str,
        *,
        password_hash: str,
        profile: dict[str, str],
        monitored_people: list[dict[str, str]],
    ) -> bool:
        normalized_rfc = (rfc or "").strip().upper()
        users = self.load_registered_users()
        is_new_user = normalized_rfc not in users
        users[normalized_rfc] = {
            "password_hash": password_hash,
            "profile": profile.copy(),
            "monitored_people": [person.copy() for person in monitored_people],
        }
        self._save_json_file(self.registered_users_path, users)
        return is_new_user

    def list_clients(self, *, owner_rfc: str | None = None) -> list[dict]:
        records = self._load_records()
        if owner_rfc:
            records = [
                record for record in records if record.get("owner_rfc", "") == owner_rfc
            ]
        records.sort(key=lambda record: record.get("created_at", ""), reverse=True)
        return records

    def get_client(self, client_id: str, *, owner_rfc: str | None = None) -> dict | None:
        for record in self.list_clients(owner_rfc=owner_rfc):
            if record.get("id") == client_id:
                return record
        return None

    def add_client(
        self,
        *,
        owner_rfc: str,
        payload: dict[str, str],
        key_file: FileStorage,
        cer_file: FileStorage,
    ) -> dict:
        client_id = secrets.token_hex(8)
        client_dir = self.uploads_dir / owner_rfc / client_id
        client_dir.mkdir(parents=True, exist_ok=True)

        try:
            key_path = self._save_uploaded_file(key_file, client_dir, expected_extension=".key")
            cer_path = self._save_uploaded_file(cer_file, client_dir, expected_extension=".cer")
            certified_rfc = self._validate_captured_rfc_against_certificate(
                captured_rfc=payload["rfc"],
                cer_path=cer_path,
            )
        except Exception:
            self._delete_client_dir(client_dir)
            raise

        record = {
            "id": client_id,
            "owner_rfc": owner_rfc,
            "cliente": payload["cliente"],
            "rfc": certified_rfc,
            "modo_consulta": payload["modo_consulta"],
            "requiere_pdf": payload["requiere_pdf"],
            "activo": payload["activo"],
            "efirma_password": payload["efirma_password"],
            "notas_cliente": payload["observaciones"],
            "resultado_observaciones": "",
            "resultado": "PENDIENTE",
            "fecha_consulta": "",
            "archivo": "",
            "ultimo_resultado_exitoso": "",
            "ultima_fecha_consulta_exitosa": "",
            "ultimo_archivo_exitoso": "",
            "ultimas_observaciones_exitosas": "",
            "prioridad": "",
            "key_file_name": key_path.name,
            "cer_file_name": cer_path.name,
            "key_path": str(key_path),
            "cer_path": str(cer_path),
            "created_at": payload["created_at"],
            "source": "LOCAL_STORAGE",
        }

        records = self._load_records()
        records.append(record)
        self._save_records(records)
        return record

    def sync_registered_monitored_clients(
        self,
        *,
        owner_rfc: str,
        monitored_people: list[dict[str, str]],
        synced_at: str | None = None,
    ) -> dict[str, int]:
        desired_people: dict[str, dict[str, str]] = {}
        for person in monitored_people:
            rfc = (person.get("rfc") or "").strip().upper()
            nombre = (person.get("nombre") or "").strip()
            if not rfc or not nombre:
                continue

            desired_people[rfc] = {
                "rfc": rfc,
                "cliente": nombre,
                "modo_consulta": (person.get("modo_consulta") or "PUBLICO").strip().upper(),
                "requiere_pdf": (person.get("requiere_pdf") or "SI").strip().upper(),
            }

        timestamp = synced_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        records = self._load_records()
        manual_rfcs = {
            (record.get("rfc") or "").strip().upper()
            for record in records
            if record.get("owner_rfc") == owner_rfc
            and record.get("source") != self.REGISTER_MONITOR_SOURCE
            and (record.get("rfc") or "").strip()
        }

        updated_records: list[dict] = []
        remaining_people = desired_people.copy()
        created_count = 0
        updated_count = 0
        removed_count = 0

        for record in records:
            is_synced_monitor = (
                record.get("owner_rfc") == owner_rfc
                and record.get("source") == self.REGISTER_MONITOR_SOURCE
            )
            if not is_synced_monitor:
                updated_records.append(record)
                continue

            rfc = (record.get("rfc") or "").strip().upper()
            payload = remaining_people.pop(rfc, None)
            if payload is None or rfc in manual_rfcs:
                removed_count += 1
                continue

            updated_records.append(
                self._build_registered_monitor_record(
                    owner_rfc=owner_rfc,
                    payload=payload,
                    created_at=record.get("created_at") or timestamp,
                    existing_record=record,
                )
            )
            updated_count += 1

        for rfc, payload in remaining_people.items():
            if rfc in manual_rfcs:
                continue

            updated_records.append(
                self._build_registered_monitor_record(
                    owner_rfc=owner_rfc,
                    payload=payload,
                    created_at=timestamp,
                )
            )
            created_count += 1

        self._save_records(updated_records)
        return {
            "created": created_count,
            "updated": updated_count,
            "removed": removed_count,
        }

    def update_client_result(
        self,
        client_id: str,
        *,
        resultado,
        fecha_consulta,
        archivo,
        observaciones,
    ) -> dict:
        records = self._load_records()
        updated_record: dict | None = None

        for record in records:
            if record.get("id") != client_id:
                continue

            self._ensure_last_success_snapshot(record)
            incoming_result = str(resultado or "")
            incoming_artifact = archivo or ""
            incoming_observaciones = observaciones or ""
            if (
                incoming_result.strip().upper() == "ERROR"
                and self._is_transient_network_error_observation(incoming_observaciones)
            ):
                incoming_result = "PENDIENTE"
                incoming_artifact = ""
                incoming_observaciones = self.TRANSIENT_NETWORK_PENDING_MESSAGE
            elif (
                incoming_result.strip().upper() == "ERROR"
                and self._is_efirma_error_observation(incoming_observaciones)
            ):
                incoming_artifact = ""
                incoming_observaciones = self._normalize_efirma_error_observation(
                    incoming_observaciones
                )
            elif (
                incoming_result.strip().upper() == "NO_AUTORIZADO"
                and self._should_promote_public_no_autorizado_to_pending(
                    record,
                    incoming_artifact=str(incoming_artifact),
                    incoming_observaciones=str(incoming_observaciones),
                )
            ):
                incoming_result = "PENDIENTE"
                incoming_artifact = ""
                incoming_observaciones = self.MANUAL_AUTHORIZATION_PENDING_MESSAGE

            current_result = str(record.get("resultado") or "")
            record["resultado"] = self._resolve_stored_result(
                current_result=current_result,
                incoming_result=incoming_result,
                modo_consulta=str(record.get("modo_consulta") or ""),
                current_artifact=str(record.get("archivo") or ""),
                incoming_artifact=str(incoming_artifact),
            )
            record["fecha_consulta"] = str(fecha_consulta or "")
            if record["resultado"] == incoming_result:
                record["archivo"] = incoming_artifact
                record["resultado_observaciones"] = incoming_observaciones
            if (
                record["resultado"] == incoming_result
                and self._is_successful_client_result(incoming_result)
            ):
                self._store_last_success_snapshot(
                    record,
                    resultado=incoming_result,
                    fecha_consulta=str(fecha_consulta or ""),
                    archivo=incoming_artifact,
                    observaciones=incoming_observaciones,
                )
            updated_record = record.copy()
            break

        if updated_record is None:
            raise ValueError(f"No existe el cliente local con id {client_id}.")

        self._save_records(records)
        return updated_record

    def update_client(
        self,
        client_id: str,
        *,
        owner_rfc: str,
        payload: dict[str, str],
        key_file: FileStorage | None = None,
        cer_file: FileStorage | None = None,
    ) -> dict:
        records = self._load_records()
        updated_record: dict | None = None

        for record in records:
            if record.get("id") != client_id:
                continue

            if record.get("owner_rfc") != owner_rfc:
                raise ValueError("No existe el cliente local solicitado para este usuario.")

            self._sync_record_upload_paths(record)
            previous_rfc = (record.get("rfc") or "").strip().upper()
            has_new_files = key_file is not None or cer_file is not None
            if has_new_files and (key_file is None or cer_file is None):
                raise ValueError(
                    "Debes adjuntar ambos archivos .key y .cer para actualizar la e.firma del cliente."
                )

            if has_new_files:
                key_path, cer_path, certified_rfc = self._replace_client_efirma_files(
                    client_id=client_id,
                    owner_rfc=owner_rfc,
                    captured_rfc=payload["rfc"],
                    current_record=record,
                    key_file=key_file,
                    cer_file=cer_file,
                )
                record["key_file_name"] = key_path.name
                record["cer_file_name"] = cer_path.name
                record["key_path"] = str(key_path)
                record["cer_path"] = str(cer_path)
            else:
                certified_rfc = self._validate_captured_rfc_against_certificate(
                    captured_rfc=payload["rfc"],
                    cer_path=Path(record.get("cer_path") or ""),
                )

            record["cliente"] = payload["cliente"]
            record["rfc"] = certified_rfc
            record["modo_consulta"] = payload["modo_consulta"]
            record["requiere_pdf"] = payload["requiere_pdf"]
            record["activo"] = payload["activo"]
            record["efirma_password"] = payload["efirma_password"]
            record["notas_cliente"] = payload["observaciones"]
            if previous_rfc != certified_rfc or has_new_files:
                self._reset_client_result_state(record)
            updated_record = record.copy()
            break

        if updated_record is None:
            raise ValueError(f"No existe el cliente local con id {client_id}.")

        self._save_records(records)
        return updated_record

    def _replace_client_efirma_files(
        self,
        *,
        client_id: str,
        owner_rfc: str,
        captured_rfc: str,
        current_record: dict,
        key_file: FileStorage,
        cer_file: FileStorage,
    ) -> tuple[Path, Path, str]:
        client_dir = self.uploads_dir / owner_rfc / client_id
        client_dir.mkdir(parents=True, exist_ok=True)

        staging_dir = client_dir / "_pending_efirma_update"
        if staging_dir.exists():
            self._delete_directory(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        staged_key_path: Path | None = None
        staged_cer_path: Path | None = None

        try:
            staged_key_path = self._save_uploaded_file(
                key_file,
                staging_dir,
                expected_extension=".key",
            )
            staged_cer_path = self._save_uploaded_file(
                cer_file,
                staging_dir,
                expected_extension=".cer",
            )
            certified_rfc = self._validate_captured_rfc_against_certificate(
                captured_rfc=captured_rfc,
                cer_path=staged_cer_path,
            )

            final_key_path = client_dir / staged_key_path.name
            final_cer_path = client_dir / staged_cer_path.name

            previous_key_path = Path(current_record.get("key_path") or "")
            previous_cer_path = Path(current_record.get("cer_path") or "")

            staged_key_path.replace(final_key_path)
            staged_cer_path.replace(final_cer_path)

            if previous_key_path and previous_key_path != final_key_path:
                previous_key_path.unlink(missing_ok=True)
            if previous_cer_path and previous_cer_path != final_cer_path:
                previous_cer_path.unlink(missing_ok=True)

            return final_key_path, final_cer_path, certified_rfc
        except Exception:
            if staged_key_path is not None:
                staged_key_path.unlink(missing_ok=True)
            if staged_cer_path is not None:
                staged_cer_path.unlink(missing_ok=True)
            raise
        finally:
            if staging_dir.exists():
                self._delete_directory(staging_dir)

    def sync_client_rfcs_from_certificates(
        self,
        *,
        owner_rfc: str | None = None,
    ) -> dict[str, int]:
        records = self._load_records()
        updated_count = 0
        skipped_count = 0

        for record in records:
            if owner_rfc and record.get("owner_rfc") != owner_rfc:
                continue

            record_updated = self._sync_record_upload_paths(record)

            cer_path_value = record.get("cer_path") or ""
            if not cer_path_value:
                skipped_count += 1
                continue

            try:
                certified_rfc = extract_certificate_rfc_from_file(Path(cer_path_value))
            except ValueError:
                skipped_count += 1
                continue

            stored_rfc = (record.get("rfc") or "").strip().upper()
            if stored_rfc != certified_rfc:
                record["rfc"] = certified_rfc
                self._reset_client_result_state(record)
                record_updated = True

            if self._record_has_stale_result_artifact(record):
                self._reset_client_result_state(record)
                record_updated = True

            if record_updated:
                updated_count += 1

        if updated_count:
            self._save_records(records)

        return {
            "updated": updated_count,
            "skipped": skipped_count,
        }

    def normalize_manual_authorization_pending(
        self,
        *,
        owner_rfc: str,
    ) -> int:
        records = self._load_records()
        updated_count = 0

        for record in records:
            if record.get("owner_rfc") != owner_rfc:
                continue

            if not self._should_reset_no_autorizado_for_manual_authorization(record):
                continue

            record["resultado"] = "PENDIENTE"
            record["fecha_consulta"] = ""
            record["archivo"] = ""
            record["resultado_observaciones"] = self.MANUAL_AUTHORIZATION_PENDING_MESSAGE
            updated_count += 1

        if updated_count:
            self._save_records(records)

        return updated_count

    def normalize_public_png_pending_for_efirma(
        self,
        *,
        owner_rfc: str,
    ) -> int:
        records = self._load_records()
        updated_count = 0

        for record in records:
            if record.get("owner_rfc") != owner_rfc:
                continue

            if not self._should_reset_public_png_result_for_efirma(record):
                continue

            self._ensure_last_success_snapshot(record)
            record["resultado"] = "PENDIENTE"
            record["fecha_consulta"] = ""
            record["archivo"] = ""
            record["resultado_observaciones"] = self.PUBLICO_PNG_PENDING_MESSAGE
            updated_count += 1

        if updated_count:
            self._save_records(records)

        return updated_count

    def delete_client(self, client_id: str, *, owner_rfc: str) -> None:
        records = self._load_records()
        kept_records = []
        deleted_record: dict | None = None

        for record in records:
            if record.get("id") == client_id and record.get("owner_rfc") == owner_rfc:
                deleted_record = record
                continue
            kept_records.append(record)

        if deleted_record is None:
            raise ValueError(f"No existe el cliente local con id {client_id}.")

        self._save_records(kept_records)

        owner = deleted_record.get("owner_rfc", "")
        client_dir = self.uploads_dir / owner / client_id
        if client_dir.exists():
            for path in client_dir.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
            client_dir.rmdir()

            owner_dir = self.uploads_dir / owner
            if owner_dir.exists() and not any(owner_dir.iterdir()):
                owner_dir.rmdir()

    def to_cliente(self, record: dict, *, row_number: int) -> Cliente:
        observaciones = (
            record.get("resultado_observaciones")
            or record.get("observaciones")
            or record.get("notas_cliente")
            or ""
        )
        return Cliente(
            rfc=record.get("rfc", ""),
            cliente=record.get("cliente", ""),
            modo_consulta=record.get("modo_consulta", ""),
            requiere_pdf=record.get("requiere_pdf", ""),
            autorizado=record.get("autorizado", ""),
            activo=record.get("activo", ""),
            resultado=record.get("resultado") or "",
            fecha_consulta=record.get("fecha_consulta") or "",
            archivo=record.get("archivo") or "",
            observaciones=observaciones,
            prioridad=record.get("prioridad") or "",
            row_number=row_number,
            key_path=record.get("key_path") or "",
            cer_path=record.get("cer_path") or "",
            efirma_password=record.get("efirma_password") or "",
        )

    def _load_records(self) -> list[dict]:
        return self._load_json_list(self.db_path)

    def _save_records(self, records: list[dict]) -> None:
        self._save_json_file(self.db_path, records)

    def _load_json_list(self, path: Path) -> list:
        payload = self._load_json_file(path)
        return payload if isinstance(payload, list) else []

    def _load_json_dict(self, path: Path) -> dict:
        payload = self._load_json_file(path)
        return payload if isinstance(payload, dict) else {}

    def _load_json_file(self, path: Path):
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            return None

        try:
            try:
                raw_text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raw_text = path.read_text(encoding="utf-8-sig")
            if raw_text.startswith("\ufeff"):
                raw_text = raw_text.lstrip("\ufeff")
            return json.loads(raw_text)
        except json.JSONDecodeError:
            return None

    def _save_json_file(self, path: Path, payload) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with NamedTemporaryFile(
                delete=False,
                suffix=path.suffix,
                dir=str(path.parent),
                mode="w",
                encoding="utf-8",
            ) as temp_file:
                temp_path = Path(temp_file.name)
                json.dump(payload, temp_file, ensure_ascii=True, indent=2)

            temp_path.replace(path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def _save_uploaded_file(
        self,
        uploaded_file: FileStorage,
        client_dir: Path,
        *,
        expected_extension: str,
    ) -> Path:
        original_name = secure_filename(uploaded_file.filename or "")
        if not original_name:
            raise ValueError(f"Debes seleccionar un archivo {expected_extension}.")

        if Path(original_name).suffix.lower() != expected_extension:
            raise ValueError(
                f"El archivo {original_name} debe tener la extension {expected_extension}."
            )

        target_path = client_dir / original_name
        uploaded_file.save(target_path)
        return target_path

    def _resolve_stored_result(
        self,
        *,
        current_result: str,
        incoming_result: str,
        modo_consulta: str,
        current_artifact: str,
        incoming_artifact: str,
    ) -> str:
        normalized_current = current_result.strip().upper()
        normalized_incoming = incoming_result.strip().upper()
        normalized_mode = modo_consulta.strip().upper()
        if (
            normalized_current in EXPLICIT_OPINION_RESULT_STATUSES
            and normalized_incoming in {"NO_AUTORIZADO", "ERROR", "CAPTCHA_PENDIENTE", "PENDIENTE"}
        ):
            if normalized_mode == "PUBLICO" and normalized_incoming == "NO_AUTORIZADO":
                return incoming_result

            current_artifact_source = self._resolve_artifact_source(current_artifact)
            incoming_artifact_source = self._resolve_artifact_source(incoming_artifact)
            if (
                normalized_mode == "PUBLICO"
                and current_artifact_source == "TERCEROS"
                and incoming_artifact_source == "PUBLICO"
            ):
                return incoming_result

            if (
                normalized_mode == "PUBLICO"
                and normalized_incoming == "ERROR"
                and current_artifact_source == "PUBLICO"
                and incoming_artifact_source == "ERRORES"
            ):
                return normalized_incoming

            return normalized_current
        return incoming_result

    @staticmethod
    def _is_transient_network_error_observation(observaciones: str) -> bool:
        normalized_observaciones = str(observaciones or "").upper()
        return "ERR_CONNECTION_RESET" in normalized_observaciones

    @staticmethod
    def _is_efirma_error_observation(observaciones: str) -> bool:
        normalized_observaciones = str(observaciones or "").upper().translate(
            str.maketrans("ÁÉÍÓÚÑ", "AEIOUN")
        )
        efirma_markers = (
            "CERTIFICADO, CLAVE PRIVADA O CONTRASENA DE CLAVE PRIVADA INVALID",
            "E.FIRMA ESTA REVOCADA",
            "E.FIRMA NO ESTA VIGENTE",
        )
        return any(marker in normalized_observaciones for marker in efirma_markers)

    @classmethod
    def _normalize_efirma_error_observation(cls, observaciones: str) -> str:
        normalized_observaciones = str(observaciones or "").upper().translate(
            str.maketrans("ÁÉÍÓÚÑ", "AEIOUN")
        )
        if "E.FIRMA ESTA REVOCADA" in normalized_observaciones:
            return cls.EFIRMA_REVOKED_CLEAN_MESSAGE
        if "E.FIRMA NO ESTA VIGENTE" in normalized_observaciones:
            return cls.EFIRMA_EXPIRED_CLEAN_MESSAGE
        return cls.EFIRMA_INVALID_CREDENTIALS_CLEAN_MESSAGE

    def _is_successful_client_result(self, result: str) -> bool:
        normalized_result = str(result or "").strip().upper()
        return normalized_result in EXPLICIT_OPINION_RESULT_STATUSES or normalized_result in {
            "CONSULTADO",
            "DESCARGADO",
        }

    def _ensure_last_success_snapshot(self, record: dict) -> None:
        if record.get("ultimo_resultado_exitoso"):
            return

        current_result = str(record.get("resultado") or "")
        if not self._is_successful_client_result(current_result):
            return

        self._store_last_success_snapshot(
            record,
            resultado=current_result,
            fecha_consulta=str(record.get("fecha_consulta") or ""),
            archivo=str(record.get("archivo") or ""),
            observaciones=str(record.get("resultado_observaciones") or ""),
        )

    def _store_last_success_snapshot(
        self,
        record: dict,
        *,
        resultado: str,
        fecha_consulta: str,
        archivo: str,
        observaciones: str,
    ) -> None:
        record["ultimo_resultado_exitoso"] = str(resultado or "").strip().upper()
        record["ultima_fecha_consulta_exitosa"] = str(fecha_consulta or "")
        record["ultimo_archivo_exitoso"] = str(archivo or "")
        record["ultimas_observaciones_exitosas"] = str(observaciones or "")

    def _resolve_artifact_source(self, artifact_path: str) -> str:
        normalized_path = str(artifact_path or "").replace("\\", "/").casefold()
        if "/publico/" in normalized_path:
            return "PUBLICO"
        if "/terceros/" in normalized_path:
            return "TERCEROS"
        if "/errores/" in normalized_path:
            return "ERRORES"
        return ""

    def _should_reset_no_autorizado_for_manual_authorization(self, record: dict) -> bool:
        if record.get("source") != "LOCAL_STORAGE":
            return False

        if (record.get("resultado") or "").strip().upper() != "NO_AUTORIZADO":
            return False

        if not self._record_has_local_efirma_credentials(record):
            return False

        archivo_source = self._resolve_artifact_source(str(record.get("archivo") or ""))
        if archivo_source == "PUBLICO":
            return True

        observations = str(record.get("resultado_observaciones") or "").upper()
        return (
            "NO SE ENCUENTRA AUTORIZADO PARA HACERSE PUBLICO" in observations
            or "NO SE ENCUENTRA AUTORIZADO PARA HACERSE PÚBLICO" in observations
        )

    def _should_promote_public_no_autorizado_to_pending(
        self,
        record: dict,
        *,
        incoming_artifact: str,
        incoming_observaciones: str,
    ) -> bool:
        if record.get("source") != "LOCAL_STORAGE":
            return False

        if (record.get("modo_consulta") or "").strip().upper() != "PUBLICO":
            return False

        if not self._record_has_local_efirma_credentials(record):
            return False

        if self._resolve_artifact_source(incoming_artifact) == "PUBLICO":
            return True

        return self._looks_like_public_no_autorizado_observation(incoming_observaciones)

    @staticmethod
    def _looks_like_public_no_autorizado_observation(observaciones: str) -> bool:
        normalized = str(observaciones or "").upper().translate(
            str.maketrans("ÁÉÍÓÚÑ", "AEIOUN")
        )
        return "NO SE ENCUENTRA AUTORIZADO PARA HACERSE P" in normalized

    def _should_reset_public_png_result_for_efirma(self, record: dict) -> bool:
        if record.get("source") != "LOCAL_STORAGE":
            return False

        if (record.get("modo_consulta") or "").strip().upper() != "PUBLICO":
            return False

        if not self._record_has_local_efirma_credentials(record):
            return False

        normalized_result = (record.get("resultado") or "").strip().upper()
        if normalized_result not in EXPLICIT_OPINION_RESULT_STATUSES:
            return False

        artifact_path = str(record.get("archivo") or "")
        if not artifact_path:
            return False

        if self._resolve_artifact_source(artifact_path) != "PUBLICO":
            return False

        return artifact_path.strip().lower().endswith(".png")

    def _record_has_local_efirma_credentials(self, record: dict) -> bool:
        return bool(
            (record.get("key_path") or "").strip()
            and (record.get("cer_path") or "").strip()
            and (record.get("efirma_password") or "")
        )

    def _validate_captured_rfc_against_certificate(
        self,
        *,
        captured_rfc: str,
        cer_path: Path,
    ) -> str:
        certified_rfc = extract_certificate_rfc_from_file(cer_path)
        normalized_captured_rfc = (captured_rfc or "").strip().upper()
        if normalized_captured_rfc != certified_rfc:
            raise ValueError(
                f"El RFC capturado ({normalized_captured_rfc}) no coincide con el certificado .cer ({certified_rfc})."
            )
        return certified_rfc

    def _delete_client_dir(self, client_dir: Path) -> None:
        if not client_dir.exists():
            return

        self._delete_directory(client_dir)

        owner_dir = client_dir.parent
        if owner_dir.exists() and not any(owner_dir.iterdir()):
            owner_dir.rmdir()

    def _delete_directory(self, directory: Path) -> None:
        if not directory.exists():
            return

        for path in directory.iterdir():
            if path.is_dir():
                self._delete_directory(path)
                continue
            path.unlink(missing_ok=True)

        directory.rmdir()

    def _reset_client_result_state(self, record: dict) -> None:
        record["resultado"] = "PENDIENTE"
        record["fecha_consulta"] = ""
        record["archivo"] = ""
        record["resultado_observaciones"] = ""
        record["ultimo_resultado_exitoso"] = ""
        record["ultima_fecha_consulta_exitosa"] = ""
        record["ultimo_archivo_exitoso"] = ""
        record["ultimas_observaciones_exitosas"] = ""

    def _record_has_stale_result_artifact(self, record: dict) -> bool:
        current_rfc = (record.get("rfc") or "").strip().upper()
        artifact_path = (record.get("archivo") or "").strip()
        if not current_rfc or not artifact_path:
            return False

        artifact_name = Path(artifact_path).name.upper()
        match = ARTIFACT_RFC_PATTERN.search(artifact_name)
        if not match:
            return False

        artifact_rfc = match.group(1).upper()
        return artifact_rfc != current_rfc

    def _sync_record_upload_paths(self, record: dict) -> bool:
        updated = False
        for field_name in ("key_path", "cer_path"):
            current_value = record.get(field_name) or ""
            resolved_path = self._resolve_existing_upload_path(current_value)
            if not current_value or str(resolved_path) == current_value:
                continue

            record[field_name] = str(resolved_path)
            updated = True

        return updated

    def _resolve_existing_upload_path(self, stored_path: str) -> Path:
        candidate_path = Path(stored_path)
        if candidate_path.exists():
            return candidate_path

        normalized_parts = [part.lower() for part in candidate_path.parts]
        try:
            uploads_index = normalized_parts.index("uploads")
        except ValueError:
            return candidate_path

        rebased_path = self.uploads_dir.joinpath(*candidate_path.parts[uploads_index + 1 :])
        if rebased_path.exists():
            return rebased_path

        return candidate_path

    def _build_registered_monitor_record(
        self,
        *,
        owner_rfc: str,
        payload: dict[str, str],
        created_at: str,
        existing_record: dict | None = None,
    ) -> dict:
        record = (existing_record or {}).copy()
        record.update(
            {
                "id": record.get("id") or secrets.token_hex(8),
                "owner_rfc": owner_rfc,
                "cliente": payload["cliente"],
                "rfc": payload["rfc"],
                "modo_consulta": payload["modo_consulta"],
                "requiere_pdf": payload["requiere_pdf"],
                "activo": "SI",
                "efirma_password": record.get("efirma_password") or "",
                "notas_cliente": record.get("notas_cliente") or "",
                "resultado_observaciones": record.get("resultado_observaciones") or "",
                "resultado": record.get("resultado") or "PENDIENTE",
                "fecha_consulta": record.get("fecha_consulta") or "",
                "archivo": record.get("archivo") or "",
                "ultimo_resultado_exitoso": record.get("ultimo_resultado_exitoso") or "",
                "ultima_fecha_consulta_exitosa": record.get("ultima_fecha_consulta_exitosa") or "",
                "ultimo_archivo_exitoso": record.get("ultimo_archivo_exitoso") or "",
                "ultimas_observaciones_exitosas": record.get("ultimas_observaciones_exitosas") or "",
                "prioridad": record.get("prioridad") or "",
                "key_file_name": record.get("key_file_name") or "",
                "cer_file_name": record.get("cer_file_name") or "",
                "key_path": record.get("key_path") or "",
                "cer_path": record.get("cer_path") or "",
                "created_at": created_at,
                "source": self.REGISTER_MONITOR_SOURCE,
            }
        )
        return record
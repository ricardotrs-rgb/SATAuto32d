from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import ObjectIdentifier


RFC_IDENTIFIER_OID = ObjectIdentifier("2.5.4.45")
RFC_PATTERN = re.compile(r"\b[A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3}\b")


def extract_certificate_rfc_from_file(cert_path: Path) -> str:
    try:
        cert_bytes = Path(cert_path).read_bytes()
    except OSError as error:
        raise ValueError(f"No fue posible leer el certificado .cer: {cert_path}") from error

    try:
        return extract_certificate_rfc_from_bytes(cert_bytes)
    except ValueError as error:
        if os.name != "nt":
            raise
        return _extract_certificate_rfc_with_powershell(Path(cert_path), error)


def extract_certificate_rfc_from_bytes(cert_bytes: bytes) -> str:
    certificate = _load_certificate(cert_bytes)
    subject_values = [
        attribute.value
        for attribute in certificate.subject.get_attributes_for_oid(RFC_IDENTIFIER_OID)
    ]

    for subject_value in subject_values:
        extracted_rfc = extract_rfc_from_subject_identifier(subject_value)
        if extracted_rfc:
            return extracted_rfc

    subject_text = certificate.subject.rfc4514_string()
    extracted_rfc = extract_rfc_from_subject_identifier(subject_text)
    if extracted_rfc:
        return extracted_rfc

    raise ValueError("No fue posible extraer el RFC del certificado .cer.")


def extract_rfc_from_subject_identifier(identifier: str) -> str:
    normalized_identifier = (identifier or "").upper()
    for segment in [part.strip() for part in normalized_identifier.split("/") if part.strip()]:
        match = RFC_PATTERN.search(segment)
        if match:
            return match.group(0)

    match = RFC_PATTERN.search(normalized_identifier)
    return match.group(0) if match else ""


def _load_certificate(cert_bytes: bytes):
    try:
        return x509.load_der_x509_certificate(cert_bytes)
    except ValueError:
        try:
            return x509.load_pem_x509_certificate(cert_bytes)
        except ValueError as error:
            raise ValueError("No fue posible interpretar el certificado .cer.") from error


def _extract_certificate_rfc_with_powershell(cert_path: Path, original_error: ValueError) -> str:
    escaped_path = str(cert_path).replace("'", "''")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            (
                "$cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]"
                f"::new('{escaped_path}'); $cert.Subject"
            ),
        ],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    subject_text = (result.stdout or "").strip()
    if result.returncode != 0 or not subject_text:
        raise ValueError("No fue posible extraer el RFC del certificado .cer.") from original_error

    extracted_rfc = extract_rfc_from_subject_identifier(subject_text)
    if extracted_rfc:
        return extracted_rfc

    raise ValueError("No fue posible extraer el RFC del certificado .cer.") from original_error
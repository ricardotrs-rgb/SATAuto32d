from __future__ import annotations

import json

from src.local_client_store import LocalClientStore


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

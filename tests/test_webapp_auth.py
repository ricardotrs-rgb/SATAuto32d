from __future__ import annotations

import logging
from pathlib import Path

import src.webapp as webapp_module
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
from src.webapp import create_web_app


class _FakeLocalClientStore:
    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = Path(storage_dir)
        self._registered_users: dict[str, dict] = {}

    def load_registered_users(self) -> dict[str, dict]:
        return {
            rfc: {
                "password_hash": record.get("password_hash", ""),
                "profile": dict(record.get("profile") or {}),
                "monitored_people": [
                    person.copy() for person in record.get("monitored_people", [])
                ],
            }
            for rfc, record in self._registered_users.items()
        }

    def save_registered_user(
        self,
        rfc: str,
        *,
        password_hash: str,
        profile: dict[str, str],
        monitored_people: list[dict[str, str]],
    ) -> bool:
        normalized_rfc = (rfc or "").strip().upper()
        is_new_user = normalized_rfc not in self._registered_users
        self._registered_users[normalized_rfc] = {
            "password_hash": password_hash,
            "profile": profile.copy(),
            "monitored_people": [person.copy() for person in monitored_people],
        }
        return is_new_user


def _build_app_config(tmp_path) -> AppConfig:
    robot_config = WebRobotConfig(
        enabled=True,
        start_url="https://example.com",
        timeout_seconds=45,
        browser="chromium",
        headless=True,
        capture=CaptureConfig(enabled=False, file_name="robot.png", full_page=True),
        download=DownloadConfig(enabled=False, trigger_selector="", target_subdir="robot"),
        login=LoginConfig(
            enabled=False,
            username="",
            password="",
            username_selector="",
            password_selector="",
            submit_selector="",
        ),
        public_query=PublicQueryConfig(
            rfc_input_selector="",
            submit_selector="",
            result_selector="",
            unauthorized_selector="",
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
    return AppConfig(
        project_name="sat32d_web",
        paths=ProjectPaths(
            control=tmp_path / "control",
            publico=tmp_path / "publico",
            terceros=tmp_path / "terceros",
            errores=tmp_path / "errores",
            logs=tmp_path / "logs",
        ),
        logging=LoggingConfig(level="INFO", file_name="sat32d_web.log"),
        robots={"sat32d_publico": robot_config},
    )


def _build_app(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp_module, "LocalClientStore", _FakeLocalClientStore)
    return create_web_app(
        _build_app_config(tmp_path),
        logging.getLogger("test.webapp.auth"),
    )


def test_login_accepts_whitelisted_account_without_registration(tmp_path, monkeypatch) -> None:
    app = _build_app(tmp_path, monkeypatch)

    with app.test_client() as client:
        response = client.post(
            "/login",
            data={"rfc": "IOEF840128UC4", "password": "Javi1984"},
            follow_redirects=False,
        )

        with client.session_transaction() as session_state:
            assert session_state["user_rfc"] == "IOEF840128UC4"

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/clientes")


def test_login_rejects_whitelisted_rfc_with_wrong_password(tmp_path, monkeypatch) -> None:
    app = _build_app(tmp_path, monkeypatch)

    with app.test_client() as client:
        response = client.post(
            "/login",
            data={"rfc": "IOEF840128UC4", "password": "Otra123"},
            follow_redirects=True,
        )

        with client.session_transaction() as session_state:
            assert "user_rfc" not in session_state

    assert response.status_code == 200
    assert b"La contrase\xc3\xb1a no coincide con la autorizada para esta aplicacion." in response.data


def test_login_rejects_non_whitelisted_rfc_and_clears_previous_session(tmp_path, monkeypatch) -> None:
    app = _build_app(tmp_path, monkeypatch)

    with app.test_client() as client:
        with client.session_transaction() as session_state:
            session_state["user_rfc"] = "IOEF840128UC4"
            session_state["web_session_id"] = "session-old"

        response = client.post(
            "/login",
            data={"rfc": "ZZZ010101ZZZ", "password": "secret"},
            follow_redirects=False,
        )

        with client.session_transaction() as session_state:
            assert "user_rfc" not in session_state

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_login_accepts_registered_rfc(tmp_path, monkeypatch) -> None:
    app = _build_app(tmp_path, monkeypatch)

    with app.test_client() as client:
        register_response = client.post(
            "/register",
            data={
                "rfc": "IOEF840128UC4",
                "email": "usuario@example.com",
                "password": "Javi1984",
            },
            follow_redirects=False,
        )
        assert register_response.status_code == 302

        logout_response = client.post("/logout", follow_redirects=False)
        assert logout_response.status_code == 302

        login_response = client.post(
            "/login",
            data={"rfc": "IOEF840128UC4", "password": "Javi1984"},
            follow_redirects=False,
        )

        with client.session_transaction() as session_state:
            assert session_state["user_rfc"] == "IOEF840128UC4"

    assert login_response.status_code == 302
    assert login_response.headers["Location"].endswith("/clientes")


def test_register_rejects_non_whitelisted_rfc(tmp_path, monkeypatch) -> None:
    app = _build_app(tmp_path, monkeypatch)

    with app.test_client() as client:
        response = client.post(
            "/register",
            data={
                "rfc": "AAA010101AAA",
                "email": "usuario@example.com",
                "password": "secret",
            },
            follow_redirects=True,
        )

        login_response = client.post(
            "/login",
            data={"rfc": "AAA010101AAA", "password": "secret"},
            follow_redirects=True,
        )

        with client.session_transaction() as session_state:
            assert "user_rfc" not in session_state

    assert response.status_code == 200
    assert b"Solo el RFC autorizado puede registrarse en esta aplicacion." in response.data
    assert login_response.status_code == 200
    assert b"Ese RFC no esta autorizado para usar esta aplicacion." in login_response.data


def test_clientes_rejects_invalid_stale_session(tmp_path, monkeypatch) -> None:
    app = _build_app(tmp_path, monkeypatch)

    with app.test_client() as client:
        with client.session_transaction() as session_state:
            session_state["user_rfc"] = "AAA010101AAA"
            session_state["web_session_id"] = "session-old"

        response = client.get("/clientes", follow_redirects=False)

        with client.session_transaction() as session_state:
            assert "user_rfc" not in session_state

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_clientes_survives_sync_failure(tmp_path, monkeypatch) -> None:
    class _FailingLocalClientStore(_FakeLocalClientStore):
        def list_clients(self, *args, **kwargs):
            return []

        def sync_registered_monitored_clients(self, *args, **kwargs):
            raise RuntimeError("sync error")

        def sync_client_rfcs_from_certificates(self, *args, **kwargs):
            raise RuntimeError("sync error")

    monkeypatch.setattr(webapp_module, "LocalClientStore", _FailingLocalClientStore)
    app = create_web_app(
        _build_app_config(tmp_path),
        logging.getLogger("test.webapp.auth"),
    )

    with app.test_client() as client:
        login_response = client.post(
            "/login",
            data={"rfc": "IOEF840128UC4", "password": "Javi1984"},
            follow_redirects=False,
        )
        assert login_response.status_code == 302

        response = client.get("/clientes")

    assert response.status_code == 200
    assert b"Clientes registrados" in response.data


def test_clientes_triggers_public_png_normalization(tmp_path, monkeypatch) -> None:
    class _TrackingLocalClientStore(_FakeLocalClientStore):
        normalize_calls = 0

        def list_clients(self, *args, **kwargs):
            return []

        def sync_registered_monitored_clients(self, *args, **kwargs):
            return {"created": 0, "updated": 0, "removed": 0}

        def sync_client_rfcs_from_certificates(self, *args, **kwargs):
            return {"updated": 0, "skipped": 0}

        def normalize_public_png_pending_for_efirma(self, *args, **kwargs):
            type(self).normalize_calls += 1
            return 0

    monkeypatch.setattr(webapp_module, "LocalClientStore", _TrackingLocalClientStore)
    app = create_web_app(
        _build_app_config(tmp_path),
        logging.getLogger("test.webapp.auth"),
    )

    with app.test_client() as client:
        login_response = client.post(
            "/login",
            data={"rfc": "IOEF840128UC4", "password": "Javi1984"},
            follow_redirects=False,
        )
        assert login_response.status_code == 302

        response = client.get("/clientes")

    assert response.status_code == 200
    assert _TrackingLocalClientStore.normalize_calls == 1

from __future__ import annotations

from datetime import datetime

from src.models import RobotRunResult
from src.robots.base import BaseWebRobot
from src.robots.playwright_client import open_page


class SAT32DWebBaseRobot(BaseWebRobot):
    robot_name = "sat32d_web_base"

    def run(self) -> RobotRunResult:
        with open_page(self.robot_config) as page:
            page.goto(self.resolve_start_url(), wait_until="domcontentloaded")
            self.run_login_if_enabled(page)
            self.run_steps(page)
            capture_path = self.capture_page_if_enabled(page, label="landing")
            download_path = self.download_if_enabled(page)
            page_title = page.title()

        self.logger.info(
            "Robot %s conecto con Playwright. URL base: %s | titulo: %s | captura: %s | descarga: %s",
            self.robot_name,
            self.robot_config.start_url,
            page_title,
            capture_path,
            download_path,
        )
        print(
            f"Robot {self.robot_name} conectado con Playwright. Titulo detectado: {page_title}"
        )
        if capture_path is not None:
            print(f"Captura guardada en: {capture_path}")
        if download_path is not None:
            print(f"Descarga guardada en: {download_path}")

        return RobotRunResult(
            resultado="NAVEGADO",
            fecha_consulta=datetime.now(),
            archivo=str(download_path or capture_path) if (download_path or capture_path) else None,
            observaciones=f"Robot base ejecutado. Titulo detectado: {page_title}",
        )
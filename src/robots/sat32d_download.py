from __future__ import annotations

from datetime import datetime

from src.models import RobotRunResult
from src.robots.base import BaseWebRobot
from src.robots.playwright_client import open_page


class SAT32DDownloadRobot(BaseWebRobot):
    robot_name = "sat32d_descarga_demo"

    def run(self) -> RobotRunResult:
        with open_page(self.robot_config) as page:
            page.goto(self.resolve_start_url(), wait_until="domcontentloaded")
            self.run_login_if_enabled(page)
            self.run_steps(page)
            capture_path = self.capture_page_if_enabled(page, label="pre_download")
            download_path = self.download_if_enabled(page)
            page_title = page.title()

        self.logger.info(
            "Robot %s completo descarga. URL base: %s | titulo: %s | captura: %s | descarga: %s",
            self.robot_name,
            self.robot_config.start_url,
            page_title,
            capture_path,
            download_path,
        )
        print(
            f"Robot {self.robot_name} completo la descarga. Titulo detectado: {page_title}"
        )
        if capture_path is not None:
            print(f"Captura guardada en: {capture_path}")
        if download_path is not None:
            print(f"Descarga guardada en: {download_path}")

        return RobotRunResult(
            resultado="DESCARGADO" if download_path is not None else "CONSULTADO",
            fecha_consulta=datetime.now(),
            archivo=str(download_path or capture_path) if (download_path or capture_path) else None,
            observaciones=f"Descarga demo completada. Titulo detectado: {page_title}",
        )
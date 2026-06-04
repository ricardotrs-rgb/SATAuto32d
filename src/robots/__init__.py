from src.robots.base import BaseWebRobot
from src.robots.robot_autoriza_tercero import SAT32DAutorizaTerceroRobot
from src.robots.sat32d_download import SAT32DDownloadRobot
from src.robots.sat32d_opinion import SAT32DOpinionRobot
from src.robots.robot_publico import SAT32DPublicoRobot
from src.robots.robot_tercero import SAT32DTerceroRobot
from src.robots.sat32d_web_base import SAT32DWebBaseRobot


def get_robot(name: str, config, logger, cliente=None) -> BaseWebRobot:
    registry = {
        "sat32d_autoriza_tercero": SAT32DAutorizaTerceroRobot,
        "sat32d_publico": SAT32DPublicoRobot,
        "sat32d_tercero": SAT32DTerceroRobot,
        "sat32d_descarga_demo": SAT32DDownloadRobot,
        "sat32d_opinion_cumplimiento": SAT32DOpinionRobot,
        "sat32d_web_base": SAT32DWebBaseRobot,
    }

    robot_class = registry.get(name)
    if robot_class is None:
        available = ", ".join(sorted(registry))
        from src.errors import RobotError

        raise RobotError(f"Robot no registrado: {name}. Disponibles: {available}")

    return robot_class(config=config, logger=logger, cliente=cliente)
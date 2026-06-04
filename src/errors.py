class SAT32DError(Exception):
    """Error base del proyecto."""


class ConfigError(SAT32DError):
    """Error de configuracion del proyecto."""


class RobotError(SAT32DError):
    """Error relacionado con la ejecucion de robots."""
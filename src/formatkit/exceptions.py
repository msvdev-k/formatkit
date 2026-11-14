



class BaseFormatKitException(Exception):
    """Базовое исключение пакета"""


class FileManagerError(BaseFormatKitException):
    """Базовое исключение для менеджера файлов"""
    pass


class FormatManagerError(BaseFormatKitException):
    """Базовое исключение для менеджера формата файлов"""
    pass

class FormatKitException(Exception):
    """Базовое исключение пакета"""


# =====================================================================
# 1. Ошибки уровня контейнера и файловой системы (FileManager)
# =====================================================================

class FileManagerError(FormatKitException):
    """Базовое исключение для менеджера файлов"""
    pass


class FileNotOpenError(FileManagerError):
    """Попытка выполнить операцию с закрытым файлом"""


class FileAlreadyOpenError(FileManagerError):
    """Попытка открыть или создать файл, когда менеджер уже удерживает другой файл"""


class FileAlreadyExistsError(FileManagerError):
    """Попытка создать новый файл поверх существующего"""


class FileMissingError(FileManagerError):
    """Запрашиваемый файл не найден"""


class ReadOnlyFileError(FileManagerError):
    """Попытка выполнить операцию записи или открыть write-транзакцию в файле, открытом только для чтения"""


class FileLockedError(FileManagerError):
    """Файл заблокирован другим процессом или потоком (истек timeout ожидания)"""


class FileCorruptedError(FileManagerError):
    """Файл поврежден, не является валидным контейнером или нарушена его целостность"""


# =====================================================================
# 2. Ошибки уровня формата и схемы данных (FormatManager)
# =====================================================================

class FormatManagerError(FormatKitException):
    """Базовое исключение для менеджера формата файлов"""
    pass


class InvalidFileSignatureError(FormatManagerError):
    """Файл не принадлежит данному приложению (неверный application_id)"""


class UnsupportedFormatVersionError(FormatManagerError):
    """Версия файла новее, чем поддерживает текущая версия приложения (нужно обновить программу)"""


class FormatCorruptedError(FormatManagerError):
    """Ошибка при проверке схемы (версия файла не соответствует схеме БД)"""


class MigrationError(FormatManagerError):
    """Ошибка при обновлении схемы данных файла со старой версии на новую"""

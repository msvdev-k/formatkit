import sqlite3
from contextlib import closing, AbstractContextManager
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from sqlite3 import Connection
from types import TracebackType
from typing import Callable, Protocol, runtime_checkable

from .exceptions import (FileManagerError, FileAlreadyOpenError, FileNotOpenError, FileCorruptedError, FileMissingError,
                         FileLockedError, ReadOnlyFileError, FileAlreadyExistsError)


class OpenMode(Enum):
    """Режим открытия файла"""
    READ_ONLY = auto()
    READ_WRITE = auto()


def make_backup_file(project_path: Path | str,
                     backup_dir: Path | str | None = None,
                     progress: Callable[[int, int, int], None] | None = None) -> Path:
    """Создаёт резервную копию указанного файла проекта.

    Функция возврата принимает три параметра: статус последней итерации,
    оставшееся количество страниц для копирования и общее количество страниц.

    Возвращает путь к созданной резервной копии файла.
    """
    source_path = Path(project_path).resolve()
    if not source_path.is_file():
        raise FileMissingError(f"Невозможно сделать копию: файл не найден {source_path}")

    # Если директория не задана, сохраняем рядом с проектом
    if backup_dir is None:
        dest_dir = source_path.parent
    else:
        dest_dir = Path(backup_dir).resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)

    # Формируем имя файла с временной меткой (безопасно для Windows и Linux)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup_filename = f"{source_path.stem}_{timestamp}{source_path.suffix}.bak"
    backup_path = dest_dir.joinpath(backup_filename)

    try:
        # Безопасное Online-копирование через SQLite
        # Исходный файл открывается в режиме Read-Only (чтобы не мешать другим процессам)
        # и его слепок атомарно выгружается в новый файл
        src_uri = f"{source_path.as_uri()}?mode=ro"

        with closing(sqlite3.connect(src_uri, uri=True)) as src_conn:
            with closing(sqlite3.connect(backup_path)) as dst_conn:
                # Оптимизация источника
                src_conn.execute("PRAGMA cache_size = -524288;")  # 512 МБ

                # Оптимизации для приёмника
                dst_conn.execute("PRAGMA synchronous = OFF;")
                dst_conn.execute("PRAGMA journal_mode = MEMORY;")
                dst_conn.execute("PRAGMA cache_size = -524288;")  # 512 МБ
                dst_conn.execute("PRAGMA temp_store = MEMORY;")

                # Параметр pages=500 означает, что за один шаг копируется 500 страниц (2 МБ).
                # Между шагами SQLite отдает управление Python и вызывает функцию progress
                src_conn.backup(dst_conn, pages=500, progress=progress)

        return backup_path
    except (sqlite3.Error, OSError) as e:
        # Удаляем файл при сбое диска или SQLite
        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            pass

        raise FileManagerError(f"Ошибка при создании резервной копии проекта: {e}")


class FileConnectionContext(AbstractContextManager[Connection]):
    """Контекстный менеджер соединения SQLite с ручным управлением транзакциями."""

    def __init__(self, uri: str, is_write_mode: bool) -> None:
        self._uri = uri
        self._is_write_mode = is_write_mode
        self._conn: Connection | None = None

    def __enter__(self) -> Connection:
        """Формирование соединения с БД"""
        if self._conn is not None:
            raise RuntimeError("Соединение уже открыто. Контекстный менеджер не поддерживает повторный вход.")

        try:
            # Подключение к БД
            self._conn = sqlite3.connect(self._uri, timeout=10, autocommit=True, uri=True)

            # Настройки производительности и целостности
            self._conn.execute("PRAGMA cache_size = -24576;")  # 24 МБ
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.execute("PRAGMA trusted_schema = OFF;")

            # Настройка режима записи или чтения
            if self._is_write_mode:
                self._conn.execute("PRAGMA journal_mode = WAL;")
                self._conn.execute("BEGIN IMMEDIATE;")
            else:
                self._conn.execute("PRAGMA query_only = ON;")

            return self._conn

        except sqlite3.OperationalError:
            self._rollback_safely()
            self._close_safely()
            raise FileLockedError("Файл заблокирован")

        except Exception:
            # ВАЖНО: если ошибка произошла внутри __enter__, метод __exit__ вызван НЕ будет!
            # Поэтому закрываем соединение в ручную перед пробросом исключения.
            self._rollback_safely()
            self._close_safely()
            raise

    def __exit__(self,
                 exc_type: type[BaseException] | None,
                 exc_val: BaseException | None,
                 exc_tb: TracebackType | None) -> None:
        """Закрытие соединения и контекста"""
        if self._conn is None: return

        try:
            if exc_type is None:
                # Блок with завершился успешно
                if self._is_write_mode:
                    try:
                        self._conn.execute("COMMIT;")
                    except sqlite3.Error:
                        self._rollback_safely()
                        raise
            else:
                # Внутри блока with возникла ошибка
                self._rollback_safely()
        finally:
            # Гарантированное закрытие ресурса
            self._close_safely()

    def _rollback_safely(self) -> None:
        if self._conn is not None and self._is_write_mode and self._conn.in_transaction:
            try:
                self._conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass

    def _close_safely(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            finally:
                self._conn = None


@runtime_checkable
class FileConnectionManager(Protocol):

    @property
    def is_open(self) -> bool:
        """True - файл открыт"""

    @property
    def is_read_only(self) -> bool:
        """True - файл доступен только для чтения"""

    def connection(self, write_mode: bool = False) -> FileConnectionContext:
        """
        Контекстный менеджер с ручным управлением транзакциями (Autocommit).
        Предотвращает неявные скрытые транзакции от Python.

        Флаг write_mode=False позволяет создать соединение только для чтения в режиме редактирования файла.
        В режиме только для чтения данный параметр не используется
        """


class FileManager:

    def __init__(self):
        self._project_path: Path | None = None
        self._project_uri: str | None = None
        self._read_only_project_uri: str | None = None
        self._open_mode: OpenMode = OpenMode.READ_ONLY

    @property
    def is_open(self) -> bool:
        """True - файл открыт"""
        return self._project_path is not None

    @property
    def mode(self) -> OpenMode:
        return self._open_mode

    @property
    def is_read_only(self) -> bool:
        return self._open_mode == OpenMode.READ_ONLY

    def new_file(self, project_path: Path | str):
        """Атомарно создаёт новый пустой файл проекта и открывает его на запись."""
        if not project_path:
            raise ValueError("project_path не может быть пустым")
        resolved_path: Path = Path(project_path).resolve()

        # 1. Проверяем, не открыто ли уже что-то
        if self.is_open:
            raise FileAlreadyOpenError("Перед созданием нового файла необходимо закрыть старый")

        # 2. Если файл уже существует, это ошибка перезаписи (защита данных пользователя)
        if resolved_path.exists():
            raise FileAlreadyExistsError(f"Файл уже существует: {resolved_path}")

        # 3. Гарантируем, что директория существует (актуально для Windows/Linux)
        resolved_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Атомарное создание пустого файла с флагом эксклюзивности "x" (O_CREAT | O_EXCL)
            # Это защищает от состояния гонки, если другой процесс создал файл миллисекунду назад
            with resolved_path.open(mode="x") as _:
                pass  # Просто создаём пустой файл
        except FileExistsError:
            raise FileAlreadyExistsError(f"Файл был создан другим процессом: {resolved_path}")
        except OSError as e:
            raise FileManagerError(f"Не удалось создать файл проекта: {e}")

        # 4. Передаем управление в open_file. Он захватит lock и проинициализирует БД
        try:
            self.open_file(resolved_path, open_mode=OpenMode.READ_WRITE)
        except Exception:
            # Если open_file упал (например, не удалось захватить lock сразу после создания),
            # удаляем пустой файл, чтобы не мусорить на диске
            if resolved_path.is_file() and resolved_path.stat().st_size == 0:
                try:
                    resolved_path.unlink()
                except OSError:
                    pass
            raise

    def open_file(self, project_path: Path | str, open_mode: OpenMode = OpenMode.READ_ONLY):
        """Открывает файл проекта"""
        # 1. Проверка пути к файлу и текущего состояния проекта
        if not project_path:
            raise ValueError("project_path не может быть пустым")

        resolved_path: Path = Path(project_path).resolve()
        read_only_project_uri = f"{resolved_path.as_uri()}?mode=ro"

        if self.is_open:
            if self._project_path == resolved_path and self._open_mode == open_mode:
                return
            else:
                raise FileAlreadyOpenError("Перед открытием нового файла необходимо закрыть старый")

        if not resolved_path.is_file():
            raise FileMissingError(f"Файл не найден: {resolved_path}")

        # 2. Проверка целостности БД
        try:
            with closing(sqlite3.connect(read_only_project_uri, uri=True)) as conn:
                # Увеличиваем кэш и переносим временные файлы в RAM
                conn.execute("PRAGMA cache_size = -24576;")  # 24 МБ
                conn.execute("PRAGMA temp_store = MEMORY;")
                # Быстрая проверка до первой ошибки
                cursor = conn.cursor()
                cursor.execute("PRAGMA quick_check(1);")
                result = cursor.fetchone()
                if result is None or result[0] != "ok":
                    raise FileCorruptedError("Файл повреждён или имеет неверный формат")

        except sqlite3.OperationalError:
            raise FileLockedError("Файл заблокирован")

        except sqlite3.Error as e:
            raise FileCorruptedError("Файл повреждён или имеет неверный формат") from e

        # Считаем файл открытым (для чтения или записи)
        self._project_path = resolved_path
        self._project_uri = f"{resolved_path.as_uri()}?mode=rw"
        self._read_only_project_uri = read_only_project_uri
        self._open_mode = open_mode

    def close_file(self):
        """Освобождает проект"""
        self._project_path = None
        self._project_uri = None
        self._read_only_project_uri = None
        self._open_mode: OpenMode = OpenMode.READ_ONLY

    def connection(self, write_mode: bool = False) -> FileConnectionContext:
        """
        Контекстный менеджер с ручным управлением транзакциями (Autocommit).
        Предотвращает неявные скрытые транзакции от Python.

        Флаг write_mode=False позволяет создать соединение только для чтения в режиме редактирования файла.
        В режиме только для чтения данный параметр не используется
        """
        if not self.is_open:
            raise FileNotOpenError("Необходимо открыть проект")

        if write_mode and self._open_mode == OpenMode.READ_ONLY:
            raise ReadOnlyFileError("Файл открыт только для чтения")

        is_write_mode = (self._open_mode == OpenMode.READ_WRITE and write_mode)

        return FileConnectionContext(
            uri=self._project_uri if is_write_mode else self._read_only_project_uri,
            is_write_mode=is_write_mode
        )

    def copy_file(self, dist: Path | str, progress: Callable[[int, int, int], None] | None = None) -> None:
        """Создаёт копию файла проекта.

        dist - экспортируемый путь

        Функция возврата принимает три параметра: статус последней итерации,
        оставшееся количество страниц для копирования и общее количество страниц
        """
        if not self.is_open:
            raise FileNotOpenError("Необходимо открыть проект")

        dist = Path(dist).resolve()
        if dist.exists():
            raise FileAlreadyExistsError(f"Файл по указанному пути существует: {dist}")

        # Гарантируем, что директория существует (актуально для Windows/Linux)
        dist.parent.mkdir(parents=True, exist_ok=True)

        # Переменная для контроля успешности операции
        success = False
        try:
            with self.connection() as src_conn:
                with closing(sqlite3.connect(dist)) as dst_conn:
                    # Оптимизации для приёмника
                    dst_conn.execute("PRAGMA synchronous = OFF;")
                    dst_conn.execute("PRAGMA journal_mode = MEMORY;")
                    dst_conn.execute("PRAGMA cache_size = -524288;")  # 512 МБ
                    dst_conn.execute("PRAGMA temp_store = MEMORY;")

                    # Параметр pages=500 означает, что за один шаг копируется 500 страниц (2 МБ).
                    # Между шагами SQLite отдает управление Python и вызывает функцию progress
                    src_conn.backup(dst_conn, pages=500, progress=progress)

            success = True

        except (sqlite3.Error, OSError) as e:
            raise FileManagerError(f"Ошибка при создании копии проекта: {e}")

        finally:
            if not success:
                # Удаляем файл при сбое диска или SQLite
                try:
                    dist.unlink(missing_ok=True)
                except OSError:
                    pass

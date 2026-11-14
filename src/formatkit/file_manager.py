"""Управление жизненным циклом файлов-контейнеров и соединениями с базой данных.

Модуль реализует низкоуровневый слой физического хранения фреймворка `formatkit`
на базе встраиваемого движка SQLite. Он управляет деталями работы с файловой
системой, URI-параметрами доступа, блокировками конкурентного доступа, а также
обеспечивает безопасное ручное управление транзакциями (без неявных транзакций Python)
и атомарное горячее резервное копирование (Online Backup).

Основные компоненты:
    - `OpenMode`: перечисление режимов доступа к файлу (`READ_ONLY`, `READ_WRITE`).
    - `FileConnectionContext`: контекстный менеджер одиночного соединения с БД.
    - `FileConnectionManager`: протокол (интерфейс) для выдачи соединений верхним слоям.
    - `FileManager`: основной класс управления открытием, созданием и копированием файлов.
    - `make_backup_file`: утилита для создания резервных копий закрытых или открытых файлов.

Notes:
    Требования к окружению:
        * Python 3.12 и выше — используется параметр `autocommit` функции
          `sqlite3.connect`, добавленный в версии 3.12.
        * SQLite 3.6.11 и выше — используется Online Backup API
          (`sqlite3.Connection.backup`).
        * Файловая система с поддержкой разделяемых блокировок. Режим WAL
          неработоспособен на сетевых файловых системах (SMB, NFS) и на
          носителях, доступных только для чтения.

    Модель транзакций:
        Соединения создаются с `autocommit=True`, поэтому драйвер не открывает
        неявных транзакций. В режиме записи транзакция открывается явно
        (`BEGIN IMMEDIATE`) на входе в контекст и завершается на выходе:
        `COMMIT` при штатном завершении блока либо `ROLLBACK` при любом
        исключении.

    Модель владения ресурсами:
        `FileManager` хранит только путь и URI файла и не удерживает постоянных
        дескрипторов. Физическое соединение открывается на входе в блок `with`
        и закрывается на выходе из него. Один экземпляр `FileManager`
        обслуживает не более одного открытого файла.

    Потокобезопасность:
        Классы модуля не синхронизированы и рассчитаны на использование в одном
        потоке. Объект `sqlite3.Connection` по умолчанию привязан к создавшему
        его потоку (`check_same_thread=True`).

Examples:
    Создание файла, запись данных и экспорт копии::

        manager = FileManager()
        manager.new_file("project.fk")

        with manager.connection(write_mode=True) as conn:
            conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO item (id) VALUES (?)", (1,))
        # COMMIT выполнен, соединение закрыто

        manager.copy_file("export/project_copy.fk")
        manager.close_file()
"""

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
    """Режим доступа к открытому файлу проекта.

    Определяет, какие соединения вправе выдавать `FileManager` и какой
    URI-параметр доступа применяется при подключении к SQLite. Значения
    порождаются функцией `auto()` и не имеют стабильного внешнего
    представления: сохранять их в файле или передавать по сети не следует.

    Attributes:
        READ_ONLY: Файл доступен только для чтения. Соединения открываются
            с URI-параметром `mode=ro` и настройкой `PRAGMA query_only = ON`;
            любые изменяющие запросы запрещены. Режим применяется по умолчанию
            и является также значением состояния «файл закрыт».
        READ_WRITE: Файл доступен для чтения и записи. Записывающие соединения
            открываются с `mode=rw`, переводятся в режим WAL (Write-Ahead Logging)
            и немедленно захватывают блокировку записи (`BEGIN IMMEDIATE`).
            Чтение в этом режиме возможно через отдельное соединение
            с `write_mode=False`.
    """
    READ_ONLY = auto()
    READ_WRITE = auto()


def make_backup_file(project_path: Path | str,
                     backup_dir: Path | str | None = None,
                     progress: Callable[[int, int, int], None] | None = None) -> Path:
    """Создаёт резервную копию файла проекта средствами Online Backup API.

    Копия формируется из согласованного снимка базы данных: источник
    открывается в режиме «только чтение», поэтому функция применима как к
    закрытому файлу, так и к файлу, который в этот момент изменяется другим
    процессом. Запись в источник не блокируется на всё время копирования.

    Имя копии строится по шаблону ``{stem}_{YYYYMMDDTHHMMSSffffff}{suffix}.bak``,
    где метка времени формируется из локального времени с точностью до микросекунд,
    что практически исключает коллизии имён при последовательных вызовах.

    Args:
        project_path: Путь к файлу проекта. Приводится к абсолютному виду
            методом `Path.resolve()`. Файл должен существовать и быть обычным
            файлом (не каталогом и не символьной ссылкой на каталог).
        backup_dir: Каталог размещения копии. При значении `None` копия
            сохраняется рядом с исходным файлом. Несуществующий каталог
            создаётся вместе с родительскими (`parents=True, exist_ok=True`).
        progress: Функция обратного вызова вида `progress(status, remaining, total)`,
            где `status` — код результата последней итерации копирования,
            `remaining` — количество ещё не скопированных страниц, `total` —
            общее количество страниц источника. Вызывается в том же потоке
            после каждого пакета страниц. Исключение, вызываемое функцией,
            прерывает копирование и приводит к удалению незавершённой копии.

    Returns:
        Абсолютный путь к созданному файлу резервной копии.

    Raises:
        FileMissingError: Исходный файл либо не существует, либо не является файлом.
        FileManagerError: Ошибка SQLite или файловой системы в ходе копирования.
            Частично созданная копия при этом удаляется.

    Notes:
        Копирование выполняется пакетами по 500 страниц (около 2 МиБ при
        размере страницы 4 КиБ). Между пакетами SQLite возвращает управление
        интерпретатору, что позволяет обновлять индикатор прогресса и
        обрабатывать события интерфейса.

        Для приёмника намеренно снижены гарантии долговечности
        (`synchronous = OFF`, `journal_mode = MEMORY`), а кэш обоих соединений
        увеличен до 512 МиБ (`cache_size = -524288`; отрицательное значение
        задаётся в КиБ). Это допустимо, поскольку копия является расходным
        артефактом: при сбое она удаляется и создаётся заново.

        Проверка целостности источника (`PRAGMA quick_check`) не выполняется:
        повреждённая база копируется «как есть». Для проверки перед копированием
        следует использовать `FileManager.open_file`.

        Ошибки создания каталога `backup_dir` возникают до входа в защищённый
        блок и пробрасываются как `OSError`, не оборачиваясь в `FileManagerError`.

    Warning:
        При аварийном завершении процесса (сигнал `SIGKILL`, отключение питания)
        частично записанный файл копии остаётся на диске и может быть непригоден
        к использованию. Гарантируется отсутствие мусора только при обработке
        исключений, а не при аварийном останове.

    Examples:
        Копирование рядом с проектом::

            backup_path = make_backup_file("project.fk")

        Копирование в отдельный каталог с индикатором прогресса::

            def on_progress(status: int, remaining: int, total: int) -> None:
                print(f"{100 * (total - remaining) // total}%")

            backup_path = make_backup_file(
                "project.fk", backup_dir="backups", progress=on_progress
            )
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

        raise FileManagerError(f"Ошибка при создании резервной копии проекта") from e


class FileConnectionContext(AbstractContextManager[Connection]):
    """Контекстный менеджер одиночного соединения SQLite с ручным управлением транзакциями.

    Объект управляет открытием соединения, настройкой PRAGMA, захватом
    блокировки записи и гарантированным освобождением ресурса. Соединение
    создаётся в `__enter__` и закрывается в `__exit__`, поэтому вне блока
    `with` объект не удерживает системных ресурсов.

    Экземпляр одноразовый: повторное использование объект запрещено. Для каждой
    операции следует получать новый контекст методом `FileManager.connection()`.

    Attributes:
        _uri: URI подключения к SQLite, включающий параметр доступа
            (`mode=ro` или `mode=rw`). Передаётся в `sqlite3.connect` с `uri=True`.
        _is_write_mode: Признак записывающего соединения. Определяет, будет ли
            открыта явная транзакция и выполнен `COMMIT` на выходе из блока.
        _conn: Активное соединение либо `None`, если контекст ещё не открыт
            или уже закрыт. Служит также признаком повторного входа.

    Notes:
        Соединение создаётся с `timeout=10` (ожидание снятия блокировки до
        10 секунд) и `autocommit=True`, что исключает неявные транзакции драйвера.

        Общие настройки: `cache_size = -24576` (24 МиБ), `foreign_keys = ON`
        (включение проверки внешних ключей), `trusted_schema = OFF` (снижение
        риска исполнения недоверенного кода из схемы стороннего файла БД).

        В режиме записи дополнительно устанавливается `journal_mode = WAL`
        и выполняется `BEGIN IMMEDIATE`: блокировка записи захватывается сразу,
        а весь блок `with` образует одну транзакцию. В режиме чтения
        устанавливается `query_only = ON`.

    Warning:
        Режим WAL является свойством самого файла базы данных, а не соединения:
        он сохраняется после закрытия и требует прав на создание служебных
        файлов `-wal` и `-shm` в каталоге базы данных.

    Examples:
        Запись с автоматической фиксацией транзакции::

            with manager.connection(write_mode=True) as conn:
                conn.execute("INSERT INTO item (id) VALUES (?)", (1,))
            # COMMIT выполнен, соединение закрыто

        Чтение без блокировки записи::

            with manager.connection() as conn:
                rows = conn.execute("SELECT id FROM item").fetchall()
    """

    def __init__(self, uri: str, is_write_mode: bool) -> None:
        """Инициализирует параметры будущего соединения без его открытия.

        Args:
            uri: URI подключения к SQLite вида `file:///path?mode=rw` или
                `file:///path?mode=ro`. Строка должна содержать параметр `mode`,
                задающий права доступа на уровне SQLite.
            is_write_mode: Если `True`, соединение открывается для записи:
                включается WAL и открывается транзакция `BEGIN IMMEDIATE`.
                Если `False`, соединение открывается только для чтения
                (`query_only = ON`) независимо от режима файла.
        """
        self._uri = uri
        self._is_write_mode = is_write_mode
        self._conn: Connection | None = None

    def __enter__(self) -> Connection:
        """Открывает соединение, настраивает PRAGMA и начинает транзакцию.

        Returns:
            Готовое к работе соединение `sqlite3.Connection` в режиме
            autocommit. В записывающем режиме транзакция уже открыта,
            поэтому явные `BEGIN` внутри блока `with` не требуются.

        Raises:
            RuntimeError: Выполняется повторный вход в уже открытый контекст.
            FileLockedError: Получено `sqlite3.OperationalError` — файл занят
                другим соединением и блокировка не была снята за 10 секунд,
                либо запись невозможна по иной причине, отображаемой движком
                в этот класс ошибок.
            sqlite3.Error: Прочие ошибки драйвера, не относящиеся к
                `OperationalError`; пробрасываются без преобразования.

        Notes:
            При любой ошибке соединение откатывается и закрывается до
            проброса исключения: метод `__exit__` в случае неуспешного
            `__enter__` не вызывается, поэтому освобождение ресурса
            выполняется явно.

        Warning:
            `sqlite3.OperationalError` охватывает не только блокировки, но и
            часть ошибок доступа и ввода-вывода; все они отображаются в
            `FileLockedError`. Диагностику причины следует выполнять по
            исходному тексту ошибки в цепочке исключений.
        """
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

        except sqlite3.OperationalError as e:
            self._rollback_safely()
            self._close_safely()
            raise FileLockedError("Файл заблокирован") from e

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
        """Завершает транзакцию и гарантированно закрывает соединение.

        Args:
            exc_type: Класс исключения, возникшего в блоке `with`, либо `None`
                при штатном завершении.
            exc_val: Экземпляр исключения либо `None`.
            exc_tb: Объект трассировки либо `None`.

        Returns:
            `None` — исключения не подавляются и всегда распространяются
            вызывающему коду.

        Raises:
            sqlite3.Error: Ошибка фиксации `COMMIT`. Перед пробросом
                выполняется попытка отката транзакции.

        Notes:
            Логика завершения: при `exc_type is None` и режиме записи
            выполняется `COMMIT`; при любом исключении выполняется `ROLLBACK`.
            Закрытие соединения производится в блоке `finally` при любом
            исходе.

            При соединении только для чтения транзакционные операции не
            выполняются — контекст лишь закрывает соединение.

            Повторный вызов на закрытом контексте безопасен и является
            холостой операцией. Использовать объект `Connection`, полученный
            в `__enter__`, после выхода из блока `with` недопустимо.
        """
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
        """Откатывает активную транзакцию, подавляя ошибки отката.

        Notes:
            Операция выполняется только для соединения открытого на запись
            при наличии открытой транзакции (`Connection.in_transaction`).
            Ошибки `sqlite3.Error` намеренно игнорируются: метод вызывается
            на пути обработки другого исключения и не должен маскировать
            его первопричину. Метод идемпотентен.
        """
        if self._conn is not None and self._is_write_mode and self._conn.in_transaction:
            try:
                self._conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass

    def _close_safely(self) -> None:
        """Закрывает соединение и сбрасывает ссылку на него.

        Notes:
            Ошибки `sqlite3.Error` при закрытии подавляются; ссылка `_conn`
            обнуляется в блоке `finally` независимо от исхода, что делает
            метод идемпотентным. Незафиксированная транзакция откатывается
            самим движком SQLite при закрытии соединения.
        """
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            finally:
                self._conn = None


@runtime_checkable
class FileConnectionManager(Protocol):
    """Протокол источника соединений с файлом проекта.

    Определяет минимальный контракт, на который опираются верхние слои
    фреймворка: сведения о состоянии файла и фабрику контекстов соединений.
    Структурная типизация позволяет подставлять любую реализацию без
    явного наследования; штатная реализация — `FileManager`.

    Notes:
        Протокол помечен `@runtime_checkable`, поэтому допустима проверка
        `isinstance(obj, FileConnectionManager)`. Такая проверка устанавливает
        лишь наличие требуемых членов, но не соответствие их сигнатур и
        семантики. Вызов `issubclass()` для протокола с членами-свойствами
        приводит к `TypeError`.

        Тела членов протокола не содержат реализации: при явном наследовании
        все они должны быть переопределены.
    """

    @property
    def is_open(self) -> bool:
        """Признак того, что файл открыт и готов к выдаче соединений.

        Returns:
            `True`, если файл открыт в любом режиме; `False`, если файл закрыт.
        """

    @property
    def is_read_only(self) -> bool:
        """Признак доступности файла исключительно для чтения.

        Returns:
            `True`, если файл открыт в режиме `OpenMode.READ_ONLY` либо закрыт;
            `False` для файла, открытого на запись.
        """

    def connection(self, write_mode: bool = False) -> FileConnectionContext:
        """Возвращает контекст соединения с ручным управлением транзакциями.

        Соединение работает в режиме autocommit, что предотвращает появление
        неявных транзакций драйвера `sqlite3`. Границы транзакции совпадают
        с границами блока `with`.

        Args:
            write_mode: Запрос записывающего соединения. При `False` выдаётся
                соединение только для чтения — в том числе для файла,
                открытого на запись, что позволяет читать данные, не захватывая
                блокировку записи. Для файла, открытого в режиме
                `OpenMode.READ_ONLY`, значение `True` является ошибкой.

        Returns:
            Новый одноразовый контекст соединения. Подключение к базе
            выполняется при входе в блок `with`, а не в момент вызова метода.

        Raises:
            FileNotOpenError: Файл не открыт.
            ReadOnlyFileError: Запрошена запись для файла, открытого
                в режиме только для чтения.
        """


class FileManager:
    """Менеджер жизненного цикла файла проекта.

    Класс ведёт логическое состояние одного открытого файла (путь, URI
    доступа, режим) и выдаёт соединения через контекстные менеджеры.
    Постоянные дескрипторы файла не удерживаются: подключение открывается
    на входе в блок `with` и закрывается на выходе.

    Attributes:
        _project_path: Абсолютный путь к открытому файлу либо `None`, если
            файл закрыт. Служит единственным признаком состояния «открыт».
        _project_uri: URI доступа на чтение и запись (`?mode=rw`) либо `None`.
        _read_only_project_uri: URI доступа только для чтения (`?mode=ro`)
            либо `None`.
        _open_mode: Текущий режим доступа. В закрытом состоянии равен
            `OpenMode.READ_ONLY`.

    Notes:
        Переходы состояний: `new_file()` и `open_file()` переводят объект
        в состояние «открыт», `close_file()` — в состояние «закрыт». Повторное
        открытие того же файла в том же режиме является холостой операцией;
        открытие другого файла или того же файла в ином режиме требует
        предварительного закрытия.

        Класс не реализует протокол контекстного менеджера и не имеет
        финализатора: вызов `close_file()` остаётся обязанностью владельца.

        Экземпляр не является потокобезопасным: изменение состояния не
        синхронизировано. Для параллельной работы следует использовать
        отдельный экземпляр на поток.

    Examples:
        Полный цикл работы с файлом::

            manager = FileManager()
            manager.open_file("project.fk", OpenMode.READ_WRITE)
            try:
                with manager.connection(write_mode=True) as conn:
                    conn.execute("DELETE FROM item WHERE id = ?", (1,))
            finally:
                manager.close_file()
    """

    def __init__(self) -> None:
        """Создаёт менеджер. Исходное состояние: «файл закрыт».

        Notes:
            Обращений к файловой системе не выполняется. Режим доступа
            инициализируется значением `OpenMode.READ_ONLY`, поэтому
            свойства `is_read_only` и `mode` до открытия файла возвращают
            значения по умолчанию и содержательны только при `is_open == True`.
        """
        self._project_path: Path | None = None
        self._project_uri: str | None = None
        self._read_only_project_uri: str | None = None
        self._open_mode: OpenMode = OpenMode.READ_ONLY

    @property
    def is_open(self) -> bool:
        """Признак того, что файл открыт логически.

        Returns:
            `True`, если путь к файлу сохранён, иначе `False`.

        Notes:
            Значение `True` не означает наличия активного соединения или
            удерживаемой блокировки: соединения создаются только методом
            `connection()` и живут в пределах своего блока `with`.
        """
        return self._project_path is not None

    @property
    def mode(self) -> OpenMode:
        """Текущий режим доступа к файлу.

        Returns:
            Режим, заданный при открытии файла. Если файл закрыт, возвращается
            `OpenMode.READ_ONLY` как значение по умолчанию.
        """
        return self._open_mode

    @property
    def is_read_only(self) -> bool:
        """Признак доступности файла только для чтения.

        Returns:
            `True` для режима `OpenMode.READ_ONLY`, включая закрытое состояние;
            `False` для режима `OpenMode.READ_WRITE`.
        """
        return self._open_mode == OpenMode.READ_ONLY

    def new_file(self, project_path: Path | str) -> None:
        """Создаёт новый пустой файл проекта и открывает его на запись.

        Файл создаётся с флагом эксклюзивности (`open(mode="x")`, то есть
        `O_CREAT | O_EXCL`), что исключает перезапись существующих данных
        и состояние гонки с другим процессом. После создания управление
        передаётся методу `open_file()` в режиме `OpenMode.READ_WRITE`.

        Args:
            project_path: Путь к создаваемому файлу. Приводится к абсолютному
                виду. Родительские каталоги создаются автоматически.
                Файл по этому пути не должен существовать.

        Returns:
            `None`. Результат выражается в изменении состояния объекта:
            `is_open` становится `True`, `mode` — `OpenMode.READ_WRITE`.

        Raises:
            ValueError: Передана пустая строка в качестве пути.
            FileAlreadyOpenError: Менеджер уже обслуживает другой открытый файл.
            FileAlreadyExistsError: Файл существует либо был создан другим
                процессом между проверкой и созданием.
            FileManagerError: Ошибка файловой системы при создании файла
                (недостаточно прав, недопустимый путь, переполнение диска).
            FileLockedError: Файл не удалось открыть сразу после создания.
            FileCorruptedError: Созданный файл не прошёл проверку целостности.

        Notes:
            Если открытие завершается неудачей, созданный файл удаляется,
            но только при условии, что его размер всё ещё равен нулю.
            Файл, в который SQLite успел записать данные, остаётся на диске.

        Warning:
            Атомарность гарантируется для операции создания файла, а не для
            всей последовательности «создание — инициализация — открытие».
            В интервале между этими шагами сторонний процесс может наблюдать
            пустой файл по указанному пути.
        """
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

    def open_file(self, project_path: Path | str, open_mode: OpenMode = OpenMode.READ_ONLY) -> None:
        """Открывает существующий файл проекта после проверки его целостности.

        Файл подключается во временном соединении только для чтения, где
        выполняется `PRAGMA quick_check(1)`. При успешном прохождении проверки
        сохраняются путь, URI доступа и режим; постоянное соединение не
        создаётся.

        Args:
            project_path: Путь к существующему файлу проекта. Приводится
                к абсолютному виду методом `Path.resolve()`.
            open_mode: Режим доступа. По умолчанию `OpenMode.READ_ONLY`.
                Значение `OpenMode.READ_WRITE` разрешает последующую выдачу
                записывающих соединений.

        Returns:
            `None`. Повторный вызов для того же файла в том же режиме является
            холостой операцией и не выполняет повторной проверки целостности.

        Raises:
            ValueError: Передана пустая строка в качестве пути.
            FileAlreadyOpenError: Уже открыт другой файл либо тот же файл,
                но в ином режиме.
            FileMissingError: Файл не существует или не является обычным файлом.
            FileLockedError: Получено `sqlite3.OperationalError` — файл занят
                другим процессом либо недоступен для чтения.
            FileCorruptedError: Проверка `quick_check` вернула результат,
                отличный от `"ok"`, либо получена иная ошибка `sqlite3.Error`
                (в частности, файл не является базой данных SQLite).

        Notes:
            `PRAGMA quick_check(1)` прекращает проверку после первой ошибки
            и не проверяет ограничения `UNIQUE` и соответствие содержимого
            индексов содержимому таблиц. Для полной проверки требуется
            `PRAGMA integrity_check`.

            Проверка выполняется через URI `?mode=ro`, поэтому наличие файла
            обязательно; для больших баз операция может занимать заметное время.

        Warning:
            Права на запись при открытии в режиме `OpenMode.READ_WRITE`
            не проверяются. Отсутствие прав проявится позднее — при входе
            в записывающий контекст `connection(write_mode=True)` — в виде
            `FileLockedError`.
        """
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

    def close_file(self) -> None:
        """Сбрасывает состояние менеджера и логически освобождает файл.

        Returns:
            `None`. После вызова `is_open` возвращает `False`, `mode` —
            `OpenMode.READ_ONLY`.

        Notes:
            Метод идемпотентен и безопасен для закрытого файла. Исключений
            не порождает, поскольку не выполняет обращений к файловой системе.

        Warning:
            Ранее выданные контексты соединений не затрагиваются: активный
            блок `with` продолжает работать, а его транзакция будет завершена
            по обычным правилам. Вызов `close_file()` внутри такого блока
            не откатывает изменения и не закрывает соединение.
        """
        self._project_path = None
        self._project_uri = None
        self._read_only_project_uri = None
        self._open_mode: OpenMode = OpenMode.READ_ONLY

    def connection(self, write_mode: bool = False) -> FileConnectionContext:
        """Возвращает контекст соединения с ручным управлением транзакциями.

        Соединение работает в режиме autocommit, что предотвращает появление
        неявных скрытых транзакций драйвера `sqlite3`. Границы транзакции
        совпадают с границами блока `with`: `COMMIT` при штатном завершении
        и `ROLLBACK` при исключении.

        Args:
            write_mode: Запрос на соединение в режиме записи. Значение `False`
                позволяет получить соединение только для чтения даже для
                файла, открытого на запись, что делает чтение без захвата
                блокировки записи. Для файла, открытого в режиме
                `OpenMode.READ_ONLY`, запрос записи является ошибкой.

        Returns:
            Новый одноразовый контекст `FileConnectionContext`. Подключение
            к базе выполняется при входе в блок `with`, а не в момент вызова.

        Raises:
            FileNotOpenError: Файл не открыт.
            ReadOnlyFileError: Запрошена запись для файла, открытого только
                для чтения.

        Notes:
            Итоговый режим соединения определяется конъюнкцией режима файла
            и запрошенного флага: запись возможна только при
            `mode == OpenMode.READ_WRITE and write_mode is True`.

            Каждый вызов создаёт независимое соединение, поэтому вложенные
            контексты допустимы для чтения, но не для записи.

        Warning:
            Вложение двух записывающих контекстов приводит к самоблокировке:
            внутренний `BEGIN IMMEDIATE` будет ожидать снятия блокировки,
            удерживаемой внешним соединением, и завершится `FileLockedError`
            по истечении 10 секунд. Все изменения следует выполнять в рамках
            одного записывающего контекста.

        Examples:
            ::

                with manager.connection(write_mode=True) as conn:
                    conn.execute("UPDATE item SET name = ? WHERE id = ?", ("x", 1))
        """
        if not self.is_open:
            raise FileNotOpenError("Необходимо открыть проект")

        if write_mode and self._open_mode == OpenMode.READ_ONLY:
            raise ReadOnlyFileError("Файл открыт только для чтения")

        is_write_mode = (write_mode and self._open_mode == OpenMode.READ_WRITE)

        return FileConnectionContext(
            uri=self._project_uri if is_write_mode else self._read_only_project_uri,
            is_write_mode=is_write_mode
        )

    def copy_file(self, dist: Path | str, progress: Callable[[int, int, int], None] | None = None) -> None:
        """Экспортирует копию открытого файла проекта по указанному пути.

        Копирование выполняется через Online Backup API из соединения открытого
        только для чтения, поэтому формируется согласованный снимок базы данных.
        Закрывать файл на время операции не требуется, а параллельные
        записи не блокируются на всё время копирования.

        Args:
            dist: Путь к создаваемой копии. Приводится к абсолютному виду.
                Файл по этому пути не должен существовать; родительские
                каталоги создаются автоматически.
            progress: Функция обратного вызова вида `progress(status, remaining, total)`,
                где `status` — код результата последней итерации, `remaining` —
                количество ещё не скопированных страниц, `total` — общее
                количество страниц источника. Вызывается после каждого пакета
                страниц в том же потоке.

        Returns:
            `None`. Результатом операции является файл по пути `dist`.

        Raises:
            FileNotOpenError: Файл проекта не открыт.
            FileAlreadyExistsError: По целевому пути уже существует файл
                или каталог.
            FileManagerError: Ошибка SQLite или файловой системы в ходе
                копирования; частично созданная копия удаляется.

        Notes:
            Копирование выполняется пакетами по 500 страниц (около 2 МиБ при
            размере страницы 4 КиБ). Для приёмника снижены гарантии
            долговечности (`synchronous = OFF`, `journal_mode = MEMORY`), кэш
            увеличен до 512 МиБ — копия считается расходным артефактом.

            Снимок не включает незафиксированные изменения других соединений:
            в копию попадают только данные, зафиксированные на момент начала
            копирования.

            Ошибки создания родительских каталогов возникают до входа
            в защищённый блок и пробрасываются как `OSError`, не оборачиваясь
            в `FileManagerError`.

        Warning:
            Целевой файл не должен совпадать с файлом проекта: попытка
            копирования «самого в себя» приведёт к `FileAlreadyExistsError`.
            При аварийном завершении процесса незавершённая копия удаляется.
        """
        if not self.is_open:
            raise FileNotOpenError("Необходимо открыть проект")

        dist_resolved = Path(dist).resolve()
        if dist_resolved.exists():
            raise FileAlreadyExistsError(f"Файл по указанному пути существует: {dist_resolved}")

        # Гарантируем, что директория существует (актуально для Windows/Linux)
        dist_resolved.parent.mkdir(parents=True, exist_ok=True)

        # Переменная для контроля успешности операции
        success = False
        try:
            with self.connection() as src_conn:
                with closing(sqlite3.connect(dist_resolved)) as dst_conn:
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
            raise FileManagerError(f"Ошибка при создании копии проекта:") from e

        finally:
            if not success:
                # Удаляем файл при сбое диска или SQLite
                try:
                    dist_resolved.unlink(missing_ok=True)
                except OSError:
                    pass

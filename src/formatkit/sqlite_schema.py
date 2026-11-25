from contextlib import closing
from dataclasses import dataclass, field
from enum import StrEnum, IntEnum
from sqlite3 import Connection, Row
from types import MappingProxyType
from typing import Self

from .exceptions import DataTypeError


class DataClass(StrEnum):
    """Перечисление базовых классов данных SQLite"""

    # Знаковые целые числа, занимающие 0, 1, 2, 3, 4, 6 или 8 байт в зависимости от значения
    INTEGER = "INTEGER"
    # 8-байтовые числа с плавающей запятой по стандарту IEEE
    REAL = "REAL"
    # Текстовая строка, хранящуюся в кодировке базы данных (UTF-8, UTF-16BE или UTF-16LE)
    TEXT = "TEXT"
    # Блок данных, хранящийся в точно таком же виде, в каком был введен
    BLOB = "BLOB"

    @classmethod
    def type_affinity(cls, raw_type: str) -> Self:
        """Определяет класс данных согласно правилам Type Affinity из документации SQLite."""

        match raw_type.strip().upper():
            case s if "INT" in s:
                return cls.INTEGER
            case s if any(sub in s for sub in ("CHAR", "CLOB", "TEXT")):
                return cls.TEXT
            case s if not s or "BLOB" in s:
                return cls.BLOB
            case s if any(sub in s for sub in ("REAL", "FLOA", "DOUB")):
                return cls.REAL
            case _:
                raise DataTypeError(f"Неподдерживаемый тип данных (требуется NUMERIC): {raw_type!r}")


class ColumnType(IntEnum):
    """Тип колонки в таблице"""
    NORMAL = 0
    HIDDEN = 1
    DYNAMIC = 2
    STORED = 3


@dataclass(frozen=True, slots=True, kw_only=True)
class Column:
    name: str
    data_type: DataClass
    not_null: bool = True
    default_value: str | None = None
    primary_key: int = 0
    hidden: ColumnType = ColumnType.NORMAL

    @property
    def is_primary_key(self) -> bool:
        return self.primary_key > 0

    @property
    def is_normal(self) -> bool:
        return self.hidden is ColumnType.NORMAL

    @property
    def is_generated(self) -> bool:
        return self.hidden in (ColumnType.DYNAMIC, ColumnType.STORED)


class ForeignKeyAction(StrEnum):
    """Действия при изменении или удалении родительской записи (ON UPDATE / ON DELETE)"""
    # При изменении или удалении родительского ключа из базы данных никаких специальных действий
    # не предпринимается (значение по умолчанию)
    NO_ACTION = "NO ACTION"
    # Приложению запрещено удалять (ON DELETE) или изменять (ON UPDATE) родительский ключ,
    # если к нему привязан один или несколько дочерних ключей
    RESTRICT = "RESTRICT"
    # При удалении (ON DELETE) или изменении (ON UPDATE) родительского ключа столбцы
    # дочернего ключа будут содержать значения NULL
    SET_NULL = "SET NULL"
    # Аналогично "SET NULL", за исключением того, что столбцы дочернего ключа будут
    # содержать значения по умолчанию
    SET_DEFAULT = "SET DEFAULT"
    # Действие распространяет операцию удаления или обновления родительского ключа на каждый
    # зависимый дочерний ключ. То есть, например, каждая строка в дочерней таблице, связанная
    # с удаленной родительской строкой, также удаляется
    CASCADE = "CASCADE"


@dataclass(frozen=True, slots=True, kw_only=True)
class ForeignKey:
    """Описание внешнего ключа таблицы согласно PRAGMA foreign_key_list."""
    id: int
    seq: int
    from_column: str
    to_table: str
    # В SQLite 'to' может быть None, если указано просто REFERENCES parent_table
    to_column: str | None = None
    on_update: ForeignKeyAction = ForeignKeyAction.CASCADE
    on_delete: ForeignKeyAction = ForeignKeyAction.RESTRICT


class IndexOrigin(StrEnum):
    """Способ создания индекса согласно полю origin из PRAGMA index_list."""
    # Индекс создан вручную явной командой CREATE INDEX
    CREATE_INDEX = "c"
    # Индекс создан автоматически из-за ограничения UNIQUE
    UNIQUE_CONSTRAINT = "u"
    # Индекс создан автоматически для первичного ключа PRIMARY KEY
    PRIMARY_KEY = "pk"


@dataclass(frozen=True, slots=True, kw_only=True)
class IndexColumn:
    """Описание отдельного элемента (колонки или выражения) внутри индекса согласно PRAGMA index_info"""
    # Ранг столбца в индексе. 0 означает крайний левый столбец.
    seq_no: int
    # Ранг столбца в таблице. Значение -1 означает rowid, а значение -2 означает использование выражения
    c_id: int
    # Имя индексируемого столбца. Равно NULL, если столбец является идентификатором строки (rowid) или выражением
    name: str | None

    @property
    def is_expression(self) -> bool:
        """Возвращает True, если элемент индекса является выражением"""
        return self.c_id == -2


@dataclass(frozen=True, slots=True, kw_only=True)
class Index:
    """Описание индекса таблицы согласно PRAGMA index_list и PRAGMA index_info"""
    # Название индекса
    name: str
    # Флаг уникального значения
    is_unique: bool
    # Способ создания индекса
    origin: IndexOrigin
    # Частичный (True) или полный (False) индекс
    is_partial: bool = False
    # Столбцы индекса
    columns: tuple[IndexColumn, ...] = ()


class TableType(StrEnum):
    """Типы табличных объектов согласно полю type из PRAGMA table_list"""
    TABLE = "table"
    VIEW = "view"
    SHADOW = "shadow"
    VIRTUAL = "virtual"


@dataclass(frozen=True, slots=True, kw_only=True)
class Table:
    """Описание таблицы базы данных SQLite"""
    # Схема, в которой отображается таблица или представление (например, "main" или "temp")
    schema: str = "main"
    # Имя таблицы или представления
    name: str
    # Тип объекта
    table_type: TableType = TableType.TABLE
    # Количество столбцов в таблице, включая сгенерированные и скрытые столбцы
    n_cols: int = 0
    # True (1), если таблица создана как WITHOUT ROWID. False (0) - обычная таблица с rowid
    is_without_rowid: bool = False
    # True (1) - к таблице применяются строгие правила типизации. False (0) - гибкие правила типизации
    is_strict: bool = False

    # Неизменяемый словарь колонок (имя колонки -> объект Column)
    columns: MappingProxyType[str, Column] = field(default_factory=lambda: MappingProxyType({}))
    # Внешние ключи
    foreign_keys: tuple[ForeignKey, ...] = ()
    # Индексы
    indexes: tuple[Index, ...] = ()

    @property
    def primary_key_columns(self) -> tuple[Column, ...]:
        """Возвращает кортеж колонок первичного ключа, отсортированный по их порядку в ключе"""
        pk_cols = (col for col in self.columns.values() if col.is_primary_key)
        return tuple(sorted(pk_cols, key=lambda col: col.primary_key))

    @property
    def has_primary_key(self) -> bool:
        """Возвращает True, если в таблице явно объявлен PRIMARY KEY"""
        return any(col.is_primary_key for col in self.columns.values())

    @property
    def is_without_rowid_valid(self) -> bool:
        """Проверяет архитектурное правило SQLite: WITHOUT ROWID требует явного PRIMARY KEY"""
        return not self.is_without_rowid or self.has_primary_key


def get_database_schema(conn: Connection, schema: str = "main", include_system: bool = False, ) -> dict[str, Table]:
    """Читает схему базы данных SQLite через PRAGMA и возвращает словарь объектов Table.

    Args:
        conn: Активное соединение sqlite3.Connection.
        schema: Имя схемы (по умолчанию "main").
        include_system: Включать ли системные таблицы (sqlite_*).
    """
    with closing(conn.cursor()) as cursor:
        cursor.row_factory = lambda cur, row: Row(cur, row)
        tables: dict[str, Table] = {}

        # 1. Фильтруем системные таблицы и схему прямо в SQL
        query_tables = r"""
            SELECT schema, name, type, ncol, wr, strict
            FROM pragma_table_list
            WHERE schema = ? AND (? OR name NOT LIKE 'sqlite\_%' ESCAPE '\');
        """
        cursor.execute(query_tables, (schema, include_system))
        table_rows = cursor.fetchall()

        for t_row in table_rows:
            t_name: str = t_row["name"]

            # 2. Колонки (запрос компилируется один раз и берется из кеша)
            cursor.execute(
                "SELECT name, type, [notnull], dflt_value, pk, hidden FROM pragma_table_xinfo(?, ?);",
                (t_name, schema),
            )
            columns = {
                c_row["name"]: Column(
                    name=c_row["name"],
                    data_type=DataClass.type_affinity(c_row["type"]),
                    not_null=bool(c_row["notnull"]),
                    default_value=c_row["dflt_value"],
                    primary_key=c_row["pk"],
                    hidden=ColumnType(c_row["hidden"]),
                )
                for c_row in cursor.fetchall()
            }

            # 3. Внешние ключи (сразу сортируем в БД)
            cursor.execute(
                """
                SELECT id, seq, [from], [table], [to], on_update, on_delete
                FROM pragma_foreign_key_list(?, ?)
                ORDER BY id, seq;
                """,
                (t_name, schema),
            )
            foreign_keys = tuple(
                ForeignKey(
                    id=fk_row["id"],
                    seq=fk_row["seq"],
                    from_column=fk_row["from"],
                    to_table=fk_row["table"],
                    to_column=fk_row["to"],
                    on_update=ForeignKeyAction(fk_row["on_update"].upper()),
                    on_delete=ForeignKeyAction(fk_row["on_delete"].upper()),
                )
                for fk_row in cursor.fetchall()
            )

            # 4. Индексы и их колонки
            cursor.execute(
                "SELECT name, [unique], origin, partial FROM pragma_index_list(?, ?);",
                (t_name, schema),
            )
            indexes_list: list[Index] = []

            for idx_row in cursor.fetchall():
                idx_name: str = idx_row["name"]

                cursor.execute(
                    """
                    SELECT seqno, cid, name
                    FROM pragma_index_info(?, ?)
                    ORDER BY seqno;
                    """,
                    (idx_name, schema),
                )
                idx_columns = tuple(
                    IndexColumn(
                        seq_no=ic_row["seqno"],
                        c_id=ic_row["cid"],
                        name=ic_row["name"],
                    )
                    for ic_row in cursor.fetchall()
                )

                indexes_list.append(
                    Index(
                        name=idx_name,
                        is_unique=bool(idx_row["unique"]),
                        origin=IndexOrigin(idx_row["origin"]),
                        is_partial=bool(idx_row["partial"]),
                        columns=idx_columns,
                    )
                )

            # 5. Собираем объект Table
            tables[t_name] = Table(
                schema=t_row["schema"],
                name=t_name,
                table_type=TableType(t_row["type"]),
                n_cols=t_row["ncol"],
                is_without_rowid=bool(t_row["wr"]),
                is_strict=bool(t_row["strict"]),
                columns=MappingProxyType(columns),
                foreign_keys=foreign_keys,
                indexes=tuple(indexes_list),
            )

        return tables

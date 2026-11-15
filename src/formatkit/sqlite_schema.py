from dataclasses import dataclass
from enum import StrEnum, IntEnum
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
    NO_ACTION = "NO ACTION"
    RESTRICT = "RESTRICT"
    SET_NULL = "SET NULL"
    SET_DEFAULT = "SET DEFAULT"
    CASCADE = "CASCADE"


@dataclass(frozen=True, slots=True, kw_only=True)
class ForeignKey:
    """Описание внешнего ключа таблицы согласно PRAGMA foreign_key_list."""
    id: int
    seq: int
    from_column: str
    to_table: str
    # В SQLite 'to' может быть None, если в DDL указано просто REFERENCES parent_table
    to_column: str | None
    on_update: ForeignKeyAction = ForeignKeyAction.NO_ACTION
    on_delete: ForeignKeyAction = ForeignKeyAction.NO_ACTION


@dataclass(frozen=True)
class TableSchema:
    name: str
    sql: str
    columns: list[ColumnSchema] = field(default_factory=list)
    foreign_keys: list[ForeignKeySchema] = field(default_factory=list)


def get_database_schema(db_path: str | Path) -> dict[str, TableSchema]:
    """Читает схему таблиц SQLite и возвращает словарь объектов TableSchema."""
    schema: dict[str, TableSchema] = {}

    # Используем менеджер контекста для автоматического закрытия соединения
    with sqlite3.connect(db_path) as conn:
        # Включаем доступ к столбцам по имени
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Получаем список пользовательских таблиц
        cursor.execute(
            """
            SELECT name, sql 
            FROM sqlite_schema 
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%';
            """
        )
        tables = cursor.fetchall()

        for table_row in tables:
            table_name = table_row["name"]
            table_sql = table_row["sql"]

            # Имя таблицы в PRAGMA нельзя передать через стандартный плейсхолдер '?',
            # поэтому экранируем двойные кавычки для защиты от SQL-инъекций.
            safe_table_name = table_name.replace('"', '""')

            # 1. Читаем информацию о колонках
            cursor.execute(f'PRAGMA table_info("{safe_table_name}");')
            columns = [
                ColumnSchema(
                    name=col["name"],
                    data_type=col["type"],
                    not_null=bool(col["notnull"]),
                    default_value=col["dflt_value"],
                    is_primary_key=bool(col["pk"]),
                )
                for col in cursor.fetchall()
            ]

            # 2. Читаем информацию о внешних ключах
            cursor.execute(f'PRAGMA foreign_key_list("{safe_table_name}");')
            foreign_keys = [
                ForeignKeySchema(
                    from_column=fk["from"],
                    to_table=fk["table"],
                    to_column=fk["to"],
                    on_update=fk["on_update"],
                    on_delete=fk["on_delete"],
                )
                for fk in cursor.fetchall()
            ]

            schema[table_name] = TableSchema(
                name=table_name,
                sql=table_sql,
                columns=columns,
                foreign_keys=foreign_keys,
            )

    return schema

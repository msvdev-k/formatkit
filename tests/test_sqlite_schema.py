import sqlite3
import unittest
from types import MappingProxyType

from formatkit.exceptions import DataTypeError
from formatkit.sqlite_schema import (
    Column,
    ColumnType,
    DataClass,
    ForeignKey,
    ForeignKeyAction,
    IndexColumn,
    IndexOrigin,
    Table,
    TableType,
    get_database_schema,
)


class TestDataClass(unittest.TestCase):
    """Тесты определения классов данных (Type Affinity)."""

    def test_type_affinity_valid(self) -> None:
        cases = [
            ("INT", DataClass.INTEGER),
            ("INTEGER", DataClass.INTEGER),
            ("TINYINT", DataClass.INTEGER),
            ("BIGINT", DataClass.INTEGER),
            ("UNSIGNED BIG INT", DataClass.INTEGER),
            ("int2", DataClass.INTEGER),
            ("CHARACTER(20)", DataClass.TEXT),
            ("VARCHAR(255)", DataClass.TEXT),
            ("NCHAR(55)", DataClass.TEXT),
            ("CLOB", DataClass.TEXT),
            ("text", DataClass.TEXT),
            ("", DataClass.BLOB),
            ("   ", DataClass.BLOB),
            ("BLOB", DataClass.BLOB),
            ("blob_data", DataClass.BLOB),
            ("REAL", DataClass.REAL),
            ("DOUBLE", DataClass.REAL),
            ("DOUBLE PRECISION", DataClass.REAL),
            ("FLOAT", DataClass.REAL),
        ]
        for raw_type, expected in cases:
            with self.subTest(raw_type=raw_type):
                self.assertIs(DataClass.type_affinity(raw_type), expected)

    def test_type_affinity_unsupported(self) -> None:
        unsupported_types = ["NUMERIC", "DECIMAL(10,5)", "BOOLEAN", "DATE", "DATETIME", "ANY"]
        for raw_type in unsupported_types:
            with self.subTest(raw_type=raw_type):
                with self.assertRaisesRegex(DataTypeError, "Неподдерживаемый тип данных"):
                    DataClass.type_affinity(raw_type)


class TestEntitiesProperties(unittest.TestCase):
    """Тесты вычисляемых свойств датаклассов."""

    def test_column_properties(self) -> None:
        pk_col = Column(name="id", data_type=DataClass.INTEGER, primary_key=1, hidden=ColumnType.NORMAL)
        self.assertTrue(pk_col.is_primary_key)
        self.assertTrue(pk_col.is_normal)
        self.assertFalse(pk_col.is_generated)

        hidden_col = Column(name="rowid", data_type=DataClass.INTEGER, hidden=ColumnType.HIDDEN)
        self.assertFalse(hidden_col.is_primary_key)
        self.assertFalse(hidden_col.is_normal)
        self.assertFalse(hidden_col.is_generated)

        dynamic_col = Column(name="v_col", data_type=DataClass.TEXT, hidden=ColumnType.DYNAMIC)
        self.assertFalse(dynamic_col.is_normal)
        self.assertTrue(dynamic_col.is_generated)

        stored_col = Column(name="s_col", data_type=DataClass.TEXT, hidden=ColumnType.STORED)
        self.assertFalse(stored_col.is_normal)
        self.assertTrue(stored_col.is_generated)

    def test_index_column_properties(self) -> None:
        regular_col = IndexColumn(seq_no=0, c_id=1, name="email")
        self.assertFalse(regular_col.is_expression)

        rowid_col = IndexColumn(seq_no=0, c_id=-1, name=None)
        self.assertFalse(rowid_col.is_expression)

        expr_col = IndexColumn(seq_no=0, c_id=-2, name=None)
        self.assertTrue(expr_col.is_expression)

    def test_table_properties(self) -> None:
        col_b = Column(name="b", data_type=DataClass.INTEGER, primary_key=2)
        col_a = Column(name="a", data_type=DataClass.TEXT, primary_key=1)
        col_c = Column(name="c", data_type=DataClass.REAL, primary_key=0)

        table_with_pk = Table(
            name="composite",
            is_without_rowid=True,
            columns=MappingProxyType({"b": col_b, "c": col_c, "a": col_a}, )
        )
        self.assertTrue(table_with_pk.has_primary_key)
        self.assertEqual(table_with_pk.primary_key_columns, (col_a, col_b))
        self.assertTrue(table_with_pk.is_without_rowid_valid)

        invalid_without_rowid = Table(
            name="invalid_wr",
            is_without_rowid=True,
            columns=MappingProxyType({"c": col_c}),
        )
        self.assertFalse(invalid_without_rowid.has_primary_key)
        self.assertEqual(invalid_without_rowid.primary_key_columns, ())
        self.assertFalse(invalid_without_rowid.is_without_rowid_valid)

        regular_no_pk = Table(name="regular", is_without_rowid=False, columns=MappingProxyType({"c": col_c}))
        self.assertTrue(regular_no_pk.is_without_rowid_valid)


class TestGetDatabaseSchema(unittest.TestCase):
    """Интеграционные тесты чтения схемы БД через get_database_schema."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys = ON;")

    def tearDown(self) -> None:
        self.conn.close()

    def test_empty_database(self) -> None:
        schema = get_database_schema(self.conn)
        self.assertEqual(schema, {})

    def test_full_features(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE departments (
                dept_id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                budget REAL DEFAULT 0.0
            );

            CREATE TABLE employees (
                emp_id INTEGER NOT NULL,
                dept_id INTEGER,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                avatar BLOB,
                full_name TEXT GENERATED ALWAYS AS (first_name || ' ' || last_name) VIRTUAL,
                initials TEXT GENERATED ALWAYS AS (substr(first_name, 1, 1)) STORED,
                PRIMARY KEY (dept_id, emp_id),
                FOREIGN KEY (dept_id) REFERENCES departments(dept_id)
                    ON UPDATE CASCADE
                    ON DELETE SET NULL
            ) WITHOUT ROWID;

            CREATE TABLE strict_audit (
                id INT PRIMARY KEY,
                payload TEXT NOT NULL
            ) STRICT;

            CREATE INDEX idx_emp_partial ON employees (last_name) WHERE dept_id IS NOT NULL;
            CREATE INDEX idx_emp_expr ON employees (lower(first_name));

            CREATE VIEW active_employees AS
                SELECT emp_id, full_name FROM employees;
            """
        )

        schema = get_database_schema(self.conn)
        self.assertEqual(
            set(schema.keys()),
            {"departments", "employees", "strict_audit", "active_employees"},
        )

        # 1. Проверка обычной таблицы departments
        dept = schema["departments"]
        self.assertEqual(dept.schema, "main")
        self.assertIs(dept.table_type, TableType.TABLE)
        self.assertEqual(dept.n_cols, 3)
        self.assertFalse(dept.is_without_rowid)
        self.assertFalse(dept.is_strict)
        self.assertEqual(dept.columns["budget"].default_value, "0.0")
        self.assertTrue(dept.columns["code"].not_null)
        self.assertEqual(len(dept.indexes), 1)
        self.assertIs(dept.indexes[0].origin, IndexOrigin.UNIQUE_CONSTRAINT)
        self.assertTrue(dept.indexes[0].is_unique)

        # 2. Проверка WITHOUT ROWID, generated columns, FK и индексов
        emp = schema["employees"]
        self.assertTrue(emp.is_without_rowid)
        self.assertTrue(emp.is_without_rowid_valid)
        self.assertEqual(emp.n_cols, 7)

        self.assertIs(emp.columns["full_name"].hidden, ColumnType.DYNAMIC)
        self.assertTrue(emp.columns["full_name"].is_generated)
        self.assertIs(emp.columns["initials"].hidden, ColumnType.STORED)
        self.assertTrue(emp.columns["initials"].is_generated)
        self.assertIs(emp.columns["avatar"].data_type, DataClass.BLOB)

        self.assertEqual([c.name for c in emp.primary_key_columns], ["dept_id", "emp_id"])

        self.assertEqual(
            emp.foreign_keys,
            (
                ForeignKey(
                    id=0,
                    seq=0,
                    from_column="dept_id",
                    to_table="departments",
                    to_column="dept_id",
                    on_update=ForeignKeyAction.CASCADE,
                    on_delete=ForeignKeyAction.SET_NULL,
                ),
            ),
        )

        indexes_by_name = {idx.name: idx for idx in emp.indexes}
        self.assertIs(indexes_by_name["sqlite_autoindex_employees_1"].origin, IndexOrigin.PRIMARY_KEY)

        partial_idx = indexes_by_name["idx_emp_partial"]
        self.assertTrue(partial_idx.is_partial)
        self.assertFalse(partial_idx.is_unique)
        self.assertIs(partial_idx.origin, IndexOrigin.CREATE_INDEX)
        self.assertEqual(partial_idx.columns[0].name, "last_name")

        expr_idx = indexes_by_name["idx_emp_expr"]
        self.assertTrue(expr_idx.columns[0].is_expression)
        self.assertIsNone(expr_idx.columns[0].name)

        # 3. Проверка STRICT-таблицы
        audit = schema["strict_audit"]
        self.assertTrue(audit.is_strict)
        self.assertIs(audit.columns["id"].data_type, DataClass.INTEGER)

        # 4. Проверка VIEW
        view = schema["active_employees"]
        self.assertIs(view.table_type, TableType.VIEW)
        self.assertEqual(set(view.columns.keys()), {"emp_id", "full_name"})

    def test_system_tables_filter_and_escape(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);
            CREATE TABLE sqlite1 (id INTEGER PRIMARY KEY);
            """
        )

        default_schema = get_database_schema(self.conn, include_system=False)
        self.assertIn("users", default_schema)
        self.assertIn("sqlite1", default_schema)
        self.assertNotIn("sqlite_sequence", default_schema)

        with_system = get_database_schema(self.conn, include_system=True)
        self.assertIn("sqlite_sequence", with_system)
        self.assertIn("sqlite_schema", with_system)

    def test_temp_schema(self) -> None:
        self.conn.execute("CREATE TEMP TABLE temp_logs (msg TEXT);")
        main_schema = get_database_schema(self.conn, schema="main")
        self.assertNotIn("temp_logs", main_schema)
        temp_schema = get_database_schema(self.conn, schema="temp")
        self.assertIn("temp_logs", temp_schema)
        self.assertEqual(temp_schema["temp_logs"].schema, "temp")

    def test_preserves_connection_row_factory(self) -> None:
        self.assertIsNone(self.conn.row_factory)
        self.conn.execute("CREATE TABLE t (id INTEGER);")
        get_database_schema(self.conn)
        self.assertIsNone(self.conn.row_factory)
        row = self.conn.execute("SELECT 1;").fetchone()
        self.assertIsInstance(row, tuple)


if __name__ == "__main__":
    unittest.main()

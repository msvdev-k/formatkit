import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from formatkit.exceptions import (
    FileAlreadyExistsError,
    FileAlreadyOpenError,
    FileCorruptedError,
    FileLockedError,
    FileMissingError,
    FileNotOpenError,
    ReadOnlyFileError,
)
from formatkit.file_manager import (
    FileConnectionManager,
    FileManager,
    OpenMode,
    make_backup_file,
)


class TestFileManager(unittest.TestCase):
    def setUp(self) -> None:
        """Создаём изолированную временную папку перед каждым тестом."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.project_file = self.base_path / "test_project.fk"
        self.manager = FileManager()

    def tearDown(self) -> None:
        """Закрываем менеджер и удаляем временную папку после каждого теста."""
        self.manager.close_file()
        self.temp_dir.cleanup()

    def test_protocol_compliance(self) -> None:
        """FileManager соответствует runtime-протоколу FileConnectionManager."""
        self.assertIsInstance(self.manager, FileConnectionManager)

    def test_new_file_creation_and_state(self) -> None:
        """Создание нового файла инициализирует его и открывает в режиме READ_WRITE."""
        self.assertFalse(self.manager.is_open)

        self.manager.new_file(self.project_file)

        self.assertTrue(self.project_file.is_file())
        self.assertTrue(self.manager.is_open)
        self.assertFalse(self.manager.is_read_only)
        self.assertEqual(self.manager.mode, OpenMode.READ_WRITE)

    def test_new_file_raises_if_already_exists(self) -> None:
        """Попытка создать файл поверх существующего вызывает FileAlreadyExistsError."""
        self.project_file.touch()

        with self.assertRaises(FileAlreadyExistsError):
            self.manager.new_file(self.project_file)

    def test_new_file_raises_if_manager_already_open(self) -> None:
        """Нельзя создать новый файл, пока в менеджере открыт другой."""
        self.manager.new_file(self.project_file)
        second_file = self.base_path / "second.fk"

        with self.assertRaises(FileAlreadyOpenError):
            self.manager.new_file(second_file)

    def test_open_missing_file_raises(self) -> None:
        """Попытка открыть несуществующий файл вызывает FileMissingError."""
        with self.assertRaises(FileMissingError):
            self.manager.open_file(self.project_file)

    def test_open_corrupted_file_raises(self) -> None:
        """Попытка открыть невалидный (не SQLite) файл вызывает FileCorruptedError."""
        self.project_file.write_bytes(b"Not a valid SQLite database header!")

        with self.assertRaises(FileCorruptedError):
            self.manager.open_file(self.project_file)

    def test_transaction_commit_and_rollback(self) -> None:
        """Успешный выход из with фиксирует данные, исключение — откатывает."""
        self.manager.new_file(self.project_file)

        # 1. Успешная транзакция (COMMIT)
        with self.manager.connection(write_mode=True) as conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT);")
            conn.execute("INSERT INTO items (name) VALUES ('Item 1');")

        # 2. Транзакция со сбоем (ROLLBACK)
        with self.assertRaises(RuntimeError):
            with self.manager.connection(write_mode=True) as conn:
                conn.execute("INSERT INTO items (name) VALUES ('Item 2');")
                raise RuntimeError("Искусственный сбой внутри транзакции")

        # 3. Проверяем через read-only соединение, что в БД осталась только первая запись
        with self.manager.connection(write_mode=False) as conn:
            rows = conn.execute("SELECT name FROM items;").fetchall()
            self.assertEqual(rows, [("Item 1",)])

    def test_read_only_mode_protects_from_writes(self) -> None:
        """Файл, открытый в READ_ONLY, запрещает write_mode=True и SQL-запись."""
        self.manager.new_file(self.project_file)
        with self.manager.connection(write_mode=True) as conn:
            conn.execute("CREATE TABLE test (val INTEGER);")
        self.manager.close_file()

        # Открываем повторно только для чтения
        self.manager.open_file(self.project_file, open_mode=OpenMode.READ_ONLY)
        self.assertTrue(self.manager.is_read_only)

        # Запрос write_mode=True должен выбрасывать ReadOnlyFileError
        with self.assertRaises(ReadOnlyFileError):
            self.manager.connection(write_mode=True)

        # Попытка выполнить INSERT в обычном соединении должна блокироваться PRAGMA query_only
        with self.manager.connection(write_mode=False) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO test (val) VALUES (42);")

    def test_context_reentrancy_raises_runtime_error(self) -> None:
        """Повторный вход в один и тот же экземпляр FileConnectionContext запрещён."""
        self.manager.new_file(self.project_file)
        ctx = self.manager.connection(write_mode=False)

        with ctx:
            with self.assertRaises(RuntimeError):
                with ctx:
                    pass

    def test_connection_when_closed_raises(self) -> None:
        """Запрос соединения на закрытом менеджере вызывает FileNotOpenError."""
        with self.assertRaises(FileNotOpenError):
            self.manager.connection()

    def test_external_lock_raises_file_locked_error(self) -> None:
        """Если другой процесс удерживает EXCLUSIVE-блокировку, возникает FileLockedError."""
        self.manager.new_file(self.project_file)
        self.manager.close_file()

        # Имитируем другой процесс, который жестко заблокировал БД
        external_conn = sqlite3.connect(self.project_file, isolation_level=None)
        try:
            external_conn.execute("BEGIN EXCLUSIVE;")

            with self.assertRaises(FileLockedError):
                self.manager.open_file(self.project_file, open_mode=OpenMode.READ_WRITE)
        finally:
            external_conn.execute("ROLLBACK;")
            external_conn.close()

    def test_copy_file(self) -> None:
        """Проверяем экспорт копии файла (copy_file) и защиту от перезаписи."""
        self.manager.new_file(self.project_file)
        with self.manager.connection(write_mode=True) as conn:
            conn.execute("CREATE TABLE data (info TEXT);")
            conn.execute("INSERT INTO data (info) VALUES ('secret');")

        exported_path = self.base_path / "exported" / "copy.fk"
        self.manager.copy_file(exported_path)
        self.assertTrue(exported_path.is_file())

        # Повторное копирование по тому же пути запрещено
        with self.assertRaises(FileAlreadyExistsError):
            self.manager.copy_file(exported_path)

        # Проверяем целостность данных и гарантированно закрываем соединение через closing
        with closing(sqlite3.connect(exported_path)) as check_conn:
            row = check_conn.execute("SELECT info FROM data;").fetchone()
            self.assertEqual(row[0], "secret")

    def test_make_backup_file(self) -> None:
        """Проверяем создание резервной копии (make_backup_file)."""
        self.manager.new_file(self.project_file)
        with self.manager.connection(write_mode=True) as conn:
            conn.execute("CREATE TABLE data (info TEXT);")
            conn.execute("INSERT INTO data (info) VALUES ('backup_secret');")

        backup_dir = self.base_path / "backups"
        backup_path = make_backup_file(self.project_file, backup_dir=backup_dir)

        self.assertTrue(backup_path.is_file())
        self.assertEqual(backup_path.parent, backup_dir.resolve())

        # Проверяем, что данные внутри бэкапа целые, и закрываем дескриптор файла
        with closing(sqlite3.connect(backup_path)) as check_conn:
            row = check_conn.execute("SELECT info FROM data;").fetchone()
            self.assertEqual(row[0], "backup_secret")


if __name__ == "__main__":
    unittest.main()

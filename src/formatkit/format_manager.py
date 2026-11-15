from abc import ABC, abstractmethod
from typing import Callable, Type, Iterable, Generator

from .file_manager import FileConnectionManager


class PipelineContext:
    """Объект передаваемый по цепочке (контекст)"""

    def __init__(self, connection: FileConnectionManager) -> None:
        self._fcm: FileConnectionManager = connection
        self._stop_flag: bool = False

    @property
    def connection_manager(self) -> FileConnectionManager:
        """Возвращает активный источник соединения с файлом проекта"""
        return self._fcm

    def stop(self) -> None:
        """Остановить конвейер"""
        self._stop_flag = True

    @property
    def is_stopped(self) -> bool:
        """Флаг остановки конвейера."""
        return self._stop_flag


class FormatControlStep(ABC):
    """
    Базовый класс для звена цепочки управление форматом.
    Определяет логику конкретного звена.
    """

    @abstractmethod
    def execute(self, context: PipelineContext) -> None:
        """
        Выполняет определённое действие
        """
        pass


class PipelineRegistry:
    """
    Глобальный реестр и координатор конвейера миграций.
    Автоматически собирает звенья через декоратор @order.
    """
    # Внутренний глобальный реестр
    _pipeline_registry: dict[str | None, dict[Type[FormatControlStep], int]] = {}

    @classmethod
    def register_step(cls,
                      step_order: int,
                      file_format: Iterable[str] | str | None,
                      step_cls: Type[FormatControlStep]) -> None:
        """Внутренний метод для авто регистрации звена."""
        if file_format is None or isinstance(file_format, str):
            cls._pipeline_registry.setdefault(file_format, {})[step_cls] = step_order
        elif isinstance(file_format, Iterable):
            for ff in file_format:
                if isinstance(ff, str):
                    cls._pipeline_registry.setdefault(ff, {})[step_cls] = step_order
                else:
                    raise TypeError(
                        f"Неверный тип поля file_format декоратора @order() в классе {step_cls.__name__}"
                    )

    @classmethod
    def pipeline_steps(cls, file_format: str | None = None) -> Generator[FormatControlStep, None, None]:
        """Список последовательности шагов конвейера"""
        sorted_items = sorted(cls._pipeline_registry[file_format].items(), key=lambda item: item[1])
        return (step_cls() for step_cls, _ in sorted_items)


def order(step_order: int,
          schema: Iterable[str] | str | None = None
          ) -> Callable[[Type[FormatControlStep]], Type[FormatControlStep]]:
    """
    Декоратор для установки порядка звена в конвейере.
    """

    def decorator(cls: Type[FormatControlStep]) -> Type[FormatControlStep]:
        if not issubclass(cls, FormatControlStep):
            raise TypeError(
                f"Декоратор @order можно применять только к подклассам FormatControlStep. Ошибка в {cls.__name__}"
            )
        PipelineRegistry.register_step(step_order, schema, cls)
        return cls

    return decorator







class FormatManager:
    def __init__(self,
                 application_id: int,
                 schema_version: int,
                 schema: str | None = None) -> None:
        self._application_id: int = application_id
        self._schema_version: int = schema_version
        self._schema: str | None = schema




    def check(self, fcm:FileConnectionManager):
        """Проверить и обновить формат файла"""

    def update(self):
        """Обновить формат до актуального состояния"""
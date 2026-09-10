"""Коробка вокруг модели.

Снаружи видны только эти имена. Всё остальное — политики, судья, планировщик,
транспорт — внутренности пакета, и приложение про них не знает.
"""

from .core import Agent
from .llm import AgentError
from .settings import ROLE, blocks

__all__ = ["Agent", "AgentError", "ROLE", "blocks"]

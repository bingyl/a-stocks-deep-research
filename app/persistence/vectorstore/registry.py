"""向量后端注册表（与 factory 分离，避免 backends ↔ factory 循环依赖）。"""

from __future__ import annotations

from collections.abc import Callable

from app.persistence.vectorstore.base import VectorStore

BackendFactory = Callable[..., VectorStore]

_REGISTRY: dict[str, BackendFactory] = {}


def register_backend(name: str) -> Callable[[BackendFactory], BackendFactory]:
    """装饰器：注册后端工厂。名称大小写不敏感。"""

    key = name.strip().lower()

    def decorator(factory: BackendFactory) -> BackendFactory:
        if key in _REGISTRY:
            raise ValueError(f"vector backend already registered: {key}")
        _REGISTRY[key] = factory
        return factory

    return decorator


def get_backend_factory(name: str) -> BackendFactory | None:
    return _REGISTRY.get(name.strip().lower())


def registered_backend_names() -> list[str]:
    return sorted(_REGISTRY)


def clear_backend_registry() -> None:
    """测试用。"""
    _REGISTRY.clear()

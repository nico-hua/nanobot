"""Discovery and registration of built-in tools."""

from __future__ import annotations

import importlib
import inspect
import pkgutil

from .base import Tool
from .context import ToolContext
from .registry import ToolRegistry

_BUILTIN_PACKAGE = "nanobot.tools.builtin"


class ToolLoader:
    """Create concrete built-in tools and register them in a ToolRegistry."""

    def load(
        self,
        registry: ToolRegistry,
        context: ToolContext,
    ) -> tuple[str, ...]:
        """Discover, create, and register tools in a stable order."""

        if not isinstance(registry, ToolRegistry):
            raise TypeError("ToolLoader requires a ToolRegistry")
        if not isinstance(context, ToolContext):
            raise TypeError("ToolLoader requires a ToolContext")

        registered_names: list[str] = []
        for tool_class in self._discover_tool_classes():
            if not tool_class.enabled(context):
                continue
            tool = tool_class.create(context)
            if registry.has(tool.name):
                continue
            registry.register(tool)
            registered_names.append(tool.name)

        return tuple(registered_names)

    def _discover_tool_classes(self) -> tuple[type[Tool], ...]:
        package = importlib.import_module(_BUILTIN_PACKAGE)
        package_paths = getattr(package, "__path__", None)
        if package_paths is None:
            raise ValueError(f"Built-in tools package is not a package: {_BUILTIN_PACKAGE}")

        classes: list[type[Tool]] = []
        seen_classes: set[type[Tool]] = set()
        modules = sorted(
            pkgutil.iter_modules(
                package_paths,
                prefix=f"{package.__name__}.",
            ),
            key=lambda module: module.name,
        )
        for module_info in modules:
            module_name = module_info.name.rsplit(".", maxsplit=1)[-1]
            if module_name.startswith("_"):
                continue

            module = importlib.import_module(module_info.name)
            for _, candidate in inspect.getmembers(module, inspect.isclass):
                if (
                    candidate is Tool
                    or not issubclass(candidate, Tool)
                    or inspect.isabstract(candidate)
                    or candidate.__module__ != module.__name__
                    or candidate in seen_classes
                ):
                    continue
                seen_classes.add(candidate)
                classes.append(candidate)

        return tuple(classes)

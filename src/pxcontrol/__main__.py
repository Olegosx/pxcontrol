"""Точка входа: ``python -m pxcontrol``."""

from __future__ import annotations

import sys

from pxcontrol.app import run


def main() -> int:
	"""Запускает приложение и возвращает код выхода процесса."""
	return run()


if __name__ == "__main__":
	sys.exit(main())

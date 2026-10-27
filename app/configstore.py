"""Живой конфиг: перечитывается, когда файл изменился, без перезапуска бота.

ConfigStore ведёт себя как Config (settings, funnels, funnel()), но каждый раз отдаёт текущий разбор.
Код, которому нужна согласованность внутри одной операции, берёт `snapshot()` один раз.
Битый файл (правили руками) не роняет бота: остаётся прежний конфиг, ошибка — в журнал и в `last_error`.
"""
import logging
import os

from .config import ConfigError, load_config

log = logging.getLogger("funnel.config")


class ConfigStore:
    def __init__(self, path: str, loader=load_config):
        self.path, self._loader = path, loader
        self.last_error = None
        self._cfg = loader(path)          # на старте ошибка конфига — фатальна, как раньше
        self._sig = self._signature()

    def _signature(self):
        # inode меняется при замене файла (os.replace), поэтому две быстрые правки подряд не склеиваются
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def snapshot(self):
        return self._cfg

    @property
    def settings(self):
        return self._cfg.settings

    @property
    def funnels(self):
        return self._cfg.funnels

    def funnel(self, key):
        return self._cfg.funnel(key)

    def maybe_reload(self) -> bool:
        """Дёшево (один stat): True, если подхватили новый конфиг."""
        sig = self._signature()
        if sig is None or sig == self._sig:
            return False
        return self.reload()

    def reload(self) -> bool:
        self._sig = self._signature()
        try:
            cfg = self._loader(self.path)
        except ConfigError as e:
            self.last_error = str(e)
            log.error("конфиг изменён, но не разобран, остаюсь на прежнем: %s", e)
            return False
        self._cfg, self.last_error = cfg, None
        log.info("конфиг воронок перечитан")
        return True

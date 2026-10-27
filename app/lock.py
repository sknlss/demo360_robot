"""Замок на файле: второй процесс не стартует.

flock снимается ядром, когда процесс умирает (в том числе kill -9), поэтому
«зависшего» замка не бывает и чистить файл руками не нужно.
"""
import fcntl
import os


class AlreadyRunning(Exception):
    pass


class ProcessLock:
    def __init__(self, path: str):
        self.path = path
        self._fd = None

    def acquire(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = ""
            try:
                holder = os.read(fd, 32).decode().strip()
            except OSError:
                pass
            os.close(fd)
            raise AlreadyRunning(
                f"Бот уже запущен другим процессом (PID {holder or 'неизвестен'}), "
                f"замок: {self.path}. Остановите его или используйте другую папку данных."
            )
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return self

    def release(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

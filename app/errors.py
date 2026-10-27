"""Ошибки отправки, не зависящие от библиотеки Telegram.

Адаптер aiogram переводит свои исключения в эти, а очередь знает только их.
"""


class SendError(Exception):
    pass


class RetryAfter(SendError):
    """Telegram просит подождать ровно `seconds` секунд."""

    def __init__(self, seconds: float):
        super().__init__(f"retry after {seconds}s")
        self.seconds = seconds


class Permanent(SendError):
    """Повторять бессмысленно (запрос отклонён)."""


class Blocked(Permanent):
    """Пользователь заблокировал бота или удалил чат."""


class Transient(SendError):
    """Временная ошибка: сеть, 5xx. Повторяем ограниченное число раз."""

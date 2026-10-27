"""Фото профиля: тянем из Telegram только по запросу дашборда и кешируем на диск."""
import asyncio
import hashlib
import os


class PhotoCache:
    def __init__(self, db, bot, directory: str):
        self.db, self.bot, self.dir = db, bot, directory
        self._locks: dict[str, asyncio.Lock] = {}
        os.makedirs(directory, exist_ok=True)

    async def get_path(self, user_id: int):
        row = self.db.conn.execute(
            "SELECT photo_file_id FROM users WHERE tg_id=?", (user_id,)).fetchone()
        if row is None or not row["photo_file_id"]:
            return None
        file_id = row["photo_file_id"]
        # имя файла от file_id: сменилось фото — скачается новое; токен в имени не участвует
        path = os.path.join(self.dir, hashlib.sha256(file_id.encode()).hexdigest()[:32] + ".jpg")
        if os.path.exists(path):
            return path
        async with self._locks.setdefault(file_id, asyncio.Lock()):
            if not os.path.exists(path):
                tmp = path + ".part"
                await self.bot.download(file_id, destination=tmp)
                os.replace(tmp, path)
        return path

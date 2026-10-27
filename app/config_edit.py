"""Запись воронок обратно в YAML-файл, из которого читает бот. Отдельно от разбора (config.py).

Правила:
- комментарии и порядок ключей сохраняются: файл читается и пишется круговым разбором ruamel.yaml
  (PyYAML при записи их теряет). Кодек подставляемый: тесты без ruamel гоняют ту же логику на JSON;
- меняется только список шагов выбранной воронки, и только то, что действительно изменилось;
- проверка — та же `parse_config`, что при чтении, и не на черновике, а на ПЕРЕЧИТАННОМ тексте, который
  собираются записать: проверенное и записанное не могут разойтись;
- запись — во временный файл рядом и `os.replace` одним движением: бот не прочитает половину.
"""
import json
import os
import tempfile
import threading

from .config import ConfigError, clean_tags, funnel_to_dict, parse_config, parse_duration

MANAGED = ("kind", "text", "duration", "tags", "check", "on_true", "on_false", "request_phone", "on_reply", "next")
# какие поля бывают у какого вида: остальные из черновика отбрасываются на сервере, как бы ни была устроена форма
KIND_FIELDS = {"message": ("text", "request_phone", "on_reply", "next"),
               "delay": ("duration",),
               "condition": ("check", "on_true", "on_false"),
               "tags": ("tags",)}
_write_lock = threading.Lock()


class EditError(Exception):
    """Черновик нельзя записать; текст понятен человеку."""


class RuamelCodec:
    """Круговой разбор: комментарии, кавычки и порядок ключей остаются как были."""

    def __init__(self):
        from ruamel.yaml import YAML  # импорт здесь: остальное приложение ruamel не требует
        y = YAML()                      # typ="rt" по умолчанию
        y.preserve_quotes = True
        y.width = 4096                  # не переносить длинные тексты
        y.indent(mapping=2, sequence=4, offset=2)
        self._y = y

    def load(self, text):
        return self._y.load(text)

    def dump(self, doc) -> str:
        import io
        buf = io.StringIO()
        self._y.dump(doc, buf)
        return buf.getvalue()

    def new_map(self):
        from ruamel.yaml.comments import CommentedMap
        return CommentedMap()


class JsonCodec:
    """Для тестов и предпросмотра без ruamel/PyYAML: JSON — подмножество YAML, логика правки та же."""

    def load(self, text):
        return json.loads(text)

    def dump(self, doc) -> str:
        return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"

    def new_map(self):
        return {}


def plain(obj):
    """Обычные dict/list/str/числа: без типов ruamel, пригодно для parse_config и json."""
    if isinstance(obj, dict):
        return {str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [plain(v) for v in obj]
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj)
    return str(obj)


def _clean_step(raw, i: int) -> dict:
    """Черновик шага из формы -> только значимые поля. Пустое = поля нет."""
    if not isinstance(raw, dict):
        raise EditError(f"шаг №{i + 1}: ожидался объект")
    key = raw.get("key")
    if not isinstance(key, str) or not key.strip():
        raise EditError(f"шаг №{i + 1}: нет ключа")
    kind = raw.get("kind") or "message"
    if kind not in KIND_FIELDS:
        raise EditError(f"шаг '{key}': неизвестный вид '{kind}' (есть: {', '.join(KIND_FIELDS)})")
    out = {"key": key.strip(), "kind": kind}
    fields = KIND_FIELDS[kind]
    if "text" in fields:
        out["text"] = raw.get("text")
    if "duration" in fields:
        duration = raw.get("duration")
        out["duration"] = duration.strip() if isinstance(duration, str) else duration
    if "tags" in fields:
        out["tags"] = clean_tags(raw.get("tags"))  # негодные метки молча отбрасываются; пусто — не пройдёт проверку
    if "check" in fields:
        check = raw.get("check")
        out["check"] = {k: v for k, v in dict(check).items() if v not in (None, "")} if isinstance(check, dict) else check
        out["on_true"], out["on_false"] = raw.get("on_true"), raw.get("on_false")
    if "request_phone" in fields and raw.get("request_phone"):
        out["request_phone"] = dict(raw["request_phone"])
    if "on_reply" in fields and raw.get("on_reply"):
        out["on_reply"] = {k: v for k, v in dict(raw["on_reply"]).items() if v}
    if "next" in fields and raw.get("next"):
        out["next"] = raw["next"]
    return out


def _same_seconds(a, b) -> bool:
    try:
        return parse_duration(a, "") == parse_duration(b, "")
    except ConfigError:
        return False


def _like(old_value, value):
    """Текст в кавычках остаётся в кавычках: ruamel хранит стиль скаляра в его типе."""
    if isinstance(old_value, str) and type(old_value) is not str and isinstance(value, str):
        try:
            return type(old_value)(value)
        except TypeError:
            pass
    return value


def _merge_step(old, new: dict, codec):
    """Привести существующий шаг к черновику, трогая только отличающееся."""
    old.pop("delay", None)                      # поле прежней модели: задержка теперь отдельный шаг
    for f in MANAGED:
        if f not in new:
            old.pop(f, None)
        elif f == "duration":
            if "duration" not in old or not _same_seconds(old["duration"], new["duration"]):
                old["duration"] = new["duration"]  # «10s» остаётся «10s», если значение то же
        elif isinstance(new[f], dict):
            cur = old.get(f)
            if not isinstance(cur, dict):
                cur = codec.new_map()
                old[f] = cur
            for k in [k for k in cur if k not in new[f]]:
                del cur[k]
            for k, v in new[f].items():
                if cur.get(k) != v:
                    cur[k] = v
        elif old.get(f) != new[f]:
            old[f] = _like(old.get(f), new[f])


def _new_step(new: dict, codec):
    m = codec.new_map()
    for f in ("key",) + MANAGED:
        if f in new:
            if isinstance(new[f], dict):
                sub = codec.new_map()
                sub.update(new[f])
                m[f] = sub
            else:
                m[f] = new[f]
    return m


def _index_is(seq, item):
    for i, x in enumerate(seq):
        if x is item:
            return i
    return -1


def apply_steps(doc, funnel_key: str, steps, codec) -> None:
    """Привести шаги воронки в `doc` к черновику. Шаг опознаётся по key. Порядок меняется удалением и вставкой
    (`del`/`insert`) — так ruamel переносит комментарии вместе с шагом."""
    try:
        funnel = doc["funnels"][funnel_key]
        seq = funnel["steps"]
    except (KeyError, TypeError):
        raise EditError(f"воронки '{funnel_key}' нет в файле")
    if not isinstance(steps, list) or not steps:
        raise EditError("в воронке должен быть хотя бы один шаг")
    drafts = [_clean_step(s, i) for i, s in enumerate(steps)]
    keys = [d["key"] for d in drafts]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise EditError(f"ключи шагов повторяются: {', '.join(dup)}")
    by_key = {str(s.get("key")): s for s in seq}
    want = []
    for d in drafts:
        old = by_key.get(d["key"])
        if old is None:
            want.append(_new_step(d, codec))
        else:
            _merge_step(old, d, codec)
            want.append(old)
    for i in reversed(range(len(seq))):                 # убрать удалённые
        if _index_is(want, seq[i]) < 0:
            del seq[i]
    for j, item in enumerate(want):                     # расставить по порядку
        if j < len(seq) and seq[j] is item:
            continue
        k = _index_is(seq, item)
        if k >= 0:
            del seq[k]
        seq.insert(j, item)


def write_atomic(path: str, text: str) -> None:
    """Временный файл в той же папке (на одной файловой системе), fsync, os.replace."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".funnels-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_funnel_steps(path: str, funnel_key: str, steps, codec=None):
    """Проверить и записать шаги воронки. Возвращает разобранный Config записанного файла.
    Не прошло проверку — файл не тронут, исключение EditError/ConfigError с причиной."""
    codec = codec or RuamelCodec()
    with _write_lock:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            raise EditError(f"не могу прочитать {path}: {e}")
        doc = codec.load(text)
        apply_steps(doc, funnel_key, steps, codec)
        out = codec.dump(doc)
        # проверяем то, что реально запишем: перечитываем итоговый текст
        cfg = parse_config(plain(codec.load(out)))
        got = [s.key for s in cfg.funnel(funnel_key).steps]
        if got != [str(s["key"]).strip() for s in steps]:
            raise EditError("после записи порядок шагов не совпал бы с черновиком; файл не изменён")
        if out != text:
            write_atomic(path, out)
        return cfg

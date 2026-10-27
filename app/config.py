"""Разбор конфига воронок. Единственное место, где знают структуру YAML.

Разбор (`parse_config`) работает со словарём и не зависит от PyYAML: чтение файла
(`load_config`) отдельно. Так будущие страница-схема и редактор могут брать
готовые объекты `Config` или вызывать `parse_config` на черновике из формы.

Шаги опознаются по `key` и `kind`, а не по месту в списке. Виды шагов
регистрируются в STEP_KINDS: новый вид = новая функция разбора.
"""
import re
from dataclasses import dataclass, field
from typing import Optional


class ConfigError(Exception):
    pass


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value, where: str) -> float:
    """10, '10', '10s', '1m', '1h' -> секунды."""
    if isinstance(value, bool):
        raise ConfigError(f"{where}: ожидалась длительность, получено {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", value)
        if not m:
            raise ConfigError(f"{where}: не понимаю длительность {value!r} (примеры: 10s, 1m)")
        seconds = float(m.group(1)) * _UNITS[m.group(2) or "s"]
    else:
        raise ConfigError(f"{where}: ожидалась длительность, получено {value!r}")
    if seconds < 0:
        raise ConfigError(f"{where}: длительность не может быть отрицательной")
    return seconds


END = "end"  # значение `next`: после этого шага прогон завершается; ключ шага так назвать нельзя
REPLY_OUTCOMES = ("contact", "decline")  # ответы человека, из которых делается ветвление


@dataclass(frozen=True)
class PhoneRequest:
    """Шаг просит номер: кнопка запроса контакта и кнопка отказа."""
    button: str
    decline_button: str


@dataclass(frozen=True)
class MessageStep:
    key: str
    text: str
    delay: float = 0.0                          # секунд между отправкой предыдущего шага и этим
    request_phone: Optional[PhoneRequest] = None
    on_reply: tuple = ()                        # ((исход, ключ шага), ...): куда идти после ответа
    next: Optional[str] = None                  # явный следующий шаг, "end" или None = по умолчанию
    kind: str = "message"

    def reply_target(self, outcome: str) -> Optional[str]:
        return dict(self.on_reply).get(outcome)


ENTRY_KINDS = ("phone_given",)


@dataclass(frozen=True)
class EntryCondition:
    kind: str  # сейчас единственный вид: phone_given («человек уже оставил номер в этой воронке»)
    reply: str


@dataclass(frozen=True)
class Funnel:
    key: str
    steps: tuple
    entry_conditions: tuple = ()

    @property
    def first(self):
        return self.steps[0]

    def step(self, key: str):
        for s in self.steps:
            if s.key == key:
                return s
        return None

    def branch_targets(self) -> set:
        """Шаги, в которые попадают ответом человека: по списку в них не «проваливаются»."""
        return {k for s in self.steps for _, k in s.on_reply}

    def next_step(self, step):
        """Следующий шаг цепочки после `step` или None, если цепочка закончилась.

        Явный `next` главнее. Иначе — сосед по списку, но только если он не цель ветвления:
        шаги «спасибо» и «ладно, без номера» лежат в списке после напоминаний, и по порядку
        на них никто не попадает."""
        if step.next == END:
            return None
        if step.next:
            return self.step(step.next)
        i = self.steps.index(step)
        if i + 1 < len(self.steps) and self.steps[i + 1].key not in self.branch_targets():
            return self.steps[i + 1]
        return None


@dataclass(frozen=True)
class Settings:
    default_funnel: str
    stale_after: float = 300.0   # опоздание, после которого шаг не отправляем
    send_pause: float = 0.1      # пауза между отправками
    max_attempts: int = 3
    retry_delay: float = 5.0


@dataclass(frozen=True)
class Config:
    settings: Settings
    funnels: dict = field(default_factory=dict)

    def funnel(self, key):
        return self.funnels.get(key)


def _req(d: dict, name: str, where: str, typ=str):
    if not isinstance(d, dict):
        raise ConfigError(f"{where}: ожидался словарь")
    if name not in d:
        raise ConfigError(f"{where}: нет поля '{name}'")
    v = d[name]
    if not isinstance(v, typ) or (typ is str and not v.strip()):
        raise ConfigError(f"{where}.{name}: ожидалось непустое значение типа {typ.__name__}")
    return v


def _parse_phone_request(raw, where: str) -> PhoneRequest:
    return PhoneRequest(button=_req(raw, "button", where), decline_button=_req(raw, "decline_button", where))


def _parse_message(raw: dict, where: str) -> MessageStep:
    phone = _parse_phone_request(raw["request_phone"], f"{where}.request_phone") if "request_phone" in raw else None
    on_reply = ()
    if "on_reply" in raw:
        r = raw["on_reply"]
        if not isinstance(r, dict) or not r:
            raise ConfigError(f"{where}.on_reply: ожидался словарь «ответ: ключ шага»")
        for outcome, target in r.items():
            if outcome not in REPLY_OUTCOMES:
                raise ConfigError(f"{where}.on_reply.{outcome}: неизвестный ответ (есть: {', '.join(REPLY_OUTCOMES)})")
            if not isinstance(target, str) or not target.strip():
                raise ConfigError(f"{where}.on_reply.{outcome}: нужен ключ шага")
        on_reply = tuple((o, r[o]) for o in REPLY_OUTCOMES if o in r)
    nxt = raw.get("next")
    if nxt is not None and (not isinstance(nxt, str) or not nxt.strip()):
        raise ConfigError(f"{where}.next: нужен ключ шага или '{END}'")
    return MessageStep(
        key=raw["key"],
        text=_req(raw, "text", where),
        delay=parse_duration(raw.get("delay", 0), f"{where}.delay"),
        request_phone=phone,
        on_reply=on_reply,
        next=nxt,
    )


STEP_KINDS = {"message": _parse_message}


def _parse_entry(raw: dict, where: str) -> EntryCondition:
    kind = _req(raw, "kind", where)
    if kind == "already_completed":
        raise ConfigError(f"{where}.kind: условие 'already_completed' заменено на 'phone_given' (оставил номер): "
                          f"напоминания без ответа воронку не завершают для этого условия")
    if kind not in ENTRY_KINDS:
        raise ConfigError(f"{where}.kind: неизвестное условие '{kind}' (есть: {', '.join(ENTRY_KINDS)})")
    return EntryCondition(kind=kind, reply=_req(raw, "reply", where))


def _check_links(funnel: Funnel, where: str):
    """Ссылки между шагами и ответы на запросы. Битая ссылка = прогон, который встанет на месте."""
    keys = {s.key for s in funnel.steps}
    for i, s in enumerate(funnel.steps):
        sw = f"{where}.steps[{i}] ('{s.key}')"
        if s.on_reply and not s.request_phone:
            raise ConfigError(f"{sw}: on_reply есть, а request_phone нет — ответа ждать не от чего")
        if s.request_phone:
            for outcome in REPLY_OUTCOMES:
                if s.reply_target(outcome) is None:
                    raise ConfigError(f"{sw}.on_reply: не указан шаг для ответа '{outcome}'")
        for outcome, target in s.on_reply:
            if target not in keys:
                raise ConfigError(f"{sw}.on_reply.{outcome}: шага '{target}' нет в воронке")
        if s.next and s.next != END and s.next not in keys:
            raise ConfigError(f"{sw}.next: шага '{s.next}' нет в воронке")
    # цепочка по next (без ветвлений, которые требуют ответа человека) не должна замыкаться
    for s in funnel.steps:
        seen, cur = set(), s
        while cur is not None:
            if cur.key in seen:
                raise ConfigError(f"{where}: цепочка шагов зацикливается на '{cur.key}'")
            seen.add(cur.key)
            cur = funnel.next_step(cur)


def _parse_funnel(key: str, raw: dict) -> Funnel:
    where = f"funnels.{key}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: ожидался словарь")
    for old in ("welcome", "phone"):
        if old in raw:
            raise ConfigError(f"{where}.{old}: поле убрано — приветствие и запрос номера теперь обычные шаги в steps "
                              f"(см. funnels.yml в репозитории)")
    steps, seen = [], set()
    for i, s in enumerate(_req(raw, "steps", where, list)):
        sw = f"{where}.steps[{i}]"
        skey = _req(s, "key", sw)
        kind = _req(s, "kind", sw)
        if kind not in STEP_KINDS:
            raise ConfigError(f"{sw}.kind: неизвестный вид '{kind}' (есть: {', '.join(STEP_KINDS)})")
        if skey == END:
            raise ConfigError(f"{sw}.key: '{END}' — служебное слово, ключ шага так назвать нельзя")
        if skey in seen:
            raise ConfigError(f"{sw}.key: ключ шага '{skey}' повторяется")
        seen.add(skey)
        steps.append(STEP_KINDS[kind](s, sw))
    if not steps:
        raise ConfigError(f"{where}.steps: пустой список шагов")
    conds = tuple(
        _parse_entry(c, f"{where}.entry_conditions[{i}]")
        for i, c in enumerate(raw.get("entry_conditions") or [])
    )
    funnel = Funnel(key=key, steps=tuple(steps), entry_conditions=conds)
    _check_links(funnel, where)
    return funnel


def parse_config(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("корень конфига должен быть словарём")
    funnels_raw = _req(raw, "funnels", "config", dict)
    funnels = {str(k): _parse_funnel(str(k), v) for k, v in funnels_raw.items()}
    if not funnels:
        raise ConfigError("funnels: нет ни одной воронки")
    s = raw.get("settings") or {}
    default = s.get("default_funnel") or next(iter(funnels))
    if default not in funnels:
        raise ConfigError(f"settings.default_funnel: воронки '{default}' нет в funnels")
    d = Settings(default_funnel=default)
    max_attempts = int(s.get("max_attempts", d.max_attempts))
    if max_attempts < 1:
        raise ConfigError("settings.max_attempts: должно быть не меньше 1")
    settings = Settings(
        default_funnel=default,
        stale_after=parse_duration(s.get("stale_after", d.stale_after), "settings.stale_after"),
        send_pause=parse_duration(s.get("send_pause", d.send_pause), "settings.send_pause"),
        max_attempts=max_attempts,
        retry_delay=parse_duration(s.get("retry_delay", d.retry_delay), "settings.retry_delay"),
    )
    return Config(settings=settings, funnels=funnels)


def load_config(path: str) -> Config:
    import yaml  # импорт здесь: parse_config не требует PyYAML

    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"не могу прочитать {path}: {e}")
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: некорректный YAML: {e}")
    return parse_config(raw)


# ---------- сериализация для страницы (и будущего редактора) ----------
# Исходные поля шага — ровно то, что лежит в YAML. Вычисленное (куда пойдёт дальше, откуда в шаг
# попадают) — отдельно, в `derived`: черновик из редактора его не имеет, и отрисовка обходится без него.

def step_to_dict(step, funnel: Optional[Funnel] = None) -> dict:
    d = {"key": step.key, "kind": step.kind, "text": step.text, "delay": step.delay}
    if step.request_phone:
        d["request_phone"] = {"button": step.request_phone.button, "decline_button": step.request_phone.decline_button}
    if step.on_reply:
        d["on_reply"] = dict(step.on_reply)
    if step.next:
        d["next"] = step.next
    if funnel is not None:
        nxt = funnel.next_step(step)
        reached_by = []
        for s in funnel.steps:
            for outcome, target in s.on_reply:
                if target == step.key:
                    reached_by.append({"step": s.key, "reply": outcome})
        d["derived"] = {"next_resolved": nxt.key if nxt else None, "reached_by": reached_by}
    return d


def funnel_to_dict(funnel: Funnel) -> dict:
    return {"key": funnel.key,
            "entry_conditions": [{"kind": c.kind, "reply": c.reply} for c in funnel.entry_conditions],
            "steps": [step_to_dict(s, funnel) for s in funnel.steps]}

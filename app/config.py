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


def format_duration(seconds: float) -> str:
    """Секунды -> '10s' / '1m' / '1h' (обратное к parse_duration; редактор пишет задержки в этом виде)."""
    n = int(seconds) if float(seconds).is_integer() else seconds
    if isinstance(n, int) and n and n % 3600 == 0:
        return f"{n // 3600}h"
    if isinstance(n, int) and n and n % 60 == 0:
        return f"{n // 60}m"
    return f"{n}s"


END = "end"    # значение `next` и выходов условия: прогон завершается; ключ шага так назвать нельзя
NEXT = "next"  # выход условия «следующий шаг по списку»; ключ шага так назвать нельзя
RESERVED_KEYS = (END, NEXT)
REPLY_OUTCOMES = ("contact", "decline")  # ответы человека, из которых делается ветвление
CHECK_KINDS = ("tag", "phone")     # проверки условия; вычисляются в app/checks.py (там тот же набор)


@dataclass(frozen=True)
class PhoneRequest:
    """Шаг просит номер: кнопка запроса контакта и кнопка отказа."""
    button: str
    decline_button: str


@dataclass(frozen=True)
class MessageStep:
    key: str
    text: str
    request_phone: Optional[PhoneRequest] = None
    on_reply: tuple = ()                        # ((исход, ключ шага), ...): куда идти после ответа
    next: Optional[str] = None                  # явный следующий шаг, "end" или None = по умолчанию
    kind: str = "message"

    def reply_target(self, outcome: str) -> Optional[str]:
        return dict(self.on_reply).get(outcome)


@dataclass(frozen=True)
class DelayStep:
    """Пауза. Сама ничего не отправляет; отработав, ведёт на следующий шаг по списку."""
    key: str
    duration: float                             # секунд
    kind: str = "delay"


@dataclass(frozen=True)
class Check:
    kind: str                                   # tag | phone
    tag: Optional[str] = None                   # для tag: какая метка (phone параметров не имеет)


@dataclass(frozen=True)
class ConditionStep:
    """Проверка и два выхода. Каждый выход: «next» (следующий по списку), «end» (закончить прогон) или ключ шага."""
    key: str
    check: Check
    on_true: str
    on_false: str
    kind: str = "condition"

    def exit_for(self, result: bool) -> str:
        return self.on_true if result else self.on_false


@dataclass(frozen=True)
class TagsStep:
    """Ставит метки пользователю. Ничего не отправляет, проходится насквозь, как условие; ведёт на следующий по списку."""
    key: str
    tags: tuple                                 # метки, как их ставит шаг
    kind: str = "tags"


@dataclass(frozen=True)
class Funnel:
    key: str
    steps: tuple

    @property
    def first(self):
        return self.steps[0]

    def step(self, key: str):
        for s in self.steps:
            if s.key == key:
                return s
        return None

    def branch_targets(self) -> set:
        """Шаги, в которые попадают ответом человека или выходом условия: по списку в них не «проваливаются»."""
        out = {k for s in self.steps if s.kind == "message" for _, k in s.on_reply}
        for s in self.steps:
            if s.kind == "condition":
                out |= {x for x in (s.on_true, s.on_false) if x not in RESERVED_KEYS}
        return out

    def neighbor(self, step):
        i = self.steps.index(step)
        return self.steps[i + 1] if i + 1 < len(self.steps) else None

    def next_step(self, step):
        """Следующий шаг цепочки после сообщения или задержки; None — цепочка закончилась.

        Явный `next` сообщения главнее. Иначе — сосед по списку, но только если он не цель ветвления:
        шаги «спасибо» и «заявка у менеджера» лежат в списке после напоминаний, и по порядку на них
        никто не попадает. У условия своих «дальше» нет: см. `exit_step`."""
        if step.kind == "message" and step.next == END:
            return None
        if step.kind == "message" and step.next:
            return self.step(step.next)
        nb = self.neighbor(step)
        return nb if nb is not None and nb.key not in self.branch_targets() else None

    def exit_step(self, cond, result: bool):
        """Шаг, куда ведёт выход условия: «next» — сосед по списку (буквально, без исключений), «end» — None."""
        target = cond.exit_for(result)
        if target == END:
            return None
        if target == NEXT:
            return self.neighbor(cond)
        return self.step(target)

    def successors(self, step) -> list:
        """Куда может пойти прогон без участия человека (для проверок конфига и страницы)."""
        if step.kind == "condition":
            return [x for x in (self.exit_step(step, True), self.exit_step(step, False)) if x is not None]
        nxt = self.next_step(step)
        return [nxt] if nxt is not None else []

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

    def snapshot(self):
        """Конфиг на один момент. Один проход петли или один обработчик берут снимок раз и не
        обращаются к конфигу повторно: у ConfigStore то же имя возвращает текущий разбор."""
        return self


_TAG_RE = re.compile(r"\w+")  # буквы, цифры, подчёркивание (Unicode); пробелов, запятых, дефисов нет


def is_valid_tag(tag) -> bool:
    return isinstance(tag, str) and _TAG_RE.fullmatch(tag) is not None


def clean_tags(value) -> list:
    """Из строки через запятую или списка — только годные метки, без повторов, в исходном порядке.
    Негодное молча отбрасывается (так ведёт себя редактор); разбор конфига вызывает его же и сверяет."""
    items = value.split(",") if isinstance(value, str) else (value if isinstance(value, (list, tuple)) else [])
    out = []
    for t in items:
        t = t.strip() if isinstance(t, str) else t
        if is_valid_tag(t) and t not in out:
            out.append(t)
    return out


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
    if "delay" in raw:
        raise ConfigError(f"{where}.delay: у сообщения задержки больше нет — поставьте перед ним отдельный шаг "
                          f"kind: delay с полем duration")
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
    return MessageStep(key=raw["key"], text=_req(raw, "text", where), request_phone=phone,
                       on_reply=on_reply, next=nxt)


def _parse_delay(raw: dict, where: str) -> DelayStep:
    if "duration" not in raw:
        raise ConfigError(f"{where}: нет поля 'duration' (например 10s, 1m)")
    return DelayStep(key=raw["key"], duration=parse_duration(raw["duration"], f"{where}.duration"))


def _parse_check(raw, where: str) -> Check:
    kind = _req(raw, "kind", where)
    if kind == "phone_given":
        raise ConfigError(f"{where}.kind: проверка 'phone_given' убрана — она смотрела на метку, а не на номер. "
                          f"Теперь две проверки: 'phone' (поле телефона у пользователя заполнено) и "
                          f"'tag' с параметром tag (есть метка, например {{kind: tag, tag: signup_lead}})")
    if kind not in CHECK_KINDS:
        raise ConfigError(f"{where}.kind: неизвестная проверка '{kind}' (есть: {', '.join(CHECK_KINDS)})")
    if kind == "tag":
        return Check(kind=kind, tag=_req(raw, "tag", where))
    return Check(kind=kind)


def _parse_condition(raw: dict, where: str) -> ConditionStep:
    exits = []
    for name in ("on_true", "on_false"):
        if name not in raw:
            raise ConfigError(f"{where}: нет поля '{name}' — оба выхода условия указываются явно "
                              f"({NEXT} / {END} / ключ шага)")
        exits.append(_req(raw, name, where))
    return ConditionStep(key=raw["key"], check=_parse_check(raw.get("check"), f"{where}.check"),
                         on_true=exits[0], on_false=exits[1])


def _parse_tags(raw: dict, where: str) -> TagsStep:
    tags = raw.get("tags")
    if not isinstance(tags, list) or not tags:
        raise ConfigError(f"{where}.tags: нужен непустой список меток (например [lead, funnel_msg_1_sent])")
    for t in tags:
        if not is_valid_tag(t):
            raise ConfigError(f"{where}.tags: метка {t!r} не годится — только буквы, цифры и нижнее подчёркивание")
    return TagsStep(key=raw["key"], tags=tuple(dict.fromkeys(tags)))


STEP_KINDS = {"message": _parse_message, "delay": _parse_delay, "condition": _parse_condition, "tags": _parse_tags}


def _check_links(funnel: Funnel, where: str):
    """Ссылки между шагами, ответы на запросы, достижимость и зацикливание."""
    keys = {s.key for s in funnel.steps}
    for i, s in enumerate(funnel.steps):
        sw = f"{where}.steps[{i}] ('{s.key}')"
        if s.kind == "condition":
            for name in ("on_true", "on_false"):
                t = getattr(s, name)
                if t not in RESERVED_KEYS and t not in keys:
                    raise ConfigError(f"{sw}.{name}: шага '{t}' нет в воронке")
            continue
        if s.kind != "message":
            continue
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
    # в каждый шаг должен быть путь от первого: по цепочке, выходу условия или ответу человека
    reach, todo = set(), [funnel.steps[0]]
    while todo:
        cur = todo.pop()
        if cur.key in reach:
            continue
        reach.add(cur.key)
        todo.extend(funnel.successors(cur))
        if cur.kind == "message":
            todo.extend(funnel.step(k) for _, k in cur.on_reply)
    for i, s in enumerate(funnel.steps):
        if s.key not in reach:
            raise ConfigError(f"{where}.steps[{i}] ('{s.key}'): в шаг нельзя попасть — на него не ведёт ни порядок "
                              f"в списке, ни next, ни выход условия, ни on_reply")
    # цикл без единой задержки крутится вечно (условия) или заспамит человека (сообщения). Цикл с задержкой —
    # законная вещь. Ответы человека (on_reply) в цикл не входят: без человека они не срабатывают.
    plain = {s.key: [n.key for n in funnel.successors(s)] for s in funnel.steps if s.kind != "delay"}
    state = {}

    def walk(k, path):
        state[k] = 1
        path.append(k)
        for n in plain.get(k, ()):
            if n not in plain:
                continue  # задержка разрывает цикл
            if state.get(n) == 1:
                loop = path[path.index(n):] + [n]
                raise ConfigError(f"{where}: цикл без задержки: {' → '.join(loop)}. "
                                  f"Добавьте в цикл шаг kind: delay или разорвите его")
            if n not in state:
                walk(n, path)
        path.pop()
        state[k] = 2

    for k in plain:
        if k not in state:
            walk(k, [])


def _parse_funnel(key: str, raw: dict) -> Funnel:
    where = f"funnels.{key}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: ожидался словарь")
    for old in ("welcome", "phone"):
        if old in raw:
            raise ConfigError(f"{where}.{old}: поле убрано — приветствие и запрос номера теперь обычные шаги в steps "
                              f"(см. config/funnels.yml в репозитории)")
    if "entry_conditions" in raw:
        raise ConfigError(f"{where}.entry_conditions: раздел убран — условие теперь обычный шаг kind: condition "
                          f"в начале steps (см. config/funnels.yml в репозитории)")
    steps, seen = [], set()
    for i, s in enumerate(_req(raw, "steps", where, list)):
        sw = f"{where}.steps[{i}]"
        skey = _req(s, "key", sw)
        kind = _req(s, "kind", sw)
        if kind not in STEP_KINDS:
            raise ConfigError(f"{sw}.kind: неизвестный вид '{kind}' (есть: {', '.join(STEP_KINDS)})")
        if skey in RESERVED_KEYS:
            raise ConfigError(f"{sw}.key: '{skey}' — служебное слово, ключ шага так назвать нельзя")
        if skey in seen:
            raise ConfigError(f"{sw}.key: ключ шага '{skey}' повторяется")
        seen.add(skey)
        steps.append(STEP_KINDS[kind](s, sw))
    if not steps:
        raise ConfigError(f"{where}.steps: пустой список шагов")
    funnel = Funnel(key=key, steps=tuple(steps))
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
    d = {"key": step.key, "kind": step.kind}
    if step.kind == "message":
        d["text"] = step.text
        if step.request_phone:
            d["request_phone"] = {"button": step.request_phone.button,
                                  "decline_button": step.request_phone.decline_button}
        if step.on_reply:
            d["on_reply"] = dict(step.on_reply)
        if step.next:
            d["next"] = step.next
    elif step.kind == "delay":
        d["duration"] = step.duration
        d["duration_text"] = format_duration(step.duration)
    elif step.kind == "tags":
        d["tags"] = list(step.tags)
    else:
        d["check"] = {"kind": step.check.kind, **({"tag": step.check.tag} if step.check.tag else {})}
        d["on_true"], d["on_false"] = step.on_true, step.on_false
    if funnel is not None:
        # derived — вычисленное: куда пойдёт дальше и откуда сюда попадают; в YAML этого нет
        derived = {}
        if step.kind == "condition":
            derived["exits"] = {name: (lambda t: t.key if t else None)(funnel.exit_step(step, r))
                                for name, r in (("on_true", True), ("on_false", False))}
        else:
            nxt = funnel.next_step(step)
            derived["next_resolved"] = nxt.key if nxt else None
        reached_by = []
        for s in funnel.steps:
            if s.kind == "message":
                reached_by += [{"step": s.key, "reply": o} for o, t in s.on_reply if t == step.key]
            elif s.kind == "condition":
                for name, r in (("on_true", True), ("on_false", False)):
                    t = funnel.exit_step(s, r)
                    if t is not None and t.key == step.key and getattr(s, name) != NEXT:
                        reached_by.append({"step": s.key, "reply": name})
        derived["reached_by"] = reached_by
        d["derived"] = derived
    return d


def funnel_to_dict(funnel: Funnel) -> dict:
    return {"key": funnel.key, "steps": [step_to_dict(s, funnel) for s in funnel.steps]}

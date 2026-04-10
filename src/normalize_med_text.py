# src/normalize_med_text.py
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Pattern, Tuple, Union, Any

# replacement может быть строкой или функцией (для sub/repl)
Replacement = Union[str, Callable[[re.Match[str]], str]]


@dataclass(frozen=True)
class Change:
    kind: str          # "literal" | "regex"
    pattern: str       # что меняли
    replacement: str   # на что
    count: int         # сколько замен


# -------------------------
# JSON LEXICON (путь по умолчанию + override)
# -------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[1]  # если файл в src/
_DEFAULT_LEXICON_PATH = _PROJECT_ROOT / "resources" / "lexicon.json"
LEXICON_PATH = Path(os.getenv("MED_LEXICON_PATH", str(_DEFAULT_LEXICON_PATH)))


def _rx(p: str, flags: int = re.IGNORECASE) -> Pattern[str]:
    return re.compile(p, flags)

def MMR_DP(m: re.Match[str]) -> str:
    # dMMR / pMMR: приводим префикс к нижнему регистру, остальное фиксируем как MMR
    return f"{m.group(1).lower()}MMR"

def _flags_from_str(s: str) -> int:
    s = (s or "").upper().strip()
    if s in ("", "NONE", "0"):
        return 0
    flags = 0
    parts = [p.strip() for p in s.split("|")]
    for p in parts:
        if p == "IGNORECASE":
            flags |= re.IGNORECASE
        elif p == "MULTILINE":
            flags |= re.MULTILINE
        elif p == "DOTALL":
            flags |= re.DOTALL
        elif p == "UNICODE":
            flags |= re.UNICODE
        else:
            # неизвестный флаг — игнорируем (чтобы не падать)
            pass
    return flags


# -------------------------
# ФУНКЦИИ ДЛЯ func-replacements (1:1 с твоим текущим кодом)
# -------------------------
def _ru_to_lat_stage(ch: str) -> str:
    # только для IIA/IIIB и т.п.
    return {"А": "A", "В": "B", "С": "C"}.get(ch, ch)


def _ru_to_lat_x(ch: str) -> str:
    # кириллица -> латиница только для x/X (и иногда Nх)
    return {"х": "x", "Х": "X"}.get(ch, ch)


def _func_ntrk_group(m: re.Match[str]) -> str:
    return f"NTRK{m.group(1)}"


def _func_stage_ru_to_lat(m: re.Match[str]) -> str:
    # m.group(1) = I|II|III|IV ; m.group(2) = А|В|С
    return f"{m.group(1)}{_ru_to_lat_stage(m.group(2))}"


def _func_tnm_c(m: re.Match[str]) -> str:
    # m.group(1)=T, m.group(2)=N, m.group(3)=M
    return f"cT{m.group(1)}N{_ru_to_lat_x(m.group(2))}M{_ru_to_lat_x(m.group(3))}"


def _func_tnm_cp(m: re.Match[str]) -> str:
    # m.group(1)=c|p, m.group(2)=T, m.group(3)=N, m.group(4)=M
    return f"{m.group(1)}T{m.group(2)}N{_ru_to_lat_x(m.group(3))}M{_ru_to_lat_x(m.group(4))}"


_FUNC_MAP: Dict[str, Callable[[re.Match[str]], str]] = {
    "NTRK_GROUP": _func_ntrk_group,
    "STAGE_RU_TO_LAT": _func_stage_ru_to_lat,
    "TNM_C": _func_tnm_c,
    "TNM_CP": _func_tnm_cp,
    "MMR_DP": MMR_DP,
    # RU_TO_LAT_X как func-replacement не нужен напрямую (он внутри TNM_*),
    # но оставим на будущее:
    # "RU_TO_LAT_X": lambda m: _ru_to_lat_x(m.group(0)),
}


# -------------------------
# 5) служебные регулярки (как у тебя)
# -------------------------
_RE_SPACES = re.compile(r"[ \t\u00A0\u2007\u202F]+")
_RE_HYPHEN_WRAP = re.compile(r"([А-Яа-яA-Za-z])-\n([А-Яа-яA-Za-z])")


def _collapse_spaces(s: str) -> str:
    return _RE_SPACES.sub(" ", s).strip()


def _collapse_empty_lines(lines: List[str]) -> List[str]:
    out: List[str] = []
    prev_empty = False
    for ln in lines:
        if ln == "":
            if not prev_empty:
                out.append("")
            prev_empty = True
        else:
            out.append(ln)
            prev_empty = False
    return out


def _unwrap_wrapped_lines(lines: List[str]) -> List[str]:
    """
    Склеивает 'обёрнутые' строки (часто после PDF extraction),
    не трогает строки таблиц с ' | ' и не склеивает, если строка заканчивается .!?:;
    """
    out: List[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            out.append(buf.strip())
            buf = ""

    END_PUNCT = (".", "!", "?", ":", ";")

    for ln in lines:
        if ln == "":
            flush()
            out.append("")
            continue

        if " | " in ln:
            flush()
            out.append(ln)
            continue

        if not buf:
            buf = ln
            continue

        if buf.endswith(END_PUNCT):
            flush()
            buf = ln
            continue

        # если новая строка начинается со строчной буквы — вероятно перенос
        if re.match(r"^[а-яa-z]", ln):
            buf = f"{buf} {ln}"
        else:
            if len(buf) < 80:
                buf = f"{buf} {ln}"
            else:
                flush()
                buf = ln

    flush()
    return out


# -------------------------
# LEXICON LOADER (JSON -> compiled rules)
# -------------------------
@lru_cache(maxsize=1)
def _load_lexicon_compiled() -> dict:
    """
    Загружает JSON-словарь и компилирует правила.
    Возвращает dict с ключами:
      - literal: Dict[str,str]
      - special_after_literal: List[Tuple[Pattern,Replacement]]
      - safe: List[Tuple[Pattern,Replacement]]
      - clinical: List[Tuple[Pattern,Replacement]]
      - units: List[Tuple[Pattern,Replacement]]
    """
    if not LEXICON_PATH.exists():
        raise FileNotFoundError(
            f"Не найден lexicon JSON: {LEXICON_PATH}. "
            f"Создай файл или выставь MED_LEXICON_PATH."
        )

    data = json.loads(LEXICON_PATH.read_text(encoding="utf-8"))

    # 1) literal
    literal: Dict[str, str] = {}
    for item in data.get("literal_replacements", []):
        literal[str(item["from"])] = str(item["to"])

    # helper: compile rule list
    def compile_rules(items: List[dict]) -> List[Tuple[Pattern[str], Replacement]]:
        out: List[Tuple[Pattern[str], Replacement]] = []
        for it in items:
            pat = str(it["pattern"])
            flags = _flags_from_str(it.get("flags", "IGNORECASE"))
            rx = re.compile(pat, flags)

            # replacement
            if it.get("to_type") == "func":
                func_name = str(it.get("func", "")).strip()
                if func_name not in _FUNC_MAP:
                    raise ValueError(f"В lexicon указан неизвестный func '{func_name}' для pattern={pat}")
                repl: Replacement = _FUNC_MAP[func_name]
            else:
                repl = str(it["to"])
            out.append((rx, repl))
        return out

    # 2) special fixes after literal (эквивалент твоих доп. subn в _apply_literal)
    special_after_literal = compile_rules(data.get("special_regex_fixes_after_literal", []))

    # 3) groups
    groups = data.get("regex_groups", {}) or {}
    safe = compile_rules(groups.get("safe", []))
    clinical = compile_rules(groups.get("clinical", []))
    units = compile_rules(groups.get("units", []))

    return {
        "literal": literal,
        "special_after_literal": special_after_literal,
        "safe": safe,
        "clinical": clinical,
        "units": units,
    }


def _apply_literal(text: str, log: Optional[List[Change]]) -> str:
    lex = _load_lexicon_compiled()

    # 1) literal replaces
    literal: Dict[str, str] = lex["literal"]
    for a, b in literal.items():
        if a in text:
            cnt = text.count(a)
            text = text.replace(a, b)
            if log is not None and cnt:
                log.append(Change("literal", a, b, cnt))

    # 2) special regex fixes after literal (No -> №, двойные пробелы после №)
    for pattern, repl in lex["special_after_literal"]:
        new_text, cnt = pattern.subn(repl, text)
        if cnt and log is not None:
            log.append(Change("regex", pattern.pattern, str(repl), cnt))
        text = new_text

    return text


def _apply_regex_rules(
    text: str,
    rules: Iterable[Tuple[Pattern[str], Replacement]],
    log: Optional[List[Change]],
) -> str:
    for pattern, repl in rules:
        new_text, cnt = pattern.subn(repl, text)
        if cnt and log is not None:
            log.append(Change("regex", pattern.pattern, str(repl), cnt))
        text = new_text
    return text


def normalize_med_text(
    text: str,
    *,
    clinical: bool = True,
    unwrap_lines: bool = True,
    return_log: bool = False,
) -> tuple[str, Optional[List[Change]]]:
    """
    Нормализация мед. текста перед извлечением фактов.

    clinical=False -> safe + units
    clinical=True  -> safe + units + clinical
    unwrap_lines=True -> склеивает переносы строк (полезно для PDF)
    return_log=True -> вернуть список изменений (Change)
    """
    if not text:
        return "", ([] if return_log else None)

    changes: Optional[List[Change]] = [] if return_log else None

    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # переносы слов: "опухо-\nль" -> "опухоль"
    text = _RE_HYPHEN_WRAP.sub(r"\1\2", text)

    # первичная чистка пробелов (не трогаем \n)
    text = _RE_SPACES.sub(" ", text)

    # literal + спец-фиксы
    text = _apply_literal(text, changes)

    lex = _load_lexicon_compiled()

    # базовая унификация
    text = _apply_regex_rules(text, lex["safe"], changes)

    # единицы/дозы
    text = _apply_regex_rules(text, lex["units"], changes)

    # клинические синонимы (по желанию)
    if clinical:
        text = _apply_regex_rules(text, lex["clinical"], changes)

    # пост-очистка по строкам
    lines = [ln.strip() for ln in text.split("\n")]
    lines = _collapse_empty_lines(lines)
    if unwrap_lines:
        lines = _unwrap_wrapped_lines(lines)

    out = "\n".join(lines).strip()
    out = "\n".join(_collapse_spaces(ln) for ln in out.split("\n")).strip()

    return out, changes


# -------------------------
# BACKWARD COMPAT: то, что может ждать старый код (facts_to_json.py)
# -------------------------
def apply_replacements(text: str) -> str:
    """
    Старый интерфейс: возвращает только строку.
    Используется в facts_to_json.py как apply_replacements(raw_text)
    """
    out, _ = normalize_med_text(text, clinical=True, unwrap_lines=True, return_log=False)
    return out
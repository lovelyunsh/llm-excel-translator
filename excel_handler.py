"""
Excel file handler — reads/writes .xlsx while preserving all styles,
merged cells, column widths, row heights, images, etc.
"""

from __future__ import annotations

import io
import re
import unicodedata
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from translator import translate_texts, TranslatorConfig, LANGUAGE_MAP

_NUMBER_ONLY = re.compile(r'^[\d\s,.\-+%/:()~=<>]+$')

_LANG_UNICODE_RANGES: dict[str, tuple[int, int]] = {
    "한국어": (0xAC00, 0xD7A3),     # Hangul syllables
    "일본어": (0x3040, 0x309F),     # Hiragana (also Katakana 30A0-30FF, Kanji shared with Chinese)
    "중국어 (간체)": (0x4E00, 0x9FFF),  # CJK Unified
    "중국어 (번체)": (0x4E00, 0x9FFF),
    "태국어": (0x0E00, 0x0E7F),
    "베트남어": (0x00C0, 0x024F),   # Latin Extended (Vietnamese diacritics)
}


def _has_chars_in_range(text: str, start: int, end: int) -> bool:
    return any(start <= ord(c) <= end for c in text)


def _needs_translation(text: str, source_lang: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False

    if _NUMBER_ONLY.match(stripped):
        return False

    if len(stripped) <= 1 and not stripped.isalpha():
        return False

    lang_range = _LANG_UNICODE_RANGES.get(source_lang)
    if lang_range:
        return _has_chars_in_range(stripped, lang_range[0], lang_range[1])

    return any(c.isalpha() for c in stripped)


def _collect_texts(wb, source_lang: str) -> dict[str, list[tuple[int, int, str]]]:
    sheet_texts: dict[str, list[tuple[int, int, str]]] = {}

    for ws_name in wb.sheetnames:
        ws = wb[ws_name]
        cells = []
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell, MergedCell):
                    continue
                if cell.value is not None and isinstance(cell.value, str) and _needs_translation(cell.value, source_lang):
                    cells.append((cell.row, cell.column, cell.value))
        if cells:
            sheet_texts[ws_name] = cells

    return sheet_texts


def translate_workbook(
    input_path: str | Path | io.BytesIO,
    source_lang: str,
    target_lang: str,
    domain: str = "일반",
    config: TranslatorConfig | None = None,
    batch_size: int = 50,
    progress_callback=None,
) -> io.BytesIO:
    wb = load_workbook(input_path)
    sheet_texts = _collect_texts(wb, source_lang)

    all_entries = []
    for ws_name, cells in sheet_texts.items():
        for row, col, text in cells:
            all_entries.append((ws_name, row, col, text))

    if not all_entries:
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return output

    # Phase 1: Extract unique texts only
    unique_texts = list({entry[3] for entry in all_entries})
    total_unique = len(unique_texts)
    cache: dict[str, str] = {}

    # Phase 2: Translate unique texts in batches (API calls here only)
    for i in range(0, total_unique, batch_size):
        batch = unique_texts[i:i + batch_size]
        translated_batch = translate_texts(
            batch, source_lang, target_lang, domain,
            config=config, batch_size=batch_size, cache=cache,
        )
        for original, translated in zip(batch, translated_batch):
            cache[original] = translated

        if progress_callback:
            done = min(i + batch_size, total_unique)
            progress_callback(done, total_unique)

    # Phase 3: Apply cached translations to all cells (fast dict lookup)
    for ws_name, row, col, original in all_entries:
        ws = wb[ws_name]
        ws.cell(row=row, column=col).value = cache.get(original, original)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def get_workbook_info(input_path: str | Path | io.BytesIO, source_lang: str = "한국어") -> dict:
    wb = load_workbook(input_path, read_only=True)
    info: dict = {
        "sheets": [],
        "total_text_cells": 0,
        "total_all_cells": 0,
        "unique_texts": 0,
    }

    unique_set: set[str] = set()

    for ws_name in wb.sheetnames:
        ws = wb[ws_name]
        all_count = 0
        translatable_count = 0
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is not None and isinstance(cell.value, str) and cell.value.strip():
                    all_count += 1
                    if _needs_translation(cell.value, source_lang):
                        translatable_count += 1
                        unique_set.add(cell.value)
        info["sheets"].append({"name": ws_name, "text_cells": translatable_count, "all_cells": all_count})
        info["total_text_cells"] += translatable_count
        info["total_all_cells"] += all_count

    info["unique_texts"] = len(unique_set)
    wb.close()
    return info

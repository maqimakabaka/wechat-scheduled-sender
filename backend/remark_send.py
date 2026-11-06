# -*- coding: utf-8 -*-
"""联系人备注匹配及按发送账号去重。"""
from __future__ import annotations

import os
import re
import sqlite3
import time
import unicodedata
from contextlib import closing


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def parse_keywords(raw) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[\n,，]+", raw)
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError("请输入备注关键词，每行一个")
    terms = {}
    for item in raw:
        term = item.strip()
        if term:
            terms.setdefault(_fold(term), term)
    if not terms:
        raise ValueError("请至少输入一个备注关键词")
    return list(terms.values())


def matches(friends: list[dict], keywords: list[str]) -> list[dict]:
    patterns = [(word, re.compile(r"(?<!\w)" + re.escape(_fold(word)) + r"(?!\w)"))
                for word in keywords]
    result = []
    for friend in friends:
        remark = _fold(friend.get("remark") or "")
        hits = [word for word, pattern in patterns if pattern.search(remark)]
        if hits:
            result.append({**friend, "keywords": hits})
    return result


def _ledger_path(data_dir: str) -> str:
    return os.path.join(data_dir, "remark-send.sqlite3")


def attempted(data_dir: str, account: str) -> set[str]:
    path = _ledger_path(data_dir)
    if not os.path.isfile(path):
        return set()
    with closing(sqlite3.connect(path)) as conn:
        return {row[0] for row in conn.execute(
            "SELECT recipient FROM attempts WHERE account = ?", (account,))}


def reserve(data_dir: str, account: str, recipient: str) -> bool:
    """先提交再点击发送；即使结果不明或程序退出，仍保留这次尝试。"""
    with closing(sqlite3.connect(_ledger_path(data_dir))) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS attempts (
            account TEXT NOT NULL, recipient TEXT NOT NULL, attempted_at INTEGER NOT NULL,
            PRIMARY KEY (account, recipient)
        )""")
        cursor = conn.execute(
            "INSERT OR IGNORE INTO attempts VALUES (?, ?, ?)",
            (account, recipient, int(time.time())),
        )
        return cursor.rowcount == 1

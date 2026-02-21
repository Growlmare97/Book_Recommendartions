from __future__ import annotations

import sqlite3


def book_score(book: sqlite3.Row, preferences: dict[str, int], fav_authors: dict[str, int]) -> int:
    score = preferences.get(book["category"].lower(), 0) * 3
    score += fav_authors.get(book["author"].lower(), 0) * 2
    return score


def calculate_recommendations(books: list[sqlite3.Row], favourites: list[sqlite3.Row]) -> list[sqlite3.Row]:
    category_pref: dict[str, int] = {}
    author_pref: dict[str, int] = {}
    for fav in favourites:
        category_pref[fav["category"].lower()] = category_pref.get(fav["category"].lower(), 0) + 1
        author_pref[fav["author"].lower()] = author_pref.get(fav["author"].lower(), 0) + 1

    fav_keys = {(f["title"].lower(), f["author"].lower()) for f in favourites}
    scored = []
    for book in books:
        if (book["title"].lower(), book["author"].lower()) in fav_keys:
            continue
        scored.append((book_score(book, category_pref, author_pref), book))

    scored.sort(key=lambda pair: (pair[0], pair[1]["title"]), reverse=True)
    return [book for _, book in scored]

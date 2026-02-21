import sqlite3
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from recommender import calculate_recommendations


def row(title: str, author: str, category: str) -> sqlite3.Row:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE books (title TEXT, author TEXT, category TEXT)")
    con.execute("INSERT INTO books VALUES (?, ?, ?)", (title, author, category))
    return con.execute("SELECT * FROM books").fetchone()


def test_recommendations_prioritize_favourite_author_and_category():
    books = [
        row("Book A", "Fav Author", "fantasy"),
        row("Book B", "Other", "fantasy"),
        row("Book C", "Other", "history"),
    ]
    favourites = [row("Old", "Fav Author", "fantasy")]

    results = calculate_recommendations(books, favourites)

    assert results[0]["title"] == "Book A"
    assert results[-1]["title"] == "Book C"

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from flask import Flask, flash, g, redirect, render_template, request, url_for

from recommender import calculate_recommendations

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "books.db"

app = Flask(__name__)
app.config["SECRET_KEY"] = "dev-secret-change-me"


@dataclass
class Book:
    title: str
    author: str
    category: str
    goodreads_id: str | None = None
    cover_url: str | None = None


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: object) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            category TEXT NOT NULL,
            goodreads_id TEXT,
            cover_url TEXT,
            imported_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS favourites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            category TEXT NOT NULL,
            goodreads_id TEXT,
            cover_url TEXT,
            added_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS weekly_recommendation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            category TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    db.commit()
    db.close()


def parse_user_id(profile_input: str) -> str:
    profile_input = profile_input.strip()
    if profile_input.isdigit():
        return profile_input
    match = re.search(r"goodreads\.com/user/show/(\d+)", profile_input)
    if match:
        return match.group(1)
    raise ValueError("Enter a Goodreads user ID or profile URL like https://www.goodreads.com/user/show/12345")


def scrape_goodreads_books(user_id: str, max_pages: int = 2) -> list[Book]:
    books: list[Book] = []
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BookRecommenderBot/1.0)"}

    for page in range(1, max_pages + 1):
        url = f"https://www.goodreads.com/review/list/{user_id}?shelf=read&page={page}"
        response = session.get(url, timeout=20, headers=headers)
        if response.status_code != 200:
            break

        soup = BeautifulSoup(response.text, "html.parser")
        rows = soup.select("tr.bookalike.review")
        if not rows:
            rows = soup.select("tr[id^='review_']")
        if not rows:
            break

        for row in rows:
            title_el = row.select_one("td.field.title a.bookTitle") or row.select_one("a.bookTitle")
            author_el = row.select_one("td.field.author a.authorName") or row.select_one("a.authorName")
            if not title_el or not author_el:
                continue

            title = " ".join(title_el.get_text(strip=True).split())
            author = " ".join(author_el.get_text(strip=True).split())

            shelves_el = row.select_one("td.field.shelves")
            category = "General"
            if shelves_el:
                shelf_tags = [s.get_text(strip=True) for s in shelves_el.select("a") if s.get_text(strip=True)]
                if shelf_tags:
                    category = shelf_tags[0]

            href = title_el.get("href", "")
            id_match = re.search(r"/book/show/(\d+)", href)
            goodreads_id = id_match.group(1) if id_match else None

            cover_el = row.select_one("td.field.cover img") or row.select_one("img.bookSmallImg")
            cover_url = cover_el.get("src") if cover_el else None

            books.append(Book(title=title, author=author, category=category, goodreads_id=goodreads_id, cover_url=cover_url))

    unique: dict[tuple[str, str], Book] = {}
    for book in books:
        unique[(book.title.lower(), book.author.lower())] = book
    return list(unique.values())


def import_books(books: Iterable[Book]) -> int:
    now = dt.datetime.utcnow().isoformat()
    db = get_db()
    db.execute("DELETE FROM books")
    db.executemany(
        """
        INSERT INTO books (title, author, category, goodreads_id, cover_url, imported_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(b.title, b.author, b.category, b.goodreads_id, b.cover_url, now) for b in books],
    )
    db.commit()
    return db.execute("SELECT COUNT(*) FROM books").fetchone()[0]




def get_or_create_weekly_recommendation(recommendations: list[sqlite3.Row]) -> sqlite3.Row | None:
    if not recommendations:
        return None

    db = get_db()
    week_key = dt.date.today().strftime("%Y-W%U")
    existing = db.execute("SELECT * FROM weekly_recommendation WHERE week_key = ?", (week_key,)).fetchone()
    if existing:
        return existing

    top = recommendations[0]
    reason = f"Strong match because you like {top['category']} books and/or author patterns from favourites."
    now = dt.datetime.utcnow().isoformat()
    db.execute(
        """
        INSERT INTO weekly_recommendation (week_key, title, author, category, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (week_key, top["title"], top["author"], top["category"], reason, now),
    )
    db.commit()
    return db.execute("SELECT * FROM weekly_recommendation WHERE week_key = ?", (week_key,)).fetchone()


@app.route("/", methods=["GET", "POST"])
def index():
    db = get_db()
    if request.method == "POST":
        profile = request.form.get("profile", "")
        try:
            user_id = parse_user_id(profile)
            books = scrape_goodreads_books(user_id)
            if not books:
                flash("No books found. Make sure your Goodreads profile is public.", "error")
            else:
                count = import_books(books)
                flash(f"Imported {count} books from Goodreads.", "success")
        except Exception as exc:  # noqa: BLE001
            flash(f"Unable to import profile: {exc}", "error")
        return redirect(url_for("index"))

    category_filter = request.args.get("category", "").strip()
    author_filter = request.args.get("author", "").strip()
    title_filter = request.args.get("title", "").strip()

    query = "SELECT * FROM books WHERE 1=1"
    params: list[str] = []
    if category_filter:
        query += " AND LOWER(category) LIKE ?"
        params.append(f"%{category_filter.lower()}%")
    if author_filter:
        query += " AND LOWER(author) LIKE ?"
        params.append(f"%{author_filter.lower()}%")
    if title_filter:
        query += " AND LOWER(title) LIKE ?"
        params.append(f"%{title_filter.lower()}%")

    query += " ORDER BY title"

    books = db.execute(query, params).fetchall()
    favourites = db.execute("SELECT * FROM favourites ORDER BY added_at DESC").fetchall()
    recommendations = calculate_recommendations(books, favourites)
    weekly = get_or_create_weekly_recommendation(recommendations)

    categories = db.execute("SELECT DISTINCT category FROM books ORDER BY category").fetchall()
    authors = db.execute("SELECT DISTINCT author FROM books ORDER BY author LIMIT 100").fetchall()

    return render_template(
        "index.html",
        books=books,
        favourites=favourites,
        recommendations=recommendations[:10],
        weekly=weekly,
        categories=[c["category"] for c in categories],
        authors=[a["author"] for a in authors],
        filters={"category": category_filter, "author": author_filter, "title": title_filter},
    )


@app.route("/favourite", methods=["POST"])
def add_favourite():
    db = get_db()
    book_id = request.form.get("book_id")
    book = db.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if not book:
        flash("Book not found.", "error")
        return redirect(url_for("index"))

    exists = db.execute(
        "SELECT 1 FROM favourites WHERE LOWER(title)=? AND LOWER(author)=?",
        (book["title"].lower(), book["author"].lower()),
    ).fetchone()
    if exists:
        flash("Already in favourites.", "info")
        return redirect(url_for("index"))

    db.execute(
        """
        INSERT INTO favourites (title, author, category, goodreads_id, cover_url, added_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (book["title"], book["author"], book["category"], book["goodreads_id"], book["cover_url"], dt.datetime.utcnow().isoformat()),
    )
    db.commit()
    flash("Added to favourites.", "success")
    return redirect(url_for("index"))


@app.route("/favourite/delete", methods=["POST"])
def remove_favourite():
    db = get_db()
    fav_id = request.form.get("fav_id")
    db.execute("DELETE FROM favourites WHERE id = ?", (fav_id,))
    db.commit()
    flash("Removed favourite.", "info")
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)

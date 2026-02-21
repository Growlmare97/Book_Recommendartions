from __future__ import annotations

import datetime as dt
import html
import re
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable

from recommender import calculate_recommendations

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "books.db"


@dataclass
class Book:
    title: str
    author: str
    category: str
    goodreads_id: str | None = None
    cover_url: str | None = None


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    db = get_db()
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

        CREATE TABLE IF NOT EXISTS flash_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    db.commit()
    db.close()


def add_flash(category: str, message: str) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO flash_messages (category, message, created_at) VALUES (?, ?, ?)",
        (category, message, dt.datetime.utcnow().isoformat()),
    )
    db.commit()
    db.close()


def pop_flash_messages() -> list[sqlite3.Row]:
    db = get_db()
    msgs = db.execute("SELECT id, category, message FROM flash_messages ORDER BY id").fetchall()
    db.execute("DELETE FROM flash_messages")
    db.commit()
    db.close()
    return msgs


def parse_user_id(profile_input: str) -> str:
    profile_input = profile_input.strip()
    if profile_input.isdigit():
        return profile_input
    match = re.search(r"goodreads\.com/user/show/(\d+)", profile_input)
    if match:
        return match.group(1)
    raise ValueError("Enter a Goodreads user ID or profile URL like https://www.goodreads.com/user/show/12345")


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def scrape_goodreads_books(user_id: str, max_pages: int = 2) -> list[Book]:
    books: list[Book] = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BookRecommenderBot/1.0)"}

    for page in range(1, max_pages + 1):
        url = f"https://www.goodreads.com/review/list/{user_id}?shelf=read&page={page}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status != HTTPStatus.OK:
                    break
                body = response.read().decode("utf-8", errors="ignore")
        except Exception:
            break

        row_matches = re.findall(r"<tr[^>]*id=\"review_\d+\"[\s\S]*?</tr>", body)
        if not row_matches:
            break

        for row in row_matches:
            title_match = re.search(r'href="(/book/show/\d+[^\"]*)"[^>]*><span[^>]*>(.*?)</span>', row)
            author_match = re.search(r'class="authorName"[^>]*>\s*<span[^>]*>(.*?)</span>', row)
            if not title_match or not author_match:
                continue

            href = html.unescape(title_match.group(1))
            title = html.unescape(strip_tags(title_match.group(2))).strip()
            author = html.unescape(strip_tags(author_match.group(1))).strip()

            shelf_tags = re.findall(r'<a[^>]*class="actionLinkLite"[^>]*>([^<]+)</a>', row)
            category = shelf_tags[0].strip() if shelf_tags else "General"

            id_match = re.search(r"/book/show/(\d+)", href)
            goodreads_id = id_match.group(1) if id_match else None

            books.append(Book(title=title, author=author, category=category, goodreads_id=goodreads_id))

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
    count = db.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    db.close()
    return count


def get_or_create_weekly_recommendation(recommendations: list[sqlite3.Row]) -> sqlite3.Row | None:
    if not recommendations:
        return None

    db = get_db()
    week_key = dt.date.today().strftime("%Y-W%U")
    existing = db.execute("SELECT * FROM weekly_recommendation WHERE week_key = ?", (week_key,)).fetchone()
    if existing:
        db.close()
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
    row = db.execute("SELECT * FROM weekly_recommendation WHERE week_key = ?", (week_key,)).fetchone()
    db.close()
    return row


def render_page(params: dict[str, str] | None = None) -> str:
    params = params or {}
    category_filter = params.get("category", "").strip()
    author_filter = params.get("author", "").strip()
    title_filter = params.get("title", "").strip()

    db = get_db()
    query = "SELECT * FROM books WHERE 1=1"
    args: list[str] = []
    if category_filter:
        query += " AND LOWER(category) LIKE ?"
        args.append(f"%{category_filter.lower()}%")
    if author_filter:
        query += " AND LOWER(author) LIKE ?"
        args.append(f"%{author_filter.lower()}%")
    if title_filter:
        query += " AND LOWER(title) LIKE ?"
        args.append(f"%{title_filter.lower()}%")
    query += " ORDER BY title"

    books = db.execute(query, args).fetchall()
    favourites = db.execute("SELECT * FROM favourites ORDER BY added_at DESC").fetchall()
    recommendations = calculate_recommendations(books, favourites)
    weekly = get_or_create_weekly_recommendation(recommendations)
    flashes = pop_flash_messages()
    db.close()

    def esc(s: str) -> str:
        return html.escape(s)

    flash_html = "".join(
        f'<div class="flash {esc(m["category"])}">{esc(m["message"])}</div>' for m in flashes
    )

    books_html = "".join(
        (
            "<li><div><strong>"
            + esc(b["title"])
            + "</strong><br /><small>"
            + esc(b["author"])
            + " • "
            + esc(b["category"])
            + "</small></div>"
            + f'<form method="post" action="/favourite"><input type="hidden" name="book_id" value="{b["id"]}" /><button type="submit">☆ Favourite</button></form></li>'
        )
        for b in books
    ) or "<li>No books imported yet.</li>"

    fav_html = "".join(
        (
            "<li><div><strong>"
            + esc(f["title"])
            + "</strong><br /><small>"
            + esc(f["author"])
            + " • "
            + esc(f["category"])
            + "</small></div>"
            + f'<form method="post" action="/favourite/delete"><input type="hidden" name="fav_id" value="{f["id"]}" /><button type="submit" class="delete">Remove</button></form></li>'
        )
        for f in favourites
    ) or "<li>No favourites yet.</li>"

    rec_html = "".join(
        f"<li>{esc(r['title'])} — {esc(r['author'])} ({esc(r['category'])})</li>" for r in recommendations[:10]
    ) or "<li>No recommendations yet.</li>"

    weekly_html = (
        f"<p><strong>{esc(weekly['title'])}</strong> by {esc(weekly['author'])} ({esc(weekly['category'])})</p><p>{esc(weekly['reason'])}</p>"
        if weekly
        else "<p>Add favourites and import books to unlock your weekly recommendation.</p>"
    )

    return f"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"UTF-8\" /><meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
<title>Goodreads Book Recommender</title><link rel=\"stylesheet\" href=\"/static/styles.css\" /></head>
<body><main class=\"container\"><h1>Goodreads Book Recommender</h1>
<p>Import your public Goodreads profile, filter books, save favourites, and get a weekly pick.</p>
{flash_html}
<section class=\"card\"><h2>1) Import Goodreads profile</h2>
<form method=\"post\" action=\"/import\" class=\"row\"><input type=\"text\" name=\"profile\" placeholder=\"Goodreads profile URL or numeric user id\" required />
<button type=\"submit\">Import</button></form></section>
<section class=\"card\"><h2>2) Filter imported books</h2>
<form method=\"get\" action=\"/\" class=\"filters\">
<input type=\"text\" name=\"title\" value=\"{esc(title_filter)}\" placeholder=\"Title contains...\" />
<input type=\"text\" name=\"author\" value=\"{esc(author_filter)}\" placeholder=\"Author contains...\" />
<input type=\"text\" name=\"category\" value=\"{esc(category_filter)}\" placeholder=\"Category contains...\" />
<button type=\"submit\">Apply</button></form></section>
<section class=\"grid\"><div class=\"card\"><h2>Imported books ({len(books)})</h2><ul class=\"book-list\">{books_html}</ul></div>
<div class=\"card\"><h2>Your favourites ({len(favourites)})</h2><ul class=\"book-list\">{fav_html}</ul></div></section>
<section class=\"card\"><h2>Weekly recommendation</h2>{weekly_html}</section>
<section class=\"card\"><h2>Top recommendations</h2><ol>{rec_html}</ol></section></main></body></html>"""


class AppHandler(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect_home(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/static/styles.css"):
            css = (BASE_DIR / "static" / "styles.css").read_text(encoding="utf-8")
            self._send(css, content_type="text/css; charset=utf-8")
            return

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/":
            self._send("Not Found", status=404, content_type="text/plain; charset=utf-8")
            return

        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        self._send(render_page(params))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

        if self.path == "/import":
            profile = form.get("profile", "")
            try:
                user_id = parse_user_id(profile)
                books = scrape_goodreads_books(user_id)
                if not books:
                    add_flash("error", "No books found. Make sure your Goodreads profile is public.")
                else:
                    count = import_books(books)
                    add_flash("success", f"Imported {count} books from Goodreads.")
            except Exception as exc:  # noqa: BLE001
                add_flash("error", f"Unable to import profile: {exc}")
            self._redirect_home()
            return

        db = get_db()
        if self.path == "/favourite":
            book_id = form.get("book_id", "")
            book = db.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
            if not book:
                add_flash("error", "Book not found.")
            else:
                exists = db.execute(
                    "SELECT 1 FROM favourites WHERE LOWER(title)=? AND LOWER(author)=?",
                    (book["title"].lower(), book["author"].lower()),
                ).fetchone()
                if exists:
                    add_flash("info", "Already in favourites.")
                else:
                    db.execute(
                        """
                        INSERT INTO favourites (title, author, category, goodreads_id, cover_url, added_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (book["title"], book["author"], book["category"], book["goodreads_id"], book["cover_url"], dt.datetime.utcnow().isoformat()),
                    )
                    db.commit()
                    add_flash("success", "Added to favourites.")
            db.close()
            self._redirect_home()
            return

        if self.path == "/favourite/delete":
            fav_id = form.get("fav_id", "")
            db.execute("DELETE FROM favourites WHERE id = ?", (fav_id,))
            db.commit()
            db.close()
            add_flash("info", "Removed favourite.")
            self._redirect_home()
            return

        db.close()
        self._send("Not Found", status=404, content_type="text/plain; charset=utf-8")


def main() -> None:
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", 5000), AppHandler)
    print("Serving on http://localhost:5000")
    server.serve_forever()


if __name__ == "__main__":
    main()

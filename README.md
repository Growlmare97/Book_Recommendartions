# Goodreads Profile Book Recommender

A local web app that:
- Scrapes books from a **public Goodreads profile** (`read` shelf).
- Lets you filter imported books by title, author, and category.
- Lets you store favourites.
- Creates a **weekly recommendation** based on your favourite categories/authors.

## Run locally (no external packages required)

```bash
python app.py
```

Open `http://localhost:5000`.

## Notes
- Goodreads profile must be public.
- Use a profile URL like `https://www.goodreads.com/user/show/12345-name` or just the numeric user id.
- Data is stored in local SQLite file `books.db`.

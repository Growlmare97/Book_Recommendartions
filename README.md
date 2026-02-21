# Goodreads Profile Book Recommender

A Flask app that:
- Scrapes books from a **public Goodreads profile** (`read` shelf).
- Lets you filter imported books by title, author, and category.
- Lets you store favourites.
- Creates a **weekly recommendation** based on your favourite categories/authors.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000`.

## Notes
- Goodreads profile must be public.
- Use a profile URL like `https://www.goodreads.com/user/show/12345-name` or just the numeric user id.

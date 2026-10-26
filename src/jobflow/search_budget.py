"""Persistent, concurrency-safe limit; reserve before sending, even on failures."""
import sqlite3
import json
import time
from datetime import datetime, timezone
from pathlib import Path


class SearchBudget:
    def __init__(self, path='data/search-budget.db', limit=900):
        self.path = Path(path)
        self.limit = max(0, min(int(limit), 900))

    def _cache_schema(self, db):
        db.execute('CREATE TABLE IF NOT EXISTS search_cache (query TEXT PRIMARY KEY, urls TEXT, expires REAL NOT NULL)')

    def cached(self, query):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=10) as db:
            self._cache_schema(db)
            row = db.execute('SELECT urls FROM search_cache WHERE query=? AND expires>?', (query, time.time())).fetchone()
            return None if row is None else json.loads(row[0]) if row[0] is not None else []

    def save_results(self, query, urls):
        with sqlite3.connect(self.path, timeout=10) as db:
            self._cache_schema(db)
            db.execute('INSERT OR REPLACE INTO search_cache VALUES (?,?,?)',
                       (query, json.dumps(urls), time.time() + 7 * 86400))

    def cached_entries(self, prefix):
        """Read usable evidence only; expired and in-flight claims are excluded."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=10) as db:
            self._cache_schema(db)
            return [(query, json.loads(raw)) for query, raw in db.execute(
                'SELECT query,urls FROM search_cache WHERE substr(query,1,?)=? '
                'AND expires>? AND urls IS NOT NULL ORDER BY expires DESC',
                (len(prefix), prefix, time.time()))]

    def reserve(self, query=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        month = datetime.now(timezone.utc).strftime('%Y-%m')
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('CREATE TABLE IF NOT EXISTS search_usage (month TEXT PRIMARY KEY, attempts INTEGER NOT NULL)')
            self._cache_schema(db)
            db.execute('BEGIN IMMEDIATE')
            if query and db.execute('SELECT 1 FROM search_cache WHERE query=? AND expires>?', (query, time.time())).fetchone():
                return False
            db.execute('INSERT OR IGNORE INTO search_usage VALUES (?,0)', (month,))
            changed = db.execute('UPDATE search_usage SET attempts=attempts+1 WHERE month=? AND attempts<?',
                                 (month, self.limit)).rowcount
            if changed and query:
                # Claim before network I/O: competing runs and failed requests
                # cannot repeat this search for 24 hours.
                db.execute('INSERT OR REPLACE INTO search_cache VALUES (?,NULL,?)', (query, time.time() + 86400))
            return bool(changed)

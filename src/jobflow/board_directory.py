"""Public directory seeds; historical listing is not proof of current liveness."""
import csv
import io
import json
import re
from pathlib import Path

SOURCES = {
    'LastRound AI (CC BY 4.0)': 'https://raw.githubusercontent.com/fyrosofttech/lastroundai-hiring-data/main/ats-directory/lastroundai-ats-company-directory-2026-08.csv',
    'Open ATS Feed': 'https://raw.githubusercontent.com/veresk06/open-ats-feed/main/actor/data/companies.json',
}
PROVIDERS = ('greenhouse', 'lever', 'ashby')
DEFAULT_PATH = Path(__file__).resolve().parents[2] / 'config/public-ats-directory.json'


def merge_directory(csv_text, feed):
    rows = {}
    def add(provider, token, company, source, checked):
        token = str(token).strip()
        if provider not in PROVIDERS or not re.fullmatch(r'[A-Za-z0-9_.-]+', token):
            return
        key = provider, token.casefold()
        row = rows.setdefault(key, dict(provider=provider, board_token=token,
            company=company or token, sources=[], source_dates=[]))
        if source not in row['sources']:
            row['sources'].append(source)
        if checked and checked not in row['source_dates']:
            row['source_dates'].append(checked)
    for row in csv.DictReader(io.StringIO(csv_text)):
        add(row['ats_vendor'], row['board_slug'], row['company_name'],
            'LastRound AI (CC BY 4.0)', row.get('last_crawled'))
    for provider, data in feed['providers'].items():
        for token, count in data.get('live', []):
            add(provider, token, token, 'Open ATS Feed', feed.get('as_of'))
    return [rows[key] for key in sorted(rows)]


def extend_boards(existing, provider, path=DEFAULT_PATH):
    """Preserve existing labels and all distinct tokens, even with name collisions."""
    result = dict(existing)
    seen = {token.casefold() for token in result.values()}
    if not Path(path).is_file():
        return result
    for row in json.loads(Path(path).read_text())['boards']:
        if row['provider'] != provider or row['board_token'].casefold() in seen:
            continue
        token = row['board_token']
        label = row['company']
        if label in result:
            label = '{} [{}:{}]'.format(label, provider, token)
        while label in result:
            label += '_'
        result[label] = token
        seen.add(token.casefold())
    return result

import csv
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable

from .models import Compensation, JobInput


class JobSource(ABC):
    """Integration boundary for user-authorized job sources."""

    @abstractmethod
    def fetch(self) -> Iterable[JobInput]:
        raise NotImplementedError


class CSVJobSource(JobSource):
    REQUIRED = {"title", "company", "location", "url", "description"}

    def __init__(self, path: str):
        self.path = Path(path)

    def fetch(self) -> Iterable[JobInput]:
        with self.path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = self.REQUIRED - set(reader.fieldnames or ())
            if missing:
                raise ValueError("Missing CSV columns: {}".format(", ".join(sorted(missing))))
            for row_number, row in enumerate(reader, start=2):
                if not row["title"].strip() or not row["company"].strip():
                    raise ValueError("Row {} needs title and company".format(row_number))
                yield JobInput(
                    title=row["title"].strip(),
                    company=row["company"].strip(),
                    location=row["location"].strip(),
                    url=row["url"].strip(),
                    description=row["description"].strip(),
                    source=(row.get("source") or "csv").strip(),
                    external_id=(row.get("external_id") or "").strip() or None,
                    canonical_url=(row.get("canonical_url") or "").strip(),
                    workplace_type=(row.get("workplace_type") or "unspecified").strip(),
                    employment_type=(row.get("employment_type") or "full-time").strip(),
                    country=(row.get("country") or "").strip(),
                    language_requirements=(row.get("language_requirements") or "").strip(),
                    sponsorship=(row.get("sponsorship") or "unknown").strip(),
                    security_clearance=(row.get("security_clearance") or "").strip(),
                    posted_at=(row.get("posted_at") or "").strip(),
                    closes_at=(row.get("closes_at") or "").strip(),
                    compensation=_compensation(row),
                    raw_payload=json.dumps(row, sort_keys=True),
                )


def _number(value):
    value = (value or "").replace(",", "").replace("$", "").strip()
    return float(value) if value else None


def _compensation(row):
    if not (row.get("salary_min") or row.get("salary_max")):
        return None
    return Compensation(
        minimum=_number(row.get("salary_min")),
        maximum=_number(row.get("salary_max")),
        currency=(row.get("salary_currency") or "CAD").strip(),
        interval=(row.get("salary_interval") or "year").strip(),
        is_estimated=(row.get("salary_estimated") or "").strip().lower() in {"1", "true", "yes"},
        estimate_source=(row.get("salary_estimate_source") or "").strip(),
        estimate_confidence=(row.get("salary_estimate_confidence") or "").strip(),
        includes_bonus_or_equity=(row.get("salary_includes_bonus_or_equity") or "").strip().lower()
        in {"1", "true", "yes"},
    )

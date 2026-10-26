"""Read-only, company-independent Job Bank search (no login or application API)."""
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlencode


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


def visible_text(html):
    parser = _Text()
    parser.feed(html)
    return " ".join(parser.parts)


def search_url(query, page=1):
    # 15141 is the Remote checkbox's observed value, not a guessed filter.
    return "https://www.jobbank.gc.ca/jobsearch/jobsearch?" + urlencode(
        {"searchstring": query, "fskl": "15141", "page": page, "sort": "D"})


def parse_results(html):
    jobs = []
    for article in re.findall(r"<article\b[^>]*>[\s\S]*?</article>", html, re.I):
        job_id = re.search(r"/jobsearch/jobposting/(\d+)", article)
        if not job_id:
            continue
        def field(name):
            found = re.search(r'<(?:span|li)\b[^>]*class="' + name + r'"[^>]*>([\s\S]*?)</(?:span|li)>', article)
            return visible_text(found[1]) if found else ""
        jobs.append({"external_id": job_id[1], "title": field("noctitle"),
                     "company": field("business"), "summary": visible_text(article.split("</a>")[0]),
                     "url": "https://www.jobbank.gc.ca/jobsearch/jobposting/" + job_id[1]})
    zero = "We could not find any results for your search keywords." in visible_text(html)
    if not jobs and not zero:
        raise ValueError("Job Bank returned neither recognized job cards nor explicit zero results")
    return {"jobs": jobs, "explicit_zero_results": zero}

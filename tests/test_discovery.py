import json
import tempfile
import unittest
import httpx
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jobflow.discovery import DiscoveryService, _safe_error, excluded_job_urls
from jobflow.models import Compensation, JobInput
from jobflow.providers import (
    AshbyProvider, BraveProvider, DiscoveryProvider, GreenhouseProvider, LeverProvider, LinkedInLinkProvider,
    _country_code_from_location, aggregator_url, configured_providers, infer_compensation, inferred_ats_boards,
    normalize_employment_type, resolve_canonical_job, _target_index_match,
)
from jobflow.repository import Repository


class FixtureProvider(DiscoveryProvider):
    name = "fixture"

    def fetch(self):
        for number in range(35):
            yield JobInput(
                "Senior Data Engineer", "Company {}".format(number), "Canada Remote",
                "https://example.com/jobs/{}".format(number),
                "Build production Python SQL PySpark Databricks ETL pipelines, data validation, analytics, and reporting.",
                source=self.name, external_id=str(number), canonical_url="https://example.com/jobs/{}".format(number),
                workplace_type="remote", employment_type="permanent full-time", country="CA",
                compensation=Compensation(130000, 160000, "CAD"),
            )
        yield JobInput(
            "Senior Data Engineer", "Rejected", "Toronto, ON", "https://example.com/jobs/rejected",
            "Build Python SQL data pipelines.", source=self.name, external_id="rejected",
            canonical_url="https://example.com/jobs/rejected", workplace_type="on-site",
            employment_type="permanent full-time", country="CA", compensation=Compensation(130000, 160000, "CAD"),
        )


class DiscoveryTests(unittest.TestCase):
    def test_linkedin_gather_uses_only_search_index_and_deduplicates_ids(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"query": {"more_results_available": False}, "web": {"results": [
                    {"title": "Acme hiring Data Analyst in Ottawa, Ontario, Canada | LinkedIn",
                     "url": "https://ca.linkedin.com/jobs/view/data-analyst-at-acme-1234567890?trk=x",
                     "description": "SQL and Power BI reporting"},
                    {"title": "Acme hiring Data Analyst in Ottawa | LinkedIn",
                     "url": "https://www.linkedin.com/jobs/view/1234567890/", "description": "duplicate"},
                    {"title": "Data Analyst at Acme | LinkedIn",
                     "url": "https://example.com/jobs/view/1234567891", "description": "wrong site"},
                ]}}

        class Client:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, headers=None):
                self.urls.append(url)
                return Response()

        client = Client()
        jobs = list(LinkedInLinkProvider("test-key", client=client).fetch())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Data Analyst")
        self.assertEqual(jobs[0].company, "Acme")
        self.assertEqual(jobs[0].location, "Ottawa, Ontario, Canada")
        self.assertEqual(jobs[0].url, "https://www.linkedin.com/jobs/view/1234567890/")
        self.assertEqual(jobs[0].workplace_type, "unspecified")
        self.assertEqual(set(client.urls), {"https://api.search.brave.com/res/v1/web/search"})

    def test_prefilter_includes_adjacent_titles_and_province_only_locations(self):
        self.assertTrue(_target_index_match("Senior Business Intelligence Engineer", "Ontario, Canada"))
        self.assertTrue(_target_index_match("IT Systems Analyst", "Quebec"))
        self.assertTrue(_target_index_match("Data Platform Analyst", "Remote - United States"))
        self.assertFalse(_target_index_match("Data Analyst Manager", "Canada"))
        self.assertFalse(_target_index_match("Data Scientist", "Remote Canada"))
        # Geography is evaluated using full evidence after this cheap title gate.
        self.assertTrue(_target_index_match("Data Analyst", "Toronto, ON"))
        self.assertTrue(_target_index_match("Data Engineer", "Remote Europe"))
        self.assertTrue(_target_index_match("Data Analyst", "Spain - Remote"))
        self.assertTrue(_target_index_match("Data Analyst", "Remote - US"))
        self.assertEqual(_country_code_from_location("Remote - US"), "US")
        self.assertEqual(_country_code_from_location("Miami, FL"), "US")
        self.assertEqual(_country_code_from_location("Tampa"), "US")
        self.assertEqual(_country_code_from_location("Seattle"), "US")
        self.assertEqual(_country_code_from_location("Remote Europe"), "OTHER")

    def test_greenhouse_board_failures_are_recorded_without_stopping_other_boards(self):
        class Response:
            def __init__(self, jobs):
                self.jobs = jobs

            def raise_for_status(self):
                return None

            def json(self):
                return {"jobs": self.jobs}

        class Client:
            def get(self, url, params=None):
                if "/broken/" in url:
                    raise httpx.ConnectError("failed")
                return Response([])

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(str(Path(directory) / "jobs.db"))
            repository.init()
            provider = GreenhouseProvider({"Broken": "broken", "Working": "working"}, Client(), prefilter=True)
            run_id = DiscoveryService(repository, [provider]).run()
            summary = repository.discovery_board_summary(run_id)[0]
            self.assertEqual((summary["boards"], summary["failed"]), (2, 1))
            self.assertEqual(repository.failed_discovery_boards(run_id)[0]["board_token"], "broken")
            self.assertEqual(repository.discovery_run(run_id)["state"], "partial")

    def test_excluded_job_urls_are_normalized(self):
        with patch.dict("os.environ", {"JOBFLOW_EXCLUDED_JOB_URLS": "https://example.com/one/, https://example.com/two"}):
            self.assertEqual(excluded_job_urls(), {"https://example.com/one", "https://example.com/two"})

    def test_simplyhired_is_treated_as_an_unresolved_aggregator(self):
        self.assertTrue(aggregator_url("https://www.simplyhired.ca/job/example"))

    def test_ats_employment_type_enums_are_normalized(self):
        self.assertEqual(normalize_employment_type("FullTime"), "full-time")
        self.assertEqual(normalize_employment_type("PartTime"), "part-time")
        self.assertEqual(normalize_employment_type("CONTRACT"), "contract")

    def test_known_direct_urls_bootstrap_public_ats_providers(self):
        rows = [
            {"company": "Acme", "url": "https://job-boards.greenhouse.io/acme/jobs/123", "canonical_url": ""},
            {"company": "Beta", "url": "https://jobs.lever.co/beta/456", "canonical_url": ""},
            {"company": "Gamma", "url": "https://jobs.ashbyhq.com/gamma/789", "canonical_url": ""},
            {"company": "Jobgether", "url": "https://jobs.lever.co/jobgether/noise", "canonical_url": ""},
        ]
        boards = inferred_ats_boards(rows)
        self.assertEqual(boards["greenhouse"], {"Acme": "acme"})
        self.assertEqual(boards["lever"], {"Beta": "beta"})
        self.assertEqual(boards["ashby"], {"Gamma": "gamma"})

        with patch.dict("os.environ", {"JOBFLOW_DISCOVERY_SOURCES": "greenhouse,lever,ashby"}, clear=True):
            providers = configured_providers(known_jobs=rows)
        self.assertEqual([type(provider) for provider in providers], [
            GreenhouseProvider, LeverProvider, AshbyProvider,
        ])

    def test_greenhouse_registry_csv_configures_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "greenhouse.csv"
            registry.write_text("company,board_token\nAcme,acme\nBeta,beta\n", encoding="utf-8")
            with patch.dict("os.environ", {
                "JOBFLOW_DISCOVERY_SOURCES": "greenhouse",
                "JOBFLOW_GREENHOUSE_REGISTRY": str(registry),
            }, clear=True):
                providers = configured_providers()
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0].boards, {"Acme": "acme", "Beta": "beta"})

    def test_greenhouse_registry_preserves_two_boards_for_same_company(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "greenhouse.csv"
            registry.write_text("company,board_token\nAcme,acme-us\nAcme,acme-ca\n", encoding="utf-8")
            with patch.dict("os.environ", {
                "JOBFLOW_DISCOVERY_SOURCES": "greenhouse",
                "JOBFLOW_GREENHOUSE_REGISTRY": str(registry),
            }, clear=True):
                providers = configured_providers()
        self.assertEqual(providers[0].boards, {"Acme": "acme-us", "Acme (acme-ca)": "acme-ca"})

    def test_lever_registry_csv_is_an_allow_list(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "lever.csv"
            registry.write_text("company,board_token\nAcme,acme\nBeta,beta\n", encoding="utf-8")
            known = [{
                "company": "Historical", "url": "https://jobs.lever.co/historical/123",
                "canonical_url": "",
            }]
            with patch.dict("os.environ", {
                "JOBFLOW_DISCOVERY_SOURCES": "lever",
                "JOBFLOW_LEVER_REGISTRY": str(registry),
            }, clear=True):
                providers = configured_providers(known_jobs=known)
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0].sites, {"Acme": "acme", "Beta": "beta"})

    def test_lever_prefilter_parses_full_posting_and_board_audit(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return [
                    {
                        "id": "one", "text": "Data Engineer",
                        "hostedUrl": "https://jobs.lever.co/acme/one",
                        "descriptionPlain": "Build production Python and SQL pipelines.",
                        "additionalPlain": "Applicants throughout Canada are welcome.",
                        "lists": [{"text": "Requirements", "content": "<li>Databricks</li><li>ETL</li>"}],
                        "categories": {"location": "Remote Canada", "commitment": "FullTime"},
                        "workplaceType": "remote",
                        "salaryRange": {"min": 125000, "max": 155000, "currency": "CAD", "interval": "year"},
                    },
                    {
                        "id": "two", "text": "Account Executive",
                        "hostedUrl": "https://jobs.lever.co/acme/two",
                        "categories": {"location": "Remote Canada", "commitment": "FullTime"},
                    },
                ]

        class Client:
            def get(self, url, params=None):
                return Response()

        provider = LeverProvider({"Acme": "acme"}, Client(), prefilter=True)
        jobs = list(provider.fetch())
        self.assertEqual([job.title for job in jobs], ["Data Engineer"])
        self.assertIn("Databricks", jobs[0].description)
        self.assertEqual(jobs[0].country, "CA")
        self.assertEqual(jobs[0].compensation.maximum, 155000)
        result = provider.board_results[0]
        self.assertEqual((result["raw_count"], result["index_count"], result["emitted_count"]), (2, 1, 1))

    def test_lever_board_failure_is_auditable(self):
        class Client:
            def get(self, url, params=None):
                raise httpx.ConnectError("failed")

        provider = LeverProvider({"Broken": "broken"}, Client(), prefilter=True)
        self.assertEqual(list(provider.fetch()), [])
        self.assertEqual(provider.board_results[0]["state"], "failed")

    def test_explicit_greenhouse_registry_does_not_merge_historical_boards(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "greenhouse.csv"
            registry.write_text("company,board_token\nAcme,acme\n", encoding="utf-8")
            known = [{
                "company": "Historical",
                "url": "https://job-boards.greenhouse.io/historical/jobs/123",
                "canonical_url": "",
            }]
            with patch.dict("os.environ", {
                "JOBFLOW_DISCOVERY_SOURCES": "greenhouse",
                "JOBFLOW_GREENHOUSE_REGISTRY": str(registry),
            }, clear=True):
                providers = configured_providers(known_jobs=known)
        self.assertEqual(providers[0].boards, {"Acme": "acme"})

    def test_greenhouse_normalizes_null_application_dates(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"jobs": [{
                    "id": 123,
                    "title": "Data Engineer",
                    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/123",
                    "content": "Build SQL pipelines",
                    "location": {"name": "Remote Canada"},
                    "first_published": None,
                    "application_deadline": None,
                }]}

        class Client:
            def get(self, url, params=None):
                return Response()

        job = list(GreenhouseProvider({"Acme": "acme"}, Client()).fetch())[0]
        self.assertEqual(job.posted_at, "")
        self.assertEqual(job.closes_at, "")

    def test_greenhouse_prefers_hybrid_signal_and_infers_country(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"jobs": [{
                    "id": 124,
                    "title": "Data Engineer",
                    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/124",
                    "content": "Location: Hybrid (3 days in office / 2 days remote)",
                    "location": {"name": "New York, New York, United States"},
                }]}

        class Client:
            def get(self, url, params=None):
                return Response()

        job = list(GreenhouseProvider({"Acme": "acme"}, Client()).fetch())[0]
        self.assertEqual(job.workplace_type, "hybrid")
        self.assertEqual(job.country, "US")

    def test_greenhouse_ignores_remote_job_legal_boilerplate(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"jobs": [{
                    "id": 125,
                    "title": "Data Scientist",
                    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/125",
                    "content": "Notice to applicants for remote jobs associated with our NYC office.",
                    "location": {"name": "Toronto, ON"},
                }]}

        class Client:
            def get(self, url, params=None):
                return Response()

        job = list(GreenhouseProvider({"Acme": "acme"}, Client()).fetch())[0]
        self.assertEqual(job.workplace_type, "unspecified")

    def test_greenhouse_prefilter_fetches_details_only_for_likely_matches(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class Client:
            def __init__(self):
                self.calls = []

            def get(self, url, params=None):
                self.calls.append((url, params))
                if url.endswith("/jobs"):
                    return Response({"jobs": [
                        {"id": 1, "title": "Data Engineer", "location": {"name": "Remote Canada"}},
                        {"id": 2, "title": "Account Executive", "location": {"name": "Remote Canada"}},
                        {"id": 3, "title": "Data Scientist", "location": {"name": "Remote Canada"}},
                        {"id": 4, "title": "Data Analyst", "location": {"name": "London, UK"}},
                    ]})
                if url.endswith("/jobs/4"):
                    return Response({
                        "id": 4, "title": "Data Analyst", "location": {"name": "London, UK"},
                        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/4",
                        "content": "Full-time office role based in London, United Kingdom.",
                    })
                return Response({
                    "id": 1, "title": "Data Engineer", "location": {"name": "Remote Canada"},
                    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1",
                    "content": "Build production Python SQL Databricks pipelines.",
                })

        client = Client()
        provider = GreenhouseProvider({"Acme": "acme"}, client, prefilter=True)
        jobs = list(provider.fetch())
        self.assertEqual([job.title for job in jobs], ["Data Engineer"])
        self.assertEqual(
            (provider.board_results[0]["raw_count"], provider.board_results[0]["index_count"],
             provider.board_results[0]["emitted_count"]), (4, 2, 1)
        )
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.calls[0][1], {"content": "true", "pay_transparency": "true"})
        self.assertTrue(client.calls[1][0].endswith("/jobs/1"))

    def test_ashby_normalizes_null_optional_fields(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"jobs": [{
                    "id": "abc",
                    "title": "Data Engineer",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/abc",
                    "descriptionPlain": "Build SQL pipelines",
                    "location": None,
                    "employmentType": None,
                    "publishedAt": None,
                }]}

        class Client:
            def get(self, url, params=None):
                return Response()

        job = list(AshbyProvider({"Acme": "acme"}, Client()).fetch())[0]
        self.assertEqual(job.location, "")
        self.assertEqual(job.employment_type, "full-time")
        self.assertEqual(job.posted_at, "")

    def test_ashby_uses_structured_salary_currency_and_title_country(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"jobs": [{
                    "id": "abc",
                    "title": "Databricks Architect | US",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/abc",
                    "descriptionPlain": "Remote SQL and Databricks role",
                    "location": "Remote US",
                    "workplaceType": "Remote",
                    "employmentType": "FullTime",
                    "compensation": {"summaryComponents": [{
                        "compensationType": "Salary", "currencyCode": "USD",
                        "interval": "1 YEAR", "minValue": 145000, "maxValue": 188000,
                    }]},
                }]}

        class Client:
            def get(self, url, params=None):
                return Response()

        job = list(AshbyProvider({"Acme": "acme"}, Client()).fetch())[0]
        self.assertEqual(job.country, "US")
        self.assertEqual(job.compensation.currency, "USD")
        self.assertEqual(job.compensation.maximum, 188000)

    def test_ashby_prefilter_keeps_only_targeted_eligible_jobs(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                def item(identifier, title, location, salary):
                    return {
                        "id": identifier, "title": title, "location": location,
                        "jobUrl": "https://jobs.ashbyhq.com/acme/{}".format(identifier),
                        "descriptionPlain": "Build Python SQL Databricks analytics pipelines.",
                        "workplaceType": "Remote", "employmentType": "FullTime",
                        "compensation": {"summaryComponents": [{
                            "compensationType": "Salary", "currencyCode": "CAD",
                            "interval": "1 YEAR", "minValue": salary, "maxValue": salary + 20000,
                        }]},
                    }
                return {"jobs": [
                    item("1", "Data Analyst", "Remote - Canada", 125000),
                    item("2", "Data Scientist", "Remote - Canada", 150000),
                    item("3", "Data Engineer", "London, UK", 150000),
                    item("4", "Data Engineer", "Remote - Canada", 90000),
                ]}

        class Client:
            def get(self, url, params=None):
                return Response()

        jobs = list(AshbyProvider({"Acme": "acme"}, Client(), prefilter=True).fetch())
        # The shared daily policy ignores salary, but retains role/geography gates.
        self.assertEqual([job.title for job in jobs], ["Data Analyst", "Data Engineer"])

    def test_ashby_registry_csv_is_an_allow_list(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "ashby.csv"
            registry.write_text("company,board_token\nAcme,acme\n", encoding="utf-8")
            known = [{
                "company": "Historical",
                "url": "https://jobs.ashbyhq.com/historical/123",
                "canonical_url": "",
            }]
            with patch.dict("os.environ", {
                "JOBFLOW_DISCOVERY_SOURCES": "ashby",
                "JOBFLOW_ASHBY_REGISTRY": str(registry),
            }, clear=True):
                providers = configured_providers(known_jobs=known)
        self.assertEqual(providers[0].boards, {"Acme": "acme"})

    def test_salary_parser_ignores_benefits_ids_and_operational_counts(self):
        text = "401k retirement match. Process 40,000,000 records. Job ID 255255. Base salary USD $145,000-$175,000 per year."
        compensation = infer_compensation(text, "USD")
        self.assertEqual(compensation.minimum, 145000)
        self.assertEqual(compensation.maximum, 175000)

    def test_salary_parser_requires_salary_or_currency_context(self):
        self.assertIsNone(infer_compensation("Processed 40,000,000 rows and supported 255,000 accounts."))

    def test_permitted_aggregator_resolves_to_original_ats_posting(self):
        aggregator = "https://www.adzuna.ca/details/123"
        direct = "https://jobs.lever.co/acme/abc"
        posting = {
            "@context": "https://schema.org", "@type": "JobPosting", "title": "Data Engineer",
            "description": "Build Python and SQL production data pipelines. " * 20,
            "hiringOrganization": {"name": "Acme"},
            "jobLocation": {"address": {"addressCountry": "Canada"}},
            "baseSalary": {"currency": "CAD", "value": {"minValue": 130000, "maxValue": 160000}},
        }

        class Response:
            def __init__(self, url, text):
                self.url, self.text = url, text

            def raise_for_status(self):
                return None

        class Client:
            def __init__(self):
                self.calls = []

            def get(self, url, headers=None):
                self.calls.append(url)
                if url == aggregator:
                    return Response(url, '<a href="{}">Apply now</a>'.format(direct))
                return Response(direct, '<script type="application/ld+json">{}</script>'.format(json.dumps(posting)))

        candidate = JobInput(
            "Data Engineer", "Unknown", "Remote Canada", aggregator, "Short snippet", source="adzuna",
            canonical_url=aggregator, workplace_type="remote", country="CA",
        )
        client = Client()
        resolved = resolve_canonical_job(candidate, client)
        self.assertEqual(resolved.canonical_url, direct)
        self.assertEqual(resolved.company, "Acme")
        self.assertEqual(client.calls, [aggregator, direct])

    def test_resolution_extracts_remote_and_available_sponsorship_evidence(self):
        url = "https://employer.example/jobs/1"
        posting = {
            "@type": "JobPosting", "title": "Data Engineer",
            "description": ("This is a remote full-time role. Visa sponsorship is available. "
                            "The base salary is USD $150,000 to $180,000. " * 8),
            "hiringOrganization": {"name": "Acme"},
            "jobLocation": {"address": {"addressCountry": "United States"}},
        }

        class Response:
            def __init__(self):
                self.url, self.text = url, '<script type="application/ld+json">{}</script>'.format(json.dumps(posting))

            def raise_for_status(self):
                return None

        class Client:
            def get(self, requested, headers=None):
                return Response()

        candidate = JobInput(
            "Data Engineer", "Unknown", "United States", url, "Snippet", source="brave",
            canonical_url=url, country="US",
        )
        resolved = resolve_canonical_job(candidate, Client())
        self.assertEqual(resolved.workplace_type, "remote")
        self.assertEqual(resolved.sponsorship, "available")
        self.assertEqual(resolved.compensation.maximum, 180000)

    def test_restricted_board_is_never_fetched_during_resolution(self):
        class Client:
            def __init__(self):
                self.calls = []

            def get(self, url, headers=None):
                self.calls.append(url)
                raise AssertionError("restricted board must not be fetched")

        url = "https://www.indeed.com/viewjob?jk=abc"
        candidate = JobInput(
            "Data Engineer", "Acme", "Remote Canada", url, "Snippet", source="brave", canonical_url=url
        )
        client = Client()
        resolved = resolve_canonical_job(candidate, client)
        self.assertEqual(resolved.canonical_url, "")
        self.assertEqual(client.calls, [])

    def test_structured_location_country_does_not_confuse_state_codes(self):
        self.assertEqual(_country_code_from_location("Bangalore, Karnataka, IN", "CA"), "IN")
        self.assertEqual(_country_code_from_location("Indianapolis, IN", "US"), "")
        self.assertEqual(_country_code_from_location("San Francisco, CA", "US"), "")
        self.assertEqual(_country_code_from_location("Remote - Canada", "US"), "CA")

    def test_brave_paginates_only_while_more_results_are_available(self):
        class Response:
            def __init__(self, offset):
                self.offset = offset

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "query": {"more_results_available": self.offset == 0},
                    "web": {"results": [{
                        "title": "Data Engineer",
                        "url": "https://employer.example/jobs/{}".format(self.offset),
                        "description": "Python SQL Databricks",
                    }]},
                }

        class Client:
            def __init__(self):
                self.offsets = []

            def get(self, url, params=None, headers=None):
                self.offsets.append(params["offset"])
                return Response(params["offset"])

        client = Client()
        results = list(BraveProvider("key", pages=5, client=client).fetch())
        self.assertEqual(len(results), len(client.offsets))
        self.assertGreater(len(results), 100)
        self.assertEqual(client.offsets[:2], [0, 1])
        self.assertNotIn(2, client.offsets)

    def test_http_error_credentials_are_redacted(self):
        message = _safe_error(ValueError("https://example.test/jobs?app_id=abc&app_key=secret"))
        self.assertNotIn("secret", message)
        self.assertIn("app_key=[redacted]", message)

    def test_surface_is_limited_but_rejections_remain_auditable(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(str(Path(folder) / "test.db"))
            repo.init()
            run_id = DiscoveryService(repo, [FixtureProvider()], result_limit=30).run()
            run = repo.discovery_run(run_id)
            self.assertEqual(run["discovered_count"], 36)
            self.assertEqual(run["qualified_count"], 30)
            self.assertEqual(len(repo.jobs(queue="qualified")), 30)
            self.assertEqual(len(repo.jobs(queue="rejected")), 1)

    def test_unrelated_rejections_are_not_persisted_at_registry_scale(self):
        class Provider(DiscoveryProvider):
            name = "fixture"

            def fetch(self):
                yield JobInput(
                    "Account Executive", "Acme", "Remote Canada", "https://example.com/jobs/sales",
                    "Use SQL reporting to support customers.", source=self.name,
                    canonical_url="https://example.com/jobs/sales", workplace_type="remote",
                    country="CA", compensation=Compensation(130000, 160000, "CAD"),
                )

        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(str(Path(folder) / "test.db"))
            repo.init()
            DiscoveryService(repo, [Provider()]).run()
            self.assertEqual(len(repo.jobs()), 0)

    def test_strong_search_snippet_is_resolved_and_rescored_before_storage(self):
        class SearchProvider(DiscoveryProvider):
            name = "brave"

            def fetch(self):
                yield JobInput(
                    "Senior Data Engineer", "Result", "Remote Canada", "https://search.example/job/1",
                    "Python SQL Databricks ETL data modeling pipelines.", source="brave",
                    external_id="https://search.example/job/1", workplace_type="remote",
                    employment_type="full-time", country="CA",
                    compensation=Compensation(130000, 160000, "CAD", is_estimated=True),
                )

        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(str(Path(folder) / "test.db"))
            repo.init()
            full_description = "Build Python SQL Databricks ETL and data modeling pipelines. " * 12

            def resolve(candidate, client=None):
                return replace(
                    candidate,
                    canonical_url="https://employer.example/jobs/1",
                    description=full_description,
                    compensation=Compensation(135000, 165000, "CAD"),
                )

            with patch("jobflow.discovery.resolve_canonical_job", side_effect=resolve) as resolver:
                DiscoveryService(repo, [SearchProvider()]).run()
            stored = repo.jobs()[0]
            self.assertEqual(resolver.call_count, 1)
            self.assertEqual(stored["canonical_url"], "https://employer.example/jobs/1")
            self.assertEqual(stored["salary_estimated"], 0)
            self.assertIn("high (full description)", stored["score_components"])


if __name__ == "__main__":
    unittest.main()

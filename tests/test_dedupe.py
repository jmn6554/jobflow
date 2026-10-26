import unittest

from jobflow.dedupe import canonical_url, identity_key
from jobflow.providers import aggregator_url
from jobflow.models import JobInput


class DedupeTests(unittest.TestCase):
    def test_country_adzuna_and_haystack_are_aggregators(self):
        self.assertTrue(aggregator_url("https://www.adzuna.ca/details/123"))
        self.assertTrue(aggregator_url("https://haystackapp.io/jobs/123"))

    def test_tracking_parameters_do_not_change_url(self):
        self.assertEqual(
            canonical_url("HTTPS://Example.com/jobs/1/?utm_source=x&team=data#apply"),
            "https://example.com/jobs/1?team=data",
        )

    def test_external_identity_is_stable(self):
        a = JobInput("A", "B", "X", "https://one", "", "board", "42")
        b = JobInput("Different", "Company", "Y", "https://two", "", "board", "42")
        self.assertEqual(identity_key(a), identity_key(b))


if __name__ == "__main__":
    unittest.main()

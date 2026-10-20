"""Checks the title filters in config.json against known QA job titles and look-alikes.

Run after editing config.json -> filters:
    py test_filters.py
"""

import unittest

import sqa_jobs

# Software-testing titles kept wherever they're posted, even by a company with a non-tech name.
STRONG = [
    "SQA Engineer", "Jr. SQA Engineer", "S.Q.A Engineer", "Senior SQA Engineer (Automation)", "SQA Intern", "Trainee SQA",
    "QA Engineer", "Q.A. Engineer", "QA Automation Engineer", "Automation QA Engineer", "QA Analyst", "QA Lead",
    "QA Architect", "Manual QA Engineer", "QA Tester", "QA Intern", "QA Developer", "QA/SQA Engineer", "SQA/QA Engineer",
    "QE Engineer", "QE Lead",
    "Software Quality Assurance Engineer", "Software QA Engineer", "Software Test Engineer", "Software Tester",
    "Software Testing Engineer", "Software Quality Engineer", "Software Engineer in Test",
    "Software Development Engineer in Test", "Senior SDET", "SDET II",
    "Test Automation Engineer", "Automation Test Engineer", "Automation Tester", "Automated Testing Engineer",
    "Test Engineer", "Test Analyst", "Test Lead", "Test Manager", "Test Architect", "Testing Specialist", "Test Consultant",
    "Mobile App Tester", "Mobile QA Engineer", "Game Tester", "Game QA Tester", "API Tester", "API Test Engineer",
    "Performance Test Engineer", "Performance Tester", "Load Testing Engineer", "Security Tester", "Web Tester",
    "Website Tester", "UI Tester", "Functional Tester", "Regression Tester", "E2E Test Engineer", "End-to-End Tester",
    "Usability Tester", "Accessibility Tester", "Localization Tester", "Beta Tester", "Bug Tester", "Android Tester",
    "iOS QA Engineer", "Data QA Engineer", "ETL Tester", "Blockchain QA Engineer", "Embedded Test Engineer",
    "Firmware Test Engineer",
    "Playwright Tester", "Playwright Automation Engineer", "Selenium Automation Engineer", "Cypress Engineer",
    "Appium Mobile Automation", "Katalon Tester", "JMeter Performance Engineer", "WebdriverIO Engineer",
    "Robot Framework Developer", "ISTQB Certified Tester",
    "UAT Analyst", "UAT Tester", "Quality Assurance Engineer", "Quality Assurance Analyst",
    "Quality Assurance Automation Engineer", "Quality Engineer (Automation)", "Quality Engineering - Software",
]

# Vague titles: kept at a software company or on a company careers page, dropped elsewhere.
WEAK = [
    "QA Executive", "Quality Assurance Executive", "QA Specialist", "QA Officer", "QA Manager", "Quality Engineer",
    "Quality Analyst", "Tester", "Testing Executive", "QC Engineer", "Automation Engineer",
]

# Look-alikes from manufacturing, labs, call centers and industrial automation.
DROP = [
    ("QA Executive", "Acme Garments Ltd"), ("Quality Assurance Officer", "Rahim Textiles"),
    ("QA Manager", "Square Pharmaceuticals"), ("QA Executive", "Acme Group"), ("Quality Controller", "Acme Fashions"),
    ("Asst. QC Engineer", "BOF Korea and Bangladesh Ltd."), ("Assistant Testing Engineer (Dhaka EPZ Lab)", "Bureau Veritas"),
    ("Lab Test Engineer", "Intertek"), ("Test Engineer (Electrical)", "Siemens"), ("Fabric Tester", "Acme Knit"),
    ("Automation Engineer (PLC/SCADA)", "Acme Tech"), ("Industrial Automation Engineer", "ABB"),
    ("Quality Analyst - Call Center", "Genex Infosys"), ("QA/QC Engineer (Civil)", "Acme Builders"),
    ("Python Automation Intern", "Marive"), ("Marketing Automation Specialist", "Acme Digital"),
    ("Water Testing Officer", "WASA"), ("Quality Control (QC) Officer", "Munshi HR Solutions Limited"),
    ("Quality Assurance Rater - Spanish (ES)", "TELUS Digital"), ("QA & Source Inspection", "micro1"),
    ("QA Inspector", "Acme Tech"),
]


# filters.skip_level: seniority you don't want (checked separately; the lists above ignore it).
SKIPPED_LEVELS = ["Junior SQA Engineer", "Jr. QA Engineer", "QA Tester Entry Level", "Associate SQA Engineer",
                  "QA Manager", "Senior QA Manager - Fully Remote", "Head of QA", "Director of Quality Engineering"]
KEPT_LEVELS = ["SQA Engineer", "Senior SQA Engineer", "QA Automation Engineer II", "Mid-Level SQA Engineer",
               "QA Lead", "SQA Intern"]


class TitleFilters(unittest.TestCase):
    cfg = sqa_jobs.load_config()
    no_levels = {**cfg, "filters": {**cfg["filters"], "skip_level": ""}}

    def kept(self, title, company, **extra):
        return sqa_jobs.is_sqa(self.no_levels, {"title": title, "company": company, **extra})

    def test_skipped_levels(self):
        for title in SKIPPED_LEVELS:
            with self.subTest(title=title):
                self.assertFalse(sqa_jobs.is_sqa(self.cfg, {"title": title, "company": "Acme Software Ltd"}))
        for title in KEPT_LEVELS:
            with self.subTest(title=title):
                self.assertTrue(sqa_jobs.is_sqa(self.cfg, {"title": title, "company": "Acme Software Ltd"}))

    def test_strong_titles_always_kept(self):
        for title in STRONG:
            with self.subTest(title=title):
                self.assertTrue(self.kept(title, "Acme Group"))

    def test_weak_titles_kept_at_software_companies(self):
        for title in WEAK:
            with self.subTest(title=title):
                self.assertTrue(self.kept(title, "Acme Software Ltd"))
                self.assertTrue(self.kept(title, "Acme Group", tech=True))  # company careers page
                self.assertTrue(self.kept(title, "Acme Group", category=self.cfg["bdjobs"]["it_category_ids"][0]))

    def test_look_alikes_dropped(self):
        for title, company in DROP:
            with self.subTest(title=title, company=company):
                self.assertFalse(self.kept(title, company))


class Duplicates(unittest.TestCase):
    """dup_key: one job seen on two sources, or reposted, is tracked once."""
    SAME = [
        (("Ubuntu Linux Kernel Test Engineer", "Canonical"), ("Ubuntu Linux Kernel Test Engineer", "Canonical Ltd.")),
        (("QA Engineer", "Halborn"), ("QA Engineer (Remote locations)", "Halborn")),
        (("QA Engineer [Remote]", "CyberArrow"), ("QA Engineer", "CyberArrow")),
        (("Sr. SQA Engineer", "Acme Software Ltd"), ("Senior SQA Engineer", "Acme Software Limited")),
        (("QA Automation Engineer (m/f/d)", "zvoove"), ("QA Automation Engineer", "zvoove GmbH")),
        (("QA Engineer | Remote - Contract", "Xperteez Technology"), ("QA Engineer", "Xperteez Technology")),
        (("Binance Accelerator Program - QA (Web3)", "Binance (Lever)"), ("Binance Accelerator Program - QA (Web3)", "Binance")),
    ]
    DIFFERENT = [
        (("QA Engineer", "Novacard"), ("Senior QA Engineer", "Novacard")),
        (("QA Engineer", "Nitka"), ("QA Engineer", "Halborn")),
        (("QA Automation Engineer", "TransPerfect"), ("QA Manual Engineer", "TransPerfect")),
        (("QA Engineer (Unity Team)", "Infomediji"), ("QA Engineer", "Infomediji")),
    ]

    def test_same_job(self):
        for a, b in self.SAME:
            with self.subTest(a=a, b=b):
                self.assertEqual(sqa_jobs.dup_key(*a), sqa_jobs.dup_key(*b))

    def test_different_jobs(self):
        for a, b in self.DIFFERENT:
            with self.subTest(a=a, b=b):
                self.assertNotEqual(sqa_jobs.dup_key(*a), sqa_jobs.dup_key(*b))


if __name__ == "__main__":
    unittest.main()

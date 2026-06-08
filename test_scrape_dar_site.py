import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import scrape_dar_site


class TestDarProjectScraper(unittest.TestCase):
    def test_content_links_ignore_navigation_and_noise(self):
        html = """
        <html><body>
          <nav><a href="/about/">About</a></nav>
          <main>
            <a href="/projects/abcar-project/">ABC/AR</a>
            <a href="/donate/">Donate</a>
            <a href="/">Home</a>
          </main>
        </body></html>
        """

        links = scrape_dar_site.extract_links(
            html,
            "https://dharamsalaanimalrescue.org/projects",
            "dharamsalaanimalrescue.org",
            content_only=True,
            excluded_path_parts=scrape_dar_site.PROJECT_NOISE_PATHS,
            excluded_exact_paths=scrape_dar_site.PROJECT_NOISE_EXACT_PATHS,
        )

        self.assertEqual(
            links,
            ["https://dharamsalaanimalrescue.org/projects/abcar-project"],
        )

    def test_project_crawl_stops_at_depth_and_writes_manifest(self):
        pages = {
            "https://dharamsalaanimalrescue.org/projects": """
                <main><h1>Projects</h1><p>Core rescue projects and animal welfare programs.</p>
                <a href="/projects/abcar-project/">ABC/AR</a><a href="/donate/">Donate</a></main>
            """,
            "https://dharamsalaanimalrescue.org/projects/abcar-project": """
                <main><h1>ABC AR</h1><p>Animal birth control and anti-rabies vaccination program.</p>
                <a href="/rabies-quiz/">Rabies quiz</a></main>
            """,
            "https://dharamsalaanimalrescue.org/rabies-quiz": """
                <main><h1>Rabies Quiz</h1><p>Educational information about preventing rabies.</p>
                <a href="/too-deep/">Too deep</a></main>
            """,
            "https://dharamsalaanimalrescue.org/too-deep": """
                <main><h1>Too Deep</h1><p>This page must not be visited.</p></main>
            """,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "docs"
            manifest_path = Path(temp_dir) / "manifest.json"

            with patch(
                "scripts.scrape_dar_site.fetch_html",
                side_effect=lambda _session, url, timeout: pages.get(url),
            ):
                result = scrape_dar_site.scrape(
                    "https://dharamsalaanimalrescue.org/projects/",
                    output_dir,
                    max_pages=20,
                    delay=0,
                    timeout=1,
                    fresh=False,
                    content_links_only=True,
                    max_depth=2,
                    min_words=5,
                    excluded_path_parts=scrape_dar_site.PROJECT_NOISE_PATHS,
                    manifest_path=manifest_path,
                )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            urls = {page["url"] for page in manifest["pages"]}

            self.assertEqual(result["visited_count"], 3)
            self.assertEqual(result["saved_count"], 3)
            self.assertNotIn("https://dharamsalaanimalrescue.org/donate", urls)
            self.assertNotIn("https://dharamsalaanimalrescue.org/too-deep", urls)
            self.assertTrue((output_dir / "projects.md").exists())
            self.assertEqual(manifest["saved_count"], 3)


if __name__ == "__main__":
    unittest.main()

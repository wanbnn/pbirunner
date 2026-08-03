import tempfile
import unittest
from pathlib import Path

from pbi_viewer.parser import PBIParseError, parse_project


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "MonitorIA_NeoEnergia/MonitorIA_NeoEnergia"


@unittest.skipUnless(EXAMPLE.exists(), "fixture Power BI local não está versionada")
class ParserIntegrationTests(unittest.TestCase):
    def test_parses_reference_pbip(self):
        project = parse_project(EXAMPLE / "MonitorIA_NeoEnergia.pbip")
        self.assertEqual(project["format"], "PBIP")
        self.assertEqual(len(project["pages"]), 11)
        self.assertGreaterEqual(sum(len(page["visuals"]) for page in project["pages"]), 100)
        self.assertEqual(len(project["model"]["tables"]), 6)
        self.assertIn("neoenergia-light1785772800002.png", project["assets"])
        textboxes = [visual for page in project["pages"] for visual in page["visuals"] if visual["type"] == "textbox"]
        self.assertTrue(all(visual["text"] and visual["paragraphs"] for visual in textboxes))
        cover_title = next(visual for visual in textboxes if len(visual["paragraphs"][0]["runs"]) == 3)
        self.assertEqual(len(cover_title["paragraphs"][0]["runs"]), 3)
        self.assertFalse(cover_title["style"]["backgroundShow"])
        chart = next(visual for page in project["pages"] for visual in page["visuals"] if visual["type"] == "barChart")
        self.assertEqual(chart["style"]["borderRadius"], 14)
        self.assertEqual(chart["style"]["title"]["fontFamily"], "Trebuchet MS")
        priority_chart = next(
            visual
            for page in project["pages"]
            for visual in page["visuals"]
            if visual.get("title") == "Fila por nivel de prioridade"
        )
        self.assertEqual(priority_chart["style"]["categoryColors"]["P0 | RISCO A VIDA"], "#D92D20")
        self.assertEqual(priority_chart["style"]["categoryColors"]["P4 | MONITORAR"], "#00A443")
        self.assertIn("xAxis", priority_chart["style"])
        self.assertIn("legend", priority_chart["style"])

    def test_parses_reference_pbix(self):
        project = parse_project(EXAMPLE / "MonitorIA_NeoEnergia.pbix")
        self.assertEqual(project["format"], "PBIX")
        self.assertEqual(len(project["pages"]), 11)
        self.assertTrue(project["hasEmbeddedModel"])

    def test_rejects_unknown_file(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as handle:
            with self.assertRaises(PBIParseError):
                parse_project(handle.name)


if __name__ == "__main__":
    unittest.main()

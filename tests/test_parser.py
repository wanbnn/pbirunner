import tempfile
import unittest
from pathlib import Path

from pbi_viewer.parser import PBIParseError, _navigator_orientation, _page_hidden, _visual, _visual_filters, parse_project


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
        self.assertEqual(priority_chart["sort"][0]["direction"], "Descending")
        navigator = next(visual for page in project["pages"] for visual in page["visuals"] if visual["type"] == "pageNavigator")
        self.assertEqual(navigator["style"]["navigator"]["orientation"], "Horizontal")
        filtered = next(visual for page in project["pages"] for visual in page["visuals"] if visual.get("title") == "Causa raiz dos recontatos")
        self.assertEqual(filtered["filters"][0]["field"]["table"], "Atendimentos")
        self.assertEqual(filtered["filters"][0]["operator"], "in")
        self.assertEqual(filtered["filters"][0]["values"], [1])

    def test_parses_reference_pbix(self):
        project = parse_project(EXAMPLE / "MonitorIA_NeoEnergia.pbix")
        self.assertEqual(project["format"], "PBIX")
        self.assertEqual(len(project["pages"]), 11)
        self.assertTrue(project["hasEmbeddedModel"])

    def test_rejects_unknown_file(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as handle:
            with self.assertRaises(PBIParseError):
                parse_project(handle.name)

    def test_parses_measure_greater_than_zero_filter(self):
        field = {"Measure": {"Expression": {"SourceRef": {"Entity": "Fatos"}}, "Property": "Total"}}
        raw = {"filterConfig": {"filters": [{"field": field, "filter": {"Where": [{"Condition": {"Comparison": {"ComparisonKind": 1, "Left": field, "Right": {"Literal": {"Value": "0L"}}}}}]}}]}}
        self.assertEqual(_visual_filters(raw), [{"field": {"kind": "Measure", "table": "Fatos", "name": "Total"}, "operator": "gt", "value": 0}])

    def test_infers_vertical_page_navigator(self):
        raw = {"name": "nav", "position": {"width": 80, "height": 400}, "visual": {"visualType": "pageNavigator", "objects": {}}}
        parsed = _visual(raw, {})
        self.assertEqual(parsed["style"]["navigator"]["orientation"], "Vertical")

    def test_normalizes_pbir_navigator_orientation_enum(self):
        position = {"width": 212, "height": 561}
        self.assertEqual(_navigator_orientation(0, position), "Horizontal")
        self.assertEqual(_navigator_orientation(1, position), "Vertical")
        self.assertEqual(_navigator_orientation("2D", position), "Grid")

    def test_normalizes_hidden_page_variants(self):
        self.assertTrue(_page_hidden({"visibility": "HiddenInViewMode"}))
        self.assertTrue(_page_hidden({"isHidden": True}))
        self.assertFalse(_page_hidden({"visibility": "Visible"}))
        self.assertFalse(_page_hidden({}))

    @unittest.skipUnless((ROOT / "MonitorIA_IFood_FGX.pbix").exists(), "fixture com página oculta não está versionada")
    def test_parses_hidden_page_from_pbix(self):
        project = parse_project(ROOT / "MonitorIA_IFood_FGX.pbix")
        hidden = [page["name"] for page in project["pages"] if page["hidden"]]
        self.assertEqual(hidden, ["👐 Liderança & Equipes"])


if __name__ == "__main__":
    unittest.main()

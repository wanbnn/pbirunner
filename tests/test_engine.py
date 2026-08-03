import unittest
from copy import deepcopy
import tempfile
from pathlib import Path

try:
    import pandas  # noqa: F401
    import pbixray  # noqa: F401
except ImportError:
    pandas = None

from pbi_viewer.dax import Condition, FilterContext
from pbi_viewer.engine import ModelEngine
from pbi_viewer.parser import parse_project
from pbi_viewer.server import ReportRuntime


ROOT = Path(__file__).resolve().parents[1]
PBIP = ROOT / "MonitorIA_NeoEnergia/MonitorIA_NeoEnergia/MonitorIA_NeoEnergia.pbip"


@unittest.skipIf(pandas is None, "dependências do runtime não instaladas")
@unittest.skipUnless(PBIP.exists(), "fixture Power BI local não está versionada")
class RealDataRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = parse_project(PBIP)
        cls.engine = ModelEngine.for_project(PBIP, cls.project["model"])
        cls.runtime = cls.engine._ensure_runtime()

    @classmethod
    def tearDownClass(cls):
        cls.engine.close()

    def test_extracts_real_vertipaq_rows(self):
        self.assertEqual(len(self.runtime.tables["Atendimentos"]), 4344)
        self.assertEqual(len(self.runtime.tables["Resultados KPI"]), 130320)

    def test_text_formatting_does_not_remove_characters(self):
        self.assertEqual(
            ModelEngine.format_value("PAULO ROBERTO DO NASCIMENTO SOUZA", None),
            "PAULO ROBERTO DO NASCIMENTO SOUZA",
        )
        self.assertEqual(ModelEngine.format_value("2026-08-03T12:30:45.000", None), "2026-08-03 12:30:45")

    def test_executes_report_measures(self):
        self.assertEqual(self.runtime.evaluate_measure("Atendimentos"), 4344)
        self.assertEqual(self.runtime.evaluate_measure("Riscos a Vida"), 309)
        self.assertAlmostEqual(self.runtime.evaluate_measure("NPS Calculado"), -8.4351233, places=5)
        self.assertAlmostEqual(self.runtime.evaluate_measure("Cobertura de KPIs"), 1.0, places=6)

    def test_all_reference_measures_are_supported(self):
        failures = []
        for measure in self.runtime.measures.values():
            try:
                self.runtime.evaluate_measure(measure["name"])
            except Exception as exc:  # pragma: no cover - mensagem diagnóstica
                failures.append(f"{measure['name']}: {exc}")
        self.assertEqual(failures, [])

    def test_filter_context_changes_measures(self):
        series = self.runtime.tables["Atendimentos"]["Criticidade Consolidada"]
        context = FilterContext()
        context.add(Condition("Atendimentos", series == "Risco a vida", "Criticidade Consolidada"))
        self.assertEqual(self.runtime.evaluate_measure("Atendimentos", context), 309)
        self.assertEqual(self.runtime.evaluate_measure("Riscos a Vida", context), 309)
        self.assertAlmostEqual(self.runtime.evaluate_measure("Taxa de Casos Prioritarios", context), 1.0)

    def test_dimension_filter_propagates_to_facts(self):
        series = self.runtime.tables["Supervisores"]["Supervisor"]
        values = set(series.dropna())
        self.assertIn("ARTHUR MOURA DA SILVA", values)
        self.assertIn("PAULO ROBERTO DO NASCIMENTO SOUZA", values)
        context = FilterContext()
        context.add(Condition("Supervisores", series == "CAMILA KLYVIA NASCIMENTO LIMA", "Supervisor"))
        self.assertLess(self.runtime.evaluate_measure("Atendimentos", context), 4344)
        self.assertLess(self.runtime.evaluate_measure("Resultados Avaliados", context), 130320)

    def test_visual_filter_and_descending_sort_are_applied(self):
        visual = next(
            visual
            for page in self.project["pages"]
            for visual in page["visuals"]
            if visual.get("title") == "Causa raiz dos recontatos"
        )
        result = self.engine.query_visual(visual, self.engine.context([]))
        values = [row["Atendimentos.Recontatos"] for row in result["rows"]]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(len(result["rows"]), 6)

    def test_categories_without_measure_data_are_removed(self):
        visual = next(
            visual
            for page in self.project["pages"]
            for visual in page["visuals"]
            if visual.get("title") == "Pontuacao media por competencia"
        )
        result = self.engine.query_visual(visual, self.engine.context([]))
        values = [row["Resultados KPI.Pontuacao SEC"] for row in result["rows"]]
        self.assertTrue(values)
        self.assertTrue(all(value is not None for value in values))
        self.assertEqual(values, sorted(values, reverse=True))

    def test_measure_greater_than_zero_filter_and_ascending_sort(self):
        visual = deepcopy(next(
            visual
            for page in self.project["pages"]
            for visual in page["visuals"]
            if visual.get("title") == "Volume e casos prioritarios por dia"
        ))
        visual["filters"] = [{
            "field": {"kind": "Measure", "table": "Atendimentos", "name": "Atendimentos"},
            "operator": "gt",
            "value": 0,
        }]
        result = self.engine.query_visual(visual, self.engine.context([]))
        values = [row["Atendimentos.Atendimentos"] for row in result["rows"]]
        dates = [row["Calendario.Data"] for row in result["rows"]]
        self.assertTrue(all(value > 0 for value in values))
        self.assertEqual(dates, sorted(dates))

    def test_persistent_query_cache_avoids_reloading_vertipaq(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "prepared-cache.json"
            first = ReportRuntime(PBIP, cache)
            page = next(page for page in first.project["pages"] if page["name"] == "Visao Executiva")
            expected = first.query(page["id"], [])
            first.close()
            second = ReportRuntime(PBIP, cache)
            self.assertFalse(second.engine.status()["loaded"])
            actual = second.query(page["id"], [])
            self.assertEqual(actual["visuals"], expected["visuals"])
            self.assertFalse(second.engine.status()["loaded"])
            second.close()


if __name__ == "__main__":
    unittest.main()

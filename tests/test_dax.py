import unittest

import pandas as pd

from pbi_viewer.dax import DAXRuntime, FilterContext


class DAXVirtualTableTests(unittest.TestCase):
    def setUp(self):
        self.runtime = DAXRuntime(
            {"Fatos": pd.DataFrame({
                "Categoria": ["B", "A", "B", "C", "C"],
                "Valor": [1, 2, 3, 4, 5],
                "Data": pd.date_range("2026-01-01", periods=5),
            })},
            [],
            {"total": {"name": "Total", "expression": "SUM(Fatos[Valor])"}},
        )

    def test_var_values_and_concatenatex(self):
        result = self.runtime.evaluate(
            'VAR Categorias = VALUES(Fatos[Categoria]) RETURN CONCATENATEX(Categorias, Fatos[Categoria], ", ")',
            FilterContext(),
        )
        self.assertEqual(result, "B, A, C")

    def test_addcolumns_topn_and_sumx(self):
        result = self.runtime.evaluate(
            'VAR Linhas = ADDCOLUMNS(VALUES(Fatos[Categoria]), "@Total", [Total]) '
            'VAR Topo = TOPN(1, Linhas, [@Total], DESC) '
            'RETURN SUMX(Topo, [@Total])',
            FilterContext(),
        )
        self.assertEqual(result, 9)

    def test_datesinperiod_with_date_variable(self):
        result = self.runtime.evaluate(
            "VAR Fim = MAX(Fatos[Data]) RETURN CALCULATE([Total], DATESINPERIOD(Fatos[Data], Fim, -3, DAY))",
            FilterContext(),
        )
        self.assertEqual(result, 12)

    def test_switch_and_percentilex(self):
        percentile = self.runtime.evaluate("PERCENTILEX.INC(Fatos, Fatos[Valor], 0.5)", FilterContext())
        label = self.runtime.evaluate('SWITCH(TRUE(), [Total] > 10, "alto", "baixo")', FilterContext())
        self.assertEqual(percentile, 3)
        self.assertEqual(label, "alto")


if __name__ == "__main__":
    unittest.main()

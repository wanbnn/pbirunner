import unittest

import pandas as pd

from pbi_viewer.dax import DAXRuntime, FilterContext


class DAXVirtualTableTests(unittest.TestCase):
    def setUp(self):
        self.runtime = DAXRuntime(
            {"Fatos": pd.DataFrame({"Categoria": ["B", "A", "B"], "Valor": [2, 1, 3]})},
            [],
            {"total": {"name": "Total", "expression": "SUM(Fatos[Valor])"}},
        )

    def test_var_values_and_concatenatex(self):
        result = self.runtime.evaluate(
            'VAR Categorias = VALUES(Fatos[Categoria]) RETURN CONCATENATEX(Categorias, Fatos[Categoria], ", ")',
            FilterContext(),
        )
        self.assertEqual(result, "B, A")

    def test_addcolumns_topn_and_sumx(self):
        result = self.runtime.evaluate(
            'VAR Linhas = ADDCOLUMNS(VALUES(Fatos[Categoria]), "@Total", [Total]) '
            'VAR Topo = TOPN(1, Linhas, [@Total], DESC) '
            'RETURN SUMX(Topo, [@Total])',
            FilterContext(),
        )
        self.assertEqual(result, 5)


if __name__ == "__main__":
    unittest.main()

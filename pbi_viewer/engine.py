from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import re
from typing import Any

from .dax import Condition, DAXError, DAXRuntime, FilterContext, _python


class ModelEngine:
    """Camada analítica: decodifica VertiPaq e executa consultas dos visuais."""

    def __init__(self, source: Path, model_definition: dict[str, Any] | None = None):
        self.source = source
        self.model_definition = model_definition or {"tables": [], "relationships": []}
        self.available = False
        self.error: str | None = None
        self.row_counts: dict[str, int] = {}
        self._model: Any = None
        self._tables: dict[str, Any] = {}
        self._runtime: DAXRuntime | None = None
        self._measure_meta: dict[str, dict[str, Any]] = {}
        self._open()

    @classmethod
    def for_project(cls, path: Path, model_definition: dict[str, Any] | None = None) -> "ModelEngine":
        source = path
        if path.suffix.lower() == ".pbip":
            sibling = path.with_suffix(".pbix")
            if sibling.exists():
                source = sibling
            else:
                report_name = path.stem
                cache = path.parent / f"{report_name}.SemanticModel/.pbi/cache.abf"
                source = cache if cache.exists() else path
        return cls(source, model_definition)

    def _open(self) -> None:
        try:
            from pbixray import PBIXRay
            self._model = PBIXRay(str(self.source), on_disk=True)
            self.available = True
            for table in self.model_definition.get("tables", []):
                for measure in table.get("measures", []):
                    self._measure_meta[measure["name"].casefold()] = {**measure, "table": table["name"]}
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def close(self) -> None:
        if self._model is not None:
            try: self._model.close()
            except Exception: pass

    def _ensure_runtime(self) -> DAXRuntime:
        if not self.available or self._model is None:
            raise DAXError(self.error or "Modelo de dados indisponível")
        if self._runtime is not None:
            return self._runtime
        visible = [name for name in list(self._model.tables) if not name.startswith(("LocalDateTable_", "DateTableTemplate_"))]
        for name in visible:
            frame = self._model.get_table(name)
            self._tables[name] = frame
            self.row_counts[name] = len(frame)
        measures: dict[str, dict[str, Any]] = {}
        internal_formats: dict[str, str] = {}
        try:
            format_rows = self._model._metadata.source._db.query("SELECT Name, FormatString FROM Measure").to_dict("records")
            internal_formats = {str(row["Name"]).casefold(): row.get("FormatString") for row in format_rows}
        except Exception:
            pass
        for row in self._model.dax_measures.to_dict("records"):
            meta = self._measure_meta.get(str(row["Name"]).casefold(), {})
            measures[str(row["Name"]).casefold()] = {
                "name": row["Name"], "table": row["TableName"], "expression": row["Expression"],
                "formatString": internal_formats.get(str(row["Name"]).casefold()), **meta,
            }
        relationships = []
        for row in self._model.relationships.to_dict("records"):
            if not bool(row.get("IsActive", True)):
                continue
            relationships.append({
                "fromTable": row["FromTableName"], "fromColumn": row["FromColumnName"],
                "toTable": row["ToTableName"], "toColumn": row["ToColumnName"],
            })
        self._runtime = DAXRuntime(self._tables, relationships, measures)
        return self._runtime

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "source": self.source.name if self.available else None,
            "loaded": self._runtime is not None,
            "rows": self.row_counts,
            "error": self.error,
        }

    def context(self, filters: list[dict[str, Any]] | None = None) -> FilterContext:
        runtime = self._ensure_runtime()
        context = FilterContext()
        for item in filters or []:
            table, column = item.get("table"), item.get("column")
            if table not in runtime.tables or column not in runtime.tables[table]:
                continue
            values = item.get("values", [])
            series = runtime.tables[table][column]
            normalized = self._normalize_filter_values(series, values)
            context.add(Condition(table, series.isin(normalized), column))
        return context

    @staticmethod
    def _normalize_filter_values(series: Any, values: list[Any]) -> list[Any]:
        if str(series.dtype).startswith("datetime"):
            import pandas as pd
            return [pd.Timestamp(value) for value in values]
        return values

    def query_page(self, page: dict[str, Any], filters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        runtime = self._ensure_runtime()
        context = self.context(filters)
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for visual in page.get("visuals", []):
            if visual.get("type") in {"textbox", "pageNavigator"}:
                continue
            try:
                results[visual["id"]] = self.query_visual(visual, context)
            except Exception as exc:
                message = str(exc)
                errors[visual["id"]] = message
                # Keep a terminal result for the visual.  Returning no entry
                # makes the browser treat it as an in-flight query forever.
                results[visual["id"]] = {"kind": "error", "message": message}
        return {"visuals": results, "errors": errors, "runtime": self.status()}

    def query_visual(self, visual: dict[str, Any], context: FilterContext) -> dict[str, Any]:
        runtime = self._ensure_runtime()
        fields = visual.get("fields", [])
        columns = [field for field in fields if field["kind"] == "Column"]
        measures = [self._enrich_measure(field) for field in fields if field["kind"] == "Measure"]
        visual_filters = visual.get("filters", [])
        context = self._visual_context(context, visual_filters)
        visual_type = visual.get("type")
        if visual_type == "slicer" and columns:
            field = columns[0]
            values = self._distinct(field, context, 500)
            return {"kind": "slicer", "field": field, "values": [_python(value) for value in values]}
        if visual_type == "multiRowCard":
            return {"kind": "cards", "values": [self._measure_value(field, context) for field in measures]}
        if visual_type in {"tableEx", "pivotTable"}:
            row_columns = [field for field in columns if field["role"] in {"Values", "Rows"}]
            required = self._required_measures(measures, visual)
            has_measure_filter = any(item.get("field", {}).get("kind") == "Measure" for item in visual_filters)
            rows = self._table_rows(row_columns, required, context, limit=500 if has_measure_filter else 200)
            rows = self._post_process(rows, visual, row_columns, measures, 200)
            return {"kind": "table", "columns": row_columns + measures, "rows": rows, "truncated": len(rows) == 200}

        group_fields = [field for field in columns if field["role"] in {"Category", "Series"}]
        values = [field for field in measures if field["role"] not in {"Tooltips"}] or measures[:1]
        required = self._required_measures(values, visual)
        has_measure_filter = any(item.get("field", {}).get("kind") == "Measure" for item in visual_filters)
        rows = self._group_rows(group_fields, required, context, limit=500 if has_measure_filter else 150)
        rows = self._post_process(rows, visual, group_fields, values, 150, drop_empty=True)
        return {"kind": "chart", "groups": group_fields, "measures": values, "rows": rows}

    def _visual_context(self, context: FilterContext, filters: list[dict[str, Any]]) -> FilterContext:
        runtime = self._ensure_runtime()
        result = context.copy()
        for item in filters:
            field, operator = item.get("field", {}), item.get("operator")
            if field.get("kind") != "Column":
                continue
            table, column = field.get("table"), field.get("name")
            if table not in runtime.tables or column not in runtime.tables[table]:
                continue
            series = runtime.tables[table][column]
            if operator in {"in", "notIn"}:
                values = self._normalize_filter_values(series, item.get("values", []))
                mask = series.isin(values)
                if operator == "notIn": mask = ~mask
            else:
                value = self._normalize_filter_values(series, [item.get("value")])[0]
                mask = self._compare(series, operator, value)
            result.add(Condition(table, mask, column), replace=False)
        return result

    def _required_measures(self, visible: list[dict[str, Any]], visual: dict[str, Any]) -> list[dict[str, Any]]:
        result = list(visible)
        known = {(field["table"], field["name"]) for field in result}
        candidates = [item.get("field", {}) for item in visual.get("filters", [])] + [item.get("field", {}) for item in visual.get("sort", [])]
        for field in candidates:
            key = (field.get("table"), field.get("name"))
            if field.get("kind") == "Measure" and key not in known:
                result.append(self._enrich_measure({**field, "role": "Internal"}))
                known.add(key)
        return result

    @staticmethod
    def _compare(left: Any, operator: str, right: Any) -> Any:
        if operator == "gt": return left > right
        if operator == "gte": return left >= right
        if operator == "lt": return left < right
        if operator == "lte": return left <= right
        if operator == "neq": return left != right
        return left == right

    def _post_process(self, rows: list[dict[str, Any]], visual: dict[str, Any], groups: list[dict[str, Any]], visible: list[dict[str, Any]], limit: int, drop_empty: bool = False) -> list[dict[str, Any]]:
        for item in visual.get("filters", []):
            field = item.get("field", {})
            if field.get("kind") != "Measure":
                continue
            key, operator = self._key(field), item.get("operator")
            if operator in {"in", "notIn"}:
                allowed = item.get("values", [])
                rows = [row for row in rows if (row.get(key) in allowed) != (operator == "notIn")]
            else:
                value = item.get("value")
                rows = [row for row in rows if row.get(key) is not None and bool(self._compare(row.get(key), operator, value))]
        if drop_empty and visible:
            measure_keys = [self._key(field) for field in visible]
            rows = [row for row in rows if any(row.get(key) is not None for key in measure_keys)]
        rows = self._sort_rows(rows, visual.get("sort", []), groups)
        allowed_keys = {self._key(field) for field in groups + visible}
        return [{key: value for key, value in row.items() if key in allowed_keys} for row in rows[:limit]]

    def _sort_rows(self, rows: list[dict[str, Any]], sorts: list[dict[str, Any]], groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not sorts or not rows:
            return rows
        category_key = self._key(groups[0]) if groups else None
        for spec in reversed(sorts):
            field, descending = spec.get("field", {}), str(spec.get("direction", "Ascending")).lower().startswith("desc")
            key = self._key(field)
            if category_key and field.get("kind") == "Measure" and len(groups) > 1:
                totals: dict[Any, float] = {}
                for row in rows:
                    value = row.get(key)
                    totals[row.get(category_key)] = totals.get(row.get(category_key), 0.0) + (float(value) if isinstance(value, (int, float)) else 0.0)
                rows.sort(key=lambda row: totals.get(row.get(category_key), 0.0), reverse=descending)
            else:
                populated = [row for row in rows if row.get(key) is not None]
                empty = [row for row in rows if row.get(key) is None]
                try:
                    populated.sort(key=lambda row: row.get(key), reverse=descending)
                except TypeError:
                    populated.sort(key=lambda row: str(row.get(key)), reverse=descending)
                rows = populated + empty
        return rows

    def _distinct(self, field: dict[str, Any], context: FilterContext, limit: int) -> list[Any]:
        runtime = self._ensure_runtime()
        table, column = field["table"], field["name"]
        series = runtime.tables[table].loc[runtime.mask(table, context), column].drop_duplicates().dropna()
        try: series = series.sort_values()
        except TypeError: pass
        return series.head(limit).tolist()

    def _group_rows(self, columns: list[dict[str, Any]], measures: list[dict[str, Any]], context: FilterContext, limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        def visit(index: int, current: FilterContext, values: OrderedDict[str, Any]) -> None:
            if len(rows) >= limit:
                return
            if index == len(columns):
                result = OrderedDict((key, _python(value)) for key, value in values.items())
                for measure in measures:
                    item = self._measure_value(measure, current)
                    result[self._key(measure)] = item["value"]
                rows.append(result)
                return
            field = columns[index]
            runtime = self._ensure_runtime()
            series = runtime.tables[field["table"]][field["name"]]
            for value in self._distinct(field, current, limit - len(rows)):
                child = current.copy()
                child.add(Condition(field["table"], series == value, field["name"]))
                values[self._key(field)] = value
                visit(index + 1, child, values)
                values.pop(self._key(field), None)

        visit(0, context, OrderedDict())
        return rows

    def _table_rows(self, columns: list[dict[str, Any]], measures: list[dict[str, Any]], context: FilterContext, limit: int) -> list[dict[str, Any]]:
        runtime = self._ensure_runtime()
        if not columns:
            return self._group_rows(columns, measures, context, limit)
        table_names = {field["table"] for field in columns}
        source_table: str | None = next(iter(table_names)) if len(table_names) == 1 else None
        # Auto-exist: dimensões Supervisor/Operador coexistem na fato Atendimentos.
        if source_table is None and "Atendimentos" in runtime.tables and all(field["name"] in runtime.tables["Atendimentos"] for field in columns):
            source_table = "Atendimentos"
        if source_table is None:
            return self._group_rows(columns, measures, context, limit)
        frame = runtime.tables[source_table].loc[runtime.mask(source_table, context), [field["name"] for field in columns]]
        frame = frame.drop_duplicates().head(limit)
        rows: list[dict[str, Any]] = []
        for _, record in frame.iterrows():
            child = context.copy()
            result: OrderedDict[str, Any] = OrderedDict()
            for field in columns:
                value = record[field["name"]]
                series = runtime.tables[field["table"]][field["name"]]
                child.add(Condition(field["table"], series.isna() if value is None else series == value, field["name"]))
                result[self._key(field)] = _python(value)
            for measure in measures:
                result[self._key(measure)] = self._measure_value(measure, child)["value"]
            rows.append(result)
        return rows

    def _measure_value(self, field: dict[str, Any], context: FilterContext) -> dict[str, Any]:
        runtime = self._ensure_runtime()
        value = _python(runtime.evaluate_measure(field["name"], context))
        meta = runtime.measures.get(field["name"].casefold(), {})
        return {"field": field, "value": value, "formatted": self.format_value(value, meta.get("formatString"))}

    def _enrich_measure(self, field: dict[str, Any]) -> dict[str, Any]:
        runtime = self._ensure_runtime()
        meta = runtime.measures.get(field["name"].casefold(), {})
        return {**field, "formatString": meta.get("formatString")}

    @staticmethod
    def _key(field: dict[str, Any]) -> str:
        return f"{field['table']}.{field['name']}"

    @staticmethod
    def format_value(value: Any, format_string: str | None) -> str:
        if value is None: return "–"
        if isinstance(value, str):
            if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
                return value.replace("T", " ", 1)[:19]
            return value
        fmt = format_string or ""
        if "%" in fmt: return f"{value * 100:.1f}%"
        if ".0" in fmt: return f"{value:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
        if isinstance(value, float) and not value.is_integer(): return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{value:,.0f}".replace(",", ".")

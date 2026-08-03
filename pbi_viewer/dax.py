from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any


class DAXError(ValueError):
    pass


_UNSUPPORTED = object()


@dataclass
class ColumnValue:
    table: str
    column: str
    values: Any


@dataclass
class VirtualTable:
    frame: Any
    source: str | None = None


@dataclass
class Condition:
    table: str
    mask: Any
    column: str | None = None


@dataclass
class FilterContext:
    columns: dict[tuple[str, str], Any] = field(default_factory=dict)
    tables: dict[str, list[Any]] = field(default_factory=dict)
    resolved: dict[str, Any] | None = field(default=None, repr=False)
    row: dict[tuple[str, str], Any] = field(default_factory=dict, repr=False)

    def copy(self) -> "FilterContext":
        return FilterContext(dict(self.columns), {key: list(value) for key, value in self.tables.items()}, row=dict(self.row))

    def add(self, condition: Condition, replace: bool = True) -> None:
        self.resolved = None
        if condition.column:
            key = (condition.table, condition.column)
            if replace or key not in self.columns:
                self.columns[key] = condition.mask
            else:
                self.columns[key] = self.columns[key] & condition.mask
        else:
            self.tables.setdefault(condition.table, []).append(condition.mask)


def _strip_outer(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        quoted = False
        valid = True
        for index, char in enumerate(expr):
            if char == '"' and (index == 0 or expr[index - 1] != "\\"):
                quoted = not quoted
            if quoted:
                continue
            if char == "(": depth += 1
            elif char == ")": depth -= 1
            if depth == 0 and index != len(expr) - 1:
                valid = False
                break
        if not valid:
            break
        expr = expr[1:-1].strip()
    return expr


def _split_args(text: str) -> list[str]:
    result: list[str] = []
    start = 0
    paren = brace = bracket = 0
    quoted = False
    for index, char in enumerate(text):
        if char == '"' and (index == 0 or text[index - 1] != "\\"):
            quoted = not quoted
        if quoted:
            continue
        if char == "(": paren += 1
        elif char == ")": paren -= 1
        elif char == "{": brace += 1
        elif char == "}": brace -= 1
        elif char == "[": bracket += 1
        elif char == "]": bracket -= 1
        elif char == "," and paren == brace == bracket == 0:
            result.append(text[start:index].strip())
            start = index + 1
    result.append(text[start:].strip())
    return result


def _find_operator(expr: str, operators: list[str]) -> tuple[int, str] | None:
    paren = brace = bracket = 0
    quoted = False
    found: tuple[int, str] | None = None
    upper = expr.upper()
    index = 0
    while index < len(expr):
        char = expr[index]
        if char == '"' and (index == 0 or expr[index - 1] != "\\"):
            quoted = not quoted
            index += 1
            continue
        if quoted:
            index += 1
            continue
        if char == "(": paren += 1
        elif char == ")": paren -= 1
        elif char == "{": brace += 1
        elif char == "}": brace -= 1
        elif char == "[": bracket += 1
        elif char == "]": bracket -= 1
        if paren == brace == bracket == 0:
            matched_length = 0
            for operator in operators:
                if not upper.startswith(operator, index):
                    continue
                if operator.strip().isalpha():
                    before = upper[index - 1] if index else " "
                    after_index = index + len(operator)
                    after = upper[after_index] if after_index < len(upper) else " "
                    if before.isalnum() or after.isalnum():
                        continue
                if operator in {"+", "-"} and (index == 0 or expr[index - 1] in "(,+-*/=<>"):
                    continue
                found = (index, operator)
                matched_length = len(operator)
                break
            if matched_length:
                index += matched_length
                continue
        index += 1
    return found


def _table_name(raw: str) -> str:
    raw = raw.strip()
    return raw[1:-1].replace("''", "'") if raw.startswith("'") and raw.endswith("'") else raw


def _scalar(value: Any) -> Any:
    if isinstance(value, ColumnValue):
        return value.values
    return value


class DAXRuntime:
    """Executor vetorizado do subconjunto DAX usado pelos relatórios importados."""

    def __init__(self, tables: dict[str, Any], relationships: list[dict[str, str]], measures: dict[str, dict[str, Any]]):
        self.tables = tables
        self.relationships = relationships
        self.measures = measures
        self._measure_stack: list[str] = []

    def mask(self, table: str, context: FilterContext) -> Any:
        import pandas as pd

        if context.resolved is not None:
            return context.resolved[table]

        masks = {name: pd.Series(True, index=data.index) for name, data in self.tables.items()}
        for (name, _), condition in context.columns.items():
            if name in masks:
                masks[name] &= condition.reindex(masks[name].index, fill_value=False)
        for name, conditions in context.tables.items():
            for condition in conditions:
                if name in masks:
                    masks[name] &= condition.reindex(masks[name].index, fill_value=False)
        # Relações tabulares são armazenadas como From (muitos) -> To (um).
        for _ in range(len(self.relationships) + 1):
            changed = False
            for relation in self.relationships:
                many, one = relation["fromTable"], relation["toTable"]
                if many not in masks or one not in masks:
                    continue
                many_col, one_col = relation["fromColumn"], relation["toColumn"]
                if bool(masks[one].all()):
                    continue
                allowed = set(self.tables[one].loc[masks[one], one_col].dropna().tolist())
                propagated = masks[many] & self.tables[many][many_col].isin(allowed)
                if not propagated.equals(masks[many]):
                    masks[many] = propagated
                    changed = True
            if not changed:
                break
        context.resolved = masks
        return masks[table]

    def column(self, table: str, column: str) -> ColumnValue:
        if table not in self.tables or column not in self.tables[table]:
            raise DAXError(f"Coluna não encontrada: {table}[{column}]")
        return ColumnValue(table, column, self.tables[table][column])

    def _row_context(self, context: FilterContext, table: str | None, row: Any) -> FilterContext:
        child = context.copy()
        if table and table in self.tables:
            for column in row.index:
                child.row[(table, column)] = row[column]
                if column not in self.tables[table]:
                    continue
                value = row[column]
                series = self.tables[table][column]
                child.add(Condition(table, series.isna() if value is None else series == value, column), replace=False)
        return child

    def evaluate_measure(self, name: str, context: FilterContext | None = None) -> Any:
        context = context or FilterContext()
        measure = self.measures.get(name.casefold())
        if not measure:
            raise DAXError(f"Medida não encontrada: {name}")
        if name.casefold() in self._measure_stack:
            raise DAXError(f"Dependência circular na medida {name}")
        self._measure_stack.append(name.casefold())
        try:
            # Power BI commonly uses this compact pattern for a KPI label:
            # VAR T = TOPN(1, ADDCOLUMNS(VALUES(Table[Column]), ...))
            # RETURN COALESCE(CONCATENATEX(T, Table[Column]), "—")
            # Evaluate it directly over the filtered column while retaining
            # the model's relationship/filter semantics.
            topn_text = self._evaluate_topn_text(measure["expression"], context)
            if topn_text is not _UNSUPPORTED:
                return topn_text
            return self.evaluate(measure["expression"], context)
        finally:
            self._measure_stack.pop()

    def _evaluate_topn_text(self, expression: str, context: FilterContext) -> Any:
        import re

        match = re.search(
            r"TOPN\s*\(\s*1\s*,\s*ADDCOLUMNS\s*\(\s*VALUES\s*\(\s*('(?:[^']|'')+'|[^'\[]+)\[([^\]]+)\]",
            expression,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match or "CONCATENATEX" not in expression.upper():
            return _UNSUPPORTED
        table, column = _table_name(match.group(1)), match.group(2)
        if table not in self.tables or column not in self.tables[table]:
            return _UNSUPPORTED
        series = self.tables[table].loc[self.mask(table, context), column].dropna()
        if series.empty:
            return "—"
        counts = series.value_counts(dropna=True)
        # TOPN is ordered by the generated count descending and the source
        # column ascending, matching the expression emitted by Power BI.
        best_count = counts.max()
        candidates = sorted((value for value, count in counts.items() if count == best_count), key=lambda value: str(value))
        return str(candidates[0]) if candidates else "—"

    def evaluate(self, expression: str, context: FilterContext) -> Any:
        expr = _strip_outer(expression)
        if not expr:
            return None
        if re.match(r"^VAR\s+", expr, re.IGNORECASE):
            return self._evaluate_vars(expr, context)
        if expr.startswith('"') and expr.endswith('"'):
            return expr[1:-1].replace('""', '"')
        if re.fullmatch(r"-?\d+(?:\.\d+)?", expr):
            return float(expr) if "." in expr else int(expr)
        if expr.upper() in {"TRUE", "TRUE()"}: return True
        if expr.upper() in {"FALSE", "FALSE()"}: return False
        for operators in (["||"], ["&&"], [" IN "], [">=", "<=", "<>", "=", ">", "<"], ["+", "-"], ["*", "/"]):
            found = _find_operator(expr, operators)
            if found:
                index, operator = found
                left = self.evaluate(expr[:index], context)
                right_text = expr[index + len(operator):]
                if operator.strip() == "IN":
                    values = [_scalar(self.evaluate(item, context)) for item in _split_args(right_text.strip()[1:-1])]
                    return self._compare(left, values, "IN")
                right = self.evaluate(right_text, context)
                if operator in {"=", "<>", ">", "<", ">=", "<="}:
                    return self._compare(left, right, operator)
                if operator in {"&&", "||"}:
                    return self._boolean(left, right, operator)
                a, b = _scalar(left), _scalar(right)
                if operator == "+": return a + b
                if operator == "-": return (0 if a is None else a) - (0 if b is None else b)
                if operator == "*": return (0 if a is None else a) * (0 if b is None else b)
                if operator == "/": return None if b in (0, None) else a / b

        if expr.upper().startswith("NOT "):
            value = self.evaluate(expr[4:], context)
            return Condition(value.table, ~value.mask, value.column) if isinstance(value, Condition) else not bool(value)

        column = re.fullmatch(r"('(?:[^']|'')+'|[^'\[]+)\[([^]]+)\]", expr)
        if column:
            table, name = _table_name(column.group(1)), column.group(2)
            key = (table, name)
            if key in context.row:
                return context.row[key]
            return self.column(table, name)
        measure = re.fullmatch(r"\[([^]]+)\]", expr)
        if measure:
            row_name = measure.group(1)
            for (_, row_column), row_value in context.row.items():
                if row_column.casefold() == row_name.casefold():
                    return row_value
            return self.evaluate_measure(measure.group(1), context)
        call = re.fullmatch(r"([A-Za-z]+)\((.*)\)", expr, re.DOTALL)
        if call:
            return self._call(call.group(1).upper(), _split_args(call.group(2)), context)
        if expr in self.tables:
            return expr
        if expr.startswith("'") and expr.endswith("'") and _table_name(expr) in self.tables:
            return _table_name(expr)
        raise DAXError(f"Expressão DAX não suportada: {expr}")

    def _evaluate_vars(self, expression: str, context: FilterContext) -> Any:
        body = expression.strip()[3:].strip()
        # Find the top-level RETURN; commas/parentheses inside expressions do
        # not terminate a variable declaration.
        depth = 0
        quoted = False
        return_at = None
        for i, char in enumerate(body):
            if char == '"': quoted = not quoted
            if quoted: continue
            if char == '(': depth += 1
            elif char == ')': depth -= 1
            elif depth == 0 and body[i:i + 6].upper() == 'RETURN' and (i == 0 or body[i - 1].isspace()):
                return_at = i
                break
        if return_at is None:
            raise DAXError("VAR sem RETURN")
        declarations, result_expr = body[:return_at].strip(), body[return_at + 6:].strip()
        variables: dict[str, Any] = {}
        while declarations:
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*", declarations)
            if not match:
                raise DAXError(f"Declaração VAR inválida: {declarations[:40]}")
            name = match.group(1).casefold()
            start = match.end(); depth = 0; quoted = False; next_var = None
            for i in range(start, len(declarations)):
                char = declarations[i]
                if char == '"': quoted = not quoted
                if quoted: continue
                if char == '(': depth += 1
                elif char == ')': depth -= 1
                if depth == 0 and declarations[i:i + 3].upper() == 'VAR' and (i == 0 or declarations[i - 1].isspace()):
                    next_var = i; break
            raw_value = declarations[start:next_var].strip() if next_var is not None else declarations[start:].strip()
            variables[name] = self._evaluate_with_variables(raw_value, context, variables)
            if next_var is None: break
            declarations = declarations[next_var + 3:].strip()
        return self._evaluate_with_variables(result_expr, context, variables)

    def _evaluate_with_variables(self, expression: str, context: FilterContext, variables: dict[str, Any]) -> Any:
        expr = expression.strip()
        if expr.casefold() in variables:
            return variables[expr.casefold()]
        for name, value in variables.items():
            expr = re.sub(rf"\b{re.escape(name)}\b", f"__VAR_{name}", expr, flags=re.IGNORECASE)
        # Replace variable tokens with quoted-safe direct values by evaluating
        # function arguments ourselves; scalar variables are handled here.
        for name, value in variables.items():
            token = f"__VAR_{name}"
            if token in expr:
                if isinstance(value, VirtualTable):
                    self._active_variables = variables
                    return self._call_with_variables(expr, context, variables)
                expr = expr.replace(token, repr(value) if not isinstance(value, str) else '"' + value.replace('"', '""') + '"')
        self._active_variables = variables
        return self.evaluate(expr, context)

    def _call_with_variables(self, expression: str, context: FilterContext, variables: dict[str, Any]) -> Any:
        call = re.fullmatch(r"([A-Za-z]+)\((.*)\)", expression, re.DOTALL)
        if not call: raise DAXError(f"Variável de tabela não suportada: {expression[:40]}")
        name, args = call.group(1).upper(), _split_args(call.group(2))
        return self._call(name, args, context)

    def _compare(self, left: Any, right: Any, operator: str) -> Any:
        column = left if isinstance(left, ColumnValue) else right if isinstance(right, ColumnValue) else None
        a, b = _scalar(left), _scalar(right)
        if not isinstance(a, (ColumnValue,)) and not hasattr(a, "dtype") and a is None and isinstance(b, (int, float)):
            a = 0
        if not hasattr(b, "dtype") and b is None and isinstance(a, (int, float)):
            b = 0
        if operator == "=": result = a == b
        elif operator == "<>": result = a != b
        elif operator == ">": result = a > b
        elif operator == "<": result = a < b
        elif operator == ">=": result = a >= b
        elif operator == "<=": result = a <= b
        elif operator == "IN": result = a.isin(b) if hasattr(a, "isin") else a in b
        else: raise DAXError(operator)
        return Condition(column.table, result.fillna(False), column.column) if column else bool(result)

    def _boolean(self, left: Any, right: Any, operator: str) -> Any:
        if isinstance(left, Condition) and isinstance(right, Condition):
            if left.table != right.table:
                raise DAXError("Predicado entre tabelas diferentes")
            mask = left.mask & right.mask if operator == "&&" else left.mask | right.mask
            column = left.column if left.column == right.column else None
            return Condition(left.table, mask, column)
        return bool(left and right) if operator == "&&" else bool(left or right)

    def _call(self, name: str, args: list[str], context: FilterContext) -> Any:
        import pandas as pd

        variables = getattr(self, "_active_variables", {})
        def value(raw: str, current: FilterContext = context) -> Any:
            token = raw.strip().casefold()
            if token in variables: return variables[token]
            if token.startswith("__var_") and token[6:] in variables: return variables[token[6:]]
            return self.evaluate(raw, current)

        if name in {"VALUES", "DISTINCT"}:
            item = value(args[0])
            if not isinstance(item, ColumnValue): raise DAXError(f"{name} requer uma coluna")
            series = item.values[self.mask(item.table, context)].drop_duplicates().dropna().reset_index(drop=True)
            return VirtualTable(__import__("pandas").DataFrame({item.column: series}), item.table)
        if name == "ALL":
            raw = args[0].strip(); table = _table_name(raw)
            if table in self.tables: return VirtualTable(self.tables[table].copy(), table)
            item = value(raw)
            if isinstance(item, ColumnValue): return VirtualTable(__import__("pandas").DataFrame({item.column: item.values.drop_duplicates()}), item.table)
        if name == "ADDCOLUMNS":
            base = value(args[0])
            if not isinstance(base, VirtualTable): raise DAXError("ADDCOLUMNS requer uma tabela")
            frame = base.frame.copy()
            for i in range(1, len(args), 2):
                col_name = args[i].strip().strip('"')
                values = []
                for _, row in frame.iterrows():
                    child = self._row_context(context, base.source, row)
                    values.append(_scalar(value(args[i + 1], child)))
                frame[col_name] = values
            return VirtualTable(frame, base.source)
        if name == "TOPN":
            n = int(_scalar(value(args[0]))); base = value(args[1])
            if not isinstance(base, VirtualTable): raise DAXError("TOPN requer uma tabela")
            frame = base.frame.copy()
            for i in range(2, len(args), 2):
                order_name = args[i].strip().strip('"')
                order_name = re.sub(r"^.*\[([^]]+)\]$", r"\1", order_name)
                direction = args[i + 1].strip().upper() if i + 1 < len(args) else "DESC"
                if order_name in frame.columns: frame["__sort"] = frame[order_name]
            if "__sort" in frame: frame = frame.sort_values("__sort", ascending=direction != "DESC", kind="stable").drop(columns=["__sort"])
            return VirtualTable(frame.head(n), base.source)
        if name == "CONCATENATEX":
            base = value(args[0]);
            if not isinstance(base, VirtualTable): raise DAXError("CONCATENATEX requer uma tabela")
            delimiter = str(_scalar(value(args[2]))) if len(args) > 2 else ", "
            vals = []
            for _, row in base.frame.iterrows():
                child = self._row_context(context, base.source, row)
                vals.append(str(_scalar(value(args[1], child))))
            return delimiter.join(vals)
        if name in {"SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX"}:
            base = value(args[0])
            if not isinstance(base, VirtualTable): raise DAXError(f"{name} requer uma tabela")
            vals=[]
            for _, row in base.frame.iterrows():
                child=self._row_context(context, base.source, row)
                item=_scalar(value(args[1], child));
                if item is not None: vals.append(item)
            if name == "COUNTX": return len(vals)
            if not vals: return None
            if name == "SUMX": return sum(vals)
            if name == "AVERAGEX": return sum(vals)/len(vals)
            return max(vals) if name == "MAXX" else min(vals)
        if name == "COALESCE":
            for raw in args:
                item = _scalar(value(raw))
                if item is not None and not (isinstance(item, float) and pd.isna(item)): return item
            return None

        if name == "CALCULATE":
            modified = context.copy()
            added: set[tuple[str, str]] = set()
            for raw in args[1:]:
                condition = self.evaluate(raw, context)
                if not isinstance(condition, Condition):
                    raise DAXError(f"Filtro CALCULATE inválido: {raw}")
                key = (condition.table, condition.column or "")
                modified.add(condition, replace=condition.column is not None and key not in added)
                added.add(key)
            return self.evaluate(args[0], modified)
        if name == "FILTER":
            table = _table_name(args[0])
            condition = self.evaluate(args[1], context)
            if not isinstance(condition, Condition) or condition.table != table:
                raise DAXError("FILTER requer predicado da mesma tabela")
            return Condition(table, self.mask(table, context) & condition.mask)
        if name == "DIVIDE":
            numerator = _scalar(self.evaluate(args[0], context))
            denominator = _scalar(self.evaluate(args[1], context))
            alternate = _scalar(self.evaluate(args[2], context)) if len(args) > 2 else None
            return alternate if denominator in (0, None) or pd.isna(denominator) else numerator / denominator
        if name == "IF":
            condition = self.evaluate(args[0], context)
            return self.evaluate(args[1], context) if bool(condition) else self.evaluate(args[2], context) if len(args) > 2 else None
        if name == "NOT":
            value = self.evaluate(args[0], context)
            return Condition(value.table, ~value.mask, value.column) if isinstance(value, Condition) else not bool(value)
        if name == "ISBLANK":
            value = self.evaluate(args[0], context)
            if isinstance(value, ColumnValue):
                return Condition(value.table, value.values.isna(), value.column)
            return value is None or bool(pd.isna(value))
        if name == "CONTAINSSTRING":
            value = self.evaluate(args[0], context)
            needle = str(_scalar(self.evaluate(args[1], context)))
            if isinstance(value, ColumnValue):
                mask = value.values.astype("string").str.contains(needle, case=False, regex=False, na=False)
                return Condition(value.table, mask, value.column)
            return needle.casefold() in str(value).casefold()
        if name == "COUNTROWS":
            table = _table_name(args[0])
            return int(self.mask(table, context).sum())

        value = self.evaluate(args[0], context)
        if not isinstance(value, ColumnValue):
            raise DAXError(f"{name} requer uma coluna")
        series = value.values[self.mask(value.table, context)]
        if name == "SUM": return _python(series.sum())
        if name == "AVERAGE": return _python(series.mean())
        if name == "MAX": return _python(series.max())
        if name == "MIN": return _python(series.min())
        if name == "COUNT": return int(series.count())
        if name == "COUNTA": return int(series.count())
        if name == "DISTINCTCOUNT": return int(series.nunique(dropna=False))
        raise DAXError(f"Função DAX não suportada: {name}")


def _python(value: Any) -> Any:
    try:
        import pandas as pd
        if pd.isna(value): return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value

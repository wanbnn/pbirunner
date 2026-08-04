from __future__ import annotations

import base64
import json
import mimetypes
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable


class PBIParseError(ValueError):
    """Arquivo não suportado ou projeto Power BI inválido."""


def _json(data: bytes | str) -> dict[str, Any]:
    if isinstance(data, bytes):
        # Alguns artefatos legados usam UTF-16.
        for encoding in ("utf-8-sig", "utf-16-le", "utf-16"):
            try:
                return json.loads(data.decode(encoding))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        raise PBIParseError("JSON interno inválido")
    return json.loads(data)


def _literal(node: Any, default: Any = None) -> Any:
    """Obtém um Literal.Value PBIR, convertendo os tipos mais comuns."""
    if not isinstance(node, dict):
        return default
    value = node
    for key in ("expr", "Literal", "Value"):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    if not isinstance(value, str):
        return value
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    number = re.fullmatch(r"(-?\d+(?:\.\d+)?)[dDlL]?", value)
    if number:
        raw = number.group(1)
        return float(raw) if "." in raw else int(raw)
    return value


def _color(props: dict[str, Any], key: str, fallback: str) -> str:
    value = props.get(key, {})
    if isinstance(value, dict):
        value = value.get("solid", {}).get("color", value)
    if isinstance(value, str):
        return value
    return str(_literal(value, fallback))


def _property(props: dict[str, Any], key: str, fallback: Any = None) -> Any:
    value = props.get(key, fallback)
    if isinstance(value, dict) and "expr" in value:
        return _literal(value, fallback)
    return value


def _object_properties(objects: dict[str, Any], name: str, selector: str | None = None) -> dict[str, Any]:
    items = objects.get(name, [])
    if not isinstance(items, list):
        return {}
    for item in items:
        item_selector = item.get("selector", {}).get("id")
        if selector is None and item_selector is None:
            return item.get("properties", {key: value for key, value in item.items() if key != "selector"})
        if selector == item_selector:
            return item.get("properties", {key: value for key, value in item.items() if key != "selector"})
    return items[0].get("properties", items[0]) if items and selector is None else {}


def _title_from_visual(raw: dict[str, Any]) -> str | None:
    container = raw.get("visual", {}).get("visualContainerObjects", {})
    title_items = container.get("title", [])
    if title_items:
        props = title_items[0].get("properties", {})
        if _literal(props.get("show"), True) is False:
            return None
        text = _literal(props.get("text"))
        if text:
            return str(text)
    return None


def _textbox_content(raw: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    general = _object_properties(raw.get("visual", {}).get("objects", {}), "general")
    paragraphs = general.get("paragraphs", [])
    normalized: list[dict[str, Any]] = []
    plain: list[str] = []
    for paragraph in paragraphs:
        runs: list[dict[str, Any]] = []
        for run in paragraph.get("textRuns", []):
            value = str(run.get("value", ""))
            style = run.get("textStyle", {})
            plain.append(value)
            runs.append({
                "value": value,
                "fontFamily": style.get("fontFamily", "Segoe UI"),
                "fontSize": style.get("fontSize", "12px"),
                "color": style.get("color", "#252a34"),
                "bold": style.get("fontWeight") == "bold",
                "italic": style.get("fontStyle") == "italic",
                "underline": bool(style.get("textDecoration") == "underline"),
            })
        normalized.append({
            "alignment": paragraph.get("horizontalTextAlignment", "left"),
            "runs": runs,
        })
        plain.append("\n")
    text = "".join(plain).strip()
    return text or None, normalized


def _field(field: dict[str, Any]) -> dict[str, str] | None:
    for kind in ("Measure", "Column", "Aggregation", "HierarchyLevel"):
        spec = field.get(kind)
        if not isinstance(spec, dict):
            continue
        entity = spec.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
        prop = spec.get("Property", "")
        if kind == "Aggregation":
            inner = spec.get("Expression", {}).get("Column", {})
            entity = inner.get("Expression", {}).get("SourceRef", {}).get("Entity", entity)
            prop = inner.get("Property", prop)
        return {"kind": kind, "table": str(entity), "name": str(prop)}
    return None


def _theme_defaults(theme: dict[str, Any]) -> dict[str, Any]:
    generic = theme.get("visualStyles", {}).get("*", {}).get("*", {})
    return {
        "background": _object_properties(generic, "background"),
        "border": _object_properties(generic, "border"),
        "padding": _object_properties(generic, "padding"),
    }


def _theme_visual_objects(theme: dict[str, Any], visual_type: str) -> dict[str, Any]:
    styles = theme.get("visualStyles", {})
    merged: dict[str, Any] = {}
    for group in (styles.get("*", {}).get("*", {}), styles.get(visual_type, {}).get("*", {})):
        for name, items in group.items():
            merged[name] = items
    return merged


def _category_colors(objects: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in objects.get("dataPoint", []):
        comparison = (item.get("selector", {}).get("data") or [{}])[0].get("scopeId", {}).get("Comparison", {})
        raw_value = comparison.get("Right", {}).get("Literal", {}).get("Value")
        if raw_value is None:
            continue
        value = str(raw_value)
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1].replace("''", "'")
        color = _color(item.get("properties", {}), "fill", "")
        if color:
            result[value] = color
    return result


def _node_literal(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    if "Literal" in node:
        value = node["Literal"].get("Value")
        return _literal({"expr": {"Literal": {"Value": value}}}, value)
    if "Value" in node:
        return _literal({"expr": {"Literal": node}}, node.get("Value"))
    return None


def _visual_filters(raw: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    comparison_kinds = {0: "eq", 1: "gt", 2: "gte", 3: "lt", 4: "lte"}
    inverse = {"eq": "neq", "in": "notIn", "gt": "lte", "gte": "lt", "lt": "gte", "lte": "gt"}

    def parse_condition(condition: dict[str, Any], fallback: dict[str, Any] | None, negated: bool = False) -> None:
        if "Not" in condition:
            nested = condition["Not"].get("Expression", condition["Not"])
            parse_condition(nested, fallback, not negated)
            return
        if "In" in condition:
            spec = condition["In"]
            expressions = spec.get("Expressions", [])
            field = _field(expressions[0]) if expressions else fallback
            if field and not field.get("table") and fallback:
                field = {**field, "table": fallback.get("table", "")}
            values = [_node_literal(row[0]) for row in spec.get("Values", []) if row]
            if field and values:
                result.append({"field": field, "operator": "notIn" if negated else "in", "values": values})
            return
        if "Comparison" in condition:
            spec = condition["Comparison"]
            field = _field(spec.get("Left", {})) or fallback
            if field and not field.get("table") and fallback:
                field = {**field, "table": fallback.get("table", "")}
            value = _node_literal(spec.get("Right", {}))
            raw_kind = spec.get("ComparisonKind", spec.get("Kind", 0))
            operator = comparison_kinds.get(raw_kind, str(raw_kind).lower())
            aliases = {"greaterthan": "gt", "greaterthanorequal": "gte", "lessthan": "lt", "lessthanorequal": "lte", "equal": "eq"}
            operator = aliases.get(operator.replace("_", ""), operator)
            if negated:
                operator = inverse.get(operator, operator)
            if field and value is not None:
                result.append({"field": field, "operator": operator, "value": value})

    for item in raw.get("filterConfig", {}).get("filters", []):
        fallback = _field(item.get("field", {}))
        for clause in item.get("filter", {}).get("Where", []):
            condition = clause.get("Condition", {})
            if isinstance(condition, dict):
                parse_condition(condition, fallback)
    return result


def _visual_sort(visual: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in visual.get("query", {}).get("sortDefinition", {}).get("sort", []):
        field = _field(item.get("field", {}))
        if field:
            result.append({"field": field, "direction": str(item.get("direction", "Ascending"))})
    return result


def _navigator_orientation(value: Any, position: dict[str, Any]) -> str:
    """Normalize PBIR's numeric grid-orientation enum to a renderable mode.

    PBIR stores this property as a literal (for example ``1D``), which the
    literal reader exposes as the number 1.  The page navigator enum is
    Horizontal=0, Vertical=1, Grid=2.  Older/custom files may contain the
    names directly, so keep those forms supported and use the visual bounds
    as a safe fallback for unknown values.
    """
    fallback = "Vertical" if position.get("height", 0) > position.get("width", 0) else "Horizontal"
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        return {0: "Horizontal", 1: "Vertical", 2: "Grid"}.get(int(value), fallback)
    text = str(value or "").strip().lower()
    if text in {"0", "0d", "horizontal"}:
        return "Horizontal"
    if text in {"1", "1d", "vertical"}:
        return "Vertical"
    if text in {"2", "2d", "grid"}:
        return "Grid"
    return fallback


def _visual(raw: dict[str, Any], theme: dict[str, Any]) -> dict[str, Any]:
    visual = raw.get("visual", {})
    query_state = visual.get("query", {}).get("queryState", {})
    fields: list[dict[str, str]] = []
    roles: dict[str, list[str]] = {}
    for role, state in query_state.items():
        role_fields: list[str] = []
        for projection in state.get("projections", []):
            parsed = _field(projection.get("field", {}))
            if parsed:
                parsed["role"] = role
                fields.append(parsed)
                role_fields.append(f"{parsed['table']}.{parsed['name']}".strip("."))
        if role_fields:
            roles[role] = role_fields
    position = raw.get("position", {})
    objects = visual.get("objects", {})
    container = visual.get("visualContainerObjects", {})
    defaults = _theme_defaults(theme)
    theme_objects = _theme_visual_objects(theme, visual.get("visualType", ""))
    background_props = {**defaults["background"], **_object_properties(container, "background")}
    border_props = {**defaults["border"], **_object_properties(container, "border")}
    padding_props = {**defaults["padding"], **_object_properties(container, "padding")}
    title_props = _object_properties(container, "title")
    label_props = {**_object_properties(theme_objects, "dataLabels"), **_object_properties(objects, "dataLabels")}
    category_props = {**_object_properties(theme_objects, "categoryLabels"), **_object_properties(objects, "categoryLabels")}
    card_props = {**_object_properties(theme_objects, "card"), **_object_properties(objects, "card")}
    legend_props = {**_object_properties(theme_objects, "legend"), **_object_properties(objects, "legend")}
    category_axis = {**_object_properties(theme_objects, "categoryAxis"), **_object_properties(objects, "categoryAxis")}
    value_axis = {**_object_properties(theme_objects, "valueAxis"), **_object_properties(objects, "valueAxis")}
    x_axis = {**category_axis, **_object_properties(theme_objects, "xAxis"), **_object_properties(objects, "xAxis")}
    y_axis = {**value_axis, **_object_properties(theme_objects, "yAxis"), **_object_properties(objects, "yAxis")}
    line_props = {**_object_properties(theme_objects, "lineStyles"), **_object_properties(objects, "lineStyles")}
    marker_props = {**_object_properties(theme_objects, "markers"), **_object_properties(objects, "markers")}
    layout_props = _object_properties(objects, "layout")
    nav_fill = _object_properties(objects, "fill", "default")
    nav_selected_fill = _object_properties(objects, "fill", "selected")
    nav_text = _object_properties(objects, "text", "default")
    nav_selected_text = _object_properties(objects, "text", "selected")
    data_point = _object_properties(objects, "dataPoint")
    text, paragraphs = _textbox_content(raw)
    palette = theme.get("dataColors", ["#5c3df5", "#0792E5", "#FF9C1A"])
    return {
        "id": raw.get("name", ""),
        "type": visual.get("visualType", "unknown"),
        "title": _title_from_visual(raw),
        "text": text,
        "paragraphs": paragraphs,
        "position": {key: position.get(key, 0) for key in ("x", "y", "z", "width", "height")},
        "fields": fields,
        "roles": roles,
        "filters": _visual_filters(raw),
        "sort": _visual_sort(visual),
        "style": {
            "backgroundShow": bool(_property(background_props, "show", True)),
            "background": _color(background_props, "color", str(theme.get("background", "#ffffff"))),
            "transparency": _property(background_props, "transparency", 0),
            "border": _color(border_props, "color", "#d8dee8"),
            "borderShow": bool(_property(border_props, "show", False)),
            "borderRadius": _property(border_props, "radius", 0),
            "borderWidth": _property(border_props, "width", 1),
            "padding": {side: _property(padding_props, side, 0) for side in ("top", "right", "bottom", "left")},
            "title": {
                "color": _color(title_props, "fontColor", theme.get("foreground", "#252a34")),
                "fontSize": _property(title_props, "fontSize", theme.get("textClasses", {}).get("title", {}).get("fontSize", 12)),
                "fontFamily": _property(title_props, "fontFamily", theme.get("textClasses", {}).get("title", {}).get("fontFace", "Segoe UI")),
                "bold": bool(_property(title_props, "bold", False)),
                "alignment": _property(title_props, "alignment", "left"),
            },
            "dataLabel": {
                "show": bool(_property(label_props, "show", False)),
                "color": _color(label_props, "color", theme.get("foreground", "#252a34")),
                "fontSize": _property(label_props, "fontSize", 12),
                "fontFamily": _property(label_props, "fontFamily", "Segoe UI"),
                "bold": bool(_property(label_props, "bold", False)),
                "precision": _property(label_props, "precision", None),
                "position": _property(label_props, "position", "Auto"),
            },
            "categoryLabel": {
                "show": bool(_property(category_props, "show", True)),
                "color": _color(category_props, "color", theme.get("secondLevelElements", "#657080")),
                "fontSize": _property(category_props, "fontSize", 10),
                "fontFamily": _property(category_props, "fontFamily", "Segoe UI"),
            },
            "card": {
                "background": _color(card_props, "cardBackground", "#ffffff"),
                "barShow": bool(_property(card_props, "barShow", False)),
                "barColor": _color(card_props, "barColor", palette[0]),
                "barWeight": _property(card_props, "barWeight", 0),
                "padding": _property(card_props, "cardPadding", 6),
            },
            "navigator": {
                "fill": _color(nav_fill, "fillColor", "#303748"),
                "selectedFill": _color(nav_selected_fill, "fillColor", palette[0]),
                "color": _color(nav_text, "fontColor", "#ffffff"),
                "selectedColor": _color(nav_selected_text, "fontColor", "#ffffff"),
                "fontSize": _property(nav_text, "fontSize", 9),
                "fontFamily": _property(nav_text, "fontFamily", "Segoe UI"),
                "bold": bool(_property(nav_text, "bold", False)),
                "orientation": _navigator_orientation(
                    _property(layout_props, "orientation", _property(layout_props, "gridOrientation", None)),
                    position,
                ),
                "cellPadding": _property(layout_props, "cellPadding", 4),
                "rows": _property(layout_props, "rows", None),
                "columns": _property(layout_props, "columns", None),
            },
            "primaryColor": _color(data_point, "fill", palette[0]),
            "palette": palette,
            "categoryColors": _category_colors(objects),
            "legend": {
                "show": bool(_property(legend_props, "show", True)),
                "position": _property(legend_props, "position", "Top"),
                "color": _color(legend_props, "labelColor", theme.get("secondLevelElements", "#657080")),
                "fontSize": _property(legend_props, "fontSize", 9),
                "fontFamily": _property(legend_props, "fontFamily", "Segoe UI"),
                "title": _literal(legend_props.get("titleText"), ""),
            },
            "xAxis": {
                "show": bool(_property(x_axis, "show", True)),
                "color": _color(x_axis, "labelColor", theme.get("secondLevelElements", "#657080")),
                "fontSize": _property(x_axis, "fontSize", 9),
                "fontFamily": _property(x_axis, "fontFamily", "Segoe UI"),
                "titleShow": bool(_property(x_axis, "showAxisTitle", False)),
                "title": _property(x_axis, "titleText", ""),
                "gridShow": bool(_property(x_axis, "gridlineShow", False)),
                "gridColor": _color(x_axis, "gridlineColor", theme.get("thirdLevelElements", "#e5e8eb")),
            },
            "yAxis": {
                "show": bool(_property(y_axis, "show", True)),
                "color": _color(y_axis, "labelColor", theme.get("secondLevelElements", "#657080")),
                "fontSize": _property(y_axis, "fontSize", 9),
                "fontFamily": _property(y_axis, "fontFamily", "Segoe UI"),
                "titleShow": bool(_property(y_axis, "showAxisTitle", False)),
                "title": _property(y_axis, "titleText", ""),
                "gridShow": bool(_property(y_axis, "gridlineShow", True)),
                "gridColor": _color(y_axis, "gridlineColor", theme.get("thirdLevelElements", "#e5e8eb")),
            },
            "line": {
                "width": _property(line_props, "strokeWidth", 2),
                "style": _property(line_props, "lineStyle", "solid"),
                "stepped": bool(_property(line_props, "stepped", False)),
                "markers": bool(_property(marker_props, "show", False)),
                "markerSize": _property(marker_props, "markerSize", 5),
            },
        },
        "raw": raw,
    }


def _page_background(page: dict[str, Any]) -> dict[str, Any]:
    props = ((page.get("objects", {}).get("background") or [{}])[0]).get("properties", {})
    image = props.get("image", {}).get("image", {})
    return {
        "color": _color(props, "color", "#f3f5f8"),
        "transparency": _literal(props.get("transparency"), 0),
        "image": image.get("url", {}).get("expr", {}).get("ResourcePackageItem", {}).get("ItemName"),
        "scaling": _literal(image.get("scaling"), "Fit"),
    }


def _page_hidden(page: dict[str, Any]) -> bool:
    if page.get("hidden") is True or page.get("isHidden") is True:
        return True
    visibility = str(page.get("visibility") or "").strip().casefold()
    return visibility.startswith("hidden")


class _Source:
    def __init__(self, read: Callable[[str], bytes], names: list[str], prefix: str = ""):
        self.read = read
        self.names = names
        self.prefix = prefix.rstrip("/")

    def path(self, relative: str) -> str:
        return f"{self.prefix}/{relative}" if self.prefix else relative

    def json(self, relative: str) -> dict[str, Any]:
        return _json(self.read(self.path(relative)))


def _parse_report(source: _Source) -> dict[str, Any]:
    report = source.json("definition/report.json")
    theme: dict[str, Any] = {}
    theme_name = report.get("themeCollection", {}).get("customTheme", {}).get("name")
    if theme_name:
        candidates = [name for name in source.names if name.endswith(f"/{theme_name}") or name == theme_name]
        if candidates:
            theme = _json(source.read(candidates[0]))
    pages_meta = source.json("definition/pages/pages.json")
    order = pages_meta.get("pageOrder", [])
    pages: list[dict[str, Any]] = []
    for page_id in order:
        page = source.json(f"definition/pages/{page_id}/page.json")
        visual_prefix = source.path(f"definition/pages/{page_id}/visuals/")
        visual_names = sorted(
            name for name in source.names if name.startswith(visual_prefix) and name.endswith("/visual.json")
        )
        visuals = [_visual(_json(source.read(name)), theme) for name in visual_names]
        visuals.sort(key=lambda item: item["position"]["z"])
        pages.append({
            "id": page.get("name", page_id),
            "name": page.get("displayName", page_id),
            "hidden": _page_hidden(page),
            "visibility": page.get("visibility", "Visible"),
            "width": page.get("width", 1280),
            "height": page.get("height", 720),
            "displayOption": page.get("displayOption", "FitToPage"),
            "background": _page_background(page),
            "visuals": visuals,
            "raw": page,
        })
    return {"pages": pages, "activePage": pages_meta.get("activePageName"), "definition": report, "theme": theme}


_DECLARATION = re.compile(r"^\s*(table|measure|column)\s+('(?:[^']|'')+'|[^=\n]+?)(?:\s*=.*)?$", re.MULTILINE)


def _unquote_tmdl(value: str) -> str:
    value = value.strip()
    return value[1:-1].replace("''", "'") if value.startswith("'") and value.endswith("'") else value


def _column_ref(value: str) -> str:
    """Normaliza referências como 'Tabela'.'Minha Coluna'."""
    parts = re.fullmatch(r"(?:'((?:[^']|'')+)'|([^.]+))\.(?:'((?:[^']|'')+)'|(.+))", value.strip())
    if not parts:
        return _unquote_tmdl(value)
    table = (parts.group(1) or parts.group(2) or "").replace("''", "'")
    column = (parts.group(3) or parts.group(4) or "").replace("''", "'")
    return f"{table}.{column}"


def _parse_tmdl(table_files: list[tuple[str, str]], relationships_text: str = "") -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    for filename, text in table_files:
        table_match = re.search(r"^table\s+(.+)$", text, re.MULTILINE)
        table_name = _unquote_tmdl(table_match.group(1)) if table_match else Path(filename).stem
        columns: list[dict[str, Any]] = []
        measures: list[dict[str, Any]] = []
        lines = text.splitlines()
        for index, line in enumerate(lines):
            match = re.match(r"^\s*(column|measure)\s+('(?:[^']|'')+'|[^=]+?)(?:\s*=\s*(.*))?$", line)
            if not match:
                continue
            kind, raw_name, expression = match.groups()
            item: dict[str, Any] = {"name": _unquote_tmdl(raw_name), "expression": (expression or "").strip()}
            indent = len(line) - len(line.lstrip())
            for following in lines[index + 1:]:
                if following.strip() and len(following) - len(following.lstrip()) <= indent:
                    break
                prop = re.match(r"\s*(dataType|formatString|displayFolder|description):\s*(.*)", following)
                if prop:
                    item[prop.group(1)] = _unquote_tmdl(prop.group(2))
            (measures if kind == "measure" else columns).append(item)
        tables.append({"name": table_name, "columns": columns, "measures": measures})
    relationships: list[dict[str, str]] = []
    blocks = re.split(r"(?=^relationship\s+)", relationships_text, flags=re.MULTILINE)
    for block in blocks:
        if not block.startswith("relationship"):
            continue
        source = re.search(r"fromColumn:\s*(.+)", block)
        target = re.search(r"toColumn:\s*(.+)", block)
        if source and target:
            relationships.append({"from": _column_ref(source.group(1)), "to": _column_ref(target.group(1))})
    return {"tables": tables, "relationships": relationships}


def _asset_data(read: Callable[[str], bytes], names: list[str], prefix: str) -> dict[str, str]:
    assets: dict[str, str] = {}
    for name in names:
        if not name.startswith(prefix) or name.endswith(".json") or name.endswith("/"):
            continue
        data = read(name)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        assets[PurePosixPath(name).name] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    return assets


def parse_pbip(path: Path) -> dict[str, Any]:
    root = path.parent
    config = _json(path.read_bytes())
    artifacts = config.get("artifacts", [])
    if not artifacts or "report" not in artifacts[0]:
        raise PBIParseError("PBIP sem artefato de relatório")
    report_dir = (root / artifacts[0]["report"]["path"]).resolve()
    if not report_dir.is_dir():
        raise PBIParseError(f"Pasta do relatório não encontrada: {report_dir.name}")
    names = [item.relative_to(report_dir).as_posix() for item in report_dir.rglob("*") if item.is_file()]
    source = _Source(lambda name: (report_dir / name).read_bytes(), names)
    result = _parse_report(source)
    result["assets"] = _asset_data(source.read, names, "StaticResources/")

    pbir = _json((report_dir / "definition.pbir").read_bytes())
    model_path = pbir.get("datasetReference", {}).get("byPath", {}).get("path")
    model = {"tables": [], "relationships": []}
    if model_path:
        model_dir = (report_dir / model_path).resolve()
        definition = model_dir / "definition"
        table_files = [(file.name, file.read_text("utf-8-sig")) for file in sorted((definition / "tables").glob("*.tmdl"))]
        relationships = definition / "relationships.tmdl"
        model = _parse_tmdl(table_files, relationships.read_text("utf-8-sig") if relationships.exists() else "")
    result.update({"name": path.stem, "format": "PBIP", "model": model, "source": str(path)})
    return result


def parse_pbix(path: Path) -> dict[str, Any]:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise PBIParseError("Este PBIX não usa o formato ZIP/PBIR suportado") from exc
    with archive:
        names = archive.namelist()
        if "Report/definition/pages/pages.json" not in names:
            raise PBIParseError("PBIX legado sem definição PBIR; converta-o no Power BI Desktop para PBIP")
        source = _Source(archive.read, names, "Report")
        result = _parse_report(source)
        result["assets"] = _asset_data(archive.read, names, "Report/StaticResources/")
        result.update({
            "name": path.stem,
            "format": "PBIX",
            "model": {"tables": [], "relationships": []},
            "source": str(path),
            "hasEmbeddedModel": "DataModel" in names,
        })
        return result


def parse_project(path: str | Path) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise PBIParseError(f"Arquivo não encontrado: {path}")
    suffix = path.suffix.lower()
    if suffix == ".pbip":
        return parse_pbip(path)
    if suffix == ".pbix":
        return parse_pbix(path)
    raise PBIParseError("Use um arquivo .pbip ou .pbix")

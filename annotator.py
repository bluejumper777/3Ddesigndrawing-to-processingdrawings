"""STEP assembly structure parser — text-based regex approach.

Parses PRODUCT, PRODUCT_DEFINITION_FORMATION, PRODUCT_DEFINITION, and
NEXT_ASSEMBLY_USAGE_OCCURRENCE entities from STEP text to build the assembly
hierarchy tree. Decodes \\X2\\...\\X0\\ Unicode escapes for Chinese names.

Solid export still uses OCP (cq.importers.importStep + solid map).
"""

import re
import base64
import json
from pathlib import Path

# Constants
TOLERANCE = 0.15
TAP_DRILL_TO_THREAD: dict[float, str] = {
    1.6: "M2", 2.05: "M2.5", 2.5: "M3", 3.3: "M4", 4.2: "M5",
    5.0: "M6", 6.8: "M8", 8.5: "M10", 10.2: "M12", 12.0: "M14",
    14.0: "M16", 15.5: "M18", 17.5: "M20",
}
PIN_HOLE_DIAMETERS = {3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0}


# ═══════════════════════════════════════════════════════════════════════════════
# Unicode \X2\ decode
# ═══════════════════════════════════════════════════════════════════════════════

def _decode_step_unicode(raw: str) -> str:
    r"""Decode STEP AP214 \X2\...\X0\ Unicode escapes to real characters.

    Example: '\X2\56FA5B9A\X0\板' → '固定板'
    Each 4 hex chars = one Unicode codepoint.
    """
    def _replace(m: re.Match) -> str:
        hex_str = m.group(1)
        chars = []
        for i in range(0, len(hex_str), 4):
            code = int(hex_str[i:i+4], 16)
            chars.append(chr(code))
        return "".join(chars)

    return re.sub(r"\\X2\\([0-9A-Fa-f]+)\\X0\\", _replace, raw)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP text entity parsing
# ═══════════════════════════════════════════════════════════════════════════════

# Regex patterns for entity extraction
# PRODUCT ( 'name', 'description', ... )
_RE_PRODUCT = re.compile(
    r"#(\d+)\s*=\s*PRODUCT\s*\(\s*'([^']*)'\s*,\s*'([^']*)'"
)

# PRODUCT_DEFINITION_FORMATION[_WITH_SPECIFIED_SOURCE] ( '...', '...', #product_ref, ... )
_RE_PDF = re.compile(
    r"#(\d+)\s*=\s*PRODUCT_DEFINITION_FORMATION(?:_WITH_SPECIFIED_SOURCE)?\s*\("
    r"\s*'[^']*'\s*,\s*'[^']*'\s*,\s*#(\d+)"
)

# PRODUCT_DEFINITION ( '...', '...', #pdf_ref, #context_ref )
_RE_PD = re.compile(
    r"#(\d+)\s*=\s*PRODUCT_DEFINITION\s*\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*#(\d+)\s*,\s*#(\d+)"
)

# NEXT_ASSEMBLY_USAGE_OCCURRENCE ( 'name', '...', '...', #parent_pd, #child_pd, $ )
_RE_NAUO = re.compile(
    r"#(\d+)\s*=\s*NEXT_ASSEMBLY_USAGE_OCCURRENCE\s*\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*'[^']*'\s*,"
    r"\s*#(\d+)\s*,\s*#(\d+)"
)


def _parse_step_entities(step_text: str) -> dict:
    """Parse all relevant entities from STEP text.

    Returns dict with keys:
        products: {id: {'name': str, 'desc': str}}
        pdfs:     {id: product_id}     (PRODUCT_DEFINITION_FORMATION → PRODUCT)
        pds:      {id: pdf_id}         (PRODUCT_DEFINITION → PDF)
        nauos:    [(nauo_id, parent_pd_id, child_pd_id), ...]
    """
    products = {}
    pdfs = {}
    pds = {}
    nauos = []

    for m in _RE_PRODUCT.finditer(step_text):
        eid = int(m.group(1))
        name = _decode_step_unicode(m.group(2))
        desc = _decode_step_unicode(m.group(3))
        products[eid] = {"name": name, "desc": desc}

    for m in _RE_PDF.finditer(step_text):
        pdf_id = int(m.group(1))
        product_id = int(m.group(2))
        pdfs[pdf_id] = product_id

    for m in _RE_PD.finditer(step_text):
        pd_id = int(m.group(1))
        pdf_id = int(m.group(2))
        pds[pd_id] = pdf_id

    for m in _RE_NAUO.finditer(step_text):
        nauo_id = int(m.group(1))
        parent_pd = int(m.group(2))
        child_pd = int(m.group(3))
        nauos.append((nauo_id, parent_pd, child_pd))

    return {
        "products": products,
        "pdfs": pdfs,
        "pds": pds,
        "nauos": nauos,
    }


def _pd_to_product_name(pd_id: int, pds: dict, pdfs: dict, products: dict) -> str:
    """Resolve PRODUCT_DEFINITION id → product name."""
    pdf_id = pds.get(pd_id)
    if pdf_id is None:
        return f"Part_#{pd_id}"
    product_id = pdfs.get(pdf_id)
    if product_id is None:
        return f"Part_#{pd_id}"
    product = products.get(product_id)
    if product is None:
        return f"Part_#{pd_id}"
    return product["name"] or f"Part_#{pd_id}"


# ═══════════════════════════════════════════════════════════════════════════════
# Assembly tree builder
# ═══════════════════════════════════════════════════════════════════════════════

def split_assembly(step_file_path: str, output_dir: str) -> list[dict]:
    """Parse STEP file text to extract assembly hierarchy tree.

    Uses regex to parse PRODUCT, PRODUCT_DEFINITION_FORMATION,
    PRODUCT_DEFINITION, and NEXT_ASSEMBLY_USAGE_OCCURRENCE entities.
    Builds parent-child tree from NAUO relationships.

    Solid export still uses OCP for the actual geometry extraction.

    Args:
        step_file_path: Path to STEP file.
        output_dir: Output directory (used by solid export stage, not here).

    Returns:
        List of tree nodes:
        [
            {"name": "子装配体A", "type": "assembly", "children": [...]},
            {"name": "零件B", "type": "part", "pd_id": 12345, "solid_indices": [...]},
            ...
        ]
        Empty list if not an assembly or parsing fails.
    """
    step_path = Path(step_file_path)
    if not step_path.exists():
        return []

    # Read the STEP file text
    try:
        step_text = step_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        try:
            step_text = step_path.read_text(encoding="latin-1")
        except Exception:
            return []

    # Parse entities
    entities = _parse_step_entities(step_text)
    products = entities["products"]
    pdfs = entities["pdfs"]
    pds = entities["pds"]
    nauos = entities["nauos"]

    if not nauos:
        # No assembly relationships → single part
        return []

    # Build parent→children mapping (pd_id → [child_pd_id, ...])
    children_map: dict[int, list[int]] = {}
    child_set: set[int] = set()  # all pd_ids that are children

    for _, parent_pd, child_pd in nauos:
        children_map.setdefault(parent_pd, []).append(child_pd)
        child_set.add(child_pd)

    # Find root(s): pd_ids that appear as parents but never as children
    all_parents = set(children_map.keys())
    roots = all_parents - child_set

    if not roots:
        # Fallback: use the parent with the most children
        if children_map:
            roots = {max(children_map, key=lambda k: len(children_map[k]))}
        else:
            return []

    # Load solid map via OCP for solid_indices mapping
    solid_map_data = _load_solid_map(step_file_path)

    # Recursively build tree
    def _build_node(pd_id: int, visited: set) -> dict:
        if pd_id in visited:
            # Avoid infinite recursion
            name = _pd_to_product_name(pd_id, pds, pdfs, products)
            return {"name": name, "type": "part", "pd_id": pd_id, "solid_indices": []}
        visited.add(pd_id)

        name = _pd_to_product_name(pd_id, pds, pdfs, products)

        if pd_id in children_map:
            # This is an assembly node
            child_nodes = []
            for child_pd in children_map[pd_id]:
                child_node = _build_node(child_pd, visited)
                child_nodes.append(child_node)
            return {
                "name": name,
                "type": "assembly",
                "pd_id": pd_id,
                "children": child_nodes,
            }
        else:
            # Leaf part
            indices = solid_map_data.get(pd_id, [])
            return {
                "name": name,
                "type": "part",
                "pd_id": pd_id,
                "solid_indices": indices,
            }

    # Build tree from roots
    tree = []
    visited = set()
    for root_pd in sorted(roots):
        node = _build_node(root_pd, visited)
        tree.append(node)

    # Keep the root assembly node so users can mark it as a weldment.
    # Only strip it if it has sub-assemblies (multi-level), to avoid
    # showing a redundant single root with the same name as the file.
    # For flat assemblies (root → all parts), keep root as the weldment container.
    if len(tree) == 1 and tree[0]["type"] == "assembly" and tree[0].get("children"):
        children = tree[0]["children"]
        # Check if any child is an assembly (multi-level)
        has_sub_assemblies = any(c["type"] == "assembly" for c in children)
        if has_sub_assemblies:
            # Multi-level: strip root, show sub-assemblies at top
            return children
        # Flat assembly (all children are parts): keep root as container
        # so user can mark it as weldment

    return tree


def load_assembly_with_meshes(step_file_path: str) -> dict | None:
    """Load STEP via XDE: produce tree structure + tessellated mesh data in one pass.

    Like eDrawings: one XDE load gives both tree and geometry with ID linkage.

    Returns:
        {
            "tree": [...],  # tree with unique 'mesh_id' on leaf nodes
            "meshes": [     # mesh data array, indexed by mesh_id
                {"id": 0, "name": "...", "vertices": [...], "normals": [...], "indices": [...]},
                ...
            ]
        }
        or None if loading fails.
    """
    try:
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
        from OCP.TDocStd import TDocStd_Document
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        from OCP.TDF import TDF_LabelSequence, TDF_Label
        from OCP.TDataStd import TDataStd_Name
        from OCP.TopAbs import TopAbs_SOLID, TopAbs_FACE
        from OCP.TopExp import TopExp_Explorer
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.TopLoc import TopLoc_Location
        from OCP.BRep import BRep_Tool
    except ImportError:
        return None

    step_path = Path(step_file_path)
    if not step_path.exists():
        return None

    # Load via XDE
    try:
        doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        reader.ReadFile(str(step_path))
        reader.Transfer(doc)
        shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    except Exception:
        return None

    def get_label_name(label) -> str:
        name_attr = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), name_attr):
            raw = name_attr.Get().ToExtString()
            return _decode_step_unicode(raw) if raw else ""
        return ""

    def tessellate_shape(shape, linear_deflection=1.0, angular_deflection=0.5):
        """Tessellate a shape and return vertices, normals, indices."""
        from OCP.TopoDS import TopoDS

        try:
            BRepMesh_IncrementalMesh(shape, linear_deflection, False, angular_deflection, True)
        except Exception:
            BRepMesh_IncrementalMesh(shape, linear_deflection)

        vertices = []
        normals = []
        indices = []
        offset = 0

        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        while explorer.More():
            face = TopoDS.Face_s(explorer.Current())
            loc = TopLoc_Location()
            triangulation = BRep_Tool.Triangulation_s(face, loc)
            if triangulation is None:
                explorer.Next()
                continue

            n_nodes = triangulation.NbNodes()
            n_tris = triangulation.NbTriangles()
            trsf = loc.Transformation()

            # Extract vertices
            for i in range(1, n_nodes + 1):
                node = triangulation.Node(i)
                node.Transform(trsf)
                vertices.extend([float(node.X()), float(node.Y()), float(node.Z())])
                normals.extend([0.0, 0.0, 1.0])

            # Extract triangles
            for i in range(1, n_tris + 1):
                tri = triangulation.Triangle(i)
                i1, i2, i3 = tri.Get()
                indices.extend([offset + i1 - 1, offset + i2 - 1, offset + i3 - 1])

            offset += n_nodes
            explorer.Next()

        return vertices, normals, indices

    meshes = []
    mesh_counter = [0]

    def build_tree_with_meshes(label, depth=0, parent_loc=None):
        """Recursively build tree with mesh_id on leaf nodes.
        parent_loc accumulates the assembly placement transforms.
        """
        name = get_label_name(label)

        if shape_tool.IsAssembly_s(label):
            children = []
            comp_labels = TDF_LabelSequence()
            shape_tool.GetComponents_s(label, comp_labels)
            for j in range(1, comp_labels.Length() + 1):
                comp_label = comp_labels.Value(j)
                comp_name = get_label_name(comp_label)
                
                # For components, get the shape directly (includes position)
                # This gives us the shape in its correct assembly position
                comp_shape = shape_tool.GetShape_s(comp_label)
                
                # Resolve reference for tree structure
                actual_label = comp_label
                if shape_tool.IsReference_s(comp_label):
                    ref_label = TDF_Label()
                    shape_tool.GetReferredShape_s(comp_label, ref_label)
                    actual_label = ref_label

                if shape_tool.IsAssembly_s(actual_label):
                    # Sub-assembly: recurse
                    child = build_tree_with_meshes(actual_label, depth + 1)
                    if child:
                        if comp_name:
                            child["name"] = comp_name
                        children.append(child)
                else:
                    # Leaf part: tessellate using comp_shape (has correct position)
                    if not comp_shape.IsNull():
                        part_name = comp_name or get_label_name(actual_label) or f"Part_{mesh_counter[0]}"
                        verts, norms, idxs = tessellate_shape(comp_shape)
                        if verts:
                            mesh_id = mesh_counter[0]
                            mesh_counter[0] += 1
                            meshes.append({
                                "id": mesh_id,
                                "name": part_name,
                                "vertices": verts,
                                "normals": norms,
                                "indices": idxs,
                            })
                            children.append({
                                "name": part_name,
                                "type": "part",
                                "mesh_id": mesh_id,
                            })

            if not name:
                name = f"Assembly_{depth}"
            return {
                "name": name,
                "type": "assembly",
                "children": children,
            }

        elif shape_tool.IsSimpleShape_s(label):
            # Direct simple shape (rare at top level)
            shape = shape_tool.GetShape_s(label)
            if shape.IsNull():
                return None
            if not name:
                name = f"Part_{mesh_counter[0]}"
            verts, norms, idxs = tessellate_shape(shape)
            if not verts:
                return None
            mesh_id = mesh_counter[0]
            mesh_counter[0] += 1
            meshes.append({
                "id": mesh_id,
                "name": name,
                "vertices": verts,
                "normals": norms,
                "indices": idxs,
            })
            return {"name": name, "type": "part", "mesh_id": mesh_id}

        return None

    # Build from free shapes
    free_labels = TDF_LabelSequence()
    shape_tool.GetFreeShapes(free_labels)
    if free_labels.Length() == 0:
        return None

    tree = []
    try:
        if free_labels.Length() == 1:
            root = build_tree_with_meshes(free_labels.Value(1))
            if root and root["type"] == "assembly" and root.get("children"):
                tree = root["children"]
            elif root:
                tree = [root]
        else:
            for i in range(1, free_labels.Length() + 1):
                node = build_tree_with_meshes(free_labels.Value(i))
                if node:
                    tree.append(node)
    except Exception as e:
        print(f"[load_assembly_with_meshes] tree build error: {e}")
        return None

    if not tree and not meshes:
        return None

    return {
        "tree": tree,
        "meshes": meshes,
    }


def _load_solid_map(step_file_path: str) -> dict[int, list[int]]:
    """Load STEP via OCP and build pd_id → solid_indices mapping.

    Uses XDE to correlate label names with solid indices in the flat solid map.
    Falls back to empty mapping if OCP is not available.
    """
    try:
        import cadquery as cq
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
        from OCP.TDocStd import TDocStd_Document
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        from OCP.TDF import TDF_LabelSequence, TDF_Label
        from OCP.TDataStd import TDataStd_Name
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopTools import TopTools_IndexedMapOfShape
        from OCP.TopExp import TopExp
        from OCP.TCollection import TCollection_ExtendedString
    except ImportError:
        return {}

    step_path = Path(step_file_path)
    if not step_path.exists():
        return {}

    # Load via XDE
    try:
        doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        reader.ReadFile(str(step_path))
        reader.Transfer(doc)
        shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    except Exception:
        return {}

    # Load with CadQuery to get the global solid map
    try:
        cq_result = cq.importers.importStep(str(step_path))
        top_shape = cq_result.val().wrapped
        solid_map = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(top_shape, TopAbs_SOLID, solid_map)
    except Exception:
        return {}

    # Walk XDE tree to correlate labels with solid indices
    # This maps the label structure to solid indices
    pd_solid_map: dict[int, list[int]] = {}

    def _get_solid_indices(shape) -> list[int]:
        """Get all solid indices contained in a shape."""
        indices = []
        if shape.IsNull():
            return indices
        sub_map = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, TopAbs_SOLID, sub_map)
        for j in range(1, sub_map.Extent() + 1):
            sub_solid = sub_map.FindKey(j)
            for i in range(1, solid_map.Extent() + 1):
                if solid_map.FindKey(i).IsSame(sub_solid):
                    indices.append(i)
                    break
        return indices

    # Get all simple shapes (leaves) and their solid indices
    labels = TDF_LabelSequence()
    shape_tool.GetFreeShapes(labels)

    def _walk_labels(label):
        """Walk the XDE label tree and collect solid indices per label."""
        if shape_tool.IsAssembly_s(label):
            comp_labels = TDF_LabelSequence()
            shape_tool.GetComponents_s(label, comp_labels)
            for j in range(1, comp_labels.Length() + 1):
                comp_label = comp_labels.Value(j)
                if shape_tool.IsReference_s(comp_label):
                    ref_label = TDF_Label()
                    shape_tool.GetReferredShape_s(comp_label, ref_label)
                    _walk_labels(ref_label)
                else:
                    _walk_labels(comp_label)
        elif shape_tool.IsSimpleShape_s(label):
            shape = shape_tool.GetShape_s(label)
            indices = _get_solid_indices(shape)
            if indices:
                # Store by label tag as a proxy for identification
                tag = label.Tag()
                pd_solid_map[tag] = indices

    for i in range(1, labels.Length() + 1):
        _walk_labels(labels.Value(i))

    return pd_solid_map


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: flatten tree for UI
# ═══════════════════════════════════════════════════════════════════════════════

def _flatten_tree(tree: list[dict], prefix: str = "") -> list[dict]:
    """Flatten a hierarchical tree into a flat list for the UI.

    Each entry has: name (with path prefix), type, solid_indices.
    Assemblies are shown as groups, parts as selectable items.
    """
    flat = []
    for node in tree:
        display_name = f"{prefix}{node['name']}" if prefix else node["name"]
        if node["type"] == "assembly":
            all_indices = _collect_all_indices(node)
            flat.append({
                "name": display_name,
                "type": "assembly",
                "solid_indices": all_indices,
            })
            children_flat = _flatten_tree(node.get("children", []), prefix="  ")
            flat.extend(children_flat)
        else:
            flat.append({
                "name": display_name,
                "type": "part",
                "solid_indices": node.get("solid_indices", []),
            })
    return flat


def _collect_all_indices(node: dict) -> list[int]:
    """Recursively collect all solid indices from a node and its children."""
    if node["type"] == "part":
        return node.get("solid_indices", [])
    indices = []
    for child in node.get("children", []):
        indices.extend(_collect_all_indices(child))
    return indices


# ═══════════════════════════════════════════════════════════════════════════════
# Export selected part (OCP-based)
# ═══════════════════════════════════════════════════════════════════════════════


def export_and_analyze_part(step_file_path: str, solid_indices: list[int], part_name: str,
                            session_id: str, output_dir: str) -> dict:
    """Export solid(s) from assembly and run hole analysis."""
    try:
        import cadquery as cq
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopoDS import TopoDS_Compound
        from OCP.TopTools import TopTools_IndexedMapOfShape
        from OCP.TopExp import TopExp
        from OCP.BRep import BRep_Builder
    except ImportError:
        return {"error": "CadQuery not available", "total_holes": 0, "html_path": None}

    try:
        result = cq.importers.importStep(str(step_file_path))
    except Exception as exc:
        return {"error": f"Failed to load STEP: {exc}", "total_holes": 0, "html_path": None}

    shape = result.val().wrapped
    solid_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, solid_map)

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)

    for idx in solid_indices:
        if idx <= solid_map.Extent():
            builder.Add(compound, solid_map.FindKey(idx))

    cq_part = cq.Workplane("XY").newObject([cq.Shape(compound)])

    out_path = Path(output_dir) / session_id
    out_path.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in part_name).strip() or "part"
    part_step_path = out_path / f"{safe_name}.step"

    try:
        cq.exporters.export(cq_part, str(part_step_path), exportType="STEP")
    except Exception as exc:
        return {"error": f"Export failed: {exc}", "total_holes": 0, "html_path": None}

    res = analyze_step_and_generate_viewer(str(part_step_path), session_id, output_dir)
    res["part_name"] = part_name
    return res


# Standard part detection
_STANDARD_PART_KEYWORDS = [
    "螺栓", "螺母", "螺钉", "螺柱", "垫圈", "垫片", "弹垫", "挡圈",
    "销", "开口销", "弹簧销", "圆柱销", "铆销",
    "铆钉", "卡簧", "轴承", "密封圈", "O型圈",
    "键", "平键", "半圆键",
    "bolt", "nut", "screw", "washer", "gasket", "rivet",
    "pin", "dowel", "bearing", "bushing", "seal", "o-ring",
    "spring washer", "lock washer", "cotter", "clip", "retaining ring",
    "circlip", "snap ring", "key", "woodruff",
    "GB", "ISO", "DIN", "ANSI", "JIS",
    "M3", "M4", "M5", "M6", "M8", "M10", "M12", "M14", "M16", "M18", "M20",
    "M22", "M24", "M27", "M30",
]


def _is_standard_part(part_name: str) -> bool:
    """Detect standard/fastener parts by name matching."""
    name_lower = part_name.lower()
    for keyword in _STANDARD_PART_KEYWORDS:
        if keyword.lower() in name_lower:
            return True
    if re.match(r"(gb|iso|din|ansi|jis)[/\s\-t]*\d", name_lower):
        return True
    return False


def analyze_assembly(step_file_path: str, session_id: str, output_dir: str) -> dict:
    """Analyze a STEP assembly: split into parts, annotate each one."""
    parts = split_assembly(step_file_path, str(Path(output_dir) / session_id))

    if not parts:
        single_result = analyze_step_and_generate_viewer(step_file_path, session_id, output_dir)
        if single_result.get("total_holes", 0) == 0 and not single_result.get("error"):
            return {
                "is_assembly": False, "parts": [], "skipped_standard": 0,
                "skipped_no_holes": 1, "total_parts": 0,
                "error": "该零件没有识别到孔特征，无需标注。",
            }
        return {
            "is_assembly": False, "parts": [single_result],
            "skipped_standard": 0, "skipped_no_holes": 0, "total_parts": 1,
            "error": single_result.get("error"),
        }

    part_results = []
    skipped_standard = 0
    skipped_no_holes = 0

    for idx, part_info in enumerate(parts):
        if _is_standard_part(part_info["name"]):
            skipped_standard += 1
            continue
        part_session = f"{session_id}_p{idx}"
        result = analyze_step_and_generate_viewer(
            part_info.get("step_path", ""), part_session, output_dir
        )
        result["part_name"] = part_info["name"]
        if result.get("total_holes", 0) == 0:
            skipped_no_holes += 1
            continue
        part_results.append(result)

    return {
        "is_assembly": True, "parts": part_results,
        "skipped_standard": skipped_standard,
        "skipped_no_holes": skipped_no_holes,
        "total_parts": len(part_results), "error": None,
    }


def _export_part_by_name(step_file_path: str, part_name: str, is_weldment: bool,
                          session_id: str, output_dir: str) -> dict:
    """Export a part/weldment from STEP by matching XDE label names.

    Uses the XDE shape tree to find shapes whose label name matches part_name,
    then exports them as a compound and runs hole analysis.

    Name matching strategy (in priority order):
    1. Exact match after normalization (strip extensions/whitespace)
    2. target_name is a suffix of label name (handles path prefixes)
    3. Label name starts with target_name (handles trailing instance numbers)

    NEVER matches if target is just a substring of a much longer unrelated name,
    or if label name is a short substring of target (which could match parent assemblies).
    """
    try:
        import cadquery as cq
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
        from OCP.TDocStd import TDocStd_Document
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        from OCP.TDF import TDF_LabelSequence, TDF_Label
        from OCP.TDataStd import TDataStd_Name
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopTools import TopTools_IndexedMapOfShape
        from OCP.TopExp import TopExp
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.TopoDS import TopoDS_Compound
        from OCP.BRep import BRep_Builder
    except ImportError:
        return {"error": "CadQuery not available", "total_holes": 0, "html_path": None}

    step_path = Path(step_file_path)

    # Load via XDE
    try:
        doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        reader.ReadFile(str(step_path))
        reader.Transfer(doc)
        shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    except Exception as e:
        return {"error": f"XDE load failed: {e}", "total_holes": 0, "html_path": None}

    # Find shapes matching the name
    def get_label_name(label):
        name_attr = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), name_attr):
            raw = name_attr.Get().ToExtString()
            return _decode_step_unicode(raw) if raw else ""
        return ""

    def _normalize(n):
        """Strip file extensions and extra whitespace for comparison."""
        return n.replace(".STEP", "").replace(".step", "").replace(".stp", "").replace(".STP", "").strip()

    def _names_match(label_name: str, target_name: str) -> bool:
        """Strict name matching — avoids false positives from partial substring hits.

        Match criteria (any one):
        1. Exact match after normalization
        2. Label name ends with target (handles "path/prefix/TargetName" patterns)
        3. Label name starts with target (handles "TargetName_instance_1" patterns)
        4. Target ends with label name AND label is long enough (>60% of target length)
           This handles cases where XDE truncates names.

        Explicitly REJECTED:
        - Short label names matching inside a long target (e.g. "GT20" matching "GT20-A515-E1101-A_ASM...")
        - Target being a short substring in the middle of a long label name
        """
        if not label_name or not target_name:
            return False
        ln = _normalize(label_name)
        tn = _normalize(target_name)
        if not ln or not tn:
            return False

        # 1. Exact match
        if ln == tn:
            return True

        # 2. Label ends with target (label has extra prefix like path separators)
        if ln.endswith(tn):
            # Make sure the character before the match is a separator, not mid-word
            prefix_len = len(ln) - len(tn)
            if prefix_len == 0 or ln[prefix_len - 1] in (' ', '/', '\\', '_', '-', ':'):
                return True

        # 3. Label starts with target (label has trailing instance number/suffix)
        if ln.startswith(tn):
            # Make sure what follows is a separator or instance marker, not a different word
            suffix_start = len(tn)
            if suffix_start >= len(ln) or ln[suffix_start] in (' ', '_', '-', '.', ':', '(', '[', '#'):
                return True

        # 4. Target ends with label name (XDE may have shorter names than regex parser)
        #    Only allow if label name is substantial (>60% of target length)
        if tn.endswith(ln) and len(ln) > len(tn) * 0.6:
            prefix_len = len(tn) - len(ln)
            if prefix_len == 0 or tn[prefix_len - 1] in (' ', '/', '\\', '_', '-', ':'):
                return True

        # 5. Target starts with label name (same logic, label is a substantial prefix)
        if tn.startswith(ln) and len(ln) > len(tn) * 0.6:
            suffix_start = len(ln)
            if suffix_start >= len(tn) or tn[suffix_start] in (' ', '_', '-', '.', ':', '(', '[', '#'):
                return True

        return False

    found_shapes = []
    _match_found = [False]  # Use list to allow mutation in nested function

    def _extract_solids_from_label(label):
        """Extract all solid shapes from a label (handles both parts and assemblies)."""
        shape = shape_tool.GetShape_s(label)
        if shape.IsNull():
            return
        from OCP.TopAbs import TopAbs_SOLID as _TA_SOLID
        from OCP.TopTools import TopTools_IndexedMapOfShape as _TIMS
        from OCP.TopExp import TopExp as _TE
        sub_map = _TIMS()
        _TE.MapShapes_s(shape, _TA_SOLID, sub_map)
        for k in range(1, sub_map.Extent() + 1):
            found_shapes.append(sub_map.FindKey(k))

    def search_labels(label, target_name):
        """Search for label matching target_name. Stops after first match."""
        if _match_found[0]:
            return  # Already found a match, stop searching

        name = get_label_name(label)

        if _names_match(name, target_name):
            # Found the matching node
            _match_found[0] = True
            if is_weldment:
                # For weldment: collect all solids from this assembly and its children
                _extract_solids_from_label(label)
                # Also recurse into components to get positioned shapes
                if shape_tool.IsAssembly_s(label):
                    comp_labels = TDF_LabelSequence()
                    shape_tool.GetComponents_s(label, comp_labels)
                    for j in range(1, comp_labels.Length() + 1):
                        cl = comp_labels.Value(j)
                        comp_shape = shape_tool.GetShape_s(cl)
                        if not comp_shape.IsNull():
                            from OCP.TopAbs import TopAbs_SOLID as _TA_SOLID
                            from OCP.TopTools import TopTools_IndexedMapOfShape as _TIMS
                            from OCP.TopExp import TopExp as _TE
                            sub_map = _TIMS()
                            _TE.MapShapes_s(comp_shape, _TA_SOLID, sub_map)
                            for k in range(1, sub_map.Extent() + 1):
                                found_shapes.append(sub_map.FindKey(k))
            else:
                _extract_solids_from_label(label)
            return

        # No match at this level — recurse into children (only for assemblies)
        if shape_tool.IsAssembly_s(label):
            comp_labels = TDF_LabelSequence()
            shape_tool.GetComponents_s(label, comp_labels)
            for j in range(1, comp_labels.Length() + 1):
                if _match_found[0]:
                    return  # Stop early
                cl = comp_labels.Value(j)
                if shape_tool.IsReference_s(cl):
                    ref = TDF_Label()
                    shape_tool.GetReferredShape_s(cl, ref)
                    search_labels(ref, target_name)
                else:
                    search_labels(cl, target_name)

    # Search from free shapes
    free_labels = TDF_LabelSequence()
    shape_tool.GetFreeShapes(free_labels)
    for i in range(1, free_labels.Length() + 1):
        if _match_found[0]:
            break
        search_labels(free_labels.Value(i), part_name)

    if not found_shapes:
        # DO NOT fallback to analyzing the entire STEP file.
        # Return a clear error instead of silently giving wrong results.
        return {
            "error": f"未能在STEP文件中匹配到零件 '{part_name}'。XDE标签名可能与显示名不一致。",
            "total_holes": 0,
            "html_path": None,
            "part_name": part_name,
        }

    # Deduplicate shapes (same shape may be found via multiple paths)
    unique_shapes = []
    for shape in found_shapes:
        is_dup = False
        for existing in unique_shapes:
            if existing.IsSame(shape):
                is_dup = True
                break
        if not is_dup:
            unique_shapes.append(shape)

    # Build compound from found shapes
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for shape in unique_shapes:
        builder.Add(compound, shape)

    # Export to STEP
    out_path = Path(output_dir) / session_id
    out_path.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in part_name).strip() or "part"
    if is_weldment:
        safe_name += "_weld"
    part_step_path = out_path / f"{safe_name}.step"

    cq_part = cq.Workplane("XY").newObject([cq.Shape(compound)])
    try:
        cq.exporters.export(cq_part, str(part_step_path), exportType="STEP")
    except Exception as e:
        return {"error": f"Export failed: {e}", "total_holes": 0, "html_path": None}

    # Analyze
    result = analyze_step_and_generate_viewer(str(part_step_path), session_id, output_dir)
    result["part_name"] = part_name + (" [焊件]" if is_weldment else "")
    return result


def analyze_step_and_generate_viewer(step_file_path: str, session_id: str, output_dir: str) -> dict:
    """Analyze a STEP file for holes and generate a 3D annotated HTML viewer."""
    try:
        import cadquery as cq
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.GeomAbs import GeomAbs_Cylinder
    except ImportError as exc:
        return {"html_path": None, "stl_path": None, "annotations": [], "total_holes": 0, "error": f"CadQuery not available: {exc}"}

    step_path = Path(step_file_path)
    if not step_path.exists():
        return {"html_path": None, "stl_path": None, "annotations": [], "total_holes": 0, "error": f"STEP file not found: {step_file_path}"}

    try:
        result = cq.importers.importStep(str(step_path))
    except Exception as exc:
        return {"html_path": None, "stl_path": None, "annotations": [], "total_holes": 0, "error": f"Failed to load STEP: {exc}"}

    try:
        faces = result.faces().vals()
    except Exception as exc:
        return {"html_path": None, "stl_path": None, "annotations": [], "total_holes": 0, "error": f"No valid geometry: {exc}"}

    if not faces:
        return {"html_path": None, "stl_path": None, "annotations": [], "total_holes": 0, "error": "No faces"}

    # Find cylindrical faces — collect all, then group by coaxial+same-radius
    import math
    _TWO_PI = 2 * math.pi
    _MIN_TOTAL_ARC = _TWO_PI * 0.90  # 90% of full circle required

    raw_cyls = []
    _hole_solid_shape = result.val().wrapped  # for inner/outer containment test
    for face in result.faces().vals():
        if face.geomType() != "CYLINDER":
            continue
        adaptor = BRepAdaptor_Surface(face.wrapped)
        if adaptor.GetType() != GeomAbs_Cylinder:
            continue
        u_min = adaptor.FirstUParameter()
        u_max = adaptor.LastUParameter()
        arc_angle = abs(u_max - u_min)
        if arc_angle < 0.1:
            continue  # skip degenerate slivers
        cyl = adaptor.Cylinder()
        radius = cyl.Radius()
        diameter = round(radius * 2, 2)

        # === Inner/Outer face check (robust, orientation-independent) ===
        # Sample a point on the cylinder surface, move it slightly TOWARD the axis.
        # HOLE: that point enters the void → OUTSIDE solid.
        # BOSS/PIN: that point enters material → INSIDE solid.
        try:
            from OCP.BRepClass3d import BRepClass3d_SolidClassifier as _SC
            from OCP.TopAbs import TopAbs_IN as _IN
            from OCP.gp import gp_Pnt as _gp_Pnt
            um = (u_min + u_max) / 2
            vm = (adaptor.FirstVParameter() + adaptor.LastVParameter()) / 2
            sp = adaptor.Value(um, vm)  # point on cylinder surface
            ap = cyl.Location()
            adir = cyl.Axis().Direction()
            spv = (sp.X()-ap.X(), sp.Y()-ap.Y(), sp.Z()-ap.Z())
            along = spv[0]*adir.X() + spv[1]*adir.Y() + spv[2]*adir.Z()
            ca = (ap.X()+adir.X()*along, ap.Y()+adir.Y()*along, ap.Z()+adir.Z()*along)
            toward = (ca[0]-sp.X(), ca[1]-sp.Y(), ca[2]-sp.Z())
            tlen = (toward[0]**2+toward[1]**2+toward[2]**2)**0.5
            if tlen > 1e-9:
                step = min(0.5, radius * 0.3)
                tp = _gp_Pnt(
                    sp.X() + toward[0]/tlen*step,
                    sp.Y() + toward[1]/tlen*step,
                    sp.Z() + toward[2]/tlen*step,
                )
                clf = _SC(_hole_solid_shape, tp, 1e-4)
                if clf.State() == _IN:
                    continue  # toward-axis is inside material → external cylinder
        except Exception:
            pass  # keep the face if check fails (conservative)

        face_center = face.Center()
        loc = cyl.Location()
        axis = cyl.Axis().Direction()
        # Compute the top edge (surface entry point) of the cylindrical face
        # V parameter range gives the extent along the axis
        v_min = adaptor.FirstVParameter()
        v_max = adaptor.LastVParameter()
        # axis_center is the cylinder origin projected on axis
        ax = (axis.X(), axis.Y(), axis.Z())
        loc_pt = (loc.X(), loc.Y(), loc.Z())
        # Top and bottom positions along axis from cylinder origin
        top_pt = (loc_pt[0] + ax[0]*v_max, loc_pt[1] + ax[1]*v_max, loc_pt[2] + ax[2]*v_max)
        bot_pt = (loc_pt[0] + ax[0]*v_min, loc_pt[1] + ax[1]*v_min, loc_pt[2] + ax[2]*v_min)
        raw_cyls.append({
            "diameter": diameter,
            "arc_angle": arc_angle,
            "center": (round(face_center.x, 2), round(face_center.y, 2), round(face_center.z, 2)),
            "axis_center": (round(loc.X(), 2), round(loc.Y(), 2), round(loc.Z(), 2)),
            "axis": (round(axis.X(), 3), round(axis.Y(), 3), round(axis.Z(), 3)),
            "top_pt": (round(top_pt[0], 2), round(top_pt[1], 2), round(top_pt[2], 2)),
            "bot_pt": (round(bot_pt[0], 2), round(bot_pt[1], 2), round(bot_pt[2], 2)),
            "depth": round(abs(v_max - v_min), 2),
        })

    # Group coaxial faces with same diameter, sum their arc angles
    cylinders = []
    used = [False] * len(raw_cyls)
    for i, c in enumerate(raw_cyls):
        if used[i]:
            continue
        group_arc = c["arc_angle"]
        used[i] = True
        ax1 = c["axis"]
        ac1 = c["axis_center"]
        # Collect all endpoints (top_pt, bot_pt) of this group to find full extent
        group_pts = [c["top_pt"], c["bot_pt"]]

        # Helper: project a point onto the axis (origin = ac1)
        def _axproj(pt):
            return (pt[0]-ac1[0])*ax1[0] + (pt[1]-ac1[1])*ax1[1] + (pt[2]-ac1[2])*ax1[2]

        # Current group's axial range
        grp_lo = min(_axproj(c["top_pt"]), _axproj(c["bot_pt"]))
        grp_hi = max(_axproj(c["top_pt"]), _axproj(c["bot_pt"]))

        # Find coaxial partners with same diameter (iterate until no more join,
        # since group range can grow and admit adjacent faces)
        changed = True
        while changed:
            changed = False
            for j in range(len(raw_cyls)):
                if used[j]:
                    continue
                other = raw_cyls[j]
                if abs(c["diameter"] - other["diameter"]) > TOLERANCE:
                    continue
                ac2 = other["axis_center"]
                ax2 = other["axis"]
                # Axes must be parallel (dot product ≈ ±1)
                dot = abs(ax1[0]*ax2[0] + ax1[1]*ax2[1] + ax1[2]*ax2[2])
                if dot < 0.99:
                    continue
                # Perpendicular distance between the two axis lines must be ~0
                diff = (ac1[0]-ac2[0], ac1[1]-ac2[1], ac1[2]-ac2[2])
                along = diff[0]*ax1[0] + diff[1]*ax1[1] + diff[2]*ax1[2]
                perp_sq = (diff[0] - along*ax1[0])**2 + (diff[1] - along*ax1[1])**2 + (diff[2] - along*ax1[2])**2
                if perp_sq > 1.0:  # not coaxial
                    continue
                # ALONG-AXIS adjacency: the face's axial range must overlap or be
                # within a small gap of the group's range. This prevents merging
                # two distinct collinear holes (e.g. holes on opposite walls far apart).
                o_lo = min(_axproj(other["top_pt"]), _axproj(other["bot_pt"]))
                o_hi = max(_axproj(other["top_pt"]), _axproj(other["bot_pt"]))
                gap = max(grp_lo - o_hi, o_lo - grp_hi, 0.0)
                if gap > 3.0:  # too far apart along axis → different hole
                    continue
                group_arc += other["arc_angle"]
                group_pts.append(other["top_pt"])
                group_pts.append(other["bot_pt"])
                grp_lo = min(grp_lo, o_lo)
                grp_hi = max(grp_hi, o_hi)
                used[j] = True
                changed = True
        # Only keep if total arc coverage ≥ 90% of full circle
        if group_arc >= _MIN_TOTAL_ARC:
            # Compute full axial extent: project all endpoints onto axis,
            # pick the two extremes as the real outer/inner openings.
            ref = group_pts[0]
            def _proj(pt):
                return (pt[0]-ref[0])*ax1[0] + (pt[1]-ref[1])*ax1[1] + (pt[2]-ref[2])*ax1[2]
            pt_min = min(group_pts, key=_proj)
            pt_max = max(group_pts, key=_proj)
            full_depth = abs(_proj(pt_max) - _proj(pt_min))
            cylinders.append({
                "diameter": c["diameter"],
                "center": c["center"],
                "axis_center": c["axis_center"],
                "axis": c["axis"],
                "top_pt": pt_max,
                "bot_pt": pt_min,
                "depth": round(full_depth, 2) if full_depth > 0 else c["depth"],
            })

    try:
        bbox = result.val().BoundingBox()
        max_body_dim = max(bbox.xlen, bbox.ylen, bbox.zlen)
    except Exception:
        max_body_dim = 10000.0

    holes = [c for c in cylinders if c["diameter"] < max_body_dim * 0.9]

    # Filter out tube cavities: depth/diameter ratio > 8 is unlikely a machined hole
    # (typical max for drilled holes is ~5-6, gun drills go up to ~8)
    # Also filter: absolute depth > 80mm for non-through holes with standard pin diameters
    # (pin holes are rarely deeper than 3-4x diameter)
    def _is_likely_hole(h):
        depth = h.get("depth", 0)
        dia = h["diameter"]
        if depth <= 0 or dia <= 0:
            return True
        ratio = depth / dia
        if ratio >= 8.0:
            return False
        # Additional check: pin-diameter holes (H7) deeper than 5x diameter are suspicious
        if dia in (3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0):
            if ratio > 5.0 and depth > 60.0:
                return False
        return True

    holes = [h for h in holes if _is_likely_hole(h)]

    # Deduplication
    unique_holes = []
    for hole in holes:
        is_dup = False
        for existing in unique_holes:
            if abs(hole["diameter"] - existing["diameter"]) > TOLERANCE:
                continue
            ac1 = hole.get("axis_center", hole["center"])
            ac2 = existing.get("axis_center", existing["center"])
            if abs(ac1[0]-ac2[0]) < 0.5 and abs(ac1[1]-ac2[1]) < 0.5 and abs(ac1[2]-ac2[2]) < 0.5:
                is_dup = True
                break
        if not is_dup:
            unique_holes.append(hole)

    # Counterbore pairing
    for hole in unique_holes:
        hole["is_counterbore_of"] = None

    for i in range(len(unique_holes)):
        if unique_holes[i]["is_counterbore_of"] is not None:
            continue
        for j in range(len(unique_holes)):
            if i == j:
                continue
            # i = larger hole (potential counterbore), j = smaller hole (main bore)
            if unique_holes[i]["diameter"] <= unique_holes[j]["diameter"]:
                continue
            ac1 = unique_holes[i].get("axis_center", unique_holes[i]["center"])
            ac2 = unique_holes[j].get("axis_center", unique_holes[j]["center"])
            axis_i = unique_holes[i]["axis"]
            if abs(axis_i[2]) > 0.9:
                coaxial = abs(ac1[0]-ac2[0]) < 0.5 and abs(ac1[1]-ac2[1]) < 0.5
            elif abs(axis_i[0]) > 0.9:
                coaxial = abs(ac1[1]-ac2[1]) < 0.5 and abs(ac1[2]-ac2[2]) < 0.5
            else:
                coaxial = abs(ac1[0]-ac2[0]) < 0.5 and abs(ac1[2]-ac2[2]) < 0.5
            if not coaxial:
                continue
            # Counterbore constraints:
            # Two coaxial holes with different diameters that are adjacent
            # (centers close along axis = they share the transition)
            depth_i = unique_holes[i].get("depth", 0)
            depth_j = unique_holes[j].get("depth", 0)
            # Centers should be close along axis (within sum of half-depths + small tolerance)
            c1 = unique_holes[i]["center"]
            c2 = unique_holes[j]["center"]
            if abs(axis_i[2]) > 0.9:
                separation = abs(c1[2]-c2[2])
            elif abs(axis_i[0]) > 0.9:
                separation = abs(c1[0]-c2[0])
            else:
                separation = abs(c1[1]-c2[1])
            max_sep = (depth_i + depth_j) / 2 + 2.0
            if separation > max_sep:
                continue
            unique_holes[i]["is_counterbore_of"] = j
            break

    # Determine through-hole by checking if both ends of the cylinder are open
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_ON, TopAbs_OUT, TopAbs_IN
    _solid_shape = result.val().wrapped

    def _is_through_hole_topo(hole_data, all_faces):
        """Check if a cylindrical hole is through by examining if bot_pt is on or outside the solid surface.
        
        Uses BRepClass3d_SolidClassifier to test if the bottom point of the hole
        exits the solid body. If it does, the hole is through.
        """
        axis = hole_data["axis"]
        top = hole_data.get("top_pt", hole_data["center"])
        bot = hole_data.get("bot_pt", hole_data["center"])
        depth = hole_data.get("depth", 0)
        if depth < 0.5:
            return False

        try:
            ax = (axis[0], axis[1], axis[2])
            # Test a point slightly beyond bot_pt along the axis
            test_pt = gp_Pnt(
                bot[0] - ax[0] * 0.1,
                bot[1] - ax[1] * 0.1,
                bot[2] - ax[2] * 0.1,
            )
            classifier = BRepClass3d_SolidClassifier(_solid_shape, test_pt, 0.01)
            state = classifier.State()
            if state == TopAbs_ON or state == TopAbs_OUT:
                return True
            # Also test in the opposite direction from top_pt
            test_pt2 = gp_Pnt(
                top[0] + ax[0] * 0.1,
                top[1] + ax[1] * 0.1,
                top[2] + ax[2] * 0.1,
            )
            classifier2 = BRepClass3d_SolidClassifier(_solid_shape, test_pt2, 0.01)
            state2 = classifier2.State()
            if state2 == TopAbs_ON or state2 == TopAbs_OUT:
                return True
            return False
        except Exception:
            return False

    # Classify
    annotations = []
    for i, hole in enumerate(unique_holes):
        diameter = hole["diameter"]
        if hole["is_counterbore_of"] is not None:
            continue
        counterbore = None
        for other in unique_holes:
            if other.get("is_counterbore_of") == i:
                counterbore = other
                break
        depth = hole.get("depth", 0)
        # Through-hole: check if hole spans the full body in its axis direction
        is_through = _is_through_hole_topo(hole, faces)

        thread_spec = None
        for tap_d, spec in TAP_DRILL_TO_THREAD.items():
            if abs(diameter - tap_d) < TOLERANCE:
                thread_spec = spec
                break
        if thread_spec:
            if is_through:
                spec_text = f"{thread_spec} 通孔"
            else:
                spec_text = f"{thread_spec} 深{depth:.1f}"
            hole_type = "thread"
        elif any(abs(diameter - pd) < TOLERANCE for pd in PIN_HOLE_DIAMETERS):
            if is_through:
                spec_text = f"\u03a6{diameter}H7 通孔"
            else:
                spec_text = f"\u03a6{diameter}H7 深{depth:.1f}"
            hole_type = "pin"
        else:
            if is_through:
                spec_text = f"\u03a6{diameter} 通孔"
            else:
                spec_text = f"\u03a6{diameter} 深{depth:.1f}"
            hole_type = "clearance"
        if counterbore:
            cb_depth = counterbore.get("depth", 0)
            spec_text += f"\n\u6c89\u5b54\u03a6{counterbore['diameter']} \u6df1{cb_depth:.1f}"
        # Use surface point — for through holes pick the unobstructed side
        def _pick_surface_pt(h, is_thru):
            """Pick the OUTER opening of the hole.
            The outer opening is the end point farthest from the bbox center
            (external surfaces lie on the part's outer envelope).
            """
            top = h.get("top_pt", h["center"])
            bot = h.get("bot_pt", h["center"])
            try:
                bx = result.val().BoundingBox()
                bcx = (bx.xmin + bx.xmax) / 2
                bcy = (bx.ymin + bx.ymax) / 2
                bcz = (bx.zmin + bx.zmax) / 2
                d_top = (top[0]-bcx)**2 + (top[1]-bcy)**2 + (top[2]-bcz)**2
                d_bot = (bot[0]-bcx)**2 + (bot[1]-bcy)**2 + (bot[2]-bcz)**2
                return top if d_top >= d_bot else bot
            except Exception:
                return top
        src_hole = counterbore if counterbore else hole
        ann_point = list(_pick_surface_pt(src_hole, is_through))
        annotations.append({
            "spec": spec_text,
            "type": hole_type,
            "point": ann_point,
            "axis": list(hole["axis"]),
            "diameter": hole["diameter"],
        })

    # Export STL
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    stl_name = f"{step_path.stem}.stl"
    stl_path = out_path / stl_name
    try:
        cq.exporters.export(result, str(stl_path), exportType="STL")
    except Exception as exc:
        return {"html_path": None, "stl_path": None, "annotations": annotations, "total_holes": len(unique_holes), "error": f"STL export failed: {exc}"}

    try:
        bbox = result.val().BoundingBox()
        bbox_str = f"{bbox.xlen:.1f} \u00d7 {bbox.ylen:.1f} \u00d7 {bbox.zlen:.1f} mm"
    except Exception:
        bbox_str = "unknown"

    title = f"{step_path.stem} - \u5b54\u6807\u6ce8"
    info = {"doc_number": step_path.stem, "bounding_box": bbox_str}

    html_path = _generate_viewer_html(
        stl_file_path=str(stl_path), annotations=annotations,
        title=title, info=info, output_dir=str(out_path), session_id=session_id,
    )

    return {"html_path": html_path, "stl_path": str(stl_path), "annotations": annotations, "total_holes": len(unique_holes), "error": None}


def _generate_viewer_html(stl_file_path, annotations, title, info, output_dir, session_id):
    """Generate a standalone HTML file with 3D viewer and annotations.
    
    Inlines three.js libraries as base64 data URIs so the HTML works offline.
    """
    stl_path = Path(stl_file_path)
    if not stl_path.exists():
        return None
    stl_data = stl_path.read_bytes()
    stl_b64 = base64.b64encode(stl_data).decode("ascii")
    annotations_json = json.dumps(annotations, ensure_ascii=False)
    info_json = json.dumps(info or {}, ensure_ascii=False)

    # Read three.js libraries and encode as base64 data URIs for importmap
    static_dir = Path(__file__).parent / "static" / "three"
    three_b64 = ""
    orbit_b64 = ""
    stl_loader_b64 = ""
    if static_dir.exists():
        three_file = static_dir / "three.module.js"
        orbit_file = static_dir / "OrbitControls.js"
        stl_loader_file = static_dir / "STLLoader.js"
        if three_file.exists():
            three_b64 = base64.b64encode(three_file.read_bytes()).decode("ascii")
        if orbit_file.exists():
            orbit_b64 = base64.b64encode(orbit_file.read_bytes()).decode("ascii")
        if stl_loader_file.exists():
            stl_loader_b64 = base64.b64encode(stl_loader_file.read_bytes()).decode("ascii")

    html = _build_viewer_html(stl_b64, annotations_json, info_json, title,
                              three_b64, orbit_b64, stl_loader_b64)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    output_name = f"{stl_path.stem}.html"
    output_path = out_path / output_name
    output_path.write_text(html, encoding="utf-8")
    return str(output_path)


def _build_viewer_html(stl_b64: str, annotations_json: str, info_json: str, title: str,
                       three_b64: str = "", orbit_b64: str = "", stl_loader_b64: str = "") -> str:
    """Build the complete HTML string for the 3D annotated viewer.

    Embeds three.js as base64 data URIs for fully offline operation.
    Right panel items are clickable: clicking a group shows only that group's
    annotations (first as full label, rest as colored dots). Click again to show all.
    """
    # Build importmap with data URIs (works with file:// protocol)
    if three_b64 and orbit_b64 and stl_loader_b64:
        importmap_block = f'''<script type="importmap">{{
  "imports": {{
    "three": "data:text/javascript;base64,{three_b64}",
    "three/addons/OrbitControls.js": "data:text/javascript;base64,{orbit_b64}",
    "three/addons/STLLoader.js": "data:text/javascript;base64,{stl_loader_b64}"
  }}
}}</script>'''
    else:
        importmap_block = '''<script type="importmap">{
  "imports": {
    "three": "/static/three/three.module.js",
    "three/addons/": "/static/three/"
  }
}</script>'''

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: "Segoe UI", "PingFang SC", sans-serif; background: #c8ccd0; color: #333; overflow: hidden; }}
#viewer {{ width: 100vw; height: 100vh; display: block; }}
#info-panel {{
  position: fixed; top: 16px; left: 16px; background: rgba(255,255,255,0.94);
  border: 1px solid rgba(0,0,0,0.12); border-radius: 10px; padding: 14px 18px;
  max-width: 320px; backdrop-filter: blur(8px); z-index: 10; box-shadow: 0 2px 12px rgba(0,0,0,0.08);
}}
#info-panel h2 {{ font-size: 15px; margin-bottom: 6px; color: #1a5fcc; }}
#info-panel .field {{ font-size: 12px; margin: 3px 0; color: #555; }}
#info-panel .field b {{ color: #222; }}
#annotation-panel {{
  position: fixed; top: 16px; right: 16px; background: rgba(255,255,255,0.94);
  border: 1px solid rgba(0,0,0,0.12); border-radius: 10px; padding: 14px 18px;
  max-width: 360px; max-height: calc(100vh - 80px); overflow-y: auto;
  backdrop-filter: blur(8px); z-index: 10; box-shadow: 0 2px 12px rgba(0,0,0,0.08);
}}
#annotation-panel h3 {{ font-size: 14px; margin-bottom: 10px; color: #1a5fcc; }}
.ann-item {{
  padding: 8px 10px; margin: 4px 0; border-radius: 8px;
  background: rgba(240,243,248,0.9); font-size: 12px; line-height: 1.5;
  cursor: pointer; transition: background 0.15s; color: #333;
}}
.ann-item:hover {{ background: rgba(220,230,245,0.95); }}
.ann-item.active {{ background: rgba(200,220,255,0.9); outline: 1px solid rgba(60,120,220,0.5); }}
.ann-item.thread {{ border-left: 3px solid #e03030; }}
.ann-item.pin {{ border-left: 3px solid #1a9e3f; }}
.ann-item.clearance {{ border-left: 3px solid #2070cc; }}
.ann-item.counterbore {{ border-left: 3px solid #cc8800; }}
#controls {{
  position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
  background: rgba(255,255,255,0.88); border-radius: 999px; padding: 8px 20px;
  font-size: 12px; color: #666; z-index: 10; box-shadow: 0 1px 6px rgba(0,0,0,0.08);
}}
.label-3d {{
  position: absolute; pointer-events: auto; font-size: 11px; font-weight: 600;
  padding: 3px 8px; border-radius: 6px; white-space: nowrap;
  transform: translate(-50%, -100%); transition: opacity 0.2s;
  cursor: grab; user-select: none;
}}
.label-3d.dragging {{ cursor: grabbing; opacity: 0.8; }}
.label-3d.thread {{ background: rgba(230,50,50,0.9); color: #fff; }}
.label-3d.pin {{ background: rgba(20,160,60,0.9); color: #fff; }}
.label-3d.clearance {{ background: rgba(30,100,210,0.9); color: #fff; }}
.label-3d.counterbore {{ background: rgba(200,140,0,0.9); color: #fff; }}
.label-3d.as-dot {{
  width: 12px; height: 12px; border-radius: 50%; padding: 0;
  transform: translate(-50%, -50%); font-size: 0; min-width: 12px; min-height: 12px;
  pointer-events: none; cursor: default;
  box-shadow: 0 0 4px rgba(0,0,0,0.3);
}}
.label-3d.as-dot.thread {{ background: #e03030; }}
.label-3d.as-dot.pin {{ background: #1a9e3f; }}
.label-3d.as-dot.clearance {{ background: #2070cc; }}
.label-3d.as-dot.counterbore {{ background: #cc8800; }}
.label-3d.hidden-label {{ display: none !important; }}
</style>
</head>
<body>
<div id="viewer"></div>
<div id="info-panel"><h2>{title}</h2><div id="info-fields"></div></div>
<div id="annotation-panel"><h3>孔标注 Hole Annotations</h3><div style="font-size:10px;color:#666;margin-bottom:8px;">点击右侧某类 → 3D显示绿点位置；再点取消；双击编辑标注内容</div><div id="ann-list"></div></div>
<div id="controls">鼠标左键旋转 · 右键平移 · 滚轮缩放 · 拖动标注调整位置 · 双击标注修改内容</div>
<button id="save-btn" onclick="saveLayout()" style="position:fixed;bottom:16px;right:16px;background:rgba(26,95,204,0.9);color:#fff;border:none;border-radius:999px;padding:10px 20px;font-size:12px;font-weight:600;cursor:pointer;z-index:10;box-shadow:0 2px 8px rgba(0,0,0,0.15);">保存标注布局</button>
<svg id="leader-svg" xmlns="http://www.w3.org/2000/svg" style="position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:4;"></svg>
{importmap_block}
<script type="module">
import * as THREE from 'three';
import {{ OrbitControls }} from 'three/addons/OrbitControls.js';
import {{ STLLoader }} from 'three/addons/STLLoader.js';
const annotations = {annotations_json};
const info = {info_json};
const infoFields = document.getElementById('info-fields');
const fieldLabels = {{ material: '材料', tolerance: '公差', finish: '表面', weight: '重量(g)', doc_number: '图号', bounding_box: '外形尺寸' }};
for (const [key, label] of Object.entries(fieldLabels)) {{ if (info[key]) infoFields.innerHTML += `<div class="field"><b>${{label}}:</b> ${{info[key]}}</div>`; }}
const annList = document.getElementById('ann-list');
const annGroups = [];
const specToGroup = {{}};
annotations.forEach((a, idx) => {{ const key = a.spec; if (!specToGroup[key]) {{ specToGroup[key] = {{ spec: a.spec, type: a.type||'clearance', indices: [] }}; annGroups.push(specToGroup[key]); }} specToGroup[key].indices.push(idx); }});
annGroups.forEach((g, gi) => {{ const label = g.indices.length > 1 ? `${{g.indices.length}}\u00d7 ${{g.spec}}` : g.spec; const div = document.createElement('div'); div.className = `ann-item ${{g.type}}`; div.textContent = label.replace('\\n',' | '); div.title = '\u5355\u51fb\u9ad8\u4eae\u5b54\u4f4d\uff0c\u53cc\u51fb\u7f16\u8f91\u6807\u6ce8'; div.dataset.gi = gi; div.addEventListener('click', () => toggleGroup(gi)); div.addEventListener('dblclick', (e) => {{ e.stopPropagation(); const cur = div.textContent; const nt = prompt('\u4fee\u6539\u5b54\u6807\u6ce8\u5185\u5bb9\uff1a', cur); if (nt !== null && nt.trim()) {{ div.textContent = nt.trim(); const edits = JSON.parse(localStorage.getItem('ann_group_edits_' + document.title) || '{{}}'); edits[gi] = nt.trim(); localStorage.setItem('ann_group_edits_' + document.title, JSON.stringify(edits)); }} }}); const savedEdits = JSON.parse(localStorage.getItem('ann_group_edits_' + document.title) || '{{}}'); if (savedEdits[gi]) div.textContent = savedEdits[gi]; annList.appendChild(div); }});
let activeGroup = -1;
function toggleGroup(gi) {{ activeGroup = (activeGroup === gi) ? -1 : gi; updateVis(); document.querySelectorAll('#ann-list .ann-item').forEach((el, i) => el.classList.toggle('active', i === activeGroup)); }}

// 3D ring highlights will be created after mesh is loaded
const typeColorsHex = {{thread:0xe03030, pin:0x1a9e3f, clearance:0x2070cc, counterbore:0xcc8800}};
let ringGroup, holeRings = [];

function updateVis() {{
  // Hide all rings first
  holeRings.forEach(r => {{ if (r) r.visible = false; }});
  if (activeGroup === -1) {{ labelElements.forEach(le => {{ le.el.classList.add('hidden-label'); le.vis=false; le.asDot=false; }}); return; }}
  const group = annGroups[activeGroup]; const activeSet = new Set(group.indices);
  // Show rings for active group
  group.indices.forEach(idx => {{ if (holeRings[idx]) holeRings[idx].visible = true; }});
  // Hide all labels — only rings are shown
  labelElements.forEach(le => {{ le.el.classList.add('hidden-label'); le.vis=false; le.asDot=false; }});
}}
const container = document.getElementById('viewer');
const scene = new THREE.Scene(); scene.background = new THREE.Color(0xc8ccd0);
const camera = new THREE.PerspectiveCamera(45, window.innerWidth/window.innerHeight, 0.1, 10000);
const renderer = new THREE.WebGLRenderer({{ antialias: true }}); renderer.setSize(window.innerWidth, window.innerHeight); renderer.setPixelRatio(window.devicePixelRatio); renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.2; container.appendChild(renderer.domElement);
const controls = new OrbitControls(camera, renderer.domElement); controls.enableDamping = true; controls.dampingFactor = 0.05;
controls.mouseButtons = {{ LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.PAN, RIGHT: null }};
controls.keys = {{ LEFT: 'ArrowRight', UP: 'ArrowDown', RIGHT: 'ArrowLeft', BOTTOM: 'ArrowUp' }};
controls.keyPanSpeed = 25;
controls.listenToKeyEvents(window);
renderer.domElement.addEventListener('contextmenu', e => e.preventDefault());
scene.add(new THREE.AmbientLight(0xffffff, 0.5));
const dl = new THREE.DirectionalLight(0xffffff, 1.0); dl.position.set(200,400,300); scene.add(dl);
const dl2 = new THREE.DirectionalLight(0xddeeff, 0.4); dl2.position.set(-150,-100,-200); scene.add(dl2);
const dl3 = new THREE.DirectionalLight(0xffeedd, 0.3); dl3.position.set(0,300,-200); scene.add(dl3);
const loader = new STLLoader();
const stlData = atob('{stl_b64}');
const buffer = new ArrayBuffer(stlData.length); const view = new Uint8Array(buffer);
for (let i=0;i<stlData.length;i++) view[i]=stlData.charCodeAt(i);
const geometry = loader.parse(buffer); geometry.computeVertexNormals();
const material = new THREE.MeshPhongMaterial({{ color:0xd0d4d8, specular:0x444444, shininess:30, side: THREE.DoubleSide }});
const mesh = new THREE.Mesh(geometry, material); scene.add(mesh);
const edgeGeo = new THREE.EdgesGeometry(geometry, 25);
const edgeMat = new THREE.LineBasicMaterial({{ color: 0x444444, opacity: 0.35, transparent: true }});
const edges = new THREE.LineSegments(edgeGeo, edgeMat); mesh.add(edges);
geometry.computeBoundingBox(); const bbox = geometry.boundingBox;
const center = new THREE.Vector3(); bbox.getCenter(center); mesh.position.sub(center);
const size = new THREE.Vector3(); bbox.getSize(size);
const maxDim = Math.max(size.x,size.y,size.z);
camera.position.set(maxDim*1.2, maxDim*0.8, maxDim*1.5); controls.target.set(0,0,0); controls.update();
// Create 3D hole rings now that center is known
ringGroup = new THREE.Group(); scene.add(ringGroup);
annotations.forEach((ann, idx) => {{
  if (!ann.point || !ann.axis || !ann.diameter) {{ holeRings.push(null); return; }}
  const radius = ann.diameter / 2;
  const tubeRadius = Math.max(radius * 0.12, 0.5);
  const torusGeo = new THREE.TorusGeometry(radius, tubeRadius, 8, 48);
  const colorHex = typeColorsHex[ann.type] || 0x2070cc;
  const torusMat = new THREE.MeshBasicMaterial({{ color: colorHex, depthTest: false, transparent: true, opacity: 0.92 }});
  const torusMesh = new THREE.Mesh(torusGeo, torusMat);
  torusMesh.renderOrder = 999;
  torusMesh.position.set(ann.point[0] - center.x, ann.point[1] - center.y, ann.point[2] - center.z);
  const holeAxis = new THREE.Vector3(ann.axis[0], ann.axis[1], ann.axis[2]).normalize();
  const up = new THREE.Vector3(0, 0, 1);
  const quat = new THREE.Quaternion().setFromUnitVectors(up, holeAxis);
  torusMesh.quaternion.copy(quat);
  torusMesh.visible = false;
  ringGroup.add(torusMesh);
  holeRings.push(torusMesh);
}});
const labelContainer = document.createElement('div');
labelContainer.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:5;';
document.body.appendChild(labelContainer);
const leaderSvg = document.getElementById('leader-svg');
const labelElements = [];
const labelOffsets = JSON.parse(localStorage.getItem('ann_offsets_'+document.title)||'{{}}');
const labelEdits = JSON.parse(localStorage.getItem('ann_edits_'+document.title)||'{{}}');
annotations.forEach((ann,idx) => {{
  if (!ann.point) return;
  const el = document.createElement('div');
  el.className = `label-3d ${{ann.type||'clearance'}}`;
  el.textContent = labelEdits[idx] || ann.spec.replace('\\n',' | ');
  el.style.pointerEvents = 'auto';
  labelContainer.appendChild(el);
  const line = document.createElementNS('http://www.w3.org/2000/svg','line');
  const typeColors = {{thread:'#e03030',pin:'#1a9e3f',clearance:'#2070cc',counterbore:'#cc8800'}};
  const color = typeColors[ann.type]||'#2070cc';
  line.setAttribute('stroke',color); line.setAttribute('stroke-width','1.5'); leaderSvg.appendChild(line);
  const dot = document.createElementNS('http://www.w3.org/2000/svg','circle');
  dot.setAttribute('r','5'); dot.setAttribute('fill',color); dot.setAttribute('stroke','#fff'); dot.setAttribute('stroke-width','1.5'); leaderSvg.appendChild(dot);
  const savedOffset = labelOffsets[idx];
  const defaultOffset = {{x:(idx%3-1)*60, y:-30-(idx*15)}};
  const offset = savedOffset||defaultOffset;
  labelElements.push({{el,line,dot,position:new THREE.Vector3(ann.point[0]-center.x,ann.point[1]-center.y,ann.point[2]-center.z),offset,idx,vis:false,asDot:false}});
  el.classList.add('hidden-label');
  let dragging=false, dragStart={{x:0,y:0}};
  el.addEventListener('pointerdown',(e)=>{{dragging=true;dragStart={{x:e.clientX-offset.x,y:e.clientY-offset.y}};el.classList.add('dragging');el.setPointerCapture(e.pointerId);controls.enabled=false;e.stopPropagation();}});
  el.addEventListener('pointermove',(e)=>{{if(!dragging)return;offset.x=e.clientX-dragStart.x;offset.y=e.clientY-dragStart.y;e.stopPropagation();}});
  el.addEventListener('pointerup',(e)=>{{dragging=false;el.classList.remove('dragging');el.releasePointerCapture(e.pointerId);controls.enabled=true;labelOffsets[idx]={{x:offset.x,y:offset.y}};e.stopPropagation();}});
  el.addEventListener('dblclick',(e)=>{{e.stopPropagation();const cur=el.textContent;const nt=prompt('修改标注内容：',cur);if(nt!==null&&nt.trim()){{el.textContent=nt.trim();const ed=JSON.parse(localStorage.getItem('ann_edits_'+document.title)||'{{}}');ed[idx]=nt.trim();localStorage.setItem('ann_edits_'+document.title,JSON.stringify(ed));}}}});
}});
window.saveLayout=function(){{const o={{}};labelElements.forEach(l=>{{o[l.idx]=l.offset;}});localStorage.setItem('ann_offsets_'+document.title,JSON.stringify(o));alert('标注布局已保存');}};
function animate(){{
  requestAnimationFrame(animate); controls.update(); renderer.render(scene,camera);
  // Ring occlusion check (optimized): spread raycast tests across frames
  // Only test a few rings per frame, cycling through all visible rings
  if (!window._ringOccState) {{
    window._ringOccState = {{ frameCount: 0, nextIdx: 0, raycaster: new THREE.Raycaster() }};
  }}
  const occState = window._ringOccState;
  occState.frameCount++;
  // Run occlusion checks every 8 frames, testing up to 4 rings per batch
  if (occState.frameCount % 8 === 0) {{
    const visibleRings = [];
    holeRings.forEach((ring, idx) => {{ if (ring && ring.visible) visibleRings.push({{ ring, idx }}); }});
    if (visibleRings.length > 0) {{
      const batchSize = Math.min(4, visibleRings.length);
      for (let b = 0; b < batchSize; b++) {{
        const vi = occState.nextIdx % visibleRings.length;
        occState.nextIdx++;
        const {{ ring }} = visibleRings[vi];
        const ringPos = ring.position.clone();
        const dir = ringPos.clone().sub(camera.position).normalize();
        occState.raycaster.set(camera.position, dir);
        const intersects = occState.raycaster.intersectObject(mesh);
        const distToRing = camera.position.distanceTo(ringPos);
        const occluded = intersects.length > 0 && intersects[0].distance < distToRing - 1.0;
        ring.material.opacity = occluded ? 0.25 : 0.92;
      }}
    }}
  }}
  const raycaster=new THREE.Raycaster();
  labelElements.forEach(label=>{{
    if (!label.vis) {{ label.el.style.opacity='0'; label.line.style.opacity='0'; label.dot.style.opacity='0'; return; }}
    const projected=label.position.clone().project(camera);
    if(projected.z<1){{
      const anchorX=(projected.x*0.5+0.5)*window.innerWidth;
      const anchorY=(-projected.y*0.5+0.5)*window.innerHeight;
      if (label.asDot) {{
        label.el.style.left=`${{anchorX}}px`;label.el.style.top=`${{anchorY}}px`;label.el.style.opacity='1';
        label.line.style.opacity='0'; label.dot.style.opacity='0';
      }} else {{
        const dir=label.position.clone().sub(camera.position).normalize();
        raycaster.set(camera.position,dir);
        const intersects=raycaster.intersectObject(mesh);
        const distToLabel=camera.position.distanceTo(label.position);
        const occluded=intersects.length>0&&intersects[0].distance<distToLabel-1.0;
        const labelX=anchorX+label.offset.x;
        const labelY=anchorY+label.offset.y;
        const vis=occluded?'0.1':'1';
        label.el.style.left=`${{labelX}}px`;label.el.style.top=`${{labelY}}px`;label.el.style.opacity=vis;
        label.line.setAttribute('x1',anchorX);label.line.setAttribute('y1',anchorY);label.line.setAttribute('x2',labelX);label.line.setAttribute('y2',labelY);label.line.style.opacity=vis;
        label.dot.setAttribute('cx',anchorX);label.dot.setAttribute('cy',anchorY);label.dot.style.opacity=vis;
      }}
    }}else{{label.el.style.opacity='0';label.line.style.opacity='0';label.dot.style.opacity='0';}}
  }});
}}
animate();
window.addEventListener('resize',()=>{{camera.aspect=window.innerWidth/window.innerHeight;camera.updateProjectionMatrix();renderer.setSize(window.innerWidth,window.innerHeight);}});
</script>
</body>
</html>'''

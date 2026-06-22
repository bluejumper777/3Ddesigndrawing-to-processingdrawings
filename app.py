"""Hole Annotator Tool — 3D STEP Model Hole Recognition & Annotation Viewer

Upload a STEP file, automatically detect holes (thread, pin, counterbore),
and generate an interactive 3D annotated HTML viewer.

Features:
- Assembly tree view showing parent-child hierarchy
- Per-part 3D highlighting with color-coded standard vs custom parts
- Session persistence across page reloads
- Two-step workflow: upload → select parts → generate annotations
"""

import os
import json
import webbrowser
import threading
import time
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from annotator import (
    analyze_step_and_generate_viewer,
    split_assembly,
    _flatten_tree,
    export_and_analyze_part,
    _is_standard_part,
    _export_part_by_name,
    load_assembly_with_meshes,
)

app = FastAPI(title="Hole Annotator Tool", version="5.0.0")

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

SESSION_FILE = OUTPUT_DIR / "_last_session.json"

# In-memory session store for pending assemblies
_sessions: dict[str, dict] = {}


@app.get("/", response_class=HTMLResponse)
async def index():
    return _build_page_html()


@app.get("/session")
async def get_session():
    """Return saved session data if exists."""
    if SESSION_FILE.exists():
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            # Verify the step file still exists
            if Path(data.get("step_path", "")).exists():
                return JSONResponse(content=data)
        except Exception:
            pass
    raise HTTPException(status_code=404, detail="No saved session")


@app.post("/clear-session")
async def clear_session():
    """Clear the saved session so the upload page shows on reload."""
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
    _sessions.clear()
    return {"status": "ok"}


def _save_session(session_id: str, tree: list, step_path: str, output_dir: str = ""):
    """Persist session to disk for reload recovery."""
    payload = {
        "session_id": session_id,
        "tree": tree,
        "step_path": step_path,
        "output_dir": output_dir or str(OUTPUT_DIR),
    }
    SESSION_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@app.post("/upload")
async def upload_step(file: UploadFile = File(...)):
    """Upload STEP, split assembly, export STL for preview, return tree."""
    original_name = file.filename or "model.step"
    content = await file.read()

    # Use STEP filename stem as project folder name (sanitized)
    project_name = Path(original_name).stem
    safe_project = "".join(c if c.isalnum() or c in "._- " else "_" for c in project_name).strip() or "project"

    session_id = uuid4().hex[:8]
    # Create project folder under output
    project_dir = OUTPUT_DIR / safe_project
    project_dir.mkdir(parents=True, exist_ok=True)
    session_dir = project_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    step_path = session_dir / original_name
    step_path.write_bytes(content)

    # Try splitting as assembly (text regex, fast)
    parts = split_assembly(str(step_path), str(session_dir))

    if not parts:
        # Single part — process directly
        result_data = analyze_step_and_generate_viewer(str(step_path), session_id, str(project_dir))
        if result_data.get("error"):
            raise HTTPException(status_code=500, detail=result_data["error"])
        if result_data.get("total_holes", 0) == 0:
            return {
                "status": "completed",
                "is_assembly": False,
                "message": "该零件没有识别到孔特征，无需标注。",
            }
        return {
            "status": "completed",
            "is_assembly": False,
            "total_holes": result_data["total_holes"],
            "viewer_url": f"/output/{safe_project}/{session_id}/{Path(result_data['html_path']).name}",
        }

    # Assembly — return tree structure
    flat_list = _flatten_tree(parts)

    # Mark standard parts in the tree
    _mark_standard_parts(parts)

    # Store session
    _sessions[session_id] = {
        "tree": parts,
        "parts": flat_list,
        "step_path": str(step_path),
        "output_dir": str(project_dir),
    }

    # Persist session for reload
    _save_session(session_id, parts, str(step_path), str(project_dir))

    return {
        "status": "pending_confirmation",
        "is_assembly": True,
        "session_id": session_id,
        "total_parts": len(flat_list),
        "tree": parts,
    }


def _mark_standard_parts(nodes: list):
    """Recursively mark nodes with is_standard flag."""
    for node in nodes:
        if node.get("type") == "part":
            node["is_standard"] = _is_standard_part(node.get("name", ""))
        if node.get("children"):
            _mark_standard_parts(node["children"])

@app.post("/upload-single")
async def upload_single_part(file: UploadFile = File(...)):
    """Upload a single STEP part and directly generate hole annotations (no assembly tree)."""
    original_name = file.filename or "model.step"
    content = await file.read()

    project_name = Path(original_name).stem
    safe_project = "".join(c if c.isalnum() or c in "._- " else "_" for c in project_name).strip() or "part"

    session_id = uuid4().hex[:8]
    project_dir = OUTPUT_DIR / safe_project
    project_dir.mkdir(parents=True, exist_ok=True)
    session_dir = project_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    step_path = session_dir / original_name
    step_path.write_bytes(content)

    # Directly analyze as single part
    result_data = analyze_step_and_generate_viewer(str(step_path), session_id, str(project_dir))
    if result_data.get("error"):
        raise HTTPException(status_code=500, detail=result_data["error"])

    total_holes = result_data.get("total_holes", 0)
    viewer_url = None
    if result_data.get("html_path"):
        html_path = Path(result_data["html_path"])
        try:
            rel_path = html_path.resolve().relative_to(OUTPUT_DIR.resolve())
        except ValueError:
            rel_path = Path(html_path.parent.name) / html_path.name
        viewer_url = f"/output/{rel_path.as_posix()}"

    return {
        "status": "completed",
        "total_holes": total_holes,
        "viewer_url": viewer_url,
    }


@app.post("/confirm")
async def confirm_and_annotate(session_id: str = Form(...),
                                parts_json: str = Form(""),
                                skip_indices: str = Form("")):
    """Generate hole annotations. Supports weldment mode (merge children)."""
    # Load session from memory or disk
    if session_id in _sessions:
        session = _sessions[session_id]
    elif SESSION_FILE.exists():
        try:
            saved = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            if saved.get("session_id") == session_id:
                flat_list = _flatten_tree(saved["tree"])
                session = {
                    "tree": saved["tree"],
                    "parts": flat_list,
                    "step_path": saved["step_path"],
                    "output_dir": saved.get("output_dir", str(OUTPUT_DIR)),
                }
            else:
                raise HTTPException(status_code=404, detail="会话已过期")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=404, detail="会话数据损坏")
    else:
        raise HTTPException(status_code=404, detail="会话已过期，请重新上传")

    step_path = session["step_path"]
    output_dir = session["output_dir"]
    tree = session.get("tree", [])
    flat_parts = session["parts"]

    results = []
    no_holes = 0

    # New mode: parts_json specifies exactly what to process
    if parts_json.strip():
        confirm_list = json.loads(parts_json)

        # Build a name->node lookup from tree (recursive)
        def find_node(nodes, name):
            for n in nodes:
                if n.get("name") == name:
                    return n
                if n.get("children"):
                    found = find_node(n["children"], name)
                    if found:
                        return found
            return None

        # Collect all solid_indices recursively from a node
        def collect_indices(node):
            indices = []
            if node.get("solid_indices"):
                indices.extend(node["solid_indices"])
            if node.get("children"):
                for child in node["children"]:
                    indices.extend(collect_indices(child))
            return indices

        for item in confirm_list:
            name = item["name"]
            is_weldment = item.get("is_weldment", False)
            node = find_node(tree, name)
            if not node:
                continue

            if is_weldment:
                # Merge all children's solid_indices into one
                all_indices = collect_indices(node)
            else:
                all_indices = node.get("solid_indices", [])

            # If no solid_indices available (common when XDE mapping fails),
            # fall back to exporting by name matching via XDE
            if not all_indices:
                # Use the full STEP file and let export handle by name
                part_session = f"{session_id}_{name[:8]}"
                result = _export_part_by_name(
                    step_path, name, is_weldment,
                    part_session, output_dir
                )
            else:
                part_session = f"{session_id}_{name[:8]}"
                result = export_and_analyze_part(
                    step_path, all_indices, name + (" [焊件]" if is_weldment else ""),
                    part_session, output_dir
                )

            if result.get("error"):
                # Name matching or export failed — report but continue
                results.append({
                    "part_name": name,
                    "total_holes": 0,
                    "html_path": None,
                    "error": result["error"],
                })
                continue
            if result.get("total_holes", 0) == 0:
                no_holes += 1
                continue
            results.append(result)
    else:
        # Legacy mode: use skip_indices
        skip_set = set()
        if skip_indices.strip():
            for s in skip_indices.split(","):
                try:
                    skip_set.add(int(s.strip()))
                except ValueError:
                    pass

        for idx, part in enumerate(flat_parts):
            if idx in skip_set:
                continue
            if part.get("type") == "assembly":
                continue
            part_session = f"{session_id}_p{idx}"
            result = export_and_analyze_part(
                step_path, part.get("solid_indices", []), part["name"],
                part_session, output_dir
            )
            if result.get("total_holes", 0) == 0:
                no_holes += 1
                continue
            results.append(result)

    parts_info = []
    errors_info = []
    total_holes = 0
    for part in results:
        if part.get("error") and not part.get("html_path"):
            errors_info.append({
                "name": part.get("part_name", "Unknown"),
                "error": part["error"],
            })
            continue
        if part.get("html_path"):
            html_path = Path(part["html_path"])
            # Build relative path from OUTPUT_DIR for URL
            try:
                rel_path = html_path.resolve().relative_to(OUTPUT_DIR.resolve())
            except ValueError:
                rel_path = Path(html_path.parent.name) / html_path.name
            parts_info.append({
                "name": part.get("part_name", "Unknown"),
                "total_holes": part["total_holes"],
                "viewer_url": f"/output/{rel_path.as_posix()}",
            })
            total_holes += part["total_holes"]

    return {
        "status": "completed",
        "total_parts": len(parts_info),
        "total_holes": total_holes,
        "skipped_no_holes": no_holes,
        "errors": errors_info,
        "parts": parts_info,
    }


@app.get("/step-file/{session_id}")
async def get_step_file(session_id: str):
    """Serve the uploaded STEP file for frontend 3D rendering."""
    # Try to find via saved session first
    if SESSION_FILE.exists():
        try:
            saved = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            if saved.get("session_id") == session_id:
                sp = Path(saved["step_path"])
                if sp.exists():
                    return FileResponse(str(sp), media_type="application/octet-stream")
        except Exception:
            pass
    # Fallback: search in OUTPUT_DIR recursively
    for sub in OUTPUT_DIR.rglob(f"{session_id}"):
        if sub.is_dir():
            for f in sub.iterdir():
                if f.suffix.lower() in ('.step', '.stp'):
                    return FileResponse(str(f), media_type="application/octet-stream")
    # Legacy: direct session_id folder
    session_dir = OUTPUT_DIR / session_id
    if session_dir.exists():
        for f in session_dir.iterdir():
            if f.suffix.lower() in ('.step', '.stp'):
                return FileResponse(str(f), media_type="application/octet-stream")
    raise HTTPException(status_code=404)


@app.get("/mesh-data/{session_id}")
async def get_mesh_data(session_id: str):
    """Return cached tessellated mesh data for 3D rendering (XDE-based, ID-linked)."""
    mesh_file = OUTPUT_DIR / session_id / "meshes.json"
    if mesh_file.exists():
        return FileResponse(str(mesh_file), media_type="application/json")
    # Generate on-the-fly if not cached
    session_dir = OUTPUT_DIR / session_id
    step_path = None
    for f in session_dir.iterdir():
        if f.suffix.lower() in ('.step', '.stp'):
            step_path = f
            break
    if not step_path:
        raise HTTPException(status_code=404, detail="No STEP file found")
    try:
        result = load_assembly_with_meshes(str(step_path))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tessellation error: {e}")
    if not result:
        raise HTTPException(status_code=500, detail="Tessellation returned no data")
    # Cache to disk
    mesh_file.write_text(json.dumps(result["meshes"]), encoding="utf-8")
    return JSONResponse(content=result["meshes"])


@app.post("/save-glb")
async def save_glb(session_id: str = Form(...), file: UploadFile = File(...)):
    """Save GLB cache from frontend after STEP parsing."""
    session_dir = OUTPUT_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    glb_path = session_dir / "preview.glb"
    content = await file.read()
    glb_path.write_bytes(content)
    return {"status": "ok", "size": len(content)}


@app.post("/save-mesh-names")
async def save_mesh_names(session_id: str = Form(...), mesh_names: str = Form(...)):
    """Cache mesh name list from frontend STEP parsing for name-mapping."""
    session_dir = OUTPUT_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    mesh_file = session_dir / "mesh_names.json"
    mesh_file.write_text(mesh_names, encoding="utf-8")
    return {"status": "ok"}


@app.post("/save-tree")
async def save_tree(session_id: str = Form(...), tree_json: str = Form(...)):
    """Save updated tree state (standard/weldment flags) to session file."""
    if not SESSION_FILE.exists():
        raise HTTPException(status_code=404, detail="No session to update")
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        if data.get("session_id") != session_id:
            raise HTTPException(status_code=404, detail="Session mismatch")
        # Update tree with new flags
        data["tree"] = json.loads(tree_json)
        SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Serve output files
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")

# Serve static files (occt-import-js wasm, etc.)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _build_page_html() -> str:
    """Read the frontend HTML from static/index.html."""
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<html><body><h1>Error: static/index.html not found</h1></body></html>"


def main():
    port = 8080
    print(f"\n  3D 孔标注工具 v5.0")
    print(f"  浏览器访问: http://127.0.0.1:{port}")
    print(f"  项目会自动保存，关闭后重新打开无需重新上传\n")

    def open_browser():
        time.sleep(1.5)
        webbrowser.open(f"http://127.0.0.1:{port}")

    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()

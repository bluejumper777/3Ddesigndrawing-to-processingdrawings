import sys, glob
sys.path.insert(0, '.')

fs = glob.glob('output/*/总装*.STEP') + glob.glob('output/*/*.STEP')
if not fs:
    print("No STEP file found"); sys.exit(1)

step_path = fs[0]
print(f"Testing: {step_path}")

import cadquery as cq
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.TDocStd import TDocStd_Document
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TDF import TDF_LabelSequence, TDF_Label
from OCP.TDataStd import TDataStd_Name
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.TopExp import TopExp

# XDE
doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
reader = STEPCAFControl_Reader()
reader.SetNameMode(True)
st = reader.ReadFile(step_path)
print(f"ReadFile status: {st}")
tr = reader.Transfer(doc)
print(f"Transfer result: {tr}")

shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

free_labels = TDF_LabelSequence()
shape_tool.GetFreeShapes(free_labels)
print(f"Free shapes: {free_labels.Length()}")

for i in range(1, min(free_labels.Length() + 1, 4)):
    label = free_labels.Value(i)
    name_attr = TDataStd_Name()
    name = ""
    if label.FindAttribute(TDataStd_Name.GetID_s(), name_attr):
        name = name_attr.Get().ToExtString()
    is_asm = shape_tool.IsAssembly(label)
    is_simple = shape_tool.IsSimpleShape(label)
    is_compound = shape_tool.IsCompound(label)
    print(f"  Label {i}: name='{name}' asm={is_asm} simple={is_simple} compound={is_compound}")

    if is_asm:
        comp_labels = TDF_LabelSequence()
        shape_tool.GetComponents(label, comp_labels)
        print(f"    Components: {comp_labels.Length()}")
        for j in range(1, min(comp_labels.Length() + 1, 6)):
            cl = comp_labels.Value(j)
            cn = ""
            cn_attr = TDataStd_Name()
            if cl.FindAttribute(TDataStd_Name.GetID_s(), cn_attr):
                cn = cn_attr.Get().ToExtString()
            is_ref = shape_tool.IsReference(cl)
            print(f"      [{j}] name='{cn}' isRef={is_ref}")

# Also check solid count
result = cq.importers.importStep(step_path)
shape = result.val().wrapped
solid_map = TopTools_IndexedMapOfShape()
TopExp.MapShapes_s(shape, TopAbs_SOLID, solid_map)
print(f"\nTotal solids via TopExp: {solid_map.Extent()}")

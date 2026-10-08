"""
VAT Exporter para Maya  (soft body + rigid body)
================================================
Exporta Vertex Animation Textures para:
  - Web (Three.js): .bin float32/float16 + JSON con la malla
  - Unity / Unreal: EXR (half/float) + FBX con UV2 de lookup + JSON
  - Genérico: todo lo anterior

Uso (Script Editor de Maya, pestaña Python):
    1) Pega todo este archivo y ejecútalo -> se abre la ventana.
    2) Selecciona la malla (o varias piezas en modo Rigid) y pulsa Exportar.

Uso por código (sin ventana):
    export_vat(out="C:/MayaExports/VertexData", base="my_mesh", target="web")

Convención de la textura
------------------------
  - Ancho  = frames (x tiles), Alto = filas ("vértices" ya desdoblados).
  - Fila r = vértice de exportación r (coincide con el orden de la malla del JSON
    y con el UV2 del FBX: v = (fila + 0.5) / tile_height).
  - Si hay más filas que max_tex, se parte en "tiles" en horizontal:
        x = tile * frames + frame ,  y = fila % tile_height
    y el UV2 lleva u = tile / tiles.
  - .bin: fila 0 = v 0 (listo para THREE.DataTexture).
  - .exr: se guarda volteado vertical para que v = (fila+0.5)/alto funcione
    tal cual en Unity y Unreal con el UV2 exportado desde Maya.
"""

import os
import json
import struct
import math
import shutil
import socket
import subprocess
import sys
import time
import webbrowser

import numpy as np

try:
    import maya.cmds as cmds
    import maya.api.OpenMaya as om
except ImportError:          # permite importar el módulo fuera de Maya (tests)
    cmds = om = None

WIN = "vatExporterWin"

# ----------------------------------------------------------------------------
# Presets por destino
# ----------------------------------------------------------------------------
_SWAP_YZ = np.array([[1, 0, 0], [0, 0, 1], [0, 1, 0]], dtype=np.float32)

TARGETS = {
    "Web (Three.js)": dict(key="web",     C=np.eye(3, dtype=np.float32),                  scale=1.0,  up="y",
                           bin=True,  exr=False, fbx=False, json_mesh=True),
    "Unity":          dict(key="unity",   C=np.diag([-1, 1, 1]).astype(np.float32),       scale=0.01, up="y",
                           bin=False, exr=True,  fbx=True,  json_mesh=False),
    "Unreal":         dict(key="unreal",  C=_SWAP_YZ,                                     scale=1.0,  up="z",
                           bin=False, exr=True,  fbx=True,  json_mesh=False),
    "Genérico":       dict(key="generic", C=np.eye(3, dtype=np.float32),                  scale=1.0,  up="y",
                           bin=True,  exr=True,  fbx=True,  json_mesh=True),
}


# ----------------------------------------------------------------------------
# Utilidades puras (no dependen de Maya)
# ----------------------------------------------------------------------------
def pack_tiles(arr, tile_h, tiles):
    """(filas, frames, C) -> (tile_h, tiles*frames, C)"""
    rows, nf, c = arr.shape
    out = np.zeros((tile_h, tiles * nf, c), dtype=arr.dtype)
    for t in range(tiles):
        chunk = arr[t * tile_h:(t + 1) * tile_h]
        out[:chunk.shape[0], t * nf:(t + 1) * nf] = chunk
    return out


def convert_quat(q_xyz, w, C):
    """Convierte un cuaternión al sistema de ejes C (con posible reflexión)."""
    det = float(np.linalg.det(C))
    v = det * (C @ np.asarray(q_xyz, dtype=np.float32))
    return v, w


def write_exr(path, arr, half=True):
    """
    Escritor EXR mínimo, sin compresión (lo que piden Unity/Unreal para VAT).
    arr: (alto, ancho, 3|4) float. Fila 0 = arriba de la imagen.
    """
    arr = np.ascontiguousarray(arr)
    h, w, c = arr.shape
    if c not in (3, 4):
        raise ValueError("EXR: se esperan 3 o 4 canales")
    names = ["R", "G", "B", "A"][:c]
    order = sorted(names)                       # EXR exige orden alfabético
    idx = [names.index(n) for n in order]
    ptype = 1 if half else 2                    # 1 = HALF, 2 = FLOAT
    dt = np.float16 if half else np.float32

    def attr(name, typ, data):
        return name.encode() + b"\0" + typ.encode() + b"\0" + struct.pack("<i", len(data)) + data

    chlist = b""
    for n in order:
        chlist += n.encode() + b"\0" + struct.pack("<iBBBBii", ptype, 0, 0, 0, 0, 1, 1)
    chlist += b"\0"

    box = struct.pack("<iiii", 0, 0, w - 1, h - 1)
    header = b""
    header += attr("channels", "chlist", chlist)
    header += attr("compression", "compression", struct.pack("<B", 0))
    header += attr("dataWindow", "box2i", box)
    header += attr("displayWindow", "box2i", box)
    header += attr("lineOrder", "lineOrder", struct.pack("<B", 0))
    header += attr("pixelAspectRatio", "float", struct.pack("<f", 1.0))
    header += attr("screenWindowCenter", "v2f", struct.pack("<ff", 0.0, 0.0))
    header += attr("screenWindowWidth", "float", struct.pack("<f", 1.0))
    header += b"\0"

    bytes_per = 2 if half else 4
    line_data = w * len(order) * bytes_per
    block = 8 + line_data
    start = 8 + len(header) + 8 * h

    stacked = arr[:, :, idx].transpose(0, 2, 1).astype(dt)   # (h, canales, w)

    with open(path, "wb") as fh:
        fh.write(b"\x76\x2f\x31\x01")
        fh.write(struct.pack("<i", 2))
        fh.write(header)
        fh.write(np.array([start + y * block for y in range(h)], dtype="<u8").tobytes())
        for y in range(h):
            fh.write(struct.pack("<ii", y, line_data))
            fh.write(stacked[y].tobytes())


# ----------------------------------------------------------------------------
# Helpers de Maya
# ----------------------------------------------------------------------------
def _dag_and_mesh(node):
    sl = om.MSelectionList()
    sl.add(node)
    dag = sl.getDagPath(0)
    dag.extendToShape()
    return dag, om.MFnMesh(dag)


def build_rows(dag, mesh, hard_normals):
    """
    Desdobla vértices por (vértice, UV, normal[opcional]). Cada combinación única
    es una fila de la textura y un vértice de la malla exportada.
    """
    nuv = mesh.numUVs()
    uv_u, uv_v = mesh.getUVs() if nuv else ([], [])

    rowmap = {}
    rows = dict(vert=[], nid=[], face=[], uv=[], index=[], fv_rows=[], counts=[])

    it = om.MItMeshPolygon(dag)
    while not it.isDone():
        fverts = list(it.getVertices())
        face = it.index()
        here = []
        for local, v in enumerate(fverts):
            try:
                uvid = it.getUVIndex(local) if nuv else -1
            except RuntimeError:
                uvid = -1
            nid = it.normalIndex(local) if hard_normals else -1
            key = (v, uvid, nid)
            r = rowmap.get(key)
            if r is None:
                r = len(rows["vert"])
                rowmap[key] = r
                rows["vert"].append(v)
                rows["nid"].append(nid)
                rows["face"].append(face)
                rows["uv"] += [uv_u[uvid], uv_v[uvid]] if uvid >= 0 else [0.0, 0.0]
            here.append(r)
        rows["fv_rows"].append(here)
        rows["counts"].append(len(here))
        _, tverts = it.getTriangles()
        for tv in tverts:
            rows["index"].append(here[fverts.index(tv)])
        it.next()
    return rows


def _static_attrs(mesh, rows, hard, space, C, scale):
    """Posiciones y normales por fila en el frame actual, ya convertidas."""
    rv = np.asarray(rows["vert"])
    pts = np.array(mesh.getPoints(space), dtype=np.float32)[:, :3][rv]
    if hard:
        n = np.array(mesh.getNormals(space), dtype=np.float32)[np.asarray(rows["nid"])]
    else:
        n = np.array(mesh.getVertexNormals(True, space), dtype=np.float32)[rv]
    return pts @ C.T * scale, n @ C.T


def _tangent_ids(mesh, rows):
    return np.array([mesh.getTangentId(f, v) for f, v in zip(rows["face"], rows["vert"])])


def sample_soft(mesh, rows, frames, o, C, scale, det):
    n = len(rows["vert"])
    nf = len(frames)
    space = om.MSpace.kObject if o["space"] == "object" else om.MSpace.kWorld
    pos = np.zeros((n, nf, 4), np.float32)
    nrm = np.zeros((n, nf, 4), np.float32)
    tan = np.zeros((n, nf, 4), np.float32) if o["tangents"] else None
    col = np.zeros((n, nf, 4), np.float32) if o["colors"] else None
    rv = np.asarray(rows["vert"])

    tid = None
    if tan is not None:
        try:
            tid = _tangent_ids(mesh, rows)
        except Exception as e:
            cmds.warning("Tangentes desactivadas (%s)" % e)
            tan = None
    if col is not None and not mesh.numColorSets:
        cmds.warning("La malla no tiene color set: se omiten colores.")
        col = None

    cmds.progressWindow(title="VAT", status="Muestreando…", maxValue=nf, isInterruptable=True)
    try:
        for i, f in enumerate(frames):
            if cmds.progressWindow(q=True, isCancelled=True):
                raise RuntimeError("Exportación cancelada")
            cmds.currentTime(f, edit=True)
            om.MGlobal.viewFrame(f)
            p, n_ = _static_attrs(mesh, rows, o["hard"], space, C, scale)
            pos[:, i, :3] = p
            nrm[:, i, :3] = n_
            if tan is not None:
                tans = np.array(mesh.getTangents(space), dtype=np.float32)[tid]
                bins = np.array(mesh.getBinormals(space), dtype=np.float32)[tid]
                if o["hard"]:
                    nn = np.array(mesh.getNormals(space), dtype=np.float32)[np.asarray(rows["nid"])]
                else:
                    nn = np.array(mesh.getVertexNormals(True, space), dtype=np.float32)[rv]
                w = np.sign(np.einsum("ij,ij->i", np.cross(nn, tans), bins))
                w[w == 0] = 1.0
                tan[:, i, :3] = tans @ C.T
                tan[:, i, 3] = w * det
            if col is not None:
                carr = np.array([[c.r, c.g, c.b, c.a] for c in mesh.getVertexColors()], dtype=np.float32)
                col[:, i, :] = carr[rv]
            cmds.progressWindow(e=True, step=1)
    finally:
        cmds.progressWindow(endProgress=True)

    base = pos[:, 0, :3].copy()          # pose base (frame inicial, ya convertida)
    if o["space"] == "offset":
        pos[:, :, :3] -= base[:, None, :]
    return pos, nrm, tan, col, base


def sample_rigid(pieces, frames, C, scale, det):
    n, nf = len(pieces), len(frames)
    pos = np.zeros((n, nf, 4), np.float32)     # xyz = traslación, w = escala uniforme
    rot = np.zeros((n, nf, 4), np.float32)     # cuaternión xyzw
    for i, f in enumerate(frames):
        cmds.currentTime(f, edit=True)
        for p, name in enumerate(pieces):
            m = om.MMatrix(cmds.xform(name, q=True, ws=True, matrix=True))
            tm = om.MTransformationMatrix(m)
            t = np.array(tm.translation(om.MSpace.kWorld), dtype=np.float32)
            q = tm.rotation(asQuaternion=True)
            s = tm.scale(om.MSpace.kWorld)
            v, w = convert_quat([q.x, q.y, q.z], q.w, C)
            pos[p, i, :3] = (C @ t) * scale
            pos[p, i, 3] = (s[0] + s[1] + s[2]) / 3.0
            rot[p, i, :3] = v
            rot[p, i, 3] = w
    return pos, rot


def _write_textures(out, base, name, arr, o, files):
    """arr: (alto, ancho, 4) float32"""
    if o["bin"]:
        p = os.path.join(out, "%s_%s.bin" % (base, name))
        arr.astype(np.float16 if o["half"] else np.float32).tofile(p)
        files.setdefault(name, {})["bin"] = os.path.basename(p)
    if o["exr"]:
        p = os.path.join(out, "%s_%s.exr" % (base, name))
        write_exr(p, arr[::-1], half=o["half"])
        files.setdefault(name, {})["exr"] = os.path.basename(p)


def _layout(rows_n, frames_n, max_tex):
    tile_h = min(rows_n, max_tex)
    tiles = int(math.ceil(rows_n / float(tile_h)))
    if tiles * frames_n > max_tex:
        cmds.warning("Textura de %d px de ancho supera max_tex=%d: reduce frames, sube step o max_tex."
                     % (tiles * frames_n, max_tex))
    return tile_h, tiles


# ----------------------------------------------------------------------------
# FBX con UV2 de lookup
# ----------------------------------------------------------------------------
def _assign_uv2(mesh_node, counts, flat_rows, u_rows, v_rows):
    cmds.polyUVSet(mesh_node, create=True, uvSet="vatUV")
    cmds.polyUVSet(mesh_node, currentUVSet=True, uvSet="vatUV")
    _, m = _dag_and_mesh(mesh_node)
    m.setUVs(list(map(float, u_rows)), list(map(float, v_rows)), "vatUV")
    m.assignUVs(list(counts), list(flat_rows), "vatUV")


def _prep_duplicate(node, name, world_freeze):
    dup = cmds.duplicate(node, name=name)[0]
    if cmds.listRelatives(dup, parent=True):
        dup = cmds.parent(dup, world=True)[0]
    cmds.delete(dup, constructionHistory=True)
    if world_freeze:
        cmds.makeIdentity(dup, apply=True, t=1, r=1, s=1, n=0)
    else:
        cmds.xform(dup, matrix=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
    return dup


def _export_fbx(path, node):
    cmds.loadPlugin("fbxmaya", quiet=True)
    cmds.select(node, r=True)
    cmds.file(path, force=True, options="v=0;", type="FBX export", exportSelected=True)


# ----------------------------------------------------------------------------
# Exportación principal
# ----------------------------------------------------------------------------
def export_vat(out="C:/MayaExports/VertexData", base="my_mesh", target="Web (Three.js)",
               mode="soft", space="world", half=False, bin=None, exr=None, fbx=None,
               hard=False, tangents=False, colors=False, start=None, end=None, step=1,
               max_tex=4096, scale=None, json_mesh=None):
    """
    mode:   "soft" (una malla) | "rigid" (varias piezas seleccionadas)
    space:  "world" | "object" | "offset" (mundo relativo al primer frame; solo soft)
    target: clave de TARGETS ("Web (Three.js)", "Unity", "Unreal", "Genérico")
    """
    preset = TARGETS[target]
    o = dict(out=out, base=base, target=target, mode=mode, space=space, half=half,
             bin=preset["bin"] if bin is None else bin,
             exr=preset["exr"] if exr is None else exr,
             fbx=preset["fbx"] if fbx is None else fbx,
             hard=hard, tangents=tangents, colors=colors, step=max(1, int(step)),
             max_tex=int(max_tex),
             json_mesh=preset["json_mesh"] if json_mesh is None else json_mesh)
    scale = preset["scale"] if scale is None else scale
    C = preset["C"]
    det = float(np.linalg.det(C))

    sel = cmds.ls(sl=True, long=True)
    if not sel:
        cmds.error("Selecciona la malla primero.")
    os.makedirs(out, exist_ok=True)

    f0 = int(cmds.playbackOptions(q=True, min=True)) if start is None else int(start)
    f1 = int(cmds.playbackOptions(q=True, max=True)) if end is None else int(end)
    frames = list(range(f0, f1 + 1, o["step"]))
    nf = len(frames)
    fps = om.MTime(1, om.MTime.kSeconds).asUnits(om.MTime.uiUnit()) / o["step"]

    old_time = cmds.currentTime(q=True)
    prev_mode = cmds.evaluationManager(q=True, mode=True)
    cmds.evaluationManager(mode="off")
    try:
        if mode == "rigid":
            meta = _export_rigid(sel, frames, fps, o, C, scale, det)
        else:
            meta = _export_soft(sel[0], frames, fps, o, C, scale, det)
    finally:
        cmds.evaluationManager(mode=prev_mode[0] if prev_mode else "parallel")
        cmds.currentTime(old_time, edit=True)
        try:
            cmds.select(sel, r=True)
        except Exception:
            pass

    meta.update({
        "version": 2, "tool": "vat_exporter", "mode": mode, "target": preset["key"],
        "space": space, "position_mode": "offset" if space == "offset" else "absolute",
        "frames": nf, "fps": fps, "unit_scale": scale, "up_axis": preset["up"],
        "dtype": "float16" if half else "float32",
        "flip_winding": det < 0,
    })
    with open(os.path.join(out, "%s_vat.json" % base), "w") as fh:
        json.dump(meta, fh)
    print("OK: %s -> %s | %d frames" % (mode, out, nf))
    return meta


def _export_soft(node, frames, fps, o, C, scale, det):
    out, base = o["out"], o["base"]
    dag, mesh = _dag_and_mesh(node)
    cmds.currentTime(frames[0], edit=True)
    rows = build_rows(dag, mesh, o["hard"])
    n, nf = len(rows["vert"]), len(frames)

    pos, nrm, tan, col, bind_pos = sample_soft(mesh, rows, frames, o, C, scale, det)

    mov = (pos[:, :, :3].max(axis=1) - pos[:, :, :3].min(axis=1)).max()
    print("Movimiento máx de un vértice: %s" % mov)
    if mov < 1e-6:
        cmds.warning("Los vértices no se mueven en ningún frame. Revisa dónde está la animación.")

    tile_h, tiles = _layout(n, nf, o["max_tex"])
    files = {}
    _write_textures(out, base, "pos", pack_tiles(pos, tile_h, tiles), o, files)
    _write_textures(out, base, "nrm", pack_tiles(nrm, tile_h, tiles), o, files)
    if tan is not None:
        _write_textures(out, base, "tan", pack_tiles(tan, tile_h, tiles), o, files)
    if col is not None:
        _write_textures(out, base, "col", pack_tiles(col, tile_h, tiles), o, files)

    meta = {
        "vertices": n, "tile_height": tile_h, "tiles": tiles,
        "texture": [tiles * nf, tile_h], "files": files,
        "bbox_min": pos[:, :, :3].reshape(-1, 3).min(axis=0).tolist(),
        "bbox_max": pos[:, :, :3].reshape(-1, 3).max(axis=0).tolist(),
        "uv2": {"u": "tile / tiles", "v": "(row + 0.5) / tile_height"},
    }

    if o["json_mesh"]:
        cmds.currentTime(frames[0], edit=True)
        idx = list(rows["index"])
        if det < 0:      # la conversión invierte la mano: hay que voltear el winding
            idx = [x for t in range(0, len(idx), 3) for x in (idx[t], idx[t + 2], idx[t + 1])]
        meta["vid"] = list(range(n))       # compatibilidad con el visor: fila = vértice
        meta["position"] = bind_pos.reshape(-1).tolist()   # pose base (necesaria en modo offset)
        meta["uv"] = rows["uv"]
        meta["index"] = idx

    if o["fbx"]:
        cmds.currentTime(frames[0], edit=True)
        dup = _prep_duplicate(node, base + "_vatmesh", world_freeze=(o["space"] != "object"))
        # UV2 por vértice de exportación
        rr = np.arange(n)
        u_rows = (rr // tile_h) / float(tiles)
        v_rows = ((rr % tile_h) + 0.5) / float(tile_h)
        flat = [r for fv in rows["fv_rows"] for r in fv]
        _assign_uv2(dup, rows["counts"], flat, u_rows, v_rows)
        path = os.path.join(out, "%s_vat.fbx" % base)
        _export_fbx(path, dup)
        cmds.delete(dup)
        files["fbx"] = os.path.basename(path)
    return meta


def _export_rigid(sel, frames, fps, o, C, scale, det):
    out, base = o["out"], o["base"]
    pieces = sel
    n, nf = len(pieces), len(frames)

    pos, rot = sample_rigid(pieces, frames, C, scale, det)
    tile_h, tiles = n, 1
    files = {}
    _write_textures(out, base, "pos", pos, o, files)
    _write_textures(out, base, "rot", rot, o, files)

    # Geometría local (espacio objeto) de todas las piezas, con id de pieza por vértice
    cmds.currentTime(frames[0], edit=True)
    gp, gn, guv, gpiece, gidx = [], [], [], [], []
    offset = 0
    all_rows = []
    for pi, name in enumerate(pieces):
        dag, mesh = _dag_and_mesh(name)
        rows = build_rows(dag, mesh, o["hard"])
        p, nn = _static_attrs(mesh, rows, o["hard"], om.MSpace.kObject, C, scale)
        gp.append(p)
        gn.append(nn)
        guv += rows["uv"]
        gpiece += [pi] * len(rows["vert"])
        idx = [offset + x for x in rows["index"]]
        if det < 0:
            idx = [x for t in range(0, len(idx), 3) for x in (idx[t], idx[t + 2], idx[t + 1])]
        gidx += idx
        offset += len(rows["vert"])
        all_rows.append(rows)

    meta = {
        "pieces": [p.split("|")[-1] for p in pieces],
        "vertices": n, "tile_height": tile_h, "tiles": tiles,
        "texture": [nf, n], "files": files,
        "pos_channels": "xyz = traslación, w = escala uniforme", "rot_channels": "cuaternión xyzw",
        "bbox_min": pos[:, :, :3].reshape(-1, 3).min(axis=0).tolist(),
        "bbox_max": pos[:, :, :3].reshape(-1, 3).max(axis=0).tolist(),
        "uv2": {"u": "0", "v": "(pieza + 0.5) / n_piezas"},
    }
    if o["json_mesh"]:
        meta["mesh"] = {
            "position": np.concatenate(gp).reshape(-1).tolist(),
            "normal": np.concatenate(gn).reshape(-1).tolist(),
            "uv": guv, "piece": gpiece, "index": gidx,
        }

    if o["fbx"]:
        cmds.currentTime(frames[0], edit=True)
        dups = []
        for pi, name in enumerate(pieces):
            d = _prep_duplicate(name, "%s_p%d" % (base, pi), world_freeze=False)
            rows = all_rows[pi]
            vv = (pi + 0.5) / float(n)
            _assign_uv2(d, rows["counts"], [0] * sum(rows["counts"]), [0.0], [vv])
            dups.append(d)
        merged = cmds.polyUnite(dups, name=base + "_vatmesh", ch=False)[0] if len(dups) > 1 else dups[0]
        path = os.path.join(out, "%s_vat.fbx" % base)
        _export_fbx(path, merged)
        cmds.delete(merged)
        files["fbx"] = os.path.basename(path)
    return meta



# ----------------------------------------------------------------------------
# Test de ejes (para verificar Unity / Unreal con tu versión del motor)
# ----------------------------------------------------------------------------
def axis_test(out="C:/MayaExports/VertexData", base="axis_test", target="Unity"):
    """
    Crea un objeto asimétrico animado (traslación + rotación), exporta su VAT en modo
    Mundo (absoluto) y un FBX de referencia estático en el frame 15.

    En el motor: pon la malla VAT (material VAT, _OffsetMode = 0) en el frame 15
    y colócala junto al FBX de referencia (mismo origen). Deben SUPERPONERSE.
    Si salen espejadas, rotadas o desplazadas, los ejes de ese motor no coinciden.
    """
    cmds.playbackOptions(min=1, max=30)
    long_ = cmds.polyCube(w=4, h=1, d=1, name="axt_long")[0]
    tall = cmds.polyCube(w=1, h=2.5, d=1, name="axt_tall")[0]
    cmds.move(2, 1.75, 0, tall)        # sube desde el extremo +X
    small = cmds.polyCube(w=1, h=1, d=2, name="axt_small")[0]
    cmds.move(-2, 0, 1.5, small)       # extremo -X, saliendo hacia +Z
    obj = cmds.polyUnite(long_, tall, small, name="axis_test_obj", ch=False)[0]
    cmds.delete(obj, constructionHistory=True)
    for at, v0, v1 in (("tx", 0, 10), ("ty", 0, 5), ("tz", 0, -3),
                       ("rx", 0, 20), ("ry", 0, 90), ("rz", 0, 30)):
        cmds.setKeyframe(obj, attribute=at, time=1, value=v0)
        cmds.setKeyframe(obj, attribute=at, time=30, value=v1)

    cmds.select(obj, r=True)
    export_vat(out=out, base=base, target=target, space="world", start=1, end=30,
               fbx=True, bin=False, exr=True)

    cmds.currentTime(15, edit=True)
    ref = cmds.duplicate(obj, name=base + "_ref")[0]
    cmds.delete(ref, constructionHistory=True)
    _export_fbx(os.path.join(out, base + "_ref.fbx"), ref)
    cmds.delete(ref)
    cmds.select(obj, r=True)
    print("Test de ejes listo en %s: %s_vat.fbx (malla VAT) y %s_ref.fbx (referencia frame 15)." % (out, base, base))


# ----------------------------------------------------------------------------
# Abrir directo en el visor web (sin copiar archivos a mano)
# ----------------------------------------------------------------------------
_SERVERS = {}   # carpeta -> (proceso, puerto), para no abrir un servidor por cada export


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ensure_server(web_dir):
    """Levanta (o reutiliza) un servidor HTTP simple sobre web_dir, en segundo plano."""
    web_dir = os.path.abspath(web_dir)
    proc_port = _SERVERS.get(web_dir)
    if proc_port and proc_port[0].poll() is None:
        return proc_port[1]
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port)],
        cwd=web_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _SERVERS[web_dir] = (proc, port)
    time.sleep(0.6)  # darle un instante a arrancar antes de abrir el navegador
    return port


def export_and_open_web(web_dir, base="my_mesh", open_browser=True, **export_kwargs):
    """
    Exporta con destino Web directo a web_dir (donde ya deben estar index.html y
    vat.js del paquete) y, si open_browser=True, levanta un servidor local y abre
    el navegador apuntando al resultado — sin copiar archivos a mano.

    web_dir: carpeta del visor (la que tiene index.html / vat.js).
    Resto de kwargs se pasan tal cual a export_vat() (mode, space, frames, etc.).
    """
    if not os.path.isfile(os.path.join(web_dir, "index.html")):
        cmds.warning(
            "No encuentro index.html en %s. Elige la carpeta donde están index.html y vat.js "
            "del paquete web (no una carpeta vacía)." % web_dir
        )
    export_kwargs.pop("target", None)
    export_kwargs.setdefault("bin", True)
    export_kwargs.setdefault("exr", False)
    export_kwargs.setdefault("fbx", False)
    export_vat(out=web_dir, base=base, target="Web (Three.js)", **export_kwargs)

    if open_browser:
        try:
            port = _ensure_server(web_dir)
            url = "http://localhost:%d/index.html?base=./%s&v=%d" % (port, base, int(time.time()))
            webbrowser.open(url)
            print("Visor abierto en: %s" % url)
        except Exception as e:
            cmds.warning(
                "No se pudo abrir el navegador automáticamente (%s). "
                "Sirve la carpeta manualmente (python -m http.server) y abre index.html." % e
            )


# ----------------------------------------------------------------------------
# Interfaz
# ----------------------------------------------------------------------------
def _ui_apply_preset(*_):
    t = TARGETS[cmds.optionMenuGrp("vatTarget", q=True, value=True)]
    cmds.checkBox("vatBin", e=True, value=t["bin"])
    cmds.checkBox("vatExr", e=True, value=t["exr"])
    cmds.checkBox("vatFbx", e=True, value=t["fbx"])
    cmds.checkBox("vatJsonMesh", e=True, value=t["json_mesh"])
    cmds.floatFieldGrp("vatScale", e=True, value1=t["scale"])


def _ui_browse(*_):
    r = cmds.fileDialog2(dialogStyle=2, fileMode=3, caption="Carpeta de salida")
    if r:
        cmds.textFieldButtonGrp("vatOut", e=True, text=r[0])


def _ui_export(*_):
    space = {"Mundo": "world", "Objeto": "object", "Offset (mundo - frame inicial)": "offset"}[
        cmds.optionMenuGrp("vatSpace", q=True, value=True)]
    mode = "rigid" if cmds.optionMenuGrp("vatMode", q=True, value=True).startswith("Rigid") else "soft"
    base = cmds.textFieldGrp("vatBase", q=True, text=True)
    common = dict(
        mode=mode, space=space,
        half=cmds.optionMenuGrp("vatPrec", q=True, value=True).startswith("Float16"),
        bin=cmds.checkBox("vatBin", q=True, value=True),
        exr=cmds.checkBox("vatExr", q=True, value=True),
        fbx=cmds.checkBox("vatFbx", q=True, value=True),
        json_mesh=cmds.checkBox("vatJsonMesh", q=True, value=True),
        hard=cmds.checkBox("vatHard", q=True, value=True),
        tangents=cmds.checkBox("vatTan", q=True, value=True),
        colors=cmds.checkBox("vatCol", q=True, value=True),
        start=cmds.intFieldGrp("vatRange", q=True, value1=True),
        end=cmds.intFieldGrp("vatRange", q=True, value2=True),
        step=cmds.intFieldGrp("vatStep", q=True, value1=True),
        max_tex=cmds.intFieldGrp("vatMaxTex", q=True, value1=True),
        scale=cmds.floatFieldGrp("vatScale", q=True, value1=True),
    )
    export_vat(
        out=cmds.textFieldButtonGrp("vatOut", q=True, text=True),
        base=base, target=cmds.optionMenuGrp("vatTarget", q=True, value=True),
        **common
    )
    if cmds.checkBox("vatOpenWeb", q=True, value=True):
        web_dir = cmds.textFieldButtonGrp("vatWebDir", q=True, text=True)
        if not web_dir:
            cmds.warning("Elige la carpeta del visor web (donde está index.html) para poder abrirlo.")
        else:
            export_and_open_web(web_dir, base=base, open_browser=True, **common)


# ----------------------------------------------------------------------------
# Ayudas (botones "?")
# ----------------------------------------------------------------------------
HELP = {
    "target": ("Destino",
        "¿A dónde vas a llevar la animación?\n\n"
        "Imagina que la animación es una canasta de manzanas y este es el lugar donde la vas a servir:\n"
        "  - Web (Three.js): una página web.\n"
        "  - Unity / Unreal: un videojuego.\n"
        "  - Genérico: te da de todo, por si aún no decides.\n\n"
        "Al cambiarlo, los pasos siguientes se rellenan solos."),
    "mode": ("Tipo de animación",
        "¿Cómo se comporta lo que animas?\n\n"
        "  - Soft body: UNA manzana que se aplasta, se estira o se dobla. Cada punto de su piel se mueve distinto.\n"
        "  - Rigid body: VARIAS manzanas (piezas) que ruedan y giran, pero ninguna se deforma. "
        "Selecciona todas las piezas antes de exportar."),
    "space": ("Espacio",
        "¿Qué quieres guardar del movimiento?\n\n"
        "Imagina una manzana que rueda por una mesa mientras se aplasta:\n"
        "  - Mundo: guardas DÓNDE está en la mesa en cada momento (rodar + aplastarse). "
        "Ideal para un objeto único.\n"
        "  - Objeto: guardas SOLO cómo se aplasta, sin importar dónde está. "
        "Ideal para copiar la manzana muchas veces en sitios distintos.\n"
        "  - Offset: guardas CUÁNTO se movió cada punto respecto a donde empezó. "
        "Ideal para motores y para poder usar Float16."),
    "prec": ("Precisión",
        "¿Qué tan fino medimos cada punto?\n\n"
        "Imagina que mides una manzana:\n"
        "  - Float32: con regla al milímetro. Exacto, pero pesa el doble.\n"
        "  - Float16: con regla en centímetros. Pesa la mitad, pero puede 'temblar' si la manzana "
        "está lejos del centro. Úsalo solo con Offset."),
    "bin": (".bin crudo",
        "Archivo para la WEB.\n\n"
        "Es la 'foto' de la animación en formato crudo, lista para Three.js. "
        "Si vas a una página web, déjalo marcado. Los motores de videojuegos no lo leen."),
    "exr": (".exr",
        "Archivo para UNITY y UNREAL.\n\n"
        "Es lo mismo que el .bin, pero como imagen que los motores sí entienden. "
        "Si vas a un videojuego, márcalo."),
    "fbx": ("FBX con UV2",
        "La malla para el videojuego.\n\n"
        "Imagina que a cada manzana le pegas una etiqueta con su número de fila: 'tú eres la manzana 37'. "
        "El motor lee esa etiqueta (el UV2) para saber qué parte de la animación te toca.\n\n"
        "Necesario para Unity y Unreal."),
    "jsonmesh": ("Malla dentro del JSON",
        "El JSON trae además el 'molde' de la manzana (sus triángulos y UVs), "
        "para que la web pueda dibujarla sin otro archivo.\n\n"
        "Déjalo marcado para web. En motores la malla ya viene en el FBX."),
    "hard": ("Bordes duros",
        "Una manzana es redonda y suave. Un cubo de queso tiene esquinas filosas.\n\n"
        "Si tu modelo tiene esquinas filosas (cajas, piezas mecánicas), márcalo para que la luz las muestre nítidas. "
        "Con modelos redondos déjalo apagado: solo agregaría peso."),
    "tan": ("Tangentes",
        "Si vas a 'pintar' detalles finos sobre la manzana (arruguitas, poros) con un normal map, "
        "la luz necesita saber hacia dónde 'peinar' esos detalles. Eso son las tangentes.\n\n"
        "Sin normal map, déjalo apagado."),
    "col": ("Color / alpha de vértice",
        "Imagina una manzana que pasa de verde a roja mientras la miras, o que se vuelve transparente.\n\n"
        "Esto guarda ese color en cada momento. Útil para fuego, humo o líquidos. "
        "Si tu modelo no cambia de color, déjalo apagado."),
    "range": ("Frames (inicio / fin)",
        "Son las 'fotos' de la animación que se guardan.\n\n"
        "Por defecto usa el rango de tu línea de tiempo. Menos fotos = archivos más livianos."),
    "step": ("Paso",
        "1 = guarda todas las fotos. 2 = una sí, una no. 3 = una de cada tres.\n\n"
        "Sube el paso si los archivos pesan mucho: la animación sigue viéndose fluida "
        "porque el visor mezcla las fotos entre sí."),
    "maxtex": ("Tamaño máximo de textura",
        "La animación se guarda en una bandeja de manzanas. Si hay demasiadas manzanas para una bandeja, "
        "se reparten en varias bandejas (tiles).\n\n"
        "4096 sirve en casi todos los teléfonos y PCs. No lo cambies si no sabes."),
    "scale": ("Escala de unidades",
        "Maya mide en centímetros; Unity, en metros. Es como pasar manzanas de una báscula a otra.\n\n"
        "Web: 1  |  Unity: 0.01  |  Unreal: 1.\n"
        "Se pone solo al elegir el destino."),
    "out": ("Carpeta y nombre",
        "Aquí caen los archivos exportados. El nombre base es el prefijo de todos ellos.\n\n"
        "Ejemplo: 'mi_manzana' genera mi_manzana_pos.bin, mi_manzana_nrm.bin, mi_manzana_vat.json..."),
    "axis": ("Test de ejes",
        "Crea una pieza asimétrica (como una manzana con una hoja torcida a un lado) que se mueve y gira, "
        "para comprobar en Unity o Unreal que NO aparece espejada ni volteada.\n\n"
        "Úsalo una vez por motor. Detalles en el README."),
    "webdir": ("Abrir en visor web",
        "Es un atajo para no copiar archivos a mano.\n\n"
        "Señala la carpeta donde ya están index.html y vat.js (la carpeta 'web' del paquete, "
        "o la tuya si la integraste a tu proyecto). Al exportar, además de generar los archivos "
        "ahí mismo, se abre tu navegador ya apuntando a la animación recién exportada.\n\n"
        "Si la carpeta no tiene index.html, se avisa y no hace nada raro."),
}


def _help(key):
    title, text = HELP[key]
    cmds.button(label="?", width=22, height=22, backgroundColor=(0.30, 0.42, 0.60),
                annotation=title,
                command=lambda *_: cmds.confirmDialog(title=title, message=text,
                                                      button=["Entendido"], defaultButton="Entendido"))


def _row():
    cmds.rowLayout(numberOfColumns=2, adjustableColumn=1, columnAttach=[(2, "left", 4)])


def _row_end(key):
    _help(key)
    cmds.setParent("..")


def _step(n, title, subtitle):
    cmds.separator(height=12, style="in")
    cmds.text(label="  PASO %d   |   %s" % (n, title), align="left", font="boldLabelFont",
              height=24, enableBackground=True, backgroundColor=(0.20, 0.28, 0.38))
    cmds.text(label="  " + subtitle, align="left", font="smallObliqueLabelFont", height=18)


def show_ui():
    if cmds.window(WIN, exists=True):
        cmds.deleteUI(WIN)
    cmds.window(WIN, title="VAT Exporter", widthHeight=(480, 860))
    cmds.scrollLayout(childResizable=True)
    cmds.columnLayout(adjustableColumn=True, rowSpacing=3, columnOffset=("both", 8))

    cmds.text(label="Selecciona tu malla en Maya, sigue los pasos 1 a 7 y pulsa EXPORTAR.\n"
                    "Pulsa [?] en cualquier opción para ver qué hace (con manzanas).",
              align="left", height=40)

    # ---------------- PASO 1 ----------------
    _step(1, "¿A dónde va?", "Elige el destino; los pasos siguientes se ajustan solos.")
    _row()
    cmds.optionMenuGrp("vatTarget", label="Destino", changeCommand=_ui_apply_preset, adjustableColumn=2)
    for t in TARGETS:
        cmds.menuItem(label=t)
    _row_end("target")

    # ---------------- PASO 2 ----------------
    _step(2, "¿Qué se mueve?", "Una malla que se deforma, o varias piezas rígidas.")
    _row()
    cmds.optionMenuGrp("vatMode", label="Tipo", adjustableColumn=2)
    cmds.menuItem(label="Soft body (una malla deformable)")
    cmds.menuItem(label="Rigid body (varias piezas seleccionadas)")
    _row_end("mode")

    # ---------------- PASO 3 ----------------
    _step(3, "¿Cómo se guarda el movimiento?", "Espacio y precisión de los números.")
    _row()
    cmds.optionMenuGrp("vatSpace", label="Espacio", adjustableColumn=2)
    cmds.menuItem(label="Mundo")
    cmds.menuItem(label="Objeto")
    cmds.menuItem(label="Offset (mundo - frame inicial)")
    _row_end("space")
    _row()
    cmds.optionMenuGrp("vatPrec", label="Precisión", adjustableColumn=2)
    cmds.menuItem(label="Float32 (recomendado)")
    cmds.menuItem(label="Float16 (solo con Offset)")
    _row_end("prec")

    # ---------------- PASO 4 ----------------
    _step(4, "¿Qué archivos necesitas?", "Web usa .bin. Unity y Unreal usan .exr y FBX.")
    _row(); cmds.checkBox("vatBin", label=".bin crudo (Web / Three.js)", value=True); _row_end("bin")
    _row(); cmds.checkBox("vatExr", label=".exr (Unity / Unreal)", value=False); _row_end("exr")
    _row(); cmds.checkBox("vatFbx", label="FBX de la malla con UV2 de lookup", value=False); _row_end("fbx")
    _row(); cmds.checkBox("vatJsonMesh", label="Incluir malla en el JSON", value=True); _row_end("jsonmesh")

    # ---------------- PASO 5 ----------------
    _step(5, "Extras (opcional)", "Solo si tu modelo los necesita.")
    _row(); cmds.checkBox("vatHard", label="Respetar bordes duros (normales por cara)", value=False); _row_end("hard")
    _row(); cmds.checkBox("vatTan", label="Exportar tangentes (para normal maps)", value=False); _row_end("tan")
    _row(); cmds.checkBox("vatCol", label="Exportar color / alpha de vértice", value=False); _row_end("col")

    # ---------------- PASO 6 ----------------
    _step(6, "¿Cuánta animación?", "Rango de frames y límites de tamaño.")
    _row()
    cmds.intFieldGrp("vatRange", label="Frames (ini / fin)", numberOfFields=2,
                     value1=int(cmds.playbackOptions(q=True, min=True)),
                     value2=int(cmds.playbackOptions(q=True, max=True)))
    _row_end("range")
    _row(); cmds.intFieldGrp("vatStep", label="Paso (1 = todos)", numberOfFields=1, value1=1); _row_end("step")
    _row(); cmds.intFieldGrp("vatMaxTex", label="Tamaño máx textura", numberOfFields=1, value1=4096); _row_end("maxtex")
    _row(); cmds.floatFieldGrp("vatScale", label="Escala unidades", numberOfFields=1, value1=1.0); _row_end("scale")

    # ---------------- PASO 7 ----------------
    _step(7, "¿Dónde se guarda?", "Carpeta de salida y nombre de los archivos.")
    _row()
    cmds.textFieldButtonGrp("vatOut", label="Carpeta", text="C:/MayaExports/VertexData",
                            buttonLabel="...", buttonCommand=_ui_browse, adjustableColumn=2)
    _row_end("out")
    cmds.textFieldGrp("vatBase", label="Nombre base", text="my_mesh", adjustableColumn=2)
    cmds.separator(height=6, style="none")
    _row()
    cmds.checkBox("vatOpenWeb", label="Abrir en visor web tras exportar", value=False,
                  changeCommand=lambda v: cmds.textFieldButtonGrp("vatWebDir", e=True, enable=v))
    _row_end("webdir")
    cmds.textFieldButtonGrp("vatWebDir", label="Carpeta del visor (index.html)", text="",
                            buttonLabel="...", enable=False, adjustableColumn=2,
                            buttonCommand=lambda *_: cmds.textFieldButtonGrp(
                                "vatWebDir", e=True,
                                text=(cmds.fileDialog2(dialogStyle=2, fileMode=3,
                                      caption="Carpeta del visor web") or [""])[0]))

    # ---------------- EXPORTAR ----------------
    cmds.separator(height=14, style="in")
    cmds.button(label="EXPORTAR VAT", height=44, backgroundColor=(0.25, 0.5, 0.3), command=_ui_export)

    # ---------------- HERRAMIENTAS ----------------
    cmds.separator(height=14, style="in")
    cmds.text(label="  HERRAMIENTAS", align="left", font="boldLabelFont", height=22,
              enableBackground=True, backgroundColor=(0.20, 0.28, 0.38))
    _row()
    cmds.button(label="Crear test de ejes (Unity/Unreal)", height=28,
                command=lambda *_: axis_test(
                    out=cmds.textFieldButtonGrp("vatOut", q=True, text=True),
                    target=cmds.optionMenuGrp("vatTarget", q=True, value=True)))
    _row_end("axis")
    cmds.separator(height=10, style="none")

    cmds.showWindow(WIN)


if __name__ == "__main__":
    show_ui()

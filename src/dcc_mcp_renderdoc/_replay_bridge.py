"""RenderDoc bundled-Python bridge for deep replay queries.

The adapter runs this module with ``qrenderdoc --python``. It reads one
operation name and one JSON parameter document from the environment, replays a
capture through RenderDoc's official replay API, and writes a JSON status file
that the adapter host reads back.

The module targets the Python runtime bundled with RenderDoc, so it stays free
of syntax and standard-library features newer than that runtime, and it must
never raise at import time.
"""

import base64
import binascii
import json
import os
import re
import struct
import sys
import zlib

SCHEMA_VERSION = 1

MAX_DEPTH = 4
MAX_ITEMS = 256
MAX_PREVIEW_BYTES = 4096
MAX_TEXT_CHARS = 20000
MAX_VERTICES = 4096
MAX_INDICES = 65536
#: Hard ceiling on how far one shader debug trace is stepped before it is
#: reported as truncated, so a pathological shader cannot spin forever.
MAX_DEBUG_STEPS = 20000
#: Ceilings for the region sampling and pixel diagnostics analysis ops: grid
#: points per axis and per request, texel bytes one readback may cover, texels
#: one diagnosis may scan, and anomaly coordinates reported per check.
MAX_REGION_GRID = 64
MAX_REGION_SAMPLES = 4096
MAX_REGION_BYTES = 256 * 1024 * 1024
MAX_DIAGNOSE_TEXELS = 2000000
MAX_ANOMALY_SAMPLES = 256
#: Magnitude band a float texel has to stay inside to count as well
#: conditioned. Below it the value is denormal-adjacent; above it any further
#: maths on the value has lost most of its precision.
FLOAT_TINY = 1e-20
FLOAT_HUGE = 1e20
#: Component types whose bytes decode to a number without a format table.
#: Everything else -- block-compressed, packed, YUV, and the special formats --
#: is reported as undecodable instead of sampled into a wrong number.
_DECODABLE_COMP_TYPES = (
    "CompType.Float",
    "CompType.UNorm",
    "CompType.UNormSRGB",
    "CompType.SNorm",
    "CompType.SInt",
    "CompType.UInt",
)
#: Struct codes for integer components, keyed by (byte width, signed).
_INT_CODES = {
    (1, False): "B",
    (1, True): "b",
    (2, False): "H",
    (2, True): "h",
    (4, False): "I",
    (4, True): "i",
    (8, False): "Q",
    (8, True): "q",
}
#: Struct codes for float components, keyed by byte width.
_FLOAT_CODES = {2: "e", 4: "f", 8: "d"}
#: The four anomaly checks ``diagnose_pixel_values`` can run, in report order.
_PIXEL_CHECKS = ("nan", "inf", "negative", "precision")

_SHADER_STAGES = ("Vertex", "Hull", "Domain", "Geometry", "Pixel", "Compute")
_MESH_STAGES = ("VSIn", "VSOut", "GSOut", "TaskOut", "MeshOut")
_SWIG_MEMBERS = frozenset(["acquire", "append", "disown", "next", "own", "this", "thisown"])
_ENUM_TEXT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")
_TYPED_ID_TEXT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*::[0-9]+$")
_SAVE_FORMATS = {
    ".bmp": "BMP",
    ".dds": "DDS",
    ".exr": "EXR",
    ".hdr": "HDR",
    ".jpg": "JPG",
    ".png": "PNG",
    ".raw": "Raw",
    ".tga": "TGA",
}
#: Post-VS mesh exports this bridge can write; the extension picks the format.
_MESH_EXPORT_FORMATS = {".json": "json", ".obj": "obj"}
#: Overdraw exports this bridge can write; the extension picks the format.
_OVERDRAW_EXPORT_FORMATS = {".png": "png", ".ppm": "ppm"}
#: Grid the overdraw analysis falls back to when the caller names no size. A CPU
#: rasteriser costs one pass per covered triangle, so the default stays small and
#: deterministic instead of following the capture's own viewport.
_OVERDRAW_DEFAULT_WIDTH = 256
_OVERDRAW_DEFAULT_HEIGHT = 144
#: Ceilings that keep one overdraw request bounded: draws per request, triangles
#: and vertices per draw, and rasterised samples per request.
MAX_OVERDRAW_DRAWS = 512
MAX_OVERDRAW_TRIANGLES = 200000
MAX_OVERDRAW_VERTICES = 262144
MAX_OVERDRAW_SAMPLES = 4000000
#: How the overdraw figures were produced. RenderDoc's quad-overdraw overlay is a
#: GPU pass that needs a window, so headless replays cannot run it; these
#: numbers are rasterised from post-VS geometry on the CPU instead. Reported in
#: the payload so an agent never reads them as GPU-measured.
_OVERDRAW_METHOD = "cpu_rasterised_estimate"
#: Counter name fragments that carry per-event GPU timing, most specific first.
#: The duration counter has no stable ID across APIs and drivers, so the choice
#: is made by name rather than by ordinal.
_TIMING_COUNTER_MARKERS = ("gpu duration", "duration", "elapsed time", "elapsed", "time")
#: How one ``CounterResult`` union is read, keyed by the ``CompType`` RenderDoc
#: reported for that counter: ``(marker, member, narrower member, as_float)``.
_COUNTER_VALUE_MEMBERS = (
    ("Float", "f", None, True),
    ("Double", "d", None, True),
    ("SInt", "i64", "i32", False),
    ("UInt", "u64", "u32", False),
)


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value, default=""):
    try:
        return str(value)
    except BaseException:
        return default


def _clamp(value, low, high, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < low:
        return low
    if number > high:
        return high
    return number


def _enum(value):
    """Render enums as their symbolic name so hosts do not depend on ordinals."""
    text = _text(value)
    if _ENUM_TEXT.match(text):
        return text
    return text


def _describe(value, depth=0):
    """Convert RenderDoc binding values into JSON-friendly primitives."""
    if depth > MAX_DEPTH:
        return "<max-depth>"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        text = _text(value)
        return text if _ENUM_TEXT.match(text) else value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return value[:MAX_TEXT_CHARS]
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        return {
            "byte_length": len(raw),
            "preview_hex": _text(binascii.hexlify(raw[:64]).decode("ascii", "replace")),
        }
    if isinstance(value, dict):
        return {
            _text(key): _describe(item, depth + 1) for key, item in list(value.items())[:MAX_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [_describe(item, depth + 1) for item in value[:MAX_ITEMS]]
    try:
        items = list(value)
    except TypeError:
        pass
    except BaseException:
        pass
    else:
        return [_describe(item, depth + 1) for item in items[:MAX_ITEMS]]
    try:
        as_int = int(value)
    except BaseException:
        as_int = None
    text = _text(value)
    if as_int is not None and _TYPED_ID_TEXT.match(text):
        return as_int
    described = {}
    for name in dir(value):
        if name.startswith("_") or name in _SWIG_MEMBERS:
            continue
        try:
            attribute = getattr(value, name)
        except BaseException:
            continue
        if callable(attribute):
            continue
        described[name] = _describe(attribute, depth + 1)
    if not described:
        return text[:MAX_TEXT_CHARS]
    return described


def _safe_section(sections, label, getter):
    try:
        return _describe(getter())
    except BaseException as exc:
        sections.append(label)
        return {"unavailable": "{}: {}".format(type(exc).__name__, exc)}


def _stage(rd, name):
    return getattr(rd.ShaderStage, name, None)


def _set_event(controller, event_id):
    if event_id is None:
        return None
    event_id = _int(event_id)
    if event_id <= 0:
        raise ValueError("event_id must be a positive integer")
    controller.SetFrameEvent(event_id, True)
    return event_id


def _find_action(actions, event_id):
    for action in actions:
        if _int(action.eventId) == event_id:
            return action
        found = _find_action(action.children, event_id)
        if found is not None:
            return found
    return None


def _action_flags(rd, flags):
    enum = getattr(rd, "ActionFlags", None)
    if enum is None:
        return []
    value = _int(flags)
    names = []
    for name in dir(enum):
        if name.startswith("_") or name == "Count":
            continue
        bit = _int(getattr(enum, name, 0), 0)
        if bit > 0 and value & bit == bit:
            names.append(name)
    return sorted(names)


def _action_summary(controller, rd, action, depth, parent_event_id):
    structured = None
    try:
        structured = controller.GetStructuredFile()
    except BaseException:
        structured = None
    name = ""
    try:
        name = str(action.GetName(structured)) if structured is not None else str(action.customName)
    except BaseException:
        name = ""
    return {
        "event_id": _int(action.eventId),
        "action_id": _int(action.actionId),
        "parent_event_id": parent_event_id,
        "depth": depth,
        "name": name,
        "flags": _int(action.flags),
        "flag_names": _action_flags(rd, action.flags),
        "num_indices": _int(action.numIndices),
        "num_instances": _int(action.numInstances),
        "child_count": len(action.children),
    }


def _walk_actions(controller, rd, actions, depth, max_depth, parent_event_id, visit):
    """Walk the action tree in pre-order and hand every visited node to `visit`.

    The walk is bounded by `max_depth` only: callers decide how many nodes they keep,
    so filters can never hide matches that live deeper in the tree.
    """
    for action in actions:
        summary = _action_summary(controller, rd, action, depth, parent_event_id)
        visit(summary)
        if depth + 1 <= max_depth:
            _walk_actions(
                controller,
                rd,
                action.children,
                depth + 1,
                max_depth,
                summary["event_id"],
                visit,
            )


def _subresource(rd, params):
    sub = rd.Subresource()
    sub.mip = _clamp(params.get("mip"), 0, 64, 0)
    sub.slice = _clamp(params.get("slice"), 0, 65535, 0)
    sub.sample = _clamp(params.get("sample"), 0, 65535, 0)
    return sub


def _comp_type(rd, params):
    name = _text(params.get("type_cast", "") or "Typeless").strip() or "Typeless"
    return getattr(rd.CompType, name, rd.CompType.Typeless)


def _lookup_resource_id(controller, value):
    """Resolve a caller-supplied resource ID to the native ResourceId object.

    RenderDoc's ``ResourceId`` cannot be constructed from an integer, so the
    bridge looks the ID up among the capture's textures, buffers, and resources.
    """
    wanted = _int(value)
    if wanted <= 0:
        raise ValueError("resource_id must be a positive integer")
    for group in (controller.GetTextures(), controller.GetBuffers(), controller.GetResources()):
        for resource in group:
            if _int(getattr(resource, "resourceId", 0)) == wanted:
                return resource.resourceId
    raise ValueError("resource {} is not present in this capture".format(wanted))


def _replay_capabilities(controller):
    """Report which deep replay features this capture's replay actually supports."""
    try:
        properties = controller.GetAPIProperties()
    except BaseException:
        return {"available": False}
    local = bool(getattr(properties, "localRenderer", False))
    # RenderDoc exposes no post-VS-data flag on every version, so the replay mode
    # is the reliable answer: a local (non-degraded) replay can always fetch it.
    post_vs_data = getattr(properties, "postVSData", None)
    if post_vs_data is None:
        post_vs_data = local
    return {
        "available": True,
        "pixel_history": bool(getattr(properties, "pixelHistory", False)),
        "shader_debugging": bool(getattr(properties, "shaderDebugging", False)),
        "post_vs_data": bool(post_vs_data),
        "local_replay": local,
        "degraded": not local,
    }


def _op_describe_capture(controller, rd, params, context):
    del params, context
    unavailable = []
    api = _describe(controller.GetAPIProperties())
    frame = _describe(controller.GetFrameInfo())
    roots = controller.GetRootActions()
    total = 0
    stack = list(roots)
    first_event = None
    last_event = None
    while stack:
        action = stack.pop()
        total += 1
        event_id = _int(action.eventId)
        if first_event is None or event_id < first_event:
            first_event = event_id
        if last_event is None or event_id > last_event:
            last_event = event_id
        stack.extend(action.children)
    textures = controller.GetTextures()
    buffers = controller.GetBuffers()
    return {
        "capabilities": _replay_capabilities(controller),
        "api_properties": api,
        "frame_info": frame,
        "unavailable_sections": unavailable,
        "resource_count": len(controller.GetResources()),
        "texture_count": len(textures),
        "buffer_count": len(buffers),
        "root_action_count": len(roots),
        "action_count": total,
        "first_event_id": first_event,
        "last_event_id": last_event,
    }


def _op_list_actions(controller, rd, params, context):
    del context
    name_filter = _text(params.get("name_filter", "") or "").strip().casefold()
    flag_filter = _text(params.get("flag_filter", "") or "").strip().casefold()
    max_depth = _clamp(params.get("max_depth"), 0, 32, 32)
    offset = _clamp(params.get("offset"), 0, 10000000, 0)
    limit = _clamp(params.get("limit"), 1, 20000, 200)
    parent_event_id = params.get("parent_event_id")
    roots = controller.GetRootActions()
    if parent_event_id is not None:
        parent = _find_action(roots, _int(parent_event_id))
        if parent is None:
            raise ValueError("event {} was not found".format(_int(parent_event_id)))
        roots = parent.children
    keep = offset + limit
    matched = []
    counter = [0]

    def visit(summary):
        if name_filter and name_filter not in summary["name"].casefold():
            return
        if flag_filter and flag_filter not in [name.casefold() for name in summary["flag_names"]]:
            return
        counter[0] += 1
        if len(matched) < keep:
            matched.append(summary)

    _walk_actions(controller, rd, roots, 0, max_depth, None, visit)
    return {
        "parent_event_id": None if parent_event_id is None else _int(parent_event_id),
        "max_depth": max_depth,
        "offset": offset,
        "limit": limit,
        "matched_count": counter[0],
        "truncated": counter[0] > keep,
        "actions": matched[offset : offset + limit],
    }


def _op_get_action(controller, rd, params, context):
    del context
    event_id = _int(params.get("event_id"))
    action = _find_action(controller.GetRootActions(), event_id)
    if action is None:
        raise ValueError("event {} was not found".format(event_id))
    summary = _action_summary(controller, rd, action, 0, None)
    summary.update(
        {
            "custom_name": _text(action.customName),
            "index_offset": _int(action.indexOffset),
            "base_vertex": _int(action.baseVertex),
            "vertex_offset": _int(action.vertexOffset),
            "instance_offset": _int(action.instanceOffset),
            "draw_index": _int(action.drawIndex),
            "dispatch_dimension": _describe(action.dispatchDimension),
            "dispatch_threads_dimension": _describe(action.dispatchThreadsDimension),
            "dispatch_base": _describe(action.dispatchBase),
            "outputs": [_int(target) for target in action.outputs],
            "depth_out": _int(action.depthOut),
            "copy_source": _int(action.copySource),
            "copy_destination": _int(action.copyDestination),
        }
    )
    return summary


def _op_list_resources(controller, rd, params, context):
    del context
    type_filter = _text(params.get("resource_type", "") or "").strip().casefold()
    name_filter = _text(params.get("name_filter", "") or "").strip().casefold()
    offset = _clamp(params.get("offset"), 0, 10000000, 0)
    limit = _clamp(params.get("limit"), 1, 5000, 200)
    textures = {}
    for texture in controller.GetTextures():
        textures[_int(texture.resourceId)] = texture
    buffers = {}
    for buffer in controller.GetBuffers():
        buffers[_int(buffer.resourceId)] = buffer
    entries = []
    for resource in controller.GetResources():
        resource_id = _int(getattr(resource, "resourceId", 0))
        name = _text(getattr(resource, "name", ""))
        resource_type = _enum(getattr(resource, "type", ""))
        if type_filter and type_filter not in resource_type.casefold():
            continue
        if name_filter and name_filter not in name.casefold():
            continue
        entry = {
            "resource_id": resource_id,
            "name": name,
            "type": resource_type,
            "byte_size": _int(getattr(resource, "byteSize", 0)),
        }
        texture = textures.get(resource_id)
        if texture is not None:
            entry.update(
                {
                    "width": _int(texture.width),
                    "height": _int(texture.height),
                    "depth": _int(texture.depth),
                    "arraysize": _int(texture.arraysize),
                    "mips": _int(texture.mips),
                    "format": _text(texture.format.Name())
                    if hasattr(texture.format, "Name")
                    else "",
                    "dimension": _enum(getattr(texture, "dimension", "")),
                }
            )
        buffer = buffers.get(resource_id)
        if buffer is not None:
            entry.update({"length": _int(buffer.length)})
        entries.append(entry)
    entries.sort(key=lambda entry: entry["resource_id"])
    return {
        "offset": offset,
        "limit": limit,
        "matched_count": len(entries),
        "truncated": len(entries) > offset + limit,
        "resources": entries[offset : offset + limit],
    }


def _op_get_resource_usage(controller, rd, params, context):
    del context
    resource_id = _int(params.get("resource_id"))
    if resource_id <= 0:
        raise ValueError("resource_id must be a positive integer")
    offset = _clamp(params.get("offset"), 0, 10000000, 0)
    limit = _clamp(params.get("limit"), 1, 5000, 200)
    usage = controller.GetUsage(_lookup_resource_id(controller, resource_id))
    entries = []
    for item in usage[offset : offset + limit]:
        entries.append({"event_id": _int(item.eventId), "usage": _enum(getattr(item, "usage", ""))})
    return {
        "resource_id": resource_id,
        "offset": offset,
        "limit": limit,
        "usage_count": len(usage),
        "truncated": len(usage) > offset + limit,
        "usage": entries,
    }


def _op_get_pipeline_state(controller, rd, params, context):
    del context
    unavailable = []
    event_id = _set_event(controller, params.get("event_id"))
    state = controller.GetPipelineState()
    shaders = []
    read_only = {}
    read_write = {}
    constant_blocks = {}
    for name in _SHADER_STAGES:
        stage = _stage(rd, name)
        if stage is None:
            continue
        try:
            reflection = state.GetShaderReflection(stage)
        except BaseException:
            reflection = None
        entry = {
            "stage": name,
            "resource_id": None,
            "entry_point": "",
            "encoding": "",
        }
        try:
            entry["resource_id"] = _int(state.GetShader(stage))
            entry["entry_point"] = _text(state.GetShaderEntryPoint(stage))
        except BaseException as exc:
            entry["resource_error"] = "{}: {}".format(type(exc).__name__, exc)
        if reflection is not None:
            entry["encoding"] = _enum(reflection.encoding)
        shaders.append(entry)
        read_only[name] = _safe_section(
            unavailable,
            "read_only_resources." + name,
            lambda stage=stage: state.GetReadOnlyResources(stage, True),
        )
        read_write[name] = _safe_section(
            unavailable,
            "read_write_resources." + name,
            lambda stage=stage: state.GetReadWriteResources(stage, True),
        )
        constant_blocks[name] = _safe_section(
            unavailable,
            "constant_blocks." + name,
            lambda stage=stage: state.GetConstantBlocks(stage),
        )
    return {
        "event_id": event_id,
        "primitive_topology": _safe_section(
            unavailable, "primitive_topology", state.GetPrimitiveTopology
        ),
        "shaders": shaders,
        "index_buffer": _safe_section(unavailable, "index_buffer", state.GetIBuffer),
        "vertex_buffers": _safe_section(unavailable, "vertex_buffers", state.GetVBuffers),
        "vertex_inputs": _safe_section(unavailable, "vertex_inputs", state.GetVertexInputs),
        "output_targets": _safe_section(unavailable, "output_targets", state.GetOutputTargets),
        "depth_target": _safe_section(unavailable, "depth_target", state.GetDepthTarget),
        "viewport": _safe_section(unavailable, "viewport", lambda: state.GetViewport(0)),
        "scissor": _safe_section(unavailable, "scissor", lambda: state.GetScissor(0)),
        "color_blends": _safe_section(unavailable, "color_blends", state.GetColorBlends),
        "depth_test": _safe_section(unavailable, "depth_test", state.GetDepthTestState),
        "rasterizer": _safe_section(unavailable, "rasterizer", state.GetRasterState),
        "read_only_resources": read_only,
        "read_write_resources": read_write,
        "constant_blocks": constant_blocks,
        "unavailable_sections": unavailable,
    }


def _constant_blocks(reflection):
    blocks = []
    for block in reflection.constantBlocks:
        blocks.append(
            {
                "name": _text(block.name),
                "bind": _int(getattr(block, "fixedBindNumber", 0)),
                "byte_size": _int(block.byteSize),
                "buffer_backed": bool(getattr(block, "bufferBacked", False)),
            }
        )
    return blocks


def _decode_numeric(raw, offset, count, comp_type):
    """Decode one constant-buffer variable as floats or integers, when possible."""
    text = _text(comp_type)
    count = min(_int(count, 0), 16)
    if count <= 0 or offset < 0:
        return None
    if "Float" in text:
        pattern = "<{}f".format(count)
    elif "SInt" in text:
        pattern = "<{}i".format(count)
    elif "UInt" in text:
        pattern = "<{}I".format(count)
    elif "Int" in text:
        pattern = "<{}i".format(count)
    else:
        return None
    size = struct.calcsize(pattern)
    if offset + size > len(raw):
        return None
    try:
        return list(struct.unpack(pattern, raw[offset : offset + size]))
    except BaseException:
        return None


def _constant_block_variables(block, raw, limit):
    variables = []
    for variable in list(getattr(block, "variables", []) or [])[:limit]:
        variable_type = getattr(variable, "type", None)
        rows = _clamp(getattr(variable_type, "rows", 1), 0, 16, 1)
        columns = _clamp(getattr(variable_type, "columns", 1), 0, 16, 1)
        elements = _clamp(getattr(variable_type, "elements", 1), 0, 1024, 1)
        comp_type = _enum(getattr(variable_type, "compType", ""))
        byte_offset = _int(getattr(variable, "byteOffset", 0))
        variables.append(
            {
                "name": _text(getattr(variable, "name", "")),
                "byte_offset": byte_offset,
                "comp_type": comp_type,
                "rows": rows,
                "columns": columns,
                "elements": elements,
                "values": _decode_numeric(raw, byte_offset, rows * columns * elements, comp_type),
            }
        )
    return variables


def _constant_block_values(controller, state, stage, reflection, limit):
    """Read the bound constant buffers of one shader stage, values included."""
    blocks = []
    for index, block in enumerate(
        list(getattr(reflection, "constantBlocks", []) or [])[:MAX_ITEMS]
    ):
        entry = {
            "name": _text(block.name),
            "bind": _int(getattr(block, "fixedBindNumber", 0)),
            "byte_size": _int(block.byteSize),
            "buffer_backed": bool(getattr(block, "bufferBacked", False)),
            "buffer_resource_id": None,
            "byte_offset": None,
            "preview_hex": None,
            "variables": [],
            "error": None,
        }
        try:
            used = state.GetConstantBlock(stage, index, 0)
            descriptor = getattr(used, "descriptor", used)
        except BaseException as exc:
            entry["error"] = "{}: {}".format(type(exc).__name__, exc)
            blocks.append(entry)
            continue
        resource = getattr(descriptor, "resource", None)
        offset = _int(getattr(descriptor, "byteOffset", 0))
        size = _int(getattr(descriptor, "byteSize", 0)) or _int(block.byteSize)
        entry["buffer_resource_id"] = _int(resource)
        entry["byte_offset"] = offset
        raw = b""
        if _int(resource) != 0 and size > 0:
            try:
                raw = bytes(
                    controller.GetBufferData(resource, offset, min(size, MAX_PREVIEW_BYTES))
                )
            except BaseException as exc:
                entry["error"] = "{}: {}".format(type(exc).__name__, exc)
        entry["preview_hex"] = _text(binascii.hexlify(raw[:64]).decode("ascii", "replace"))
        entry["variables"] = _constant_block_variables(block, raw, limit)
        blocks.append(entry)
    return blocks


def _signature(entries):
    described = []
    for entry in entries:
        described.append(
            {
                "name": _text(getattr(entry, "varName", "")),
                "semantic": _text(getattr(entry, "semanticName", "")),
                "semantic_index": _int(getattr(entry, "semanticIndex", 0)),
                "reg_index": _int(getattr(entry, "regIndex", 0)),
                "comp_count": _int(getattr(entry, "compCount", 0)),
                "system_value": _enum(getattr(entry, "systemValue", "")),
            }
        )
    return described


def _shader_resources(entries):
    described = []
    for entry in entries:
        described.append(
            {
                "name": _text(getattr(entry, "name", "")),
                "bind": _int(getattr(entry, "fixedBindNumber", 0)),
                "descriptor_type": _enum(getattr(entry, "descriptorType", "")),
                "texture_type": _enum(getattr(entry, "textureType", "")),
                "is_texture": bool(getattr(entry, "isTexture", False)),
                "is_read_only": bool(getattr(entry, "isReadOnly", False)),
            }
        )
    return described


def _op_get_shader_info(controller, rd, params, context):
    del context
    event_id = _set_event(controller, params.get("event_id"))
    stage_name = _text(params.get("stage", "") or "Pixel").strip() or "Pixel"
    stage = _stage(rd, stage_name)
    if stage is None:
        raise ValueError("unsupported shader stage: {}".format(stage_name))
    state = controller.GetPipelineState()
    reflection = state.GetShaderReflection(stage)
    info = {
        "event_id": event_id,
        "stage": stage_name,
        "bound": reflection is not None,
        "disassembly_target": _text(params.get("disassembly_target", "") or ""),
    }
    if reflection is None:
        return info
    info.update(
        {
            "resource_id": _int(reflection.resourceId),
            "entry_point": _text(reflection.entryPoint),
            "stage": _enum(reflection.stage),
            "encoding": _enum(reflection.encoding),
            "dispatch_threads_dimension": _describe(reflection.dispatchThreadsDimension),
            "input_signature": _signature(reflection.inputSignature),
            "output_signature": _signature(reflection.outputSignature),
            "constant_blocks": _constant_blocks(reflection),
            "read_only_resources": _shader_resources(reflection.readOnlyResources),
            "read_write_resources": _shader_resources(reflection.readWriteResources),
            "samplers": _shader_resources(reflection.samplers),
        }
    )
    if params.get("include_source"):
        source = []
        try:
            debug_info = reflection.debugInfo
            for entry in getattr(debug_info, "files", []) or []:
                source.append(
                    {
                        "filename": _text(getattr(entry, "filename", "")),
                        "contents": _text(getattr(entry, "contents", ""))[:MAX_TEXT_CHARS],
                    }
                )
        except BaseException as exc:
            source = [{"error": "{}: {}".format(type(exc).__name__, exc)}]
        info["source"] = source
    if params.get("include_disassembly"):
        pipeline = None
        for getter in (state.GetGraphicsPipelineObject, state.GetComputePipelineObject):
            try:
                pipeline = getter()
            except BaseException:
                pipeline = None
            if pipeline is not None and _int(pipeline) != 0:
                break
            pipeline = None
        try:
            info["disassembly"] = _text(
                controller.DisassembleShader(
                    pipeline if pipeline is not None else rd.ResourceId.Null(),
                    reflection,
                    info["disassembly_target"],
                )
            )[:MAX_TEXT_CHARS]
        except BaseException as exc:
            info["disassembly"] = None
            info["disassembly_error"] = "{}: {}".format(type(exc).__name__, exc)
    if params.get("include_constant_buffers"):
        variable_limit = _clamp(params.get("variable_limit"), 1, MAX_ITEMS, 64)
        info["constant_block_values"] = _constant_block_values(
            controller, state, stage, reflection, variable_limit
        )
    return info


def _op_get_texture_data(controller, rd, params, context):
    del context
    resource_id = _int(params.get("resource_id"))
    if resource_id <= 0:
        raise ValueError("resource_id must be a positive integer")
    output_file = _text(params.get("output_file", "") or "")
    include_pixels = bool(params.get("include_pixels", False))
    if not output_file and not include_pixels:
        raise ValueError("provide output_file to export a texture, or set include_pixels")
    mip = _clamp(params.get("mip"), 0, 64, 0)
    slice_index = _clamp(params.get("slice"), 0, 65535, 0)
    result = {
        "resource_id": resource_id,
        "mip": mip,
        "slice": slice_index,
        "output_file": output_file or None,
        "size_bytes": None,
        "readback": None,
    }
    if output_file:
        extension = os.path.splitext(output_file)[1].casefold()
        file_type_name = _SAVE_FORMATS.get(extension)
        if file_type_name is None:
            choices = ", ".join(sorted(_SAVE_FORMATS))
            raise ValueError("output_file must use one of these extensions: " + choices)
        directory = os.path.dirname(os.path.abspath(output_file))
        if not os.path.isdir(directory):
            raise ValueError("output directory does not exist: {}".format(directory))
        save = rd.TextureSave()
        save.resourceId = _lookup_resource_id(controller, resource_id)
        save.destType = getattr(rd.FileType, file_type_name)
        save.alpha = rd.AlphaMapping.Preserve
        save.mip = mip
        save.slice.sliceIndex = slice_index
        save.typeCast = _comp_type(rd, params)
        controller.SaveTexture(save, output_file)
        if not os.path.isfile(output_file):
            raise RuntimeError("RenderDoc did not create {}".format(output_file))
        result["size_bytes"] = int(os.path.getsize(output_file))
    if include_pixels:
        raw = controller.GetTextureData(
            _lookup_resource_id(controller, resource_id), _subresource(rd, params)
        )
        raw = bytes(raw) if raw is not None else b""
        preview_bytes = _clamp(params.get("preview_bytes"), 0, MAX_PREVIEW_BYTES, 256)
        result["readback"] = {
            "byte_length": len(raw),
            "preview_hex": _text(binascii.hexlify(raw[:preview_bytes]).decode("ascii", "replace")),
        }
    return result


def _op_get_buffer_data(controller, rd, params, context):
    del context
    resource_id = _int(params.get("resource_id"))
    if resource_id <= 0:
        raise ValueError("resource_id must be a positive integer")
    offset = _clamp(params.get("offset"), 0, 1 << 40, 0)
    length = _clamp(params.get("length"), 0, 1 << 30, 0)
    preview_bytes = _clamp(params.get("preview_bytes"), 0, MAX_PREVIEW_BYTES, 256)
    raw = controller.GetBufferData(_lookup_resource_id(controller, resource_id), offset, length)
    raw = bytes(raw) if raw is not None else b""
    output_file = _text(params.get("output_file", "") or "")
    if output_file:
        directory = os.path.dirname(os.path.abspath(output_file))
        if not os.path.isdir(directory):
            raise ValueError("output directory does not exist: {}".format(directory))
        with open(output_file, "wb") as stream:
            stream.write(raw)
    return {
        "resource_id": resource_id,
        "offset": offset,
        "length": len(raw),
        "preview_hex": _text(binascii.hexlify(raw[:preview_bytes]).decode("ascii", "replace")),
        "preview_base64": _text(base64.b64encode(raw[:preview_bytes]).decode("ascii", "replace")),
        "output_file": output_file or None,
        "size_bytes": int(os.path.getsize(output_file)) if output_file else None,
    }


def _find_texture(controller, resource_id):
    """Look one texture up among the capture's textures, or raise."""
    wanted = _int(resource_id)
    if wanted <= 0:
        raise ValueError("resource_id must be a positive integer")
    for texture in controller.GetTextures():
        if _int(getattr(texture, "resourceId", 0)) == wanted:
            return texture
    raise ValueError("texture {} is not present in this capture".format(wanted))


def _level_size(texture, mip):
    """Mip dimensions, halved the way GPUs clamp, never below one texel."""
    width = max(1, _int(getattr(texture, "width", 0)) >> mip)
    height = max(1, _int(getattr(texture, "height", 0)) >> mip)
    return width, height


def _format_facts(rd, fmt):
    """Describe one texture format well enough to sample its texels.

    Block-compressed, packed, and special formats are reported as undecodable
    rather than sampled wrongly: their bytes are blocks or packed bits, not one
    texel every ``element_byte_size`` bytes.
    """
    del rd
    name = _text(fmt.Name()) if hasattr(fmt, "Name") else ""
    comp_count = _int(getattr(fmt, "compCount", 0))
    comp_byte_width = _int(getattr(fmt, "compByteWidth", 0))
    element = _int(getattr(fmt, "elementByteSize", 0))
    if element <= 0:
        element = comp_count * comp_byte_width
    comp_type = _enum(getattr(fmt, "compType", ""))
    special = _int(getattr(fmt, "special", 0))
    facts = {
        "name": name,
        "comp_count": comp_count,
        "comp_byte_width": comp_byte_width,
        "element_byte_size": element,
        "comp_type": comp_type,
        "special": special,
    }
    reason = None
    if comp_count < 1 or comp_count > 4:
        reason = "the format reports {} component(s) per texel; only 1-4 are sampled".format(
            comp_count
        )
    elif comp_byte_width not in (1, 2, 4, 8):
        reason = "the format reports a {}-byte component, which no decoder here covers".format(
            comp_byte_width
        )
    elif element != comp_count * comp_byte_width:
        reason = (
            "the format packs {} component(s) into {} byte(s), so texels are not "
            "evenly spaced".format(comp_count, element)
        )
    elif special:
        reason = "the format is special-encoded ({}) and its bytes are not raw components".format(
            name or "unknown"
        )
    elif comp_type not in _DECODABLE_COMP_TYPES:
        reason = "component type {} has no numeric decoder".format(comp_type or "unknown")
    facts["decodable"] = reason is None
    facts["reason"] = reason
    return facts


def _is_inf(value):
    return value == float("inf") or value == float("-inf")


def _decode_texel(raw, offset, facts):
    """Decode one texel into floats, or ``None`` when it cannot be decoded.

    Values are returned as floats for every format so one payload can carry
    them; a 64-bit integer therefore comes back rounded, which is why the
    caller reports ``value_kind`` alongside them.
    """
    if not facts["decodable"]:
        return None
    comp_count = facts["comp_count"]
    byte_width = facts["comp_byte_width"]
    comp_type = facts["comp_type"]
    values = []
    for index in range(comp_count):
        start = offset + index * byte_width
        if start + byte_width > len(raw):
            return None
        chunk = raw[start : start + byte_width]
        if comp_type == "CompType.Float":
            code = _FLOAT_CODES.get(byte_width)
            if code is None:
                return None
            values.append(float(struct.unpack("<" + code, chunk)[0]))
            continue
        signed = comp_type in ("CompType.SInt", "CompType.SNorm")
        number = int(struct.unpack("<" + _INT_CODES[(byte_width, signed)], chunk)[0])
        if comp_type == "CompType.SNorm":
            # The negative extreme of a signed normalised format is one step
            # wider than the positive one, so it is clamped to -1 instead of
            # decoding past it the way a raw divide would.
            values.append(max(-1.0, float(number) / float((1 << (byte_width * 8 - 1)) - 1)))
        elif comp_type in ("CompType.UNorm", "CompType.UNormSRGB"):
            values.append(float(number) / float((1 << (byte_width * 8)) - 1))
        else:
            values.append(float(number))
    return values


def _channel_stats(comp_count):
    """Per-channel accumulators shared by the sampling and diagnosis ops."""
    return [
        {
            "min": None,
            "max": None,
            "mean": None,
            "finite_sum": 0.0,
            "finite_count": 0,
            "nan_count": 0,
            "inf_count": 0,
            "negative_count": 0,
        }
        for _ in range(comp_count)
    ]


def _accumulate(stats, values):
    """Fold one decoded texel into the per-channel accumulators."""
    for index, value in enumerate(values):
        if index >= len(stats):
            break
        bucket = stats[index]
        if value != value:
            bucket["nan_count"] += 1
            continue
        if _is_inf(value):
            bucket["inf_count"] += 1
            continue
        bucket["finite_count"] += 1
        bucket["finite_sum"] += float(value)
        if value < 0:
            bucket["negative_count"] += 1
        if bucket["min"] is None or value < bucket["min"]:
            bucket["min"] = value
        if bucket["max"] is None or value > bucket["max"]:
            bucket["max"] = value


def _finish_stats(stats):
    """Turn the accumulators into the reported min, max, and mean."""
    for bucket in stats:
        total = bucket.pop("finite_count")
        summed = bucket.pop("finite_sum")
        bucket["mean"] = float(summed / total) if total else None
        bucket["finite_texel_count"] = total
    return stats


def _region_extent(texture, mip, params):
    """Clamp the requested region into the mip level's bounds."""
    level_width, level_height = _level_size(texture, mip)
    x = _clamp(params.get("x"), 0, max(0, level_width - 1), 0)
    y = _clamp(params.get("y"), 0, max(0, level_height - 1), 0)
    # The clamp has to end at the level edge, not at the level size: a region
    # that starts at x=1 of a 2-wide level is one texel wide, not two. The
    # default is the same edge, so an omitted width means "to the end".
    width = _clamp(params.get("width"), 1, max(1, level_width - x), max(1, level_width - x))
    height = _clamp(params.get("height"), 1, max(1, level_height - y), max(1, level_height - y))
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "level_width": level_width,
        "level_height": level_height,
    }


def _read_region(controller, rd, texture, mip, slice_index, sample_index):
    """Read one mip level back and guard against an unexpected byte count."""
    sub = rd.Subresource()
    sub.mip = mip
    sub.slice = slice_index
    sub.sample = sample_index
    raw = controller.GetTextureData(texture.resourceId, sub)
    raw = bytes(raw) if raw is not None else b""
    if len(raw) > MAX_REGION_BYTES:
        raise ValueError(
            "mip {} of this texture is {} byte(s), above the {} byte readback "
            "ceiling; sample a smaller mip or a smaller texture".format(
                mip, len(raw), MAX_REGION_BYTES
            )
        )
    return raw


def _unsupported_region(base, message, hint):
    """Report a region that cannot be sampled without pretending it was empty."""
    base.update(
        {
            "supported": False,
            "error_message": message,
            "hint": hint,
            "samples": [],
            "stats": None,
        }
    )
    return base


def _op_sample_pixel_region(controller, rd, params, context):
    """Sample a rectangular region of one texture or render target on a grid.

    ``GetTextureData`` reads a whole mip level back, so the region and the grid
    are applied to the bytes RenderDoc returned. The grid is what keeps a 4K
    target affordable, and it is also what makes the statistics a sample
    rather than a census whenever the grid is coarser than the region -- the
    payload says which of the two it is instead of leaving the caller to guess.
    """
    del context
    event_id = _set_event(controller, params.get("event_id"))
    texture = _find_texture(controller, _int(params.get("resource_id")))
    mip = _clamp(params.get("mip"), 0, 64, 0)
    slice_index = _clamp(params.get("slice"), 0, 65535, 0)
    sample_index = _clamp(params.get("sample"), 0, 65535, 0)
    region = _region_extent(texture, mip, params)
    grid_x = _clamp(params.get("grid_x"), 1, MAX_REGION_GRID, 1)
    grid_y = _clamp(params.get("grid_y"), 1, MAX_REGION_GRID, 1)
    max_samples = _clamp(params.get("max_samples"), 1, MAX_REGION_SAMPLES, 256)
    facts = _format_facts(rd, texture.format)
    region.update(
        {
            "mip": mip,
            "slice": slice_index,
            "sample": sample_index,
            "grid_x": grid_x,
            "grid_y": grid_y,
        }
    )
    base = {
        "event_id": event_id,
        "resource_id": _int(getattr(texture, "resourceId", 0)),
        "resource_name": _text(getattr(texture, "name", "")),
        "format": facts,
        "region": region,
        "value_kind": "float",
    }
    if not facts["decodable"]:
        return _unsupported_region(
            base,
            "this texture's format cannot be sampled: {}".format(facts["reason"]),
            "sample a texture with an uncompressed numeric format, or export it with "
            "get_texture_data first",
        )
    element = facts["element_byte_size"]
    raw = _read_region(controller, rd, texture, mip, slice_index, sample_index)
    expected = region["level_width"] * region["level_height"] * element
    if len(raw) < expected:
        return _unsupported_region(
            base,
            "RenderDoc returned {} byte(s) for a {}x{} level of {}-byte texels".format(
                len(raw), region["level_width"], region["level_height"], element
            ),
            "the format may be block-compressed; try another mip or export the texture "
            "with get_texture_data",
        )
    stats = _channel_stats(facts["comp_count"])
    points = []
    for row in range(grid_y):
        sy = region["y"] + min(
            region["height"] - 1, ((2 * row + 1) * region["height"]) // (2 * grid_y)
        )
        for column in range(grid_x):
            sx = region["x"] + min(
                region["width"] - 1, ((2 * column + 1) * region["width"]) // (2 * grid_x)
            )
            values = _decode_texel(raw, (sy * region["level_width"] + sx) * element, facts)
            if values is None:
                continue
            _accumulate(stats, values)
            if len(points) < MAX_REGION_SAMPLES:
                points.append({"x": sx, "y": sy, "values": values})
    grid_texels = grid_x * grid_y
    region_texels = region["width"] * region["height"]
    exhaustive = grid_texels >= region_texels
    base.update(
        {
            "supported": True,
            "sampled": not exhaustive,
            "estimate": not exhaustive,
            "estimate_method": None
            if exhaustive
            else "min, max, and mean over a {}x{} grid of the {}x{} region, not over "
            "every texel".format(grid_x, grid_y, region["width"], region["height"]),
            "sample_count": len(points),
            "samples_returned": min(len(points), max_samples),
            "samples_truncated": len(points) > max_samples,
            "grid_texel_count": grid_texels,
            "region_texel_count": region_texels,
            "samples": points[:max_samples],
            "stats": {"texel_count": len(points), "channels": _finish_stats(stats)},
        }
    )
    return base


def _check_definitions(facts):
    """Describe which anomaly checks this format can answer, and why.

    NaN and Inf only exist in a floating-point format, and a negative value is
    only a signal in one: an unsigned or normalised integer is never negative
    and a signed one is negative by design. Saying so per check is more useful
    than reporting four zeros.
    """
    comp_type = facts["comp_type"]
    float_format = comp_type == "CompType.Float"
    if float_format:
        integer_reason = None
    elif comp_type in ("CompType.UNorm", "CompType.UNormSRGB", "CompType.UInt"):
        integer_reason = "{} carries no floating-point channel, so it cannot be NaN or Inf".format(
            comp_type
        )
    else:
        integer_reason = "{} is an integer format".format(comp_type)
    if float_format:
        negative_reason = None
    elif comp_type in ("CompType.SInt", "CompType.SNorm"):
        negative_reason = "{} is signed, so negative values are in range by design".format(
            comp_type
        )
    else:
        negative_reason = "{} cannot encode a negative value".format(comp_type)
    return {
        "nan": {"applicable": float_format, "reason": integer_reason, "count": 0, "samples": []},
        "inf": {"applicable": float_format, "reason": integer_reason, "count": 0, "samples": []},
        "negative": {
            "applicable": float_format,
            "reason": negative_reason,
            "count": 0,
            "samples": [],
        },
        "precision": {
            "applicable": float_format,
            "reason": integer_reason,
            "count": 0,
            "tiny_count": 0,
            "huge_count": 0,
            "samples": [],
        },
    }


def _record_anomaly(check, x, y, channel, value, limit, kind=None):
    """Add one anomaly coordinate to a check, bounded per check."""
    if len(check["samples"]) >= limit:
        return
    entry = {"x": x, "y": y, "channel": channel, "value": value}
    if kind is not None:
        entry["kind"] = kind
    check["samples"].append(entry)


def _op_diagnose_pixel_values(controller, rd, params, context):
    """Scan a region for NaN, Inf, negative, and out-of-band float values.

    RenderDoc exposes no anomaly scanner, so this one walks the decoded texels
    itself. A 4K target is eight million texels, too many to walk through a
    Python loop at agent-interactive speed, so the scan strides by ``step``
    when the region is larger than ``max_texels`` and reports both the stride
    and the fact that the counts are then a sample of the region.
    """
    del context
    event_id = _set_event(controller, params.get("event_id"))
    texture = _find_texture(controller, _int(params.get("resource_id")))
    mip = _clamp(params.get("mip"), 0, 64, 0)
    slice_index = _clamp(params.get("slice"), 0, 65535, 0)
    sample_index = _clamp(params.get("sample"), 0, 65535, 0)
    region = _region_extent(texture, mip, params)
    max_texels = _clamp(params.get("max_texels"), 1, MAX_DIAGNOSE_TEXELS, MAX_DIAGNOSE_TEXELS)
    max_anomalies = _clamp(params.get("max_anomalies"), 1, MAX_ANOMALY_SAMPLES, 32)
    requested = params.get("checks") or list(_PIXEL_CHECKS)
    selected = [name for name in _PIXEL_CHECKS if name in requested]
    if not selected:
        raise ValueError("checks must name at least one of: " + ", ".join(_PIXEL_CHECKS))
    facts = _format_facts(rd, texture.format)
    raw = b""
    element = facts["element_byte_size"]
    if facts["decodable"] and element > 0:
        raw = _read_region(controller, rd, texture, mip, slice_index, sample_index)
    step = 1
    while step < 4096:
        rows = (region["height"] + step - 1) // step
        columns = (region["width"] + step - 1) // step
        if rows * columns <= max_texels:
            break
        step += 1
    region.update(
        {
            "mip": mip,
            "slice": slice_index,
            "sample": sample_index,
            "step": step,
            "scan_columns": (region["width"] + step - 1) // step,
            "scan_rows": (region["height"] + step - 1) // step,
        }
    )
    base = {
        "event_id": event_id,
        "resource_id": _int(getattr(texture, "resourceId", 0)),
        "resource_name": _text(getattr(texture, "name", "")),
        "format": facts,
        "region": region,
        "checks_requested": selected,
    }
    if not facts["decodable"] or element <= 0:
        base.update(
            {
                "supported": False,
                "error_message": "this texture's format cannot be diagnosed: {}".format(
                    facts["reason"]
                ),
                "hint": "diagnose a texture with an uncompressed numeric format",
                "scanned_texels": 0,
                "anomaly_texel_count": 0,
                "clean": True,
                "stats": None,
            }
        )
        return base
    definitions = _check_definitions(facts)
    stats = _channel_stats(facts["comp_count"])
    scanned = 0
    anomaly_texels = 0
    for sy in range(region["y"], region["y"] + region["height"], step):
        row_offset = sy * region["level_width"]
        for sx in range(region["x"], region["x"] + region["width"], step):
            values = _decode_texel(raw, (row_offset + sx) * element, facts)
            if values is None:
                continue
            scanned += 1
            _accumulate(stats, values)
            flagged = False
            for channel, value in enumerate(values):
                missing = value != value
                infinite = not missing and _is_inf(value)
                if missing and definitions["nan"]["applicable"]:
                    if "nan" in selected:
                        definitions["nan"]["count"] += 1
                        _record_anomaly(definitions["nan"], sx, sy, channel, value, max_anomalies)
                    flagged = True
                if infinite and definitions["inf"]["applicable"]:
                    if "inf" in selected:
                        definitions["inf"]["count"] += 1
                        _record_anomaly(definitions["inf"], sx, sy, channel, value, max_anomalies)
                    flagged = True
                if not missing and not infinite and value < 0:
                    negative = definitions["negative"]
                    if "negative" in selected and negative["applicable"]:
                        negative["count"] += 1
                        _record_anomaly(negative, sx, sy, channel, value, max_anomalies)
                        flagged = True
                precision = definitions["precision"]
                if (
                    "precision" in selected
                    and precision["applicable"]
                    and not missing
                    and not infinite
                    and value != 0.0
                ):
                    magnitude = abs(value)
                    if magnitude < FLOAT_TINY:
                        precision["tiny_count"] += 1
                        precision["count"] += 1
                        _record_anomaly(precision, sx, sy, channel, value, max_anomalies, "tiny")
                        flagged = True
                    elif magnitude > FLOAT_HUGE:
                        precision["huge_count"] += 1
                        precision["count"] += 1
                        _record_anomaly(precision, sx, sy, channel, value, max_anomalies, "huge")
                        flagged = True
            if flagged:
                anomaly_texels += 1
    checks = {}
    for name in selected:
        check = dict(definitions[name])
        if not check["applicable"]:
            check["count"] = 0
            check["samples"] = []
        else:
            check["samples_truncated"] = check["count"] > len(check["samples"])
            check["max_anomalies"] = max_anomalies
        checks[name] = check
    sampled = step > 1
    base.update(
        {
            "supported": True,
            "scanned_texels": scanned,
            "region_texel_count": region["width"] * region["height"],
            "sampled": sampled,
            "estimate": sampled,
            "estimate_method": None
            if not sampled
            else "every {}. texel of the region was scanned, so the counts are a "
            "sample of {} texel(s), not a census".format(step, region["width"] * region["height"]),
            "checks": checks,
            "anomaly_texel_count": anomaly_texels,
            "clean": anomaly_texels == 0,
            "stats": {"texel_count": scanned, "channels": _finish_stats(stats)},
        }
    )
    return base


def _flag_bit(rd, name):
    """One ``ActionFlags`` bit by name, or 0 when this build lacks the member."""
    enum = getattr(rd, "ActionFlags", None)
    if enum is None:
        return 0
    return _int(getattr(enum, name, 0), 0)


def _action_kind(rd, action):
    """Classify one action for the counters the analysis ops report."""
    flags = _int(getattr(action, "flags", 0))
    if flags & _flag_bit(rd, "Drawcall"):
        return "draw"
    if flags & _flag_bit(rd, "Dispatch"):
        return "dispatch"
    if flags & _flag_bit(rd, "Clear"):
        return "clear"
    return "other"


def _new_pass_stats():
    """Accumulators for one pass subtree."""
    return {
        "action_count": 0,
        "draw_count": 0,
        "dispatch_count": 0,
        "clear_count": 0,
        "other_count": 0,
        "total_indices": 0,
        "total_instances": 0,
        "triangle_estimate": 0,
        "outputs": set(),
        "depth_outputs": set(),
        "first_event_id": None,
        "last_event_id": None,
        "actions": [],
    }


def _finish_pass_stats(stats):
    """Render one pass accumulator as JSON, sets included."""
    stats["outputs"] = sorted(stats["outputs"])
    stats["depth_outputs"] = sorted(stats["depth_outputs"])
    return stats


def _accumulate_pass(controller, rd, action, stats, max_actions):
    """Fold one action subtree into a pass accumulator.

    ``numIndices`` and ``numInstances`` are what the API recorded, so a triangle
    count derived from them counts everything the draw asked for: it is before
    culling, clipping, and the vertex shader, which is why it is reported as an
    estimate with its method attached rather than as a measured count.
    """
    structured = None
    try:
        structured = controller.GetStructuredFile()
    except BaseException:
        structured = None
    stack = [action]
    while stack:
        node = stack.pop()
        event_id = _int(getattr(node, "eventId", 0))
        stats["action_count"] += 1
        if stats["first_event_id"] is None or event_id < stats["first_event_id"]:
            stats["first_event_id"] = event_id
        if stats["last_event_id"] is None or event_id > stats["last_event_id"]:
            stats["last_event_id"] = event_id
        kind = _action_kind(rd, node)
        stats[kind + "_count"] += 1
        indices = _int(getattr(node, "numIndices", 0))
        instances = max(1, _int(getattr(node, "numInstances", 0)))
        if kind == "draw":
            # Only draws carry geometry. A clear reports an index count too, and
            # counting it would inflate every triangle estimate in the frame.
            stats["total_indices"] += indices
            stats["total_instances"] += instances
            stats["triangle_estimate"] += (indices // 3) * instances
        for target in getattr(node, "outputs", []) or []:
            target_id = _int(target)
            if target_id:
                stats["outputs"].add(target_id)
        depth = _int(getattr(node, "depthOut", 0))
        if depth:
            stats["depth_outputs"].add(depth)
        if len(stats["actions"]) < max_actions:
            name = ""
            try:
                name = str(node.GetName(structured)) if structured is not None else ""
            except BaseException:
                name = ""
            stats["actions"].append(
                {
                    "event_id": event_id,
                    "name": name or _text(getattr(node, "customName", "")),
                    "kind": kind,
                    "num_indices": indices,
                    "num_instances": instances,
                }
            )
        stack.extend(getattr(node, "children", []) or [])


def _texture_inventory(controller, limit):
    """Report the capture's textures by size and by format."""
    entries = []
    formats = {}
    total_bytes = 0
    for texture in controller.GetTextures():
        width = _int(getattr(texture, "width", 0))
        height = max(1, _int(getattr(texture, "height", 0)))
        depth = max(1, _int(getattr(texture, "depth", 0)))
        arraysize = max(1, _int(getattr(texture, "arraysize", 0)))
        mips = max(1, _int(getattr(texture, "mips", 0)))
        byte_size = _int(getattr(texture, "byteSize", 0))
        if byte_size <= 0:
            byte_size = width * height * depth * arraysize * 4
        total_bytes += byte_size
        name = ""
        try:
            name = _text(texture.format.Name())
        except BaseException:
            name = ""
        formats[name] = formats.get(name, 0) + 1
        entries.append(
            {
                "resource_id": _int(getattr(texture, "resourceId", 0)),
                "name": _text(getattr(texture, "name", "")),
                "width": width,
                "height": height,
                "depth": depth,
                "arraysize": arraysize,
                "mips": mips,
                "format": name,
                "byte_size": byte_size,
            }
        )
    entries.sort(key=lambda entry: entry["byte_size"], reverse=True)
    ranked_formats = sorted(formats.items(), key=lambda item: item[1], reverse=True)
    return {
        "total_texture_bytes": total_bytes,
        "largest_textures": entries[:limit],
        "largest_textures_truncated": len(entries) > limit,
        "formats": [{"format": name, "count": count} for name, count in ranked_formats[:limit]],
        "format_count": len(formats),
    }


def _replay_message_report(controller, event_id, limit):
    """Count the messages a replay up to ``event_id`` produced, by severity."""
    if event_id is None:
        return {"unavailable": "the capture reports no event to replay to", "count": None}
    try:
        _set_event(controller, event_id)
        messages = controller.GetDebugMessages()
    except BaseException as exc:
        return {"unavailable": "{}: {}".format(type(exc).__name__, exc), "count": None}
    by_severity = {}
    samples = []
    for message in messages:
        severity = _enum(getattr(message, "severity", ""))
        by_severity[severity] = by_severity.get(severity, 0) + 1
        if len(samples) < limit:
            samples.append(
                {
                    "event_id": _int(getattr(message, "eventId", 0)),
                    "category": _enum(getattr(message, "category", "")),
                    "severity": severity,
                    "source": _enum(getattr(message, "source", "")),
                    "id": _int(getattr(message, "messageID", 0)),
                    "description": _text(getattr(message, "description", "")),
                }
            )
    return {
        "event_id": event_id,
        "count": len(messages),
        "by_severity": by_severity,
        "samples": samples,
        "samples_truncated": len(messages) > limit,
    }


def _counter_report(controller):
    """Report whether this capture exposes counters, and one that carries time."""
    try:
        entries = _counter_descriptions(controller, controller.EnumerateCounters())
    except BaseException as exc:
        return {"unavailable": "{}: {}".format(type(exc).__name__, exc), "counter_count": None}
    timing = _find_timing_counter(entries)
    return {
        "counter_count": len(entries),
        "timing_counter": None if timing is None else timing[1],
    }


def _overview_signals(described, totals, textures, counters, messages):
    """Turn the overview facts into the signals an agent would otherwise miss.

    Every signal is a heuristic over structure: none of them is a measurement,
    so each one carries its basis and the payload says so once at the top.
    """
    signals = []

    def add(code, severity, detail):
        signals.append({"code": code, "severity": severity, "detail": detail, "basis": "heuristic"})

    capabilities = described.get("capabilities") or {}
    if capabilities.get("degraded"):
        add(
            "degraded_replay",
            "warning",
            "this capture is replaying in a degraded (remote or fallback) mode, so "
            "some readbacks are unavailable",
        )
    if not described.get("action_count"):
        add("no_actions", "warning", "the capture contains no actions to inspect")
    elif not totals["draw_count"]:
        add("no_draws", "warning", "the capture contains no draw calls")
    if totals["draw_count"] > 1000:
        add(
            "high_draw_count",
            "info",
            "the frame issues {} draw(s); consider analyze_render_passes to find "
            "where they cluster".format(totals["draw_count"]),
        )
    if counters.get("counter_count") == 0:
        add(
            "no_counters",
            "info",
            "this driver exposes no GPU counters, so counter and timing tools have "
            "nothing to sample here",
        )
    elif counters.get("counter_count") and not counters.get("timing_counter"):
        add(
            "no_timing_counter",
            "info",
            "this driver exposes {} counter(s) but none of them carries GPU "
            "duration, so per-pass timing cannot be measured here".format(
                counters["counter_count"]
            ),
        )
    if messages.get("count"):
        add(
            "debug_messages",
            "warning",
            "the replay produced {} debug message(s); get_debug_messages lists them".format(
                messages["count"]
            ),
        )
    for texture in textures.get("largest_textures") or []:
        if texture["byte_size"] >= 64 * 1024 * 1024:
            add(
                "large_texture",
                "info",
                "{} is {} byte(s); it dominates the capture's texture memory".format(
                    texture["name"], texture["byte_size"]
                ),
            )
            break
    return signals


def _op_get_frame_overview(controller, rd, params, context):
    """Answer "what is in this capture" in one replay instead of six.

    The pieces are the ones the other tools already expose individually; what
    this op adds is the join and the signals -- the facts that are only visible
    once the pieces sit next to each other, such as a frame with no draws or a
    driver with counters but no duration counter.
    """
    max_passes = _clamp(params.get("max_passes"), 1, 512, 32)
    max_textures = _clamp(params.get("max_textures"), 1, MAX_ITEMS, 8)
    max_actions = _clamp(params.get("max_actions_per_pass"), 0, MAX_ITEMS, 5)
    max_messages = _clamp(params.get("max_messages"), 0, MAX_ITEMS, 8)
    described = _op_describe_capture(controller, rd, {}, context)
    roots = controller.GetRootActions()
    totals = _new_pass_stats()
    passes = []
    for action in roots:
        stats = _new_pass_stats()
        _accumulate_pass(controller, rd, action, stats, max_actions)
        for key in (
            "action_count",
            "draw_count",
            "dispatch_count",
            "clear_count",
            "other_count",
            "total_indices",
            "total_instances",
            "triangle_estimate",
        ):
            totals[key] += stats[key]
        totals["outputs"].update(stats["outputs"])
        totals["depth_outputs"].update(stats["depth_outputs"])
        if len(passes) < max_passes:
            name = _text(getattr(action, "customName", ""))
            entry = {
                "event_id": _int(getattr(action, "eventId", 0)),
                "name": name,
            }
            entry.update(_finish_pass_stats(stats))
            passes.append(entry)
    textures = _texture_inventory(controller, max_textures)
    counters = _counter_report(controller)
    messages = (
        _replay_message_report(controller, described.get("last_event_id"), max_messages)
        if params.get("include_debug_messages", True)
        else {"unavailable": "include_debug_messages was false", "count": None}
    )
    totals_finished = _finish_pass_stats(totals)
    del totals_finished["actions"]
    del totals_finished["first_event_id"]
    del totals_finished["last_event_id"]
    return {
        "capabilities": described.get("capabilities"),
        "api_properties": described.get("api_properties"),
        "frame_info": described.get("frame_info"),
        "actions": {
            "action_count": described.get("action_count"),
            "root_action_count": described.get("root_action_count"),
            "first_event_id": described.get("first_event_id"),
            "last_event_id": described.get("last_event_id"),
            "draw_count": totals_finished["draw_count"],
            "dispatch_count": totals_finished["dispatch_count"],
            "clear_count": totals_finished["clear_count"],
            "other_count": totals_finished["other_count"],
        },
        "resources": {
            "resource_count": described.get("resource_count"),
            "texture_count": described.get("texture_count"),
            "buffer_count": described.get("buffer_count"),
            "total_texture_bytes": textures["total_texture_bytes"],
            "largest_textures": textures["largest_textures"],
            "largest_textures_truncated": textures["largest_textures_truncated"],
            "formats": textures["formats"],
            "format_count": textures["format_count"],
        },
        "pass_count": len(roots),
        "passes": passes,
        "passes_truncated": len(roots) > max_passes,
        "counters": counters,
        "debug_messages": messages,
        "signals": _overview_signals(described, totals_finished, textures, counters, messages),
        "estimate_fields": {
            "passes[].triangle_estimate": "numIndices / 3 * numInstances, counted before "
            "GPU culling, clipping, and vertex shading",
            "resources.total_texture_bytes": "sum of the texture byte sizes RenderDoc "
            "reported, not a measurement of GPU memory",
            "signals": "heuristics over the capture's structure, not measurements",
        },
    }


def _locate_action(actions, event_id, depth=0, parent=None):
    """Find one action together with its depth and parent event id."""
    for action in actions:
        if _int(getattr(action, "eventId", 0)) == event_id:
            return action, depth, parent
        found = _locate_action(
            getattr(action, "children", []) or [], event_id, depth + 1, _int(action.eventId)
        )
        if found is not None:
            return found
    return None


def _bound_stages(pipeline_state):
    """The shader stages the pipeline state reports a shader bound to."""
    stages = []
    for entry in (pipeline_state or {}).get("shaders") or []:
        if isinstance(entry, dict) and entry.get("resource_id"):
            stages.append(entry.get("stage"))
    return [stage for stage in stages if stage]


def _op_get_draw_call_state(controller, rd, params, context):
    """Snapshot everything one draw was executed with, in one replay.

    The three reads this joins already exist as separate tools; an agent that
    wants to know what a draw did otherwise pays three round trips and then has
    to line the results up by event id. The per-stage shader reads are wrapped
    individually so one stage this capture cannot reflect does not cost the
    caller the other stages.
    """
    event_id = _int(params.get("event_id"))
    located = _locate_action(controller.GetRootActions(), event_id)
    if located is None:
        raise ValueError("event {} was not found".format(event_id))
    _action, depth, parent = located
    action = _op_get_action(controller, rd, {"event_id": event_id}, context)
    action["depth"] = depth
    action["parent_event_id"] = parent
    pipeline_state = _op_get_pipeline_state(controller, rd, {"event_id": event_id}, context)
    requested = params.get("stages") or _bound_stages(pipeline_state)
    include_constant_buffers = bool(params.get("include_constant_buffers", False))
    include_source = bool(params.get("include_source", False))
    variable_limit = _clamp(params.get("variable_limit"), 1, MAX_ITEMS, 64)
    shaders = []
    unavailable = list(pipeline_state.get("unavailable_sections") or [])
    for stage_name in requested:
        stage = _stage(rd, _text(stage_name))
        if stage is None:
            shaders.append(
                {
                    "stage": _text(stage_name),
                    "requested_stage": _text(stage_name),
                    "bound": False,
                    "error": "unsupported shader stage",
                }
            )
            continue
        try:
            info = _op_get_shader_info(
                controller,
                rd,
                {
                    "event_id": event_id,
                    "stage": _text(stage_name),
                    "include_constant_buffers": include_constant_buffers,
                    "include_source": include_source,
                    "variable_limit": variable_limit,
                },
                context,
            )
        except BaseException as exc:
            unavailable.append("shader_info." + _text(stage_name))
            info = {
                "stage": _text(stage_name),
                "bound": False,
                "error": "{}: {}".format(type(exc).__name__, exc),
            }
        # A bound shader reports its stage through RenderDoc's own enum, so the
        # name the caller asked for is carried alongside it: the list is then
        # keyed the same way whether or not the stage could be reflected.
        info["requested_stage"] = _text(stage_name)
        shaders.append(info)
    read_only = pipeline_state.get("read_only_resources") or {}
    read_write = pipeline_state.get("read_write_resources") or {}
    summary = {
        "event_id": event_id,
        "name": action.get("name"),
        "flag_names": action.get("flag_names"),
        "num_indices": action.get("num_indices"),
        "num_instances": action.get("num_instances"),
        "primitive_topology": pipeline_state.get("primitive_topology"),
        "outputs": action.get("outputs"),
        "depth_out": action.get("depth_out"),
        "shader_stages": [entry.get("requested_stage") for entry in shaders],
        "shader_count": len([entry for entry in shaders if entry.get("bound")]),
        "texture_binding_count": _binding_count(read_only),
        "read_write_binding_count": _binding_count(read_write),
        "vertex_buffer_count": len(pipeline_state.get("vertex_buffers") or []),
    }
    return {
        "event_id": event_id,
        "action": action,
        "pipeline_state": pipeline_state,
        "shaders": shaders,
        "summary": summary,
        "unavailable_sections": unavailable,
    }


def _binding_count(bindings):
    """Count the resource bindings the pipeline state reported across stages."""
    total = 0
    for stage_bindings in bindings.values():
        if isinstance(stage_bindings, list):
            total += len(stage_bindings)
    return total


def _action_nodes(controller, rd, max_depth):
    """Flatten the action tree into one entry per node, parents included."""
    nodes = []
    structured = None
    try:
        structured = controller.GetStructuredFile()
    except BaseException:
        structured = None

    def walk(actions, depth, parent):
        for action in actions:
            event_id = _int(getattr(action, "eventId", 0))
            name = ""
            try:
                name = str(action.GetName(structured)) if structured is not None else ""
            except BaseException:
                name = ""
            nodes.append(
                {
                    "event_id": event_id,
                    "action_id": _int(getattr(action, "actionId", 0)),
                    "depth": depth,
                    "parent_event_id": parent,
                    "name": name or _text(getattr(action, "customName", "")),
                    "kind": _action_kind(rd, action),
                    "num_indices": _int(getattr(action, "numIndices", 0)),
                    "num_instances": max(1, _int(getattr(action, "numInstances", 0))),
                }
            )
            if depth + 1 <= max_depth:
                walk(getattr(action, "children", []) or [], depth + 1, event_id)

    walk(controller.GetRootActions(), 0, None)
    return nodes


def _op_analyze_render_passes(controller, rd, params, context):
    """Report the frame's pass structure and what each pass carries.

    RenderDoc's action tree is the only pass boundary a capture reliably has:
    markers and regions are ordinary actions, so a pass is a node at
    ``pass_depth`` together with everything beneath it. Draws, dispatches,
    index counts, and output targets are counted from the tree alone, which is
    why this op answers on every capture that replays at all -- no counter, and
    therefore no driver support, is involved.
    """
    del context
    pass_depth = _clamp(params.get("pass_depth"), 0, 32, 0)
    max_depth = _clamp(params.get("max_depth"), 0, 32, 32)
    offset = _clamp(params.get("offset"), 0, 10000000, 0)
    limit = _clamp(params.get("limit"), 1, 5000, 200)
    max_actions = _clamp(params.get("max_actions_per_pass"), 0, MAX_ITEMS, 8)
    name_filter = _text(params.get("name_filter", "") or "").strip().casefold()
    roots = controller.GetRootActions()
    nodes = []

    def collect(actions, depth):
        for action in actions:
            nodes.append((action, depth))
            if depth + 1 <= max_depth:
                collect(getattr(action, "children", []) or [], depth + 1)

    collect(roots, 0)
    # Totals come from the root subtrees, so a nested pass is counted once no
    # matter which level the caller asks for.
    totals = _new_pass_stats()
    for action in roots:
        stats = _new_pass_stats()
        _accumulate_pass(controller, rd, action, stats, 0)
        for key in (
            "action_count",
            "draw_count",
            "dispatch_count",
            "clear_count",
            "other_count",
            "total_indices",
            "total_instances",
            "triangle_estimate",
        ):
            totals[key] += stats[key]
        totals["outputs"].update(stats["outputs"])
        totals["depth_outputs"].update(stats["depth_outputs"])
    passes = []
    for action, depth in nodes:
        if depth != pass_depth:
            continue
        name = _text(getattr(action, "customName", ""))
        if name_filter and name_filter not in name.casefold():
            continue
        stats = _new_pass_stats()
        _accumulate_pass(controller, rd, action, stats, max_actions)
        entry = {
            "event_id": _int(getattr(action, "eventId", 0)),
            "name": name,
            "child_count": len(getattr(action, "children", []) or []),
        }
        entry.update(_finish_pass_stats(stats))
        passes.append(entry)
    passes.sort(key=lambda entry: entry["event_id"])
    ranked = sorted(passes, key=lambda entry: entry["draw_count"], reverse=True)
    heaviest = sorted(passes, key=lambda entry: entry["triangle_estimate"], reverse=True)
    totals_finished = _finish_pass_stats(totals)
    del totals_finished["actions"]
    del totals_finished["first_event_id"]
    del totals_finished["last_event_id"]
    return {
        "pass_depth": pass_depth,
        "max_depth": max_depth,
        "offset": offset,
        "limit": limit,
        "name_filter": name_filter or None,
        "pass_count": len(passes),
        "truncated": len(passes) > offset + limit,
        "passes": passes[offset : offset + limit],
        "top_passes_by_draws": [
            {
                "event_id": entry["event_id"],
                "name": entry["name"],
                "draw_count": entry["draw_count"],
            }
            for entry in ranked[:limit]
        ],
        "top_passes_by_triangles": [
            {
                "event_id": entry["event_id"],
                "name": entry["name"],
                "triangle_estimate": entry["triangle_estimate"],
            }
            for entry in heaviest[:limit]
        ],
        "totals": totals_finished,
        "estimate_fields": {
            "passes[].triangle_estimate": "numIndices / 3 * numInstances, counted before "
            "GPU culling, clipping, and vertex shading"
        },
    }


def _state_fingerprint(controller, rd):
    """Reduce one event's pipeline state to the parts that cost a state switch.

    Every section is read defensively: a section this capture's replay does not
    expose becomes ``None``, which is a value that compares unequal to any real
    state and is reported that way, rather than an exception that loses the
    whole diff.
    """
    state = controller.GetPipelineState()
    facts = {}

    def take(key, getter):
        try:
            facts[key] = getter()
        except BaseException:
            facts[key] = None

    take("primitive_topology", lambda: _enum(state.GetPrimitiveTopology()))
    take("graphics_pipeline", lambda: _int(state.GetGraphicsPipelineObject()))
    take("compute_pipeline", lambda: _int(state.GetComputePipelineObject()))
    shaders = {}
    for name in _SHADER_STAGES:
        stage = _stage(rd, name)
        if stage is None:
            continue

        def read(stage=stage):
            try:
                return _int(state.GetShader(stage))
            except BaseException:
                return None

        shaders[name] = read()
    facts["shaders"] = shaders
    take(
        "outputs",
        lambda: [_int(target.resource) for target in state.GetOutputTargets()],
    )
    take("depth_target", lambda: _int(state.GetDepthTarget().resource))
    take("vertex_buffers", lambda: [_int(item.resource) for item in state.GetVBuffers()])
    take("index_buffer", lambda: _int(state.GetIBuffer().resource))
    take("viewport", lambda: _size_of(state.GetViewport(0)))
    take("scissor", lambda: _size_of(state.GetScissor(0)))
    take("blend_enabled", lambda: [bool(item.enabled) for item in state.GetColorBlends()])
    take("depth_enable", lambda: bool(state.GetDepthTestState().depthEnable))
    take("cull_mode", lambda: _enum(state.GetRasterState().cullMode))
    return facts


def _size_of(rect):
    """A viewport or scissor rectangle as the two numbers that affect state."""
    return [_describe(getattr(rect, "width", 0)), _describe(getattr(rect, "height", 0))]


def _fingerprint_key(facts):
    """One comparable identity for a fingerprint, for grouping identical runs."""
    return json.dumps(facts, sort_keys=True, default=_text)


def _op_analyze_state_changes(controller, rd, params, context):
    """Diff adjacent draws' pipeline state and find where the state repeats.

    Two draws that run with an identical fingerprint are a batching opportunity:
    the switch between them was avoidable. Reporting the runs rather than only
    the diffs is what makes that visible, and bounding the walk with
    ``max_events`` is what keeps one call to a state read per draw instead of
    one per draw in the frame.
    """
    del context
    max_events = _clamp(params.get("max_events"), 2, 256, 64)
    max_changes = _clamp(params.get("max_changes"), 1, 5000, 200)
    min_run_length = _clamp(params.get("min_run_length"), 2, 100000, 2)
    first_event = params.get("first_event_id")
    last_event = params.get("last_event_id")
    first_event = None if first_event is None else _int(first_event)
    last_event = None if last_event is None else _int(last_event)
    nodes = _action_nodes(controller, rd, _clamp(params.get("max_depth"), 0, 32, 32))
    draws = [node for node in nodes if node["kind"] == "draw"]
    if first_event is not None:
        draws = [node for node in draws if node["event_id"] >= first_event]
    if last_event is not None:
        draws = [node for node in draws if node["event_id"] <= last_event]
    selected = draws[:max_events]
    unavailable = []
    fingerprints = []
    for node in selected:
        try:
            _set_event(controller, node["event_id"])
            fingerprints.append(_state_fingerprint(controller, rd))
        except BaseException as exc:
            unavailable.append(
                "state.event_{}: {}: {}".format(node["event_id"], type(exc).__name__, exc)
            )
            fingerprints.append(None)
    changes = []
    by_key = {}
    for index in range(1, len(selected)):
        before = fingerprints[index - 1]
        after = fingerprints[index]
        if before is None or after is None:
            continue
        for key in sorted(set(before) | set(after)):
            if before.get(key) == after.get(key):
                continue
            by_key[key] = by_key.get(key, 0) + 1
            if len(changes) < max_changes:
                changes.append(
                    {
                        "from_event_id": selected[index - 1]["event_id"],
                        "to_event_id": selected[index]["event_id"],
                        "key": key,
                        "before": before.get(key),
                        "after": after.get(key),
                    }
                )
    runs = []
    run = []
    previous = None
    for node, fingerprint in zip(selected, fingerprints):
        key = None if fingerprint is None else _fingerprint_key(fingerprint)
        if fingerprint is not None and key == previous:
            run.append(node["event_id"])
            continue
        if len(run) >= min_run_length:
            runs.append(run)
        run = [] if fingerprint is None else [node["event_id"]]
        previous = key
    if len(run) >= min_run_length:
        runs.append(run)
    ranked_keys = sorted(by_key.items(), key=lambda item: item[1], reverse=True)
    return {
        "first_event_id": first_event,
        "last_event_id": last_event,
        "max_events": max_events,
        "draw_count": len(draws),
        "analyzed_events": [node["event_id"] for node in selected],
        "analyzed_event_count": len(selected),
        "event_count_truncated": len(draws) > max_events,
        "change_count": sum(by_key.values()),
        "changes_returned": len(changes),
        "changes_truncated": sum(by_key.values()) > len(changes),
        "changes": changes,
        "changes_by_key": [{"key": key, "count": count} for key, count in ranked_keys],
        "min_run_length": min_run_length,
        "runs": [
            {
                "from_event_id": events[0],
                "to_event_id": events[-1],
                "draw_count": len(events),
                "event_ids": events,
            }
            for events in runs
        ],
        "batchable_draw_count": sum(len(events) for events in runs),
        "unavailable_sections": unavailable,
    }


def _ancestor_map(rows):
    """Every event's ancestor event ids, built in one pass over the tree."""
    by_event = dict((row["event_id"], row) for row in rows)
    ancestors = {}
    for row in rows:
        chain = set()
        current = row.get("parent_event_id")
        while current is not None and current not in chain:
            chain.add(current)
            parent = by_event.get(current)
            current = None if parent is None else parent.get("parent_event_id")
        ancestors[row["event_id"]] = chain
    return ancestors


def _pass_duration(row, rows, durations, ancestors):
    """Report one pass's duration and how it was arrived at.

    A pass marker event usually carries its own counter sample -- the elapsed
    time of the pass -- and that is the number to report. When the driver did
    not sample the pass event itself, the fallback sums the pass's timed
    actions. The pass event's own sample is never added to that sum, so a
    nested capture does not count the same time twice.
    """
    own = durations.get(row["event_id"])
    children = [
        other
        for other in rows
        if other["event_id"] != row["event_id"]
        and other["duration"] is not None
        and row["event_id"] in ancestors.get(other["event_id"], ())
    ]
    if own is not None:
        return own, "pass_event_counter", len(children) + 1
    return sum(other["duration"] for other in children), "sum_of_timed_actions", len(children)


def _op_get_pass_timing(controller, rd, params, context):
    """Report GPU duration per pass, joined from the timing counter.

    RenderDoc stores no duration on an action, so the number comes from the
    timing counter this driver exposes, sampled per event and joined back onto
    the action tree exactly the way ``get_action_timing`` does. When this driver
    exposes no duration counter the result says so and lists the counters that
    are available, rather than reporting a frame that looks free.
    """
    del context
    pass_depth = _clamp(params.get("pass_depth"), 0, 32, 0)
    max_depth = _clamp(params.get("max_depth"), 0, 32, 32)
    offset = _clamp(params.get("offset"), 0, 10000000, 0)
    limit = _clamp(params.get("limit"), 1, 5000, 200)
    slowest_limit = _clamp(params.get("slowest_actions"), 0, 100, 3)
    entries = _counter_descriptions(controller, controller.EnumerateCounters())
    found = _find_timing_counter(entries, params.get("counter_id"))
    if found is None:
        return {
            "supported": False,
            "timing_counter": None,
            "error_message": (
                "this capture's replay exposes no GPU timing counter"
                if params.get("counter_id") is None
                else "counter {} is not exposed by this capture's replay".format(
                    _int(params.get("counter_id"))
                )
            ),
            "counter_count": len(entries),
            "available_counters": [info for _native, info in entries[:MAX_ITEMS]],
            "hint": "call renderdoc_perf__list_counters to see what this driver exposes, "
            "or renderdoc_analysis__analyze_render_passes for the structure without "
            "durations",
            "passes": [],
            "totals": None,
        }
    native, info = found
    durations = {}
    for value in controller.FetchCounters([native]):
        sampled = _counter_value(value, info.get("result_type", ""))
        if isinstance(sampled, (int, float)) and not isinstance(sampled, bool):
            durations[_int(value.eventId)] = sampled
    nodes = _action_nodes(controller, rd, max_depth)
    rows = []
    for node in nodes:
        row = dict(node)
        row["duration"] = durations.get(node["event_id"])
        rows.append(row)
    ancestors = _ancestor_map(rows)
    passes = []
    for row in rows:
        if row["depth"] != pass_depth:
            continue
        duration, method, timed = _pass_duration(row, rows, durations, ancestors)
        descendants = [
            other
            for other in rows
            if other["event_id"] != row["event_id"]
            and row["event_id"] in ancestors.get(other["event_id"], ())
        ]
        slowest = sorted(
            (other for other in descendants if other["duration"] is not None),
            key=lambda other: other["duration"],
            reverse=True,
        )[:slowest_limit]
        passes.append(
            {
                "event_id": row["event_id"],
                "name": row["name"],
                "duration": duration,
                "duration_method": method,
                "derived": method != "pass_event_counter",
                "action_count": len(descendants) + 1,
                "timed_action_count": timed,
                "untimed_action_count": len(descendants) + 1 - timed,
                "slowest_actions": [
                    {
                        "event_id": other["event_id"],
                        "name": other["name"],
                        "duration": other["duration"],
                    }
                    for other in slowest
                ],
            }
        )
    passes.sort(key=lambda entry: entry["duration"], reverse=True)
    total = sum(entry["duration"] for entry in passes)
    for entry in passes:
        entry["share_percent"] = float(entry["duration"] / total * 100.0) if total else None
    values = [entry["duration"] for entry in passes]
    return {
        "supported": True,
        "timing_counter": info,
        "unit": info.get("unit"),
        "pass_depth": pass_depth,
        "max_depth": max_depth,
        "offset": offset,
        "limit": limit,
        "pass_count": len(passes),
        "truncated": len(passes) > offset + limit,
        "passes": passes[offset : offset + limit],
        "totals": {
            "unit": info.get("unit"),
            "pass_total": float(total),
            "timed_event_count": len(durations),
            "min": float(min(values)) if values else None,
            "max": float(max(values)) if values else None,
            "mean": float(sum(values) / len(values)) if values else None,
        },
        "estimate_fields": {
            "passes[].duration": "the pass event's own counter sample when the driver "
            "sampled it, otherwise the sum of the pass's timed actions; the method "
            "used is reported per pass",
        },
    }


def _decode_vertices(raw, stride, comp_count, limit=MAX_VERTICES):
    if stride <= 0 or comp_count <= 0:
        return []
    vertices = []
    count = min(len(raw) // stride, limit)
    for index in range(count):
        base = index * stride
        chunk = raw[base : base + comp_count * 4]
        if len(chunk) < comp_count * 4:
            break
        vertices.append([float(value) for value in struct.unpack("<{}f".format(comp_count), chunk)])
    return vertices


def _op_get_mesh_data(controller, rd, params, context):
    del context
    event_id = _set_event(controller, params.get("event_id"))
    stage_name = _text(params.get("stage", "") or "VSOut").strip() or "VSOut"
    stage = getattr(rd.MeshDataStage, stage_name, None)
    if stage is None:
        choices = ", ".join(_MESH_STAGES)
        raise ValueError("stage must be one of these values: " + choices)
    instance = _clamp(params.get("instance"), 0, 65535, 0)
    view = _clamp(params.get("view"), 0, 65535, 0)
    max_vertices = _clamp(params.get("max_vertices"), 1, MAX_VERTICES, 256)
    mesh = controller.GetPostVSData(instance, view, stage)
    info = {
        "event_id": event_id,
        "stage": stage_name,
        "instance": instance,
        "view": view,
        "status": _enum(getattr(mesh, "status", "")),
        "num_indices": _int(mesh.numIndices),
        "topology": _enum(getattr(mesh, "topology", "")),
        "base_vertex": _int(mesh.baseVertex),
        "vertex_resource_id": _int(mesh.vertexResourceId),
        "vertex_byte_offset": _int(mesh.vertexByteOffset),
        "vertex_byte_stride": _int(mesh.vertexByteStride),
        "index_resource_id": _int(mesh.indexResourceId),
        "index_byte_offset": _int(mesh.indexByteOffset),
        "index_byte_stride": _int(mesh.indexByteStride),
        "instanced": bool(getattr(mesh, "instanced", False)),
        "unproject": bool(getattr(mesh, "unproject", False)),
        "near_plane": float(getattr(mesh, "nearPlane", 0.0)),
        "far_plane": float(getattr(mesh, "farPlane", 0.0)),
    }
    vertex_format = getattr(mesh, "format", None)
    comp_count = 0
    if vertex_format is not None:
        info["vertex_format"] = (
            _text(vertex_format.Name()) if hasattr(vertex_format, "Name") else ""
        )
        info["comp_type"] = _enum(getattr(vertex_format, "compType", ""))
        comp_count = _int(getattr(vertex_format, "compCount", 0))
        info["comp_count"] = comp_count
    stride = _int(mesh.vertexByteStride)
    if stride <= 0 and comp_count > 0:
        stride = comp_count * 4
    if _int(mesh.vertexResourceId) == 0:
        info["vertices"] = []
        info["vertex_note"] = "RenderDoc reported no vertex buffer for this stage"
        return info
    raw = controller.GetBufferData(
        mesh.vertexResourceId,
        _int(mesh.vertexByteOffset),
        min(_int(mesh.vertexByteSize), stride * max_vertices) if stride > 0 else 0,
    )
    raw = bytes(raw) if raw is not None else b""
    info["vertices"] = _decode_vertices(raw, stride, comp_count)[:max_vertices]
    info["vertex_note"] = (
        None
        if comp_count > 0
        else "vertex component count unavailable; vertices decoded as float32 triples"
    )
    return info


def _require_debugging(controller):
    """Reject shader debugging when this capture's replay cannot step a shader."""
    properties = controller.GetAPIProperties()
    if not bool(getattr(properties, "shaderDebugging", False)):
        raise RuntimeError("this capture's replay does not support shader debugging")


def _debug_detail(params):
    """Resolve the trace-vs-summary payload selector shared by the debug tools."""
    detail = _text(params.get("detail", "") or "trace").strip().casefold() or "trace"
    if detail not in ("trace", "summary"):
        raise ValueError("detail must be 'trace' or 'summary'")
    return detail


def _drive_debug(controller, trace):
    """Step RenderDoc's debugger until the trace is complete and return its states.

    ``ContinueDebug`` hands back one batch of ``ShaderDebugState`` per call and an
    empty batch once the shader has finished simulating, so those batches are the
    only place the steps ever exist -- ``ShaderDebugTrace`` carries no ``states``
    member at all. Collection therefore stops on the first empty batch, or once
    ``MAX_DEBUG_STEPS`` states have been gathered. An empty collection afterwards
    is a failure to step, not an empty shader.
    """
    debugger = getattr(trace, "debugger", None)
    if debugger is None:
        raise RuntimeError(
            "RenderDoc returned a shader debug trace without a debugger, so this "
            "invocation cannot be stepped"
        )
    states = []
    while len(states) < MAX_DEBUG_STEPS:
        batch = list(controller.ContinueDebug(debugger) or ())
        if not batch:
            break
        states.extend(batch)
    return states[:MAX_DEBUG_STEPS]


def _trace_info(controller, trace, max_steps, detail, facts):
    """Serialise one shader debug trace and always hand it back to RenderDoc.

    The trace is stepped to completion before anything is read out of it, and a
    trace that ends up with no states is reported as a failure rather than as a
    successful empty result.

    ``detail`` selects the payload: ``trace`` returns every captured step up to
    ``max_steps``, ``summary`` returns only the trace shape — stage, step count,
    inputs, and source variables — so a caller can decide whether the full trace
    is worth fetching.
    """
    if trace is None:
        raise RuntimeError("RenderDoc returned no shader debug trace for this invocation")
    info = dict(facts)
    info["stage"] = _enum(getattr(trace, "stage", ""))
    info["detail"] = detail
    try:
        states = _drive_debug(controller, trace)
        if not states:
            raise RuntimeError(
                "RenderDoc stepped this invocation but recorded no debug states, "
                "so there is no trace to report"
            )
        info["step_count"] = len(states)
        info["truncated"] = len(states) > max_steps
        if detail == "trace":
            steps = []
            for state in states[:max_steps]:
                steps.append(
                    {
                        "step_index": _int(state.stepIndex),
                        "next_instruction": _int(state.nextInstruction),
                        "flags": _enum(getattr(state, "flags", "")),
                        "changes": _describe(getattr(state, "changes", None)),
                    }
                )
            info["steps"] = steps
        info["inputs"] = _describe(getattr(trace, "inputs", None))
        info["source_vars"] = _describe(getattr(trace, "sourceVars", None))
    finally:
        try:
            controller.FreeTrace(trace)
        except BaseException:
            pass
    return info


def _uint3(params, key):
    """RenderDoc's workgroup and thread selectors are three uint32 values."""
    values = params.get(key) or []
    if not isinstance(values, (list, tuple)):
        raise ValueError("{} must be a list of three integers".format(key))
    clamped = []
    for index in range(3):
        clamped.append(_clamp(values[index] if index < len(values) else 0, 0, 1 << 30, 0))
    return clamped


def _pixel_value(value):
    """Read one RenderDoc ``PixelValue`` — the contents of a pixel.

    ``PickPixel`` answers "what is in this pixel": four channels decoded as
    float, unsigned, and signed integers, and nothing about the draw that put
    them there. Anything about *which* draw wrote the pixel has to come from
    ``PixelHistory``, which is the only source of ``PixelModification``.
    """
    if value is None:
        return None
    channels = {}
    for name in ("floatValue", "uintValue", "intValue"):
        raw = getattr(value, name, None)
        if raw is None:
            continue
        try:
            channels[name] = [float(item) if name == "floatValue" else _int(item) for item in raw]
        except BaseException:
            channels[name] = None
    return channels or None


def _modification_entry(modification):
    """Serialise one RenderDoc ``PixelModification`` from a pixel history."""
    try:
        passed = bool(modification.Passed())
    except BaseException:
        passed = None
    return {
        "event_id": _int(modification.eventId),
        "primitive_id": _int(modification.primitiveID),
        "frag_index": _int(modification.fragIndex),
        "passed": passed,
        "unbound_ps": bool(getattr(modification, "unboundPS", False)),
        "shader_discarded": bool(getattr(modification, "shaderDiscarded", False)),
        "depth_test_failed": bool(getattr(modification, "depthTestFailed", False)),
        "stencil_test_failed": bool(getattr(modification, "stencilTestFailed", False)),
        "scissor_clipped": bool(getattr(modification, "scissorClipped", False)),
        "backface_culled": bool(getattr(modification, "backfaceCulled", False)),
        "pre_mod": _describe(getattr(modification, "preMod", None)),
        "post_mod": _describe(getattr(modification, "postMod", None)),
        "shader_out": _describe(getattr(modification, "shaderOut", None)),
    }


def _op_pick_pixel(controller, rd, params, context):
    """Report the last draw that actually landed on one pixel.

    ``PickPixel`` cannot attribute a pixel to a draw: it returns a
    ``PixelValue``, the pixel's contents. So the draw comes from the pixel
    history — the last modification that passed the depth and stencil tests is
    the draw that last wrote this coordinate — and ``PickPixel`` is used only for
    the value that is sitting in the pixel now.
    """
    del context
    event_id = _set_event(controller, params.get("event_id"))
    resource_id = _int(params.get("resource_id"))
    if resource_id <= 0:
        raise ValueError("resource_id must be a positive integer")
    x = _clamp(params.get("x"), 0, 1 << 20, 0)
    y = _clamp(params.get("y"), 0, 1 << 20, 0)
    properties = controller.GetAPIProperties()
    if not bool(getattr(properties, "pixelHistory", False)):
        raise RuntimeError("this capture's replay does not support pixel history")
    native_id = _lookup_resource_id(controller, resource_id)
    subresource = _subresource(rd, params)
    type_cast = _comp_type(rd, params)
    modifications = controller.PixelHistory(native_id, x, y, subresource, type_cast)
    hit = None
    for modification in modifications:
        entry = _modification_entry(modification)
        if entry["passed"]:
            hit = entry
    result = {
        "event_id": event_id,
        "resource_id": resource_id,
        "x": x,
        "y": y,
        "modification_count": len(modifications),
        "hit_event_id": None if hit is None else hit["event_id"],
        "hit": hit,
        "pixel_value": _pixel_value(controller.PickPixel(native_id, x, y, subresource, type_cast)),
    }
    if hit is None:
        result["hit_note"] = (
            "no draw passed the depth and stencil tests at this coordinate in the "
            "events replayed up to event {}".format(event_id)
        )
    return result


def _mesh_indices(controller, mesh):
    """Read the post-VS index buffer, when the stage reported one.

    Returns ``(indices, note)``. ``GetBufferData`` can hand back fewer bytes than
    were asked for, and decoding the full requested count would then raise and
    throw away the whole decodable prefix, so the pattern is sized to the bytes
    that actually arrived and a short read is reported in ``note`` instead of
    collapsing to an empty index list.
    """
    stride = _int(getattr(mesh, "indexByteStride", 0))
    count = min(_int(getattr(mesh, "numIndices", 0)), MAX_INDICES)
    if _int(getattr(mesh, "indexResourceId", 0)) == 0 or count <= 0 or stride not in (2, 4):
        return [], None
    raw = bytes(
        controller.GetBufferData(
            mesh.indexResourceId,
            _int(getattr(mesh, "indexByteOffset", 0)),
            count * stride,
        )
        or b""
    )
    usable = min(count, len(raw) // stride)
    if usable <= 0:
        return [], "RenderDoc returned no index bytes for this stage"
    if usable < count:
        note = "RenderDoc returned {} of {} index bytes; decoded {} of {} indices".format(
            len(raw), count * stride, usable, count
        )
    else:
        note = None
    pattern = "<{}H".format(usable) if stride == 2 else "<{}I".format(usable)
    try:
        return [int(value) for value in struct.unpack(pattern, raw[: usable * stride])], note
    except BaseException as exc:
        return [], "could not decode the index buffer: {}: {}".format(type(exc).__name__, exc)


def _write_obj(path, vertices, indices):
    """Write a Wavefront mesh, dropping faces that address a missing vertex.

    OBJ face indices are 1-based and have to address a vertex that was actually
    written. An index buffer is frequently wider than the vertex window this
    export fetched — ``max_vertices`` caps vertices at 4096 while
    ``MAX_INDICES`` allows 65536 — so faces pointing outside the window are
    dropped rather than written as references no loader will accept.
    """
    vertex_count = len(vertices)
    faces = 0
    dropped = 0
    with open(path, "w") as stream:
        stream.write("# post-VS mesh exported by dcc-mcp-renderdoc\n")
        for vertex in vertices:
            components = " ".join("{:.6f}".format(float(value)) for value in vertex)
            stream.write("v {}\n".format(components))
        for start in range(0, len(indices) - len(indices) % 3, 3):
            face = [int(value) + 1 for value in indices[start : start + 3]]
            if any(value < 1 or value > vertex_count for value in face):
                dropped += 1
                continue
            stream.write("f {} {} {}\n".format(face[0], face[1], face[2]))
            faces += 1
    return faces, dropped


def _write_json(path, document):
    with open(path, "w") as stream:
        json.dump(document, stream)


def _op_export_mesh(controller, rd, params, context):
    del context
    event_id = _set_event(controller, params.get("event_id"))
    output_file = _text(params.get("output_file", "") or "")
    if not output_file:
        raise ValueError("output_file is required")
    export_format = _MESH_EXPORT_FORMATS.get(os.path.splitext(output_file)[1].casefold())
    if export_format is None:
        choices = ", ".join(sorted(_MESH_EXPORT_FORMATS))
        raise ValueError("output_file must use one of these extensions: " + choices)
    directory = os.path.dirname(os.path.abspath(output_file))
    if not os.path.isdir(directory):
        raise ValueError("output directory does not exist: {}".format(directory))
    if not _replay_capabilities(controller).get("post_vs_data"):
        raise RuntimeError("this capture's replay does not support post-VS data")
    stage_name = _text(params.get("stage", "") or "VSOut").strip() or "VSOut"
    stage = getattr(rd.MeshDataStage, stage_name, None)
    if stage is None:
        choices = ", ".join(_MESH_STAGES)
        raise ValueError("stage must be one of these values: " + choices)
    instance = _clamp(params.get("instance"), 0, 65535, 0)
    view = _clamp(params.get("view"), 0, 65535, 0)
    max_vertices = _clamp(params.get("max_vertices"), 1, MAX_VERTICES, MAX_VERTICES)
    preview_vertices = _clamp(params.get("preview_vertices"), 0, 64, 8)
    mesh = controller.GetPostVSData(instance, view, stage)
    vertex_format = getattr(mesh, "format", None)
    comp_count = _int(getattr(vertex_format, "compCount", 0)) if vertex_format is not None else 0
    stride = _int(getattr(mesh, "vertexByteStride", 0))
    if stride <= 0 and comp_count > 0:
        stride = comp_count * 4
    vertices = []
    if _int(getattr(mesh, "vertexResourceId", 0)) != 0 and stride > 0:
        raw = bytes(
            controller.GetBufferData(
                mesh.vertexResourceId,
                _int(getattr(mesh, "vertexByteOffset", 0)),
                min(_int(getattr(mesh, "vertexByteSize", 0)), stride * max_vertices),
            )
            or b""
        )
        vertices = _decode_vertices(raw, stride, comp_count)[:max_vertices]
    indices, index_note = _mesh_indices(controller, mesh)
    document = {
        "event_id": event_id,
        "stage": stage_name,
        "instance": instance,
        "view": view,
        "status": _enum(getattr(mesh, "status", "")),
        "topology": _enum(getattr(mesh, "topology", "")),
        "base_vertex": _int(getattr(mesh, "baseVertex", 0)),
        "vertex_format": _text(vertex_format.Name()) if hasattr(vertex_format, "Name") else "",
        "comp_type": _enum(getattr(vertex_format, "compType", ""))
        if vertex_format is not None
        else "",
        "comp_count": comp_count,
        "vertices": vertices,
        "indices": indices,
    }
    if export_format == "obj":
        face_count, dropped_faces = _write_obj(output_file, vertices, indices)
    else:
        face_count, dropped_faces = len(indices) // 3, 0
        _write_json(output_file, document)
    if not os.path.isfile(output_file):
        raise RuntimeError("RenderDoc did not create {}".format(output_file))
    result = {key: value for key, value in document.items() if key not in ("vertices", "indices")}
    result.update(
        {
            "vertex_resource_id": _int(getattr(mesh, "vertexResourceId", 0)),
            "vertex_byte_stride": stride,
            "index_resource_id": _int(getattr(mesh, "indexResourceId", 0)),
            "num_indices": _int(getattr(mesh, "numIndices", 0)),
            "vertex_count": len(vertices),
            "index_count": len(indices),
            "face_count": face_count,
            "dropped_faces": dropped_faces,
            "index_note": index_note,
            "truncated": len(vertices) >= max_vertices,
            "output_file": output_file,
            "output_format": export_format,
            "size_bytes": int(os.path.getsize(output_file)),
            "vertex_preview": vertices[:preview_vertices],
        }
    )
    return result


def _op_get_pixel_history(controller, rd, params, context):
    del context
    event_id = _set_event(controller, params.get("event_id"))
    resource_id = _int(params.get("resource_id"))
    if resource_id <= 0:
        raise ValueError("resource_id must be a positive integer")
    x = _clamp(params.get("x"), 0, 1 << 20, 0)
    y = _clamp(params.get("y"), 0, 1 << 20, 0)
    limit = _clamp(params.get("limit"), 1, 5000, 200)
    properties = controller.GetAPIProperties()
    if not bool(getattr(properties, "pixelHistory", False)):
        raise RuntimeError("this capture's replay does not support pixel history")
    modifications = controller.PixelHistory(
        _lookup_resource_id(controller, resource_id),
        x,
        y,
        _subresource(rd, params),
        _comp_type(rd, params),
    )
    entries = []
    for modification in modifications[:limit]:
        entries.append(_modification_entry(modification))
    return {
        "event_id": event_id,
        "resource_id": resource_id,
        "x": x,
        "y": y,
        "limit": limit,
        "modification_count": len(modifications),
        "truncated": len(modifications) > limit,
        "modifications": entries,
    }


def _op_debug_pixel(controller, rd, params, context):
    del context
    event_id = _set_event(controller, params.get("event_id"))
    _require_debugging(controller)
    x = _clamp(params.get("x"), 0, 1 << 20, 0)
    y = _clamp(params.get("y"), 0, 1 << 20, 0)
    max_steps = _clamp(params.get("max_steps"), 1, MAX_DEBUG_STEPS, 200)
    inputs = rd.DebugPixelInputs()
    inputs.sample = _clamp(params.get("sample"), 0, 65535, 0)
    inputs.primitive = _clamp(params.get("primitive"), 0, 1 << 30, 0)
    inputs.view = _clamp(params.get("view"), 0, 65535, 0)
    trace = controller.DebugPixel(x, y, inputs)
    return _trace_info(
        controller,
        trace,
        max_steps,
        _debug_detail(params),
        {"event_id": event_id, "x": x, "y": y},
    )


def _op_debug_vertex(controller, rd, params, context):
    """Step the vertex shader for one vertex of one instance.

    RenderDoc's ``DebugVertex`` takes exactly four selectors — ``vertid``,
    ``instid``, ``idx``, ``view`` — and applies no drawcall offsets of its own.
    A caller that wants a specific vertex of a specific instance therefore has to
    fold the draw's ``vertex_offset`` and ``instance_offset`` into
    ``vertex_index``, ``instance``, and ``index`` first;
    ``renderdoc_inspect__get_action`` reports those offsets.
    """
    del rd, context
    event_id = _set_event(controller, params.get("event_id"))
    _require_debugging(controller)
    max_steps = _clamp(params.get("max_steps"), 1, MAX_DEBUG_STEPS, 200)
    selector = {
        "vertex_index": _clamp(params.get("vertex_index"), 0, 1 << 30, 0),
        "instance": _clamp(params.get("instance"), 0, 65535, 0),
        "index": _clamp(params.get("index"), 0, 1 << 30, 0),
        "view": _clamp(params.get("view"), 0, 65535, 0),
    }
    trace = controller.DebugVertex(
        selector["vertex_index"],
        selector["instance"],
        selector["index"],
        selector["view"],
    )
    selector["event_id"] = event_id
    return _trace_info(controller, trace, max_steps, _debug_detail(params), selector)


def _op_debug_thread(controller, rd, params, context):
    del rd, context
    event_id = _set_event(controller, params.get("event_id"))
    _require_debugging(controller)
    max_steps = _clamp(params.get("max_steps"), 1, MAX_DEBUG_STEPS, 200)
    group_id = _uint3(params, "group_id")
    thread_id = _uint3(params, "thread_id")
    trace = controller.DebugThread(group_id, thread_id)
    return _trace_info(
        controller,
        trace,
        max_steps,
        _debug_detail(params),
        {"event_id": event_id, "group_id": group_id, "thread_id": thread_id},
    )


def _counter_number(raw, as_float):
    try:
        return float(raw) if as_float else int(raw)
    except (TypeError, ValueError):
        return None


def _counter_value(value, result_type):
    """Read one ``CounterResult`` union through the type RenderDoc reported.

    A ``CounterResult`` carries every union member at once, so handing the whole
    union to the host would ship a bag of aliases with no way to tell which one
    is meaningful. The counter's ``resultType`` names the member, so this
    returns one number -- or, for a type this bridge does not recognise, the
    described union, so the sample still arrives as data instead of as ``None``.
    """
    union = getattr(value, "value", value)
    text = _text(result_type)
    for marker, member, narrower, as_float in _COUNTER_VALUE_MEMBERS:
        if marker not in text:
            continue
        raw = getattr(union, member, None)
        if raw is None and narrower is not None:
            raw = getattr(union, narrower, None)
        if raw is not None:
            return _counter_number(raw, as_float)
    return _describe(union)


def _counter_descriptions(controller, counters):
    """Describe counters as ``(native, info)`` pairs, keeping the native handle.

    ``FetchCounters`` accepts only the native objects ``EnumerateCounters``
    returned, so a caller that wants to fetch has to keep them beside the
    JSON-ready description instead of re-deriving them from the integer IDs.
    """
    entries = []
    for counter in counters:
        info = {"id": _int(counter)}
        try:
            description = controller.DescribeCounter(counter)
        except BaseException as exc:
            info["error"] = "{}: {}".format(type(exc).__name__, exc)
            entries.append((counter, info))
            continue
        info.update(
            {
                "name": _text(description.name),
                "category": _text(description.category),
                "description": _text(description.description),
                "unit": _enum(description.unit),
                "result_type": _enum(getattr(description, "resultType", "")),
            }
        )
        entries.append((counter, info))
    return entries


def _find_timing_counter(entries, requested_id=None):
    """Pick the counter that carries per-event GPU duration, if this capture has one.

    RenderDoc's duration counter has no stable ID across APIs and drivers, so the
    choice is made by name, most specific marker first. An explicit
    ``requested_id`` overrides the scan, which is how a caller that already ran
    ``list_counters`` pins the exact counter it wants.
    """
    if requested_id is not None:
        wanted = _int(requested_id)
        for native, info in entries:
            if info["id"] == wanted:
                return native, info
        return None
    for marker in _TIMING_COUNTER_MARKERS:
        for native, info in entries:
            if marker in _text(info.get("name", "")).casefold():
                return native, info
    return None


def _perf_counters(controller, params):
    """Enumerate and describe counters, applying the shared name/category filters."""
    entries = _counter_descriptions(controller, controller.EnumerateCounters())
    name_filter = _text(params.get("name_filter", "") or "").strip().casefold()
    category_filter = _text(params.get("category_filter", "") or "").strip().casefold()
    if not name_filter and not category_filter:
        return entries
    kept = []
    for native, info in entries:
        if name_filter and name_filter not in _text(info.get("name", "")).casefold():
            continue
        if category_filter and category_filter not in _text(info.get("category", "")).casefold():
            continue
        kept.append((native, info))
    return kept


def _op_get_counters(controller, rd, params, context):
    """Describe the counters this replay exposes, and optionally fetch their values.

    ``list_counters`` drives this with ``fetch`` unset to read the catalogue;
    ``fetch_counters`` sets it to sample the selected counters, optionally
    narrowed to one event range, so a caller does not have to take the whole
    frame when one draw is what it is profiling.
    """
    del rd, context
    event_id = _set_event(controller, params.get("event_id"))
    limit = _clamp(params.get("limit"), 1, 5000, 200)
    entries = _perf_counters(controller, params)
    result = {
        "event_id": event_id,
        "limit": limit,
        "counter_count": len(entries),
        "truncated": len(entries) > limit,
        "counters": [info for _native, info in entries[:limit]],
        "values": None,
    }
    if params.get("fetch"):
        requested = params.get("counter_ids") or []
        if requested:
            wanted = set(_int(item) for item in requested)
            selected = [native for native, info in entries if info["id"] in wanted]
            missing = sorted(wanted - set(info["id"] for _native, info in entries))
        else:
            selected = [native for native, _info in entries]
            missing = []
        result["fetched_counter_ids"] = [_int(native) for native in selected]
        result["missing_counter_ids"] = missing
        types = {}
        for _native, info in entries:
            types[info["id"]] = info.get("result_type", "")
        first_event = params.get("first_event_id")
        last_event = params.get("last_event_id")
        first_event = None if first_event is None else _int(first_event)
        last_event = None if last_event is None else _int(last_event)
        values = []
        matched = 0
        for value in controller.FetchCounters(selected):
            counter_id = _int(value.counter)
            occurrence = _int(value.eventId)
            if first_event is not None and occurrence < first_event:
                continue
            if last_event is not None and occurrence > last_event:
                continue
            matched += 1
            if len(values) < MAX_ITEMS:
                values.append(
                    {
                        "counter": counter_id,
                        "event_id": occurrence,
                        "value": _counter_value(value, types.get(counter_id, "")),
                    }
                )
        result["value_count"] = matched
        result["values"] = values
        result["values_truncated"] = matched > len(values)
        result["first_event_id"] = first_event
        result["last_event_id"] = last_event
    return result


def _op_get_debug_messages(controller, rd, params, context):
    """Report the debug, warning, and error messages a replay produced.

    RenderDoc accumulates these as the replay progresses, so an ``event_id``
    replays up to that event first and the messages reported are the ones the
    frame had produced by then.
    """
    del rd, context
    event_id = _set_event(controller, params.get("event_id"))
    limit = _clamp(params.get("limit"), 1, 5000, 200)
    offset = _clamp(params.get("offset"), 0, 10000000, 0)
    severity_filter = _text(params.get("severity_filter", "") or "").strip().casefold()
    category_filter = _text(params.get("category_filter", "") or "").strip().casefold()
    messages = controller.GetDebugMessages()
    matched = []
    for message in messages:
        severity = _enum(getattr(message, "severity", ""))
        category = _enum(getattr(message, "category", ""))
        if severity_filter and severity_filter not in _text(severity).casefold():
            continue
        if category_filter and category_filter not in _text(category).casefold():
            continue
        matched.append(
            {
                "event_id": _int(getattr(message, "eventId", 0)),
                "category": category,
                "severity": severity,
                "source": _enum(getattr(message, "source", "")),
                "id": _int(getattr(message, "messageID", 0)),
                "description": _text(getattr(message, "description", "")),
            }
        )
    return {
        "event_id": event_id,
        "offset": offset,
        "limit": limit,
        "severity_filter": severity_filter or None,
        "category_filter": category_filter or None,
        "message_count": len(matched),
        "unfiltered_message_count": len(messages),
        "truncated": len(matched) > offset + limit,
        "messages": matched[offset : offset + limit],
    }


def _op_describe_perf(controller, rd, params, context):
    """Report what the performance tools can do with this capture right now.

    The perf tools are gated on two different things: the deep backend (checked
    host-side) and per-capture facts only a replay can answer -- whether this
    driver exposes counters at all, whether one of them carries GPU duration,
    and whether post-VS geometry can be read back. All three are read in one
    replay so ``perf_capabilities`` costs a single launch.
    """
    del rd, params, context
    entries = _counter_descriptions(controller, controller.EnumerateCounters())
    timing = _find_timing_counter(entries)
    replay = _replay_capabilities(controller)
    return {
        "replay": replay,
        "counter_count": len(entries),
        "timing_counter": None if timing is None else timing[1],
        "counters": [info for _native, info in entries[:MAX_ITEMS]],
        "counter_truncated": len(entries) > MAX_ITEMS,
        "flags": {
            "counters": len(entries) > 0,
            "timing": timing is not None,
            "post_vs_data": bool(replay.get("post_vs_data")),
        },
    }


def _op_get_action_timing(controller, rd, params, context):
    """Report per-action GPU duration and the aggregates over them.

    RenderDoc stores no duration on an action: the number comes from the timing
    counter this driver exposes, sampled per event and joined back onto the
    action tree. When no timing counter exists the result says so explicitly and
    lists the counters that are available, rather than reporting an empty frame.
    """
    del context
    max_depth = _clamp(params.get("max_depth"), 0, 32, 32)
    offset = _clamp(params.get("offset"), 0, 10000000, 0)
    limit = _clamp(params.get("limit"), 1, 20000, 200)
    slowest_limit = _clamp(params.get("slowest"), 0, 500, 20)
    name_filter = _text(params.get("name_filter", "") or "").strip().casefold()
    flag_filter = _text(params.get("flag_filter", "") or "").strip().casefold()
    entries = _counter_descriptions(controller, controller.EnumerateCounters())
    found = _find_timing_counter(entries, params.get("counter_id"))
    if found is None:
        return {
            "supported": False,
            "timing_counter": None,
            "error_message": (
                "this capture's replay exposes no GPU timing counter"
                if params.get("counter_id") is None
                else "counter {} is not exposed by this capture's replay".format(
                    _int(params.get("counter_id"))
                )
            ),
            "counter_count": len(entries),
            "available_counters": [info for _native, info in entries[:MAX_ITEMS]],
            "hint": "call list_counters to see what this driver exposes",
            "actions": [],
            "slowest": [],
            "passes": [],
        }
    native, info = found
    durations = {}
    for value in controller.FetchCounters([native]):
        sampled = _counter_value(value, info.get("result_type", ""))
        # Only keep real numbers. A counter whose union carries no readable
        # member for its declared type would otherwise land here as a dict of
        # raw members and blow up with a TypeError further down, where the
        # totals are summed.
        if isinstance(sampled, (int, float)) and not isinstance(sampled, bool):
            durations[_int(value.eventId)] = sampled
    rows = []

    def visit(summary):
        if name_filter and name_filter not in summary["name"].casefold():
            return
        if flag_filter and flag_filter not in [name.casefold() for name in summary["flag_names"]]:
            return
        event_id = summary["event_id"]
        if event_id not in durations:
            return
        rows.append(
            {
                "event_id": event_id,
                "action_id": summary["action_id"],
                "parent_event_id": summary["parent_event_id"],
                "name": summary["name"],
                "duration": durations[event_id],
            }
        )

    _walk_actions(controller, rd, controller.GetRootActions(), 0, max_depth, None, visit)
    timed = [row for row in rows if row["duration"] is not None]
    values = [row["duration"] for row in timed]
    ranked = sorted(timed, key=lambda row: row["duration"], reverse=True)
    by_event = dict((row["event_id"], row) for row in timed)
    passes = {}
    for row in timed:
        top = row
        while top["parent_event_id"] is not None:
            parent = by_event.get(top["parent_event_id"])
            if parent is None:
                break
            top = parent
        bucket = passes.setdefault(
            top["event_id"],
            {"event_id": top["event_id"], "name": top["name"], "count": 0, "total": 0.0},
        )
        bucket["count"] += 1
        bucket["total"] += float(row["duration"])
    return {
        "supported": True,
        "timing_counter": info,
        "unit": info.get("unit"),
        "offset": offset,
        "limit": limit,
        "max_depth": max_depth,
        "action_count": len(timed),
        "matched_count": len(rows),
        "truncated": len(rows) > offset + limit,
        "actions": rows[offset : offset + limit],
        "slowest": ranked[:slowest_limit],
        "passes": sorted(passes.values(), key=lambda item: item["total"], reverse=True),
        "totals": {
            "unit": info.get("unit"),
            "total": float(sum(values)) if values else 0.0,
            "mean": float(sum(values) / len(values)) if values else 0.0,
            "min": float(min(values)) if values else 0.0,
            "max": float(max(values)) if values else 0.0,
        },
    }


def _draw_actions(controller, rd, params, max_draws):
    """Collect the draw events an overdraw analysis should rasterise.

    Draws are the only events that shade pixels, so dispatches, clears, copies,
    and bare marker regions are left out. A draw is recognised by its decoded
    ``Drawcall`` flag, falling back to a non-zero index count for RenderDoc
    builds that do not advertise the flag enum.
    """
    first_event = params.get("first_event_id")
    last_event = params.get("last_event_id")
    first_event = None if first_event is None else _int(first_event)
    last_event = None if last_event is None else _int(last_event)
    wanted = set(_int(item) for item in (params.get("event_ids") or []))
    selected = []
    candidates = [0]

    def visit(summary):
        event_id = summary["event_id"]
        if wanted and event_id not in wanted:
            return
        if first_event is not None and event_id < first_event:
            return
        if last_event is not None and event_id > last_event:
            return
        if "Drawcall" not in summary["flag_names"] and _int(summary["num_indices"]) <= 0:
            return
        candidates[0] += 1
        if len(selected) < max_draws:
            selected.append(summary)

    _walk_actions(controller, rd, controller.GetRootActions(), 0, 32, None, visit)
    return selected, candidates[0]


def _post_vs_geometry(controller, mesh, max_vertices):
    """Read one post-VS stage as ``(vertices, indices, note)``.

    RenderDoc reports no index buffer for a non-indexed draw, so the indices are
    then synthesised from the vertex count instead of collapsing the draw into
    empty geometry; the synthesised case is reported in ``note``.
    """
    vertex_format = getattr(mesh, "format", None)
    comp_count = _int(getattr(vertex_format, "compCount", 0)) if vertex_format is not None else 0
    stride = _int(getattr(mesh, "vertexByteStride", 0))
    if stride <= 0 and comp_count > 0:
        stride = comp_count * 4
    if _int(getattr(mesh, "vertexResourceId", 0)) == 0 or stride <= 0:
        return [], [], "RenderDoc reported no post-VS vertex buffer for this draw"
    byte_size = _int(getattr(mesh, "vertexByteSize", 0))
    if byte_size <= 0:
        byte_size = stride * max_vertices
    raw = bytes(
        controller.GetBufferData(
            mesh.vertexResourceId, _int(getattr(mesh, "vertexByteOffset", 0)), byte_size
        )
        or b""
    )
    vertices = _decode_vertices(raw, stride, comp_count, max_vertices)
    indices, note = _mesh_indices(controller, mesh)
    if not indices:
        count = min(len(vertices), _int(getattr(mesh, "numIndices", 0)))
        if count > 0:
            indices = list(range(count))
            note = note or "RenderDoc reported no index buffer; synthesised sequential indices"
    return vertices[:max_vertices], indices, note


def _triangle_screen_coords(vertices, indices, width, height, depth_scale, depth_bias):
    """Project post-VS vertices to pixel coordinates, one triple per triangle.

    Post-VS data is clip space, so each vertex is perspective-divided by ``w``
    before it is mapped to the grid. A vertex with a non-positive ``w`` sits
    behind the eye, so the triangles that reference it are dropped rather than
    mirrored into the frame as geometry that was never visible.
    """
    projected = []
    for vertex in vertices:
        if len(vertex) < 2:
            projected.append(None)
            continue
        w = vertex[3] if len(vertex) > 3 else 1.0
        if w <= 0:
            projected.append(None)
            continue
        projected.append(
            (
                (vertex[0] / w * 0.5 + 0.5) * width,
                (1.0 - (vertex[1] / w * 0.5 + 0.5)) * height,
                ((vertex[2] / w) if len(vertex) > 2 else 0.0) * depth_scale + depth_bias,
            )
        )
    triangles = []
    for start in range(0, len(indices) - len(indices) % 3, 3):
        triple = []
        for index in indices[start : start + 3]:
            if index < 0 or index >= len(projected) or projected[index] is None:
                triple = None
                break
            triple.append(projected[index])
        if triple is not None:
            triangles.append(triple)
    return triangles


def _owns_edge(dx, dy):
    """Top-left fill rule, expressed on the directed edge itself.

    Two triangles that share an edge traverse it in opposite directions, and this
    predicate is true for exactly one of ``d`` and ``-d``, so a pixel centre
    landing exactly on a shared edge is counted by one triangle and not both.
    That is what keeps the seam between a quad's two triangles out of the
    overdraw count: without it a single fullscreen quad reports as two layers.
    """
    if dy != 0:
        return dy < 0
    return dx > 0


def _rasterize_triangles(
    triangles, width, height, scratch, coverage, depth, depth_test, budget, stats
):
    """Count how many times each pixel is shaded, and return the samples spent.

    A CPU rasteriser is used because RenderDoc's own quad-overdraw overlay is a
    GPU pass driven through a ``ReplayOutput``, which needs a window and so
    cannot run headless. This runs wherever post-VS data is available, at the
    cost of being an estimate: it counts triangle coverage, not the quad
    rasterisation a real GPU performs, and it shades both windings because the
    draw's cull mode is not consulted.
    """
    samples = 0
    for triangle in triangles:
        if samples >= budget:
            break
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = triangle
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if area == 0:
            continue
        low_x = max(0, int(min(x0, x1, x2)))
        high_x = min(width - 1, int(max(x0, x1, x2)))
        low_y = max(0, int(min(y0, y1, y2)))
        high_y = min(height - 1, int(max(y0, y1, y2)))
        if low_x > high_x or low_y > high_y:
            continue
        # Barycentric edge functions are linear in the pixel centre, so the scan
        # walks them incrementally instead of recomputing three cross products
        # per pixel. Each eN is the signed area of the sub-triangle opposite
        # vertex N, so e0+e1+e2 is the triangle's signed area and eN/area is the
        # barycentric weight of vertex N — which is also how depth is
        # interpolated.
        dx0, dx1, dx2 = (y1 - y2), (y2 - y0), (y0 - y1)
        dy0, dy1, dy2 = (x2 - x1), (x0 - x2), (x1 - x0)
        start_x = low_x + 0.5
        start_y = low_y + 0.5
        row_e0 = (x2 - x1) * (start_y - y1) - (y2 - y1) * (start_x - x1)
        row_e1 = (x0 - x2) * (start_y - y2) - (y0 - y2) * (start_x - x2)
        row_e2 = (x1 - x0) * (start_y - y0) - (y1 - y0) * (start_x - x0)
        own0 = _owns_edge(x2 - x1, y2 - y1)
        own1 = _owns_edge(x0 - x2, y0 - y2)
        own2 = _owns_edge(x1 - x0, y1 - y0)
        span = high_x - low_x + 1
        for pixel_y in range(low_y, high_y + 1):
            e0, e1, e2 = row_e0, row_e1, row_e2
            row = pixel_y * width
            for pixel_x in range(low_x, high_x + 1):
                if (e0 > 0 and e1 > 0 and e2 > 0) or (e0 < 0 and e1 < 0 and e2 < 0):
                    hit = True
                elif (e0 >= 0 and e1 >= 0 and e2 >= 0) or (e0 <= 0 and e1 <= 0 and e2 <= 0):
                    # On the boundary, so the fill rule decides the pixel.
                    hit = (e0 != 0 or own0) and (e1 != 0 or own1) and (e2 != 0 or own2)
                else:
                    hit = False
                if hit:
                    index = row + pixel_x
                    if depth_test:
                        z = (e0 * z0 + e1 * z1 + e2 * z2) / area
                        if z > depth[index]:
                            hit = False
                        else:
                            depth[index] = z
                    if hit:
                        if scratch[index] == 0:
                            stats[0] += 1
                        scratch[index] += 1
                        coverage[index] += 1
                        stats[1] += 1
                e0 += dx0
                e1 += dx1
                e2 += dx2
            row_e0 += dy0
            row_e1 += dy1
            row_e2 += dy2
            samples += span
            if samples >= budget:
                break
    return samples


def _overdraw_heatmap(coverage, maximum):
    """Colour one overdraw count per pixel on a blue-green-red ramp."""
    pixels = bytearray(len(coverage) * 3)
    span = float(maximum - 1) if maximum > 1 else 1.0
    for index, count in enumerate(coverage):
        if count <= 0:
            continue
        ratio = (count - 1) / span
        if ratio < 0.0:
            ratio = 0.0
        elif ratio > 1.0:
            ratio = 1.0
        band = 2.0 * ratio
        red = int(255 * max(0.0, band - 1.0))
        green = int(255 * (1.0 - abs(band - 1.0)))
        blue = int(255 * max(0.0, 1.0 - band))
        base = index * 3
        pixels[base] = red
        pixels[base + 1] = green
        pixels[base + 2] = blue
    return pixels


def _png_chunk(tag, payload):
    body = tag + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _write_png(path, width, height, pixels):
    """Write an RGB PNG using only ``zlib``, so the bridge needs no encoder."""
    stride = width * 3
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        start = row * stride
        raw.extend(pixels[start : start + stride])
    with open(path, "wb") as stream:
        stream.write(b"\x89PNG\r\n\x1a\n")
        stream.write(_png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        stream.write(_png_chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        stream.write(_png_chunk(b"IEND", b""))


def _write_ppm(path, width, height, pixels):
    """Write a binary PPM, the dependency-free fallback to a PNG heatmap."""
    with open(path, "wb") as stream:
        stream.write("P6\n{} {}\n255\n".format(width, height).encode("ascii"))
        stream.write(bytes(pixels))


def _depth_range(controller):
    """Map clip-space z onto [0, 1] for the API this capture was taken on.

    Direct3D clips depth to [0, 1] while OpenGL and Vulkan clip it to [-1, 1],
    so a depth test that assumed one range would reject almost everything on the
    other. The factor is derived from the capture's own API properties.
    """
    try:
        pipeline = _text(controller.GetAPIProperties().pipelineType).casefold()
    except BaseException:
        return 1.0, 0.0
    if "opengl" in pipeline or "vulkan" in pipeline:
        return 0.5, 0.5
    return 1.0, 0.0


def _op_get_overdraw(controller, rd, params, context):
    """Quantify overdraw by rasterising each draw's post-VS geometry on the CPU.

    RenderDoc's quad-overdraw overlay is a GPU pass driven through a
    ``ReplayOutput``, which needs a window and so cannot run headless. This
    estimates the same quantity -- how many times the frame shades each pixel --
    from the post-VS triangles a replay can already return, and reports it as
    per-draw and per-frame statistics with an optional heatmap export.
    """
    del context
    if not _replay_capabilities(controller).get("post_vs_data"):
        raise RuntimeError("this capture's replay does not support post-VS data")
    stage = getattr(rd.MeshDataStage, "VSOut", None)
    if stage is None:
        raise RuntimeError("this RenderDoc build does not expose post-VS data stages")
    width = _clamp(params.get("width"), 8, 2048, _OVERDRAW_DEFAULT_WIDTH)
    height = _clamp(params.get("height"), 8, 2048, _OVERDRAW_DEFAULT_HEIGHT)
    max_draws = _clamp(params.get("max_draws"), 1, MAX_OVERDRAW_DRAWS, 64)
    max_triangles = _clamp(params.get("max_triangles"), 1, MAX_OVERDRAW_TRIANGLES, 5000)
    depth_test = bool(params.get("depth_test", False))
    output_file = _text(params.get("output_file", "") or "")
    export_format = None
    if output_file:
        export_format = _OVERDRAW_EXPORT_FORMATS.get(os.path.splitext(output_file)[1].casefold())
        if export_format is None:
            raise ValueError(
                "output_file must use one of these extensions: "
                + ", ".join(sorted(_OVERDRAW_EXPORT_FORMATS))
            )
        directory = os.path.dirname(os.path.abspath(output_file))
        if not os.path.isdir(directory):
            raise ValueError("output directory does not exist: {}".format(directory))
    pixel_count = width * height
    draws, candidates = _draw_actions(controller, rd, params, max_draws)
    depth_scale, depth_bias = _depth_range(controller)
    coverage = [0] * pixel_count
    depth = [1.0] * pixel_count if depth_test else None
    zeros = [0] * pixel_count
    scratch = [0] * pixel_count
    max_vertices = min(MAX_OVERDRAW_VERTICES, max_triangles * 3 + 2)
    per_draw = []
    unavailable = []
    notes = []
    fragment_total = 0
    triangle_total = 0
    budget_left = MAX_OVERDRAW_SAMPLES
    for summary in draws:
        if budget_left <= 0:
            break
        event_id = summary["event_id"]
        controller.SetFrameEvent(event_id, True)
        mesh = controller.GetPostVSData(0, 0, stage)
        vertices, indices, note = _post_vs_geometry(controller, mesh, max_vertices)
        if note and note not in notes:
            notes.append(note)
        if not vertices or len(indices) < 3:
            unavailable.append(
                {
                    "event_id": event_id,
                    "name": summary["name"],
                    "reason": note or "RenderDoc returned no post-VS geometry for this draw",
                }
            )
            continue
        triangles = _triangle_screen_coords(
            vertices, indices, width, height, depth_scale, depth_bias
        )[:max_triangles]
        if not triangles:
            unavailable.append(
                {
                    "event_id": event_id,
                    "name": summary["name"],
                    "reason": "no post-VS triangle projected inside the grid",
                }
            )
            continue
        scratch[:] = zeros
        stats = [0, 0]
        spent = _rasterize_triangles(
            triangles, width, height, scratch, coverage, depth, depth_test, budget_left, stats
        )
        budget_left -= spent
        covered, counted = stats[0], stats[1]
        fragment_total += counted
        triangle_total += len(triangles)
        per_draw.append(
            {
                "event_id": event_id,
                "name": summary["name"],
                "triangle_count": len(triangles),
                "fragment_count": counted,
                "covered_pixels": covered,
                "average_overdraw": (float(counted) / covered) if covered else 0.0,
            }
        )
    covered_total = 0
    maximum = 0
    for count in coverage:
        if count > 0:
            covered_total += 1
            if count > maximum:
                maximum = count
    result = {
        "method": _OVERDRAW_METHOD,
        "width": width,
        "height": height,
        "depth_test": depth_test,
        "max_draws": max_draws,
        "max_triangles": max_triangles,
        "draw_count": len(per_draw),
        "candidate_count": candidates,
        "truncated": candidates > len(per_draw) or budget_left <= 0,
        "sample_budget_exhausted": budget_left <= 0,
        "triangle_count": triangle_total,
        "fragment_count": fragment_total,
        "covered_pixels": covered_total,
        "fill_ratio": (float(covered_total) / pixel_count) if pixel_count else 0.0,
        "average_overdraw": (float(fragment_total) / covered_total) if covered_total else 0.0,
        "max_overdraw": maximum,
        "draws": per_draw,
        "unavailable_draws": unavailable,
        "geometry_notes": notes,
        "output_file": output_file or None,
        "output_format": export_format,
        "size_bytes": None,
    }
    if output_file:
        pixels = _overdraw_heatmap(coverage, maximum)
        if export_format == "png":
            _write_png(output_file, width, height, pixels)
        else:
            _write_ppm(output_file, width, height, pixels)
        if not os.path.isfile(output_file):
            raise RuntimeError("RenderDoc did not create {}".format(output_file))
        result["size_bytes"] = int(os.path.getsize(output_file))
    return result


def _op_run_python_script(controller, rd, params, context):
    source = _text(params.get("source", "") or "")
    if not source.strip():
        raise ValueError("source is required")
    event_id = params.get("event_id")
    if event_id is not None:
        event_id = _set_event(controller, event_id)
    namespace = {
        "rd": rd,
        "controller": controller,
        "pyrenderdoc": context,
        "params": params.get("args") or {},
    }
    original = sys.stdout
    captured = []

    class _Capture(object):
        def write(self, text):
            captured.append(text)

        def flush(self):
            return None

    sys.stdout = _Capture()
    try:
        exec(compile(source, "<dcc-mcp-renderdoc-script>", "exec"), namespace)
    finally:
        sys.stdout = original
    return {
        "event_id": event_id,
        "result": _describe(namespace.get("result")),
        "stdout": "".join(captured)[-MAX_TEXT_CHARS:],
        "has_result": "result" in namespace,
    }


OPERATIONS = {
    "describe_capture": _op_describe_capture,
    "list_actions": _op_list_actions,
    "get_action": _op_get_action,
    "list_resources": _op_list_resources,
    "get_resource_usage": _op_get_resource_usage,
    "get_pipeline_state": _op_get_pipeline_state,
    "get_shader_info": _op_get_shader_info,
    "get_texture_data": _op_get_texture_data,
    "get_buffer_data": _op_get_buffer_data,
    "sample_pixel_region": _op_sample_pixel_region,
    "diagnose_pixel_values": _op_diagnose_pixel_values,
    "get_frame_overview": _op_get_frame_overview,
    "get_draw_call_state": _op_get_draw_call_state,
    "analyze_render_passes": _op_analyze_render_passes,
    "analyze_state_changes": _op_analyze_state_changes,
    "get_pass_timing": _op_get_pass_timing,
    "get_mesh_data": _op_get_mesh_data,
    "export_mesh": _op_export_mesh,
    "pick_pixel": _op_pick_pixel,
    "get_pixel_history": _op_get_pixel_history,
    "debug_pixel": _op_debug_pixel,
    "debug_vertex": _op_debug_vertex,
    "debug_thread": _op_debug_thread,
    "get_counters": _op_get_counters,
    "get_debug_messages": _op_get_debug_messages,
    "describe_perf": _op_describe_perf,
    "get_action_timing": _op_get_action_timing,
    "get_overdraw": _op_get_overdraw,
    "run_python_script": _op_run_python_script,
}


def main():
    status_path = os.environ.get("DCC_MCP_RENDERDOC_REPLAY_STATUS")
    operation = os.environ.get("DCC_MCP_RENDERDOC_OPERATION", "")
    status = {
        "schema_version": SCHEMA_VERSION,
        "operation": operation or None,
        "result": None,
        "error": None,
    }
    try:
        import renderdoc as rd

        params_path = os.environ.get("DCC_MCP_RENDERDOC_REPLAY_PARAMS", "")
        params = {}
        if params_path:
            with open(params_path) as stream:
                params = json.load(stream)
        if not isinstance(params, dict):
            raise ValueError("replay parameters must be a JSON object")
        handler = OPERATIONS.get(operation)
        if handler is None:
            raise ValueError("unknown replay operation: {}".format(operation))
        capture_file = os.environ["DCC_MCP_RENDERDOC_CAPTURE"]
        if not os.path.isfile(capture_file):
            raise ValueError("capture file does not exist: {}".format(capture_file))
        context = globals().get("pyrenderdoc")
        if context is None:
            raise RuntimeError("qrenderdoc capture context is unavailable")

        context.LoadCapture(capture_file, rd.ReplayOptions(), capture_file, False, True)

        def replay_callback(controller):
            status["result"] = handler(controller, rd, params, context)

        context.Replay().BlockInvoke(replay_callback)
        if status["result"] is None:
            raise RuntimeError("RenderDoc did not open the capture for replay")
        context.CloseCapture()
    except BaseException as exc:
        status["error"] = "{}: {}".format(type(exc).__name__, exc)
    finally:
        if status_path:
            with open(status_path, "w") as stream:
                json.dump(status, stream)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    raise SystemExit(0)

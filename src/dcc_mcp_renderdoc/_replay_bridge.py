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

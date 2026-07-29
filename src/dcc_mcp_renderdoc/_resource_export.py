"""RenderDoc 1.45 bundled-Python bridge for exporting drawcall textures."""

import json
import os
import re


def _find_action(actions, event_id):
    for action in actions:
        if int(action.eventId) == event_id:
            return action
        found = _find_action(action.children, event_id)
        if found is not None:
            return found
    return None


def _safe_name(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_.")
    return value[:80] or "texture"


def _export(controller, rd, event_id, output_dir):
    action = _find_action(controller.GetRootActions(), event_id)
    if action is None:
        raise RuntimeError("event {} was not found".format(event_id))

    controller.SetFrameEvent(event_id, True)
    pipeline = controller.GetPipelineState()
    reflection = pipeline.GetShaderReflection(rd.ShaderStage.Pixel)
    shader_resources = reflection.readOnlyResources if reflection is not None else []
    resource_names = {
        int(resource.resourceId): str(resource.name) for resource in controller.GetResources()
    }
    textures = {int(texture.resourceId): texture for texture in controller.GetTextures()}
    exported = []
    seen = set()

    for used in pipeline.GetReadOnlyResources(rd.ShaderStage.Pixel, True):
        resource_id = int(used.descriptor.resource)
        if resource_id == 0 or resource_id in seen or resource_id not in textures:
            continue
        seen.add(resource_id)
        access_index = int(used.access.index)
        shader_resource = (
            shader_resources[access_index]
            if 0 <= access_index < len(shader_resources)
            else None
        )
        binding = (
            int(shader_resource.fixedBindNumber)
            if shader_resource is not None
            else access_index
        )
        shader_name = str(shader_resource.name) if shader_resource is not None else ""
        resource_name = resource_names.get(resource_id, "")
        label = _safe_name(shader_name or resource_name)
        output_file = os.path.join(
            output_dir,
            "ps_{:03d}_{}_{}.png".format(binding, resource_id, label),
        )

        save = rd.TextureSave()
        save.resourceId = used.descriptor.resource
        save.destType = rd.FileType.PNG
        save.alpha = rd.AlphaMapping.Preserve
        save.mip = 0
        save.slice.sliceIndex = 0
        if not controller.SaveTexture(save, output_file):
            raise RuntimeError("RenderDoc failed to save resource {}".format(resource_id))

        texture = textures[resource_id]
        exported.append(
            {
                "binding": binding,
                "array_element": int(used.access.arrayElement),
                "shader_name": shader_name,
                "resource_id": resource_id,
                "resource_name": resource_name,
                "width": int(texture.width),
                "height": int(texture.height),
                "format": str(texture.format.Name()),
                "output_file": output_file,
                "size_bytes": int(os.path.getsize(output_file)),
            }
        )

    return {
        "schema_version": 1,
        "event_id": event_id,
        "action_name": str(action.GetName(controller.GetStructuredFile())),
        "num_indices": int(action.numIndices),
        "resources": exported,
        "error": None,
    }


def main():
    status_path = os.environ.get("DCC_MCP_RENDERDOC_RESOURCE_STATUS")
    status = {
        "schema_version": 1,
        "event_id": None,
        "action_name": None,
        "num_indices": None,
        "resources": [],
        "error": None,
    }
    try:
        import renderdoc as rd

        capture_file = os.environ["DCC_MCP_RENDERDOC_CAPTURE"]
        event_id = int(os.environ["DCC_MCP_RENDERDOC_EVENT_ID"])
        output_dir = os.environ["DCC_MCP_RENDERDOC_RESOURCE_OUTPUT"]
        if event_id <= 0:
            raise ValueError("DCC_MCP_RENDERDOC_EVENT_ID must be positive")
        if not os.path.isfile(capture_file):
            raise ValueError("capture file does not exist: {}".format(capture_file))
        if not os.path.isdir(output_dir):
            raise ValueError("output directory does not exist: {}".format(output_dir))
        context = globals().get("pyrenderdoc")
        if context is None:
            raise RuntimeError("qrenderdoc capture context is unavailable")

        context.LoadCapture(
            capture_file,
            rd.ReplayOptions(),
            capture_file,
            False,
            True,
        )

        def export_callback(controller):
            status.update(_export(controller, rd, event_id, output_dir))

        context.Replay().BlockInvoke(export_callback)
        if status["event_id"] is None:
            raise RuntimeError("RenderDoc did not open the capture for replay")
        context.CloseCapture()
    except BaseException as exc:
        status["error"] = str(exc)
    finally:
        if status_path:
            with open(status_path, "w") as status_file:
                json.dump(status, status_file)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    raise SystemExit(0)

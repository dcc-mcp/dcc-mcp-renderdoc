"""Host-side image differencing for the RenderDoc analysis tools.

The replay bridge only ever reads texels back: :mod:`dcc_mcp_renderdoc.replay`
asks it to dump one region per event, and this module turns those dumps into
the numbers a caller actually wants. Keeping the comparison here means the
metrics exist in exactly one place even though ``diff_draws`` (one capture, two
events) and ``diff_captures`` (two captures) reach the texels differently.

Three properties this module is built around:

* **A one-sided replay is never a diff.** Every readback that can fail is
  reported as a structured result naming the side that failed, so a capture
  that will not replay is never silently compared against nothing.
* **The dumps are the contract.** Each side writes its texels beside a sidecar
  describing its size, format, and sampling stride. The two sidecars are
  checked against each other before a single texel is compared, and a mismatch
  comes back as ``comparable: false`` with the reason rather than as a raise.
* **Nothing has to fit in memory.** Both passes read the dumps in bounded
  chunks, so a 4K target costs a few rows of working set rather than a
  multi-hundred-megabyte list.
"""

from __future__ import annotations

import array
import json
import math
import os
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .replay import (
    RenderDocError,
    run_replay_operation,
    unsupported_backend,
)

#: Operation names this module reports, one per skill tool.
DIFF_DRAWS = "diff_draws"
DIFF_CAPTURES = "diff_captures"

#: Sidecar schema this module can read. Anything else is reported, not guessed.
DIFF_DUMP_SCHEMA_VERSION = 1

#: Rows one streaming comparison pass holds in memory. Both passes are bounded
#: by this rather than by the image, and it is also what lets a forced
#: comparison read the common sub-rectangle of two differently sized dumps.
COMPARE_ROWS_PER_READ = 64

#: Heatmap exports this module can write; the extension picks the format.
DIFF_EXPORT_FORMATS = {".png": "png", ".ppm": "ppm"}

#: Default tolerance for the failed-texel count: one 8-bit code value, the
#: smallest difference an 8-bit target can represent at all.
DEFAULT_THRESHOLD = 1.0 / 255.0

#: SSIM approximation constants, the ones the reference implementation uses.
SSIM_K1 = 0.01
SSIM_K2 = 0.03
#: The approximation is evaluated on 8x8 blocks with a uniform (box) window.
#: That is not the standard 11x11 Gaussian SSIM, so it is never reported under
#: the plain name -- the field is ``ssim_approx`` and the method travels with it.
SSIM_BLOCK_SIZE = 8
SSIM_WINDOW = "box"
DEFAULT_SSIM_GRID_STEP = 8

#: Nominal peak of a decoded texel, by the component type RenderDoc reported.
#: Integer formats decode to their raw value, so their peak is the largest
#: representable one; the normalised and float formats are compared against
#: unity, the reference white an LDR target is authored against.
_FORMAT_PEAK_BY_COMP_TYPE = {
    "CompType.UNorm": 1.0,
    "CompType.UNormSRGB": 1.0,
    "CompType.SNorm": 1.0,
    "CompType.Float": 1.0,
}


def _is_nan(value: float) -> bool:
    return value != value


def _is_inf(value: float) -> bool:
    return value == float("inf") or value == float("-inf")


def _is_non_finite(value: float) -> bool:
    return value != value or _is_inf(value)


def resolve_max_value(facts: Mapping[str, Any], requested: Optional[float]) -> Tuple[float, str]:
    """Pick the peak value PSNR is measured against, and say where it came from.

    A PSNR number means nothing without the peak it was computed against, so
    the caller always gets both: an explicit ``max_value`` wins, and otherwise
    the format's nominal peak is used and labelled as derived.
    """
    if requested is not None:
        value = float(requested)
        if value <= 0.0:
            raise RenderDocError("max_value must be greater than zero")
        return value, "caller_supplied"
    comp_type = str(facts.get("comp_type") or "")
    if comp_type in _FORMAT_PEAK_BY_COMP_TYPE:
        return _FORMAT_PEAK_BY_COMP_TYPE[comp_type], "format_nominal_peak"
    byte_width = int(facts.get("comp_byte_width") or 0)
    if byte_width <= 0:
        return 1.0, "format_nominal_peak"
    if comp_type == "CompType.SInt":
        return float((1 << (byte_width * 8 - 1)) - 1), "format_nominal_peak"
    return float((1 << (byte_width * 8)) - 1), "format_nominal_peak"


def _select_channels(comp_count: int, requested: Optional[Sequence[int]]) -> List[int]:
    """Validate the caller's channel selection, defaulting to every component."""
    if not requested:
        return list(range(comp_count))
    channels: List[int] = []
    for item in requested:
        index = int(item)
        if index < 0 or index >= comp_count:
            raise RenderDocError(
                "channel {} is out of range: this format has {} component(s)".format(
                    index, comp_count
                )
            )
        if index not in channels:
            channels.append(index)
    if not channels:
        raise RenderDocError("channels must name at least one component")
    return channels


def _read_sidecar(path: str, side: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Load one dump's sidecar, or explain why it cannot be trusted."""
    if not os.path.isfile(path):
        return None, "side {} wrote no sidecar at {}".format(side, path)
    try:
        with open(path, encoding="utf-8") as stream:
            sidecar = json.load(stream)
    except (OSError, ValueError) as exc:
        return None, "side {} wrote an unreadable sidecar: {}".format(side, exc)
    if not isinstance(sidecar, dict):
        return None, "side {} wrote a sidecar that is not an object".format(side)
    if int(sidecar.get("schema_version") or 0) != DIFF_DUMP_SCHEMA_VERSION:
        return None, "side {} wrote sidecar schema version {}, expected {}".format(
            side, sidecar.get("schema_version"), DIFF_DUMP_SCHEMA_VERSION
        )
    return sidecar, None


def compare_sidecars(left: Mapping[str, Any], right: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """Report the first thing that stops two dumps from being comparable.

    Returns ``None`` when they line up. The order is deliberate: a size
    mismatch makes the comparison meaningless, the rest only make it suspect.
    """
    for key in ("width", "height", "comp_count", "sample_step"):
        if int(left.get(key) or 0) != int(right.get(key) or 0):
            return {
                "reason_code": "dimension_mismatch",
                "reason": "the two sides read {}x{}x{} texel(s) at step {} and {}x{}x{} at "
                "step {}, so there is no one-to-one texel pairing".format(
                    left.get("width"),
                    left.get("height"),
                    left.get("comp_count"),
                    left.get("sample_step"),
                    right.get("width"),
                    right.get("height"),
                    right.get("comp_count"),
                    right.get("sample_step"),
                ),
            }
    left_format = left.get("format") or {}
    right_format = right.get("format") or {}
    if str(left_format.get("name") or "") != str(right_format.get("name") or ""):
        return {
            "reason_code": "format_mismatch",
            "reason": "the two sides were read as {} and {}, so the same number is a "
            "different colour on each side".format(
                left_format.get("name") or "unknown", right_format.get("name") or "unknown"
            ),
        }
    if str(left.get("api") or "") != str(right.get("api") or ""):
        return {
            "reason_code": "api_mismatch",
            "reason": "the two captures replay on {} and {}, so a difference may be the "
            "API rather than the content".format(
                left.get("api") or "unknown API", right.get("api") or "unknown API"
            ),
        }
    return None


def _psnr(mse: float, max_value: float) -> Optional[float]:
    """Peak signal-to-noise ratio, or ``None`` for two identical images.

    Two identical images have an MSE of zero and therefore an infinite PSNR.
    Reporting ``None`` alongside ``identical: true`` instead keeps a caller from
    comparing an infinity against a threshold.
    """
    if mse <= 0.0:
        return None
    return 10.0 * math.log10((max_value * max_value) / mse)


_Moments = Tuple[float, float, float, float, float]


def _block_moments(pairs: Sequence[Tuple[float, float]]) -> Optional[_Moments]:
    """Block means, variances, and covariance for the SSIM approximation."""
    count = len(pairs)
    if count == 0:
        return None
    sum_a = 0.0
    sum_b = 0.0
    for value_a, value_b in pairs:
        if _is_non_finite(value_a) or _is_non_finite(value_b):
            return None
        sum_a += value_a
        sum_b += value_b
    mean_a = sum_a / count
    mean_b = sum_b / count
    variance_a = 0.0
    variance_b = 0.0
    covariance = 0.0
    for value_a, value_b in pairs:
        offset_a = value_a - mean_a
        offset_b = value_b - mean_b
        variance_a += offset_a * offset_a
        variance_b += offset_b * offset_b
        covariance += offset_a * offset_b
    return mean_a, mean_b, variance_a / count, variance_b / count, covariance / count


def _ssim_from_block(moments: _Moments, max_value: float) -> float:
    """The SSIM index of one block, from its pre-computed moments."""
    mean_a, mean_b, variance_a, variance_b, covariance = moments
    c1 = (SSIM_K1 * max_value) ** 2
    c2 = (SSIM_K2 * max_value) ** 2
    numerator = (2.0 * mean_a * mean_b + c1) * (2.0 * covariance + c2)
    denominator = (mean_a * mean_a + mean_b * mean_b + c1) * (variance_a + variance_b + c2)
    if denominator == 0.0:
        return 1.0
    return numerator / denominator


def _unforceable_mismatch(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> Optional[Dict[str, str]]:
    """Report a mismatch that ``force`` must not be allowed to override.

    A width or height difference can be honestly handled by comparing only the
    region both sides have in common. A different component count or a different
    sampling stride cannot: there is no cropping or per-side stride that makes
    texel ``(x, y)`` mean the same thing on both sides, so any number produced
    would be quietly wrong. ``force`` is an assertion that a *known* difference
    is acceptable, not a licence to compare incomparable data.
    """
    if int(left.get("comp_count") or 0) != int(right.get("comp_count") or 0):
        return {
            "reason_code": "component_count_mismatch",
            "reason": "the two sides read {} and {} component(s) per texel, so there is no "
            "shared channel layout to compare; force cannot reconcile them".format(
                left.get("comp_count"), right.get("comp_count")
            ),
        }
    if int(left.get("sample_step") or 0) != int(right.get("sample_step") or 0):
        return {
            "reason_code": "sample_step_mismatch",
            "reason": "the two sides sampled every {}th and every {}th texel, so the same "
            "index names a different source texel on each side; force cannot "
            "reconcile them".format(left.get("sample_step"), right.get("sample_step")),
        }
    return None


def _ssim_approx(
    left_path: str,
    right_path: str,
    *,
    left_width: int,
    right_width: int,
    comp_count: int,
    channels: Sequence[int],
    grid_step: int,
    max_value: float,
    crop_width: int,
    crop_height: int,
) -> Dict[str, Any]:
    """Average SSIM over 8x8 blocks on a sampling grid.

    Each side is read with *its own* row stride: two dumps that declare
    different widths do not share a byte layout, so a stride taken from one of
    them would seek the other to the wrong rows.

    Memory is bounded by the block, not by the grid step. When the step is at
    least one block the blocks cannot overlap, so each block's rows are read and
    dropped immediately; only a step smaller than a block needs a rolling
    window, and that window holds fewer than two blocks of rows.
    """
    block = SSIM_BLOCK_SIZE
    result: Dict[str, Any] = {
        "ssim_approx": None,
        "ssim_skipped_reason": None,
        "ssim_block_size": block,
        "ssim_window": SSIM_WINDOW,
        "ssim_grid_step": grid_step,
        "ssim_block_count": 0,
        "ssim_skipped_block_count": 0,
        "ssim_constants": {"k1": SSIM_K1, "k2": SSIM_K2, "max_value": max_value},
    }
    if crop_width < block or crop_height < block:
        result["ssim_skipped_reason"] = (
            "the compared region is {}x{}, smaller than one {}x{} block".format(
                crop_width, crop_height, block, block
            )
        )
        return result
    row_floats = crop_width * comp_count
    row_bytes = row_floats * 4
    left_stride = left_width * comp_count * 4
    right_stride = right_width * comp_count * 4
    total = 0.0
    counted = 0
    skipped = 0

    def read_row(row_index):
        left_handle.seek(row_index * left_stride)
        right_handle.seek(row_index * right_stride)
        return (
            struct.unpack("<{}f".format(row_floats), left_handle.read(row_bytes)),
            struct.unpack("<{}f".format(row_floats), right_handle.read(row_bytes)),
        )

    def accumulate(rows):
        """Fold every block of one block-row into the running SSIM total."""
        nonlocal total, counted, skipped
        for origin_x in range(0, crop_width - block + 1, grid_step):
            pairs: List[Tuple[float, float]] = []
            for row_left, row_right in rows:
                base = origin_x * comp_count
                for offset in range(block):
                    for channel in channels:
                        index = base + offset * comp_count + channel
                        pairs.append((row_left[index], row_right[index]))
            moments = _block_moments(pairs)
            if moments is None:
                skipped += 1
                continue
            total += _ssim_from_block(moments, max_value)
            counted += 1

    origins = range(0, crop_height - block + 1, grid_step)
    with open(left_path, "rb") as left_handle, open(right_path, "rb") as right_handle:
        if grid_step >= block:
            # Blocks cannot overlap, so nothing has to be retained between them.
            for origin_y in origins:
                accumulate([read_row(index) for index in range(origin_y, origin_y + block)])
        else:
            # Overlapping blocks share rows, so a short rolling window saves
            # re-reading them. It holds fewer than block + grid_step rows.
            window: List[Tuple[Sequence[float], Sequence[float]]] = []
            next_row = 0
            for origin_y in origins:
                while next_row < origin_y + block and next_row < crop_height:
                    window.append(read_row(next_row))
                    next_row += 1
                while window and next_row - len(window) < origin_y:
                    window.pop(0)
                if len(window) < block:
                    break
                accumulate(window[:block])
    if not counted:
        result["ssim_skipped_reason"] = (
            "no {}x{} block on the sampling grid held only finite values".format(block, block)
        )
        return result
    result["ssim_approx"] = total / counted
    result["ssim_block_count"] = counted
    result["ssim_skipped_block_count"] = skipped
    return result


def compare_dumps(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    channels: Optional[Sequence[int]] = None,
    max_value: Optional[float] = None,
    threshold: float = DEFAULT_THRESHOLD,
    ssim_grid_step: int = DEFAULT_SSIM_GRID_STEP,
    output_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Stream two texel dumps against each other and report the difference.

    Everything that folds in one pass does: squared and absolute differences,
    the per-channel breakdown, the texels over the threshold, and the NaN and
    Inf counts -- which are kept apart on purpose, because one NaN in one image
    would otherwise make the PSNR read "identical" or "infinitely bad" with
    nothing in between. SSIM is a second pass with its own rolling window.
    """
    unforceable = _unforceable_mismatch(left, right)
    if unforceable is not None:
        raise RenderDocError(unforceable["reason"])
    left_width = int(left.get("width") or 0)
    left_height = int(left.get("height") or 0)
    comp_count = int(left.get("comp_count") or 0)
    right_width = int(right.get("width") or left_width)
    right_height = int(right.get("height") or left_height)
    if min(left_width, left_height, right_width, right_height) <= 0 or comp_count <= 0:
        raise RenderDocError("both dumps must describe a non-empty region")
    if threshold <= 0.0:
        raise RenderDocError("threshold must be greater than zero")
    facts = left.get("format") or {}
    peak, peak_source = resolve_max_value(facts, max_value)
    selected = _select_channels(comp_count, channels)
    grid_step = max(1, int(ssim_grid_step))
    # A forced comparison of two differently sized dumps compares the region
    # they have in common rather than pretending the sizes agreed. Cropping the
    # width does not give the two files a shared byte layout, so each side is
    # still read with its own row stride.
    crop_width = min(left_width, right_width)
    crop_height = min(left_height, right_height)
    left_path = str(left.get("bin_file") or "")
    right_path = str(right.get("bin_file") or "")
    for path, sidecar in ((left_path, left), (right_path, right)):
        if not os.path.isfile(path):
            raise RenderDocError("texel dump is missing: {}".format(path))
        declared = int(sidecar.get("byte_size") or 0)
        actual = os.path.getsize(path)
        if actual != declared:
            raise RenderDocError(
                "texel dump {} is {} byte(s), not the {} its own sidecar declares".format(
                    path, actual, declared
                )
            )

    squared_sums = [0.0] * comp_count
    abs_sums = [0.0] * comp_count
    max_diffs = [0.0] * comp_count
    nan_left = 0
    nan_right = 0
    inf_left = 0
    inf_right = 0
    nan_mismatch = 0
    inf_mismatch = 0
    excluded_texels = 0
    compared_texels = 0
    failed_texels = 0
    magnitudes = array.array("f", bytes(crop_width * crop_height * 4)) if output_file else None

    row_floats = crop_width * comp_count
    row_bytes = row_floats * 4
    left_stride = left_width * comp_count * 4
    right_stride = right_width * comp_count * 4
    rows_per_read = max(1, COMPARE_ROWS_PER_READ)
    with open(left_path, "rb") as left_handle, open(right_path, "rb") as right_handle:
        for row_start in range(0, crop_height, rows_per_read):
            count = min(rows_per_read, crop_height - row_start)
            left_values: List[float] = []
            right_values: List[float] = []
            for offset in range(count):
                row = row_start + offset
                left_handle.seek(row * left_stride)
                right_handle.seek(row * right_stride)
                left_values.extend(
                    struct.unpack("<{}f".format(row_floats), left_handle.read(row_bytes))
                )
                right_values.extend(
                    struct.unpack("<{}f".format(row_floats), right_handle.read(row_bytes))
                )
            for texel in range(count * crop_width):
                base = texel * comp_count
                non_finite = False
                for channel in range(comp_count):
                    value_left = left_values[base + channel]
                    value_right = right_values[base + channel]
                    left_nan = _is_nan(value_left)
                    right_nan = _is_nan(value_right)
                    left_inf = not left_nan and _is_inf(value_left)
                    right_inf = not right_nan and _is_inf(value_right)
                    nan_left += 1 if left_nan else 0
                    nan_right += 1 if right_nan else 0
                    inf_left += 1 if left_inf else 0
                    inf_right += 1 if right_inf else 0
                    if left_nan != right_nan:
                        nan_mismatch += 1
                    if left_inf != right_inf:
                        inf_mismatch += 1
                    if left_nan or right_nan or left_inf or right_inf:
                        non_finite = True
                if non_finite:
                    excluded_texels += 1
                    continue
                compared_texels += 1
                worst = 0.0
                for channel in selected:
                    difference = left_values[base + channel] - right_values[base + channel]
                    if difference < 0.0:
                        difference = -difference
                    squared_sums[channel] += difference * difference
                    abs_sums[channel] += difference
                    if difference > max_diffs[channel]:
                        max_diffs[channel] = difference
                    if difference > worst:
                        worst = difference
                if worst > threshold:
                    failed_texels += 1
                if magnitudes is not None:
                    magnitudes[row_start * crop_width + texel] = worst

    channel_reports = []
    squared_total = 0.0
    abs_total = 0.0
    for index in range(comp_count):
        mse = (squared_sums[index] / compared_texels) if compared_texels else 0.0
        channel_reports.append(
            {
                "index": index,
                "selected": index in selected,
                "mean_abs_diff": (abs_sums[index] / compared_texels) if compared_texels else None,
                "max_abs_diff": max_diffs[index],
                "mse": mse,
                "rmse": math.sqrt(mse),
                "psnr": _psnr(mse, peak),
            }
        )
        if index in selected:
            squared_total += squared_sums[index]
            abs_total += abs_sums[index]
    compared_values = compared_texels * len(selected)
    mse_total = (squared_total / compared_values) if compared_values else 0.0
    metrics: Dict[str, Any] = {
        "identical": bool(compared_texels > 0 and excluded_texels == 0 and mse_total == 0.0),
        "mse": mse_total,
        "rmse": math.sqrt(mse_total),
        "psnr": _psnr(mse_total, peak),
        "mean_abs_diff": (abs_total / compared_values) if compared_values else None,
        "max_abs_diff": max([max_diffs[index] for index in selected] or [0.0]),
        "threshold": threshold,
        "failed_texel_count": failed_texels,
        "failed_texel_ratio": (failed_texels / compared_texels) if compared_texels else None,
        "compared_texel_count": compared_texels,
        "compared_channel_count": len(selected),
        "channels": channel_reports,
        "psnr_basis": {
            "bit_depth": int(facts.get("comp_byte_width") or 0) * 8,
            "bit_depth_source": "format component byte width",
            "channels": list(selected),
            "channel_count": len(selected),
            "max_value": peak,
            "max_value_source": peak_source,
            "value_kind": "decoded texel values; normalised for UNorm/SNorm, raw for "
            "float and integer formats",
        },
        "non_finite": {
            "nan_count_left": nan_left,
            "nan_count_right": nan_right,
            "inf_count_left": inf_left,
            "inf_count_right": inf_right,
            "nan_mismatch_count": nan_mismatch,
            "inf_mismatch_count": inf_mismatch,
            "excluded_texel_count": excluded_texels,
        },
        "compared_region": {
            "width": crop_width,
            "height": crop_height,
            "comp_count": comp_count,
            "texel_count": crop_width * crop_height,
            "cropped": bool(
                crop_width != left_width
                or crop_height != left_height
                or right_width != left_width
                or right_height != left_height
            ),
        },
    }
    metrics.update(
        _ssim_approx(
            left_path,
            right_path,
            left_width=left_width,
            right_width=right_width,
            comp_count=comp_count,
            channels=selected,
            grid_step=grid_step,
            max_value=peak,
            crop_width=crop_width,
            crop_height=crop_height,
        )
    )
    if output_file and magnitudes is not None:
        metrics["output_file"] = _write_heatmap(output_file, crop_width, crop_height, magnitudes)
    return metrics


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    body = tag + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _write_png(path: str, width: int, height: int, pixels: bytes) -> int:
    """Write an RGB PNG using only ``zlib``, so the adapter needs no encoder."""
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
    return os.path.getsize(path)


def _write_ppm(path: str, width: int, height: int, pixels: bytes) -> int:
    """Write a binary PPM, the dependency-free fallback to a PNG heatmap."""
    with open(path, "wb") as stream:
        stream.write("P6\n{} {}\n255\n".format(width, height).encode("ascii"))
        stream.write(pixels)
    return os.path.getsize(path)


def _heatmap_pixels(count: int, magnitudes) -> bytearray:
    """Colour one per-texel difference on a blue-to-red ramp.

    The ramp is scaled by the largest difference actually found, so a heatmap
    of a near-identical pair is not a flat black rectangle.
    """
    peak = 0.0
    for value in magnitudes:
        if value > peak:
            peak = value
    pixels = bytearray(count * 3)
    span = peak if peak > 0.0 else 1.0
    for index, value in enumerate(magnitudes):
        ratio = value / span
        if ratio < 0.0:
            ratio = 0.0
        elif ratio > 1.0:
            ratio = 1.0
        band = 2.0 * ratio
        base = index * 3
        pixels[base] = int(255 * max(0.0, band - 1.0))
        pixels[base + 1] = int(255 * (1.0 - abs(band - 1.0)))
        pixels[base + 2] = int(255 * max(0.0, 1.0 - band))
    return pixels


def _write_heatmap(output_file: str, width: int, height: int, magnitudes) -> Dict[str, Any]:
    extension = os.path.splitext(output_file)[1].casefold()
    export_format = DIFF_EXPORT_FORMATS.get(extension)
    if export_format is None:
        raise RenderDocError(
            "output_file must use one of these extensions: "
            + ", ".join(sorted(DIFF_EXPORT_FORMATS))
        )
    directory = os.path.dirname(os.path.abspath(output_file))
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    pixels = _heatmap_pixels(width * height, magnitudes)
    if export_format == "png":
        size = _write_png(output_file, width, height, pixels)
    else:
        size = _write_ppm(output_file, width, height, pixels)
    return {
        "path": output_file,
        "format": export_format,
        "size_bytes": size,
        "width": width,
        "height": height,
    }


def _read_one_side(
    capture_file: str,
    params: Mapping[str, Any],
    side: str,
    *,
    timeout_secs: int,
    command: Optional[str],
) -> Dict[str, Any]:
    """Run one diff readback, turning any failure into a structured report.

    A cross-capture diff replays twice, and the second replay has to be able to
    fail on its own: a capture that will not open is reported as a one-sided
    failure, never as a diff against a blank image.
    """
    unsupported = unsupported_backend("inspect", command=command)
    if unsupported is not None:
        return unsupported
    try:
        return run_replay_operation(
            capture_file,
            "read_diff_region",
            params,
            timeout_secs=timeout_secs,
            command=command,
        )
    except RenderDocError as exc:
        return {
            "supported": False,
            "side": side,
            "capture_file": str(capture_file),
            "error_message": "side {} ({}) could not be replayed: {}".format(
                side, capture_file, exc
            ),
            "hint": "both sides have to replay for a diff to mean anything; fix this "
            "capture or diff a different pair",
        }


def _dumps_of(report: Mapping[str, Any], out_dir: Path, capture_file: str):
    """Pair each dump the bridge reported with the sidecar it wrote to disk."""
    descriptors: List[Dict[str, Any]] = []
    for entry in report.get("dumps") or []:
        sidecar, problem = _read_sidecar(str(entry.get("sidecar_file") or ""), out_dir.name)
        if problem is not None:
            return None, {
                "supported": False,
                "capture_file": str(capture_file),
                "error_message": problem,
                "hint": "the readback did not complete; retry the diff",
            }
        sidecar["capture_file"] = str(capture_file)
        sidecar.setdefault("bin_file", entry.get("bin_file"))
        sidecar.setdefault("sidecar_file", entry.get("sidecar_file"))
        descriptors.append(sidecar)
    if not descriptors:
        return None, {
            "supported": False,
            "capture_file": str(capture_file),
            "error_message": "the readback produced no texel dumps for {}".format(capture_file),
            "hint": "the readback did not complete; retry the diff",
        }
    return descriptors, None


def _failure(operation: str, capture_file: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    return {"capture_file": str(capture_file), "operation": operation, "result": dict(payload)}


def _region_params(
    *,
    resource_id: Any,
    x: Optional[int],
    y: Optional[int],
    width: Optional[int],
    height: Optional[int],
    mip: int,
    slice_index: int,
    sample_index: int,
    max_texels: Optional[int],
    match_by: str,
    event_ids: Optional[Sequence[int]],
    event_index: Optional[int],
    event_name: Optional[str],
    out_dir: str,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "resource_id": resource_id,
        "mip": mip,
        "slice": slice_index,
        "sample": sample_index,
        "match_by": match_by,
        "out_dir": out_dir,
    }
    for key, value in (
        ("x", x),
        ("y", y),
        ("width", width),
        ("height", height),
        ("max_texels", max_texels),
        ("event_index", event_index),
        ("event_name", event_name),
    ):
        if value is not None:
            params[key] = value
    if event_ids:
        params["event_ids"] = list(event_ids)
    return params


def _side_report(side: str, sidecar: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "side": side,
        "capture_file": sidecar.get("capture_file"),
        "event_id": sidecar.get("event_id"),
        "resource_id": sidecar.get("resource_id"),
        "resource_name": sidecar.get("resource_name"),
        "api": sidecar.get("api"),
        "width": sidecar.get("width"),
        "height": sidecar.get("height"),
        "comp_count": sidecar.get("comp_count"),
        "sample_step": sidecar.get("sample_step"),
    }


def diff_region(
    operation: str,
    capture_file: str,
    *,
    other_capture_file: Optional[str] = None,
    event_ids: Optional[Sequence[int]] = None,
    match_by: str = "event_id",
    event_index: Optional[int] = None,
    event_name: Optional[str] = None,
    resource_id: Any = None,
    other_resource_id: Any = None,
    x: Optional[int] = None,
    y: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    mip: int = 0,
    slice_index: int = 0,
    sample_index: int = 0,
    max_texels: Optional[int] = None,
    channels: Optional[Sequence[int]] = None,
    max_value: Optional[float] = None,
    threshold: float = DEFAULT_THRESHOLD,
    ssim_grid_step: int = DEFAULT_SSIM_GRID_STEP,
    output_file: Optional[str] = None,
    force: bool = False,
    timeout_secs: int = 300,
    command: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare one region across two events or two captures.

    ``diff_draws`` replays once and moves the replay between two events;
    ``diff_captures`` replays each capture in turn. Either way the texels land
    in one temporary directory that is removed on every path out of this
    function -- including the ones that fail -- and the two sidecars are
    checked against each other before anything is compared.

    Returns the same ``{"capture_file", "operation", "result"}`` shape the other
    replay drivers do, so a skill script handles a diff exactly like any other
    analysis payload.
    """
    if operation not in (DIFF_DRAWS, DIFF_CAPTURES):
        raise RenderDocError("unknown diff operation: {}".format(operation))
    if output_file and os.path.splitext(output_file)[1].casefold() not in DIFF_EXPORT_FORMATS:
        return _failure(
            operation,
            capture_file,
            {
                "supported": False,
                "error_message": "output_file must use one of these extensions: "
                + ", ".join(sorted(DIFF_EXPORT_FORMATS)),
                "hint": "choose .png for a shareable heatmap or .ppm for a raw one",
            },
        )
    with tempfile.TemporaryDirectory(prefix="dcc-mcp-renderdoc-diff-") as directory:
        root = Path(directory)
        sources: List[Dict[str, Any]] = []
        if operation == DIFF_DRAWS:
            out_dir = root / "events"
            out_dir.mkdir()
            params = _region_params(
                resource_id=resource_id,
                x=x,
                y=y,
                width=width,
                height=height,
                mip=mip,
                slice_index=slice_index,
                sample_index=sample_index,
                max_texels=max_texels,
                match_by="event_id",
                event_ids=event_ids,
                event_index=None,
                event_name=None,
                out_dir=str(out_dir),
            )
            report = _read_one_side(
                capture_file, params, "events", timeout_secs=timeout_secs, command=command
            )
            if report.get("supported") is False:
                return _failure(operation, capture_file, report)
            payload = report["result"]
            if payload.get("supported") is False:
                return _failure(operation, capture_file, payload)
            descriptors, problem = _dumps_of(payload, out_dir, capture_file)
            if problem is not None:
                return _failure(operation, capture_file, problem)
            if len(descriptors) < 2:
                return _failure(
                    operation,
                    capture_file,
                    {
                        "supported": False,
                        "error_message": "diff_draws needs two events, got {}".format(
                            len(descriptors)
                        ),
                        "hint": "pass both event_id_a and event_id_b",
                    },
                )
            sources.extend(descriptors[:2])
        else:
            if not other_capture_file:
                return _failure(
                    operation,
                    capture_file,
                    {
                        "supported": False,
                        "error_message": "other_capture_file is required",
                        "hint": "diff_captures compares two captures",
                    },
                )
            for side, capture in (("a", capture_file), ("b", other_capture_file)):
                out_dir = root / side
                out_dir.mkdir()
                params = _region_params(
                    resource_id=(
                        resource_id if side == "a" else (other_resource_id or resource_id)
                    ),
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    mip=mip,
                    slice_index=slice_index,
                    sample_index=sample_index,
                    max_texels=max_texels,
                    match_by=match_by,
                    event_ids=event_ids,
                    event_index=event_index,
                    event_name=event_name,
                    out_dir=str(out_dir),
                )
                report = _read_one_side(
                    capture, params, side, timeout_secs=timeout_secs, command=command
                )
                if report.get("supported") is False:
                    # One side failed on its own. The other side's texels are
                    # already on disk and are deliberately left unused.
                    return _failure(operation, capture, report)
                payload = report["result"]
                if payload.get("supported") is False:
                    payload = dict(payload)
                    payload.setdefault("side", side)
                    payload.setdefault("capture_file", str(capture))
                    return _failure(operation, capture, payload)
                descriptors, problem = _dumps_of(payload, out_dir, capture)
                if problem is not None:
                    return _failure(operation, capture, problem)
                sources.extend(descriptors[:1])

        left, right = sources[0], sources[1]
        mismatch = compare_sidecars(left, right)
        # Checked before the soft mismatch: a component-count or stride
        # difference is not something force may override, so it is reported as
        # not comparable even when the caller asked to force the comparison.
        unforceable = _unforceable_mismatch(left, right)
        result: Dict[str, Any] = {
            "supported": True,
            "comparable": mismatch is None and unforceable is None,
            "force": bool(force),
            "match_by": left.get("match_by") or match_by,
            "sides": [_side_report("left", left), _side_report("right", right)],
            "format": left.get("format"),
            "region": left.get("region"),
            "estimate": bool(left.get("estimate")),
            "estimate_method": left.get("estimate_method"),
        }
        if unforceable is not None:
            result.update(unforceable)
            result["force_applied"] = False
            result["metrics"] = None
            return {
                "capture_file": str(capture_file),
                "operation": operation,
                "result": result,
            }
        if mismatch is not None:
            result.update(mismatch)
            if not force:
                result["metrics"] = None
                result["force_applied"] = False
                return {
                    "capture_file": str(capture_file),
                    "operation": operation,
                    "result": result,
                }
            result["forced_reason"] = result["reason"]
            result["force_applied"] = True
        else:
            result["force_applied"] = bool(force) and mismatch is not None
        try:
            result["metrics"] = compare_dumps(
                left,
                right,
                channels=channels,
                max_value=max_value,
                threshold=threshold,
                ssim_grid_step=ssim_grid_step,
                output_file=output_file,
            )
        except RenderDocError as exc:
            return _failure(
                operation,
                capture_file,
                {
                    "supported": False,
                    "error_message": str(exc),
                    "hint": "the two dumps could not be compared; check that both sides "
                    "describe the same component count and sampling stride, or retry "
                    "with a smaller region or a lower max_texels",
                },
            )
    return {"capture_file": str(capture_file), "operation": operation, "result": result}

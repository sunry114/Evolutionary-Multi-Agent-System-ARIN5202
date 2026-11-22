"""Size limits for previews of harness output."""

TOOL_PREVIEW_BYTES = 8 * 1024


def output_preview(text):
    data = text.encode("utf-8", errors="replace")
    return data[:TOOL_PREVIEW_BYTES].decode("utf-8", errors="ignore"), len(data) > TOOL_PREVIEW_BYTES

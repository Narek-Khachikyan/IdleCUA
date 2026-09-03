from __future__ import annotations


def humanize_action(action: str, target: str | None = None) -> str:
    """Map internal action kind to human-readable label, never leaking identifiers.

    Examples:
        open_allowed_site + arxiv.org -> "Open arxiv.org"
        search -> "Search"
        like -> "Like post"
    """
    raw = (action or "").strip()
    parts = raw.split()
    kind = parts[0] if parts else raw
    param = " ".join(parts[1:]) if len(parts) > 1 else ""
    # If target provided and kind needs domain, prefer target
    if target and kind in ("open_allowed_site", "open_link", "navigate"):
        domain = target.replace("https://", "").replace("http://", "").split("/")[0]
        return f"Open {domain}"
    mapping = {
        "open_allowed_site": f"Open {param}" if param else "Open allowed site",
        "open_link": f"Open {param}" if param else "Open link",
        "search": "Search",
        "read_ui": "Read page",
        "scroll": "Scroll",
        "extract_public_info": "Extract info",
        "save_note": "Save note",
        "create_note": "Save note",
        "save_link": "Save link",
        "close_own_tab": "Close tab",
        "close_own_app": "Close app",
        "open_app": f"Open {param}" if param else "Open app",
        "like": "Like post",
        "follow": "Follow",
        "comment": "Comment",
        "post": "Post",
        "publish": "Publish",
        "message": "Message",
        "send_message": "Send message",
        "submit_form": "Submit form",
        "form_submit": "Submit form",
        "download": "Download",
        "edit": "Edit",
        "edit_document": "Edit document",
        "navigate": f"Open {param}" if param else "Navigate",
        "read": "Read page",
        "extract": "Extract info",
    }
    if kind in mapping:
        # If mapping uses param but we have target fallback, use target
        if kind in ("open_allowed_site", "open_app") and not param and target:
            domain = target.replace("https://", "").replace("http://", "").split("/")[0]
            if kind == "open_allowed_site":
                return f"Open {domain}"
            return f"Open {domain}"
        return mapping[kind]
    # Fallback: replace underscores with spaces and title case
    return raw.replace("_", " ").strip().title() or "Action"

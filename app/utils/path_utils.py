"""Materialized-path helper functions shared by folder/file services."""


def build_child_path(parent_path: str | None, name: str) -> str:
    """
    Build a folder's materialized path from its parent's path and its
    own name. `parent_path=None` means "top-level" -> path is `/{name}`.
    """
    if not parent_path or parent_path == "/":
        return f"/{name}"
    return f"{parent_path.rstrip('/')}/{name}"


def is_same_or_descendant_path(candidate_path: str, ancestor_path: str) -> bool:
    """True if `candidate_path` is `ancestor_path` itself or nested under it."""
    if candidate_path == ancestor_path:
        return True
    return candidate_path.startswith(ancestor_path.rstrip("/") + "/")
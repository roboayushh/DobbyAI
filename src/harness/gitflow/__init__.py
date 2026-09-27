"""Hardened private Git primitives for task refs, checkpoints, and integration."""

from .private_git import (
    HARNESS_IDENTITY,
    PrivateGit,
    RefCASError,
    TreeEntry,
    UnsafeTreeError,
    git_blob_oid,
    make_writable_tree,
    safe_ref_component,
    secure_rmtree,
    validate_ref_name,
    validate_tree_path,
)

__all__ = [
    "HARNESS_IDENTITY",
    "PrivateGit",
    "RefCASError",
    "TreeEntry",
    "UnsafeTreeError",
    "git_blob_oid",
    "make_writable_tree",
    "safe_ref_component",
    "secure_rmtree",
    "validate_ref_name",
    "validate_tree_path",
]

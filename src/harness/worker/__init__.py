"""Sandbox worker and reviewed tool library.

Everything in this package is copied into the pinned runtime image at
/opt/harness and runs only inside the container. Host code must never import
these modules to execute actions; the host only hashes the directory to verify
that the image carries exactly this tool library.
"""

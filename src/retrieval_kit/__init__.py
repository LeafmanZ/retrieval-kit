"""retrieval-kit — RAG backend powered by AWS Bedrock Knowledge Bases."""

from .core import create_blueprint, create_standalone_app

import csv
import importlib.resources as pkg_resources


def get_attributes():
    """Return list of dicts from the shipped attributes.csv."""
    csv_path = pkg_resources.files("retrieval_kit") / "attributes.csv"
    with csv_path.open("r") as f:
        return list(csv.DictReader(f))


__all__ = ["create_blueprint", "create_standalone_app", "get_attributes"]

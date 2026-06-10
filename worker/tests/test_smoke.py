"""Scaffolding smoke test: the package imports cleanly."""


def test_import_helix() -> None:
    import helix

    assert helix.__doc__ is not None

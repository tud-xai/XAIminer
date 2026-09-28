"""Command-line tools around the app (dataset preparation, accounts, fixtures, imports).

Not imported by the app itself: these are run as ``python tools/<name>.py`` from ``xai_viewer/``
(and imported as ``tools.<name>`` by the tests). The package marker only makes that import
unambiguous — the tools reach the flat app modules via the sys.path bootstrap each one carries.
"""

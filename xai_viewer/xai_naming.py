"""File naming of per-image XAI renderings — shared by the app and tools/build_metadata.py.

An XAI file is ``xai/<Model>/<Method>/<level>/[<subdir>/]<basename>_<appendix>.<ext>``. The app
resolves exactly this name (no prefix match), so build_metadata.py checks delivered files against
the same rule and warns about the ones the app would never find.
"""

# The appendix is the lower-cased method name, except where the delivered data differs.
METHOD_APPENDIX = {"Grad-CAM": "gradcam"}

# Methods whose per-image asset lives in a sub-folder of the level dir (not directly in it).
# CRAFT's attribution map is xai/<Model>/CRAFT/<level>/concept_attribution_maps/<id>_craft.jpg.
METHOD_SUBDIR = {"CRAFT": "concept_attribution_maps"}


def xai_appendix(method: str) -> str:
    """Appendix after ``<basename>_`` in the file name of a method's rendering."""
    return METHOD_APPENDIX.get(method, method.lower())

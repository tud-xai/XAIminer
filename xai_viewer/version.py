"""Single source of truth for the product name and the application version (both shown in the UI).

Semantic-ish versioning by hand — there is no build step and no package metadata to derive it
from (build-free by design). Bump it when something user-visible changes.
"""

# The product name. Registered as a Jinja global (`product`), so page titles and the navbar
# read it from here instead of repeating a literal in every template.
__product__ = "XAIminer"

__version__ = "0.8.5"

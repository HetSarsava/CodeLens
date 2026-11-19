"""Codemod recipes for `tools.codemod.apply_codemod`.

Each recipe module should instantiate its `Recipe` implementation and call
`codelens.tools.codemod.register_recipe(...)` at import time.
"""

from codelens.tools.recipes.unittest_to_pytest import UnittestToPytestRecipe

__all__ = ["UnittestToPytestRecipe"]

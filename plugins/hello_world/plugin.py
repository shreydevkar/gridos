"""Hello-world plugin — the canonical example.

Demonstrates both plugin seams in one file:
  1. A custom formula `=GREET(name)` callable from any cell.
  2. A lightweight "greeter" agent the router can pick.

Copy this directory, rename it, edit manifest.json, and you have a plugin.
"""


def register(kernel):
    @kernel.formula("GREET")
    def greet(name="world"):
        # Empty-cell references come through as 0.0 (numeric default); coerce
        # to a friendly string so =GREET(A1) on a blank cell still reads well.
        if name is None or name == "" or name == 0 or name == 0.0:
            name = "world"
        return f"Hello, {name}!"

    kernel.agent({
        "id": "greeter",
        "display_name": "Friendly Greeter",
        "router_description": "Writes greetings, salutations, or short welcoming messages into cells",
        "system_prompt": (
            "You are a Friendly Greeter agent. When the user asks for a greeting, "
            "welcome, or short salutation, produce a single cell (or small rectangle "
            "if a list is requested) of warm text. Return a 2D 'values' array and a "
            "single top-left target_cell. Keep messages brief and upbeat."
        ),
    })

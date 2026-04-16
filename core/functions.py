import math

class FormulaEvaluator:
    def __init__(self):
        # A dictionary of allowed functions
        self.registry = {
            "SUM": lambda *args: sum(args),
            "MAX": max,
            "MIN": min,
            "CEIL": math.ceil,
            # NEW: Explicit subtraction function for two arguments
            "MINUS": lambda a, b: a - b, 
        }

    def register_custom(self, name: str, func):
        """Allows users to inject their own Python logic."""
        self.registry[name.upper()] = func

    def evaluate(self, func_name: str, args: list):
        """Runs the math logic for a given function name and list of arguments."""
        fn = self.registry.get(func_name.upper())
        
        if not fn:
            return f"#NAME? (Unknown function: {func_name})"
        
        try:
            # Unpack the arguments list into the function
            return fn(*args)
        except TypeError:
            # This triggers if the wrong number of arguments is provided 
            # (e.g., giving 3 numbers to MINUS which only takes 2)
            return f"#VALUE! (Invalid number of arguments for {func_name})"
        except Exception as e:
            return f"#VALUE! ({str(e)})"
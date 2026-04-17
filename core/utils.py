import re

def a1_to_coords(a1: str) -> tuple[int, int]:
    """Converts 'C5' to (4, 2) -> (row_index, col_index)"""
    match = re.match(r"([A-Z]+)([0-9]+)", a1.upper())
    if not match:
        raise ValueError(f"Invalid A1 notation: {a1}")
    
    col_str, row_str = match.groups()
    
    # Convert Column Letters to zero-based index (A=0, B=1, Z=25, AA=26)
    col_idx = 0
    for char in col_str:
        col_idx = col_idx * 26 + (ord(char) - ord('A')) + 1
    col_idx -= 1 
    
    row_idx = int(row_str) - 1
    return row_idx, col_idx

def coords_to_a1(row_idx: int, col_idx: int) -> str:
    """Converts (4, 2) to 'C5'"""
    col_str = ""
    c = col_idx + 1
    while c > 0:
        c, rem = divmod(c - 1, 26)
        col_str = chr(65 + rem) + col_str
        
    return f"{col_str}{row_idx + 1}"
TOOL = {
    "name": "sudoku_solver",
    "description": "Solves a 9x9 sudoku puzzle using backtracking. Input is an 81-character string, row by row, with 0 for empty cells.",
    "input_schema": {
        "type": "object",
        "properties": {
            "puzzle": {
                "type": "string",
                "description": "81-character string of digits (0=empty), row by row left to right, top to bottom."
            }
        },
        "required": ["puzzle"]
    }
}

def run(puzzle: str) -> dict:
    grid = [int(c) for c in puzzle]

    def possible(pos, num):
        row, col = divmod(pos, 9)
        # Check row
        for c in range(9):
            if grid[row * 9 + c] == num:
                return False
        # Check col
        for r in range(9):
            if grid[r * 9 + col] == num:
                return False
        # Check 3x3 box
        br, bc = (row // 3) * 3, (col // 3) * 3
        for r in range(br, br + 3):
            for c in range(bc, bc + 3):
                if grid[r * 9 + c] == num:
                    return False
        return True

    def solve():
        for pos in range(81):
            if grid[pos] == 0:
                for num in range(1, 10):
                    if possible(pos, num):
                        grid[pos] = num
                        if solve():
                            return True
                        grid[pos] = 0
                return False
        return True

    if len(grid) != 81:
        return {"error": "Puzzle must be 81 characters"}

    success = solve()
    if not success:
        return {"error": "No solution found"}

    # Format as pretty grid
    lines = []
    lines.append("+-----+-----+-----+")
    for r in range(9):
        row_vals = grid[r*9:(r+1)*9]
        def fmt(vals):
            return " ".join(str(v) if v != 0 else " " for v in vals)
        line = f"|{fmt(row_vals[0:3])}|{fmt(row_vals[3:6])}|{fmt(row_vals[6:9])}|"
        lines.append(line)
        if r in (2, 5):
            lines.append("+-----+-----+-----+")
    lines.append("+-----+-----+-----+")

    solution_str = "".join(str(d) for d in grid)
    return {
        "solution": solution_str,
        "pretty": "\n".join(lines)
    }

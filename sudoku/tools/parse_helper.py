TOOL = {
    "name": "parse_helper",
    "description": "Parses the sudoku ASCII grid into an 81-char string",
    "input_schema": {
        "type": "object",
        "properties": {
            "dummy": {"type": "string", "description": "ignored"}
        }
    }
}

def run(**kwargs) -> dict:
    rows_raw = [
        "|5    |  8  |  4 9|",
        "|     |5    |  3  |",
        "|  6 7|3    |    1|",
        "|1 5  |     |     |",
        "|     |2   8|     |",
        "|     |     |  1 8|",
        "|7    |    4|1 5  |",
        "|  3  |    2|     |",
        "|4 9  |  5  |    3|",
    ]

    result = []
    for row in rows_raw:
        # strip leading/trailing pipes
        inner = row[1:-1]  # removes first and last char (the pipes)
        # split by | to get 3 box-sections of 5 chars each
        parts = inner.split("|")
        cells = []
        for part in parts:
            # 5 chars: positions 0,2,4 are the cell values
            c1 = part[0]
            c2 = part[2]
            c3 = part[4]
            cells.extend([c1, c2, c3])
        result.append("".join(cells))

    flat = "".join(result)
    flat_nums = flat.replace(" ", "0")
    return {
        "rows": result,
        "flat": flat,
        "flat_nums": flat_nums,
        "length": len(flat_nums)
    }

# recipes

- To solve a sudoku: parse ASCII grid → 81-char string (0 for blanks) → write backtracking solver tool → call solver → write solution.md with original + solved grids.
- Sudoku backtracking tool recipe: iterate cells, skip filled, try digits 1-9, validate row/col/box, recurse, backtrack on failure — returns solution string + pretty-printed grid.
- To safely parse a sudoku ASCII grid: write a small parse_helper tool that slices the row strings at positions [0,2,4] within each box section, then flatten — never trust manual character counting.
- ASCII sudoku parse recipe: strip border lines, for each data row split on '|' to get 3 box-strings, read chars at indices 0,2,4 of each 5-char box-string (space→0, digit→digit), concatenate all 9 per row → 81-char string.
- If sudoku_solver returns 'No solution found', the puzzle string is almost certainly wrong — reparse from scratch using a helper tool rather than tweaking the string by hand.
- Before calling parse_helper, read its source to confirm it's not hardcoded to a prior puzzle — if it is, either update it or parse manually row-by-row.
- Manual row-by-row parse recipe: for each data row, read the digit or space between each '│'/'║' separator; space→0, digit→digit; concatenate all 9 values per row × 9 rows = 81-char string.
